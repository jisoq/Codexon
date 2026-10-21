"""Model-driven comparison and navigation through the real Qt scene."""
import copy
import pytest

pytestmark = pytest.mark.integration

from cachemonitor.quick_qa import control, click, click_row, walk
from test_ui import dashboard, snapshot, wait_for_rendered


def assert_table_geometry(rendered,line=None):
    from PySide6.QtCore import QPointF
    keys=['calls','cost','cost_per_call','latency','tps','input_per_call','output_per_call','reasoning']
    viewport=rendered('usage-viewport');header=rendered('usage-table-header');view=rendered('usage-rows')
    entries=rendered('usage-metric').property('model')
    assert [entry['value'] for entry in entries]==keys
    headers=[item for item in walk(header) if item.isVisible() and item.objectName().startswith('usage-header-')]
    headers.sort(key=lambda item:item.mapToItem(viewport,QPointF()).x())
    assert [item.objectName() for item in headers]==['usage-header-'+key for key in keys]
    top=view.mapToScene(QPointF()).y()
    lines=[line] if line else [item for item in walk(view) if item.isVisible() and item.objectName().startswith('usage-row-')
        and top<=item.mapToScene(QPointF()).y() and item.mapToScene(QPointF()).y()+item.height()<=top+view.height()]
    assert lines
    for header_cell in headers:
        position=header_cell.mapToItem(header,QPointF())
        assert 0<=position.y()<position.y()+header_cell.height()<=header.height()
        assert header_cell.property('contentWidth')<=header_cell.width()+1
        assert header_cell.property('contentHeight')<=header_cell.height()+1
    for row in lines:
        cells=[next(item for item in walk(row) if item.isVisible() and item.objectName()=='usage-cell-'+key) for key in keys]
        label=next(item for item in walk(row) if item.isVisible() and item.objectName()=='usage-table-label')
        chevron=next(item for item in walk(row) if item.isVisible() and item.objectName()=='usage-table-chevron')
        edge=0
        for header_cell,cell in zip(headers,cells):
            position=cell.mapToItem(viewport,QPointF())
            assert edge<=position.x()<position.x()+cell.width()<=viewport.width()
            assert position.x()==pytest.approx(header_cell.mapToItem(viewport,QPointF()).x(),abs=.1)
            assert cell.width()==pytest.approx(header_cell.width(),abs=.1)
            assert cell.property('contentWidth')<=cell.width()+1
            assert cell.property('contentHeight')<=cell.height()+1
            assert cell.property('font').pixelSize()==label.property('font').pixelSize()
            local=cell.mapToItem(row,QPointF())
            assert 0<=local.y()<local.y()+cell.height()<=row.height()
            edge=position.x()+cell.width()
        assert edge<=chevron.mapToItem(viewport,QPointF()).x()
        assert chevron.mapToItem(viewport,QPointF()).x()+chevron.width()<viewport.width()
        first=cells[0].mapToItem(row,QPointF())
        label_position=label.mapToItem(row,QPointF())
        assert label_position.y()+label.height()<=first.y() or label_position.x()+label.width()<=first.x()
    return headers,lines


@pytest.mark.parametrize('language', ['ko', 'en'])
def test_subscription_rates_and_historical_models_render(tmp_path, language):
    from pathlib import Path
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.i18n import set_language
    from cachemonitor.lazy_table import LazyTable
    from cachemonitor.presentation import Text
    from cachemonitor.pricing import SUPPORTED_MODELS, token_cost
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    set_language(language)
    source=snapshot()
    for model in ('gpt-5.4-mini', 'gpt-5.5-pro', 'gpt-5.6'):
        row=copy.deepcopy(source['sessions'][0]['history'][0])
        row.update(key='historical-'+model,model=model,configured_model=model,requested_model=model,
                   cached=0,written=0,service_tier='Standard')
        source['sessions'][0]['history'].append(row)
    window=None
    try:
        window=Dashboard(['fixture'],start_worker=False,live_limits=False,
                         settings=QSettings(str(tmp_path/'value.ini'),QSettings.IniFormat),static_snapshot=source)
        window.show();QTest.qWait(80)
        window.change_page(1);window.metric_panel.change("dimension","all")
        assert window.model.findData('gpt-5.5-pro')>=0
        assert window.model.findData('gpt-5.4-mini')>=0
        assert {'gpt-5.5-pro','gpt-5.4-mini','gpt-5.6'} <= {r['filters']['model'] for r in window.metric_panel.data['rows']}
        priced=next(iter(window.engine.sessions.values()))['prepared']['history']
        for model in ('gpt-5.4-mini','gpt-5.5-pro'):
            record=next(r for r in priced if r['model']==model)
            assert record['cost'] is not None and record['cost']==token_cost(record)['cost']
        window.show_prices();dialog=window.price_dialog;QTest.qWait(80)
        prices=dialog.findChild(LazyTable).model().rows
        assert len(prices)==len(SUPPORTED_MODELS)+1
        assert {r[0] for r in prices}==set(SUPPORTED_MODELS)|{'gpt-daybreak-blue-latest'}
        assert all(r[-1]==('미지원' if r[0]=='gpt-daybreak-blue-latest' else '×2.5') for r in prices)
        labels='\n'.join(n.state['text'] for n in dialog.findChildren(Text))
        assert ('Standard API token rates' if language=='en' else 'Standard API 단가') in labels
        if language=='en':assert not any('\uac00'<=c<='\ud7a3' for c in labels)
        assert not window.qml_errors and not dialog.host.qml_errors
        output=Path(__file__).resolve().parents[1]/'artifacts/verification/subscription-value'
        output.mkdir(parents=True,exist_ok=True)
        assert dialog.host.grab().save(str(output/f'rates-{language}.png'))
        dialog.reject()
        window.nav.setCurrentRow(window.navigation_pages.index(3));QTest.qWait(50)
        assert window.grab().save(str(output/f'quota-{language}.png'))
    finally:
        if window:
            window.quit_app()
        set_language('ko');app.setProperty('cachemonitorDisableShellIntegration',previous)


