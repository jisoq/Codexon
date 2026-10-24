import copy
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from tools import release_pipeline as pipeline
from tools import prepare_sources
from tools import verify_release_tools
from tools.release_github import GitHub
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
        self.verification = None
    def api(self, path, **kwargs):
        if path.endswith('/jobs'):
            return {'jobs': [dict(name='verify', conclusion=self.verification or
                                 next((r['conclusion'] for r in self.runs if str(r['id']) == path.split('/')[2]), 'success'))]}
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
    assert not result['candidate'] and result['reuse_run'] == '10' and not result['verify']
    ordinary = pipeline.plan(github, SHA, '20', 'push', 'refs/heads/main', False, root)
    assert ordinary['reuse_run'] == '10' and not ordinary['verify']
    assert not ordinary['candidate']


@pytest.mark.parametrize('changes', [
    {'head_sha': 'b' * 40}, {'event': 'pull_request'}, {'head_branch': 'other'},
    {'path': '.github/workflows/other.yml'}, {'head_repository': {'full_name': 'foreign/repo'}},
    {'conclusion': 'failure'}, {'status': 'in_progress'}, {'id': 20},
])
def test_wrong_failed_pending_or_current_run_is_not_reused(changes):
    record = run_record()
    record.update(changes)
    assert pipeline.reusable_run(FakeGitHub([record]), SHA, '20') == ''


def test_expired_artifact_requires_explicit_candidate_preparation(root):
    with pytest.raises(ValueError, match='prepare=true'):
        pipeline.plan(FakeGitHub([run_record()], expired=True), SHA, '20', 'workflow_dispatch',
                      'refs/heads/main', True, root)
    result = pipeline.plan(FakeGitHub([run_record()], expired=True), SHA, '20', 'workflow_dispatch',
                           'refs/heads/main', False, root, prepare=True)
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


def prepared_artifact(root, *, plan=None):
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
    pipeline.seal(folder, product, SHA, '10', root, plan=plan)
    return folder


def test_verified_artifact_preserves_the_installer_without_rebuilding(root):
    folder = prepared_artifact(root)
    before = (folder/'Codexon-Setup.exe').read_bytes()
    assert pipeline.validate_artifact(folder, SHA, '10', root)['coverage'] == 'affected-regression-v1'
    assert (folder/'Codexon-Setup.exe').read_bytes() == before


def test_windows_artifact_notes_match_linux_checkout_line_endings(root):
    notes = root/'docs/releases/2026.10.11.1.md'
    original = notes.read_bytes().replace(b'\r\n', b'\n')
    notes.write_bytes(original.replace(b'\n', b'\r\n'))
    folder = prepared_artifact(root)
    notes.write_bytes(original)
    assert pipeline.validate_artifact(folder, SHA, '10', root)['coverage'] == 'affected-regression-v1'


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


def test_reused_qa_registers_its_default_scope_before_starting_children(tmp_path, monkeypatch):
    evidence = tmp_path/'evidence'; evidence.mkdir()
    plan = tmp_path/'plan.json'
    plan.write_text(json.dumps({'qa_smokes': ['recovery']}))
    monkeypatch.setenv('CODEXON_CHECK_DIAGNOSTICS', str(evidence))
    monkeypatch.setattr(verify_release_tools, 'ROOT', tmp_path)
    monkeypatch.setattr(verify_release_tools, 'can_reuse', lambda: True)
    monkeypatch.setattr(verify_release_tools, 'version', lambda: '2026.10.12.1')
    monkeypatch.setattr(verify_release_tools.sys, 'argv', ['verify_release_tools.py', '--plan', str(plan)])
    output = tmp_path/'artifacts/verification/changed-release-tools'
    launched = []
    def launch(command, **kwargs):
        assert output.is_dir()
        assert [json.loads(path.read_text())['root'] for path in evidence.glob('owner-*.json')] == [str(output)]
        launched.append(command)
        if '--tag' in command:
            target = Path(command[command.index('--output') + 1]); target.mkdir()
            (target/'result.json').write_text(json.dumps({'executable': str(target/'Codexon.exe')}))
    monkeypatch.setattr(verify_release_tools.subprocess, 'run', launch)
    verify_release_tools.main()
    assert len(launched) == 2
    # Never claim a pre-existing directory, even when it has the default name.
    with pytest.raises(FileExistsError):
        verify_release_tools.main()
    assert len(launched) == 2


