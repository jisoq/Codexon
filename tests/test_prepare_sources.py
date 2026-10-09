import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest
from tools import prepare_sources as sources


def fake_lock():
    data = b'corresponding source'
    return dict(version='6.11.2', sources=[
        dict(name='source.tar.xz', source='https://download.qt.io/official_releases/source.tar.xz',
             bytes=len(data), sha256=hashlib.sha256(data).hexdigest())]), data


def hosted(lock, *, mirror=False, draft=False):
    reference = sources.describe(lock)
    sha = lock['existing_mirror']['sha256'] if mirror else 'a' * 64
    assets = [
        dict(name=reference['name'], digest='sha256:' + sha, browser_download_url=reference['url']),
        dict(name=reference['name'] + '.sha256', text=f'{sha}  {reference["name"]}\n'),
        dict(name='SOURCES.json', text=json.dumps(lock['sources'])),
    ]
    return dict(draft=draft, assets=assets)


class FakeGitHub:
    repository = 'jisoq/Codexon'
    def __init__(self, release):
        self.value = release
        self.commands = []
    def release(self, tag):
        return self.value
    def asset_text(self, asset):
        return asset['text']
    def command(self, *args):
        self.commands.append(args)


def test_existing_mirror_is_reused_without_source_download_or_upload(tmp_path, monkeypatch):
    lock = sources.load_lock()
    github = FakeGitHub(hosted(lock, mirror=True))
    monkeypatch.setattr(sources, 'build_archive', lambda *args: pytest.fail('Unexpected archive build'))
    result = sources.ensure_hosted(lock, github, tmp_path)
    assert result['tag'] == 'v2026.10.10.1'
    assert not github.commands and not list(tmp_path.iterdir())


def test_source_identity_and_archive_ignore_file_order_and_mtime(tmp_path, monkeypatch):
    lock, data = fake_lock()
    second = {**lock['sources'][0], 'name': 'another.tar.xz'}
    lock['sources'].append(second)
    cache = tmp_path/'cache'
    cache.mkdir()
    for entry in lock['sources']:
        (cache/entry['name']).write_bytes(data)
    monkeypatch.setattr(sources.urllib.request, 'urlopen', lambda *a, **k: pytest.fail('Unexpected download'))
    first, first_sha = sources.build_archive(lock, tmp_path/'first', cache)
    lock['sources'].reverse()
    for path in cache.iterdir():
        os.utime(path, (1_700_000_000, 1_700_000_000))
    second, second_sha = sources.build_archive(lock, tmp_path/'second', cache)
    assert first.read_bytes() == second.read_bytes() and first_sha == second_sha
    assert sources.source_key(lock['sources']) == sources.source_key(list(reversed(lock['sources'])))


@pytest.mark.parametrize('failure', ['missing', 'digest', 'manifest', 'mirror'])
def test_missing_or_mismatched_source_blocks_publication(tmp_path, failure):
    lock, _ = fake_lock()
    release = hosted(lock)
    if failure == 'missing':
        lock['existing_mirror'] = dict(source_key=sources.source_key(lock['sources']), tag='v1', sha256='a' * 64)
        release = None
    elif failure == 'digest':
        release['assets'][0]['digest'] = 'sha256:' + 'b' * 64
    elif failure == 'manifest':
        release['assets'][2]['text'] = '[]'
    else:
        lock['existing_mirror'] = dict(source_key=sources.source_key(lock['sources']), tag=sources.describe(lock)['tag'], sha256='b' * 64)
    github = FakeGitHub(release)
    with pytest.raises((ValueError, RuntimeError)):
        sources.ensure_hosted(lock, github, tmp_path)
    assert not github.commands


def test_new_sources_publish_once_and_do_not_become_latest(tmp_path, monkeypatch):
    lock, data = fake_lock()
    cache = tmp_path/'cache'
    cache.mkdir()
    (cache/'source.tar.xz').write_bytes(data)
    original = sources.build_archive
    monkeypatch.setattr(sources, 'build_archive', lambda lock, directory, unused: original(lock, directory, cache))
    class PublishingGitHub(FakeGitHub):
        def command(self, *args):
            super().command(*args)
            if args[:2] == ('release', 'create'):
                reference = sources.describe(lock)
                files = args[3:6]
                self.value = dict(draft=True, assets=[
                    dict(name=p.name, digest='sha256:' + sources.digest(p), text=p.read_text() if p.suffix != '.zip' else '',
                         browser_download_url=reference['url'] if p.suffix == '.zip' else '') for p in files])
            elif args[:2] == ('release', 'edit'):
                self.value['draft'] = False
    github = PublishingGitHub(None)
    first = sources.ensure_hosted(lock, github, tmp_path/'output')
    count = len(github.commands)
    second = sources.ensure_hosted(lock, github, tmp_path/'unused')
    assert first == second and len(github.commands) == count
    create = github.commands[0]
    assert '--prerelease' in create and '--latest=false' in create
    assert first['url'] in sources.describe(lock)['url']


def test_dependency_version_mismatch_fails_before_download(tmp_path):
    shutil.copyfile(sources.ROOT/'third-party-sources.lock.json', tmp_path/'third-party-sources.lock.json')
    (tmp_path/'requirements-release.lock').write_text('PySide6==6.12.0\n')
    with pytest.raises(ValueError, match='locked Qt'):
        sources.load_lock(tmp_path)


def test_packaged_notice_has_a_concrete_retained_source_link(tmp_path):
    output = tmp_path/'SOURCE-OFFER.md'
    sources.write_offer(output, sources.load_lock())
    text = output.read_text(encoding='utf-8')
    assert '{{' not in text and sources.describe(sources.load_lock())['url'] in text
    assert 'docs/verification.md' not in text and 'Each release must include' not in text
