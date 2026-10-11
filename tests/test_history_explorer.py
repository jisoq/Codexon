"""Project-first navigation and bounded, reusable history projections."""
import copy
import pytest
from tests.test_ui import dashboard, snapshot
from tests.test_performance import history, query
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.history_projection import node_key


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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
@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
@pytest.mark.parametrize('width', [1120, 1680])
def test_chart_controls_stay_clear_when_switching_between_requests_and_all_calls(dashboard,tmp_path,width):
    from PySide6.QtCore import QPointF,QRectF
    from cachemonitor.quick_qa import click,control,render_plot,walk
    w=dashboard;w.resize(width,850);w.change_page(2);w.activate_record(0)
    session=w.selected_session
    click(w,render_plot(w,w.history_view_buttons['chart']))
    for kind in ('calls','requests','calls'):
        navigation=w.history_all_calls_button if kind=='calls' else w.history_up_button
        click(w,render_plot(w,navigation))
        plot=render_plot(w,w.history_chart)
        assert w.selected_session==session and w.record_view==kind and w.selected_turn is None
        assert not control(w,w.history_table_stack).isVisible()
        assert not any(item.isVisible() for item in walk(control(w,w.history_table_stack))
                       if item.objectName()=='table-header-label')
        assert plot.isVisible()
        controls=[control(w,node) for node in (w.history_metric,w.history_axis,w.history_zoom,w.history_reset_zoom)]
        rectangles=[QRectF(item.mapToScene(QPointF()),item.size()) for item in controls]
        scene=QRectF(w.quick.rect())
        assert all(item.isVisible() and scene.contains(rect) for item,rect in zip(controls,rectangles))
        assert all(not first.intersects(second) for index,first in enumerate(rectangles)
                   for second in rectangles[index+1:])
        assert max(rect.bottom() for rect in rectangles)<=plot.mapToScene(QPointF()).y()
        assert w.quick.grabFramebuffer().save(str(tmp_path/f'history-{kind}-chart-{width}.png'))
        click(w,control(w,w.history_view_buttons['table']));render_plot(w,w.table)
        assert control(w,w.table).isVisible() and not control(w,w.history_chart_panel).isVisible()
        assert any(item.isVisible() for item in walk(control(w,w.table))
                   if item.objectName()=='table-header-label')
        assert w.selected_session==session and w.record_view==kind
        click(w,control(w,w.history_view_buttons['chart']));render_plot(w,w.history_chart)
        assert not control(w,w.history_table_stack).isVisible()
    assert not w.qml_errors


