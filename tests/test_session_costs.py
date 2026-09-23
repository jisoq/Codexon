"""Session costs include confirmed descendants without duplicating account totals."""

import copy
import time

import pytest

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.session_costs import own_costs, session_costs
from cachemonitor.core import Session
from test_data_contract import source


def test_nested_costs_empty_parent_partial_prices_and_home_boundary():
    sessions = [dict(home='h', id='parent'),
                dict(home='h', id='child', parent_thread_id='parent'),
                dict(home='h', id='grandchild', parent_thread_id='child'),
                dict(home='other', id='child', parent_thread_id='parent')]
    rows = [dict(home='h', sid='child', cost=2.0, input=100, cached=50, ts=10),
            dict(home='h', sid='grandchild', cost=None, input=50, cached=0, ts=11),
            dict(home='other', sid='child', cost=9.0, input=100, cached=100, ts=12)]
    result = session_costs(sessions, own_costs(rows))
    parent = result[('h', 'parent')]
    assert parent['own_cost'] == 0 and parent['child_cost'] == 2.0
    assert (parent['cost'], parent['priced'], parent['calls'], parent['descendants']) == (2.0, 1, 2, 2)
    assert parent['partial'] and parent['latest_ts'] == 11
    assert parent['cache_ratio'] == pytest.approx(100*50/150)
    assert result[('h', 'child')]['cost'] == 2.0
    assert result[('other', 'child')]['cost'] == 9.0
    assert session_costs(sessions, own_costs(rows[:1]))[('h', 'parent')]['cost'] == 2.0


def test_unknown_cost_and_bad_lineage_never_look_like_confirmed_zero():
    sessions = [dict(home='h', id='parent'),
                dict(home='h', id='child', parent_thread_id='parent'),
                dict(home='h', id='orphan', parent_thread_id='missing'),
                dict(home='h', id='cycle-a', parent_thread_id='cycle-b'),
                dict(home='h', id='cycle-b', parent_thread_id='cycle-a')]
    rows = [dict(home='h', sid='child', cost=None, ts=1),
            dict(home='h', sid='orphan', cost=3.0, ts=2),
            dict(home='h', sid='cycle-a', cost=4.0, ts=3)]
    result = session_costs(sessions, own_costs(rows))
    assert result[('h', 'parent')]['cost'] is None
    assert result[('h', 'parent')]['partial']
    assert result[('h', 'parent')]['descendants'] == 1
    assert result[('h', 'orphan')]['cost'] == 3.0
    assert result[('h', 'cycle-b')]['cost'] is None


def test_overlay_parent_refreshes_for_child_cost_without_recounting_responses():
    parent = source()
    child = source([dict(input_tokens=100,cached_input_tokens=0,
                         cache_write_input_tokens=10,output_tokens=20,reasoning_output_tokens=5)])
    child.update(id='child', title='Child', source='subagent', parent_thread_id=parent['id'])
    child['history'][0].update(key='child-response', call_id='child-response')
    engine = AnalysisEngine();summaries = OverlaySummaries()
    engine.ingest([parent, child])
    first = {(s['home'], s['id']):s for s in summaries.collect(engine)}
    own_parent = first[('home', 's')]['own_cost']
    own_child = first[('home', 'child')]['cost']
    assert first[('home', 's')]['cost'] == pytest.approx(own_parent + own_child)
    assert first[('home', 's')]['descendants'] == 1
    assert first[('home', 's')]['calls']==2 and first[('home', 's')]['priced']==2
    assert first[('home', 's')]['mean_cost']==pytest.approx((own_parent+own_child)/2)
    assert first[('home', 's')]['token_composition']['total']==240
    assert first[('home', 's')]['token_composition']['cached']==80
    assert first[('home', 's')]['token_composition']['cache_hit_rate']==pytest.approx(40)
    assert first[('home', 's')]['cache_rate']==80  # Most recent own call stays distinct.
    assert len(first[('home', 's')]['all_calls'])==1  # Recent call remains this task's own call.
    query = engine.query(dict(page=0, start=0, end=200))
    assert query['analysis']['totals']['cost'] == pytest.approx(own_parent + own_child)
    assert len(query['analysis']['responses']) == 2
    changed = copy.deepcopy(child)
    changed['history'][0]['output'] *= 2
    engine.ingest([parent, changed])
    second = {(s['home'], s['id']):s for s in summaries.collect(engine)}
    assert second[('home', 's')]['cost'] > first[('home', 's')]['cost']
    assert second[('home', 's')]['own_cost'] == own_parent