def many_efforts():
    source=snapshot()
    efforts=['none','minimal','low','medium','high','xhigh','max','ultra','미확인']
    template=source['sessions'][0]
    rows=[]
    for index,effort in enumerate(efforts):
        for mode in ('Standard','Fast'):
            for n in range(12):
                row=copy.deepcopy(template['history'][0])
                row.update(key=f'{index}-{mode}-{n}',effort=effort,service_tier=mode,turn=f'{index}-{mode}',ts=source['ts']-100-n)
                rows.append(row)
    template['history']=rows;source['sessions']=[template]
    return source,efforts


def test_comparison_navigation_and_controls_accept_rendered_input(dashboard):
    import time
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    w=dashboard;w.resize(1120,740)
    def rendered(name):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            QApplication.processEvents()
            found=[n for n in walk(w.quick.rootObject()) if n.objectName()==name and n.isVisible() and n.width()>0]
            if found:return found[0]
            QTest.qWait(5)
        raise AssertionError(name)
    navigation=control(w,w.nav)
    target=next(item for item in walk(navigation) if item.property('index')==w.navigation_pages.index(1)
                and item.metaObject().indexOfProperty('modelData')>=0)
    click(w,target)
    assert w.current_page==1
    choice=rendered('usage-dimension');choice.forceActiveFocus()
    QTest.keyClick(w.quick,Qt.Key_Down)
    assert w.metric_panel.options['dimension']=='source'
    item=w.metric_panel.data['rows'][0]
    click(w,rendered('usage-row-'+item['key']))
    assert w.metric_panel.options['filters']['model']==item['filters']['model']
    w.go_back();assert not w.metric_panel.options['selected']
    assert not w.qml_errors


@pytest.mark.essential
def test_primary_routes_start_with_home_and_preserve_drilldown(dashboard):
    w=dashboard;w.resize(1000,680)
    assert w.current_page==6
    assert control(w,w.nav).property('count')==4
    for page in (1,5,3,6):
        w.nav.setCurrentRow(w.navigation_pages.index(page))
        wait_for_rendered(lambda:w.current_page==page and not w.analysis_pending)
        assert control(w,w.pages.widget(page-1)).isVisible()
    w.change_page(1);p=w.metric_panel
    p.records()
    wait_for_rendered(lambda:w.current_page==2 and not w.analysis_pending)
    assert w.record_rows and not w.qml_errors


def test_project_selection_preserves_filters_and_back_restores_exact_call(dashboard):
    w=dashboard;w.resize(960,720);w.change_page(2)
    wait_for_rendered(lambda: not control(w,w.record_parent).isVisible())
    click(w,control(w,w.history_projects_button))
    wait_for_rendered(lambda: control(w,w.record_parent).isVisible())
    click_row(w,w.parent_table,1);click_row(w,w.table,0);click_row(w,w.table,1);click_row(w,w.table,1)
    before=w.capture_state();assert w.selected_call
    if not w.history_projects_button.isChecked():click(w,control(w,w.history_projects_button))
    click_row(w,w.parent_table,0)
    assert w.record_view=='sessions' and w.selected_session is None and w.selected_call is None and w.selected_turn is None
    assert w.period.currentData()==before['common']['period']
    click(w,control(w,w.back_button))
    assert w.selected_call==before['selected_call'] and w.selected_turn==before['selected_turn']
    assert w.detail_scroll.isVisible() and not w.qml_errors


def test_comparison_detail_home_back_and_refresh_preserve_context(dashboard):
    w=dashboard;w.change_page(1);p=w.metric_panel
    p.select(p.data['rows'][0]['key']);selected=p.capture()
    p.reset();assert not p.options['selected']
    w.go_back();assert p.options['selected']==selected['selected']
    w.receive(copy.deepcopy(w.snapshot));assert p.options['selected']==selected['selected']
    assert not w.qml_errors


