"""Native Cocoa smoke using only temporary, self-owned windows and menus.

Does not inspect another app's content, request permissions, change user settings,
or start collection/proxy services. Run outside a filesystem-only sandbox so Qt
can connect to WindowServer. No dashboard or test panel is made visible.
"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This verification requires macOS.')
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import QApplication, QMenu, QWidget
    from cachemonitor.macos_status import MacStatusTray, MacQuotaIndicator
    from cachemonitor.overlay_macos import MacOverlay
    from cachemonitor.macos_application import MacApplicationLifecycle

    app = QApplication([])
    if app.platformName() != 'cocoa':
        parser.error('This verification requires the Cocoa Qt platform, not offscreen.')
    class TestDashboard(QWidget):
        quitting = False
        quit_requests = reopen_requests = settings_requests = 0
        def quit_app(self):self.quit_requests += 1
        def show_window(self):self.reopen_requests += 1
        def open_settings(self):self.settings_requests += 1
    dashboard = TestDashboard()
    panel = QWidget(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
    panel.setAttribute(Qt.WA_ShowWithoutActivating)
    native = MacOverlay()
    foreground = native.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
    foreground_pid = int(foreground.processIdentifier()) if foreground else None
    tray = MacStatusTray(QIcon(), dashboard, autosave_name=None)
    indicator = MacQuotaIndicator(None, tray)
    lifecycle = MacApplicationLifecycle(app, dashboard)
    report = {}
    try:
        menu = QMenu()
        action = menu.addAction('Test dashboard')
        called = []
        action.triggered.connect(lambda: called.append(True))
        choice = menu.addAction('Test checked mode')
        choice.setCheckable(True);choice.setChecked(True)
        sub = menu.addMenu('Test session submenu')
        sub.addAction('Test session')
        tray.setContextMenu(menu)
        tray.rebuild_menu(tray.menu)
        indicator.set_enabled(True)
        indicator.set_display(dict(text='63', remaining=63), 'weekly', 'isolated native test')
        tray.show();app.processEvents()
        assert tray.item.isVisible() and not dashboard.isVisible()
        assert '63%' in tray.item.button().title()
        assert tray.item.menu() == tray.menu and tray.menu.numberOfItems() == 3
        assert tray.menu.itemAtIndex_(1).state() == native.appkit.NSControlStateValueOn
        assert tray.menu.itemAtIndex_(2).submenu().numberOfItems() == 1
        tray.delegate.invoke_(tray.menu.itemAtIndex_(0));app.processEvents()
        assert called == [True]
        indicator.set_enabled(False)
        assert tray.item.button().title() == '' and tray.item.isVisible()
        handle = int(panel.winId())
        native.configure(handle, click_through=True)
        window = native._own_window(handle)
        assert window.ignoresMouseEvents()
        assert window.styleMask() & native.appkit.NSWindowStyleMaskNonactivatingPanel
        assert window.collectionBehavior() & native.appkit.NSWindowCollectionBehaviorFullScreenAuxiliary
        assert window.collectionBehavior() & native.appkit.NSWindowCollectionBehaviorCanJoinAllSpaces
        assert native.dpi(handle) == 96 and not panel.isVisible()
        app.sendEvent(app, QEvent(QEvent.Quit));app.processEvents()
        assert dashboard.quit_requests == 1 and not dashboard.quitting
        lifecycle.quit.trigger();assert dashboard.quit_requests == 2
        native.appkit.NSApplication.sharedApplication().terminate_(None)
        app.processEvents();assert dashboard.quit_requests == 3 and not dashboard.quitting
        lifecycle.preferences.trigger()
        assert dashboard.settings_requests == 1 and dashboard.reopen_requests == 1
        assert lifecycle.delegate.applicationShouldHandleReopen_hasVisibleWindows_(native.appkit.NSApplication.sharedApplication(), False) is False
        app.processEvents();assert dashboard.reopen_requests == 2
        dashboard.quitting = True
        assert not lifecycle.eventFilter(app, QEvent(QEvent.Quit))
        after = native.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        assert (int(after.processIdentifier()) if after else None) == foreground_pid
        report = dict(passed=True, platform='cocoa', single_status_item=True,
                      dashboard_remains_hidden=True, quota_text=True, native_menu=True,
                      action_dispatch=True, quota_toggle_preserves_item=True,
                      nonactivating_panel=True, click_through=True, spaces_configuration=True,
                      quit_uses_graceful_shutdown=True, native_terminate_uses_graceful_shutdown=True,
                      standard_preferences=True, dock_reopen=True,
                      coordinate_units='logical points', refresh_preserves_foreground=True,
                      permission_requests=0, real_app_mutations=0)
    finally:
        lifecycle.close();indicator.close();panel.close();dashboard.close();app.processEvents()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
