"""Settings discovery, migration and keyboard navigation on the real QML scene."""
import pytest
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.settings_page import SettingsPage
from cachemonitor.dashboard import Dashboard
from cachemonitor.quick_qa import control,click,render_plot,walk,table_view,scene_view
from cachemonitor.i18n import set_language


def wait_until(predicate):
    import time
    deadline=time.monotonic()+5
    while not predicate():
        assert time.monotonic()<deadline,'Settings operation did not complete'
        QTest.qWait(10)


@pytest.fixture
def startup_settings_window(tmp_path,monkeypatch):
    from cachemonitor.tray import TrayWindow
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    monkeypatch.setattr(TrayWindow,'startup_enabled',staticmethod(lambda:False))
    registrations=[]
    monkeypatch.setattr(TrayWindow,'set_startup',lambda self,enabled,**kwargs:registrations.append((enabled,kwargs)) or True)
    settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat)
    window=Dashboard([str(tmp_path/'home')],start_worker=False,settings=settings,live_limits=False,manage_observer=False,
        index_path=str(tmp_path/'index.sqlite'),quota_path=str(tmp_path/'quota.sqlite'),collection_autostart=False)
    try:
        window.resize(900,620);window.show();window.open_settings();window.settings_page.reveal('general')
        yield window,registrations
    finally:
        window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',previous)


@pytest.mark.integration
def test_chatgpt_startup_setting_waits_for_discovery_and_preserves_preference(startup_settings_window,monkeypatch):
    import threading
    from PySide6.QtCore import QThread
    from cachemonitor.chatgpt_startup import ENABLED_KEY,APP_ID_KEY
    window,registrations=startup_settings_window
    page=window.settings_page;child=page.controls['chatgpt_startup']
    entered=threading.Event();release=threading.Event();threads=[]
    def discover():
        threads.append(QThread.currentThread());entered.set()
        assert release.wait(5)
        return 'OpenAI.ChatGPT_test!App'
    monkeypatch.setattr('cachemonitor.chatgpt_startup.discover_chatgpt',discover)
    monkeypatch.setattr('cachemonitor.chatgpt_startup.launch_chatgpt',lambda *_:pytest.fail('Enabling must not launch ChatGPT'))
    try:
        assert not child.isChecked() and not child.isEnabled()
        click(window,render_plot(window,page.controls['startup']))
        assert child.isEnabled()
        click(window,render_plot(window,child));wait_until(entered.is_set)
        assert not child.isEnabled()
        assert not window.settings.value(ENABLED_KEY,False,type=bool)
        assert threads[0] is not QApplication.instance().thread()
        # The event loop remains usable while the app lookup is still pending.
        page.search.setText('ChatGPT');assert page.result_body.count()==1
        page.search.setText('');release.set();wait_until(lambda:window.chatgpt_discovery is None)
        assert child.isChecked() and child.isEnabled()
        saved=QSettings(window.settings.fileName(),QSettings.IniFormat)
        assert saved.value(ENABLED_KEY,False,type=bool)
        assert saved.value(APP_ID_KEY)=='OpenAI.ChatGPT_test!App'
        assert registrations[-1]==(True,{'preserve_existing':True})
        click(window,render_plot(window,page.controls['startup']))
        assert child.isChecked() and not child.isEnabled()
        assert window.settings.value(ENABLED_KEY,False,type=bool)
        click(window,render_plot(window,page.controls['startup']))
        assert child.isChecked() and child.isEnabled()
        assert not window.qml_errors
    finally:release.set()


@pytest.mark.integration
def test_chatgpt_startup_discovery_failure_leaves_setting_off(startup_settings_window,monkeypatch):
    from cachemonitor.chatgpt_startup import ENABLED_KEY,APP_ID_KEY
    window,registrations=startup_settings_window;page=window.settings_page
    errors=[]
    monkeypatch.setattr('cachemonitor.chatgpt_startup.discover_chatgpt',lambda:None)
    monkeypatch.setattr('cachemonitor.tray.QMessageBox.warning',lambda *args:errors.append(args))
    click(window,render_plot(window,page.controls['startup']))
    click(window,render_plot(window,page.controls['chatgpt_startup']))
    wait_until(lambda:window.chatgpt_discovery is None)
    assert not page.controls['chatgpt_startup'].isChecked()
    assert page.controls['chatgpt_startup'].isEnabled()
    assert not window.settings.value(ENABLED_KEY,False,type=bool)
    assert not window.settings.contains(APP_ID_KEY)
    assert len(errors)==1 and registrations==[(True,{})]


