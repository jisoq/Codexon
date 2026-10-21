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
