"""Session costs include confirmed descendants without duplicating account totals."""

import copy
import time

import pytest

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.session_costs import own_costs, session_costs
from cachemonitor.core import Session
from test_data_contract import source


def review_session(parent):
    review=copy.deepcopy(parent)
    review.update(id='review',source='subagent',parent_thread_id=parent['id'])
    review['history'][0].update(key='review-response',call_id='review-response',
                               model='codex-auto-review',configured_model='codex-auto-review')
    return review


def test_internal_review_is_separate_from_work_models_rollups_and_delivery(tmp_path):
    from cachemonitor.analytics import analyze
    from cachemonitor.analysis_delivery import SnapshotPublisher, SnapshotReceiver
    parent=source();review=review_session(parent)
    worker=copy.deepcopy(parent)
    worker.update(id='worker',source='subagent',parent_thread_id=parent['id'])
    worker['history'][0].update(key='worker-response',call_id='worker-response',written=None)
    engine=AnalysisEngine();overlays=OverlaySummaries()
    engine.ingest([parent,review,worker])
    assert len(engine.internal_sessions)==1
    analysis=engine.query(dict(page=0,start=0,end=200))['analysis']
    assert {r['sid'] for r in analysis['responses']}=={'s','worker'}
    assert analysis['unpriced_count']==1  # A real worker with missing usage stays visible.
    assert len(analyze([parent,review,worker])['responses'])==2
    summary=next(s for s in overlays.collect(engine) if s['id']=='s')
    assert (summary['calls'],summary['priced'],summary['missing'],summary['descendants'])==(2,1,1,1)
    snapshot=dict(ts=110,sessions=[parent,review,worker],homes=['home'])
    publisher=SnapshotPublisher();receiver=SnapshotReceiver()
    delivered=receiver.receive(publisher.publish(snapshot,engine,overlays.collect(engine)))
    assert delivered['internal_review_calls']==1
    assert {s['id'] for s in delivered['sessions']}=={'s','worker'}
    assert 'codex-auto-review' not in delivered['models']
    # Internal calls are retained unchanged for diagnostics/accounting, not priced as zero.
    assert review['history'][0]['model']=='codex-auto-review'
    assert engine.internal_sessions[('home','review')]['prepared']['history'][0]['cost'] is None
    engine.ingest([parent,review])
    summary=next(s for s in overlays.collect(engine) if s['id']=='s')
    assert (summary['calls'],summary['priced'],summary['missing'],summary.get('descendants',0))==(1,1,0,0)
    # Empty owners classify by their model metadata before any call exists.
    empty=dict(parent,id='empty',source='cli',history=[],model='codex-auto-review',usage_revision=1)
    engine.ingest([empty])
    assert ('home','empty') in engine.internal_sessions
    engine.ingest([dict(empty,model='gpt-6-astra')])
    assert ('home','empty') in engine.sessions and not engine.internal_sessions


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


def test_overlay_parent_refreshes_for_child_cost_without_recounting_responses(monkeypatch):
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
    assert first[('home', 's')]['cache_rate']==first[('home', 's')]['latest']['cache_rate']
    assert len(first[('home', 's')]['recent'])==2
    assert 'all_calls' not in first[('home', 's')]
    query = engine.query(dict(page=0, start=0, end=200))
    assert query['analysis']['totals']['cost'] == pytest.approx(own_parent + own_child)
    assert len(query['analysis']['responses']) == 2
    changed = copy.deepcopy(child)
    changed['history'][0]['output'] *= 2
    engine.ingest([parent, changed])
    class UnchangedHistory(list):
        def __iter__(self):raise AssertionError('A child update reread unchanged parent calls')
    engine.sessions[('home','s')]['prepared']['history']=UnchangedHistory(engine.sessions[('home','s')]['prepared']['history'])
    # Classifier reuse is covered independently; this contract owns overlay rollups.
    monkeypatch.setattr(summaries.speed_health,'collect',lambda engine:{})
    second = {(s['home'], s['id']):s for s in summaries.collect(engine)}
    assert second[('home', 's')]['cost'] > first[('home', 's')]['cost']
    assert second[('home', 's')]['own_cost'] == own_parent


