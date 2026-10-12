import sys

import pytest
from PySide6.QtWidgets import QApplication


@pytest.mark.skipif(sys.platform!='win32',reason='Actual HWND placement')
def test_hidden_and_minimized_dashboard_reopens_after_native_offscreen_move():
    from cachemonitor.tray import TrayWindow
    from cachemonitor.taskbar import NativeTaskbar
    app=QApplication.instance() or QApplication([])
    window=TrayWindow()
    native=NativeTaskbar()
    try:
        window.resize(700,450)
        window.show_window();app.processEvents()
        hwnd=int(window.winId())
        for minimize in (False,True,False):
            assert native.api.SetWindowPos(hwnd,None,-12000,-12000,700,450,0x0014)
            if minimize:window.showMinimized()
            else:window.hide()
            window.show_window();app.processEvents()
            rect=native.rect(hwnd)
            info=native.MonitorInfo();info.size=native.ctypes.sizeof(info)
            native.api.GetMonitorInfoW(native.api.MonitorFromWindow(hwnd,2),native.ctypes.byref(info))
            assert native.api.IsWindowVisible(hwnd)
            assert rect.left()>=info.work.left and rect.top()>=info.work.top
            assert rect.right()<info.work.right and rect.bottom()<info.work.bottom
    finally:
        window.hide();window.deleteLater();native.close();app.processEvents()


@pytest.mark.skipif(sys.platform!='win32',reason='Actual HWND maximization')
def test_settings_click_and_reopen_preserve_maximized_dashboard(tmp_path):
    import time
    from PySide6.QtCore import QSettings
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.quick_qa import control,click
    app=QApplication.instance() or QApplication([])
    window=Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))

    def await_maximized():
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            app.processEvents()
            if window.isVisible() and window.isMaximized() and not window.isMinimized():return
            QTest.qWait(10)
        assert window.isVisible() and window.isMaximized() and not window.isMinimized()

    try:
        window.showMaximized();await_maximized()
        before=window.frameGeometry()
        click(window,control(window,window.settings_button))
        await_maximized()
        assert window.current_page==4 and window.frameGeometry()==before
        for hide in (window.hide,window.showMinimized):
            hide();app.processEvents();window.show_window();await_maximized()
            assert window.frameGeometry()==before
        assert not window.qml_errors
    finally:
        window.quit_app();app.processEvents()
