import subprocess

import pytest

from tools import source_receipt
from tools.verify_changes import CORE_TESTS

SHA = 'a' * 40
WORKFLOW = """env:
  CODEXON_CPU_BACKEND: rust
jobs:
  source:
    runs-on: windows-latest
    steps:
      - uses: actions/setup-python@v7
        with:
          python-version: '3.12.10'
      - name: Run checks
        run: python tools/verify_changes.py --full
      - name: Retain evidence
        uses: actions/upload-artifact@v7
        with:
          name: source-evidence
          retention-days: 7
  package_build:
    runs-on: windows-latest
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path/'.github/workflows').mkdir(parents=True)
    (tmp_path/'.github/workflows/windows.yml').write_text(WORKFLOW)
    (tmp_path/'requirements-release.lock').write_text('pytest==9.0.3\n')
    return tmp_path


def planned(root, args=('--full',), affected=()):
    return dict(source_environment=source_receipt.environment_key(root), commit=SHA,
                source_base='', tests=sorted(CORE_TESTS) if '--basic' in args else ['tests'], source_args=list(args), scales=False,
                affected_tests=list(affected),
                source_suite=source_receipt.coverage_suite(args, affected))


def test_transient_runner_patch_preserves_coverage_and_is_recorded(root, monkeypatch):
    monkeypatch.setenv('ImageVersion', '20261019.264')
    plan = planned(root)
    first = source_receipt.write_receipt(root/'receipt', plan, SHA, '10', root)
    monkeypatch.setenv('ImageVersion', '20261019.275')
    assert source_receipt.valid(first, SHA, '10', root)
    checked = source_receipt.check_environment(plan, SHA, root)
    assert checked['runner']['image_version'] == '20261019.275'
    assert first['runner']['image_version'] == '20261019.264'


@pytest.mark.parametrize('change', ['dependency', 'python', 'image_os', 'command', 'backend'])
def test_meaningful_environment_change_fails_preflight_and_invalidates_baseline(root, monkeypatch, change):
    plan = planned(root)
    receipt = source_receipt.write_receipt(root/'receipt', plan, SHA, '10', root)
    if change == 'dependency':
        (root/'requirements-release.lock').write_text('pytest==10.0.0\n')
    elif change == 'python':
        monkeypatch.setattr(source_receipt.platform, 'python_version', lambda: '9.9.9')
    elif change == 'image_os':
        monkeypatch.setenv('ImageOS', 'different-image-family')
    else:
        before, after = ('--full', '--integration') if change == 'command' else ('BACKEND: rust', 'BACKEND: python')
        path = root/'.github/workflows/windows.yml'
        path.write_text(WORKFLOW.replace(before, after))
    with pytest.raises(ValueError, match='before running checks'):
        source_receipt.check_environment(plan, SHA, root)
    assert not source_receipt.valid(receipt, SHA, '10', root)


def test_workflow_evidence_names_comments_and_uploads_do_not_reset_source_coverage(root):
    key = source_receipt.environment_key(root)
    path = root/'.github/workflows/windows.yml'
    path.write_text(WORKFLOW.replace('name: Run checks', 'name: Verify source')
                    .replace('retention-days: 7', 'retention-days: 30')
                    .replace('uses: actions/upload-artifact@v7', 'uses: actions/upload-artifact@v8')
                    .replace("python-version: '3.12.10'", "python-version: '3.12.10' # Pin interpreter")
                    .replace('  source:', '  # Source documentation\n  source:'))
    assert source_receipt.environment_key(root) == key


@pytest.mark.parametrize('args, affected, suite', [
    (['--basic'], [], 'basic'),
    (['--full'], [], 'essential'),
    (['--full', '--base', 'b' * 40], ['tests/test_install_runtime.py'], 'essential+affected'),
    (['--integration'], [], 'integration'),
    (['--extended'], [], 'extended'),
    (['--base', 'b' * 40], ['tests/test_pricing.py'], 'affected'),
])
def test_receipt_records_executed_coverage_without_claiming_full_integration(root, args, affected, suite):
    plan = planned(root, args, affected)
    receipt = source_receipt.write_receipt(root/'receipt', plan, SHA, '10', root)
    assert receipt['suite'] == suite
    assert receipt['affected_tests'] == affected
    assert source_receipt.valid(receipt, SHA, '10', root)
    receipt['suite'] = 'integration' if suite != 'integration' else 'essential'
    assert not source_receipt.valid(receipt, SHA, '10', root)


def test_wrong_commit_or_false_coverage_cannot_be_recorded(root):
    plan = planned(root)
    with pytest.raises(ValueError, match='environment changed'):
        source_receipt.write_receipt(root/'receipt', plan, 'b' * 40, '10', root)
    plan['source_suite'] = 'integration'
    with pytest.raises(ValueError, match='coverage profile'):
        source_receipt.write_receipt(root/'receipt', plan, SHA, '10', root)
    assert not (root/'receipt/source-verification.json').exists()


@pytest.mark.parametrize('change', ['missing_test', 'extra_test', 'order', 'args', 'affected', 'scales'])
def test_basic_receipt_rejects_scope_that_differs_from_the_audited_selection(root, change):
    plan = planned(root, ['--basic'])
    receipt = source_receipt.write_receipt(root/'receipt', plan, SHA, '10', root)
    if change == 'missing_test':
        plan['tests'].pop()
        receipt['scope'].pop()
    elif change == 'extra_test':
        plan['tests'].append('tests/test_ui.py')
        receipt['scope'].append('tests/test_ui.py')
    elif change == 'order':
        plan['tests'].reverse()
        receipt['scope'].reverse()
    elif change == 'args':
        plan['source_args'].append('--full')
        receipt['args'] = list(plan['source_args'])
    elif change == 'affected':
        plan['affected_tests'].append('tests/test_ui.py')
        receipt['affected_tests'].append('tests/test_ui.py')
    else:
        plan['scales'] = receipt['scales'] = True
    assert not source_receipt.valid(receipt, SHA, '10', root)
    with pytest.raises(ValueError, match='canonical audited selection'):
        source_receipt.write_receipt(root/'rejected', plan, SHA, '10', root)
    assert not (root/'rejected/source-verification.json').exists()


@pytest.fixture
def git_root(root):
    subprocess.run(['git', 'init', '--quiet', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=CI Test', '-c',
                    'user.email=ci@example.invalid', 'commit', '--quiet', '-m', 'Initial inputs'], check=True)
    return root


def test_basic_identity_reuses_identical_trees_across_commits_and_release_stages(git_root):
    plan = planned(git_root, ['--basic'])
    key = source_receipt.basic_input_key(git_root, plan)
    first = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=git_root)
    subprocess.run(['git', '-c', 'user.name=CI Test', '-c', 'user.email=ci@example.invalid',
                    'commit', '--quiet', '--allow-empty', '-m', 'Prepare release'], cwd=git_root, check=True)
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=git_root) != first
    plan.update(commit='b' * 40, candidate=True, package=True, release_artifact=True)
    assert source_receipt.basic_input_key(git_root, plan) == key


@pytest.mark.parametrize('path', ['cachemonitor/pricing.py', 'tests/test_pricing.py',
                                  'requirements-release.lock', 'pytest.ini'])
def test_basic_identity_covers_all_tracked_source_test_lock_and_config_inputs(git_root, path):
    plan = planned(git_root, ['--basic'])
    key = source_receipt.basic_input_key(git_root, plan)
    target = git_root/path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('changed input\n')
    subprocess.run(['git', 'add', path], cwd=git_root, check=True)
    subprocess.run(['git', '-c', 'user.name=CI Test', '-c', 'user.email=ci@example.invalid',
                    'commit', '--quiet', '-m', 'Change input'], cwd=git_root, check=True)
    assert source_receipt.basic_input_key(git_root, plan) != key


@pytest.mark.parametrize('change', ['python', 'image_os', 'backend', 'selection', 'command'])
def test_basic_identity_binds_environment_and_exact_execution(git_root, monkeypatch, change):
    plan = planned(git_root, ['--basic'])
    key = source_receipt.basic_input_key(git_root, plan)
    if change == 'python':
        monkeypatch.setattr(source_receipt.platform, 'python_version', lambda: '3.12.11')
    elif change == 'image_os':
        monkeypatch.setenv('ImageOS', 'different-runner-family')
    elif change == 'backend':
        monkeypatch.setattr(source_receipt, 'tests_require_native', lambda tests: False)
    elif change == 'selection':
        plan['tests'] = plan['tests'][:-1]
        with pytest.raises(ValueError, match='canonical audited selection'):
            source_receipt.basic_input_key(git_root, plan)
        return
    else:
        path = git_root/'.github/workflows/windows.yml'
        path.write_text(WORKFLOW.replace('--full', '--basic --changed-command'))
    assert source_receipt.basic_input_key(git_root, plan) != key


def test_basic_receipt_binds_planned_inputs_and_cannot_reissue_a_reused_result(git_root):
    plan = planned(git_root, ['--basic'])
    plan['source_key'] = source_receipt.basic_input_key(git_root, plan)
    receipt = source_receipt.write_receipt(git_root/'receipt', plan, SHA, '10', git_root)
    assert receipt['input_key'] == plan['source_key']
    assert source_receipt.valid(receipt, SHA, '10', git_root, input_key=plan['source_key'])
    assert not source_receipt.valid(receipt, SHA, '10', git_root, input_key='wrong')
    plan['source_reuse_run'] = '10'
    with pytest.raises(ValueError, match='reused|Reused'):
        source_receipt.write_receipt(git_root/'reissued', plan, SHA, '20', git_root)
    assert not (git_root/'reissued/source-verification.json').exists()
    plan['source_reuse_run'] = ''
    plan['source_key'] = 'changed after planning'
    with pytest.raises(ValueError, match='input|identity'):
        source_receipt.write_receipt(git_root/'changed', plan, SHA, '20', git_root)
    assert not (git_root/'changed/source-verification.json').exists()


def test_explicit_extended_has_no_reusable_basic_identity(root):
    assert source_receipt.basic_input_key(root, planned(root, ['--extended'])) == ''
