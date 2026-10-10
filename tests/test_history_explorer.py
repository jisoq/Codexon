"""Project-first navigation and bounded, reusable history projections."""
import copy
import pytest
from tests.test_ui import dashboard, snapshot
from tests.test_performance import history, query
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.history_projection import node_key


def test_project_first_and_selection_does_not_navigate(dashboard):
    w=dashboard;w.change_page(2)
    assert w.record_view=='sessions' and w.selected_session is None
    assert len(w.parent_rows)==3 and all('sid' not in row for row in w.parent_rows)
    w.table.selectRow(0)
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
    assert w.record_view=='sessions' and not w.selected_session


def test_tree_window_and_reuse():
    source=history()
    sessions=[dict(source,id=f's-{i}',home=f'h-{i}') for i in range(800)]
    engine=AnalysisEngine();engine.ingest(sessions)
    q=query(page=2,presentation=True,explorer=dict(view='projects',sort='time_desc'))
    first=engine.page_query(q)['explorer']
    project=first['records']['rows'][0]
    assert project['roots']==800
    before=dict(engine.metrics)
    q['explorer'].update(view='sessions',project=project['project_id'])
    q['table_windows']={'records':{'start':40,'size':128}}
    expanded=engine.page_query(q)['explorer']
    assert expanded['parents']['total']==1
    assert expanded['records']['total']==800 and len(expanded['records']['rows'])==128
    assert all('members' not in row for row in expanded['records']['rows'])
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
    q['explorer']['expanded']=[node_key('session',source['home'],'parent')]
    child_view=engine.page_query(q)['explorer']
    assert [(r['sid'],r['depth']) for r in child_view['records']['rows']]==[('parent',0),('child',1)]


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


def test_search_and_sort_keep_one_session_hierarchy():
    source=history()
    records=[dict(source,id='parent',title='Parent'),
             dict(source,id='child',title='Child',parent_thread_id='parent'),
             dict(source,id='nested',title='Needle',parent_thread_id='child'),
             dict(source,id='other',title='Other')]
    engine=AnalysisEngine();engine.ingest(records)
    parent_key=node_key('session',source['home'],'parent')
    child_key=node_key('session',source['home'],'child')
    q=query(page=2,presentation=True,explorer=dict(view='sessions',expanded=[parent_key,child_key],sort='cost_desc'))
    data=engine.page_query(q)['explorer']
    rows=data['records']['rows']
    assert [(r['sid'],r['depth']) for r in rows]==[('parent',0),('child',1),('nested',2),('other',0)]
    assert rows[2]['guides']==[False] and rows[2]['last']
    assert len(data['parents']['rows'])==1 and 'sid' not in data['parents']['rows'][0]
    q['explorer'].update(expanded=[],search='Needle')
    data=engine.page_query(q)['explorer']
    assert [r['sid'] for r in data['records']['rows']]==['parent','child','nested']
    q['explorer']['search_collapsed']=[child_key]
    assert [r['sid'] for r in engine.page_query(q)['explorer']['records']['rows']]==['parent','child']


def test_rendered_disclosure_and_back_restore_hierarchy(dashboard,tmp_path):
    import time
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtTest import QTest
    from cachemonitor.quick_qa import click,control,walk,click_row,table_view
    w=dashboard;value=copy.deepcopy(snapshot());source=value['sessions'][0]
    child=copy.deepcopy(source);child.update(id='child',title='Child',parent_thread_id=source['id'])
    nested=copy.deepcopy(source);nested.update(id='nested',title='Needle',parent_thread_id='child')
    value['sessions']=[source,child,nested,*value['sessions'][1:]]
    w.receive(value);w.resize(1120,850);w.change_page(2)
    def wait_for(check):
        deadline=time.monotonic()+3
        while not check():
            assert time.monotonic()<deadline
            QTest.qWait(10)
    def arrow(row):
        return next(item for item in walk(control(w,w.table)) if item.objectName()==f'session-disclosure-{row}' and item.isVisible())
    wait_for(lambda: any(item.objectName()=='session-disclosure-0' and item.isVisible() for item in walk(control(w,w.table))))
    click(w,arrow(0));assert w.record_view=='sessions' and not w.selected_session
    wait_for(lambda: len(w.record_rows)==2)
    click(w,arrow(1));wait_for(lambda: len(w.record_rows)==3)
    assert [r['depth'] for r in w.record_rows]==[0,1,2]
    assert all('sid' not in r for r in w.parent_rows)
    table=table_view(w,w.table)
    first=next(item for item in walk(control(w,w.table)) if item.objectName()=='frozen-column')
    left=first.mapToScene(QPointF()).x();table.setProperty('contentX',80);QTest.qWait(30)
    assert first.mapToScene(QPointF()).x()==left
    scroll_x=w.table.horizontalScrollBar().value()
    click_row(w,w.table,2);assert w.selected_session[1]=='nested' and w.record_view=='requests'
    w.go_back();wait_for(lambda: w.record_view=='sessions' and len(w.record_rows)==3)
    wait_for(lambda: abs(table_view(w,w.table).property('contentX')-scroll_x)<1)
    w.table.selectRow(0);table_view(w,w.table).forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Left)
    wait_for(lambda: len(w.record_rows)==1)
    QTest.keyClick(w.quick,Qt.Key_Right);wait_for(lambda: len(w.record_rows)==3)
    assert w.quick.grabFramebuffer().save(str(tmp_path/'hierarchy.png'))
    assert not w.qml_errors


def test_empty_search_actions_restore_rows_and_preserve_route(dashboard,tmp_path):
    import time
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import choose
    from cachemonitor.quick_qa import click,control,walk
    w=dashboard;w.change_page(2);w.activate_record(0);w.activate_record(0)
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