@pytest.mark.integration
def test_disabling_parent_cancels_pending_chatgpt_setting(startup_settings_window,monkeypatch):
    import threading
    from cachemonitor.chatgpt_startup import ENABLED_KEY
    window,registrations=startup_settings_window;page=window.settings_page
    entered=threading.Event();release=threading.Event()
    def discover():
        entered.set();assert release.wait(5)
        return 'OpenAI.ChatGPT_test!App'
    monkeypatch.setattr('cachemonitor.chatgpt_startup.discover_chatgpt',discover)
    try:
        click(window,render_plot(window,page.controls['startup']))
        click(window,render_plot(window,page.controls['chatgpt_startup']));wait_until(entered.is_set)
        click(window,render_plot(window,page.controls['startup']))
        release.set();wait_until(lambda:window.chatgpt_discovery is None)
        assert not page.controls['chatgpt_startup'].isChecked()
        assert not page.controls['chatgpt_startup'].isEnabled()
        assert not window.settings.value(ENABLED_KEY,False,type=bool)
        assert registrations==[(True,{}),(False,{})]
    finally:release.set()


@pytest.mark.integration
@pytest.mark.parametrize('setting',['startup','chatgpt_startup'])
def test_disabling_startup_setting_cancels_waiting_login_launch(startup_settings_window,monkeypatch,setting):
    from cachemonitor.app import request_login_startup
    window,_=startup_settings_window;page=window.settings_page
    pending=[];launches=[]
    monkeypatch.setattr('cachemonitor.chatgpt_startup.discover_chatgpt',lambda:'OpenAI.ChatGPT_test!App')
    monkeypatch.setattr('cachemonitor.chatgpt_startup.launch_chatgpt',lambda app_id:launches.append(app_id))
    monkeypatch.setattr(window.service_controller,'request_login_launch',pending.append)
    click(window,render_plot(window,page.controls['startup']))
    click(window,render_plot(window,page.controls['chatgpt_startup']))
    wait_until(lambda:window.chatgpt_discovery is None)
    request_login_startup(window)
    assert len(pending)==1 and not launches
    click(window,render_plot(window,page.controls[setting]))
    # Route preparation may finish later; the captured request must stay cancelled.
    assert pending[0]()=='cancelled'
    assert not launches


@pytest.mark.parametrize('old,new',[(0,'general'),(1,'display'),(2,'display'),(3,'integration'),(4,'notifications'),(5,'about'),(6,'integration'),('bad','general'),(-1,'general')])
def test_legacy_categories(old,new):
    assert SettingsPage.category_id(old)==new

