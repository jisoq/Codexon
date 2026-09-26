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


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows enumeration callback')
@pytest.mark.parametrize('styles, expected', [
    ([0x240100], [1]),                         # Main window only.
    ([0x280088, 0x240100], [2]),               # Pet before main in Z order.
    ([0x240100, 0x2800a8], [1]),               # Click-through Mini after main.
    ([0x280088], []),                         # No main window.
    ([0x240100, 0x280088, 0x240100], [1, 3]),  # Two real main windows stay ambiguous.
    ([0x240108, 0x280088], [1]),               # Topmost main is still eligible.
])
def test_tool_windows_do_not_count_as_primary_targets(styles, expected, monkeypatch, tmp_path):
    from ctypes import wintypes as W
    from cachemonitor.overlay_windows import WindowsOverlay, observe_selection

    class API:
        @staticmethod
        def EnumWindows(visit, context):
            for hwnd in range(1, len(styles)+1):
                assert visit(hwnd, context)
        def IsWindowVisible(self, hwnd): return True
        def GetClassNameW(self, hwnd, buffer, size): buffer.value = 'Chrome_WidgetWin_1'
        def GetWindowLongPtrW(self, hwnd, index):
            assert index == -20
            return styles[hwnd-1]
        def GetWindowThreadProcessId(self, hwnd, pointer):
            ctypes.cast(pointer, ctypes.POINTER(W.DWORD)).contents.value = 42
        def GetWindowTextW(self, hwnd, buffer, size): buffer.value = 'ChatGPT'
        def OpenProcess(self, *args): return 42
        def QueryFullProcessImageNameW(self, process, flags, buffer, size):
            buffer.value = r'C:\Program Files\WindowsApps\OpenAI.Codex_26.924.2738.0_x64__fixture\app\ChatGPT.exe'
            return True
        def CloseHandle(self, process): pass

    selection = Selection('11111111-1111-1111-1111-111111111111')
    class Log:
        def poll(self, pid, now):
            assert pid == 42
            return selection

    native = WindowsOverlay.__new__(WindowsOverlay)
    native.u = native.k = API()
    native._navigation_log = Log()
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    targets = native.targets()
    assert [target['hwnd'] for target in targets] == expected
    observed = observe_selection(targets, native._navigation_log, 0)
    wanted = selection if len(expected) == 1 else None
    assert observed['selection'] == wanted
    candidate = dict(hwnd=expected[0] if expected else 1, pid=42)
    assert native.confirm_selection(candidate) == wanted


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
def test_native_bottom_right_move_resize_minimize_and_toggle(tmp_path):
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
        refresh()
        assert controller.widget.isVisible()
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
