"""User-facing evidence distinguishes missing source data from real failures."""
import time
import pytest

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from cachemonitor.dashboard import PAGE_INDEX, Dashboard
from cachemonitor.core import Session

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
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    try:
        return Dashboard([],start_worker=False,live_limits=False,
            settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    finally:app.setProperty('cachemonitorDisableShellIntegration',previous)


def close_dashboard(window):
    from cachemonitor.quick_qa import dispose
    from test_ui import wait_for_rendered
    window.quit_app()
    wait_for_rendered(lambda:window.quitting and window.shutdown_operation is None,timeout=10)
    dispose(window)


def deliver_pending(window):
    query=window.pending_query
    window.analysis_ready(dict(id=window.request_id,logical=window.pending_logical,
        result=window.engine.page_query(query),valid_until=float('inf')))


def test_hidden_dashboard_restored_navigation_never_requests_aggregate_until_shown(tmp_path,monkeypatch):
    from cachemonitor.analysis_worker import AnalysisBridge
    from test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    requests=[]
    monkeypatch.setattr(AnalysisBridge,'start',lambda self:None)
    monkeypatch.setattr(AnalysisBridge,'request',lambda self,*args:requests.append(args))
    monkeypatch.setattr(Dashboard,'start_quota_service',lambda *args:None)
    settings=QSettings(str(tmp_path/'demand.ini'),QSettings.IniFormat)
    settings.setValue('dashboard/page',1)
    window=None
    try:
        window=Dashboard([],settings=settings,index_path=tmp_path/'index.sqlite',
            quota_path=tmp_path/'quota.sqlite',live_limits=False,collection_autostart=False)
        assert not window.isVisible() and not requests
        window.change_page(1)
        window.render();window.render(automatic=True)
        app.processEvents()
        assert not requests
        window.show()
        wait_for_rendered(lambda:bool(requests))
        assert requests[-1][1]['page']==1
        window.hide()
        before=len(requests)
        window.render();window.render(automatic=True)
        app.processEvents()
        assert len(requests)==before
        assert window.worker.last_request is None
    finally:
        if window is not None:close_dashboard(window)
        app.setProperty('cachemonitorDisableShellIntegration',previous)


@pytest.mark.parametrize('language',['ko','en'])
def test_initial_preparation_reaches_native_dashboard_before_any_snapshot(tmp_path,monkeypatch,language):
    from PySide6.QtTest import QSignalSpy
    from cachemonitor.i18n import set_language
    from cachemonitor.preparation_progress import PreparationStage,PreparationWork
    from cachemonitor.quick_qa import control,scene_view,walk
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    from cachemonitor.theme import shared_theme
    from test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    monkeypatch.setattr(Dashboard,'start_quota_service',lambda *args:None)
    monkeypatch.setattr('cachemonitor.overlay_windows.system_reduce_motion',lambda:language=='en')
    set_language(language)
    channel=CollectionChannel([tmp_path/'home'],tmp_path/'source.sqlite');channel.start_live()
    snapshot=empty_snapshot(channel.homes,channel.path,time.time())
    snapshot['index'].update(done=3,files=20)
    channel.publish(snapshot,'startup-feedback',1)
    window=controller=None
    def header_text():
        return ' '.join(item.property('text') for item in walk(control(window,window.refresh_text))
            if item.isVisible() and item.property('text'))
    try:
        window=Dashboard(channel.homes,live_limits=False,collection_autostart=False,
            index_path=channel.path,quota_path=tmp_path/'quota.sqlite',
            settings=QSettings(str(tmp_path/'startup.ini'),QSettings.IniFormat))
        progress=[];snapshots=[]
        window.worker.progress.connect(progress.append);window.worker.snapshot.connect(snapshots.append)
        origin=window.preparation.started_at
        window.resize(1000,680);window.show()
        wait_for_rendered(lambda:any(isinstance(value,PreparationWork) and value.processed==3 for value in progress),timeout=15)
        assert not snapshots and window.preparation.started_at==origin
        control(window,window.home_panel.today_summary)
        summary=scene_view(window,window.home_panel.today_summary)
        wait_for_rendered(lambda:any(item.objectName()=='analysis-region-loading' and item.isVisible()
            for item in walk(summary)))
        region=next(item for item in walk(summary) if item.objectName()=='analysis-region-loading')
        wait_for_rendered(lambda:region.isVisible() and region.height()>0)
        texts=[item.property('text') for item in walk(region) if item.isVisible() and item.property('text')]
        assert any(('Reading saved' if language=='en' else '저장된 Codex') in text for text in texts)
        assert any('3 / 20' in text for text in texts)
        assert any(('other tabs' if language=='en' else '다른 탭') in text for text in texts)
        for node in (window.home_panel.readiness_title,window.home_panel.readiness_note):
            assert not control(window,node).isVisible()
        assert not window.refresh_text.isVisible()
        assert window.quick.grabFramebuffer().save(str(tmp_path/f'initial-home-{language}.png'))
        elapsed=window.analysis_loading['elapsedText']
        activity=next(item for item in walk(region) if item.objectName()=='loading-activity')
        movement=QSignalSpy(activity.xChanged)
        wait_for_rendered(lambda:window.analysis_loading.get('elapsedText')!=elapsed,timeout=3)
        assert shared_theme().reduceMotion==(language=='en')
        if language=='en':assert movement.count()==0
        else:assert movement.count()>0
        for page in (4,3,7):
            window.change_page(page)
            wait_for_rendered(lambda:window.pages.isPageReady(PAGE_INDEX[page]),timeout=5)
            wait_for_rendered(lambda:control(window,window.refresh_text).isVisible())
            header=control(window,window.refresh_text)
            wait_for_rendered(lambda:'3 / 20' in header_text())
            assert window.analysis_loading['elapsedText'] and window.preparation.started_at==origin
            assert not window.analysis_regions()
            if page==4:assert window.quick.grabFramebuffer().save(str(tmp_path/f'initial-settings-{language}.png'))
        window.change_page(1)
        current=window.preparation
        window.analysis_progressed(dict(id=window.request_id,stage='query',processed=999,total=1000))
        assert window.preparation==current and window.analysis_loading['processed']==3
        assert not window.refresh_text.isVisible()
        snapshot['index']['done']=13;channel.publish(snapshot,'startup-feedback',2)
        wait_for_rendered(lambda:window.analysis_loading.get('processed')==13,timeout=5)
        assert not snapshots and window.preparation.started_at==origin
        from cachemonitor.overlay import install_overlay
        controller=install_overlay(window,native_enabled=False)
        assert controller.preparation==window.preparation
        snapshot['index']['done']=20;channel.publish(snapshot,'startup-feedback',3)
        wait_for_rendered(lambda:window.analysis_loading.get('processed')==20,timeout=5)
        assert window.preparation is not None and not snapshots
        window.change_page(6)
        window.analysis_failed(dict(publication_failed=True))
        window.analysis_progressed(PreparationWork(PreparationStage.CONVERTING,1,2))
        assert window.analysis_loading['mode']=='error' and not window.analysis_loading['running']
        wait_for_rendered(lambda:any(item.property('text')==window.analysis_loading['title']
            for item in walk(scene_view(window,window.home_panel.today_summary))))
        for node in (window.home_panel.readiness_title,window.home_panel.readiness_note):
            assert not control(window,node).isVisible()
        assert window.quick.grabFramebuffer().save(str(tmp_path/f'initial-error-{language}.png'))
        for page in (4,3,7):
            window.change_page(page)
            header=control(window,window.refresh_text)
            wait_for_rendered(header.isVisible)
            assert ('Try again' if language=='en' else '다시 시도') in header_text()
            assert '1 / 2' not in header_text() and '20 / 20' not in header_text()
            assert not control(window,window.refresh_status).isVisible()
            assert window.retry_button.isVisible()
        window.change_page(6)
        window.retry_analysis()
        assert window.preparation.started_at==origin and window.analysis_loading['running']
        window.receive_collection_status({'collection_fault':{'expected_stop':True}})
        window.sync_preparation()
        assert window.preparation.started_at==origin and controller.preparation is None
        for page in (4,3,7):
            window.change_page(page)
            header=control(window,window.refresh_text)
            wait_for_rendered(header.isVisible)
            assert header_text()==('Usage collection stopped' if language=='en' else '사용 기록 수집 중지')
            assert not control(window,window.refresh_status).isVisible()
            assert not window.retry_button.isVisible()
        window.change_page(6)
        window.receive_collection_status({'collection_fault':{},'collection_healthy':True})
        window.sync_preparation()
        assert window.analysis_loading['running'] and window.preparation.started_at==origin
        snapshot['ts']=time.time();snapshot['usage_collection_complete']=True
        snapshot['index'].update(loading=False,usage_complete=True)
        snapshot.update(request_activity=[],quota_by_home={})
        channel.publish(snapshot,'startup-feedback',4)
        wait_for_rendered(lambda:bool(snapshots) and window.preparation is None,timeout=15)
        wait_for_rendered(lambda:not window.analysis_pending and not window.analysis_loading,timeout=10)
        assert not window.snapshot['sessions']
        assert window.home_panel.readiness=='empty'
        for node in (window.home_panel.readiness_title,window.home_panel.readiness_note):
            wait_for_rendered(lambda:control(window,node).isVisible())
        loading=dict(window.analysis_loading)
        window.analysis_progressed(PreparationStage.STARTING)
        assert window.preparation is None and window.analysis_loading==loading
        assert controller.preparation is None
        assert not window.qml_errors
    finally:
        if controller is not None:controller.stop()
        if window is not None:close_dashboard(window)
        channel.close();set_language('ko');app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_saved_maximized_window_starts_and_updates_quota_visibility(tmp_path):
    from test_ui import wait_for_rendered
    saved=dashboard(tmp_path)
    try:
        saved.resize(720,620);saved.move(100,100);saved.showMaximized()
        wait_for_rendered(lambda:saved.isMaximized() and saved.windowHandle().isExposed())
        saved.close()
    finally:close_dashboard(saved)
    window=dashboard(tmp_path)
    try:
        window.show()
        wait_for_rendered(window.isMaximized)
        window.change_page(3)
        assert window.quota_panel.presentation_active
        window.showMinimized()
        wait_for_rendered(lambda:window.isMinimized() and not window.quota_panel.presentation_active)
        window.showNormal();window.resize(720,620)
        wait_for_rendered(lambda:not window.isMinimized() and not window.isMaximized() and
                          window.quota_panel.presentation_active)
        assert not window.qml_errors
    finally:close_dashboard(window)


@pytest.mark.parametrize('language',['ko','en'])
@pytest.mark.essential
def test_tab_loading_keeps_controls_and_ignores_stale_result(tmp_path,language):
    from PySide6.QtTest import QSignalSpy,QTest
    from cachemonitor.i18n import set_language
    from cachemonitor.theme import shared_theme
    from cachemonitor.quick_qa import control,walk
    from test_ui import wait_for_rendered
    set_language(language);window=dashboard(tmp_path)
    try:
        window.receive(sample());window.resize(1000,680);window.show();window.render();window.async_mode=True
        window.change_page(1)
        old_id,old_query,old_logical=window.request_id,window.pending_query,window.pending_logical
        assert window.analysis_pending and window.pages.isVisible()
        assert window.analysis_loading['running'] and not window.analysis_loading['retained']
        usage=control(window,window.metric_panel)
        content=next(item for item in walk(usage) if item.objectName()=='usage-chart-content')
        period=next(item for item in walk(usage) if item.objectName()=='usage-period')
        progress=next(item for item in walk(usage) if item.objectName()=='analysis-region-loading')
        wait_for_rendered(lambda:progress.isVisible() and progress.width()>0 and progress.height()>0)
        assert period.isVisible() and period.isEnabled()
        assert control(window,window.nav).isVisible()
        assert not content.isVisible() and not content.isEnabled()
        result_controls=[item for item in walk(usage)
                         if item.objectName() in ('usage-result-controls','usage-legend-row')]
        assert len(result_controls)==2
        assert all(not item.isVisible() and not item.isEnabled() for item in result_controls)
        texts=[item.property('text') for item in walk(progress) if item.property('text')]
        assert any(('Loading' if language=='en' else '불러오는 중') in text for text in texts)
        if language=='en':assert not any('\uac00'<=char<='\ud7a3' for text in texts for char in text)
        fill=next(item for item in walk(progress) if item.objectName()=='loading-completed')
        assert fill.width()==0
        activity=next(item for item in walk(progress) if item.objectName()=='loading-activity')
        reduced_motion=shared_theme().reduceMotion
        movement=QSignalSpy(activity.xChanged)
        if not reduced_motion:wait_for_rendered(lambda:movement.count()>0)
        elapsed=window.analysis_loading.get('elapsedText','')
        window.analysis_started-=2
        wait_for_rendered(lambda:bool(window.analysis_loading.get('elapsedText')) and
                          window.analysis_loading['elapsedText']!=elapsed,timeout=2)
        if reduced_motion:assert movement.count()==0
        window.analysis_progressed(dict(id=old_id,stage='query'))
        assert fill.width()==0
        window.analysis_progressed(dict(id=old_id,stage='query',processed=20,total=100,unit='sessions'))
        wait_for_rendered(lambda:fill.width()>0);reading_width=fill.width()
        assert window.quick.grabFramebuffer().save(str(tmp_path/f'partial-loading-{language}.png'))
        assert window.request_id==old_id
        window.analysis_progressed(dict(id=old_id,stage='aggregate',processed=60,total=100,unit='intervals'))
        wait_for_rendered(lambda:fill.width()>reading_width)
        assert window.analysis_stage=='aggregate'
        window.show_analysis_slow()
        assert window.analysis_stage=='aggregate' and window.analysis_loading['slow']
        window.render();window.render(automatic=True)
        assert window.request_id==old_id and window.analysis_stage=='aggregate'
        period.forceActiveFocus()
        QTest.keyClick(window.quick,Qt.Key_Home);QTest.keyClick(window.quick,Qt.Key_Down);QTest.keyClick(window.quick,Qt.Key_Escape)
        assert window.metric_panel.options['period']=='today' and window.request_id>old_id
        assert window.analysis_pending and not content.isVisible()
        window.change_page(6)
        window.analysis_progressed(dict(id=old_id,stage='aggregate'))
        assert window.analysis_stage=='queued'
        assert window.analysis_clock.isActive() and window.slow_timer.isActive()
        window.analysis_started-=8
        window.update_analysis_elapsed();window.show_analysis_slow()
        assert window.analysis_loading['elapsedText'] and window.analysis_loading['slow']
        window.analysis_ready(dict(id=old_id,logical=old_logical,
            result=window.engine.page_query(old_query),valid_until=float('inf')))
        assert window.analysis_pending and window.pages.isVisible()
        assert not window.pages.isPageReady(PAGE_INDEX[6])
        deliver_pending(window)
        assert not window.analysis_pending and window.pages.isVisible()
        assert not window.analysis_loading
        # Lazy scene construction is independent of the data result.
        window.client_cache.clear();window.render()
        assert not window.pending_retained and window.pages.isVisible()
        window.analysis_failed('unbuilt-page failure')
        assert window.pages.isVisible() and not window.analysis_loading['running']
        window.render();deliver_pending(window)
        wait_for_rendered(lambda:window.pages.isPageReady(PAGE_INDEX[6]) and window.pages.isVisible(),timeout=5)
        assert not window.analysis_loading
        window.render()
        assert not window.analysis_pending and not window.analysis_loading
        window.change_page(5)
        assert window.pages.isVisible() and window.analysis_loading['active']
        from cachemonitor.quick_qa import click
        toggle=control(window,window.performance_panel.model_panel_button)
        if toggle.isVisible():
            click(window,toggle)
            wait_for_rendered(lambda:control(window,window.performance_panel.model_choice).isVisible())
        assert control(window,window.performance_panel.model_choice).isVisible()
        assert not control(window,window.performance_panel.chart_scroll).isVisible()
        deliver_pending(window)
        wait_for_rendered(lambda:window.pages.isPageReady(PAGE_INDEX[5]) and control(window,window.performance_panel.chart_scroll).isVisible(),timeout=5)
        assert not window.analysis_loading
        for page in (3,4):
            window.change_page(page)
            assert window.pages.isVisible() and not window.analysis_loading
            window.show_analysis_pending();window.show_analysis_slow()
            wait_for_rendered(lambda:window.pages.isPageReady(PAGE_INDEX[page]),timeout=5)
            assert not window.analysis_loading and not window.refresh_status.isVisible()
        assert not window.qml_errors
    finally:close_dashboard(window);set_language('ko')


def test_refresh_preserves_only_matching_results_and_failure_can_retry(tmp_path):
    from cachemonitor.quick_qa import click,render_plot,control,walk
    from test_ui import wait_for_rendered
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.show();window.change_page(1);window.render()
        wait_for_rendered(lambda:window.pages.isPageReady(0) and window.pages.isVisible(),timeout=5)
        comparison=window.view_result
        window.change_page(6)
        wait_for_rendered(lambda:window.pages.isPageReady(PAGE_INDEX[6]) and window.pages.isVisible(),timeout=5)
        window.async_mode=True
        window.snapshot={**window.snapshot,'data_revision':1}
        window.change_page(1)
        assert window.analysis_pending and window.pending_retained
        assert window.pages.isVisible() and window.view_result is comparison
        deliver_pending(window)
        window.client_cache.clear();window.render(automatic=True)
        assert window.analysis_pending and window.pending_retained and window.pages.isVisible()
        assert window.analysis_loading['retained'] and window.slow_timer.isActive()
        window.show_analysis_slow()
        assert window.analysis_loading['slow']
        automatic_request=window.request_id
        window.render()
        assert window.request_id==automatic_request and window.analysis_loading['retained']
        deliver_pending(window)
        window.client_cache.clear();window.render()
        assert window.pages.isVisible() and window.analysis_loading['active']
        assert window.analysis_loading['running'] and window.analysis_loading['retained'] and window.pending_retained
        usage=control(window,window.metric_panel)
        chart=next(item for item in walk(usage) if item.objectName()=='usage-chart-content')
        period=next(item for item in walk(usage) if item.objectName()=='usage-period')
        assert chart.isVisible() and period.isVisible() and period.isEnabled()
        result_controls=[item for item in walk(usage)
                         if item.objectName() in ('usage-result-controls','usage-legend-row')]
        assert len(result_controls)==2
        assert all(item.isVisible() and item.isEnabled() for item in result_controls)
        previous_id=window.request_id
        window.resize(window.width()+50,window.height()+50)
        assert window.request_id==previous_id
        assert window.pending_retained and window.pages.isVisible()
        window.analysis_failed('synthetic failure')
        assert window.pages.isVisible() and window.error_button.isVisible()
        assert window.retry_button.isVisible() and window.analysis_loading['retained']
        assert not window.analysis_loading['running'] and window.refresh_text.isVisible()
        click(window,control(window,window.retry_button))
        assert window.analysis_pending
        deliver_pending(window)
        assert not window.retry_button.isVisible()
        window.metric_panel.change('period','7d')
        assert window.analysis_pending and window.pages.isVisible() and not window.analysis_loading['retained']
        assert not chart.isVisible() and not chart.isEnabled()
        assert all(not item.isVisible() and not item.isEnabled() for item in result_controls)
        assert period.isVisible() and period.isEnabled()
        window.analysis_failed('changed-filter failure')
        assert window.pages.isVisible() and window.error_button.isVisible()
        assert window.analysis_loading['active'] and not window.analysis_loading['running']
        assert window.grab().save(str(tmp_path/'tab-loading-failed.png'))
        click(window,render_plot(window,window.error_button))
        assert window.current_page==4 and window.error_panel.selected=='analysis'
        click(window,render_plot(window,window.error_panel.action))
        assert window.analysis_pending and window.pages.isVisible() and not window.analysis_loading['retained']
        deliver_pending(window)
        wait_for_rendered(lambda:window.pages.isVisible(),timeout=5)
        assert not window.analysis_loading
        window.receive(dict(sample(),epoch='replacement-worker'))
        assert window.analysis_pending and not window.pending_retained
        assert window.pages.isVisible() and not window.analysis_loading['retained']
        assert not window.qml_errors
    finally:close_dashboard(window)


def test_comparison_display_controls_keep_chart_without_new_query(tmp_path):
    from cachemonitor.quick_qa import control,walk
    from test_ui import wait_for_rendered
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.resize(1000,680);window.show();window.change_page(1)
        wait_for_rendered(lambda:window.pages.isPageReady(0),timeout=5)
        panel=window.metric_panel
        chart=next(item for item in walk(control(window,panel)) if item.objectName()=='usage-chart-content')
        window.async_mode=True
        request=window.request_id
        rows=panel.data['rows']
        panel.change('search','no matching project')
        wait_for_rendered(lambda:not panel.data['rows'])
        assert window.request_id==request and not window.analysis_pending and chart.isVisible()
        panel.change('search','')
        wait_for_rendered(lambda:panel.data['rows']==rows)
        panel.change('sort','name');panel.change('mode','ratio')
        assert window.request_id==request and not window.analysis_pending and chart.isVisible()
        assert panel.options['mode']=='ratio' and not window.qml_errors
    finally:close_dashboard(window)


def test_refresh_indicator_does_not_cover_existing_chart(tmp_path):
    from PySide6.QtCore import QPointF
    from cachemonitor.quick_qa import control,walk
    from test_ui import wait_for_rendered
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.resize(1000,680);window.show();window.change_page(5)
        wait_for_rendered(lambda:window.pages.isPageReady(PAGE_INDEX[5]),timeout=5)
        chart=control(window,window.performance_panel.chart_scroll)
        window.quick.grabFramebuffer()
        position=chart.mapToScene(QPointF(0,0));size=chart.size()
        window.async_mode=True;window.client_cache.clear();window.render()
        assert window.pending_retained
        def indicators():
            return [item for item in walk(window.quick.rootObject()) if item.isVisible()
                    and item.objectName() in ('analysis-region-loading','analysis-refresh-status')]
        wait_for_rendered(lambda:bool(indicators()))
        assert len(indicators())==1
        progress=indicators()[0]
        assert window.quick.grabFramebuffer().save(str(tmp_path/'refresh-layout.png'))
        assert chart.isVisible() and chart.isEnabled()
        assert progress.mapToScene(QPointF(0,progress.height())).y()<chart.mapToScene(QPointF(0,0)).y()
        assert chart.mapToScene(QPointF(0,0))==position and chart.size()==size
        deliver_pending(window)
        wait_for_rendered(lambda:not indicators())
        assert not window.qml_errors
    finally:close_dashboard(window)


def test_empty_result_waits_for_collection_before_showing_empty_charts(tmp_path):
    from cachemonitor.quick_qa import control,walk
    from test_ui import wait_for_rendered
    window=dashboard(tmp_path)
    try:
        window.show();window.change_page(1);window.async_mode=True
        window.snapshot['index']={'loading':True}
        window.render();deliver_pending(window)
        assert not window.analysis_pending and window.pages.isVisible()
        assert window.analysis_loading['active'] and window.analysis_loading['running']
        assert window.analysis_stage=='queued'
        comparison=control(window,window.metric_panel)
        wait_for_rendered(lambda:any(item.objectName()=='analysis-region-loading' and item.isVisible()
            for item in walk(comparison)))
        assert comparison.property('masked')
        window.refresh_collection_wait('failed')
        assert window.analysis_loading['mode']=='error' and not window.analysis_loading['running']
        assert window.retry_button.isVisible() and not window.analysis_clock.isActive()
        window.refresh_collection_wait('preparing')
        assert window.analysis_loading['running'] and window.analysis_clock.isActive()
        window.snapshot['index']['loading']=False
        window.render();deliver_pending(window)
        wait_for_rendered(lambda:window.pages.isVisible(),timeout=5)
        assert not window.analysis_loading
        assert not window.metric_panel.data['rows']
        assert window.analysis['response_count']==0
        window.change_page(5);window._refresh_after_pending=True
        calls=[];window.render=lambda *args,**kwargs:calls.append(kwargs)
        deliver_pending(window)
        wait_for_rendered(lambda:bool(calls))
        assert calls==[{'automatic':True}]
        assert not window.qml_errors
    finally:close_dashboard(window)


def test_quota_retry_immediately_updates_the_selected_home(tmp_path):
    from types import SimpleNamespace
    window=dashboard(tmp_path)
    home=str(tmp_path/'home');requests=[]
    try:
        window.quota_panel.set_homes([home])
        window.quota_panel.receive(dict(home=home,report=dict(home=home,cycles=[],error=True),issue='Failed'))
        window.quota_service=SimpleNamespace(home=home,retry_report=lambda:requests.append(True))
        window.retry_quota()
        assert requests==[True] and window.quota_panel.report_pending
        assert window.quota_panel.initial_loading.isVisible()
        assert not window.quota_panel.state_retry.isVisible()
    finally:
        window.quota_service=None;close_dashboard(window)


@pytest.mark.parametrize('language',['ko','en'])
def test_subscription_prices_and_call_detail_render_supported_models(tmp_path,language):
    from PySide6.QtTest import QTest,QSignalSpy
    from cachemonitor.i18n import set_language
    from cachemonitor.pricing import usd
    from cachemonitor.table_model import Table
    from cachemonitor.quick_qa import render_plot
    set_language(language);window=dashboard(tmp_path)
    try:
        window.receive(sample());window.show();window.change_page(1)
        window.show_prices();dialog=window.price_dialog
        prices=next(node for node in dialog.findChildren(Table))
        assert window.price_button.text()=='API 가격표'
        assert dialog.host.windowTitle().startswith('API prices' if language=='en' else 'API 가격표')
        assert prices.model().headers==['모델','일반 입력','캐시 읽기','캐시 쓰기','출력','단가 확인','Fast 배수']
        rows=prices.model().rows
        models=[cells[0] for cells in rows]
        assert len(models)==10 and 'gpt-6.1-sol' in models and 'gpt-6.1-terra' in models and 'gpt-daybreak-blue-latest' in models
        assert all(model not in models for model in ('gpt-5.5-pro','gpt-5.4-mini','gpt-5.6'))
        assert next(cells for cells in rows if cells[0]=='gpt-daybreak-blue-latest')[-1]=='미지원'
        sol=next(cells for cells in rows if cells[0]=='gpt-6.1-sol')
        assert sol[1:]==['$2.00','$0.1000','$2.50','$10.00','2026-09-30','×2.5']
        terra=next(cells for cells in rows if cells[0]=='gpt-6.1-terra')
        assert terra[1:]==['$1.00','$0.0500','$1.25','$5.00','2026-10-27','×2.5']
        assert '청구액' not in str(dialog.state)
        if language=='en':
            assert 'not a bill' not in str(dialog.state)
        QTest.qWait(80)
        assert dialog.host.grab().save(str(tmp_path/f'subscription-prices-{language}.png'))
        from cachemonitor.presentation import Button
        from cachemonitor.quick_qa import click,control
        reference_button=dialog.findChild(Button,'price-reference-button')
        click(dialog.host,control(dialog.host,reference_button))
        reference=dialog.reference_dialog
        criteria,aliases=reference.findChildren(Table)
        assert criteria.model().rows[-1]==['GPT-5.6 Sol 프로모션','최소 2026-11-21까지']
        assert aliases.model().rows==[('gpt-5.6','gpt-5.6-sol'),('gpt-daybreak-blue-latest','gpt-5.6-sol')]
        assert not reference.host.qml_errors
        assert reference.host.grab().save(str(tmp_path/f'price-reference-{language}.png'))
        close_button=reference.findChild(Button,'price-reference-close')
        closed=QSignalSpy(reference.finished)
        click(reference.host,control(reference.host,close_button))
        assert closed.count()==1
        assert not window.qml_errors and not dialog.host.qml_errors
    finally:
        if getattr(window,'price_dialog',None) and window.price_dialog.host:
            window.price_dialog.reject();QTest.qWait(30)
        close_dashboard(window);set_language('ko')


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
    finally:close_dashboard(window)