def test_virtual_comparison_scroll_reuses_aggregate_without_viewport_queries(dashboard,monkeypatch,tmp_path):
    w=dashboard;w.tick.stop();w.resize(1000,680);w.change_page(1);p=w.metric_panel
    source=snapshot();template=source['sessions'][0]['history'][0]
    source['sessions'][0]['history']=[dict(template,key=f'virtual-{i}',model=f'model-{i//8:02}',
        configured_model=f'model-{i//8:02}',requested_model=f'model-{i//8:02}',
        effort=f'effort-{i%8}',ts=source['ts']-5) for i in range(160)]
    source['sessions']=source['sessions'][:1]
    w.receive(source);p.change('dimension','all')
    usage=control(w,p)
    view=next(item for item in walk(usage) if item.objectName()=='usage-rows')
    wait_for_rendered(lambda:view.property('count')==160)
    assert len(p.data['rows'])==160
    def rendered_rows():return [item for item in walk(view) if item.objectName().startswith('usage-row-')]
    wait_for_rendered(lambda:0<len(rendered_rows())<160)
    assert [row['group'] for row in p.data['rows']]==sorted(row['group'] for row in p.data['rows'])
    first=p.data['rows'][0]['key'];last=p.data['rows'][-1]['key']
    calls=w.request_id;projection=p._all_data
    with monkeypatch.context() as guarded:
        guarded.setattr(w,'render',lambda *args,**kwargs:pytest.fail('Scrolling or resizing must not request analysis'))
        guarded.setattr(w.engine,'query',lambda *args,**kwargs:pytest.fail('Scrolling must not aggregate records'))
        view.positionViewAtEnd()
        wait_for_rendered(lambda:any(item.objectName()=='usage-row-'+last for item in rendered_rows()))
        assert len(rendered_rows())<30
        old_height=view.height();w.resize(1000,900)
        wait_for_rendered(lambda:view.height()>old_height)
        assert w.request_id==calls and p._all_data is projection
        assert len(rendered_rows())<40 and len(p.row_model.cache)<=128 and p.row_model.cache_bytes<=4*1024*1024
        position=view.property('contentY');cached=dict(p.row_model.cache)
        p.apply(copy.deepcopy(p.data))
        assert view.property('contentY')==position and p.row_model.cache==cached
        assert p.row_model.rows is p.data['rows']
        assert w.quick.grabFramebuffer().save(str(tmp_path/'virtual-comparison.png'))
        view.positionViewAtBeginning()
        wait_for_rendered(lambda:any(item.objectName()=='usage-row-'+first for item in rendered_rows()))
    assert not w.qml_errors


def test_metric_modes_and_saved_scope_restore_after_monitoring(dashboard):
    w=dashboard;w.change_page(1);p=w.metric_panel
    p.change('period','7d');p.change('metric','reasoning');p.change('dimension','all');p.change('sort','name')
    saved=w.capture_state();w.change_page(3)
    assert w.current_page==3
    w.restore_state(saved);assert p.options['metric']=='reasoning' and p.options['dimension']=='all'
    assert p.options['period']=='7d' and p.options['sort']=='name'
    assert not w.qml_errors


def test_unmeasurable_pairs_hide_without_losing_zero_values_or_cached_metrics(dashboard,monkeypatch,tmp_path):
    w=dashboard;w.tick.stop();w.resize(1000,680)
    source=snapshot()
    for session,effort in zip(source['sessions'],('high','low','medium')):
        for row in session['history']:
            row['effort']=effort
            row['reasoning']=None if effort=='high' else 0 if effort=='low' else 10
            if effort=='high':
                row.update(completion_latency_ms=None,response_status='unknown')
        if effort=='low':session['history'][0]['reasoning']=None
    w.receive(source);w.change_page(1);p=w.metric_panel
    usage=control(w,p)
    view=next(item for item in walk(usage) if item.objectName()=='usage-rows')
    wait_for_rendered(lambda:view.property('count')==3)
    unknown=next(row for row in p.data['rows'] if row['filters']['effort']=='high')
    unknown_project=unknown['filters']['project']
    projection=p._all_data
    with monkeypatch.context() as guarded:
        guarded.setattr(w,'render',lambda *args,**kwargs:pytest.fail('Metric changes must reuse delivered aggregates'))
        p.change('metric','tps')
        assert {row['filters']['effort'] for row in p.data['rows']}=={'low','medium'}
        assert p.data['summary']==projection['metrics']['tps']
        p.change('metric','reasoning')
        zero=next(row for row in p.data['rows'] if row['filters']['effort']=='low')
        assert zero['value']==0 and zero['known']==15 and zero['partial']
        wait_for_rendered(lambda:view.property('count')==2)
        assert all(row['value'] is not None for row in p.data['rows'])
        assert p._all_data is projection
        p.change('view','table')
        assert len(p.data['rows'])==2
        p.change('metric','calls')
        wait_for_rendered(lambda:view.property('count')==3)
    p.change('filter:project',unknown_project)
    wait_for_rendered(lambda:view.property('count')==1)
    projection=p._all_data
    with monkeypatch.context() as guarded:
        guarded.setattr(w,'render',lambda *args,**kwargs:pytest.fail('Empty metric changes must reuse delivered aggregates'))
        p.change('metric','tps')
        wait_for_rendered(lambda:view.property('count')==0)
        assert p.data['count']==0 and p.data['summary']['total']==16
        assert p._all_data is projection and len(projection['rows'])==1
        p.change('metric','calls')
        wait_for_rendered(lambda:view.property('count')==1)
        p.change('view','chart')
    p.reset();p.change('metric','tps')
    wait_for_rendered(lambda:view.property('count')==2)
    def rendered():
        return [item for item in walk(view) if item.objectName().startswith('usage-row-') and item.isVisible()]
    wait_for_rendered(lambda:len(rendered())==2 and all(item.width()>0 and item.height()>0 for item in rendered()))
    assert w.quick.grabFramebuffer().save(str(tmp_path/'measurable-comparison.png'))
    assert not w.qml_errors


