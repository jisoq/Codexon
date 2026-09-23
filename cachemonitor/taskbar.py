"""A quota widget embedded as a native child of a selected Windows taskbar."""
from __future__ import annotations

from PySide6.QtGui import QActionGroup
import sys

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication
from .screens import screen_id
from .presentation import Node, Text
from .quick_runtime import QuickHost
from .brand_icon import brand_pixmap


def taskbar_rect():
    """Convert the native primary taskbar rectangle to Qt logical coordinates."""
    if sys.platform != 'win32':
        return None
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD)]

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
    hwnd = user32.FindWindowW('Shell_TrayWnd', None)
    rect = wintypes.RECT()
    if not hwnd or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    info = MonitorInfo()
    info.size = ctypes.sizeof(info)
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info)):
        return None
    screen = QApplication.primaryScreen()
    if screen is None:
        return None
    ratio = screen.devicePixelRatio()
    origin = screen.geometry().topLeft()
    return QRect(origin.x() + round((rect.left - info.monitor.left) / ratio),
                 origin.y() + round((rect.top - info.monitor.top) / ratio),
                 round((rect.right - rect.left) / ratio),
                 round((rect.bottom - rect.top) / ratio))


class NativeTaskbar:
    """Only reparents our own HWND; never changes Explorer's windows or styles."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.types = ctypes, wintypes
        self._clock_probe = None
        self.api = ctypes.WinDLL('user32', use_last_error=True)
        class MonitorInfo(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                        ('work', wintypes.RECT), ('flags', wintypes.DWORD),
                        ('device', wintypes.WCHAR * 32)]
        self.MonitorInfo = MonitorInfo
        signatures = {
            'FindWindowW': ([wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
            'FindWindowExW': ([wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
            'GetParent': ([wintypes.HWND], wintypes.HWND),
            'SetParent': ([wintypes.HWND, wintypes.HWND], wintypes.HWND),
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'IsWindowVisible': ([wintypes.HWND], wintypes.BOOL),
            'GetWindowRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            'GetClientRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            'ScreenToClient': ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            'MonitorFromWindow': ([wintypes.HWND, wintypes.DWORD], wintypes.HMONITOR),
            'GetMonitorInfoW': ([wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)], wintypes.BOOL),
            'GetWindowLongW': ([wintypes.HWND, ctypes.c_int], wintypes.LONG),
            'SetWindowLongW': ([wintypes.HWND, ctypes.c_int, wintypes.LONG], wintypes.LONG),
            'ShowWindow': ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            'SetWindowPos': ([wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result

    def host(self):
        return self.api.FindWindowW('Shell_TrayWnd', None)

    def screen_name(self, hwnd):
        info = self.MonitorInfo()
        info.size = self.ctypes.sizeof(info)
        if self.api.GetMonitorInfoW(self.api.MonitorFromWindow(hwnd, 2), self.ctypes.byref(info)):
            return info.device
        return ''

    def host_for_screen(self, name):
        primary = self.host()
        if primary and self.screen_name(primary) == name:
            return primary
        previous = None
        while True:
            previous = self.api.FindWindowExW(None, previous, 'Shell_SecondaryTrayWnd', None)
            if not previous:
                return None
            if self.screen_name(previous) == name:
                return previous

    def rect(self, hwnd, client=False):
        rect = self.types.RECT()
        function = self.api.GetClientRect if client else self.api.GetWindowRect
        if not function(hwnd, self.ctypes.byref(rect)):
            return None
        return QRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def clock_rect(self, host):
        # Older taskbars expose the clock as a native child window.
        clock = self.api.FindWindowExW(host, None, 'TrayClockWClass', None)
        if clock:
            return self.rect(clock)
        from .taskbar_clock import ClockProbe
        bar = self.rect(host)
        if bar is None:
            return None
        if self._clock_probe is None:
            self._clock_probe = ClockProbe()
        # Query Explorer's subtree only, never our Qt child windows. This also
        # avoids accessibility re-entry into the GUI during a placement update.
        bridge = self.api.FindWindowExW(host, None, 'Windows.UI.Composition.DesktopWindowContentBridge', None)
        bounds = self._clock_probe.get(bridge or host, bar.getRect())
        rect = QRect(*bounds) if bounds else None
        # UIA coordinates are physical pixels, including negative screen origins.
        return rect if rect and bar.contains(rect) and rect.center().x() > bar.center().x() else None

    def close(self):
        if self._clock_probe is not None:
            self._clock_probe.close()
            self._clock_probe = None

    def dock_geometry(self, host, ratio):
        bar = self.rect(host, client=True)
        if bar is None or bar.width() < bar.height():
            return None  # This integration targets the horizontal Windows 11 taskbar.
        tray = self.api.FindWindowExW(host, None, 'TrayNotifyWnd', None)
        tray_rect = self.rect(tray) if tray else None
        secondary = host != self.host()
        if tray_rect is not None:
            point = self.types.POINT(tray_rect.x(), tray_rect.y())
            if not self.api.ScreenToClient(host, self.ctypes.byref(point)):
                return None
            right = point.x
        elif secondary:
            clock_rect = self.clock_rect(host)
            if clock_rect is None:
                return None
            point = self.types.POINT(clock_rect.x(), clock_rect.y())
            if not self.api.ScreenToClient(host, self.ctypes.byref(point)):
                return None
            right = point.x
        else:
            return None
        width, height = round(100 * ratio), min(round(36 * ratio), bar.height())
        gap = round((8 + (0 if secondary else right_widgets_width())) * ratio)
        x = right - gap - width
        if x < 0:
            return None
        result = QRect(x, (bar.height() - height) // 2, width, height)
        # Avoid covering app buttons when the taskbar is crowded.
        for class_name in ('Start', 'ReBarWindow32', 'WorkerW'):
            child = self.api.FindWindowExW(host, None, class_name, None)
            child_rect = self.rect(child) if child else None
            if child_rect and not child_rect.isEmpty():
                point = self.types.POINT(child_rect.x(), child_rect.y())
                if not self.api.ScreenToClient(host, self.ctypes.byref(point)):
                    return None
                if result.intersects(QRect(point.x, point.y, child_rect.width(), child_rect.height())):
                    return None
        return result

    def attach(self, hwnd, host):
        if self.api.GetParent(hwnd) == host:
            return True
        self.api.ShowWindow(hwnd, 0)
        style = self.api.GetWindowLongW(hwnd, -16)
        # SetParent does not adjust WS_CHILD / WS_POPUP itself.
        self.api.SetWindowLongW(hwnd, -16, (style & ~0x80000000) | 0x40000000)
        exstyle = self.api.GetWindowLongW(hwnd, -20)
        self.api.SetWindowLongW(hwnd, -20, (exstyle & ~0x00040008) | 0x08000080)
        self.api.SetParent(hwnd, host)
        return self.api.GetParent(hwnd) == host

    def position(self, hwnd, rect):
        # HWND_TOP orders siblings within Explorer, not above other applications.
        return bool(self.api.SetWindowPos(hwnd, 0, *rect.getRect(), 0x0070))


def right_widgets_width():
    """Windows 11 places weather before the tray when taskbar buttons align left."""
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced') as key:
                aligned_left = winreg.QueryValueEx(key, 'TaskbarAl')[0] == 0
                widgets = bool(winreg.QueryValueEx(key, 'TaskbarDa')[0])
                if aligned_left and widgets:
                    return 160
        except OSError:
            pass
    return 0


def dark_taskbar():
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
                return not bool(winreg.QueryValueEx(key, 'SystemUsesLightTheme')[0])
        except OSError:
            pass
    return False


class TaskbarQuota(QuickHost):
    activated = Signal()
    menu_requested = Signal(QPoint)

    def __init__(self, settings, activation_hint='클릭: 대시보드'):
        # Qt owns the widget; its native window belongs to Explorer's taskbar.
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint |
                         Qt.WindowDoesNotAcceptFocus)
        self.settings = settings
        self.monitor_name = settings.value('taskbar/monitor', '', type=str)
        self.activation_hint = activation_hint
        from .i18n import tr
        self.setWindowTitle('Codexon · '+tr('작업표시줄 잔여량'))
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.PointingHandCursor if activation_hint else Qt.ArrowCursor)
        self.enabled = False
        # Smoke runs must not join Explorer's input queue via cross-process
        # SetParent. Set this process-local flag before constructing any widget.
        shell_disabled = bool(QApplication.instance().property('cachemonitorDisableShellIntegration'))
        self.native = NativeTaskbar() if sys.platform == 'win32' and not shell_disabled else None
        self._native_id = None
        self.embedding_error = None
        self._warning = False
        self._theme = None
        self.caption=Text('주간');self.value=Text('?')
        self.view=Node();self.view.put(logo='',caption='주간',value='?',foreground='#202020',valueColor='#202020')
        self.set_scene(self.view,'Taskbar.qml',transparent=True)
        self.quick.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.sync_position)

    def add_monitor_menu(self, menu):
        self.monitor_menu = menu.addMenu('표시할 모니터')
        self.monitor_group = QActionGroup(self.monitor_menu)
        self.monitor_group.setExclusive(True)
        self.monitor_menu.aboutToShow.connect(self.refresh_monitor_menu)
        self.refresh_monitor_menu()
        return self.monitor_menu

    def refresh_monitor_menu(self):
        self.monitor_menu.clear()
        self.monitor_actions = {}
        screens = QApplication.screens()
        choices = [('', '주 모니터 자동 선택', True)]
        for screen in screens:
            size, ratio = screen.size(), screen.devicePixelRatio()
            label = (f'{screen_id(screen)} · {round(size.width() * ratio)} × '
                     f'{round(size.height() * ratio)}'
                     + (' · 주 모니터' if screen == QApplication.primaryScreen() else ''))
            choices.append((screen_id(screen), label, True))
        if self.monitor_name and self.monitor_name not in {screen_id(s) for s in screens}:
            choices.append((self.monitor_name, f'{self.monitor_name} · 연결 끊김 (주 모니터에 임시 표시)', False))
        for name, label, enabled in choices:
            action = self.monitor_menu.addAction(label)
            action.setCheckable(True)
            self.monitor_group.addAction(action)
            action.setChecked(name == self.monitor_name)
            action.setEnabled(enabled)
            action.triggered.connect(lambda checked, selected=name: self.set_monitor(selected))
            self.monitor_actions[name] = action

    def set_monitor(self, name):
        self.monitor_name = name
        self.settings.setValue('taskbar/monitor', name)
        self.sync_position()

    def target_screen(self):
        return next((s for s in QApplication.screens() if screen_id(s) == self.monitor_name),
                    QApplication.primaryScreen())

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        if self.enabled:
            self.sync_position()
            self.timer.start()
        else:
            self.timer.stop()
            self.hide()

    def set_display(self, display, mode, tooltip):
        self.caption.setText('주간' if mode == 'weekly' else '5시간')
        self.value.setText(display['text'] + ('%' if display['remaining'] is not None else ''))
        self.view.put(caption=self.caption.text(),value=self.value.text())
        self._warning = display['remaining'] is not None and display['remaining'] < 10
        self.setToolTip(tooltip + ('\n' + self.activation_hint if self.activation_hint else ''))
        self.setAccessibleName(f'{self.caption.text()} 잔여량 {self.value.text()}')
        self.update_theme()

    def update_theme(self):
        theme = (dark_taskbar(), self._warning, self.devicePixelRatioF())
        if theme == self._theme:
            return
        self._theme = theme
        dark, warning, ratio = theme
        foreground = '#ffffff' if dark else '#202020'
        value_color = ('#ffb4a8' if dark else '#b42318') if warning else foreground
        from PySide6.QtCore import QBuffer, QIODevice
        buffer=QBuffer();buffer.open(QIODevice.WriteOnly)
        brand_pixmap(dark,ratio).save(buffer,'PNG')
        logo='data:image/png;base64,'+bytes(buffer.data().toBase64()).decode('ascii')
        self.view.put(foreground=foreground,valueColor=value_color,logo=logo)

    def sync_position(self):
        if not self.enabled or self.native is None:
            return
        screen = self.target_screen()
        if screen is None:
            self.hide()
            return
        host = (self.native.host() if screen == QApplication.primaryScreen()
                else self.native.host_for_screen(screen_id(screen)))
        # A connected display may have no taskbar (Windows multi-display setting).
        if not host and screen != QApplication.primaryScreen():
            screen = QApplication.primaryScreen()
            host = self.native.host()
        if not host:
            self.embedding_error = '표시할 작업표시줄을 찾을 수 없습니다'
            self.hide()
            return
        if self._native_id and not self.native.api.IsWindow(self._native_id):
            # Explorer's old child HWND can be destroyed during a shell restart.
            self.destroy()
            self._native_id = None
        ratio = screen.devicePixelRatio()
        rect = self.native.dock_geometry(host, ratio)
        if rect is None:
            self.embedding_error = '작업표시줄 위젯 위치를 확인할 수 없습니다'
            self.hide()
            return
        self.winId()
        if self.windowHandle().screen() != screen:
            self.windowHandle().setScreen(screen)
        self.resize(round(rect.width() / ratio), round(rect.height() / ratio))
        self.update_theme()
        hwnd = int(self.winId())
        self._native_id = hwnd
        if not self.native.attach(hwnd, host):
            self.embedding_error = '작업표시줄에 위젯을 연결할 수 없습니다'
            self.hide()
            return
        self.show()
        if self.native.position(hwnd, rect):
            self.embedding_error = None
        else:
            self.embedding_error = '작업표시줄 위젯 배치 실패'
            self.hide()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.activated.emit()
            event.accept()

    def contextMenuEvent(self, event):
        self.menu_requested.emit(event.globalPos())
        event.accept()

    def closeEvent(self, event):
        self.timer.stop()
        if self.native is not None:
            self.native.close()
        self.release_scene()
        super().closeEvent(event)