@pytest.mark.parametrize('language',['ko','en'])
@pytest.mark.integration
def test_search_navigation_and_preserved_preferences(tmp_path,monkeypatch,language):
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    set_language(language)
    settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat);settings.setValue('settings/category',6)
    window=Dashboard([str(tmp_path/'home')],start_worker=False,settings=settings,live_limits=False,manage_observer=False,
        index_path=str(tmp_path/'index.sqlite'),quota_path=str(tmp_path/'quota.sqlite'),collection_autostart=False)
    try:
        window.resize(1000,700);window.show();window.open_settings();QTest.qWait(80)
        page=window.settings_page
        assert page.current_category()=='integration'
        field=control(window,page.search);field.forceActiveFocus();QTest.keyClicks(window.quick,'repair');QTest.qWait(60)
        assert page.stack.currentIndex()==5 and page.result_body.count()==1
        button=page.result_body.nodes[0].nodes[1]
        item=render_plot(window,button);item.forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_Return);QTest.qWait(60)
        assert page.current_category()=='integration'
        assert page.search.text()=='repair' and page.search_back.isVisible()
        assert control(window,window.recovery_button).property('activeFocus')
        click(window,control(window,page.search_back))
        wait_until(lambda:page.stack.currentIndex()==len(page.IDS))
        assert page.search.text()=='repair' and not page.search_back.isVisible()
        clear=next(item for item in walk(control(window,page.search)) if item.objectName()=='inputClear')
        assert clear.isVisible()
        control(window,page.search).forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_Tab)
        assert clear.property('activeFocus')
        QTest.keyClick(window.quick,Qt.Key_Space);wait_until(lambda:not page.search.text())
        assert control(window,page.search).property('activeFocus') and not clear.isVisible()
        page.search.setText('zz_no_result');QTest.qWait(30)
        assert page.result_body.count()==1 and page.result_body.nodes[0].kind=='text'
        control(window,page.search).forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_Escape)
        assert page.search.text()=='' and page.stack.currentIndex()==3
        page.search.setText('CACHE refresh');assert page.result_body.count()==1 and page.result_body.nodes[0].kind=='text'
        page.reveal('notifications');window.notification_master.setChecked(False)
        assert all(not option.isEnabled() for option in window.notification_options.values())
        values=[option.isChecked() for option in window.notification_options.values()]
        window.notification_master.setChecked(True)
        assert values==[option.isChecked() for option in window.notification_options.values()]
        assert not hasattr(window,'cache_panel')
        assert not hasattr(window,'cache_master')
        assert window.nav.count()==5
        calls=[];monkeypatch.setattr('cachemonitor.update_panel.open_recovery',lambda *args:calls.append(args))
        page.reveal('troubleshooting');click(window,render_plot(window,window.recovery_button));assert len(calls)==1
        page.search.setText('cache');page.navigation.choose(page.navigation.currentRow())
        assert not page.search.text()
        page.restore_scrolls([100]*7)
        state=window.capture_state();assert state['settings_category']=='integration'
        assert isinstance(state['settings_scrolls'],dict)
        assert not window.qml_errors
    finally:
        window.quit_app();set_language('ko');app.setProperty('cachemonitorDisableShellIntegration',previous)