@pytest.mark.parametrize('start,end', [
    ('2026-10-', '2026-10-17'),
    ('2026-02-30', '2026-10-17'),
    ('2026-10-18', '2026-10-17'),
])
def test_custom_date_drafts_preserve_results_and_apply_exact_cohort(dashboard,monkeypatch,start,end):
    import json
    from datetime import datetime
    from PySide6.QtCore import Qt,QPointF
    from PySide6.QtTest import QTest
    w=dashboard;w.tick.stop();w.resize(1000,680)
    source=snapshot(datetime(2026,10,18,12).timestamp());source['sessions']=source['sessions'][:1]
    source['sessions'][0]['history']=source['sessions'][0]['history'][:2]
    for day,row in zip((16,17),source['sessions'][0]['history']):row['ts']=datetime(2026,10,day,12).timestamp()
    w.receive(source);w.change_page(1);p=w.metric_panel
    wait_for_rendered(lambda:p.data.get('summary',{}).get('value')==2)
    usage=control(w,p)
    def rendered(name):
        return next(item for item in walk(usage) if item.objectName()==name and item.isVisible())
    def type_date(name,value):
        item=rendered(name);click(w,item);QTest.keyClick(w.quick,Qt.Key_A,Qt.ControlModifier)
        QTest.keyClicks(w.quick,value)
        wait_for_rendered(lambda:item.property('text')==value)
    before=copy.deepcopy(p.data);scope=p.state['scope'];accepted=p.query();saved=p.capture()
    requests=[];page_query=w.engine.page_query
    def record_query(query):
        requests.append(copy.deepcopy(query));return page_query(query)
    monkeypatch.setattr(w.engine,'page_query',record_query)
    p.change('period','custom')
    wait_for_rendered(lambda:rendered('usage-date-start').width()>0)
    type_date('usage-date-start',start);type_date('usage-date-end',end)
    assert p.state['dateError'] and rendered('usage-date-notice').property('text')
    assert not rendered('usage-date-apply').isEnabled()
    p.change('applyDates','')
    assert not requests and p.data==before and p.state['scope']==scope and p.query()==accepted
    assert p.capture()==saved
    w.render(automatic=True)
    assert requests[-1]['period']==accepted['period'] and requests[-1]['start']==accepted['start']
    assert p.data==before and p.state['scope']==scope
    w.save_preferences()
    assert json.loads(w.settings.value('dashboard/state'))['metric_workspace']==saved
    requests.clear()
    type_date('usage-date-start','2026-10-16');type_date('usage-date-end','2026-10-16')
    assert p.state['dateCanApply'] and not p.state['dateError']
    assert not requests and p.data==before and p.state['scope']==scope
    button=rendered('usage-date-apply');position=button.mapToScene(QPointF())
    assert 0<=position.x()<position.x()+button.width()<=w.quick.width()
    click(w,button)
    wait_for_rendered(lambda:p.data['summary']['value']==1 and not p.state['datePending'])
    assert len(requests)==1 and requests[0]['start']==datetime(2026,10,16).timestamp()
    assert requests[0]['end']==datetime(2026,10,17).timestamp()
    assert p.options['period']=='custom' and p.state['scope']!=scope
    assert json.loads(w.settings.value('dashboard/state'))['metric_workspace']==p.capture()
    applied=p.capture();applied_query=p.query();applied_scope=p.state['scope']
    type_date('usage-date-start','2026-10-')
    assert p.state['dateError'] and p.query()==applied_query and p.state['scope']==applied_scope
    w.render(automatic=True)
    assert requests[-1]['start']==applied_query['start'] and requests[-1]['end']==applied_query['end']
    assert p.data['summary']['value']==1 and p.state['scope']==applied_scope
    p.records()
    wait_for_rendered(lambda:w.current_page==2 and len(w.record_rows)==1)
    assert w.record_rows[0]['key']==source['sessions'][0]['history'][0]['key']
    w.go_back();wait_for_rendered(lambda:w.current_page==1 and p.data['summary']['value']==1)
    assert p.capture()==applied and p.date_draft=={key:applied[key] for key in ('period','start','end')}
    w.go_back();wait_for_rendered(lambda:p.data['summary']['value']==2)
    assert p.capture()==saved and p.state['scope']==scope
    assert not w.qml_errors


def test_custom_date_restore_rejects_invalid_saved_ranges(dashboard):
    from datetime import datetime
    w=dashboard;w.tick.stop();w.change_page(1);p=w.metric_panel
    p.change('period','7d');accepted=p.query()
    for values in (dict(start='',end='2026-10-17'),dict(start='2026-10-18',end='2026-10-17')):
        p.restore(dict(period='custom',**values));w.render()
        assert p.state['dateError'] and p.capture()['period']=='7d' and p.query()==accepted
        assert p.data['summary']['value']>0
    p.change('start','2099-01-01');p.change('end','2099-01-02');p.change('applyDates','')
    assert p.query()['start']==datetime(2099,1,1).timestamp() and p.query()['end']==p.query()['start']
    assert p.data['summary']['value']==0 and not p.state['dateError']
    assert not w.qml_errors


