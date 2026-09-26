"""The macOS menu item is one surface with optional quota text."""
from cachemonitor.macos_status import MacQuotaIndicator

import pytest


class Tray:
    def __init__(self):self.calls=[]
    def setQuota(self, display, enabled, mode):self.calls.append((display, enabled, mode))
    def close(self):self.closed=True


def test_status_quota_switch_changes_text_without_creating_another_window():
    tray=Tray();indicator=MacQuotaIndicator(None,tray)
    display=dict(text='63',remaining=63)
    indicator.set_display(display,'weekly','quota')
    assert tray.calls[-1]==(display,False,'weekly')
    indicator.set_enabled(True)
    assert tray.calls[-1]==(display,True,'weekly')
    indicator.set_display(dict(text='?',remaining=None),'five_hour','waiting')
    assert tray.calls[-1][2]=='five_hour'
    indicator.set_enabled(False)
    assert tray.calls[-1][1] is False
    assert indicator.native is None
    indicator.close();assert tray.closed


@pytest.mark.parametrize('error',[OSError('Registry denied'),RuntimeError('LaunchAgent unavailable')])
@pytest.mark.parametrize('inspect_fails',[False,True])
def test_startup_failure_reports_and_restores_verified_state_without_escaping_ui(monkeypatch,error,inspect_fails):
    from types import SimpleNamespace
    from cachemonitor import tray,windows_integration
    reported=[];checked=[];unavailable=[]
    def denied(*args):raise error
    backend=SimpleNamespace(set_enabled=denied)
    monkeypatch.setattr(windows_integration,'WindowsStartup',lambda name:backend)
    monkeypatch.setattr(tray.QMessageBox,'warning',lambda *args:reported.append(args[2]))
    window=SimpleNamespace(snapshot={'homes':['synthetic-home']},_startup_last_verified=False,
                           startup=SimpleNamespace(setChecked=checked.append),
                           startup_unavailable=lambda:unavailable.append(True),
                           startup_enabled=denied if inspect_fails else lambda:False)
    tray.TrayWindow.set_startup(window,True)
    assert reported==[str(error)] and checked==[False]
    assert bool(unavailable)==inspect_fails


@pytest.mark.parametrize('error',[OSError('Read denied'),RuntimeError('LaunchAgent ownership conflict')])
def test_unreadable_startup_state_keeps_dashboard_usable_and_marks_setting_unknown(tmp_path,monkeypatch,error):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.tray import TrayWindow
    from cachemonitor import windows_integration
    from test_ui import snapshot
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    def unreadable():raise error
    monkeypatch.setattr(TrayWindow,'startup_enabled',staticmethod(unreadable))
    monkeypatch.setattr(windows_integration.WindowsStartup,'set_enabled',
                        lambda *args:pytest.fail('Reading unavailable state must not change login services'))
    window=None
    try:
        window=Dashboard(['fixture'],start_worker=False,settings=QSettings(str(tmp_path/'ui.ini'),QSettings.IniFormat),
                         index_path=tmp_path/'index.sqlite',static_snapshot=snapshot(),live_limits=False)
        window.show();window.open_settings();app.processEvents()
        assert window.isVisible() and window.current_page==4 and not window.qml_errors
        assert window._startup_last_verified is None
        assert not window.startup.isEnabled() and '확인 실패' in window.startup.text()
        page=window.settings_page
        assert not page.controls['startup'].isEnabled()
        assert page.startup_status.isVisible() and '설정을 잠갔습니다' in page.startup_status.text()
        assert page.controls['startup'].state['tooltip']==page.startup_status.text()
    finally:
        if window:
            window.cache_panel.stop();window.observer_panel.stop();window.quitting=True
            window.tick.stop();window.tray.hide();window.close()
        app.setProperty('cachemonitorDisableShellIntegration',previous)
