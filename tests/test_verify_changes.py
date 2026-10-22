"""Selected checks must cover the changed contract without unrelated global gates."""

import ast
import json
import pytest

from tools.verify_changes import ROOT, GROUPS, changed_files, pytest_commands, select_tests


def test_failed_check_retains_stdout_and_stderr(tmp_path):
    import sys
    from tools.verify_changes import run_logged
    log=tmp_path/'pytest-native.log'
    result=run_logged([sys.executable,'-u','-c',
        "import sys; print('started check'); print('failure detail',file=sys.stderr); sys.exit(7)"],log)
    assert result.returncode==7
    assert 'started check' in log.read_text(encoding='utf-8')
    assert 'failure detail' in result.stdout


def test_small_ui_changes_select_their_consumer_without_backend_or_package_checks():
    examples = {
        'cachemonitor/qml/OverlayControls.qml': 'tests/test_overlay_controls.py',
        'cachemonitor/qml/OverlayLinks.qml': 'tests/test_overlay_navigation.py',
        'cachemonitor/qml/QuotaDetail.qml': 'tests/test_quota_detail_card.py',
        'cachemonitor/qml/UiButton.qml': 'tests/test_quick_ui.py',
        'cachemonitor/token_colors.py': 'tests/test_theme_tokens.py',
        'cachemonitor/assets/i18n/en.json':
            'tests/test_public_release.py::test_english_token_labels_do_not_change_stored_values',
    }
    for source, consumer in examples.items():
        plan = select_tests({source})
        assert consumer in plan['tests'], source
        assert not plan['full'] and not plan['unmapped']
        assert not plan['package_impact'] and not plan['proxy_impact']
        assert not any('proxy' in test or 'test_index.' in test or 'test_data_contract.' in test
                       for test in plan['tests']), source
    # Selecting a complete changed test file subsumes specific node selections.
    plan = select_tests({'cachemonitor/assets/i18n/en.json', 'tests/test_public_release.py'})
    assert plan['tests'] == ('tests/test_public_release.py',)


def test_bundled_agent_icons_select_rendering_and_frozen_package_checks():
    for source in ('cachemonitor/overlay_icons.py', 'cachemonitor/assets/agent-icons/00-light.svg',
                   'cachemonitor/assets/agent-icons/source.json'):
        plan = select_tests({source})
        assert 'tests/test_overlay_monitor.py' in plan['tests']
        assert plan['package_impact'] and not plan['proxy_impact']
        assert not plan['full'] and not plan['unmapped']


def test_settings_and_comparison_changes_keep_wiring_without_selecting_unrelated_workflows():
    settings=select_tests({'cachemonitor/settings_page.py'})
    assert 'tests/test_settings.py' in settings['tests']
    assert 'tests/test_theme_tokens.py::test_quick_theme_selection_updates_overlay_and_preserves_state' in settings['tests']
    assert 'tests/test_app_errors.py::test_sidebar_errors_remain_visible_across_pages_and_do_not_show_main_banner' in settings['tests']
    assert not any(test.split('::')[0] in ('tests/test_history_explorer.py', 'tests/test_comparison_workflow.py',
                                          'tests/test_observer_control.py') for test in settings['tests'])
    comparison=select_tests({'cachemonitor/qml/UsageDashboard.qml'})
    assert {'tests/test_comparison_workflow.py', 'tests/test_dashboard_readability.py'} <= set(comparison['tests'])
    assert 'tests/test_dashboard_evidence.py::test_tab_loading_keeps_controls_and_ignores_stale_result' in comparison['tests']
    assert not any(test.split('::')[0] in ('tests/test_history_explorer.py', 'tests/test_home.py',
                                          'tests/test_display_scaling.py') for test in comparison['tests'])
    query=select_tests({'cachemonitor/prepared_query.py'})
    assert 'tests/test_prepared_analysis.py' in query['tests']
    assert 'tests/test_history_timeline_data.py' in query['tests']
    assert 'tests/test_ui.py::test_sequential_drilldown_and_full_call_detail' in query['tests']
    assert 'tests/test_history_explorer.py::test_session_chart_selection_detail_and_back_preserve_scope' in query['tests']
    assert 'tests/test_history_explorer.py' not in query['tests']
    assert not any(test.endswith('test_overlay_controls.py') for test in query['tests'])


def test_tool_contracts_run_without_desktop_or_native_build_but_unknown_contracts_keep_native_gate():
    from tools.verify_changes import tests_require_native
    tools=['tests/test_verify_changes.py', 'tests/test_build_native.py', 'tests/test_prepare_sources.py',
           'tests/test_release_pipeline.py', 'tests/test_collect_notices.py', 'tests/test_check_environment.py']
    commands=pytest_commands(tools)
    assert len(commands)==1 and commands[0][0]=='core'
    assert '--no-desktop' in commands[0][1]
    assert not tests_require_native(tools)
    assert not tests_require_native(['tests/test_pricing.py::test_example'])
    assert not tests_require_native([])
    for targets in (['tests'], ['tests/test_new_contract.py'], ['tests/test_native_usage.py'],
                    [*tools, 'tests/test_core.py']):
        assert tests_require_native(targets)


