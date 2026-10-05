"""User-facing evidence distinguishes missing source data from real failures."""
import time
import pytest

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from cachemonitor.dashboard import Dashboard, choose
from cachemonitor.core import Session
from cachemonitor.ui_details import price_reason, record_issues


def sample():
    now=time.time()
    session=Session('task','home',title='Actual task name',cwd='D:/work/long/project')
    session.add_usage(now-5,'response',dict(input_tokens=1000,cached_input_tokens=800,
        cache_write_input_tokens=0,output_tokens=20,reasoning_output_tokens=5),
        'gpt-6-astra','turn','high',service_tier='Standard')
    view=session.view(now)
    view.update(project='codex:project-id',project_name='Named project')
    return dict(ts=now,sessions=[view],homes=['home'],errors=[],unassigned=[])


def dashboard(tmp_path):
    app=QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration',True)
    return Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))


def test_real_conflict_is_visible_but_missing_wire_evidence_is_not_an_error(tmp_path):
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.nav.setCurrentRow(window.navigation_pages.index(2))
        window.record_view='calls';window.render_explorer()
        row=window.record_rows[0]
        assert record_issues(row)==[]
        conflicted=dict(row,requested_model='gpt-6-astra',response_model='gpt-5.6-sol',model_alert_confirmed=True)
        window.render_record_detail(conflicted)
        assert window.detail_sections['evidence'][0].isVisible()
        assert '요청 모델과 응답 모델이 다릅니다' in window.detail_sections['evidence'][1].text()
        assert 'gpt-5.6-sol' in window.detail_sections['conditions'][1].text()
        window.render_record_detail(row)
        assert not window.detail_sections['evidence'][0].isVisible()
    finally:window.quit_app()


@pytest.mark.parametrize('language',['ko','en'])
def test_subscription_prices_and_call_detail_render_supported_models(tmp_path,language):
    from PySide6.QtTest import QTest
    from cachemonitor.i18n import set_language
    from cachemonitor.pricing import usd
    from cachemonitor.table_model import Table
    from cachemonitor.quick_qa import render_plot
    set_language(language);window=dashboard(tmp_path)
    try:
        snapshot=sample();snapshot['sessions'][0]['history'][0]['service_tier']='Fast'
        window.receive(snapshot);window.show();window.nav.setCurrentRow(window.navigation_pages.index(2))
        window.record_view='calls';window.render_explorer()
        row=window.record_rows[0];window.activate_record(0)
        window.record_section='pricing';window.render_record_detail(row)
        detail=window.detail_sections['pricing'][1]
        assert '× 2.5' in detail.text() and usd(row['cost']) in detail.text()
        assert '$10.00' in detail.text()  # Standard Astra input rate.
        assert '청구액' not in detail.text() and '청구액' not in window.history_summary.text()
        rendered=render_plot(window,window.detail_sections['pricing'][0])
        assert rendered.isVisible() and rendered.height()>0
        assert window.grab().save(str(tmp_path/f'subscription-detail-{language}.png'))
        window.show_prices();dialog=window.price_dialog
        prices=next(node for node in dialog.findChildren(Table))
        assert window.price_button.text()=='API 가격표'
        assert dialog.host.windowTitle().startswith('API prices' if language=='en' else 'API 가격표')
        assert prices.model().headers==['모델','일반 입력','캐시 읽기','캐시 쓰기','출력','Fast 배수']
        rows=prices.model().rows
        models=[cells[0] for cells in rows]
        assert len(models)==9 and 'gpt-6.1-sol' in models and 'gpt-daybreak-blue-latest' in models
        assert all(model not in models for model in ('gpt-5.5-pro','gpt-5.4-mini','gpt-5.6'))
        assert next(cells for cells in rows if cells[0]=='gpt-daybreak-blue-latest')[-1]=='미지원'
        sol=next(cells for cells in rows if cells[0]=='gpt-6.1-sol')
        assert sol[1:]==['$2.00','$0.1000','$2.50','$10.00','×2.5']
        assert '청구액' not in str(dialog.state)
        if language=='en':
            assert 'Subscription multiplier' in detail.state['text']
            assert 'Standard API token rates' in detail.state['text']
            assert 'not a bill' not in str(dialog.state)
        QTest.qWait(80)
        assert dialog.host.grab().save(str(tmp_path/f'subscription-prices-{language}.png'))
        assert not window.qml_errors and not dialog.host.qml_errors
    finally:
        if getattr(window,'price_dialog',None) and window.price_dialog.host:
            window.price_dialog.reject();QTest.qWait(30)
        window.quit_app();set_language('ko')


def test_unpriced_call_explains_missing_input_without_claiming_collection_error(tmp_path):
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.nav.setCurrentRow(window.navigation_pages.index(2))
        window.record_view='calls';window.render_explorer()
        row=dict(window.record_rows[0],cost=None,service_tier='미확인',price_issue='요청 모드 미확인',price_issues=['요청 모드 미확인'])
        window.render_record_detail(row)
        assert window.detail_sections['pricing'][1].text()=='환산 제외 / 요청 모드가 기록되지 않음'
        assert not window.detail_sections['evidence'][0].isVisible()
        assert '요청 모드가 기록되지 않음' in window.detail_sections['pricing'][1].text()
        assert '미확인' in window.response_cell(row,1,Qt.DisplayRole)
        assert '공식 단가' in price_reason(dict(price_issues=['Fast 장문 단가 미확인']))
        assert price_reason(dict(mode_conflict=True,price_issues=['요청 모드 미확인']))=='요청 모드 기록이 서로 다름'
        assert price_reason(dict(model_conflict=True,price_issues=['분석 모델 미확인']))=='요청 모델 기록이 서로 다름'
    finally:window.quit_app()


def test_open_aggregate_refreshes_values_and_records_without_reveal(tmp_path):
    import copy
    window=dashboard(tmp_path)
    window.show()
    try:
        snapshot=sample();window.receive(snapshot);window.open_summary(0)
        before=window.aggregate_selection['value']
        reveals=[];window.scrollers[0].revealRequested.connect(reveals.append)
        updated=copy.deepcopy(snapshot);updated['ts']+=1
        extra=dict(updated['sessions'][0]['history'][0],key='response-new',ts=updated['ts']-.1)
        updated['sessions'][0]['history'].append(extra)
        updated['sessions'][0]['usage_revision']='new'
        window.receive(updated)
        assert window.aggregate_selection['value']==before*2
        assert len(__import__('cachemonitor.dashboard_views',fromlist=['resolve_population']).resolve_population(window.engine,window.aggregate_records))==2
        assert reveals==[]
        window.select_aggregate(window.source_bars.rows[0]);reveals.clear()
        updated['ts']+=1
        updated['sessions'][0]['history'].append(dict(extra,key='response-third',ts=updated['ts']-.1))
        updated['sessions'][0]['usage_revision']='third';window.receive(updated)
        assert window.aggregate_selection['total']==before*3
        assert len(__import__('cachemonitor.dashboard_views',fromlist=['resolve_population']).resolve_population(window.engine,window.aggregate_records))==3 and reveals==[]
    finally:window.quit_app()
