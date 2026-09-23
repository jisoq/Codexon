"""Real HWND checks use only windows owned by this test process."""
import ctypes
import sys
import time

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_tracking import Selection, anchored_monitor_geometry


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows overlay')
def test_foreground_companion_does_not_hide_its_codex_target():
    from cachemonitor.overlay_windows import WindowsOverlay
    class API:
        foreground=2
        minimized=False
        def IsWindow(self,hwnd):return True
        def IsWindowVisible(self,hwnd):return True
        def IsIconic(self,hwnd):return self.minimized
        def GetForegroundWindow(self):return self.foreground
    native=WindowsOverlay.__new__(WindowsOverlay)
    native.u=API()
    native.set_companions([2,3])
    assert native.visible_target(1)
    native.u.foreground=9
    assert not native.visible_target(1)
    native.u.foreground=1
    assert native.visible_target(1)
    native.u.minimized=True
    assert not native.visible_target(1)


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows overlay')
def test_native_bottom_right_move_resize_minimize_focus_and_toggle(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    app = QApplication.instance() or QApplication([])
    native = WindowsOverlay()
    host = QWidget(); host.setWindowTitle('Cache Monitor isolated overlay target')
    host.resize(800, 620); host.show(); host.activateWindow()
    controller = OverlayController(QSettings(str(tmp_path/'native.ini'), QSettings.IniFormat),
                                   native_enabled=False, appearance_path=tmp_path/'codex.toml')
    controller.native = native
    hwnd = int(controller.widget.winId()); target = int(host.winId())
    native.configure(hwnd)
    def refresh():
        controller.receive_target({'target': {'hwnd': target, 'pid': 0},
                                   'selection': Selection('fixture')})
        app.processEvents(); QTest.qWait(40)
    try:
        QTest.qWait(100)
        # Foreground matching is tested separately from OS foreground activation policy.
        original_visible = native.visible_target
        native.visible_target = lambda handle: bool(native.u.IsWindowVisible(handle) and not native.u.IsIconic(handle))
        foreground = native.u.GetForegroundWindow()
        refresh()
        assert controller.widget.isVisible()
        assert native.u.GetForegroundWindow() == foreground
        style = native.u.GetWindowLongPtrW(hwnd, -20)
        assert style & 0x08000000 and style & 0x20 and style & 0x80000
        for offset, size in ((0, (800, 620)), (90, (950, 700)), (-70, (760, 580))):
            host.move(120+offset, 100+offset); host.resize(*size)
            app.processEvents(); QTest.qWait(30); refresh()
            expected = anchored_monitor_geometry(native.frame(target), native.u.GetDpiForWindow(target),
                                    controller.widget.panel_width(), controller.widget.panel_height(),
                                    reference_height=560*controller.appearance.scale)
            actual = native.frame(hwnd)
            assert actual == (expected[0], expected[1], expected[0]+expected[2], expected[1]+expected[3])
        controller.set_position('top-right'); refresh()
        frame = native.frame(target)
        actual = native.frame(hwnd)
        gap = round(16*native.u.GetDpiForWindow(target)/96)
        assert actual[1]-frame[1] == frame[2]-actual[2] == gap
        controller.set_position('bottom-right'); refresh()
        # Move only our test HWND across the actual connected monitors. DWM reports
        # physical coordinates; mixing these with Qt logical positions causes drift.
        from ctypes import wintypes as W
        monitors = []
        @ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)
        def monitor_callback(handle, dc, rect, data):
            monitors.append((rect.contents.left, rect.contents.top, rect.contents.right, rect.contents.bottom))
            return True
        native.u.EnumDisplayMonitors.argtypes = [W.HDC, ctypes.POINTER(W.RECT), type(monitor_callback), W.LPARAM]
        native.u.EnumDisplayMonitors(None, None, monitor_callback, 0)
        checked = []
        for left, top, right, bottom in monitors:
            width, height = min(1100, right-left-80), min(850, bottom-top-80)
            assert native.u.SetWindowPos(target, None, left+40, top+40, width, height, 0x0014)
            app.processEvents(); QTest.qWait(70); refresh()
            dpi = native.u.GetDpiForWindow(target)
            expected = anchored_monitor_geometry(native.frame(target), dpi, controller.widget.panel_width(), controller.widget.panel_height(),
                                                  reference_height=560*controller.appearance.scale)
            if expected is None:
                assert not controller.widget.isVisible()
                continue
            assert native.frame(hwnd) == (expected[0], expected[1], expected[0]+expected[2], expected[1]+expected[3])
            panel=native.frame(hwnd)
            assert not controller.toolbar.isVisible()
            for control in (controller.header,controller.actions):
                assert control.isVisible()
                box=native.frame(int(control.winId()))
                assert panel[0]<=box[0]<box[2]<=panel[2]
                assert panel[1]<=box[1]<box[3]<=panel[3]
            checked.append({'monitor': [left, top, right, bottom], 'dpi': dpi, 'overlay': native.frame(hwnd)})
        assert checked
        import json, os
        if os.environ.get('CACHEMONITOR_OVERLAY_MONITOR_REPORT'):
            from pathlib import Path
            Path(os.environ['CACHEMONITOR_OVERLAY_MONITOR_REPORT']).write_text(json.dumps(checked, indent=2), encoding='utf-8')
        host.showMinimized(); app.processEvents(); refresh()
        assert not controller.widget.isVisible()
        host.showNormal(); app.processEvents(); refresh()
        assert controller.widget.isVisible()
        native.visible_target = lambda handle: False
        refresh(); assert not controller.widget.isVisible()
        native.visible_target = original_visible
        controller.set_enabled(False)
        assert not controller.enabled
        assert not controller.widget.isVisible()
        controller.set_enabled(True)
        assert controller.enabled
    finally:
        controller.stop(); host.close(); app.processEvents()