def test_real_session_table_shows_parent_without_own_calls_and_click_keeps_breakdown(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.quick_qa import click_row

    now = time.time()
    parent = Session('parent', 'fixture', title='부모 작업').view(now)
    parent.update(source='user', archived=False, collection_complete=True)
    child_session = Session('child', 'fixture', title='하위 작업')
    child_session.add_usage(now-10, 'child-call', dict(input_tokens=100, cached_input_tokens=20,
        cache_write_input_tokens=0, output_tokens=10), 'gpt-6-astra', 'turn', 'high', service_tier='Standard')
    child = child_session.view(now)
    child.update(source='subagent', parent_thread_id='parent', archived=False, collection_complete=True)
    snapshot = dict(ts=now, sessions=[parent, child], homes=['fixture'], errors=[],
                    unassigned=[], index={'loading':False})
    app = QApplication.instance() or QApplication([])
    before = app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration', True)
    window = Dashboard(['fixture'], start_worker=False,
        settings=QSettings(str(tmp_path/'dashboard.ini'), QSettings.IniFormat), static_snapshot=snapshot)
    try:
        window.show();window.change_page(2);app.processEvents()
        records = window.session_records(window.analysis['responses'])
        group = next(r for r in records if r['sid']=='parent')
        own_child = next(r for r in records if r['sid']=='child')
        assert group['own_cost']==0 and group['cost']==own_child['cost']
        assert group['descendants']==1 and group['calls']==1
        index = next(i for i,r in enumerate(window.parent_rows) if r['sid']=='parent')
        click_row(window, window.parent_table, index);app.processEvents()
        assert window.selected_session==('fixture','parent')
        assert '자체 ' in window.session_scope.text() and ' + 하위 ' in window.session_scope.text()
        assert window.grab().save(str(tmp_path/'parent-cost.png'))
    finally:
        window.quitting=True;window.tick.stop();window.tray.hide();window.observer_panel.stop();window.close()
        app.setProperty('cachemonitorDisableShellIntegration', before)


def test_overlay_cost_label_changes_only_for_confirmed_child(tmp_path):
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay import SessionOverlay

    parent = source()
    child = copy.deepcopy(parent)
    child.update(id='child', title='Child', source='subagent', parent_thread_id=parent['id'])
    child['history'][0].update(key='child-response', call_id='child-response')
    engine = AnalysisEngine();engine.ingest([parent,child])
    summaries = {(s['home'],s['id']):s for s in OverlaySummaries().collect(engine)}
    app = QApplication.instance() or QApplication([])
    overlay = SessionOverlay()
    try:
        overlay.set_content(summaries[('home','s')]);overlay.show();app.processEvents()
        assert '현재 작업' in overlay.accessibleName()
        assert '세션 전체 · 하위 1개 포함' in overlay.accessibleName()
        assert '세션 비용 ' in overlay.accessibleName()
        assert '토큰 구성' in overlay.accessibleName()
        assert '(하위 합산)' not in overlay.accessibleName()
        assert '세션 호출당 평균 ' in overlay.accessibleName()
        assert '세션 2호출' in overlay.accessibleName()
        assert overlay.grab().save(str(tmp_path/'parent-overlay.png'))
        overlay.set_content(summaries[('home','child')]);app.processEvents()
        assert '세션 비용 ' in overlay.accessibleName()
        assert '세션 전체' in overlay.accessibleName()
        assert '하위 1개 포함' not in overlay.accessibleName()
    finally:
        overlay.close()
