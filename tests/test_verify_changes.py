"""The release selector must widen, never silently omit, uncertain changes."""

import json
import subprocess
import sys

from tools.verify_changes import ROOT, changed_files, fixture_home, select_tests


def test_price_and_session_cost_changes_select_persistence_and_real_ui_checks():
    price = select_tests({'cachemonitor/pricing.py'})
    assert not price['full'] and 'pricing_quota' in price['groups']
    assert 'tests/test_pricing.py' in price['tests']
    assert 'tests/test_quota_tracking_integration.py' in price['tests']
    cost = select_tests({'cachemonitor/session_costs.py','cachemonitor/dashboard.py','cachemonitor/overlay_data.py'})
    assert {'cost','ui'} <= set(cost['groups'])
    assert {'tests/test_session_costs.py','tests/test_ui.py'} <= set(cost['tests'])


def test_shared_color_version_and_unknown_code_widen_the_right_gates():
    color = select_tests({'cachemonitor/token_colors.py'})
    assert {'ui','overlay'} <= set(color['groups'])
    version = select_tests({'cachemonitor/version.py'})
    assert version['package_impact'] and version['proxy_impact']
    assert 'tests/test_proxy_update.py' in version['tests']
    assert select_tests({'cachemonitor/unmapped_new.py'})['full']
    assert select_tests({'tests/conftest.py'})['full']


def test_new_deleted_and_changed_tests_and_docs_are_accounted_for():
    current = {'cachemonitor/core.py':'new','tests/test_new.py':'x'}
    previous = {'files':{'cachemonitor/core.py':'old','cachemonitor/deleted.py':'y'}}
    assert changed_files(current, previous)=={'cachemonitor/core.py','cachemonitor/deleted.py','tests/test_new.py'}
    selected = select_tests({'tests/test_new.py','tests/test_performance.py'})
    assert {'tests/test_new.py','tests/test_performance.py'} <= set(selected['tests'])
    assert select_tests({'README.md','releases/2026.09.23.3.md'})['tests']==()
    assert select_tests(set())['tests']==()


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