def test_workflow_preserves_successful_jobs_and_publishes_the_retained_artifact():
    workflow = (pipeline.ROOT/'.github/workflows/windows.yml').read_text()
    assert 'group: windows-${{ github.event.pull_request.number || github.ref }}-${{ github.sha }}' in workflow
    assert 'cancel-in-progress: false' in workflow
    assert 'needs: [resolve, native, source, package_build, package_verify]' in workflow
    verify, publish = workflow.split('\n  publish:\n')
    assert 'needs: [resolve, verify]' in publish
    assert 'release_pipeline.py publish' in publish and 'build.ps1' not in publish
    assert 'name: verified-release-${{ github.sha }}' in verify
    assert 'git worktree add' not in verify
    assert 'verify_release_tools.py --plan' in verify
    build = workflow.split('\n  package_build:\n')[1].split('\n  package_verify:\n')[0]
    validation = workflow.split('\n  package_verify:\n')[1].split('\n  verify:\n')[0]
    assert 'build.ps1' in build and 'Build-Installer.ps1' in build
    assert 'needs: [resolve, native]' in build
    assert 'source-environment' in build
    assert 'build.ps1' not in validation and 'Build-Installer.ps1' not in validation
    assert 'actions/download-artifact@' in validation
    assert 'candidate-${{ github.sha }}-${{ github.run_id }}' in build
    assert 'candidate-${{ github.sha }}-${{ github.run_id }}' in validation
    assert 'name: source-verified-${{ fromJSON(needs.resolve.outputs.plan).source_key || github.sha }}' in workflow


@pytest.mark.parametrize('existing_draft', [False, True])
def test_publication_uses_verified_artifact_and_only_uploads_installer_files(root, monkeypatch, existing_draft):
    original = prepared_artifact(root)
    record = run_record()
    class PublishingGitHub(FakeGitHub):
        release = GitHub.release
        def __init__(self):
            super().__init__()
            self.calls = []
            if existing_draft:
                self.released = dict(tag_name='v2026.10.11.1', draft=True, assets=[
                    dict(name=name, digest='sha256:' + pipeline.digest(original/name))
                    for name in ('Codexon-Setup.exe', 'Codexon-Setup.exe.sha256')])
        def api(self, path, **kwargs):
            if path == 'actions/runs/10':
                return record
            if path.startswith('releases/tags/'):
                return None  # The tag endpoint cannot find the unpublished draft.
            if path == 'releases?per_page=100&page=1':
                return [dict(tag_name='vother', draft=False)] * 100
            if path == 'releases?per_page=100&page=2':
                return [self.released] if self.released else []
            return super().api(path, **kwargs)
        def command(self, *args):
            self.calls.append(args)
            if args[:2] == ('run', 'download'):
                shutil.copytree(original, Path(args[-1]))
            elif args[:2] == ('release', 'create'):
                assert self.released is None, 'Do not duplicate an existing draft'
                self.released = dict(tag_name=args[2], draft=True, assets=[
                    dict(name=p.name, digest='sha256:' + pipeline.digest(p)) for p in args[3:5]])
            elif args[:2] == ('release', 'edit'):
                self.released['draft'] = False
    reference = prepare_sources.describe(prepare_sources.load_lock(root))
    monkeypatch.setattr(pipeline, 'ensure_hosted', lambda *args: reference)
    github = PublishingGitHub()
    result = pipeline.publish(github, SHA, '10', '20', root/'download', root)
    assert result['commit'] == SHA and not github.released['draft']
    creates = [c for c in github.calls if c[:2] == ('release', 'create')]
    assert len(creates) == (0 if existing_draft else 1)
    if creates:
        assert {p.name for p in creates[0][3:5]} == {'Codexon-Setup.exe', 'Codexon-Setup.exe.sha256'}
        assert not any('third-party-source-' in str(arg) for arg in creates[0])
    assert reference['url'] in (root/'download/publication-notes.md').read_text(encoding='utf-8')


def test_failed_run_cannot_reach_download_or_publication(root, monkeypatch):
    class FailedGitHub(FakeGitHub):
        def api(self, path, **kwargs):
            if path.endswith('/jobs'):
                return {'jobs': [dict(name='verify', conclusion='failure')]}
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


