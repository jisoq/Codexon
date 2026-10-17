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
        window.change_page(1);window.metric_panel.change("dimension","model")
        assert window.model.findData('gpt-5.5-pro')>=0
        assert window.model.findData('gpt-5.4-mini')>=0
        assert {'gpt-5.5-pro','gpt-5.4-mini','gpt-5.6'} <= {r['key'] for r in window.metric_panel.data['rows']}
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
    target=next(item for item in walk(navigation) if item.property('index')==1
                and item.metaObject().indexOfProperty('modelData')>=0)
    click(w,target)
    assert w.current_page==1
    choice=rendered('usage-dimension');choice.forceActiveFocus()
    QTest.keyClick(w.quick,Qt.Key_Down)
    assert w.metric_panel.options['dimension']=='model'
    item=w.metric_panel.data['rows'][0]
    click(w,rendered('usage-row-'+item['key']))
    assert w.metric_panel.options['filters']['model']==item['key']
    w.go_back();assert not w.metric_panel.options['selected']
    assert not w.qml_errors


@pytest.mark.essential
def test_primary_routes_start_with_monitoring_and_preserve_drilldown(dashboard):
    w=dashboard;w.resize(1000,680)
    assert w.current_page==3
    assert control(w,w.nav).property('count')==3
    for page in (1,5,3):
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
    w.receive(source);p.change('dimension','model');p.change('breakdown:effort','true')
    usage=control(w,p)
    view=next(item for item in walk(usage) if item.objectName()=='usage-rows')
    wait_for_rendered(lambda:view.property('count')==160)
    assert len(p.data['rows'])==160
    def rendered_rows():return [item for item in walk(view) if item.objectName().startswith('usage-row-')]
    wait_for_rendered(lambda:0<len(rendered_rows())<160)
    assert [row['group'] for row in p.data['rows']]==sorted(row['group'] for row in p.data['rows'])
    first=p.data['rows'][0]['key'];last=p.data['rows'][-1]['key']
    calls=w.request_id;projection=w.engine._category_projection
    with monkeypatch.context() as guarded:
        guarded.setattr(w,'render',lambda *args,**kwargs:pytest.fail('Scrolling or resizing must not request analysis'))
        guarded.setattr(w.engine,'query',lambda *args,**kwargs:pytest.fail('Scrolling must not aggregate records'))
        view.positionViewAtEnd()
        wait_for_rendered(lambda:any(item.objectName()=='usage-row-'+last for item in rendered_rows()))
        assert len(rendered_rows())<30
        old_height=view.height();w.resize(1000,900)
        wait_for_rendered(lambda:view.height()>old_height)
        assert w.request_id==calls and w.engine._category_projection is projection
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
    p.change('period','7d');p.change('metric','tps');p.change('dimension','model');p.change('sort','name')
    saved=w.capture_state();w.change_page(3)
    assert w.current_page==3
    w.restore_state(saved);assert p.options['metric']=='tps' and p.options['dimension']=='model'
    assert p.options['period']=='7d' and p.options['sort']=='name'
    assert not w.qml_errors


def test_model_breakdowns_combine_and_drill_down_in_compact_window(dashboard):
    from PySide6.QtCore import QObject,QPointF
    w=dashboard;w.resize(1000,680);w.change_page(1);p=w.metric_panel
    source,_=many_efforts();w.receive(source);p.change('dimension','model')
    control(w,p)
    def rendered(name):
        return next(item for item in walk(w.quick.rootObject()) if item.objectName()==name and item.isVisible())
    wait_for_rendered(lambda:p.data.get('dimension')=='model' and p.row_model.rowCount()>0)
    assert [entry['value'] for entry in p.state['dimensions']]==['project','model','source']
    for field in ('service_tier','effort'):
        click(w,rendered('usage-breakdown-'+field))
        wait_for_rendered(lambda:field in p.data.get('breakdowns',[]))
    assert p.options['breakdowns']==['service_tier','effort']
    assert p.data['count']==18 and p.data['summary']['value']==216
    def chart_settled():
        return rendered('usage-rows').property('count')==p.data['count']
    wait_for_rendered(chart_settled)
    row=p.data['rows'][0];scope=p.capture()
    assert p.format_row(0)['groupStart']
    click(w,rendered('usage-row-'+row['key']))
    wait_for_rendered(lambda:p.data.get('dimension')=='source')
    assert all(p.options['filters'][k]==v for k,v in row['filters'].items())
    assert p.data['summary']['value']==12
    p.records();wait_for_rendered(lambda:w.current_page==2 and bool(w.record_rows))
    assert len(w.record_rows)==12
    assert all(record[field]==value for record in w.record_rows for field,value in row['filters'].items())
    w.go_back();wait_for_rendered(lambda:w.current_page==1)
    w.go_back();wait_for_rendered(lambda:p.data.get('dimension')=='model' and chart_settled())
    assert p.capture()==scope
    chart_top=rendered('usage-chart').mapToScene(QPointF()).y()
    p.change('dimension','project')
    wait_for_rendered(lambda:p.data['dimension']=='project' and rendered('usage-chart').mapToScene(QPointF()).y()==chart_top)
    p.change('dimension','model')
    wait_for_rendered(lambda:p.data['dimension']=='model' and rendered('usage-chart').mapToScene(QPointF()).y()==chart_top)
    assert p.options['breakdowns']==['service_tier','effort']
    saved=w.capture_state();p.change('breakdown:service_tier','false');w.restore_state(saved)
    wait_for_rendered(lambda:p.data.get('breakdowns')==['service_tier','effort'])
    click(w,rendered('usage-breakdown-effort'))
    wait_for_rendered(lambda:p.data.get('breakdowns')==['service_tier'])
    assert p.data['count']==2 and p.data['summary']['value']==216
    assert not w.qml_errors


def test_saved_single_setting_dimension_restores_as_model_breakdown(dashboard):
    dashboard.change_page(1);p=dashboard.metric_panel
    for field in ('effort','service_tier'):
        p.restore(dict(dimension=field,filters={'project':'saved-project'},selected=[field,'high' if field=='effort' else 'Fast']))
        assert p.options['dimension']=='model' and p.options['breakdowns']==[field]
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
    w=dashboard;w.change_page(1);p=w.metric_panel;key=p.data['rows'][0]['key'];p.select(key)
    source=copy.deepcopy(w.snapshot);source['sessions']=[s for s in source['sessions'] if project_identity(s)!=key]
    w.receive(source)
    assert p.options['filters']['project']==key and p.data['summary']['total']==0
    assert not w.qml_errors
