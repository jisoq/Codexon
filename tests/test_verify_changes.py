"""Selected checks must cover the changed contract without unrelated global gates."""

import ast
import json
import subprocess
import sys

from tools.verify_changes import ROOT, GROUPS, changed_files, fixture_home, select_tests


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


def test_calculation_protocol_and_unknown_changes_have_explicit_gates():
    for source in ('cachemonitor/cache_execution.py','cachemonitor/cache_capture.py','cachemonitor/model_proxy.py',
                   'cachemonitor/cache_scheduler.py','cachemonitor/cache_control.py','cachemonitor/cache_hooks.py','cachemonitor/cache_operating.py'):
        chosen=select_tests({source})
        assert 'tests/test_cache_codex_connection.py' in chosen['tests']
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
    paths = {p.relative_to(ROOT).as_posix() for folder in ('cachemonitor', 'tools')
             for p in (ROOT/folder).rglob('*') if p.suffix in ('.py', '.qml', '.ps1')}
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
    assert verify.main(['--package-only', '--package-exe', 'synthetic.exe']) == 0
    assert json.loads(capsys.readouterr().out)['pytest'] is None
    assert baseline.read_bytes() == original


def test_isolated_source_smoke_renders_parent_cost(tmp_path):
    home = fixture_home(tmp_path)
    image = tmp_path/'smoke.png'
    result = subprocess.run([sys.executable,'-B',str(ROOT/'run.py'),
                             '--codex-home',str(home),'--index-path',str(tmp_path/'index.sqlite'),
                             '--smoke',str(image),'--smoke-depth','core'],
                            cwd=ROOT,capture_output=True,text=True,timeout=120)
    report = json.loads(image.with_suffix('.json').read_text(encoding='utf-8'))
    assert result.returncode==0, report.get('errors') or result.stderr
    assert report['depth']=='core' and report['errors']==[]
    assert report['session_rollup']['descendants']==1
    assert report['session_rollup']['cost']>report['session_rollup']['own']
    assert report['model_requests']==report['live_quota_requests']==0