@pytest.mark.integration
def test_closed_setting_choice_ignores_wheel_but_keeps_keyboard_selection(startup_settings_window):
    from PySide6.QtCore import QPointF,QPoint
    from PySide6.QtGui import QWheelEvent
    window,_=startup_settings_window
    page=window.settings_page;choice=page.controls['language']
    item=render_plot(window,choice)
    control(window,page.search).forceActiveFocus()
    before=choice.currentData()
    saved=window.settings.value('ui/language','ko')
    point=item.mapToScene(QPointF(item.width()/2,item.height()/2))
    event=QWheelEvent(point,window.quick.mapToGlobal(point.toPoint()),QPoint(),QPoint(0,-120),
                      Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
    QApplication.sendEvent(window.quick,event);QApplication.processEvents()
    assert choice.currentData()==before
    assert window.settings.value('ui/language','ko')==saved
    item.forceActiveFocus(Qt.TabFocusReason)
    QTest.keyClick(window.quick,Qt.Key_Down if choice.currentIndex()==0 else Qt.Key_Up)
    wait_until(lambda:choice.currentData()!=before)
    assert window.settings.value('ui/language')==choice.currentData()


@pytest.mark.integration
def test_notification_history_grows_with_records_and_scrolls_selected_rows(startup_settings_window):
    from PySide6.QtCore import QPointF,QPoint
    from PySide6.QtGui import QWheelEvent
    window,_=startup_settings_window;window.resize(1000,700)
    page=window.settings_page;log=window.notification_log
    for count in (0,1,14,60,1):
        window.confirmed_notifications.records.clear()
        window.confirmed_notifications.records.extend(
            dict(at=100+i,title='수집기 오류 발생',count=1,detail=f'detail {i}',resolution='새 수집 결과 확인')
            for i in range(count))
        window.refresh_notification_log();page.reveal('notifications',log);render_plot(window,log)
        view=table_view(window,log);expected=min(count,50)
        wait_until(lambda:view.property('rows')==expected)
        assert log.rowCount()==expected
        assert view.height()>=expected*log.state['rowHeight']
        assert view.property('contentWidth')<=view.width()+1
        error_y=scene_view(window,window.error_panel).mapToScene(QPointF(0,0)).y()
        table_y=scene_view(window,log).mapToScene(QPointF(0,0)).y()
        assert error_y<table_y
        if count==60:
            view.forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_End)
            wait_until(lambda:log.currentRow()==expected-1)
            point=view.mapToScene(QPointF(20,(expected-.5)*log.state['rowHeight']))
            assert window.quick.rect().contains(point.toPoint())
            QTest.keyClick(window.quick,Qt.Key_Home);wait_until(lambda:log.currentRow()==0)
            point=view.mapToScene(QPointF(20,log.state['rowHeight']/2))
            assert window.quick.rect().contains(point.toPoint())
            scroll=control(window,page.scrollers['notifications']);flick=scroll.property('contentItem')
            previous_y=flick.property('contentY')
            wheel=QWheelEvent(point,window.quick.mapToGlobal(point.toPoint()),QPoint(0,0),QPoint(0,-120),
                              Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
            QApplication.sendEvent(window.quick,wheel)
            wait_until(lambda:flick.property('contentY')>previous_y)
    assert not window.qml_errors


@pytest.mark.integration
def test_notification_multiline_evidence_is_truncated_and_opens_full_detail(startup_settings_window):
    from cachemonitor.quick_qa import walk,click_row
    window,_=startup_settings_window;window.resize(800,650)
    detail='새 수집 결과가 30초 넘게 갱신되지 않아 최신 사용량을 확인할 수 없습니다.\n원인은 아직 확인하지 못했으며, 수집기의 실행 상태를 다시 확인하고 있습니다.'
    event=dict(at=100,title='수집기 오류 발생',count=1,detail=detail,kind='app_error',
        error_kind='collection',action='수집 상태 확인',page='collection')
    window.confirmed_notifications.records.clear();window.confirmed_notifications.records.append(event)
    window.refresh_notification_log();window.settings_page.reveal('notifications',window.notification_log)
    render_plot(window,window.notification_log)
    view=table_view(window,window.notification_log)
    labels=[item for item in walk(view) if item.objectName()=='cell-label' and '30' in item.property('text')]
    assert len(labels)==1
    assert labels[0].property('truncated')
    assert labels[0].property('contentHeight')<=window.notification_log.state['rowHeight']
    click_row(window,window.notification_log,0,3)
    assert window.error_panel.explanation.text()==detail.split('\n')[0]
    assert window.error_panel.guidance.text()==detail.split('\n')[1]


@pytest.mark.integration
def test_collection_freshness_uses_direct_observation_when_analysis_is_behind(startup_settings_window,monkeypatch):
    window,_=startup_settings_window
    now=1000.;monkeypatch.setattr('cachemonitor.dashboard.time.time',lambda:now)
    window.error_state.clock=lambda:now
    window.snapshot['ts']=950.
    window.receive_collection_status(dict(collection_fault={},collection_healthy=True,collection_snapshot_at=999.))
    window.check_stale()
    assert 'collection' not in window.error_panel.faults
    now=1031.;window.check_stale()
    assert window.error_panel.faults['collection'].code=='stale'
    window.receive_collection_status(dict(collection_fault={},collection_healthy=True,collection_snapshot_at=1031.))
    window.check_stale()
    assert 'collection' not in window.error_panel.faults


@pytest.mark.integration
@pytest.mark.parametrize('saved,uncertain',[(True,False),(False,False),(True,True)])
def test_quota_edit_error_recovers_only_after_confirmed_saved_recalculation(startup_settings_window,saved,uncertain):
    window,_=startup_settings_window
    home=window.quota_panel.expected_home
    window.receive_quota(dict(home=home,quota=None,exclusion_result=dict(ok=False,saved=saved,uncertain=uncertain)))
    assert 'quota.edit' in window.error_state.sources
    window.receive_quota(dict(home=home,quota=None,quota_faults=dict(calculation={})))
    assert 'quota.edit' in window.error_state.sources
    window.receive_quota(dict(home=home,quota=None,report_refreshed=True,quota_faults=dict(calculation={})))
    assert ('quota.edit' not in window.error_state.sources)==(saved and not uncertain)
