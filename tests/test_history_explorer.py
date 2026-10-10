"""Project-first navigation and bounded, reusable history projections."""
import copy
import pytest
from tests.test_ui import dashboard, snapshot
from tests.test_performance import history, query
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.history_projection import node_key


def test_project_first_and_selection_does_not_navigate(dashboard):
    w=dashboard;w.change_page(2)
    assert w.record_view=='projects' and w.selected_session is None
    assert len(w.record_rows)==3
    w.table.cellClicked.emit(0,2)
    assert w.record_view=='projects'
    w.activate_record(0)
    assert w.record_view=='sessions' and w.selected_session is None
    w.activate_record(0)
    assert w.record_view=='requests' and w.selected_session
    ordinals=[r['ordinal'] for r in w.record_rows]
    assert ordinals==sorted(ordinals)
    w.activate_record(0)
    assert w.record_view=='calls'
    w.activate_record(0)
    assert w.selected_call and w.detail_scroll.isVisible()
    w.go_back()
    assert w.record_view=='calls' and not w.selected_call
    w.go_home()
    assert w.record_view=='projects' and not w.selected_session


def test_tree_window_and_reuse():
    source=history()
    sessions=[dict(source,id=f's-{i}',home=f'h-{i}') for i in range(800)]
    engine=AnalysisEngine();engine.ingest(sessions)
    q=query(page=2,presentation=True,explorer=dict(view='projects',sort='time_desc'))
    first=engine.page_query(q)['explorer']
    project=first['records']['rows'][0]
    assert project['roots']==800
    before=dict(engine.metrics)
    q['explorer']['expanded']=[project['key']]
    q['table_windows']={'parents':{'start':40,'size':128}}
    expanded=engine.page_query(q)['explorer']
    assert expanded['parents']['total']==801
    assert len(expanded['parents']['rows'])==128
    assert all('members' not in row for row in expanded['parents']['rows'])
    assert engine.metrics['priced_calls']==before['priced_calls']
    assert engine.metrics['part_builds']==before['part_builds']


def test_parent_total_and_child_route():
    source=history();parent=dict(source,id='parent',history=[],title='Parent')
    child=dict(source,id='child',parent_thread_id='parent',title='Child')
    engine=AnalysisEngine();engine.ingest([parent,child])
    q=query(page=2,presentation=True,explorer=dict(view='projects',sort='time_desc'))
    project=engine.page_query(q)['explorer']['records']['rows'][0]
    q['explorer'].update(view='sessions',project=project['project_id'])
    rows=engine.page_query(q)['explorer']['records']['rows']
    assert len(rows)==1 and rows[0]['sid']=='parent'
    assert rows[0]['cost']==project['cost']
    q['explorer'].update(view='children',session=[source['home'],'parent'])
    child_view=engine.page_query(q)['explorer']
    assert child_view['child_count']==1
    assert child_view['records']['rows'][0]['sid']=='child'


def test_filtered_request_metrics_and_zero_call_boundary():
    source=history();source=copy.deepcopy(source)
    source.setdefault('turn_records',{})['waiting']=dict(started_at=105,state='진행')
    engine=AnalysisEngine();engine.ingest([source])
    q=query(page=2,presentation=True,explorer=dict(view='requests',session=[source['home'],source['id']],sort='time_asc'))
    first=engine.page_query(q)['explorer']
    assert any(r['turn']=='waiting' and r['responses']==0 for r in first['records']['rows'])
    numbers={r['turn']:r.get('ordinal') for r in first['records']['rows']}
    q['explorer']['filters']=['cache_zero']
    narrowed=engine.page_query(q)['explorer']
    calls=[r for r in engine.query({k:v for k,v in q.items() if k not in ('explorer','presentation')})['analysis']['responses'] if r.get('cached')==0]
    assert sum(r['responses'] for r in narrowed['records']['rows'])==len(calls)
    assert all(r.get('ordinal')==numbers[r['turn']] for r in narrowed['records']['rows'])
    assert all(r['turn']!='waiting' for r in narrowed['records']['rows'])