@pytest.mark.integration
def test_session_chart_display_and_metric_restore_across_sessions(dashboard,tmp_path):
    from PySide6.QtCore import QPointF,QRectF
    from cachemonitor.quick_qa import click,control,render_plot
    from cachemonitor.ui_details import Details
    w=dashboard;w.resize(1120,760);w.change_page(2)
    assert w.history_display=='table' and render_plot(w,w.table).isVisible()
    assert not control(w,w.history_chart_panel).isVisible()
    w.activate_record(0)
    original_session=w.selected_session
    click(w,render_plot(w,w.history_view_buttons['chart']))
    chart=render_plot(w,w.history_chart)
    assert w.history_display=='chart' and not control(w,w.table).isVisible()
    assert w.history_chart.rows
    assert w.history_chart.record_kind=='requests' and w.history_axis.currentData()=='ordinal'
    assert w.history_metric.findData('output_speed')<0
    assert {row['turn'] for row in w.history_chart.rows}=={row['turn'] for row in w.record_rows}
    assert all((row['home'],row['sid'])==original_session for row in w.history_chart.rows)
    w.history_metric.setCurrentIndex(w.history_metric.findData('tokens'));render_plot(w,w.history_chart)
    input_panel=w.history_chart.plot_rect('input');output_panel=w.history_chart.plot_rect('output')
    assert input_panel.left()==output_panel.left() and input_panel.width()==output_panel.width()
    assert input_panel.bottom()<output_panel.top()
    assert w.history_chart.bounds_for('input')[1]>w.history_chart.bounds_for('output')[1]
    # Real control bounds must remain reachable in the compact dashboard.
    scene=QRectF(w.quick.rect())
    for item in (chart,control(w,w.history_metric),control(w,w.history_view_buttons['table']),
                 control(w,w.history_axis),control(w,w.history_zoom),control(w,w.history_reset_zoom),
                 control(w,w.session_scope),
                 control(w,w.history_all_calls_button),control(w,w.history_chart_note),
                 control(w,w.history_inspector),control(w,w.history_detail_button)):
        box=QRectF(item.mapToScene(QPointF()),item.size())
        assert item.isVisible() and box.width()>0 and box.height()>0
        assert scene.contains(box), f'History chart control outside viewport: {box}, {scene}'
    summary=next(node for node in w.record_current.findChildren(Details) if node.body is w.history_summary)
    summary_top=control(w,summary.toggle).mapToScene(QPointF()).y()
    for node in (w.history_inspector,w.history_detail_button):
        item=control(w,node)
        assert item.mapToScene(QPointF(0,item.height())).y()<=summary_top
    w.history_axis.setCurrentIndex(w.history_axis.findData('time'))
    state=w.capture_state()
    click(w,control(w,w.history_view_buttons['table']))
    assert render_plot(w,w.table).isVisible() and not control(w,w.history_chart_panel).isVisible()
    w.history_metric.setCurrentIndex(w.history_metric.findData('cost'))
    w.restore_state(state)
    render_plot(w,w.history_chart)
    assert w.history_display=='chart' and w.history_metric.currentData()=='tokens'
    assert w.history_axis.currentData()=='time'
    click(w,render_plot(w,w.history_all_calls_button));render_plot(w,w.history_chart)
    assert w.record_view=='calls' and w.selected_turn is None
    up=control(w,w.history_up_button)
    assert up.isVisible() and scene.contains(QRectF(up.mapToScene(QPointF()),up.size()))
    click(w,up);render_plot(w,w.history_chart)
    assert w.record_view=='requests' and w.history_metric.findData('output_speed')<0
    # Enter another project and session using the existing navigation path.
    w.activate_record_parent(1);w.activate_record(0)
    render_plot(w,w.history_chart)
    assert w.selected_session!=original_session
    assert w.history_display=='chart' and w.history_metric.currentData()=='tokens'
    assert all((row['home'],row['sid'])==w.selected_session for row in w.history_chart.rows)
    assert w.quick.grabFramebuffer().save(str(tmp_path/'session-chart-compact.png'))
    assert not w.qml_errors


@pytest.mark.parametrize('request_scope',[False,True])
@pytest.mark.integration
def test_session_chart_selection_detail_and_back_preserve_scope(dashboard,request_scope,tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from cachemonitor.quick_qa import click,control,render_plot
    w=dashboard;w.resize(1120,760);w.change_page(2);w.activate_record(0)
    if request_scope:w.activate_record(0)
    else:w.show_history_calls()
    route=(w.history_navigation.project,w.selected_session,w.selected_turn,w.record_view)
    click(w,render_plot(w,w.history_view_buttons['chart']))
    chart=w.history_chart;plot=render_plot(w,chart)
    rows=chart.rows
    assert len(rows)==(2 if request_scope else 16)
    assert chart.record_kind=='calls' and w.history_metric.findData('output_speed')>=0
    assert {row['key'] for row in rows}=={row['key'] for row in w.record_rows}
    plot.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_End)
    inspected=rows[-1]
    assert w.history_hovered_call['key']==inspected['key']
    assert not w.selected_call and w.history_inspector.text()
    click(w,render_plot(w,w.history_detail_button))
    render_plot(w,w.detail_scroll)
    assert w.selected_call==inspected['key']
    assert w.selected_call_scope==(inspected['home'],inspected['sid'])
    assert (w.history_navigation.project,w.selected_session,w.selected_turn,w.record_view)==route
    assert w.exact_record['key']==inspected['key']
    assert w.exact_record['generation_latency_ms']==500
    assert w.detail_scroll.isVisible() and not control(w,w.history_chart_panel).isVisible()
    click(w,render_plot(w,w.close_record_button))
    plot=render_plot(w,chart)
    assert w.selected_call is None and plot.isVisible()
    assert w.history_display=='chart' and chart.rows==rows
    selected=rows[0]
    # Deliver selection directly at rendered geometry, independent of hover.
    plot.activateAt(chart.x_for(selected),chart.y(selected['cost']))
    render_plot(w,w.detail_scroll)
    assert w.selected_call==selected['key']
    assert w.exact_record['key']==selected['key']
    click(w,render_plot(w,w.back_button))
    plot=render_plot(w,chart)
    assert w.selected_call is None and plot.isVisible()
    assert w.history_display=='chart' and chart.rows==rows
    assert (w.history_navigation.project,w.selected_session,w.selected_turn,w.record_view)==route
    assert w.quick.grabFramebuffer().save(str(tmp_path/f'session-chart-back-{request_scope}.png'))
    assert not w.qml_errors


