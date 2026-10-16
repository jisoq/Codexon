"""User-facing evidence distinguishes missing source data from real failures."""
import time
import pytest

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from cachemonitor.dashboard import Dashboard, choose
from cachemonitor.core import Session
from cachemonitor.ui_details import price_reason, record_issues

pytestmark = pytest.mark.integration


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


def deliver_pending(window):
    query=window.pending_query
    window.analysis_ready(dict(id=window.request_id,logical=window.pending_logical,
        result=window.engine.page_query(query),valid_until=float('inf')))


def test_displayed_generation_refreshes_stale_detail_without_losing_selection(tmp_path):
    from cachemonitor.analysis_worker import AnalysisBridge
    from cachemonitor.quick_qa import control,render_plot
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.resize(1000,680);window.show();window.tick.stop()
        window.change_page(2);window.selected_session=('home','task');window.record_view='calls'
        window.history_display='chart';window.render()
        chart=render_plot(window,window.history_chart)
        assert chart.width()>0 and chart.height()>0
        window.history_chart.selected.emit(window.history_chart.rows[0])
        detail=control(window,window.detail_scroll)
        assert detail.width()>0 and detail.height()>0
        row=window.exact_record
        assert row is not None
        selected=(window.selected_session,window.selected_turn,window.selected_call,window.selected_call_scope)
        first=('prepared-test',1);second=('prepared-test',2);third=('prepared-test',3)
        window.view_result=dict(window.view_result,prepared_token=first)
        bridge=AnalysisBridge([],cache_floor=window.confirmed_notifications.cache_floor)
        window.worker=bridge;window.async_mode=True
        bridge.stale.connect(window.analysis_stale);bridge.record.connect(window.record_ready)
        bridge.result.connect(window.analysis_ready)
        faults=[];bridge.fault.connect(faults.append)
        window.request_exact_record();old_id=window.record_request;old_query=window.request_id
        assert bridge.last_record['prepared_token']==first
        assert bridge.last_record['identity']==('home','task','response')
        bridge._deliver_result(dict(kind='stale',consumer='record',id=old_id,prepared_token=second))
        assert bridge.last_record is None and not faults
        assert window.exact_record is None and not window.detail_scroll.isVisible()
        assert window.record_status=='' and window.record_message.text()==''
        assert window.request_id==old_query+1 and bridge.pending['id']==window.request_id
        assert (window.selected_session,window.selected_turn,window.selected_call,window.selected_call_scope)==selected
        fresh=dict(window.engine.page_query(window.pending_query),prepared_token=second)
        bridge._deliver_result(dict(kind='result',id=window.request_id,logical=window.pending_logical,
            result=fresh,valid_until=float('inf')))
        assert window.exact_record==row and window.view_result['prepared_token']==second
        assert control(window,window.detail_scroll).width()>0
        window.request_exact_record();old_id=window.record_request
        assert bridge.last_record['prepared_token']==second
        window.apply_result(dict(fresh,prepared_token=third),window.pending_query)
        displayed=window.exact_record
        bridge._deliver_result(dict(kind='record',id=old_id,prepared_token=second,row=dict(row,output=9999)))
        assert window.exact_record is displayed and window.exact_record['output']!=9999
        window.request_exact_record();old_query=window.request_id
        bridge._deliver_result(dict(kind='record',id=window.record_request,prepared_token=second,row=dict(row,output=9999)))
        assert window.exact_record is None and window.request_id==old_query+1
        assert window.selected_call=='response' and not faults
        window.view_result={key:value for key,value in window.view_result.items() if key!='prepared_token'}
        window.request_exact_record();old_query=window.request_id
        assert window.record_request_token is None
        bridge._deliver_result(dict(kind='record',id=window.record_request,prepared_token=third,row=row))
        assert window.exact_record is None and window.request_id==old_query+1
    finally:window.quit_app()


