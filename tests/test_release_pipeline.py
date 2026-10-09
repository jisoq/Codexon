import copy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
from tools import release_pipeline as pipeline
from tools import prepare_sources
from tools import verify_release_tools
from tools.verify_changes import select_tests

SHA = 'a' * 40


def run_record(**changes):
    return dict(id=10, head_sha=SHA, head_branch='main', event='push', path=pipeline.WORKFLOW,
                head_repository={'full_name': 'jisoq/Codexon'}, status='completed', conclusion='success', **changes)


class FakeGitHub:
    repository = 'jisoq/Codexon'
    def __init__(self, runs=(), expired=False):
        self.runs = list(runs)
        self.expired = expired
        self.tag = None
        self.released = None
    def api(self, path, **kwargs):
        if path.startswith('git/ref'):
            return {'object': {'type': 'commit', 'sha': self.tag}} if self.tag else None
        if '/artifacts' in path:
            return {'artifacts': [dict(name='verified-release-' + SHA, expired=self.expired)]}
        return {'workflow_runs': self.runs}
    def release(self, tag):
        return self.released


@pytest.fixture
def root(tmp_path):
    (tmp_path/'cachemonitor').mkdir()
    (tmp_path/'cachemonitor/version.py').write_text("VERSION = '2026.10.11.1'\n")
    (tmp_path/'docs/releases').mkdir(parents=True)
    (tmp_path/'docs/releases/2026.10.11.1.md').write_text('## [2026.10.11.1] - 2026-10-11\n')
    for name in ('requirements-release.lock', 'third-party-sources.lock.json'):
        shutil.copyfile(pipeline.ROOT/name, tmp_path/name)
    return tmp_path


def test_release_request_reuses_exact_successful_main_run(root):
    github = FakeGitHub([run_record()])
    result = pipeline.plan(github, SHA, '20', 'workflow_dispatch', 'refs/heads/main', True, root)
    assert result['candidate'] and result['reuse_run'] == '10' and not result['verify']
    assert pipeline.plan(github, SHA, '20', 'push', 'refs/heads/main', False, root)['reuse_run'] == '10'


@pytest.mark.parametrize('changes', [
    {'head_sha': 'b' * 40}, {'event': 'pull_request'}, {'head_branch': 'other'},
    {'path': '.github/workflows/other.yml'}, {'head_repository': {'full_name': 'foreign/repo'}},
    {'conclusion': 'failure'}, {'status': 'in_progress'}, {'id': 20},
])
def test_wrong_failed_pending_or_current_run_is_not_reused(changes):
    record = run_record()
    record.update(changes)
    assert pipeline.reusable_run(FakeGitHub([record]), SHA, '20') == ''


def test_expired_artifact_requires_verification_again(root):
    result = pipeline.plan(FakeGitHub([run_record()], expired=True), SHA, '20', 'workflow_dispatch',
                           'refs/heads/main', True, root)
    assert result['verify'] and not result['reuse_run'] and result['candidate']


def test_existing_release_is_idempotent_but_different_commit_is_rejected(root):
    github = FakeGitHub()
    github.tag = SHA
    github.released = dict(draft=False)
    result = pipeline.plan(github, SHA, '20', 'workflow_dispatch', 'refs/heads/main', True, root)
    assert result['published'] and not result['verify']
    with pytest.raises(ValueError, match='new version'):
        pipeline.plan(github, 'b' * 40, '20', 'workflow_dispatch', 'refs/heads/main', True, root)
    with pytest.raises(ValueError, match='main'):
        pipeline.plan(github, SHA, '20', 'workflow_dispatch', 'refs/heads/other', True, root)


def prepared_artifact(root):
    folder = root/'release'
    folder.mkdir()
    product = root/'product'
    product.mkdir()
    for name in ('Codexon.exe', 'CodexonRecovery.exe'):
        (product/name).write_bytes(name.encode())
    manifest = dict(commit=SHA, version=pipeline.version(root),
                    sha256=pipeline.digest(product/'Codexon.exe'),
                    recovery_sha256=pipeline.digest(product/'CodexonRecovery.exe'))
    (product/'build-manifest.json').write_text(json.dumps(manifest))
    (product/'SOURCE-OFFER.md').write_text(prepare_sources.describe(prepare_sources.load_lock(root))['url'])
    (folder/'Codexon-Setup.exe').write_bytes(b'verified installer')
    (folder/'Codexon-Setup.exe.sha256').write_text(pipeline.digest(folder/'Codexon-Setup.exe') + '  Codexon-Setup.exe\n')
    pipeline.seal(folder, product, SHA, '10', root)
    return folder


def test_verified_artifact_preserves_the_installer_without_rebuilding(root):
    folder = prepared_artifact(root)
    before = (folder/'Codexon-Setup.exe').read_bytes()
    assert pipeline.validate_artifact(folder, SHA, '10', root)['coverage'] == 'full'
    assert (folder/'Codexon-Setup.exe').read_bytes() == before


@pytest.mark.parametrize('changed', ['Codexon-Setup.exe', 'RELEASE_NOTES.md', 'commit', 'run_id', 'coverage', 'source_key'])
def test_changed_or_incomplete_artifact_blocks_publication(root, changed):
    folder = prepared_artifact(root)
    if changed in pipeline.FILES:
        (folder/changed).write_bytes(b'changed')
    else:
        file = folder/'verification.json'
        receipt = json.loads(file.read_text())
        receipt[changed] = 'wrong'
        file.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        pipeline.validate_artifact(folder, SHA, '10', root)