def test_overlay_rollups_keep_weighted_populations_and_bounded_recent_context():
    from types import SimpleNamespace
    from cachemonitor.analytics import output_speed_summary
    from cachemonitor.overlay_data import token_composition,cost_composition,summarize_models
    from test_overlay_call_data import row,session
    parent=session([row(i,home='home',sid='parent',cost=.1,cost_uncached=.01,cost_cached=.02,
        cost_written=.01,cost_unclassified=.01,cost_output=.05,cost_reasoning=.02,cost_non_reasoning=.03,
        response_status='completed',completion_latency_ms=100.+i,purpose='maintenance' if i%2 else 'work')
        for i in range(20)],id='parent',unclassified={'total':12345})
    child=session([row(30,home='home',sid='child',input=None,cached=None,written=None,cost=None,
                       analysis_model='gpt-6-sol'),
                   row(31,home='home',sid='child',input=10,cached=5,written=20,input_conflict=True,
                       output_conflict=True,cost=.2,cost_uncached=None,cost_cached=.01,cost_written=.02,
                       cost_unclassified=.03,cost_output=.14,cost_reasoning=.04,cost_non_reasoning=.10,
                       response_status='completed',completion_latency_ms=50.,purpose='maintenance')],
        id='child',parent_thread_id='parent',coverage_gaps=[{'start':1}],unclassified={'total':99999})
    empty=session([],id='empty',parent_thread_id='parent')
    states={}
    for value in (parent,child,empty):
        value.update(cache_misses={'events':[]},cache_health={'events':[]},collection_complete=True)
        states[(value['home'],value['id'])]=dict(source=value,prepared=value,revision=1,content_revision=1)
    summaries=OverlaySummaries();summaries.speed_health=SimpleNamespace(collect=lambda engine:{})
    result={value['id']:value for value in summaries.collect(SimpleNamespace(sessions=states))}['parent']
    rows=parent['history']+child['history']
    assert result['token_composition']==token_composition({'history':rows})
    assert result['token_composition']['cache_hit_rate']==pytest.approx(100*(20*900+5)/(20*1000+10))
    assert result['unattached_total']==12345 and result['coverage_gap']
    assert not result['own_coverage_gap']
    assert result['own_model_breakdown']==summarize_models(parent['history'])
    models={value['model']:value for value in result['model_breakdown']}
    assert set(models)=={'gpt-6-astra','gpt-6-sol'}
    assert models['gpt-6-sol']['cost'] is None and models['gpt-6-sol']['priced']==0
    assert sum(value['calls'] for value in models.values())==result['calls']
    assert sum(value['priced'] for value in models.values())==result['priced']
    assert sum(value['cost'] or 0 for value in models.values())==pytest.approx(result['cost'])
    for key,count in result['token_composition']['counts'].items():
        assert sum(value['token_composition']['counts'][key] for value in models.values())==count
    for value in models.values():
        expected_rows=[item for item in rows if (item.get('analysis_model') or item['model'])==value['model']]
        assert value['token_composition']==token_composition({'history':expected_rows})
    expected=output_speed_summary(rows)
    assert result['output_speed_summary']==pytest.approx(expected)
    expected=cost_composition(rows)
    for section in ('input','output'):
        assert result['cost_composition'][section+'_total']==pytest.approx(expected[section+'_total'])
        for actual,wanted in zip(result['cost_composition'][section+'_parts'],expected[section+'_parts']):
            assert actual['key']==wanted['key'] and actual['known']==wanted['known']
            assert actual['tokens']==pytest.approx(wanted['tokens'])
            assert actual['share']==pytest.approx(wanted['share'])
    assert result['maintenance_calls']==11 and result['maintenance_missing']==0
    assert result['maintenance_cost']==pytest.approx(1.2)
    assert len(result['recent'])==12 and result['recent'][-1]['sid']=='child'
    assert result['recent'][-1]['source_title']==child['title']


