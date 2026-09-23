"""Mirror exact Qt/PySide6 source archives for the binary release."""
from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

VERSION = '6.11.2'
QT_BASE = f'https://download.qt.io/official_releases/qt/6.11/{VERSION}/submodules/'
PYSIDE_BASE = f'https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{VERSION}-src/'
SOURCES = [
    QT_BASE + f'{module}-everywhere-src-{VERSION}.tar.xz'
    for module in ('qtbase', 'qtdeclarative', 'qtsvg', 'qtshadertools')
] + [PYSIDE_BASE + f'pyside-setup-everywhere-src-{VERSION}.tar.xz']


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    directory = Path('artifacts/third-party-sources').resolve()
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for url in SOURCES:
        name = url.rsplit('/', 1)[-1]
        with urllib.request.urlopen(url + '.sha256', timeout=30) as response:
            expected = response.read().decode('ascii').split()[0].lower()
        path = directory / name
        if not path.exists() or digest(path) != expected:
            temporary = path.with_suffix(path.suffix + '.part')
            with urllib.request.urlopen(urllib.request.Request(url, method='HEAD'), timeout=30) as response:
                total = int(response.headers['Content-Length'])
            if temporary.exists() and temporary.stat().st_size > total:
                temporary.unlink()
            while not temporary.exists() or temporary.stat().st_size < total:
                offset = temporary.stat().st_size if temporary.exists() else 0
                end = min(offset + 8_000_000 - 1, total - 1)
                request = urllib.request.Request(url, headers={'Range': f'bytes={offset}-{end}'})
                with urllib.request.urlopen(request, timeout=60) as response:
                    if response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                        raise RuntimeError(f'Source server did not honor range: {name}')
                    with temporary.open('ab') as output:
                        shutil.copyfileobj(response, output)
                if temporary.stat().st_size <= offset:
                    raise RuntimeError(f'Source download did not progress: {name}')
            if digest(temporary) != expected:
                raise ValueError(f'Upstream checksum mismatch: {name}')
            temporary.replace(path)
        entries.append({'name': name, 'sha256': expected, 'source': url, 'bytes': path.stat().st_size})
    output = Path(f'artifacts/release/Codexon-third-party-source-{VERSION}.zip').resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(output)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('SOURCES.json', json.dumps(entries, indent=2) + '\n')
        for entry in entries:
            archive.write(directory / entry['name'], entry['name'])
    with zipfile.ZipFile(output) as archive:
        if archive.testzip():
            raise ValueError('Source ZIP integrity error')
    sha = digest(output)
    output.with_name(output.name + '.sha256').write_text(f'{sha}  {output.name}\n', encoding='ascii')
    print(json.dumps({'source_zip': str(output), 'sha256': sha, 'components': len(entries)}))


if __name__ == '__main__':
    main()