def test_stale_population_closes_chart_hover_and_notification_floor_stays_current(tmp_path):
    from cachemonitor.analysis_worker import AnalysisBridge
    from cachemonitor.charts import UsageTrend
    from cachemonitor.quick_qa import render_plot
    window=dashboard(tmp_path)
    try:
        snapshot=sample();window.receive(snapshot);window.resize(1000,680);window.show();window.tick.stop()
        window.change_page(2);window.selected_session=('home','task');window.record_view='calls'
        window.history_display='chart';window.render()
        population_chart=UsageTrend();population_chart.setFixedHeight(180)
        population_chart.set_rows([dict(value=1.,n=1,N=1,label='Selected population')])
        window.history_chart.hide();window.history_chart_panel.layout().addWidget(population_chart)
        chart=render_plot(window,population_chart)
        assert chart.width()>0 and chart.height()>0 and population_chart.hits
        position=population_chart.hits[0][0].center()
        chart.showTip(position.x(),position.y())
        assert chart.tip
        floor=window.confirmed_notifications.cache_floor
        bridge=AnalysisBridge([],cache_floor=floor);window.worker=bridge;window.async_mode=True
        bridge.stale.connect(window.analysis_stale)
        bridge.set_cache_floor(floor)
        assert not bridge.commands
        for increment in range(1,4):bridge.set_cache_floor(floor+increment)
        assert [value for value in bridge.commands if value['kind']=='cache_floor']==[
            dict(kind='cache_floor',value=floor+3)]
        window.confirmed_notifications.cache_floor=floor+4
        window.receive(snapshot)
        assert bridge.cache_floor==floor+4
        assert [value for value in bridge.commands if value['kind']=='cache_floor']==[
            dict(kind='cache_floor',value=floor+4)]
        overlay=[];bridge.overlay_detail.connect(overlay.append)
        summary={'_prepared_token':('prepared-test',2)}
        bridge.request_overlay_detail(8,summary,health=True)
        assert bridge.commands[-1]==dict(kind='overlay_detail',id=8,summary=summary,health=True,
            prepared_token=('prepared-test',2))
        bridge._deliver_result(dict(kind='stale',consumer='overlay_detail',id=8,prepared_token=('prepared-test',3)))
        assert len(overlay)==1 and overlay[0]['kind']=='stale'
        old_query=window.request_id
        bridge._deliver_result(dict(kind='stale',consumer='population',id=42,prepared_token=('prepared-test',2)))
        assert not chart.tip and chart.hoverIndex==-1
        assert window.request_id==old_query+1 and not window.analysis_errors
        assert window.current_page==2 and window.selected_session==('home','task')
    finally:window.quit_app()


@pytest.mark.parametrize('language',['ko','en'])
@pytest.mark.essential
def test_tab_loading_hides_empty_content_and_ignores_stale_result(tmp_path,language):
    from PySide6.QtTest import QTest
    from cachemonitor.i18n import set_language
    from cachemonitor.quick_qa import control
    set_language(language);window=dashboard(tmp_path)
    try:
        window.receive(sample());window.show();window.render();window.async_mode=True
        window.change_page(1)
        old_id,old_query,old_logical=window.request_id,window.pending_query,window.pending_logical
        assert window.analysis_pending and not window.pages.isVisible()
        assert window.analysis_notice.isVisible() and not window.analysis_notice_body.isVisible()
        # Timer expiry and QML visibility propagation are separate event-loop
        # turns. Await the rendered state instead of assuming 200 ms is enough.
        deadline=time.monotonic()+5
        title=None
        while time.monotonic()<deadline:
            QApplication.processEvents()
            try:title=control(window,window.analysis_notice_title)
            except AssertionError:pass
            if title is not None and title.isVisible() and title.width()>0 and title.height()>0:break
            QTest.qWait(10)
        if title is None or not title.isVisible():
            from pathlib import Path
            evidence=Path('artifacts/verification/dashboard-loading');evidence.mkdir(parents=True,exist_ok=True)
            window.grab().save(str(evidence/f'failed-{language}.png'))
        assert title is not None
        assert title.isVisible()
        assert ('Loading' if language=='en' else '불러오는 중') in window.analysis_notice_title.state['text']
        assert window.grab().save(str(tmp_path/f'tab-loading-{language}.png'))
        window.show_analysis_slow()
        assert '다른 탭' in window.analysis_notice_detail.text()
        window.change_page(2)
        window.analysis_ready(dict(id=old_id,logical=old_logical,
            result=window.engine.page_query(old_query),valid_until=float('inf')))
        assert window.analysis_pending and not window.pages.isVisible()
        deliver_pending(window)
        assert window.pages.isVisible() and not window.analysis_notice.isVisible()
        window.render()  # A valid cache hit never enters the loading state.
        assert not window.analysis_pending and not window.pending_timer.isActive()
        window.change_page(5)
        assert not window.pages.isVisible()
        window.change_page(3)
        window.show_analysis_pending();window.show_analysis_slow()
        assert window.pages.isVisible() and not window.analysis_notice.isVisible()
        assert not window.pending_label.isVisible()
        assert not window.qml_errors
    finally:window.quit_app();set_language('ko')


