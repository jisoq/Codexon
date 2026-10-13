"""Model-driven comparison and navigation through the real Qt scene."""
import copy
import pytest

pytestmark = pytest.mark.integration

from cachemonitor.quick_qa import control, click, click_row, walk
from test_ui import dashboard, snapshot


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


def test_home_card_and_comparison_controls_accept_rendered_input(dashboard):
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
    click(w,rendered('home-card-1'))
    assert w.current_page==1
    choice=rendered('usage-dimension');choice.forceActiveFocus()
    QTest.keyClick(w.quick,Qt.Key_Down)
    assert w.metric_panel.options['dimension']=='model'
    item=w.metric_panel.data['rows'][0]
    click(w,rendered('usage-row-'+item['key']))
    assert w.metric_panel.options['filters']['model']==item['key']
    w.go_back();assert not w.metric_panel.options['selected']
    assert not w.qml_errors


def test_project_selection_preserves_filters_and_back_restores_exact_call(dashboard):
    w=dashboard;w.nav.setCurrentRow(w.navigation_pages.index(2));click_row(w,w.parent_table,1);click_row(w,w.table,0);click_row(w,w.table,1);click_row(w,w.table,1)
    before=w.capture_state();assert w.selected_call
    click(w,control(w,w.history_projects_button))
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


def test_metric_modes_and_saved_scope_restore_without_changing_home_totals(dashboard):
    w=dashboard;totals=copy.deepcopy(w.home_panel.state['totals']);w.change_page(1);p=w.metric_panel
    p.change('period','7d');p.change('metric','tps');p.change('dimension','model');p.change('sort','name')
    saved=w.capture_state();w.change_page(0)
    assert w.home_panel.state['totals']==totals and w.query()['start']==0
    w.restore_state(saved);assert p.options['metric']=='tps' and p.options['dimension']=='model'
    assert p.options['period']=='7d' and p.options['sort']=='name'
    assert not w.qml_errors


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
