"""Settings discovery, migration and keyboard navigation on the real QML scene."""
import pytest
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.settings_page import SettingsPage
from cachemonitor.dashboard import Dashboard
from cachemonitor.quick_qa import control,click,render_plot
from cachemonitor.i18n import set_language

@pytest.mark.parametrize('old,new',[(0,'general'),(1,'display'),(2,'display'),(3,'integration'),(4,'notifications'),(5,'about'),(6,'integration'),('bad','general'),(-1,'general')])
def test_legacy_categories(old,new):
    assert SettingsPage.category_id(old)==new

@pytest.mark.parametrize('language',['ko','en'])
def test_search_navigation_and_preserved_preferences(tmp_path,monkeypatch,language):
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    set_language(language)
    settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat);settings.setValue('settings/category',6)
    window=Dashboard([],start_worker=False,settings=settings,live_limits=False,manage_observer=False)
    try:
        window.resize(1000,700);window.show();window.open_settings();QTest.qWait(80)
        page=window.settings_page
        assert page.current_category()=='integration'
        field=control(window,page.search);field.forceActiveFocus();QTest.keyClicks(window.quick,'diagnostics');QTest.qWait(60)
        assert page.stack.currentIndex()==6 and page.result_body.count()==1
        button=page.result_body.nodes[0].nodes[1]
        item=render_plot(window,button);item.forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_Return);QTest.qWait(60)
        assert page.current_category()=='troubleshooting' and window.diagnostic_details.toggle.isChecked()
        assert page.search.text()==''
        assert control(window,window.diagnostic_details.toggle).property('activeFocus')
        control(window,page.search).forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_Tab)
        assert control(window,page.clear_search).property('activeFocus')
        page.search.setText('zz_no_result');QTest.qWait(30)
        assert page.result_body.count()==1 and page.result_body.nodes[0].kind=='text'
        control(window,page.search).forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_Escape)
        assert page.search.text()=='' and page.stack.currentIndex()==4
        page.search.setText('CACHE refresh');assert page.result_body.count()==1
        page.reveal('notifications');window.notification_master.setChecked(False)
        assert all(not option.isEnabled() for option in window.notification_options.values())
        values=[option.isChecked() for option in window.notification_options.values()]
        window.notification_master.setChecked(True)
        assert values==[option.isChecked() for option in window.notification_options.values()]
        assert not window.cache_shortcut.isEnabled()
        calls=[];monkeypatch.setattr('cachemonitor.update_panel.open_recovery',lambda *args:calls.append(args))
        page.reveal('troubleshooting');click(window,render_plot(window,window.recovery_button));assert len(calls)==1
        page.search.setText('cache');page.navigation.choose(page.navigation.currentRow())
        assert not page.search.text()
        page.restore_scrolls([100]*7)
        state=window.capture_state();assert state['settings_category']=='troubleshooting'
        assert isinstance(state['settings_scrolls'],dict)
        assert not window.qml_errors
    finally:
        window.quit_app();set_language('ko');app.setProperty('cachemonitorDisableShellIntegration',previous)
