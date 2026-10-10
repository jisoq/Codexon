"""Model-driven comparison and navigation through the real Qt scene."""
import copy
import pytest

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
        window.refresh_target_editors()
        assert window.model.findData('gpt-5.5-pro')>=0
        assert window.model.findData('gpt-5.4-mini')>=0
        assert window.compare_model.findData('gpt-5.5-pro')<0
        assert window.compare_model.findData('gpt-5.4-mini')<0
        assert window.compare_model.findData('gpt-5.6')>=0
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


def test_project_selection_preserves_filters_and_back_restores_exact_call(dashboard):
    w=dashboard;w.nav.setCurrentRow(w.navigation_pages.index(2));click_row(w,w.parent_table,1);click_row(w,w.table,0);click_row(w,w.table,1);click_row(w,w.table,1)
    before=w.capture_state();assert w.selected_call
    click_row(w,w.parent_table,0)
    assert w.record_view=='sessions' and w.selected_session is None and w.selected_call is None and w.selected_turn is None
    assert w.period.currentData()==before['common']['period']
    click(w,control(w,w.back_button))
    assert w.selected_call==before['selected_call'] and w.selected_turn==before['selected_turn']
    assert w.detail_scroll.isVisible() and not w.qml_errors


def test_comparison_detail_home_back_and_refresh_preserve_context(dashboard):
    w=dashboard;w.nav.setCurrentRow(1);w.select_comparison(w.comparison_chart.rows[0])
    selected=w.comparison_selection.copy();assert w.home_button.isEnabled()
    w.go_home();assert not w.comparison_detail['group'].isVisible()
    w.go_back();assert w.comparison_selection==selected and w.comparison_detail['group'].isVisible()
    before=w.scrollers[1].verticalPosition.value()
    w.receive(copy.deepcopy(w.snapshot))
    assert w.comparison_selection==selected and w.scrollers[1].verticalPosition.value()==before
    assert not w.qml_errors
