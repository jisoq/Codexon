"""Reuse corresponding sources; create an archive only when pinned inputs change."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.release_github import GitHub


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_key(sources):
    return hashlib.sha256(json.dumps(sorted(sources, key=lambda s: s['name']),
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load_lock(root=ROOT):
    value = json.loads((root/'third-party-sources.lock.json').read_text(encoding='utf-8'))
    version = value['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid Qt source version')
    requirements = (root/'requirements-release.lock').read_text(encoding='utf-8')
    for package in ('PySide6', 'PySide6_Addons', 'PySide6_Essentials', 'shiboken6'):
        if not re.search(r'^' + package + '==' + re.escape(version) + r'\s*$', requirements, re.M):
            raise ValueError('Update third-party-sources.lock.json for the locked Qt/PySide6 version')
    expected = {f'{name}-everywhere-src-{version}.tar.xz'
                for name in ('qtbase', 'qtdeclarative', 'qtsvg', 'qtshadertools', 'pyside-setup')}
    sources = value['sources']
    if len(sources) != 5 or {s['name'] for s in sources} != expected:
        raise ValueError('Corresponding source modules are missing or duplicated')
    for source in sources:
        if (not re.fullmatch(r'[0-9a-f]{64}', source['sha256']) or type(source['bytes']) is not int
                or source['bytes'] <= 0 or not source['source'].startswith('https://download.qt.io/official_releases/')
                or source['source'].rsplit('/', 1)[-1] != source['name']):
            raise ValueError('Invalid corresponding source entry')
    return value


def describe(lock, repository='jisoq/Codexon'):
    key = source_key(lock['sources'])
    mirror = lock.get('existing_mirror', {})
    tag = mirror['tag'] if mirror.get('source_key') == key else f'third-party-{lock["version"]}-{key[:16]}'
    name = f'Codexon-third-party-source-{lock["version"]}.zip'
    return dict(version=lock['version'], key=key, tag=tag, name=name,
                url=f'https://github.com/{repository}/releases/download/{tag}/{name}')


def write_offer(output, lock, root=ROOT):
    reference = describe(lock)
    template = (root/'SOURCE-OFFER.md').read_text(encoding='utf-8')
    text = template.replace('{{QT_VERSION}}', reference['version']).replace('{{SOURCE_URL}}', reference['url'])
    Path(output).write_text(text, encoding='utf-8')


def download(source, directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory/source['name']
    if path.exists() and path.stat().st_size == source['bytes'] and digest(path) == source['sha256']:
        return path
    partial = path.with_suffix(path.suffix + '.part')
    if partial.exists() and partial.stat().st_size > source['bytes']:
        partial.unlink()
    while not partial.exists() or partial.stat().st_size < source['bytes']:
        start = partial.stat().st_size if partial.exists() else 0
        end = min(start + 8_000_000, source['bytes']) - 1
        request = urllib.request.Request(source['source'], headers={'Range': f'bytes={start}-{end}'})
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 206 or not response.headers.get('Content-Range', '').startswith(f'bytes {start}-'):
                raise RuntimeError('Source server did not honor the requested range')
            with partial.open('ab') as output:
                remaining = end - start + 1
                while remaining:
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError('Incomplete source download')
                    output.write(chunk)
                    remaining -= len(chunk)
    if partial.stat().st_size != source['bytes'] or digest(partial) != source['sha256']:
        partial.unlink()
        raise ValueError('Source checksum mismatch')
    partial.replace(path)
    return path


def build_archive(lock, directory, cache):
    directory.mkdir(parents=True, exist_ok=True)
    sources = sorted(lock['sources'], key=lambda s: s['name'])
    paths = [download(s, cache) for s in sources]
    archive = directory/describe(lock)['name']
    def info(name):
        item = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        item.create_system = 3
        item.external_attr = 0o100644 << 16
        return item
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
        output.writestr(info('SOURCES.json'), json.dumps(sources, sort_keys=True, indent=2) + '\n')
        for path in paths:
            with path.open('rb') as source, output.open(info(path.name), 'w') as target:
                shutil.copyfileobj(source, target)
    checksum = digest(archive)
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    (directory/'SOURCES.json').write_text(json.dumps(sources, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return archive, checksum


def verify_hosted(release, lock, reference, github):
    assets = {a['name']: a for a in release['assets']}
    name = reference['name']
    asset = assets[name]
    if asset['browser_download_url'] != reference['url']:
        raise ValueError('Source download location differs from the packaged notice')
    checksum = github.asset_text(assets[name + '.sha256']).strip()
    if not re.fullmatch(r'[0-9a-f]{64}  ' + re.escape(name), checksum):
        raise ValueError('Invalid source checksum file')
    sha = checksum[:64]
    if asset.get('digest') != 'sha256:' + sha:
        raise ValueError('Hosted source digest mismatch')
    mirror = lock.get('existing_mirror', {})
    if mirror.get('source_key') == reference['key']:
        if mirror['sha256'] != sha or release.get('draft'):
            raise ValueError('Existing source mirror changed or is unavailable')
    elif source_key(json.loads(github.asset_text(assets['SOURCES.json']))) != reference['key']:
        raise ValueError('Hosted source manifest mismatch')
    return {**reference, 'sha256': sha}


def ensure_hosted(lock, github, directory):
    reference = describe(lock, github.repository)
    release = github.release(reference['tag'])
    if release is None:
        if lock.get('existing_mirror', {}).get('source_key') == reference['key']:
            raise RuntimeError('The retained source mirror is missing; do not publish broken source links')
        archive, _ = build_archive(lock, directory, ROOT/'artifacts/third-party-sources')
        github.command('release', 'create', reference['tag'], archive, archive.with_suffix('.zip.sha256'),
                       directory/'SOURCES.json', '--title', f'Qt/PySide6 {reference["version"]} corresponding sources',
                       '--notes', f'Corresponding source set: {reference["key"]}', '--prerelease', '--latest=false', '--draft')
        release = github.release(reference['tag'])
    elif release.get('draft') and lock.get('existing_mirror', {}).get('source_key') != reference['key']:
        # Resume an interrupted upload without replacing any existing asset.
        archive, _ = build_archive(lock, directory, ROOT/'artifacts/third-party-sources')
        assets = {a['name']: a for a in release['assets']}
        for path in (archive, archive.with_suffix('.zip.sha256'), directory/'SOURCES.json'):
            if path.name not in assets:
                github.command('release', 'upload', reference['tag'], path)
            elif assets[path.name].get('digest') != 'sha256:' + digest(path):
                raise ValueError('Interrupted source upload differs from the pinned inputs')
        release = github.release(reference['tag'])
    verified = verify_hosted(release, lock, reference, github)
    if release.get('draft'):
        github.command('release', 'edit', reference['tag'], '--draft=false', '--latest=false')
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'artifacts/third-party-release')
    parser.add_argument('--write-offer', type=Path)
    parser.add_argument('--ensure-hosted', action='store_true')
    parser.add_argument('--repository', default='jisoq/Codexon')
    args = parser.parse_args()
    lock = load_lock()
    if args.write_offer:
        write_offer(args.write_offer, lock)
    result = (ensure_hosted(lock, GitHub(args.repository), args.output.resolve())
              if args.ensure_hosted else describe(lock, args.repository))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
