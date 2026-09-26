"""Native Mac menus and reopen behavior with the same graceful shutdown path."""
from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenuBar

from .i18n import tr


_reopen_delegate_class = None


def reopen_delegate_class():
    """Forward Qt's application delegate intact, handling only Dock reopen."""
    global _reopen_delegate_class
    if _reopen_delegate_class is None:
        import AppKit
        import objc
        class CodexonApplicationDelegate(AppKit.NSObject):
            def respondsToSelector_(self, selector):
                original=getattr(self,'original',None)
                return bool(objc.super(CodexonApplicationDelegate, self).respondsToSelector_(selector)
                            or original and original.respondsToSelector_(selector))

            def forwardingTargetForSelector_(self, selector):
                original=getattr(self,'original',None)
                if original and original.respondsToSelector_(selector):
                    return original
                return objc.super(CodexonApplicationDelegate, self).forwardingTargetForSelector_(selector)

            def applicationShouldHandleReopen_hasVisibleWindows_(self, application, visible):
                owner=getattr(self,'owner',None)
                if owner is not None:
                    owner.reopen()
                return False

            def applicationShouldTerminate_(self, application):
                owner=getattr(self,'owner',None)
                if owner is not None and not owner.window.quitting:
                    owner.queue_quit()
                    return AppKit.NSTerminateCancel
                original=getattr(self,'original',None)
                if original and original.respondsToSelector_('applicationShouldTerminate:'):
                    return original.applicationShouldTerminate_(application)
                return AppKit.NSTerminateNow
        _reopen_delegate_class = CodexonApplicationDelegate
    return _reopen_delegate_class


class MacApplicationLifecycle(QObject):
    def __init__(self, app, window, *, native=True):
        super().__init__(app)
        self.app, self.window = app, window
        self.queued = False
        self.native_app = self.delegate = self.original_delegate = None
        self.menu_bar = QMenuBar()
        self.menu_bar.setNativeMenuBar(True)
        menu = self.menu_bar.addMenu('Codexon')
        self.preferences = QAction(tr('설정')+'…', self)
        self.preferences.setMenuRole(QAction.PreferencesRole)
        self.preferences.setShortcut(QKeySequence(QKeySequence.Preferences))
        self.preferences.triggered.connect(self.open_settings)
        self.quit = QAction(tr('종료'), self)
        self.quit.setMenuRole(QAction.QuitRole)
        self.quit.setShortcut(QKeySequence(QKeySequence.Quit))
        self.quit.triggered.connect(window.quit_app)
        menu.addAction(self.preferences);menu.addAction(self.quit)
        window.addAction(self.preferences);window.addAction(self.quit)
        app.installEventFilter(self)
        if native and app.platformName() == 'cocoa':
            import AppKit
            self.native_app = AppKit.NSApplication.sharedApplication()
            self.original_delegate = self.native_app.delegate()
            self.delegate = reopen_delegate_class().alloc().init()
            self.delegate.original = self.original_delegate
            self.delegate.owner = self
            self.native_app.setDelegate_(self.delegate)
        app.aboutToQuit.connect(self.close)

    def eventFilter(self, watched, event):
        if watched is self.app and event.type() == QEvent.Quit and not self.window.quitting:
            event.ignore()
            self.queue_quit()
            return True
        return super().eventFilter(watched, event)

    def queue_quit(self):
        if not self.queued:
            self.queued = True
            QTimer.singleShot(0, self.request_quit)

    def request_quit(self):
        self.queued = False
        if not self.window.quitting:
            self.window.quit_app()

    def open_settings(self):
        if not getattr(self.window, '_closing', False):
            self.window.open_settings()
            self.window.show_window()

    def reopen(self):
        if not self.window.quitting and not getattr(self.window, '_closing', False):
            QTimer.singleShot(0, self.window.show_window)

    def close(self):
        self.app.removeEventFilter(self)
        if self.native_app and self.native_app.delegate() == self.delegate:
            self.native_app.setDelegate_(self.original_delegate)
        if self.delegate:
            self.delegate.owner = None
        self.menu_bar.close()


def install(app, window, *, native=True):
    # Store lifetime explicitly: the native delegate must never outlive its Qt owner.
    window.macos_lifecycle = MacApplicationLifecycle(app, window, native=native)
    return window.macos_lifecycle
