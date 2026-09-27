"""Project-first navigation and bounded, reusable history projections."""
import copy
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


def test_responsive_render(dashboard,tmp_path):
    from PySide6.QtTest import QTest
    w=dashboard;w.change_page(2)
    for width,height in ((1120,760),(1440,940),(2560,1440),(3440,1440),(5120,2082)):
        w.resize(width,height);QTest.qWait(60)
        assert w.records_body.available_width>0
        assert w.quick.grabFramebuffer().save(str(tmp_path/f'history-{width}.png'))
    assert not w.qml_errors


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