def test_grouped_model_effort_metrics_and_drilldown_in_compact_window(dashboard,monkeypatch,tmp_path):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    w=dashboard;w.resize(1000,680);w.change_page(1);p=w.metric_panel
    source,_=many_efforts();w.receive(source);p.change('dimension','service_tier')
    control(w,p)
    def rendered(name):
        return next(item for item in walk(w.quick.rootObject()) if item.objectName()==name and item.isVisible())
    wait_for_rendered(lambda:p.data.get('dimension')=='service_tier' and p.row_model.rowCount()>0)
    assert p.data['count']==16 and p.data['summary']['value']==216
    assert all(set(row['filters'])=={'service_tier','model','effort'} for row in p.data['rows'])
    row=p.data['rows'][0];scope=p.capture()
    assert p.format_row(0)['groupStart'] and len(p.format_row(0)['metrics'])==8
    with monkeypatch.context() as guard:
        guard.setattr(w,'render',lambda *a,**kw:pytest.fail('Metric selection must reuse delivered aggregates'))
        toggle=rendered('usage-show-unknown')
        position=toggle.mapToScene(QPointF())
        assert position.x()>=0 and position.x()+toggle.width()<=w.quick.width()
        click(w,toggle)
        wait_for_rendered(lambda:p.data['count']==18)
        assert p.data['summary']['value']==216 and p.capture()['showUnknown']
        saved=p.capture()
        click(w,toggle)
        wait_for_rendered(lambda:p.data['count']==16)
        p.restore(saved);p.present()
        assert p.data['count']==18
        p.change('showUnknown','false')
        p.change('metric','cost_per_call');p.change('metric','calls')
        choice=rendered('usage-view');choice.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Down)
        wait_for_rendered(lambda:p.options['view']=='table')
        assert w.quick.grabFramebuffer().save(str(tmp_path/'paired-comparison-table.png'))
        assert_table_geometry(rendered)
        view=rendered('usage-rows')
        view.setProperty('contentY',view.property('contentHeight')-view.height())
        wait_for_rendered(lambda:view.property('contentY')>0)
        assert_table_geometry(rendered)
        view.setProperty('contentY',0)
        choice.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Up)
        wait_for_rendered(lambda:p.options['view']=='chart')
    item=rendered('usage-row-'+row['key'])
    position=item.mapToScene(QPointF())
    assert position.x()>=0 and position.x()+item.width()<=w.quick.width()
    assert w.quick.grabFramebuffer().save(str(tmp_path/'paired-comparison-compact.png'))
    click(w,item)
    wait_for_rendered(lambda:p.data.get('dimension')=='all')
    assert all(p.options['filters'][k]==v for k,v in row['filters'].items())
    assert p.data['summary']['value']==12
    p.records();wait_for_rendered(lambda:w.current_page==2 and bool(w.record_rows))
    assert len(w.record_rows)==12
    assert all(record[field]==value for record in w.record_rows for field,value in row['filters'].items())
    w.go_back();wait_for_rendered(lambda:w.current_page==1)
    w.go_back();wait_for_rendered(lambda:p.data.get('dimension')=='service_tier')
    assert p.capture()==scope
    assert not w.qml_errors


def test_legacy_dimensions_restore_with_fixed_model_effort_pairs(dashboard):
    dashboard.change_page(1);p=dashboard.metric_panel
    for field in ('model','effort','service_tier'):
        p.restore(dict(dimension=field,filters={'project':'saved-project'},selected=[field,'high']))
        assert p.options['dimension']==('service_tier' if field=='service_tier' else 'all')
        assert p.options['filters']=={'project':'saved-project'} and p.options['selected'][0]==field
    assert {choice['key'] for choice in p.state['choices']} >= {'effort','service_tier'}


def test_metric_drilldown_ignores_history_filters_and_restores_latest_scope(dashboard):
    from cachemonitor.dashboard import identity
    w=dashboard;w.change_page(2);w.call_filter_controls['cache_zero'].setChecked(True)
    w.change_page(1);p=w.metric_panel;p.select(p.data['rows'][0]['key']);p.change('period','7d')
    before=p.capture();expected={identity(r) for r in w.engine.query(w.query())['analysis']['responses']}
    p.records();assert w.current_page==2 and not any(n.isChecked() for n in w.call_filter_controls.values())
    assert {identity(r) for r in w.record_rows}==expected
    w.go_back();assert w.current_page==1 and p.capture()==before
    assert not w.qml_errors


def test_saved_project_scope_remains_explicit_after_project_disappears(dashboard):
    from cachemonitor.analytics import project_identity
    w=dashboard;w.change_page(1);p=w.metric_panel;row=p.data['rows'][0];key=row['filters']['project'];p.select(row['key'])
    source=copy.deepcopy(w.snapshot);source['sessions']=[s for s in source['sessions'] if project_identity(s)!=key]
    w.receive(source)
    assert p.options['filters']['project']==key and p.data['summary']['total']==0
    assert not w.qml_errors



@pytest.fixture
def boxplot_language(request):
    from cachemonitor.i18n import language,set_language
    prior=language();set_language(request.param)
    try:yield request.param
    finally:set_language(prior)