def test_missing_verified_baseline_requests_basic_source_and_native_package_checks(root):
    assert pipeline.verified_base(FakeGitHub(), SHA, root) == ''
    from tools.ci_plan import build_plan
    checks = build_plan(None, candidate=True)
    assert checks['source_args'] == ['--basic']
    assert checks['source_suite'] == 'basic'
    assert checks['package'] and checks['native']
    assert not checks['proxy'] and not checks['msix']


def test_publish_failure_does_not_discard_successfully_verified_installer(root):
    record = run_record()
    record['conclusion'] = 'failure'
    github = FakeGitHub([record])
    github.verification = 'success'
    result = pipeline.plan(github, SHA, '20', 'workflow_dispatch', 'refs/heads/main', True, root)
    assert result['reuse_run'] == '10' and not result['verify']


def test_required_skipped_or_missing_job_cannot_pass_final_verdict():
    plan = dict(verify=True, package=True, qa_reuse=False)
    results = {name:'success' for name in ('resolve', 'source', 'package_build', 'package_verify')}
    assert pipeline.final_verdict(plan, results)['result'] == 'success'
    for state in ('skipped', 'failure', 'cancelled', 'missing'):
        with pytest.raises(ValueError, match='Required checks'):
            pipeline.final_verdict(plan, {**results, 'package_verify':state})
    # Pure source changes require no deployment job, but still require source.
    assert pipeline.final_verdict({**plan, 'package':False}, {**results, 'package_build':'skipped', 'package_verify':'skipped'})
    with pytest.raises(ValueError, match='Required checks'):
        pipeline.final_verdict({**plan, 'package':False}, {'resolve':'success', 'source':'skipped'})


def test_candidate_is_reused_only_for_its_exact_commit_run_and_payload(tmp_path):
    product = tmp_path/'Codexon'; product.mkdir()
    (product/'build-manifest.json').write_text(json.dumps({'commit':SHA}))
    binary = product/'Codexon.exe'; binary.write_bytes(b'candidate')
    pipeline.candidate_receipt(tmp_path, SHA, '10')
    pipeline.candidate_receipt(tmp_path, SHA, '10', validate=True)
    with pytest.raises(ValueError, match='another commit'):
        pipeline.candidate_receipt(tmp_path, 'b'*40, '10', validate=True)
    with pytest.raises(ValueError, match='changed'):
        pipeline.candidate_receipt(tmp_path, SHA, '11', validate=True)
    binary.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        pipeline.candidate_receipt(tmp_path, SHA, '10', validate=True)


def test_failed_deployment_is_not_hidden_by_a_new_source_baseline():
    from tools.ci_plan import build_plan
    plan = build_plan({'docs/followup.md'}, base='source-passed',
                      package_changed={'cachemonitor/install_activation.py', 'docs/followup.md'})
    assert plan['source_suite'] == 'basic' and plan['tests']
    assert plan['package'] and plan['native']
    assert not plan['proxy'] and not plan['msix']


def test_notes_do_not_make_an_ordinary_push_a_release_candidate(root):
    github = FakeGitHub()
    ordinary = pipeline.plan(github, SHA, '20', 'push', 'refs/heads/main', False, root)
    assert not ordinary['candidate'] and ordinary['verify']
    prepared = pipeline.plan(github, SHA, '20', 'workflow_dispatch', 'refs/heads/main', False, root, prepare=True)
    assert prepared['candidate'] and prepared['verify']
    from tools.ci_plan import build_plan
    assert not build_plan({'cachemonitor/pricing.py'}, base='verified')['dependencies']
    assert build_plan({'cachemonitor/pricing.py'}, base='verified', candidate=True)['dependencies']
    assert build_plan({'cachemonitor/install_activation.py'}, base='verified')['package']
    with pytest.raises(ValueError, match='separately'):
        pipeline.plan(github, SHA, '20', 'workflow_dispatch', 'refs/heads/main', True, root, prepare=True)
    branch = pipeline.plan(github, SHA, '20', 'workflow_dispatch', 'refs/heads/codex/verification', False, root, prepare=True, exercise_rerun=True)
    assert branch['candidate'] and branch['verify'] and not branch['published']
    with pytest.raises(ValueError, match='non-main'):
        pipeline.plan(github, SHA, '20', 'workflow_dispatch', 'refs/heads/main', False, root, exercise_rerun=True)


