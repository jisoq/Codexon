"""Copy notices from the frozen Python graph and locked native dependencies."""
from __future__ import annotations

import argparse
import ast
import importlib.metadata as metadata
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
import zlib
from pathlib import Path, PurePosixPath

if __package__:
    from . import build_native
else:
    import build_native


ROOT = Path(__file__).resolve().parents[1]
RUST_ARTIFACT_SCHEMA = 1
RUST_ARTIFACT_MANIFEST = 'rust-notices.json'
RUST_STANDARD_NOTICE = 'LICENSES/rust/standard-library/COPYRIGHT-library.html'


class InvalidRustArtifact(RuntimeError):
    pass


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


def collect_rust_notices(product, root=ROOT, *, rust_artifact=None, expected_rustc=None):
    if rust_artifact is not None:
        return copy_rust_artifact(product, rust_artifact, root)
    rustc = subprocess.check_output(['rustc', '--version', '--verbose'], cwd=root, text=True)
    if expected_rustc is not None and rustc.strip() != expected_rustc:
        raise RuntimeError('The Rust notices toolchain differs from the tested native build')
    details = dict(line.split(': ', 1) for line in rustc.splitlines() if ': ' in line)
    sysroot = subprocess.check_output(['rustc', '--print', 'sysroot'], cwd=root, text=True).strip()
    graph = json.loads(subprocess.check_output([
        'cargo', 'metadata', '--manifest-path', str(Path(root)/'native/Cargo.toml'),
        '--locked', '--format-version', '1', '--filter-platform', details['host'],
    ], cwd=root, text=True))
    return copy_rust_notices(product, graph, sysroot, details['release'])


def rust_artifact_identity(root=ROOT):
    root = Path(root).resolve()
    binary = build_native.checked_extension(root, smoke=False, require_tested=True)
    paths = {
        'native_receipt_sha256': build_native.receipt_path(root),
        'native_binary_sha256': binary,
        'cargo_manifest_sha256': root/'native/Cargo.toml',
        'cargo_lock_sha256': root/'native/Cargo.lock',
        'toolchain_sha256': root/'rust-toolchain.toml',
        'collector_sha256': root/'tools/collect_notices.py',
    }
    return {name: build_native.binary_digest(path) for name, path in paths.items()}


def checked_notice_name(name):
    path = PurePosixPath(name)
    if (str(path) != name or path.is_absolute() or '\\' in name or ':' in name
            or any(part in ('.', '..') or part.endswith(('.', ' ')) for part in path.parts)):
        raise InvalidRustArtifact('Rust notices artifact contains an unsafe path')
    if name not in (RUST_ARTIFACT_MANIFEST, 'BUNDLED-RUST.md') and not (
            len(path.parts) >= 4 and path.parts[:2] == ('LICENSES', 'rust')):
        raise InvalidRustArtifact('Rust notices artifact contains an unexpected path')
    return name


def read_rust_artifact(artifact, root=ROOT, *, identity=None):
    expected = rust_artifact_identity(root) if identity is None else identity
    try:
        with zipfile.ZipFile(artifact) as archive:
            members = {}
            seen = set()
            total_size = 0
            for member in archive.infolist():
                name = checked_notice_name(member.orig_filename)
                kind = stat.S_IFMT(member.external_attr >> 16)
                if (member.filename != name or member.flag_bits & 1 or member.is_dir()
                        or kind not in (0, stat.S_IFREG) or name.casefold() in seen):
                    raise InvalidRustArtifact('Rust notices artifact contains duplicate or linked members')
                total_size += member.file_size
                if total_size > 64 * 1024 * 1024 or len(members) >= 4096:
                    raise InvalidRustArtifact('Rust notices artifact exceeds the notice inventory budget')
                seen.add(name.casefold())
                members[name] = member
            if RUST_ARTIFACT_MANIFEST not in members or members[RUST_ARTIFACT_MANIFEST].file_size > 256 * 1024:
                raise InvalidRustArtifact('Rust notices artifact is missing its inventory')
            manifest = json.loads(archive.read(members.pop(RUST_ARTIFACT_MANIFEST)).decode('utf-8'))
            if (not isinstance(manifest, dict) or type(manifest.get('schema')) is not int
                    or manifest['schema'] != RUST_ARTIFACT_SCHEMA):
                raise InvalidRustArtifact('Rust notices artifact has an unsupported inventory')
            mismatches = build_native.identity_mismatches(expected, manifest.get('identity'), exact=True)
            if mismatches:
                raise InvalidRustArtifact('Rust notices artifact is stale; mismatched fields: ' + ', '.join(mismatches))
            files = manifest.get('files')
            if not isinstance(files, dict) or set(files) != members.keys():
                raise InvalidRustArtifact('Rust notices artifact has missing or unexpected notice files')
            if not {'BUNDLED-RUST.md', RUST_STANDARD_NOTICE} <= files.keys():
                raise InvalidRustArtifact('Rust notices artifact is missing required notices')
            contents = {name: archive.read(member) for name, member in members.items()}
            if any(hashlib.sha256(data).hexdigest() != files[name] for name, data in contents.items()):
                raise InvalidRustArtifact('Rust notices artifact has changed notice bytes')
            crates = {PurePosixPath(name).parts[2] for name in files
                      if name.startswith('LICENSES/rust/') and name != RUST_STANDARD_NOTICE}
            count = manifest.get('crate_count')
            if type(count) is not int or count != len(crates):
                raise InvalidRustArtifact('Rust notices artifact has an incomplete crate inventory')
            return contents, count
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, EOFError, zlib.error) as error:
        raise InvalidRustArtifact('Rust notices artifact is missing or corrupt; prepare it in the native build job.') from error