@pytest.mark.parametrize('boxplot_language',['ko','en'],indirect=True)
def test_boxplots_render_mean_coverage_totals_table_and_keyboard_drilldown(boxplot_language,dashboard,monkeypatch):
    from pathlib import Path
    from PySide6.QtCore import QPointF,Qt
    from PySide6.QtTest import QTest
    from cachemonitor.theme import shared_theme
    w=dashboard;w.tick.stop();w.resize(1120,740)
    source=snapshot();source['sessions']=source['sessions'][:1]
    template=source['sessions'][0]['history'][0];rows=[]
    for effort,values in [('high',list(range(1,10))+[100]),('low',[0]),('medium',[4,4,4])]:
        for index,value in enumerate(values):
            row=dict(template,key=f'box-{effort}-{index}',effort=effort,input=value*1000,cached=0,written=0,
                     output=value*100,reasoning=value*10,completion_latency_ms=max(1,value)*1000,
                     ts=source['ts']-100+len(rows))
            rows.append(row)
    source['sessions'][0]['history']=rows
    w.receive(source);w.change_page(1);p=w.metric_panel
    p.change('dimension','all');usage=control(w,p)
    wait_for_rendered(lambda:p.data.get('count')==3)
    evidence=Path(__file__).resolve().parents[1]/'artifacts/verification/comparison-boxplot'
    evidence.mkdir(parents=True,exist_ok=True)
    def rendered(name,within=None):
        matches=[item for item in walk(within or usage) if item.objectName()==name and item.isVisible()]
        assert matches,name
        return matches[0]
    def metric(value):
        choice=rendered('usage-metric');choice.forceActiveFocus()
        target=next(i for i,item in enumerate(p.state['metrics']) if item['value']==value)
        while choice.property('currentIndex')!=target:
            prior=choice.property('currentIndex');QTest.keyClick(w.quick,Qt.Key_Down if prior<target else Qt.Key_Up)
            wait_for_rendered(lambda:choice.property('currentIndex')!=prior)
        wait_for_rendered(lambda:p.data['metric']==value)
    def marks(effort):
        row=next(row for row in p.data['rows'] if row['filters']['effort']==effort)
        cell=rendered('usage-plot-'+row['key'])
        return row,cell,rendered('usage-box',cell),rendered('usage-mean',cell),rendered('usage-median',cell)
    theme=shared_theme();appearance=theme.appearance
    mode='dark' if boxplot_language=='en' else 'light';theme.configure(mode)
    suffix=mode+'-'+boxplot_language
    try:
        projection=p._all_data
        with monkeypatch.context() as guard:
            guard.setattr(w,'render',lambda *a,**kw:pytest.fail('Chart controls must reuse delivered aggregates'))
            guard.setattr(w.engine,'page_query',lambda *a,**kw:pytest.fail('Chart controls must not read records'))
            metric('latency')
            wait_for_rendered(lambda:bool([item for item in walk(usage) if item.objectName()=='usage-mean' and item.isVisible()]))
            row,cell,box,mean,median=marks('high')
            assert row['value']==14.5 and row['plot']['median']==5.5
            assert mean.x()>median.x() and box.width()>0
            for mark in (box,mean,median,rendered('usage-whisker',cell)):
                assert 0<=mark.x()<mark.x()+mark.width()<=cell.width()
                assert 0<=mark.y()<mark.y()+mark.height()<=cell.height()
            formatted=p.format_row(p.data['rows'].index(row))
            assert formatted['coverage']=='10 / 10' and formatted['plot']['mean']==row['value']/p.data['markMaximum']
            assert all(label in formatted['tip'] for label in ('P10','Q1','Q3','P90'))
            assert w.quick.grabFramebuffer().save(str(evidence/(f'latency-{suffix}.png')))
            metric('input_per_call')
            row,cell,box,mean,median=marks('low')
            assert row['value']==0 and row['known']==1 and row['plot']['q1']==row['plot']['q3']==0
            assert box.width()>0 and mean.x()>=0 and mean.x()+mean.width()<=cell.width()
            assert median.height()>mean.height()
            metric('cost_per_call')
            assert 0<p.data['markMaximum']<1
            assert p.format_row(0)['plot']['mean']>.1
            search=rendered('usage-search');click(w,search);QTest.keyClicks(w.quick,'low')
            wait_for_rendered(lambda:p.data['count']==1)
            assert p.data['markMaximum']==1 and p.data['rows'][0]['value']==0
            QTest.keyClick(w.quick,Qt.Key_A,Qt.ControlModifier);QTest.keyClick(w.quick,Qt.Key_Backspace)
            wait_for_rendered(lambda:p.data['count']==3)
            assert 0<p.data['markMaximum']<1
            for value in ('calls','cost'):
                metric(value)
                assert not any(item.isVisible() for item in walk(usage) if item.objectName()=='usage-box')
                bars=[item for item in walk(usage) if item.objectName()=='usage-total-bar' and item.isVisible()]
                assert len(bars)==3
                assert any(item.width()>0 for item in bars)
            metric('latency')
            choice=rendered('usage-view');choice.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Down)
            wait_for_rendered(lambda:p.options['view']=='table')
            row=next(row for row in p.data['rows'] if row['filters']['effort']=='high')
            line=rendered('usage-row-'+row['key'])
            assert any(item.isVisible() and item.property('text')=='14.5s' for item in walk(line))
            assert w.quick.grabFramebuffer().save(str(evidence/(f'table-{suffix}.png')))
            choice.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Up)
            wait_for_rendered(lambda:p.options['view']=='chart')
            prior_width=rendered('usage-rows').width();w.resize(1000,680)
            wait_for_rendered(lambda:rendered('usage-rows').width()<prior_width)
            for effort in ('high','low','medium'):
                row,cell,box,mean,median=marks(effort)
                position=cell.mapToScene(QPointF())
                assert 0<=position.x()<position.x()+cell.width()<=w.quick.width()
                assert 0<=mean.x()<mean.x()+mean.width()<=cell.width()
            assert w.quick.grabFramebuffer().save(str(evidence/(f'latency-compact-{suffix}.png')))
            for value,expected in [('reasoning',145),('tps',100)]:
                metric(value)
                row,cell,box,mean,median=marks('high')
                assert row['value']==expected and row['known']==10
                assert p.state['meanMetric'] and box.width()>0
                assert not any(item.isVisible() for item in walk(usage) if item.objectName()=='usage-total-bar')
                position=cell.mapToScene(QPointF())
                assert 0<=position.x()<position.x()+cell.width()<=w.quick.width()
                assert 0<=mean.x()<mean.x()+mean.width()<=cell.width()
                if value=='reasoning':
                    assert {key:row['plot'][key] for key in ('p10','q1','median','q3','p90')}==pytest.approx(dict(p10=19,q1=32.5,median=55,q3=77.5,p90=181))
                    assert p.data['markMaximum']==pytest.approx(190.05)
                    assert {'95.0','190.0'}<={item.property('text') for item in walk(usage) if item.isVisible()}
                    assert mean.x()>median.x()
                    if boxplot_language=='ko':
                        assert w.quick.grabFramebuffer().save(str(evidence/'reasoning-compact.png'))
            metric('latency')
            if boxplot_language=='en':
                assert 'mean' in rendered('usage-box-legend').property('text')
                row,cell,box,mean,median=marks('high')
                assert 'arithmetic mean' in p.format_row(p.data['rows'].index(row))['tip']
            assert p._all_data is projection
        row=next(row for row in p.data['rows'] if row['filters']['effort']=='high')
        line=rendered('usage-row-'+row['key']);line.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Space)
        wait_for_rendered(lambda:p.options['filters'].get('effort')=='high')
        assert p.data['summary']['known']==10
        click(w,rendered('usage-records'))
        wait_for_rendered(lambda:w.current_page==2 and len(w.record_rows)==10)
        assert all(record['effort']=='high' for record in w.record_rows)
        assert not w.qml_errors
    finally:
        theme.configure(appearance=appearance)