@pytest.mark.parametrize('state', ['skipped', 'failure', 'cancelled', 'missing'])
def test_native_contract_must_pass_when_planned(state):
    plan = dict(verify=True, package=False, qa_reuse=False, rust=True)
    results = dict(resolve='success', source='success', native='success')
    assert pipeline.final_verdict(plan, results)['result'] == 'success'
    with pytest.raises(ValueError, match='native'):
        pipeline.final_verdict(plan, {**results, 'native': state})
    assert pipeline.final_verdict({**plan, 'rust': False}, {**results, 'native': 'skipped'})


@pytest.mark.parametrize('event, ref, expected', [
    ('push', 'refs/heads/main', True),
    ('workflow_dispatch', 'refs/heads/main', True),
    ('workflow_dispatch', 'refs/heads/preview', False),
    ('pull_request', 'refs/pull/1/merge', False),
])
def test_ordinary_checked_main_installer_can_be_retained_for_prepare(root, event, ref, expected):
    result = pipeline.plan(FakeGitHub(), SHA, '20', event, ref, False, root)
    assert result['release_artifact'] is expected
    assert not result['candidate']


def test_existing_public_version_or_missing_notes_cannot_create_release_artifact(root):
    github = FakeGitHub()
    github.tag = SHA
    github.released = dict(draft=False)
    assert not pipeline.plan(github, SHA, '20', 'push', 'refs/heads/main', False, root)['release_artifact']
    github.tag = 'b' * 40
    assert not pipeline.plan(github, SHA, '20', 'push', 'refs/heads/main', False, root)['release_artifact']
    github.tag = None
    (root/'docs/releases/2026.10.11.1.md').unlink()
    assert not pipeline.plan(github, SHA, '20', 'push', 'refs/heads/main', False, root)['release_artifact']


@pytest.mark.parametrize('extended, coverage', [(False, 'basic-ci-v1'), (True, 'extended-ci-v1')])
def test_release_receipt_records_the_immutable_ci_plan_coverage(root, extended, coverage):
    from tools import source_receipt
    from tools.ci_plan import build_plan
    plan = build_plan(set(), candidate=True, extended=extended)
    plan.update(commit=SHA, source_environment=source_receipt.environment_key(root))
    folder = prepared_artifact(root, plan=plan)
    assert pipeline.validate_artifact(folder, SHA, '10', root)['coverage'] == coverage
    (folder/'Codexon-Setup.exe').write_bytes(b'changed after verification')
    with pytest.raises(ValueError, match='artifact changed'):
        pipeline.validate_artifact(folder, SHA, '10', root)


def test_release_sealing_rejects_a_partial_basic_plan(root):
    from tools import source_receipt
    from tools.ci_plan import build_plan
    plan = build_plan(set(), candidate=True)
    plan.update(commit=SHA, source_environment=source_receipt.environment_key(root))
    plan['tests'] = list(plan['tests'])[:-1]
    with pytest.raises(ValueError, match='canonical audited selection'):
        prepared_artifact(root, plan=plan)
    assert not (root/'release/verification.json').exists()


def test_workflow_compatibility_steps_use_the_extended_plan_gates():
    from tools.ci_plan import build_plan
    workflow = (pipeline.ROOT/'.github/workflows/windows.yml').read_text()
    gates = {
        'tests/test_overlay_resilience.py':'scales',
        'verify_msix_installation.ps1':'msix',
        'tools/verify_proxy_update.py':'proxy',
        'tools/verify_gui_handoff.py':'gui_handoff',
    }
    basic = build_plan(None, candidate=True)
    extended = build_plan(None, candidate=True, extended=True)
    for command, gate in gates.items():
        steps = [step for step in workflow.split('      - ') if command in step]
        assert steps
        assert all('if: fromJSON(needs.resolve.outputs.plan).' + gate in step
                   for step in steps)
        assert not basic[gate] and extended[gate]


