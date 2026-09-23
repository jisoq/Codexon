import sys

import pytest
from PySide6.QtWidgets import QApplication
from cachemonitor.windows_integration import visible_window_rect


@pytest.mark.parametrize('rect,work', [
    ((-1338,-2200,2982,2051),(0,0,5120,2088)),
    ((100,100,2600,1600),(0,0,1920,1040)),
    ((5000,3000,1200,800),(-1920,0,0,1040)),
])
def test_recovery_contains_unreachable_window_in_nearest_work_area(rect,work):
    x,y,w,h=visible_window_rect(rect,work)
    assert work[0]<=x and work[1]<=y
    assert x+w<=work[2] and y+h<=work[3]


def test_recovery_preserves_accessible_secondary_monitor_position():
    rect=(-1800,80,1200,800)
    assert visible_window_rect(rect,(-1920,0,0,1040))==rect


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
