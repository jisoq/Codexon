"""Model-driven comparison and navigation through the real Qt scene."""
import copy
import pytest

pytestmark = pytest.mark.integration

from cachemonitor.quick_qa import control, click, click_row, walk
from test_ui import dashboard, snapshot, wait_for_rendered


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
    p.change('period','7d');p.change('metric','tps');p.change('dimension','all');p.change('sort','name')
    saved=w.capture_state();w.change_page(3)
    assert w.current_page==3
    w.restore_state(saved);assert p.options['metric']=='tps' and p.options['dimension']=='all'
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
    assert p.format_row(0)['groupStart'] and len(p.format_row(0)['metrics'])==7
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
        horizontal=rendered('usage-horizontal')
        horizontal.setProperty('contentX',horizontal.property('contentWidth')-horizontal.width())
        wait_for_rendered(lambda:horizontal.property('contentX')>0)
        assert w.quick.grabFramebuffer().save(str(tmp_path/'paired-comparison-table-end.png'))
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