@pytest.fixture
def basic_candidate(root):
    from tools import source_receipt
    key = 'c' * 64
    record = {**run_record(), 'repository': {'full_name': 'jisoq/Codexon', 'id': 123},
              'head_repository': {'full_name': 'jisoq/Codexon', 'id': 123},
              'conclusion': 'failure'}
    receipt = dict(schema=2, policy=source_receipt.POLICY, commit=SHA, run_id='10',
                   environment=source_receipt.environment_key(root), complete=True,
                   scope=list(source_receipt.basic_tests()), args=['--basic'], suite='basic',
                   affected_tests=[], scales=False, input_key=key)
    artifact = dict(id=100, name='source-verified-' + key, expired=False, size_in_bytes=1024,
                    workflow_run={'id': 10})
    class SourceGitHub(FakeGitHub):
        def __init__(self):
            self.requests = []
            self.downloads = []
            self.jobs = [dict(name='source', conclusion='success'),
                         dict(name='package_verify', conclusion='failure')]
            self.payload = None
            self.pulls = []
        def api(self, path, **kwargs):
            self.requests.append(path)
            if path == f'actions/artifacts?name=source-verified-{key}&per_page=100':
                return {'artifacts': [artifact]}
            if path == 'actions/runs/10':
                return record
            if path == 'actions/runs/10/jobs':
                return {'jobs': self.jobs}
            if path == f'commits/{record["head_sha"]}/pulls?per_page=100':
                return self.pulls
            pytest.fail('Unexpected source lookup request ' + path)
        def command(self, *args):
            self.downloads.append(args)
            assert args[:5] == ('run', 'download', '10', '--name', 'source-verified-' + key)
            text = json.dumps(receipt) if self.payload is None else self.payload
            (Path(args[-1])/'source-verification.json').write_text(text)
    return SourceGitHub(), key, record, receipt, artifact


@pytest.mark.parametrize('event', ['push', 'workflow_dispatch', 'pull_request'])
def test_identical_basic_source_survives_downstream_failure_and_same_repo_pr_merge(root, basic_candidate, event):
    github, key, record, receipt, _ = basic_candidate
    record['event'] = event
    if event == 'pull_request':
        record['pull_requests'] = [dict(head={'repo': {'id': 123}})]
        receipt['commit'] = 'b' * 40
    assert pipeline.reusable_source(github, key, root) == '10'
    assert len(github.downloads) == 1
    assert pipeline.trusted(record, SHA, github.repository) == (event != 'pull_request')


@pytest.mark.parametrize('origin', ['same_repo', 'fork', 'other_head', 'foreign_base', 'missing_head_repo', 'empty'])
def test_merged_pr_source_reuse_requires_commit_bound_same_repository_origin(root, basic_candidate, origin):
    github, key, record, _, _ = basic_candidate
    record.update(event='pull_request', head_sha='e2c55000c9ebe5f13f24e7ec71022fa9a164d24b', pull_requests=[])
    record['repository']['id'] = record['head_repository']['id'] = 1383017076
    pull = dict(number=28, state='closed', base={'repo': {'id': 1383017076}},
                head={'sha': record['head_sha'], 'repo': {'id': 1383017076}})
    if origin == 'fork':
        pull['head']['repo']['id'] = 999
    elif origin == 'other_head':
        pull['head']['sha'] = 'b' * 40
    elif origin == 'foreign_base':
        pull['base']['repo']['id'] = 999
    elif origin == 'missing_head_repo':
        pull['head']['repo'] = None
    github.pulls = [] if origin == 'empty' else [pull]
    assert pipeline.reusable_source(github, key, root) == ('10' if origin == 'same_repo' else '')
    assert len(github.downloads) == (1 if origin == 'same_repo' else 0)


@pytest.mark.parametrize('invalid', ['artifact_name', 'expired', 'artifact_size', 'false_size', 'producer',
                                     'workflow', 'repository', 'head_repository', 'fork_pr',
                                     'missing_pr_origin', 'source_failure', 'wrong_key',
                                     'wrong_run', 'wrong_commit', 'false_coverage', 'partial_scope',
                                     'legacy_receipt', 'receipt_size', 'invalid_json'])