def test_qa_only_change_keeps_targeted_real_checks_without_rebuilding():
    plan = select_tests({'tools/verify_proxy_update.py'})
    assert plan['qa_smokes'] == ['proxy_update']
    assert not plan['package_impact'] and not plan['proxy_impact']
    assert 'tests/test_proxy_update.py' in plan['tests']
    commands = verify_release_tools.commands(plan['qa_smokes'], 'old.exe', 'new.exe', Path('isolated'))
    assert len(commands) == 1 and commands[0][0] == 'tools/verify_proxy_update.py'
    assert '--idle-connections' in commands[0]


@pytest.mark.parametrize('changed, expected', [
    ('tools/verify_proxy_update.py\n', True),
    ('cachemonitor/proxy_update.py\n', False),
    ('SOURCE-OFFER.md\n', False),
    ('requirements-release.lock\n', False),
    ('README.md\n', False),
])
def test_reusing_a_public_binary_requires_unchanged_packaged_inputs(root, monkeypatch, changed, expected):
    monkeypatch.setattr(verify_release_tools.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=changed))
    assert verify_release_tools.can_reuse(root) is expected


def test_workflow_serializes_same_commit_and_publishes_the_retained_artifact():
    workflow = (pipeline.ROOT/'.github/workflows/windows.yml').read_text()
    assert 'group: windows-${{ github.ref }}-${{ github.sha }}\n  cancel-in-progress: false' in workflow
    assert 'verify:\n    needs: resolve' in workflow
    verify, publish = workflow.split('\n  publish:\n')
    assert 'needs: [resolve, verify]' in publish
    assert 'release_pipeline.py publish' in publish and 'build.ps1' not in publish
    assert 'name: verified-release-${{ github.sha }}' in verify
    assert 'git worktree add' not in verify
    assert 'verify_release_tools.py --plan' in verify


def test_publication_uses_verified_artifact_and_only_uploads_installer_files(root, monkeypatch):
    original = prepared_artifact(root)
    record = run_record()
    class PublishingGitHub(FakeGitHub):
        def __init__(self):
            super().__init__()
            self.calls = []
        def api(self, path, **kwargs):
            if path == 'actions/runs/10':
                return record
            return super().api(path, **kwargs)
        def command(self, *args):
            self.calls.append(args)
            if args[:2] == ('run', 'download'):
                shutil.copytree(original, Path(args[-1]))
            elif args[:2] == ('release', 'create'):
                self.tag = SHA
                self.released = dict(draft=True, assets=[
                    dict(name=p.name, digest='sha256:' + pipeline.digest(p)) for p in args[3:5]])
            elif args[:2] == ('release', 'edit'):
                self.released['draft'] = False
    reference = prepare_sources.describe(prepare_sources.load_lock(root))
    monkeypatch.setattr(pipeline, 'ensure_hosted', lambda *args: reference)
    github = PublishingGitHub()
    result = pipeline.publish(github, SHA, '10', '20', root/'download', root)
    assert result['commit'] == SHA and not github.released['draft']
    create = next(c for c in github.calls if c[:2] == ('release', 'create'))
    assert {p.name for p in create[3:5]} == {'Codexon-Setup.exe', 'Codexon-Setup.exe.sha256'}
    assert not any('third-party-source-' in str(arg) for arg in create)
    assert reference['url'] in (root/'download/publication-notes.md').read_text(encoding='utf-8')


def test_failed_run_cannot_reach_download_or_publication(root, monkeypatch):
    class FailedGitHub(FakeGitHub):
        def api(self, path, **kwargs):
            record = run_record()
            record['conclusion'] = 'failure'
            return record
        def command(self, *args):
            pytest.fail('Failed verification must not reach publication')
    with pytest.raises(ValueError, match='did not pass'):
        pipeline.publish(FailedGitHub(), SHA, '10', '20', root/'download', root)


def test_current_run_requires_successful_verify_job(root):
    class PendingGitHub(FakeGitHub):
        def api(self, path, **kwargs):
            if path.endswith('/jobs'):
                return {'jobs': [dict(name='verify', conclusion=None)]}
            record = run_record()
            record.update(status='in_progress', conclusion=None)
            return record
        def command(self, *args):
            pytest.fail('Verification has not completed')
    with pytest.raises(ValueError, match='has not passed'):
        pipeline.publish(PendingGitHub(), SHA, '10', '10', root/'download', root)


def test_failed_and_nonancestor_runs_cannot_hide_unverified_changes(root, monkeypatch):
    passed = run_record()
    passed.update(id=8, head_sha='b' * 40)
    failed = {**passed, 'id': 11, 'head_sha': 'c' * 40, 'conclusion': 'failure'}
    foreign = {**passed, 'id': 10, 'head_sha': 'd' * 40}
    github = FakeGitHub([failed, foreign, passed])
    checked = []
    def ancestry(command, **kwargs):
        checked.append(command[3])
        return SimpleNamespace(returncode=0 if command[3] == 'b' * 40 else 1)
    monkeypatch.setattr(pipeline.subprocess, 'run', ancestry)
    assert pipeline.verified_base(github, SHA, root) == 'b' * 40
    assert checked == ['d' * 40, 'b' * 40]


def test_missing_verified_baseline_requests_full_checks(root):
    assert pipeline.verified_base(FakeGitHub(), SHA, root) == ''
    workflow = (pipeline.ROOT/'.github/workflows/windows.yml').read_text()
    assert 'PUSH_BASE: ${{ needs.resolve.outputs.base }}' in workflow
    assert '-not $base' in workflow