def test_subagent_finance_counts_nested_owners_once_and_tracks_reparenting():
    from types import SimpleNamespace
    from cachemonitor.overlay_data import token_composition,summarize_models
    from test_overlay_call_data import row,session
    sources=[session([row(0,cost=10)],id='parent'),
             session([row(1,cost=2,model='gpt-6.1-sol',requested_model='gpt-6.1-sol',response_model='gpt-6.1-sol',
                          response_status='completed',model_match='일치',effort='xhigh')],id='child',parent_thread_id='parent'),
             session([row(2,cost=.5),row(3,cost=None,analysis_model='gpt-6-sol',input=None,cached=None)],
                     id='grandchild',parent_thread_id='child',coverage_gaps=[{'start':1}]),
             session([],id='empty',parent_thread_id='parent'),session([],id='other')]
    states={}
    for value in sources:
        value.update(cache_misses={'events':[]},cache_health={'events':[]},collection_complete=True)
        states[(value['home'],value['id'])]=dict(source=value,prepared=value,revision=1,content_revision=1)
    summaries=OverlaySummaries();summaries.speed_health=SimpleNamespace(collect=lambda engine:{})
    engine=SimpleNamespace(sessions=states)
    first={value['id']:value for value in summaries.collect(engine)}
    finance=first['parent']['subagent_finance']
    assert (finance['cost'],finance['calls'],finance['priced'],finance['missing'])==(2.5,3,2,1)
    assert finance['coverage_gap'] and first['parent']['own_cost']==10
    agents={value['id']:value for value in finance['agents']}
    assert set(agents)=={'child','grandchild','empty'}
    assert agents['child']['cost']==2 and agents['grandchild']['cost']==.5
    assert agents['empty']['cost']==0 and agents['empty']['calls']==0
    assert (agents['child']['model'],agents['child']['effort'],agents['child']['model_state'])==('gpt-6.1-sol','xhigh','일치')
    assert agents['grandchild']['model']=='gpt-6-sol' and agents['grandchild']['model_state']=='미확인'
    descendants=sources[1]['history']+sources[2]['history']
    assert finance['models']==summarize_models(descendants)
    unknown=next(value for value in finance['models'] if value['model']=='gpt-6-sol')
    assert unknown['cost'] is None and unknown['priced']==0
    assert agents['grandchild']['token_composition']==token_composition(sources[2])
    own=summaries.cache[('home','grandchild')][1]
    assert agents['grandchild']['model_breakdown'] is own['model_breakdown']
    assert agents['grandchild']['token_composition'] is own['token_composition']
    assert agents['grandchild']['cost_composition'] is own['cost_composition']
    assert first['child']['subagent_finance']['cost']==.5
    assert next(value for value in summaries.collect(engine) if value['id']=='parent')['subagent_finance'] is finance
    sources[2]['parent_thread_id']='other'
    second={value['id']:value for value in summaries.collect(engine)}
    assert second['parent']['subagent_finance']['cost']==2
    assert {value['id'] for value in second['parent']['subagent_finance']['agents']}=={'child','empty'}
    assert 'subagent_finance' not in second['child']
    assert second['other']['subagent_finance']['missing']==1
    assert finance['cost']==2.5


def test_real_session_table_shows_parent_without_own_calls_and_click_keeps_breakdown(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.quick_qa import click_row, control
    from cachemonitor.pricing import usd

    now = time.time()
    parent = Session('parent', 'fixture', title='부모 작업').view(now)
    parent.update(source='user', archived=False, collection_complete=True)
    child_session = Session('child', 'fixture', title='하위 작업')
    child_session.add_usage(now-10, 'child-call', dict(input_tokens=100, cached_input_tokens=20,
        cache_write_input_tokens=0, output_tokens=10), 'gpt-6-astra', 'turn', 'high', service_tier='Standard')
    child = child_session.view(now)
    child.update(source='subagent', parent_thread_id='parent', archived=False, collection_complete=True)
    review=review_session(child);review['parent_thread_id']='parent'
    snapshot = dict(ts=now, sessions=[parent, child, review], homes=['fixture'], errors=[],
                    unassigned=[], index={'loading':False})
    app = QApplication.instance() or QApplication([])
    before = app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration', True)
    window = Dashboard(['fixture'], start_worker=False,
        settings=QSettings(str(tmp_path/'dashboard.ini'), QSettings.IniFormat), static_snapshot=snapshot)
    try:
        window.show();window.change_page(2);app.processEvents()
        records = list(window.record_rows)
        group = next(r for r in records if r['sid']=='parent')
        own_child = window.engine.query(window.query())['session_costs'][('fixture','child')]
        assert group['own_cost']==0 and group['cost']==own_child['cost']
        assert group['descendants']==1 and group['calls']==1
        assert {r['sid'] for r in records}=={'parent'}
        assert 'codex-auto-review' not in [window.model.itemData(i) for i in range(window.model.count())]
        assert '산정 / 전체 호출' not in window.parent_table.model().headers
        index = next(i for i,r in enumerate(window.record_rows) if r['sid']=='parent')
        click_row(window, window.table, index);app.processEvents()
        assert window.selected_session==('fixture','parent')
        selected=window.view_result['explorer']
        assert selected['summary']['calls']==0 and not window.record_rows
        assert selected['group']['own_cost']==0 and selected['group']['calls']==1
        assert selected['group']['child_cost']==own_child['cost']
        assert control(window,window.session_scope).isVisible()
        assert usd(selected['summary']['cost']) in window.session_scope.text()
        assert usd(0) in window.history_summary.text() and usd(own_child['cost']) in window.history_summary.text()
        assert window.snapshot['internal_review_calls']==1
        assert window.grab().save(str(tmp_path/'parent-cost.png'))
    finally:
        window.quitting=True;window.tick.stop();window.tray.hide();window.observer_panel.stop();window.close()
        app.setProperty('cachemonitorDisableShellIntegration', before)