def test_full_fallback_retains_affected_integration_targets_without_duplicate_collection(tmp_path,monkeypatch,capsys):
    plan=select_tests({'requirements-release.lock', 'cachemonitor/settings_page.py'})
    assert plan['full'] and plan['tests']==('tests',)
    assert 'tests/test_settings.py' in plan['affected_tests']
    commands=pytest_commands(plan['tests'], affected_tests=plan['affected_tests'])
    gui=next(command for kind,command in commands if kind=='gui')
    assert '--affected-test' in gui and 'tests/test_settings.py' in gui
    assert 'not extended' not in gui
    assert select_tests(None)['affected_tests']==()
    import tools.verify_changes as verify
    monkeypatch.setattr(verify,'BASELINE',tmp_path/'missing.json')
    monkeypatch.setattr(verify,'file_hashes',lambda:{'cachemonitor/settings_page.py':'changed'})
    monkeypatch.setattr(verify,'changed_files',lambda *args:{'cachemonitor/settings_page.py'})
    monkeypatch.setattr(verify,'release_version_only',lambda **kwargs:False)
    assert verify.main(['--full','--base','HEAD','--plan'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['suite']=='essential+affected'
    assert 'tests/test_settings.py' in report['affected_tests']


def test_essential_and_affected_collection_runs_each_contract_once_and_excludes_other_integrations(tmp_path):
    import os
    import subprocess
    import sys
    from xml.etree import ElementTree
    config=tmp_path/'pytest.ini'
    config.write_text('[pytest]\naddopts = -m "not extended and (not integration or essential)"\n'
                      'markers =\n    integration: integration\n    essential: essential\n    extended: extended\n', encoding='utf-8')
    (tmp_path/'test_contracts.py').write_text(
        'import pytest\ndef test_unit():pass\n'
        '@pytest.mark.integration\n@pytest.mark.essential\ndef test_baseline():pass\n'
        '@pytest.mark.integration\n@pytest.mark.parametrize("value",[1,2])\ndef test_affected(value):pass\n'
        '@pytest.mark.integration\ndef test_affected_other():pass\n'
        '@pytest.mark.integration\ndef test_unrelated():pass\n'
        '@pytest.mark.integration\n@pytest.mark.extended\ndef test_extended():pass\n', encoding='utf-8')
    (tmp_path/'test_module.py').write_text(
        'import pytest\n@pytest.mark.integration\ndef test_module():pass\n'
        '@pytest.mark.extended\ndef test_module_extended():pass\n', encoding='utf-8')
    report=tmp_path/'results.xml'
    result=subprocess.run([sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'tools.check_progress',
        '-c', str(config), '--confcutdir', str(tmp_path), '--junitxml='+str(report),
        '--affected-test', 'test_contracts.py::test_affected',
        '--affected-test', 'test_contracts.py::test_affected', '--affected-test', 'test_module.py', str(tmp_path)],
        cwd=ROOT, env={**os.environ, 'CODEXON_CHECK_DIAGNOSTICS':''}, capture_output=True, text=True)
    assert result.returncode==0, result.stdout+result.stderr
    names=[case.attrib['name'] for case in ElementTree.parse(report).iter('testcase')]
    assert sorted(names)==['test_affected[1]', 'test_affected[2]', 'test_baseline', 'test_module', 'test_unit']


def test_calculation_protocol_and_unknown_changes_have_explicit_gates():
    runner = select_tests({'tools/run_ui_checks.py'})
    assert {'tests/test_ui_check_runner.py', 'tests/test_completed_layout.py'} <= set(runner['tests'])
    for source in ('cachemonitor/cache_execution.py','cachemonitor/cache_capture.py','cachemonitor/model_proxy.py',
                   'cachemonitor/cache_scheduler.py','cachemonitor/cache_control.py','cachemonitor/cache_hooks.py','cachemonitor/cache_operating.py'):
        chosen=select_tests({source})
        assert ('tests/test_model_proxy.py' if source=='cachemonitor/model_proxy.py' else 'tests/test_retired_cache.py') in chosen['tests']
        assert not chosen['full']
    price = select_tests({'cachemonitor/pricing.py'})
    assert {'tests/test_pricing.py', 'tests/test_quota_tracking_integration.py',
            'tests/test_session_costs.py'} <= set(price['tests'])
    proxy = select_tests({'cachemonitor/model_proxy.py'})
    assert {'tests/test_model_proxy.py', 'tests/test_proxy_http2.py'} <= set(proxy['tests'])
    assert proxy['package_impact'] and proxy['proxy_impact']
    version = select_tests({'cachemonitor/version.py'})
    assert version['package_impact'] and version['proxy_impact']
    assert 'tests/test_proxy_update.py' in version['tests']
    version_only = select_tests({'cachemonitor/version.py'},version_only=True)
    assert version_only['package_impact'] and not version_only['proxy_impact']
    assert 'tests/test_proxy_update.py' not in version_only['tests']
    restart = select_tests({'cachemonitor/app_restart.py'})
    assert restart['package_impact'] and not restart['proxy_impact']
    unknown = select_tests({'cachemonitor/unmapped_new.py'})
    assert unknown['unmapped'] == ('cachemonitor/unmapped_new.py',)
    assert not unknown['full']
    for source in ('tests/conftest.py', 'requirements-release.lock'):
        assert select_tests({source})['full']
    assert select_tests(None)['full']


def test_changed_and_deleted_fixture_modules_select_existing_transitive_consumers(tmp_path):
    current = {'cachemonitor/core.py':'new', 'tests/test_new.py':'x'}
    previous = {'files':{'cachemonitor/core.py':'old', 'cachemonitor/deleted.py':'y'}}
    assert changed_files(current, previous) == {
        'cachemonitor/core.py', 'cachemonitor/deleted.py', 'tests/test_new.py'}
    folder = tmp_path/'tests'; folder.mkdir()
    (folder/'test_new.py').write_text('def test_new(): pass', encoding='utf-8')
    (folder/'test_consumer.py').write_text(
        'def test_consumer():\n    from test_deleted import fixture\n', encoding='utf-8')
    (folder/'test_nested.py').write_text('from tests.test_consumer import test_consumer', encoding='utf-8')
    (folder/'test_unrelated.py').write_text('def test_unrelated(): pass', encoding='utf-8')
    plan = select_tests({'tests/test_deleted.py', 'tests/test_new.py'}, root=tmp_path)
    assert set(plan['tests']) == {'tests/test_new.py', 'tests/test_consumer.py', 'tests/test_nested.py'}
    assert not plan['full'] and not plan['package_impact']
    assert select_tests({'README.md', 'CONTRIBUTING.md', 'releases/example.md'})['tests'] == ()
    assert select_tests(set())['tests'] == ()


def test_current_code_is_mapped_and_selected_tests_still_exist():
    paths = {p.relative_to(ROOT).as_posix() for folder in ('cachemonitor', 'tools', 'native/src')
             for p in (ROOT/folder).rglob('*') if p.suffix in ('.py', '.qml', '.ps1', '.rs')}
    paths.update(('native/Cargo.toml', 'native/Cargo.lock', 'rust-toolchain.toml'))
    plan = select_tests(paths)
    assert not plan['unmapped'], plan['unmapped']
    for target in {test for tests in GROUPS.values() for test in tests}:
        path, _, node = target.partition('::')
        assert (ROOT/path).is_file(), target
        if node:
            tree = ast.parse((ROOT/path).read_text(encoding='utf-8-sig'))
            assert node in {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}, target


def test_removed_unmapped_subsystem_requires_full_source_and_package_checks(tmp_path):
    removed={'cachemonitor/retired_platform.py','.github/workflows/retired-platform.yml'}
    plan=select_tests(removed,root=tmp_path,deleted=removed)
    assert not plan['unmapped'] and plan['tests']==('tests',)
    assert plan['full'] and plan['package_impact'] and plan['proxy_impact']
    assert select_tests({'cachemonitor/new_contract.py'},root=tmp_path)['unmapped']
    path=tmp_path/'cachemonitor/new_contract.py';path.parent.mkdir();path.touch()
    assert select_tests({'cachemonitor/new_contract.py'},root=tmp_path,deleted={str(path.relative_to(tmp_path)).replace('\\','/')})['unmapped']


def test_unmapped_and_package_only_runs_do_not_advance_source_baseline(tmp_path, monkeypatch, capsys):
    import tools.verify_changes as verify
    baseline = tmp_path/'last-success.json'
    baseline.write_text('{"files": {}}', encoding='utf-8')
    original = baseline.read_bytes()
    monkeypatch.setattr(verify, 'BASELINE', baseline)
    monkeypatch.setattr(verify, 'REPORTS', tmp_path/'reports')
    monkeypatch.setattr(verify, 'file_hashes', lambda: {'cachemonitor/new_contract.py':'changed'})
    assert verify.main([]) == 2
    assert json.loads(capsys.readouterr().out)['result'] == 'unmapped'
    assert baseline.read_bytes() == original
    monkeypatch.setattr(verify, 'package_check', lambda exe, report: {'runtime':'verified'})
    executable = tmp_path/'synthetic.exe'; executable.write_bytes(b'package fixture')
    assert verify.main(['--package-only', '--package-exe', str(executable)]) == 0
    assert json.loads(capsys.readouterr().out)['pytest'] is None
    assert baseline.read_bytes() == original


def test_extended_checks_are_explicit_and_each_run_retains_test_timings(tmp_path):
    default = pytest_commands(['tests'], report_dir=tmp_path)[0][1]
    extended = pytest_commands(['tests'], extended=True, report_dir=tmp_path)[0][1]
    affected = pytest_commands(['tests/test_ui.py'], report_dir=tmp_path)[0][1]
    integration = pytest_commands(['tests'], integration=True, report_dir=tmp_path)[0][1]
    assert 'extended or not extended' not in default
    assert 'extended or not extended' in extended
    assert 'not extended' in affected and 'not extended' in integration
    assert 'not extended' not in default
    assert '--junitxml=' + str(tmp_path/'tests-core.xml') in default
    commands = pytest_commands(['tests'], report_dir=tmp_path)
    assert [kind for kind, _ in commands] == ['core', 'gui', 'installation']
    assert '--no-desktop' in commands[0][1]
    assert all('--no-desktop' not in command for _, command in commands[1:])
    targets = [part for _, command in commands for part in command if part.startswith('tests/test_')]
    assert len(targets) == len(set(targets))


@pytest.mark.parametrize('path,candidate,package,proxy,msix', [
    ('cachemonitor/version.py', True, True, False, False),
    ('cachemonitor/pricing.py', False, False, False, False),
    ('cachemonitor/qml/OverlayControls.qml', False, False, False, False),
    ('cachemonitor/install_activation.py', True, True, False, False),
    ('cachemonitor/model_proxy.py', True, True, False, False),
    ('cachemonitor/quick_smoke.py', False, True, False, False),
    ('requirements-release.lock', True, True, False, False),
    ('docs/releases/example.md', False, False, False, False),
])
def test_ci_keeps_changed_contracts_without_repeating_unrelated_release_checks(path,candidate,package,proxy,msix):
    from tools.ci_plan import build_plan
    plan = build_plan({path}, base='verified', candidate=candidate,version_only=True)
    assert (plan['package'],plan['proxy'],plan['msix']) == (package,proxy,msix)
    assert plan['native'] == (package and not msix)
    assert not plan['unmapped']
    assert plan['source_args'] == ['--basic']


def test_ci_extended_and_qa_tool_fallback_cannot_skip_required_checks():
    from tools.ci_plan import build_plan
    extended = build_plan(set(),base='verified',extended=True)
    assert extended['source_args'] == ['--extended']
    assert all(extended[key] for key in ('package','proxy','msix','scales','dependencies'))
    assert not extended['native']
    for reusable in (False,True):
        plan = build_plan({'tools/verify_proxy_update.py'},base='verified',reuse_qa=reusable)
        assert not plan['qa_reuse'] and not plan['qa_smokes']
        assert not plan['package'] and not plan['proxy']


def test_rust_changes_require_native_parity_and_packaging_without_proxy_migration():
    from tools.ci_plan import build_plan
    for path in ('native/src/lib.rs', 'native/Cargo.lock', 'native/Cargo.toml',
                 'rust-toolchain.toml', 'cachemonitor/native_backend.py', 'tools/build_native.py'):
        plan = build_plan({path}, base='verified')
        assert plan['rust'] and plan['package'] and plan['native'], path
        assert not plan['full'] and not plan['proxy'] and not plan['msix'], path
        assert not plan['unmapped'], path
        assert {'tests/test_native_index.py', 'tests/test_build_native.py'} <= set(plan['tests']), path
    assert build_plan({'cachemonitor/pricing.py'}, base='verified')['rust']
    assert build_plan({'README.md'}, base='verified')['rust']


def test_only_app_version_literal_can_bypass_proxy_compatibility_checks():
    from tools.verify_changes import version_contract
    before = "VERSION='1'\nPROXY_VERSION='p1'\ndef proxy_compatible(v):return v=='p1'\n"
    assert version_contract(before) == version_contract(before.replace("VERSION='1'", "VERSION='2'"))
    assert version_contract(before) != version_contract(before.replace("PROXY_VERSION='p1'", "PROXY_VERSION='p2'"))
    assert version_contract(before) != version_contract(before.replace("v=='p1'", 'True'))
    assert version_contract(before) != version_contract(before.replace("VERSION='1'", 'VERSION=compute()'))


def test_environment_failure_stops_before_tests_and_preserves_baseline(tmp_path,monkeypatch,capsys):
    import tools.verify_changes as verify
    import tools.check_environment as environment
    baseline=tmp_path/'last-success.json'
    baseline.write_text(json.dumps({'files':{'cachemonitor/dashboard.py':'old'}}),encoding='utf-8')
    original=baseline.read_bytes()
    monkeypatch.setattr(verify,'BASELINE',baseline)
    monkeypatch.setattr(verify,'REPORTS',tmp_path/'reports')
    monkeypatch.setattr(verify,'file_hashes',lambda:{'cachemonitor/dashboard.py':'changed'})
    problem=dict(kind='unlocked_import',module='yaml',file='tests/test_release_pipeline.py',line=1)
    monkeypatch.setattr(environment,'check_environment',lambda:dict(errors=[problem]))
    def unexpected(*args,**kwargs):raise AssertionError('Tests must not run after failed preflight')
    monkeypatch.setattr(verify.subprocess,'run',unexpected)
    assert verify.main([])==2
    result=json.loads(capsys.readouterr().out)
    assert result['result']=='environment_failed' and result['errors']==[problem]
    assert baseline.read_bytes()==original
    report=json.loads(__import__('pathlib').Path(result['report']).read_text(encoding='utf-8'))
    assert 'pytest' not in report and report['environment']['errors']==[problem]


def test_stale_native_extension_stops_before_tests_and_preserves_baseline(tmp_path,monkeypatch,capsys):
    import tools.verify_changes as verify
    import tools.check_environment as environment
    import tools.build_native as native
    baseline=tmp_path/'last-success.json'
    baseline.write_text(json.dumps({'files':{'cachemonitor/core.py':'old'}}),encoding='utf-8')
    original=baseline.read_bytes()
    binary=tmp_path/'_cpu_native.pyd';binary.write_bytes(b'old build')
    monkeypatch.setattr(verify,'BASELINE',baseline)
    monkeypatch.setattr(verify,'REPORTS',tmp_path/'reports')
    monkeypatch.setattr(verify,'file_hashes',lambda:{'cachemonitor/core.py':'changed'})
    monkeypatch.setattr(environment,'check_environment',lambda:dict(errors=[]))
    monkeypatch.setattr(native,'extension_path',lambda:binary)
    def stale():raise RuntimeError('Rust extension is stale or changed')
    monkeypatch.setattr(native,'checked_extension',stale)
    def unexpected(*args,**kwargs):raise AssertionError('Tests must not run with a stale extension')
    monkeypatch.setattr(verify.subprocess,'run',unexpected)
    assert verify.main([])==2
    result=json.loads(capsys.readouterr().out)
    assert result['result']=='native_failed'
    assert baseline.read_bytes()==original


@pytest.mark.parametrize('other', ['--full', '--integration', '--extended', '--package-only'])
def test_basic_rejects_conflicting_profiles_before_execution(other):
    import tools.verify_changes as verify
    with pytest.raises(SystemExit) as error:
        verify.main(['--basic', other, '--plan'])
    assert error.value.code == 2


@pytest.mark.parametrize('existing_baseline', [False, True])
def test_basic_success_preserves_local_baseline_and_gui_work(tmp_path, monkeypatch, capsys, existing_baseline):
    from types import SimpleNamespace
    import tools.verify_changes as verify
    import tools.check_environment as environment
    import tools.build_native as native
    baseline = tmp_path/'last-success.json'
    if existing_baseline:
        baseline.write_text(json.dumps({'files':{'cachemonitor/dashboard.py':'old'}}), encoding='utf-8')
    original = baseline.read_bytes() if existing_baseline else None
    monkeypatch.setattr(verify, 'BASELINE', baseline)
    monkeypatch.setattr(verify, 'REPORTS', tmp_path/'reports')
    monkeypatch.setattr(verify, 'file_hashes', lambda: {'cachemonitor/dashboard.py':'changed'})
    monkeypatch.setattr(verify, 'changed_files', lambda *args: {'cachemonitor/dashboard.py'})
    monkeypatch.setattr(verify, 'release_version_only', lambda **kwargs: False)
    monkeypatch.setattr(environment, 'check_environment', lambda: dict(errors=[]))
    monkeypatch.setattr(native, 'extension_path', lambda: tmp_path/'absent.pyd')
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'auto')
    executed = []
    def run(command, log):
        executed.append(command)
        log.write_text('Basic contracts passed', encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout='Basic contracts passed')
    monkeypatch.setattr(verify, 'run_logged', run)
    assert verify.main(['--basic']) == 0
    capsys.readouterr()
    reports = list((tmp_path/'reports').glob('*/report.json'))
    report = json.loads(reports[0].read_text(encoding='utf-8'))
    assert report['suite'] == 'basic' and not report['full'] and report['affected_tests'] == []
    assert report['tests'] == sorted(verify.CORE_TESTS)
    assert len(executed) == 1 and '--no-desktop' in executed[0] and 'core' in executed[0]
    assert report['pytest']['runs'][0]['exit_code'] == 0
    if existing_baseline:
        assert baseline.read_bytes() == original
    else:
        assert not baseline.exists()
    assert verify.main(['--plan']) == 0
    assert 'tests/test_ui.py' in json.loads(capsys.readouterr().out)['tests']


def test_basic_missing_baseline_stays_basic_and_keeps_unmapped_guard(tmp_path, monkeypatch, capsys):
    import tools.verify_changes as verify
    monkeypatch.setattr(verify, 'BASELINE', tmp_path/'missing.json')
    monkeypatch.setattr(verify, 'file_hashes', lambda: {})
    assert verify.main(['--basic', '--plan']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['suite'] == 'basic' and not plan['full'] and plan['affected_tests'] == []
    assert plan['tests'] == sorted(verify.CORE_TESTS)
    monkeypatch.setattr(verify, 'file_hashes', lambda: {'cachemonitor/unmapped_new.py':'new'})
    monkeypatch.setattr(verify, 'changed_files', lambda *args: {'cachemonitor/unmapped_new.py'})
    assert verify.main(['--basic', '--plan']) == 2
    assert json.loads(capsys.readouterr().out)['unmapped'] == ['cachemonitor/unmapped_new.py']


@pytest.fixture
def diagnostic_state(monkeypatch):
    import tools.check_diagnostics as diagnostics
    diagnostics.reset()
    monkeypatch.setattr(diagnostics, 'emit', lambda value: None)
    yield
    diagnostics.reset()


def test_first_diagnostic_survives_large_output_and_product_failure(tmp_path, diagnostic_state):
    import sys
    from tools.verify_changes import run_logged
    first = tmp_path/'first.json'; first.mkdir()
    second = tmp_path/'second.json'; second.mkdir()
    source = (
        'from pathlib import Path\n'
        'from tools.check_diagnostics import DiagnosticFailure, write_json, emit, status\n'
        'import sys\n'
        f'for name in ({str(first)!r}, {str(second)!r}):\n'
        '    try: write_json(Path(name), {})\n'
        '    except DiagnosticFailure: pass\n'
        'sys.stdout.write("x" * 1100000 + "\\n")\n'
        'emit(dict(product_exit_code=7, diagnostics=status()))\n'
        'sys.exit(7)\n')
    result = run_logged([sys.executable, '-u', '-c', source], tmp_path/'check.log')
    assert result.returncode == result.product_exit_code == 7
    assert result.diagnostics['first_error']['path'] == str(first)
    assert result.diagnostics['first_error']['exception_type'] in ('PermissionError', 'IsADirectoryError')
    assert str(first) not in result.stdout


def verification_probe(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import tools.verify_changes as verify
    import tools.check_environment as environment
    import tools.build_native as native
    baseline = tmp_path/'last-success.json'
    baseline.write_text(json.dumps({'files':{'cachemonitor/dashboard.py':'before'}}), encoding='utf-8')
    monkeypatch.setattr(verify, 'BASELINE', baseline)
    monkeypatch.setattr(verify, 'REPORTS', tmp_path/'reports')
    monkeypatch.setattr(verify, 'file_hashes', lambda: {'cachemonitor/dashboard.py':'after'})
    monkeypatch.setattr(verify, 'release_version_only', lambda **kwargs: False)
    monkeypatch.setattr(environment, 'check_environment', lambda: dict(errors=[]))
    monkeypatch.setattr(native, 'extension_path', lambda: tmp_path/'absent.pyd')
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'auto')
    def run(command, log):
        log.write_text('Check completed', encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout='Check completed')
    monkeypatch.setattr(verify, 'run_logged', run)
    return verify, baseline


@pytest.mark.parametrize('product_code', [0, 9])
def test_report_failure_preserves_product_status_and_baseline(tmp_path, monkeypatch, capsys,
                                                            diagnostic_state, product_code):
    from types import SimpleNamespace
    verify, baseline = verification_probe(tmp_path, monkeypatch)
    original = baseline.read_bytes()
    def run(command, log):
        log.write_text('Check completed', encoding='utf-8')
        (log.parent/'report.json').mkdir(exist_ok=True)
        return SimpleNamespace(returncode=product_code, stdout='Check completed')
    monkeypatch.setattr(verify, 'run_logged', run)
    assert verify.main([]) == (product_code or 86)
    result = json.loads(capsys.readouterr().out)
    assert result['product_exit_code'] == product_code
    assert not result['report_persisted']
    assert result['diagnostics']['first_error']['path'].endswith('report.json')
    assert baseline.read_bytes() == original
    assert __import__('pathlib').Path(result['rerun_manifest']).is_file()


def test_relocated_rerun_keeps_selection_resolved_base_and_original_baseline(tmp_path, monkeypatch, capsys, diagnostic_state):
    import base64
    from pathlib import Path
    from types import SimpleNamespace
    verify, baseline = verification_probe(tmp_path, monkeypatch)
    original = baseline.read_bytes()
    resolved = 'a' * 40
    def git_bytes(*args):
        if args[0] == 'rev-parse': return resolved.encode()
        if args[0] == 'diff': return b'cachemonitor/dashboard.py\0'
        if args[0] == 'ls-files': return b''
        raise AssertionError(args)
    monkeypatch.setattr(verify, 'git_bytes', git_bytes)
    def fail(command, log):
        log.write_text('Product failed', encoding='utf-8')
        return SimpleNamespace(returncode=9, stdout='Product failed')
    monkeypatch.setattr(verify, 'run_logged', fail)
    first_root = tmp_path/'first'
    assert verify.main(['--base', 'mutable-ref', '--diagnostics', str(first_root)]) == 9
    first = json.loads(capsys.readouterr().out)
    manifest = Path(first['rerun_manifest'])
    frozen = json.loads(manifest.read_text(encoding='utf-8'))
    assert frozen['base_commit'] == resolved
    assert base64.b64decode(frozen['baseline']['content']) == original
    first_report = json.loads(Path(first['report']).read_text(encoding='utf-8'))
    baseline.write_text(json.dumps({'files':{'cachemonitor/dashboard.py':'after'}}), encoding='utf-8')
    newer = baseline.read_bytes()
    def passed(command, log):
        log.write_text('Passed', encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout='Passed')
    monkeypatch.setattr(verify, 'run_logged', passed)
    retry_root = tmp_path/'relocated'
    assert verify.main(['--rerun', str(manifest), '--diagnostics', str(retry_root)]) == 86
    retry = json.loads(capsys.readouterr().out)
    retry_report = json.loads(Path(retry['report']).read_text(encoding='utf-8'))
    assert retry_report['tests'] == first_report['tests']
    assert retry_report['affected_tests'] == first_report['affected_tests']
    assert retry_report['base_commit'] == resolved
    assert Path(retry['diagnostics_directory']).parent == retry_root
    assert retry['diagnostics']['first_error']['operation'] == 'advance verification baseline'
    assert baseline.read_bytes() == newer
    with pytest.raises(SystemExit) as conflict:
        verify.main(['--rerun', str(manifest), '--basic'])
    assert conflict.value.code == 2
    monkeypatch.setattr(verify, 'file_hashes', lambda: {'cachemonitor/dashboard.py':'drift'})
    assert verify.main(['--rerun', str(manifest), '--diagnostics', str(tmp_path/'drift')]) == 86
    assert not (tmp_path/'drift').exists()


def test_diagnostic_output_failure_retains_replay_and_plan_stays_read_only(tmp_path, monkeypatch, capsys, diagnostic_state):
    from pathlib import Path
    verify, baseline = verification_probe(tmp_path, monkeypatch)
    original = baseline.read_bytes()
    blocked = tmp_path/'blocked'; blocked.write_text('file')
    assert verify.main(['--diagnostics', str(blocked)]) == 86
    result = json.loads(capsys.readouterr().out)
    assert result['diagnostics_root'] == str(blocked)
    assert result['product_exit_code'] is None and not result['report_persisted']
    assert Path(result['rerun_manifest']).is_file() and '--rerun' in result['rerun_command']
    target = tmp_path/'plan-only'
    assert verify.main(['--plan', '--diagnostics', str(target)]) == 0
    assert json.loads(capsys.readouterr().out)['diagnostics_root'] == str(target)
    assert not target.exists() and baseline.read_bytes() == original


def test_diagnostic_output_cannot_enter_source_snapshot(tmp_path, capsys):
    import tools.verify_changes as verify
    with pytest.raises(SystemExit) as rejected:
        verify.main(['--plan', '--diagnostics', str(ROOT/'tools'/'diagnostic-output')])
    assert rejected.value.code == 2
    assert not (ROOT/'tools'/'diagnostic-output').exists()



def test_rerun_preserves_absent_baseline_and_basic_selection(tmp_path, monkeypatch, capsys, diagnostic_state):
    from pathlib import Path
    verify, baseline = verification_probe(tmp_path, monkeypatch)
    baseline.unlink()
    assert verify.main(['--basic']) == 0
    first = json.loads(capsys.readouterr().out)
    manifest = json.loads(Path(first['rerun_manifest']).read_text(encoding='utf-8'))
    assert manifest['baseline']['content'] is None and manifest['baseline']['sha256'] is None
    assert verify.main(['--rerun', first['rerun_manifest'], '--diagnostics', str(tmp_path/'retry')]) == 0
    retry = json.loads(capsys.readouterr().out)
    report = json.loads(Path(retry['report']).read_text(encoding='utf-8'))
    assert report['suite'] == 'basic' and report['tests'] == sorted(verify.CORE_TESTS)
    assert not baseline.exists()


def test_unavailable_rerun_manifest_stops_before_checks(tmp_path, monkeypatch, capsys, diagnostic_state):
    import tempfile
    verify, baseline = verification_probe(tmp_path, monkeypatch)
    original = baseline.read_bytes()
    def unavailable(**kwargs):
        raise PermissionError('Temporary storage is unavailable')
    monkeypatch.setattr(tempfile, 'NamedTemporaryFile', unavailable)
    assert verify.main([]) == 86
    result = json.loads(capsys.readouterr().out)
    assert result['rerun_manifest'] is None and result['rerun_command'] is None
    assert result['product_exit_code'] is None
    assert result['diagnostics']['first_error']['operation'] == 'write rerun manifest'
    assert not (tmp_path/'reports').exists() and baseline.read_bytes() == original



def test_check_output_ignores_non_protocol_json_and_keeps_split_outcome():
    from tools.check_diagnostics import CheckOutput
    output = CheckOutput()
    for value in (None, [], 'text', 7, {'diagnostics':[]}, {'diagnostics':{'first_error':[]}},
                  {'product_exit_code':False}, {'product_exit_code':'0'}):
        output.feed('[codexon-check] ' + json.dumps(value) + '\n')
    assert output.first_error is None and not output.has_outcome
    output.feed('[codexon-check] {"product_exit_')
    output.feed('code": 9}\n')
    assert output.product_exit_code == 9 and output.has_outcome


def test_rerun_command_round_trips_powershell_arguments(tmp_path, monkeypatch, capsys, diagnostic_state):
    from venv import EnvBuilder
    import subprocess
    verify, _ = verification_probe(tmp_path, monkeypatch)
    special = tmp_path/"Python path $literal's`value"
    EnvBuilder(with_pip=False).create(special)
    executable = special/'Scripts'/'python.exe'
    tools = special/'tools'; tools.mkdir()
    received = special/'received.json'
    (tools/'verify_changes.py').write_text(
        'import json, sys\nfrom pathlib import Path\n'
        f'Path({str(received)!r}).write_text(json.dumps(sys.argv), encoding="utf-8")\n', encoding='utf-8')
    manifest = special/"manifest $literal's`value.json"
    manifest.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(verify, 'ROOT', special)
    monkeypatch.setattr(verify, 'version_contract', lambda: 'contract')
    monkeypatch.setattr(verify.sys, 'executable', str(executable))
    monkeypatch.setattr(verify, 'save_rerun', lambda selection: manifest)
    output = tmp_path/"output path $literal's`value"
    assert verify.main(['--basic', '--diagnostics', str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    replay = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                             result['rerun_command']], capture_output=True, text=True, timeout=20)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert json.loads(received.read_text(encoding='utf-8')) == [
        str(tools/'verify_changes.py'), '--rerun', str(manifest), '--diagnostics', str(output)]


def test_logged_capture_drains_output_written_at_process_exit(tmp_path, monkeypatch, capsys, diagnostic_state):
    import tools.verify_changes as verify
    error = dict(operation='write final evidence', path=str(tmp_path/'blocked.json'),
                 exception_type='PermissionError', message='blocked evidence', pid=123)
    payload = 'final stdout\n' + '[codexon-check] ' + json.dumps(
        dict(product_exit_code=7, diagnostics=dict(first_error=error)))
    class Process:
        returncode = None

        def __init__(self, command, *, stdout, **kwargs):
            self.output = stdout

        def poll(self):
            if self.returncode is None:
                self.output.write(payload.encode('utf-8'))
                self.returncode = 86
            return self.returncode

        def wait(self):
            return self.poll()

    monkeypatch.setattr(verify.subprocess, 'Popen', Process)
    log = tmp_path/'check.log'
    result = verify.run_logged(['controlled-process'], log)
    assert result.returncode == result.product_exit_code == 7
    assert result.diagnostics['first_error'] == error
    assert result.stdout == log.read_text(encoding='utf-8') == capsys.readouterr().out == payload


@pytest.mark.parametrize('stage,package_code,source_first', [
    ('runtime', 7, False), ('runtime', 0, True), ('smoke', 11, True), ('smoke', 0, False)])
def test_package_evidence_failure_retains_known_product_outcome(tmp_path, monkeypatch, capsys,
                                                              diagnostic_state, stage, package_code, source_first):
    from pathlib import Path
    from types import SimpleNamespace
    from cachemonitor.version import VERSION, PROXY_VERSION
    verify, baseline = verification_probe(tmp_path, monkeypatch)
    original = baseline.read_bytes()
    executable = tmp_path/'package.exe'; executable.write_bytes(b'package probe')
    calls = []
    def run(command, **kwargs):
        current = 'runtime' if '--verify-runtime' in command else 'smoke'
        calls.append(current)
        evidence = Path(command[command.index('--verify-runtime' if current == 'runtime' else '--smoke') + 1])
        if current == stage:
            evidence = evidence if current == 'runtime' else evidence.with_suffix('.json')
            evidence.write_text('{', encoding='utf-8')
            return SimpleNamespace(returncode=package_code, stderr='package product failure')
        evidence.write_text(json.dumps(dict(errors=[], version=VERSION, proxy_version=PROXY_VERSION,
                                            native=dict(abi_version=1))), encoding='utf-8')
        return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(verify.subprocess, 'run', run)
    monkeypatch.setattr(verify, 'fixture_home', lambda folder: folder)
    args = ['--package-exe', str(executable)] + ([] if source_first else ['--package-only'])
    assert verify.main(args) == (package_code or 86)
    result = json.loads(capsys.readouterr().out)
    report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
    assert result['product_exit_code'] == report['product_exit_code'] == (package_code or None)
    assert result['diagnostics']['first_error']['operation'] == f'read package {stage} evidence'
    assert calls == (['runtime'] if stage == 'runtime' else ['runtime', 'smoke'])
    assert ('pytest' in report) == source_first
    assert baseline.read_bytes() == original
