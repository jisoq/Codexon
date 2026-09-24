"""Create and inspect the explicit public Windows payload."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

REQUIRED = {'Codexon.exe', 'CodexonRecovery.exe', 'CodexonHook.exe','build-manifest.json', 'LICENSE',
            'THIRD-PARTY-NOTICES.md', 'BUNDLED-PYTHON.md', 'BUNDLED-QT.md', 'SOURCE-OFFER.md',
            'USER-GUIDE.md', 'USER-GUIDE.ko.md'}
MANIFEST_KEYS = {'product', 'version', 'commit', 'architecture', 'executable', 'sha256', 'recovery_sha256'}
FORBIDDEN_NAMES = {'.env', 'config.toml', 'auth.json'}
FORBIDDEN_SUFFIXES = {'.db', '.sqlite', '.jsonl', '.log', '.ini', '.zip', '.sha256'}
PRIVATE_PATH = re.compile(rb'(?i)(?:[a-z]:\\(?:users|repos)\\|/users/[^/\r\n]+/|\\users\\[^\\\r\n]+\\)')


def payload(folder: Path):
    folder = folder.resolve(strict=True)
    if folder.name != 'Codexon':
        raise ValueError('Expected the Codexon distribution folder')
    paths = sorted(path for path in folder.rglob('*') if path.is_file())
    if not REQUIRED <= {path.name for path in paths if path.parent == folder}:
        raise ValueError('Distribution is missing a required top-level file')
    for path in paths:
        if path.is_symlink():
            raise ValueError(f'Symlink in distribution: {path}')
        rel = path.relative_to(folder).as_posix()
        if path.parent != folder and not rel.startswith(('_internal/', 'LICENSES/')):
            raise ValueError(f'Unexpected distribution path: {rel}')
        if path.name.lower() in FORBIDDEN_NAMES or (path.suffix.lower() in FORBIDDEN_SUFFIXES
                and rel != '_internal/base_library.zip'):
            raise ValueError(f'Private or diagnostic file in distribution: {rel}')
        if any(part.lower() in {'artifacts', 'screenshots', 'captures', '.codex'} for part in path.parts):
            raise ValueError(f'Diagnostic directory in distribution: {rel}')
        if path.suffix.lower() in {'.json', '.toml', '.txt', '.md', '.qml'} and path.stat().st_size < 5_000_000:
            if PRIVATE_PATH.search(path.read_bytes()):
                raise ValueError(f'Local absolute path in distribution: {rel}')
    manifest = json.loads((folder/'build-manifest.json').read_text(encoding='utf-8-sig'))
    if set(manifest) != MANIFEST_KEYS or manifest['product'] != 'Codexon':
        raise ValueError('Public manifest has unexpected fields')
    if manifest['architecture'] != 'x64' or not re.fullmatch(r'[0-9a-f]{40}', manifest['commit']):
        raise ValueError('Public manifest architecture or commit is invalid')
    if manifest['executable'] != 'Codexon.exe':
        raise ValueError('Public manifest executable must be relative')
    exe_hash = hashlib.sha256((folder/'Codexon.exe').read_bytes()).hexdigest()
    if manifest['sha256'].lower() != exe_hash:
        raise ValueError('Public manifest executable hash mismatch')
    if hashlib.sha256((folder/'CodexonRecovery.exe').read_bytes()).hexdigest()!=manifest['recovery_sha256']:
        raise ValueError('Public manifest recovery hash mismatch')
    dlls = {p.name.lower() for p in paths if p.suffix.lower() == '.dll'}
    banned = ('qt63d', 'qt6charts', 'qt6graphs', 'qt6datavisualization',
              'qt6quick3d', 'qt6quicktimeline', 'qt6virtualkeyboard',
              'qt6labswavefrontmesh')
    if any(name.startswith(banned) for name in dlls):
        raise ValueError('Unused GPL-only Qt module found in distribution')
    forbidden_qml = ('/qtcharts/', '/qtgraphs/', '/qtdatavisualization/',
                     '/qtquick3d/', '/qtvirtualkeyboard/', '/qtquick/timeline/')
    if any(any(mark in '/' + path.relative_to(folder).as_posix().lower()
                   for mark in forbidden_qml) for path in paths):
        raise ValueError('Unused GPL-only QML module found in distribution')
    return folder, paths, manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--product', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    folder, paths, manifest = payload(args.product)
    if args.validate_only:
        print(json.dumps({'validated': True, 'files': len(paths), 'source_commit': manifest['commit']}))
        return
    if args.output is None:
        parser.error('--output is required unless --validate-only is used')
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in paths:
            archive.write(path, 'Codexon/' + path.relative_to(folder).as_posix())
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        if len(names) != len(paths) or len(names) != len(set(names)) or archive.testzip():
            raise ValueError('ZIP file list or integrity mismatch')
        packed_manifest = json.loads(archive.read('Codexon/build-manifest.json').decode('utf-8-sig'))
        if packed_manifest != manifest:
            raise ValueError('ZIP manifest differs from the checked product')
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_name(output.name + '.sha256')
    checksum.write_text(f'{digest}  {output.name}\n', encoding='ascii')
    print(json.dumps({'zip': str(output), 'files': len(paths), 'sha256': digest,
                      'source_commit': manifest['commit']}))


if __name__ == '__main__':
    main()
