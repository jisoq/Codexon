"""A single native menu bar item backed by the application's existing actions."""
from PySide6.QtCore import QObject, QBuffer, QIODevice, QTimer, Signal
from PySide6.QtGui import QAction


from .macos_notifications import notification_status, request_notifications, show_notification


_menu_delegate_class = None


def menu_delegate_class():
    global _menu_delegate_class
    if _menu_delegate_class is None:
        import AppKit
        class CodexonMenuDelegate(AppKit.NSObject):
            def menuNeedsUpdate_(self, menu):
                owner=getattr(self,'owner',None)
                if owner is not None:
                    try:
                        owner.rebuild_menu(menu)
                    except Exception:
                        # Qt teardown may race a queued native menu callback.
                        # Never let a Python exception unwind through AppKit.
                        pass

            def invoke_(self, sender):
                owner=getattr(self,'owner',None)
                if owner is not None:
                    try:
                        action = owner.actions.get(int(sender.tag()))
                        if action is not None and action.isEnabled():
                            QTimer.singleShot(0, action.trigger)
                    except Exception:
                        pass
        _menu_delegate_class = CodexonMenuDelegate
    return _menu_delegate_class


class MacStatusTray(QObject):
    """QSystemTrayIcon-compatible surface, with a real text menu bar item."""
    activated = Signal(object)

    @staticmethod
    def isSystemTrayAvailable():
        return True

    def __init__(self, icon, parent=None, *, autosave_name='CodexonUsage'):
        super().__init__(parent)
        import AppKit
        self.appkit = AppKit
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        if autosave_name:
            self.item.setAutosaveName_(autosave_name)
        self.delegate = menu_delegate_class().alloc().init()
        self.delegate.owner = self
        self.menu = AppKit.NSMenu.alloc().initWithTitle_('Codexon')
        self.menu.setAutoenablesItems_(False)
        self.menu.setDelegate_(self.delegate)
        self.item.setMenu_(self.menu)
        self.qt_menu = None
        self.actions = {}
        self.native_menus = {}
        self.image = None
        self.setIcon(icon)
        self.setQuota(None, False)

    def setContextMenu(self, menu):
        self.qt_menu = menu

    def contextMenu(self):
        return self.qt_menu

    def rebuild_menu(self, native_menu):
        if native_menu != self.menu or self.qt_menu is None:
            return
        self.actions = {}
        self.native_menus = {}
        self._copy_menu(self.qt_menu, self.menu)

    def _copy_menu(self, source, target):
        a = self.appkit
        source.aboutToShow.emit()
        target.removeAllItems()
        for action in source.actions():
            if not action.isVisible():
                continue
            if action.isSeparator():
                target.addItem_(a.NSMenuItem.separatorItem())
                continue
            title = action.text().replace('&&', '\0').replace('&', '').replace('\0', '&')
            item = a.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, 'invoke:', '')
            item.setTarget_(self.delegate)
            tag = len(self.actions)+1
            self.actions[tag] = action
            item.setTag_(tag)
            item.setEnabled_(action.isEnabled())
            if action.isCheckable():
                item.setState_(a.NSControlStateValueOn if action.isChecked() else a.NSControlStateValueOff)
            if action.menu() is not None:
                submenu = a.NSMenu.alloc().initWithTitle_(title)
                submenu.setAutoenablesItems_(False)
                self._copy_menu(action.menu(), submenu)
                item.setSubmenu_(submenu)
            target.addItem_(item)

    def setIcon(self, icon):
        from Foundation import NSData
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        icon.pixmap(36, 36).save(buffer, 'PNG')
        data = bytes(buffer.data())
        self.image = self.appkit.NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(data, len(data)))
        if self.image is not None:
            self.image.setSize_((18, 18))
            self.item.button().setImage_(self.image)

    def setQuota(self, display, enabled=True, mode='weekly'):
        button = self.item.button()
        if enabled and display:
            prefix = '주간' if mode == 'weekly' else '5시간'
            from .i18n import tr
            value = display['text']+('%' if display.get('remaining') is not None else '')
            button.setTitle_(f' {tr(prefix)} {value}')
            button.setAccessibilityLabel_(f'Codexon {tr(prefix)} {value}')
        else:
            button.setTitle_('')
            button.setAccessibilityLabel_('Codexon')

    def setToolTip(self, text):
        self.item.button().setToolTip_(str(text))

    def show(self):
        if self.item is not None:
            self.item.setVisible_(True)

    def hide(self):
        if self.item is not None:
            self.item.setVisible_(False)

    def showMessage(self, title, message, *args):
        return show_notification(title, message)

    def close(self):
        if self.item is not None:
            self.item.setMenu_(None)
            self.menu.setDelegate_(None)
            self.delegate.owner = None
            self.appkit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
            self.item = None


class MacQuotaIndicator(QObject):
    """The former taskbar option controls text on the one status item."""
    activated = Signal()

    def __init__(self, settings, tray, parent=None):
        super().__init__(parent)
        self.settings, self.tray = settings, tray
        self.enabled = False
        self.native = None
        self.embedding_error = None
        self.monitor_name = ''
        self.monitor_actions = {}
        self.display = None
        self.mode = 'weekly'

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self._update()

    def set_display(self, display, mode, tooltip):
        self.display, self.mode = display, mode
        self._update()

    def _update(self):
        if hasattr(self.tray, 'setQuota'):
            self.tray.setQuota(self.display, self.enabled, self.mode)

    def add_monitor_menu(self, menu):
        self.monitor_menu = menu.addMenu('메뉴 막대 위치는 macOS가 관리합니다')
        self.monitor_menu.menuAction().setVisible(False)
        self.refresh_monitor_menu()

    def refresh_monitor_menu(self):
        action = QAction('메뉴 막대 위치는 macOS가 관리합니다', self)
        action.setEnabled(False)
        self.monitor_actions = {'': action}

    def set_monitor(self, name):
        pass

    def close(self):
        if hasattr(self.tray, 'close'):
            self.tray.close()


class NotificationPermission(QObject):
    """Marshal native background completion blocks back onto the Qt thread."""
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = 'not_determined'
        self.changed.connect(self._received)

    def _received(self, status):
        self.status = status

    def refresh(self):
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None and (app.platformName() != 'cocoa' or app.property('cachemonitorDisableShellIntegration')):
            self.changed.emit('unavailable')
            return
        notification_status(self.changed.emit)

    def request(self):
        request_notifications(lambda allowed: self.changed.emit('authorized' if allowed else 'denied'))
