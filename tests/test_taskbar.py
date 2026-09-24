from cachemonitor.screens import screen_id
import time

import sys
import pytest
from PySide6.QtCore import QRect, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from cachemonitor.dashboard import Dashboard
from cachemonitor.taskbar import TaskbarQuota


pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows taskbar integration')


def settle_position(indicator):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        indicator.sync_position()
        if indicator.isVisible() and indicator.embedding_error is None:
            return
        QTest.qWait(20)
    probe = indicator.native._clock_probe
    raise AssertionError((indicator.embedding_error, indicator.monitor_name,
                          probe.result if probe else None, probe.last_error if probe else None))


@pytest.mark.live_taskbar
def test_monitor_menu_moves_native_widget_and_restores_selection(tmp_path):
    app = QApplication.instance() or QApplication([])
    path = str(tmp_path / 'monitors.ini')
    settings = QSettings(path, QSettings.IniFormat)
    indicator = TaskbarQuota(settings)
    menu = QMenu()
    indicator.add_monitor_menu(menu)
    clicks, menus = [], []
    indicator.activated.connect(lambda: clicks.append(True))
    indicator.menu_requested.connect(lambda point: menus.append(point))
    try:
        indicator.set_display({'text': '84', 'remaining': 84}, 'weekly', '주간 잔여량 84%')
        indicator.set_enabled(True)
        for index, screen in enumerate(app.screens(), 1):
            indicator.monitor_actions[screen_id(screen)].trigger()
            app.processEvents()
            settle_position(indicator)
            native = indicator.native
            host = native.host_for_screen(screen_id(screen))
            assert host, f'No taskbar for {screen_id(screen)}'
            hwnd = int(indicator.winId())
            assert native.api.GetParent(hwnd) == host
            assert native.api.IsWindowVisible(hwnd)
            assert native.rect(host).contains(native.rect(hwnd))
            assert native.screen_name(hwnd) == screen_id(screen)
            assert indicator.embedding_error is None
            assert indicator.value.text() == '84%'
            QTest.mouseClick(indicator, Qt.LeftButton)
            from PySide6.QtGui import QContextMenuEvent
            event = QContextMenuEvent(QContextMenuEvent.Mouse, indicator.rect().center(),
                                     indicator.mapToGlobal(indicator.rect().center()))
            app.sendEvent(indicator, event)
            assert len(menus) == len(clicks) == index
            indicator.refresh_monitor_menu()
            assert indicator.monitor_actions[screen_id(screen)].isChecked()
        selected = indicator.monitor_name
        settings.sync()
    finally:
        indicator.close()
        indicator.destroy()
        menu.close()
    restored = TaskbarQuota(QSettings(path, QSettings.IniFormat))
    try:
        restored.set_enabled(True)
        settle_position(restored)
        assert restored.monitor_name == selected
        assert restored.native.screen_name(int(restored.winId())) == selected
    finally:
        restored.close()
        restored.destroy()


