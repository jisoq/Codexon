"""Settings discovery, migration and keyboard navigation on the real QML scene."""
import pytest
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.settings_page import SettingsPage
from cachemonitor.dashboard import Dashboard
from cachemonitor.quick_qa import control,click,render_plot
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
        assert page.search.text()==''
        assert control(window,window.recovery_button).property('activeFocus')
        control(window,page.search).forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_Tab)
        assert control(window,page.clear_search).property('activeFocus')
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
