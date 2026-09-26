"""Quit shortcuts and native quit requests must drain the same services."""
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QWidget

from cachemonitor.macos_application import MacApplicationLifecycle


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.quitting = False
        self.quit_requests = self.reopens = self.settings_requests = 0
    def quit_app(self):self.quit_requests += 1
    def show_window(self):self.reopens += 1
    def open_settings(self):self.settings_requests += 1


def test_quit_event_waits_for_graceful_completion_and_can_be_retried():
    app = QApplication.instance() or QApplication([])
    window = Window();lifecycle = MacApplicationLifecycle(app, window, native=False)
    try:
        assert lifecycle.eventFilter(app, QEvent(QEvent.Quit))
        assert lifecycle.eventFilter(app, QEvent(QEvent.Quit))
        app.processEvents()
        assert window.quit_requests == 1 and not window.quitting
        assert lifecycle.eventFilter(app, QEvent(QEvent.Quit))
        app.processEvents();assert window.quit_requests == 2
        window.quitting = True
        assert not lifecycle.eventFilter(app, QEvent(QEvent.Quit))
    finally:lifecycle.close();window.close()


def test_standard_menu_actions_and_dock_reopen_share_existing_window_paths():
    app = QApplication.instance() or QApplication([])
    window = Window();lifecycle = MacApplicationLifecycle(app, window, native=False)
    try:
        lifecycle.quit.trigger();assert window.quit_requests == 1
        lifecycle.preferences.trigger()
        assert window.settings_requests == 1 and window.reopens == 1
        lifecycle.reopen();app.processEvents();assert window.reopens == 2
        window._closing = True
        lifecycle.reopen();lifecycle.preferences.trigger();app.processEvents()
        assert window.reopens == 2 and window.settings_requests == 1
    finally:lifecycle.close();window.close()