@pytest.mark.live_taskbar
def test_monitor_disconnect_fallback_reconnect_and_disabled_selection(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    screens = app.screens()
    if len(screens) < 2:
        pytest.skip('Requires two physical screens for reconnect simulation')
    primary = app.primaryScreen()
    secondary = next(s for s in screens if s != primary)
    settings = QSettings(str(tmp_path / 'reconnect.ini'), QSettings.IniFormat)
    indicator = TaskbarQuota(settings)
    menu = QMenu()
    indicator.add_monitor_menu(menu)
    try:
        indicator.set_monitor(screen_id(secondary))
        assert not indicator.isVisible() and not indicator.timer.isActive()
        indicator.set_enabled(True)
        settle_position(indicator)
        assert indicator.native.screen_name(int(indicator.winId())) == screen_id(secondary)
        with monkeypatch.context() as patch:
            patch.setattr(QApplication, 'screens', staticmethod(lambda: [primary]))
            indicator.sync_position()
            assert indicator.native.api.GetParent(int(indicator.winId())) == indicator.native.host()
            assert settings.value('taskbar/monitor') == screen_id(secondary)
            indicator.refresh_monitor_menu()
            missing = indicator.monitor_actions[screen_id(secondary)]
            assert missing.isChecked() and not missing.isEnabled()
        settle_position(indicator)
        assert indicator.native.screen_name(int(indicator.winId())) == screen_id(secondary)
        indicator.refresh_monitor_menu()
        indicator.monitor_actions[''].trigger()
        assert indicator.native.api.GetParent(int(indicator.winId())) == indicator.native.host()
        assert settings.value('taskbar/monitor') == ''
    finally:
        indicator.close()
        indicator.destroy()
        menu.close()


def test_taskbar_data_visibility_menu_and_persistence(tmp_path, monkeypatch, owned_taskbar):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr('cachemonitor.tray.QSystemTrayIcon.isSystemTrayAvailable', lambda: True)
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.IniFormat)
    w = Dashboard([], start_worker=False, settings=settings)
    try:
        indicator = w.taskbar_quota
        assert indicator.monitor_menu.title() == '표시할 모니터'
        assert indicator.monitor_actions[''].isChecked()
        assert indicator.isVisible()
        assert indicator.native.api.GetParent(int(indicator.winId())) == indicator.native.host()
        now = time.time()
        w.snapshot['quota'] = {'plan_type': 'pro', 'has_five_hour': False,
                               'observed_at': now, 'windows': {'weekly': {
                                   'used_percent': 16, 'resets_at': now + 100}}}
        w.refresh_tray()
        assert indicator.value.text() == '84%'
        assert '84%' in indicator.toolTip()
        w.show()
        w.close()
        assert not w.isVisible() and indicator.isVisible()
        QTest.mouseClick(indicator, Qt.LeftButton)
        assert w.isVisible()
        w.showMinimized();app.processEvents()
        QTest.mouseClick(indicator, Qt.LeftButton)
        assert w.isVisible() and not w.isMinimized()
        w.set_quota_mode('five_hour')
        assert indicator.caption.text() == '5시간' and indicator.value.text() == '?'
        w.snapshot['quota']['unlimited_windows']=['five_hour'];w.refresh_tray()
        assert indicator.value.text()=='∞'
        w.set_quota_mode('weekly')
        w.snapshot['quota']['windows']['weekly']['used_percent'] = 95
        w.refresh_tray()
        assert indicator.value.text() == '5%' and indicator._warning
        w.snapshot['quota']['windows']['weekly']['resets_at'] = now - 1
        w.snapshot['quota']['observed_at'] = now - 2  # No server observation since this reset.
        w.refresh_tray()
        assert indicator.value.text() == '?' and not indicator._warning
        w.taskbar_action.trigger()
        assert not indicator.isVisible() and not indicator.timer.isActive()
        assert settings.value('taskbar/enabled', True, type=bool) is False
    finally:
        w.quitting = True
        w.tick.stop()
        w.tray.hide()
        w.close()
        indicator.destroy()
    assert not indicator.isVisible()
    restored = Dashboard([], start_worker=False, settings=settings)
    try:
        assert not restored.taskbar_quota.isVisible()
        restored.taskbar_action.trigger()
        assert restored.taskbar_quota.isVisible()
    finally:
        restored.quitting = True
        restored.tick.stop()
        restored.tray.hide()
        restored.close()
        restored.taskbar_quota.destroy()


def test_routine_tests_cannot_attach_to_explorer(tmp_path):
    app = QApplication.instance() or QApplication([])
    indicator = TaskbarQuota(QSettings(str(tmp_path / 'isolated.ini'), QSettings.IniFormat))
    try:
        assert indicator.native.host() is None
        assert indicator.native.host_for_screen(app.primaryScreen().name()) is None
        indicator.set_enabled(True)
        assert not indicator.isVisible()
        shell = indicator.native.api.FindWindowW('Shell_TrayWnd', None)
        if shell:
            with pytest.raises(AssertionError, match='own windows'):
                indicator.native.attach(int(indicator.winId()), shell)
    finally:
        indicator.close()
        indicator.destroy()


def test_native_child_follows_parent_visibility_and_reconnects(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / 'native.ini'), QSettings.IniFormat)
    indicator = TaskbarQuota(settings)
    native = indicator.native
    import ctypes
    from ctypes import wintypes
    native.api.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    native.api.CreateWindowExW.restype = wintypes.HWND
    native.api.DestroyWindow.argtypes = [wintypes.HWND]
    def create_parent():
        return native.api.CreateWindowExW(0, 'STATIC', 'CacheMonitor test host',
            0x90000000, 100, 100, 600, 48, None, None, None, None)
    host = create_parent()
    assert host
    monkeypatch.setattr(native, 'host', lambda: host)
    monkeypatch.setattr(native, 'dock_geometry', lambda hwnd, ratio: QRect(450, 6, 112, 36))
    try:
        indicator.set_enabled(True)
        hwnd = int(indicator.winId())
        assert native.api.GetParent(hwnd) == host
        assert native.api.GetWindowLongW(hwnd, -16) & 0x40000000  # WS_CHILD
        assert not native.api.GetWindowLongW(hwnd, -20) & 8  # Not WS_EX_TOPMOST
        assert native.api.IsWindowVisible(hwnd)
        native.api.ShowWindow(host, 0)
        assert not native.api.IsWindowVisible(hwnd)
        native.api.ShowWindow(host, 4)
        assert native.api.IsWindowVisible(hwnd)
        native.api.DestroyWindow(host)
        app.processEvents()
        host = create_parent()
        indicator.sync_position()
        assert native.api.GetParent(int(indicator.winId())) == host
        assert native.api.IsWindowVisible(int(indicator.winId()))
    finally:
        indicator.close()
        indicator.destroy()
        native.api.DestroyWindow(host)
