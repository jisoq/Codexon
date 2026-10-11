"""Copy notices from the frozen Python graph and locked native dependencies."""
from __future__ import annotations

import argparse
import ast
import importlib.metadata as metadata
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def rust_dependencies(graph):
    packages = {package['id']: package for package in graph['packages']}
    nodes = {node['id']: node for node in graph['resolve']['nodes']}
    root = graph['resolve']['root']
    runtime, pending = set(), [root]
    while pending:
        current = pending.pop()
        if current in runtime:
            continue
        if any('proc-macro' in target['kind'] for target in packages[current]['targets']):
            continue
        runtime.add(current)
        pending.extend(dependency['pkg'] for dependency in nodes[current]['deps']
                       if any(kind['kind'] is None for kind in dependency['dep_kinds']))
    return [(packages[key], 'Runtime dependency' if key in runtime else 'Build dependency')
            for key in sorted(nodes, key=lambda key: (packages[key]['name'], packages[key]['version']))
            if key != root]


def copy_rust_notices(product, graph, sysroot, version):
    product = Path(product)
    directory = product/'LICENSES/rust'
    rows = rust_dependencies(graph)
    for package, _ in rows:
        source_root = Path(package['manifest_path']).parent.resolve()
        files = {path for path in source_root.rglob('*') if path.is_file()
                 and path.name.lower().startswith(('license', 'licence', 'copying', 'notice', 'unlicense'))}
        if package.get('license_file'):
            files.add(source_root / package['license_file'])
        if not files:
            raise RuntimeError(f"No cached license file for Rust crate {package['name']}")
        for source in sorted(files):
            source = source.resolve(strict=True)
            if not source.is_relative_to(source_root):
                raise RuntimeError(f"Rust license escaped the crate directory: {package['name']}")
            target = directory / f"{package['name']}-{package['version']}" / source.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    standard_library = Path(sysroot)/'share/doc/rust/COPYRIGHT-library.html'
    if not standard_library.is_file():
        raise RuntimeError('The pinned Rust toolchain is missing its standard-library copyright notice')
    std_directory = directory/'standard-library'
    std_directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(standard_library, std_directory/standard_library.name)
    lines = ['# Rust native dependencies', '',
             'This inventory uses the locked Cargo graph for the build target. It includes runtime dependencies and build dependencies for completeness. License files are copied unchanged from the cached crate sources and pinned Rust toolchain.',
             '', '| Package | Version | Role | Declared license |', '| --- | --- | --- | --- |']
    for package, role in rows:
        license_name = (package.get('license') or 'See included license file').replace('|', '/')
        lines.append(f"| {package['name']} | {package['version']} | {role} | {license_name} |")
    lines += [f'| Rust standard library | {version} | Runtime library | Apache-2.0 OR MIT; component-specific notices included |',
              '', 'The corresponding notices are under `LICENSES/rust/`. The standard-library copyright document includes its component notices.', '']
    (product/'BUNDLED-RUST.md').write_text('\n'.join(lines), encoding='utf-8')
    return len(rows)


def collect_rust_notices(product, root=ROOT):
    rustc = subprocess.check_output(['rustc', '--version', '--verbose'], cwd=root, text=True)
    details = dict(line.split(': ', 1) for line in rustc.splitlines() if ': ' in line)
    sysroot = subprocess.check_output(['rustc', '--print', 'sysroot'], cwd=root, text=True).strip()
    graph = json.loads(subprocess.check_output([
        'cargo', 'metadata', '--manifest-path', str(Path(root)/'native/Cargo.toml'),
        '--locked', '--offline', '--format-version', '1', '--filter-platform', details['host'],
    ], cwd=root, text=True))
    return copy_rust_notices(product, graph, sysroot, details['release'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--toc', type=Path, required=True)
    parser.add_argument('--product', type=Path, required=True)
    args = parser.parse_args()
    entries = ast.literal_eval(args.toc.read_text(encoding='utf-8'))[1]
    roots = {name.split('.')[0] for name, *_ in entries}
    packages = metadata.packages_distributions()
    distributions = {name for root in roots for name in packages.get(root, [])}
    # Binary-only modules can have no PYZ entry.
    distributions.update({'PySide6', 'PySide6_Addons', 'PySide6_Essentials', 'shiboken6'})
    directory = args.product / 'LICENSES' / 'python'
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in sorted(distributions, key=str.casefold):
        dist = metadata.distribution(name)
        files = [item for item in (dist.files or [])
                 if any(part in str(item).lower() for part in ('license', 'copying', 'notice'))
                 and dist.locate_file(item).is_file()]
        if not files:
            raise RuntimeError(f'No installed license file for {name}')
        copied = []
        for file in files:
            source = dist.locate_file(file)
            relative = Path(str(file).replace('\\', '/'))
            target = directory / name / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(target.relative_to(args.product).as_posix())
        rows.append((name, dist.version, dist.metadata.get('License-Expression')
                     or dist.metadata.get('License') or 'See included license files', copied))
    lines = ['# Bundled Python packages', '',
             'This list comes from the frozen module graph. License files are copied from the pinned build environment.',
             '', '| Package | Version | Declared license |', '| --- | --- | --- |']
    for name, version, license_name, _ in rows:
        lines.append(f'| {name} | {version} | {license_name.replace("|", "/")} |')
    lines += ['', 'The full corresponding notices are under `LICENSES/python/`. Qt modules require separate review under `LICENSES/` and the source offer.', '']
    (args.product/'BUNDLED-PYTHON.md').write_text('\n'.join(lines), encoding='utf-8')
    qt_root = args.product/'_internal'/'PySide6'
    qt_files = sorted(qt_root.glob('Qt6*.dll'), key=lambda path: path.name.casefold())
    if not qt_files:
        raise RuntimeError('No Qt runtime DLLs found in the packaged product')
    qt_lines = ['# Bundled Qt 6.11.2 modules', '',
                'This is the actual Qt DLL inventory of this release. Qt module terms are checked against the official Qt 6.11.2 licensing documentation. The listed source component is in the matching source release asset.',
                '', '| Runtime DLL | Source component | Open-source terms |', '| --- | --- | --- |']
    for file in qt_files:
        stem = file.stem.lower()
        source = ('qtsvg' if stem.startswith('qt6svg') else
                  'qtshadertools' if stem.startswith('qt6shadertools') else
                  'qtdeclarative' if stem.startswith(('qt6qml', 'qt6quick', 'qt6labs')) else
                  'qtbase')
        qt_lines.append(f'| {file.name} | {source} | LGPL-3.0 / GPL-3.0 |')
    qt_lines += ['', 'The license texts are under `LICENSES/`. Qt may contain independently licensed third-party code; the corresponding source archives include those notices. See the source offer and Qt 6.11.2 third-party component listing.', '']
    (args.product/'BUNDLED-QT.md').write_text('\n'.join(qt_lines), encoding='utf-8')
    rust_count = collect_rust_notices(args.product)
    print(f'{len(rows)} Python distributions; {sum(len(row[3]) for row in rows)} Python license files; {rust_count} Rust crates and standard-library notices')


if __name__ == '__main__':
    main()