def test_refresh_preserves_only_matching_results_and_failure_can_retry(tmp_path):
    from PySide6.QtTest import QTest
    from cachemonitor.quick_qa import click,render_plot
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.show();window.change_page(1);window.render();window.async_mode=True
        window.client_cache.clear();window.render();window.show_analysis_pending()
        assert window.pages.isVisible() and window.pending_label.text()=='최신 기록 반영 중'
        window.analysis_failed('synthetic failure')
        assert window.pages.isVisible() and window.error_button.isVisible()
        assert not window.retry_button.isVisible() and not window.analysis_notice.isVisible()
        click(window,render_plot(window,window.error_button))
        assert window.current_page==4 and window.error_panel.selected=='analysis'
        click(window,render_plot(window,window.error_panel.action))
        assert window.analysis_pending
        deliver_pending(window)
        assert not window.retry_button.isVisible()
        window.metric_panel.change('period','7d')
        assert window.analysis_pending and not window.pages.isVisible()
        window.analysis_failed('changed-filter failure')
        assert not window.pages.isVisible() and window.error_button.isVisible()
        assert not window.analysis_notice.isVisible()
        assert window.grab().save(str(tmp_path/'tab-loading-failed.png'))
        click(window,render_plot(window,window.error_button))
        assert window.current_page==4 and window.error_panel.selected=='analysis'
        click(window,render_plot(window,window.error_panel.action))
        assert window.analysis_pending and not window.pages.isVisible()
        deliver_pending(window)
        assert window.pages.isVisible() and not window.analysis_notice.isVisible()
        assert not window.qml_errors
    finally:window.quit_app()


def test_empty_result_waits_for_collection_before_showing_empty_charts(tmp_path):
    from PySide6.QtTest import QTest
    window=dashboard(tmp_path)
    try:
        window.show();window.async_mode=True
        window.snapshot['index']={'loading':True}
        window.render();deliver_pending(window)
        assert not window.analysis_pending and not window.pages.isVisible()
        assert window.analysis_notice_title.text()=='사용 기록 수집 중'
        window.snapshot['index']['loading']=False
        window.render();deliver_pending(window)
        assert window.pages.isVisible() and not window.analysis_notice.isVisible()
        assert window.home_panel.state['totals'][1]['value']=='0'
        assert window.analysis['response_count']==0
        window.change_page(5);window._refresh_after_pending=True
        calls=[];window.render=lambda *args,**kwargs:calls.append(kwargs)
        deliver_pending(window);QTest.qWait(20)
        assert calls==[{'automatic':True}]
        assert not window.qml_errors
    finally:window.quit_app()


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
    window=dashboard(tmp_path);window.show()
    try:
        snapshot=sample();window.receive(snapshot);window.change_page(1);p=window.metric_panel
        p.select(p.data['rows'][0]['key']);before=p.data['summary']['value'];selected=p.capture()
        updated=copy.deepcopy(snapshot);updated['ts']+=1
        extra=dict(updated['sessions'][0]['history'][0],key='response-new',ts=updated['ts']-.1)
        updated['sessions'][0]['history'].append(extra);updated['sessions'][0]['usage_revision']='new'
        window.receive(updated)
        assert p.data['summary']['value']==before*2 and p.options['selected']==selected['selected']
        assert len(__import__('cachemonitor.dashboard_views',fromlist=['resolve_population']).resolve_population(window.engine,p.data['population']))==2
    finally:window.quit_app()