def test_untrusted_or_incompatible_basic_evidence_runs_fresh_checks(root, basic_candidate, invalid):
    github, key, record, receipt, artifact = basic_candidate
    if invalid == 'artifact_name':
        artifact['name'] += '-foreign'
    elif invalid == 'expired':
        artifact['expired'] = True
    elif invalid == 'artifact_size':
        artifact['size_in_bytes'] = 65537
    elif invalid == 'false_size':
        artifact['size_in_bytes'] = False
    elif invalid == 'producer':
        artifact['workflow_run']['id'] = 11
    elif invalid == 'workflow':
        record['path'] = '.github/workflows/foreign.yml'
    elif invalid == 'repository':
        record['repository']['full_name'] = 'foreign/repo'
    elif invalid == 'head_repository':
        record['head_repository']['full_name'] = 'foreign/repo'
    elif invalid in ('fork_pr', 'missing_pr_origin'):
        record['event'] = 'pull_request'
        record['pull_requests'] = [dict(head={'repo': {'id': 999}})] if invalid == 'fork_pr' else []
    elif invalid == 'source_failure':
        github.jobs[0]['conclusion'] = 'failure'
    elif invalid == 'wrong_key':
        receipt['input_key'] = 'd' * 64
    elif invalid == 'wrong_run':
        receipt['run_id'] = '11'
    elif invalid == 'wrong_commit':
        receipt['commit'] = 'b' * 40
    elif invalid == 'false_coverage':
        receipt['complete'] = False
    elif invalid == 'partial_scope':
        receipt['scope'].pop()
    elif invalid == 'legacy_receipt':
        receipt.pop('input_key')
    elif invalid == 'receipt_size':
        github.payload = ' ' * 32769
    else:
        github.payload = '{broken json'
    if invalid == 'producer':
        original_api = github.api
        github.api = lambda path, **kwargs: {**record, 'id': 10} if path == 'actions/runs/11' else original_api(path, **kwargs)
    assert pipeline.reusable_source(github, key, root) == ''


def test_optional_source_lookup_failure_runs_checks_and_reports_the_fallback(root, basic_candidate, capsys):
    github, key, *_ = basic_candidate
    def unavailable(*args, **kwargs):
        raise RuntimeError('Actions API unavailable')
    github.api = unavailable
    assert pipeline.reusable_source(github, key, root) == ''
    assert 'Actions API unavailable' in capsys.readouterr().err


def test_source_lookup_does_not_hide_programming_errors(root, basic_candidate):
    github, key, *_ = basic_candidate
    def invalid(*args, **kwargs):
        raise TypeError('programming defect')
    github.api = invalid
    with pytest.raises(TypeError, match='programming defect'):
        pipeline.reusable_source(github, key, root)


@pytest.mark.parametrize('event, ref', [('push', 'refs/heads/main'),
                                      ('workflow_dispatch', 'refs/heads/preview'),
                                      ('pull_request', 'refs/pull/1/merge')])
def test_release_planning_does_not_scan_source_baselines_for_fixed_source_profiles(root, event, ref):
    class PlanningGitHub(FakeGitHub):
        def api(self, path, **kwargs):
            if '?branch=' in path:
                assert 'status=completed' in path, 'Only deployment coverage uses ancestor runs'
            return super().api(path, **kwargs)
    result = pipeline.plan(PlanningGitHub(), SHA, '20', event, ref, False, root)
    assert result['verify'] and result['base'] == ''


def test_dependency_audit_install_failure_stops_resolve_before_downstream_jobs(tmp_path):
    workflow = (pipeline.ROOT/'.github/workflows/windows.yml').read_text()
    resolve = workflow.split('\n  resolve:\n')[1].split('\n  native:\n')[0]
    build = workflow.split('\n  package_build:\n')[1].split('\n  package_verify:\n')[0]
    step = next(step for step in resolve.split('      - ') if 'python -m pip_audit' in step)
    assert 'python -m pip_audit' not in build
    assert 'steps.plan.outputs.plan).verify && fromJSON(steps.plan.outputs.plan).dependencies' in step
    script = step.split('run: |\n', 1)[1]
    script = '\n'.join(line[10:] for line in script.splitlines() if line.startswith('          '))
    harness = tmp_path/'audit.ps1'
    harness.write_text("function python {\n"
                       "  if ($args[1] -eq 'pip') { $global:LASTEXITCODE = 17 }\n"
                       "  else { Write-Output 'AUDIT_RAN'; $global:LASTEXITCODE = 0 }\n"
                       "}\n" + script)
    result = subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-File', str(harness)],
                            capture_output=True, text=True)
    assert result.returncode == 17 and 'AUDIT_RAN' not in result.stdout