@pytest.mark.integration
def test_request_chart_drilldown_restores_filtered_range_and_selection(dashboard,tmp_path):
    from PySide6.QtCore import QPointF,QRectF,Qt
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import choose
    from cachemonitor.quick_qa import click,control,render_plot,walk
    w=dashboard;w.resize(1120,760);w.change_page(2);w.activate_record(0)
    choose(w.mode,'Standard');w.filter_changed()
    click(w,render_plot(w,w.history_view_buttons['chart']))
    w.history_metric.setCurrentIndex(w.history_metric.findData('tokens'))
    w.history_axis.setCurrentIndex(w.history_axis.findData('time'))
    chart=w.history_chart;plot=render_plot(w,chart)
    assert chart.record_kind=='requests' and len(chart.rows)==8
    assert all(row['calls']==1 and row['total_calls']==2 and row['partial'] for row in chart.rows)
    plot.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Home)
    for _ in range(3):QTest.keyClick(w.quick,Qt.Key_Right)
    selected_request=chart.current_row
    click(w,render_plot(w,w.history_zoom));plot=render_plot(w,chart)
    assert chart.range[1]-chart.range[0]<chart.full_range[1]-chart.full_range[0]
    saved=chart.capture_state();session=w.selected_session
    plot.activateAt(chart.x_for(selected_request),chart.y(selected_request['input'],'input'))
    plot=render_plot(w,chart)
    assert w.record_view=='calls' and w.selected_turn==selected_request['turn']
    assert chart.record_kind=='calls' and len(chart.rows)==1
    assert chart.rows[0]['turn']==selected_request['turn']
    assert w.history_metric.findData('output_speed')>=0
    # A deeper filter/metric change must not replace the saved request view.
    choose(w.mode,'Fast');w.filter_changed()
    w.history_metric.setCurrentIndex(w.history_metric.findData('output_speed'))
    plot=render_plot(w,chart);selected_call=chart.rows[0]
    plot.activateAt(chart.x_for(selected_call),chart.y(selected_call['output_speed']))
    render_plot(w,w.detail_scroll)
    assert w.selected_call==selected_call['key'] and not control(w,w.history_chart_panel).isVisible()
    w.resize(1900,1000);render_plot(w,w.detail_scroll)
    chart_item=control(w,w.history_chart);detail_item=control(w,w.detail_scroll)
    assert chart_item.isVisible() and detail_item.isVisible()
    chart_box=QRectF(chart_item.mapToScene(QPointF()),chart_item.size())
    detail_box=QRectF(detail_item.mapToScene(QPointF()),detail_item.size())
    assert chart_box.right()<=detail_box.left()
    assert w.quick.grabFramebuffer().save(str(tmp_path/'session-chart-wide-detail.png'))
    click(w,render_plot(w,w.close_record_button));render_plot(w,chart)
    assert w.selected_call is None and w.selected_turn==selected_request['turn']
    click(w,render_plot(w,w.history_up_button));plot=render_plot(w,chart)
    assert w.record_view=='requests' and w.selected_session==session and w.selected_turn is None
    assert w.mode.currentData()=='Standard' and w.history_metric.currentData()=='tokens'
    assert w.history_axis.currentData()=='time' and chart.range==pytest.approx(saved['range'])
    assert chart.capture_state()['selected']==saved['selected']
    assert w.history_metric.findData('output_speed')<0
    # The rendered session breadcrumb is the same direct route back up.
    plot.activateAt(chart.x_for(chart.current_row),chart.y(chart.current_row['input'],'input'))
    render_plot(w,chart)
    index=next(index for index,item in enumerate(w.history_path.entries)
               if (item.get('home'),item.get('sid'))==session)
    crumb=next(item for item in walk(control(w,w.history_path)) if item.objectName()==f'history-crumb-{index}')
    click(w,crumb);render_plot(w,chart)
    assert w.record_view=='requests' and chart.capture_state()['selected']==saved['selected']
    assert chart.range==pytest.approx(saved['range']) and w.mode.currentData()=='Standard'
    assert not w.qml_errors
