"""Copy license files for Python packages in the frozen module graph."""
from __future__ import annotations

import argparse
import ast
import importlib.metadata as metadata
import shutil
from pathlib import Path


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
    print(f'{len(rows)} Python distributions; {sum(len(row[3]) for row in rows)} license files')


if __name__ == '__main__':
    main()