def test_output_mean_table_rounds_unscaled_values_without_changing_chart_or_order(dashboard,monkeypatch):
    from pathlib import Path
    from PySide6.QtCore import QPointF,Qt
    from PySide6.QtTest import QTest
    w=dashboard;w.tick.stop();w.resize(1000,680)
    source=snapshot();source['sessions']=source['sessions'][:1]
    template=source['sessions'][0]['history'][0];rows=[]
    cases=[('a-plain',[985]*33+[986]*467,985.934,'986'),
           ('b-lower',[985]*2+[986]*3,985.6,'986'),
           ('c-boundary',[999]*2+[1000]*3,999.6,'1,000'),
           ('d-k',[1000],1000,'1.0k'),('d-fraction',[1300],1300,'1.3k'),
           ('e-k-fraction',[2500],2500,'2.5k'),
           ('f-m',[1250000],1250000,'1.25M'),('g-b',[1250000000],1250000000,'1.25B'),
           ('h-zero',[0],0,'0'),('i-missing',[None],None,'—')]
    for effort,outputs,_,_ in cases:
        for index,output in enumerate(outputs):
            rows.append(dict(template,key=f'table-{effort}-{index}',effort=effort,output=output,
                             reasoning=0,cached=0,written=0,ts=source['ts']-10))
    source['sessions'][0]['history']=rows
    w.receive(source);w.change_page(1);p=w.metric_panel;p.change('dimension','all')
    usage=control(w,p)
    def rendered(name):
        return next(item for item in walk(usage) if item.objectName()==name and item.isVisible())
    wait_for_rendered(lambda:p.data.get('count')==len(cases))
    projection=p._all_data
    with monkeypatch.context() as guard:
        guard.setattr(w,'render',lambda *a,**kw:pytest.fail('Table controls must reuse delivered aggregates'))
        guard.setattr(w.engine,'page_query',lambda *a,**kw:pytest.fail('Table controls must not read records'))
        p.change('metric','output_per_call');p.change('sort','asc')
        efforts=[row['filters']['effort'] for row in p.data['rows']]
        assert efforts.index('b-lower')<efforts.index('a-plain')
        index=efforts.index('a-plain')
        assert p.data['rows'][index]['value']==985.934
        assert p.format_row(index)['value']=='985.934'
        assert '985.934' in p.format_row(index)['tip']
        p.change('metric','calls')
        choice=rendered('usage-view');choice.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Down)
        wait_for_rendered(lambda:p.options['view']=='table')
        assert_table_geometry(rendered)
        for effort,_,value,expected in cases:
            p.change('search',effort)
            wait_for_rendered(lambda:p.data['count']==1 and p.data['rows'][0]['filters']['effort']==effort)
            assert p.data['rows'][0]['metrics']['output_per_call']['value']==value
            formatted=p.format_row(0)
            assert len(formatted['metrics'])==8
            output_cell=next(cell for cell in formatted['metrics'] if cell['key']=='output_per_call')
            assert output_cell['value']==expected
            displayed=expected+(' *' if output_cell['partial'] else '')
            line=rendered('usage-row-'+p.data['rows'][0]['key'])
            wait_for_rendered(lambda:any(item.isVisible() and item.property('text')==displayed for item in walk(line)))
            cell=next(item for item in walk(line) if item.isVisible() and item.objectName()=='usage-cell-output_per_call')
            assert_table_geometry(rendered,line)
            position=cell.mapToScene(QPointF())
            assert 0<=position.x()<position.x()+cell.width()<=w.quick.width()
        p.change('search','a-plain')
        wait_for_rendered(lambda:p.data['count']==1 and p.data['rows'][0]['filters']['effort']=='a-plain')
        evidence=Path(__file__).resolve().parents[1]/'artifacts/verification/comparison-boxplot'
        evidence.mkdir(parents=True,exist_ok=True)
        assert w.quick.grabFramebuffer().save(str(evidence/'output-table-compact.png'))
        assert p._all_data is projection
    assert not w.qml_errors