def copy_rust_artifact(product, artifact, root=ROOT):
    contents, count = read_rust_artifact(artifact, root)
    product = Path(product).resolve()
    targets = {}
    for name in contents:
        target = product.joinpath(*PurePosixPath(name).parts).resolve()
        if not target.is_relative_to(product):
            raise InvalidRustArtifact('Rust notice destination escaped the product directory')
        targets[name] = target
    for name, target in targets.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents[name])
    return count


def prepare_rust_artifact(artifact, root=ROOT):
    root = Path(root).resolve()
    artifact = Path(artifact).resolve()
    identity = rust_artifact_identity(root)
    try:
        read_rust_artifact(artifact, root, identity=identity)
        return artifact
    except InvalidRustArtifact:
        pass
    artifact.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='rust-notices-', dir=artifact.parent) as folder:
        staging = Path(folder)/'product'
        receipt = json.loads(build_native.receipt_path(root).read_text(encoding='utf-8'))
        expected_rustc = receipt['identity']['toolchain']['rustc']
        count = collect_rust_notices(staging, root, expected_rustc=expected_rustc)
        if rust_artifact_identity(root) != identity:
            raise InvalidRustArtifact('Native inputs changed while preparing Rust notices')
        contents = {path.relative_to(staging).as_posix(): path.read_bytes()
                    for path in staging.rglob('*') if path.is_file()}
        manifest = {'schema': RUST_ARTIFACT_SCHEMA, 'identity': identity, 'crate_count': count,
                    'files': {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
        contents[RUST_ARTIFACT_MANIFEST] = (json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode('utf-8')
        temporary = Path(folder)/'notices.zip'
        with zipfile.ZipFile(temporary, 'w') as archive:
            for name, data in sorted(contents.items()):
                member = zipfile.ZipInfo(checked_notice_name(name))
                member.compress_type = zipfile.ZIP_DEFLATED
                member.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(member, data)
        read_rust_artifact(temporary, root, identity=identity)
        os.replace(temporary, artifact)
    return artifact


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--toc', type=Path)
    parser.add_argument('--product', type=Path)
    parser.add_argument('--prepare-rust-artifact', type=Path)
    parser.add_argument('--rust-artifact', type=Path)
    args = parser.parse_args(argv)
    if args.prepare_rust_artifact is not None:
        if args.toc is not None or args.product is not None or args.rust_artifact is not None:
            parser.error('--prepare-rust-artifact cannot be combined with product collection')
        artifact = prepare_rust_artifact(args.prepare_rust_artifact)
        print(json.dumps({'rust_artifact': str(artifact), 'sha256': build_native.binary_digest(artifact)}))
        return 0
    if args.toc is None or args.product is None:
        parser.error('--toc and --product are required for product collection')
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
    rust_count = collect_rust_notices(args.product, rust_artifact=args.rust_artifact)
    print(f'{len(rows)} Python distributions; {sum(len(row[3]) for row in rows)} Python license files; {rust_count} Rust crates and standard-library notices')


if __name__ == '__main__':
    main()