def test_missing_and_cyclic_parents_remain_reachable():
    source=history()
    sessions=[dict(source,id='a',parent_thread_id='b'),dict(source,id='b',parent_thread_id='a'),dict(source,id='orphan',parent_thread_id='missing')]
    engine=AnalysisEngine();engine.ingest(sessions)
    q=query(page=2,presentation=True,explorer=dict(view='projects'))
    project=engine.page_query(q)['explorer']['records']['rows'][0]
    q['explorer'].update(view='sessions',project=project['project_id'])
    roots=engine.page_query(q)['explorer']['records']['rows']
    assert any(r['sid']=='orphan' for r in roots)
    assert sum(r['calls'] for r in roots)==project['calls']


def test_empty_search_actions_restore_rows_and_preserve_route(dashboard,tmp_path):
    import time
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import choose
    from cachemonitor.quick_qa import click,control,walk
    w=dashboard;w.change_page(2);w.activate_record(0);w.activate_record(0);w.activate_record(0)
    route=(w.history_navigation.project,w.selected_session,w.selected_turn,w.record_view)
    choose(w.mode,'Fast');w.filter_changed();w.search.setText('없는 작업 12345')
    deadline=time.monotonic()+3
    while w.search_timer.isActive() or w.record_rows:
        assert time.monotonic()<deadline, 'Search did not finish rendering its empty result'
        QTest.qWait(10)
    assert not w.record_rows
    title=next(item for item in walk(control(w,w.table)) if item.objectName()=='table-empty-title')
    assert title.property('text')=='검색 조건에 맞는 기록 없음'
    clear_search,reset_filters=w.table.nodes
    assert w.quick.grabFramebuffer().save(str(tmp_path/'검색결과없음.png'))
    assert control(w,clear_search).isVisible() and control(w,reset_filters).isVisible()
    click(w,control(w,reset_filters))
    assert (w.history_navigation.project,w.selected_session,w.selected_turn,w.record_view)==route
    assert not w.mode.currentData() and w.period.currentData()=='all'
    assert w.search.text()=='없는 작업 12345' and not w.record_rows
    assert not control(w,reset_filters).isVisible()
    click(w,control(w,clear_search))
    assert w.record_rows and not w.search.text()
    assert (w.history_navigation.project,w.selected_session,w.selected_turn,w.record_view)==route
    assert not control(w,clear_search).isVisible()
    w.go_back()
    assert w.search.text()=='없는 작업 12345' and not w.record_rows
    assert not w.qml_errors


@pytest.mark.parametrize('loading',[False,True])
def test_source_empty_history_distinguishes_collection(dashboard,loading,tmp_path):
    from PySide6.QtTest import QTest
    from cachemonitor.quick_qa import control,walk
    w=dashboard;w.change_page(2)
    value=copy.deepcopy(w.snapshot);value['sessions']=[];value['index']['loading']=loading
    w.receive(value);QTest.qWait(40)
    expected='사용 기록 수집 중' if loading else '사용 기록 없음'
    title=next(item for item in walk(control(w,w.table)) if item.objectName()=='table-empty-title')
    assert title.property('text')==expected and w.record_message.text()==expected
    assert not any(control(w,node).isVisible() for node in w.table.nodes)
    assert w.quick.grabFramebuffer().save(str(tmp_path/('수집중.png' if loading else '기록없음.png')))
    w.receive(snapshot());QTest.qWait(40)
    assert w.record_rows and not w.table.state['emptyText']
    assert not w.qml_errors


def test_reset_empty_history_filters_preserves_overlay_context(dashboard):
    from cachemonitor.overlay_navigation import NavigationTarget
    w=dashboard;w.navigate(NavigationTarget('fixture','s0',view='calls',request_id='t0'))
    context=copy.deepcopy(w.temporary_context)
    route=(w.history_navigation.project,w.selected_session,w.selected_turn,w.record_view)
    w.call_filter_controls['observation_problem'].setChecked(True)
    w.reset_history_filters()
    assert w.temporary_context==context
    assert (w.history_navigation.project,w.selected_session,w.selected_turn,w.record_view)==route
    assert not any(node.isChecked() for node in w.call_filter_controls.values())
    assert w.record_rows