@pytest.mark.parametrize('boxplot_language',['ko','en'],indirect=True)
def test_main_metric_table_fits_resizes_and_preserves_reasoning_coverage(boxplot_language,dashboard,monkeypatch):
    import os
    from pathlib import Path
    from PySide6.QtCore import QPoint,QPointF,Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QSignalSpy,QTest
    from PySide6.QtWidgets import QApplication
    w=dashboard;w.tick.stop();w.resize(1000,680)
    source=snapshot();source['sessions']=source['sessions'][:1]
    template=source['sessions'][0]['history'][0]
    cases=[('complete',[32,34],33,'33.0','2 / 2'),('zero',[0],0,'0.0','1 / 1'),
           ('missing',[None],None,'— *','0 / 1'),('partial',[12,None],12,'12.0 *','1 / 2')]
    source['sessions'][0]['history']=[dict(template,key=f'reasoning-{effort}-{index}',effort=effort,
        input=10**15 if effort=='complete' else 1000,output=100,reasoning=value,cached=0,written=0,
        ts=source['ts']-10) for effort,values,_,_,_ in cases for index,value in enumerate(values)]
    w.receive(source);w.change_page(1);p=w.metric_panel;p.change('dimension','all');usage=control(w,p)
    def rendered(name):
        return next(item for item in walk(usage) if item.objectName()==name and item.isVisible())
    wait_for_rendered(lambda:p.data.get('count')==4)
    diagnostics=os.environ.get('CODEXON_CHECK_DIAGNOSTICS')
    evidence=(Path(diagnostics).parent if diagnostics else Path(__file__).resolve().parents[1]/'artifacts/verification/metric-table-fit')/'screenshots'
    evidence.mkdir(parents=True,exist_ok=True)
    projection=p._all_data
    with monkeypatch.context() as guard:
        guard.setattr(w,'render',lambda *a,**kw:pytest.fail('Table controls must reuse delivered aggregates'))
        guard.setattr(w.engine,'page_query',lambda *a,**kw:pytest.fail('Table controls must not read records'))
        p.change('view','table')
        for effort,_,value,expected,coverage in cases:
            row=next(row for row in p.data['rows'] if row['filters']['effort']==effort)
            assert row['metrics']['reasoning']['value']==value
            formatted=p.format_row(p.data['rows'].index(row))
            cell=next(cell for cell in formatted['metrics'] if cell['key']=='reasoning')
            assert cell['tip']==coverage and cell['partial']==(effort in ('missing','partial'))
            line=rendered('usage-row-'+row['key'])
            wait_for_rendered(lambda:any(item.isVisible() and item.objectName()=='usage-cell-reasoning'
                and item.property('text')==expected for item in walk(line)))
        headers,lines=assert_table_geometry(rendered)
        long_row=rendered('usage-row-'+next(row['key'] for row in p.data['rows'] if row['filters']['effort']=='complete'))
        currency=next(item for item in walk(long_row) if item.isVisible() and item.objectName()=='usage-cell-cost')
        assert currency.property('contentHeight')>currency.property('font').pixelSize()*1.5
        assert w.quick.grabFramebuffer().save(str(evidence/f'table-compact-{boxplot_language}.png'))
        viewport=rendered('usage-viewport');before=[item.mapToScene(QPointF()).x() for item in headers]
        point=headers[0].mapToScene(QPointF(headers[0].width()/2,headers[0].height()/2))
        QTest.mousePress(w.quick,Qt.LeftButton,pos=point.toPoint())
        QTest.mouseMove(w.quick,(point+QPointF(-120,0)).toPoint())
        QTest.mouseRelease(w.quick,Qt.LeftButton,pos=(point+QPointF(-120,0)).toPoint())
        QApplication.sendEvent(w.quick,QWheelEvent(point,QPointF(w.quick.mapToGlobal(point.toPoint())),
            QPoint(),QPoint(120,0),Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False))
        assert [item.mapToScene(QPointF()).x() for item in headers]==before
        assert rendered('usage-rows').property('contentX')==0
        for size in ((1600,900),(1000,680)):
            frames=QSignalSpy(w.quick.quickWindow().afterRendering)
            w.resize(*size);w.quick.update()
            wait_for_rendered(lambda:frames.count()>0 and w.quick.size()==w.size())
            headers,lines=assert_table_geometry(rendered)
            header=rendered('usage-table-header');heading=rendered('usage-table-label-header')
            stacked=heading.mapToItem(header,QPointF()).y()+heading.height()<=headers[0].mapToItem(header,QPointF()).y()
            for line in lines:
                label=next(item for item in walk(line) if item.isVisible() and item.objectName()=='usage-table-label')
                cell=next(item for item in walk(line) if item.isVisible() and item.objectName()=='usage-cell-calls')
                label_position=label.mapToItem(line,QPointF());cell_position=cell.mapToItem(line,QPointF())
                assert (label_position.y()+label.height()<=cell_position.y())==stacked
                if not stacked:
                    assert label_position.x()+label.width()<=cell_position.x()
                    assert label_position.y()<cell_position.y()+cell.height()
                    assert cell_position.y()<label_position.y()+label.height()
            geometry=(size,w.size(),viewport.width())
            if size[0]==1000:
                assert stacked,geometry
            elif w.quick.width()>=size[0]:
                assert not stacked,geometry
            layout='compact' if stacked else 'wide'
            assert w.quick.grabFramebuffer().save(str(evidence/f'table-{layout}-{boxplot_language}.png'))
        p.change('metric','reasoning');p.change('sort','asc')
        assert [row['filters']['effort'] for row in p.data['rows']]==['zero','partial','complete']
        p.change('metric','calls')
        wait_for_rendered(lambda:p.data['count']==4)
        assert_table_geometry(rendered)
        assert p._all_data is projection
    row=next(row for row in p.data['rows'] if row['filters']['effort']=='partial')
    line=rendered('usage-row-'+row['key']);line.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_Space)
    wait_for_rendered(lambda:p.options['filters'].get('effort')=='partial')
    p.records();wait_for_rendered(lambda:w.current_page==2 and len(w.record_rows)==2)
    assert {record['reasoning'] for record in w.record_rows}=={12,None}
    w.go_back();wait_for_rendered(lambda:w.current_page==1)
    w.go_back();wait_for_rendered(lambda:p.data.get('count')==4)
    assert not w.qml_errors
