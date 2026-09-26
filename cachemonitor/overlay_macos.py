"""macOS window integration. Observe other apps; only change our own panels.

All bounds use Qt/Quartz screen points (top-left origin), never Retina pixels.
No method requests Accessibility or Screen Recording permission. Accessibility
notifications improve tracking when already allowed; Quartz remains the fallback.
"""
from pathlib import Path
import os
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QWidget

from .overlay_tracking import RouteLog


CODEX_BUNDLE_IDS = frozenset(('com.openai.codex',))
MANUAL_WINDOW = 0


def window_targets(windows, applications):
    """Use bundle identity and public bounds, without reading window titles.

    Codex can be installed as ChatGPT.app. Its bundle ID, not its filename or
    localized application name, identifies the process. Ambiguous windows stay
    separate so the selection tracker can fail closed.
    """
    processes = {int(app['pid']): app for app in applications
                 if app.get('bundle_id') in CODEX_BUNDLE_IDS}
    targets = []
    for window in windows:
        pid = int(window.get('kCGWindowOwnerPID', 0))
        if pid not in processes or int(window.get('kCGWindowLayer', 0)) != 0:
            continue
        bounds = window.get('kCGWindowBounds', {})
        try:
            x, y, width, height = (float(bounds[key]) for key in ('X', 'Y', 'Width', 'Height'))
            handle = int(window['kCGWindowNumber'])
        except (KeyError, TypeError, ValueError):
            continue
        if width < 240 or height < 160 or not window.get('kCGWindowIsOnscreen', True):
            continue
        targets.append(dict(hwnd=handle, pid=pid, version=processes[pid].get('version', ''),
                            frame=(round(x), round(y), round(x+width), round(y+height))))
    return targets


def selection_state(targets, log, now):
    if len(targets) != 1:
        return dict(target=None, selection=None,
                    issue='Codex 창이 여러 개입니다. 메뉴에서 표시할 세션을 선택하세요.' if targets
                    else 'Codex 창을 기다립니다. 메뉴에서 세션을 선택해 독립 패널을 열 수 있습니다.')
    target = targets[0]
    selection = log.poll(target['pid'], now)
    issue = '' if selection and selection.thread_id else '현재 세션을 확인 중입니다. 메뉴에서 세션을 선택할 수 있습니다.'
    return dict(target=target, selection=selection, issue=issue)


class MacOverlay:
    """AppKit panel adapter with a permission-free Qt standalone fallback."""
    def __init__(self):
        self.companions = set()
        self._windows = {}
        self._widgets = {}
        self._placed = set()
        self._targets = {}
        self.screen_name = ''
        self.issue = ''
        self.appkit = self.quartz = self.objc = self.accessibility = None
        if QApplication.instance() and QApplication.platformName() != 'cocoa':
            self.issue = '창 연동을 사용할 수 없습니다. 메뉴에서 세션을 선택해 독립 패널을 여세요.'
            return
        try:
            import AppKit
            import Quartz
            import objc
            import ApplicationServices
            self.appkit, self.quartz, self.objc, self.accessibility = AppKit, Quartz, objc, ApplicationServices
        except ImportError:
            self.issue = '창 연동을 사용할 수 없습니다. 메뉴에서 세션을 선택해 독립 패널을 여세요.'

    def applications(self):
        if self.appkit is None:
            return []
        result = []
        for app in self.appkit.NSWorkspace.sharedWorkspace().runningApplications():
            bundle = str(app.bundleIdentifier() or '')
            if bundle in CODEX_BUNDLE_IDS:
                result.append(dict(pid=int(app.processIdentifier()), bundle_id=bundle))
        return result

    def targets(self):
        if self.quartz is None:
            return []
        q = self.quartz
        windows = q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly | q.kCGWindowListExcludeDesktopElements,
                                             q.kCGNullWindowID) or []
        targets = window_targets(windows, self.applications())
        self._targets = {target['hwnd']: target for target in targets}
        return targets

    def dpi(self, handle):
        # Cocoa and Quartz bounds are points, as are Qt's macOS geometries.
        return 96

    def frame(self, handle):
        if handle == MANUAL_WINDOW:
            from .screens import screen_id
            screen = next((s for s in QApplication.screens() if screen_id(s) == self.screen_name),
                          QApplication.primaryScreen())
            if screen is None:
                return None
            rect = screen.availableGeometry()
            return rect.x(), rect.y(), rect.x()+rect.width(), rect.y()+rect.height()
        target = self._targets.get(handle)
        return target['frame'] if target else None

    def _own_window(self, handle):
        if handle in self._windows:
            return self._windows[handle]
        if self.objc is None:
            return None
        # On Cocoa a Qt WId points to NSView, not NSWindow.
        view = self.objc.objc_object(c_void_p=int(handle))
        window = view.window()
        if window is not None:
            self._windows[handle] = window
        return window

    def _widget(self, handle):
        if handle not in self._widgets:
            widget = QWidget.find(int(handle))
            if widget is not None:
                self._widgets[handle] = widget
        return self._widgets.get(handle)

    def configure(self, handle, click_through=True):
        self._widget(handle)
        window = self._own_window(handle)
        if window is None:
            return
        a = self.appkit
        window.setIgnoresMouseEvents_(bool(click_through))
        window.setLevel_(a.NSFloatingWindowLevel)
        window.setHidesOnDeactivate_(False)
        window.setCanHide_(False)
        if isinstance(window, a.NSPanel):
            window.setStyleMask_(window.styleMask() | a.NSWindowStyleMaskNonactivatingPanel)
            window.setBecomesKeyOnlyIfNeeded_(True)
        behavior = (a.NSWindowCollectionBehaviorCanJoinAllSpaces
                    | a.NSWindowCollectionBehaviorFullScreenAuxiliary
                    | a.NSWindowCollectionBehaviorIgnoresCycle)
        behavior |= getattr(a, 'NSWindowCollectionBehaviorCanJoinAllApplications', 0)
        window.setCollectionBehavior_(behavior)

    def set_companions(self, handles):
        self.companions = set(handles)

    def visible_target(self, handle):
        if handle == MANUAL_WINDOW:
            return True
        target = self._targets.get(handle)
        if target is None or self.appkit is None:
            return False
        foreground = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if foreground and int(foreground.processIdentifier()) == target['pid']:
            return True
        key = self.appkit.NSApplication.sharedApplication().keyWindow()
        return bool(key and any(self._own_window(h) == key for h in self.companions))

    def place(self, handle, geometry):
        widget = self._widget(handle)
        if widget is None:
            return False
        widget.setGeometry(*map(round, geometry))
        if handle not in self._placed:
            window = self._own_window(handle)
            if window is not None:
                window.orderFrontRegardless()
            self._placed.add(handle)
        return True

    def place_many(self, placements):
        return all(self.place(handle, geometry) for handle, geometry in placements)

    def raise_companion(self, handle):
        window = self._own_window(handle)
        if window is not None:
            window.orderFrontRegardless()
        return True

    def activate_companion(self, handle):
        window = self._own_window(handle)
        if window is not None:
            window.makeKeyWindow()
            return True
        widget = self._widget(handle)
        if widget is not None:
            widget.activateWindow()
        return widget is not None

    def activate_target(self, handle):
        if handle == MANUAL_WINDOW:
            return True
        target = self._targets.get(handle)
        if target is None or self.appkit is None:
            return False
        app = self.appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(target['pid'])
        return bool(app and app.activateWithOptions_(self.appkit.NSApplicationActivateIgnoringOtherApps))

    def restore_target_focus(self, handle):
        if handle != MANUAL_WINDOW:
            self.activate_target(handle)

    def confirm_selection(self, target):
        targets = self.targets()
        if len(targets) != 1 or any(targets[0][key] != target[key] for key in ('pid', 'hwnd')):
            return None
        if not hasattr(self, '_navigation_log'):
            self._navigation_log = RouteLog(Path.home()/'Library/Logs/com.openai.codex')
        try:
            return self._navigation_log.poll(target['pid'], time.monotonic())
        except (OSError, ValueError):
            return None

    def cursor(self):
        point = QCursor.pos()
        return point.x(), point.y()

    def primary_down(self):
        return bool(self.appkit and self.appkit.NSEvent.pressedMouseButtons() & 1)

    def reduce_motion(self):
        return bool(self.appkit and self.appkit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())

    def accessibility_enabled(self):
        return bool(self.accessibility and self.accessibility.AXIsProcessTrusted())

    def forward_wheel(self, handle, delta, position, modifiers, pixel_delta=None):
        # Foreign-app event posting requires existing Accessibility permission.
        # Never trigger a permission prompt from scrolling or background work.
        if not self.accessibility_enabled() or not self.visible_target(handle) or handle == MANUAL_WINDOW:
            return False
        target = self._targets.get(handle)
        if target is None:
            return False
        q = self.quartz
        precise = pixel_delta is not None and not pixel_delta.isNull()
        dx, dy = ((pixel_delta.x(), pixel_delta.y()) if precise else (delta.x()/120, delta.y()/120))
        event = q.CGEventCreateScrollWheelEvent(None, q.kCGScrollEventUnitPixel if precise else q.kCGScrollEventUnitLine,
                                               2, round(dy), round(dx))
        if event is None:
            return False
        q.CGEventSetLocation(event, (position.x(), position.y()))
        from PySide6.QtCore import Qt
        flags = 0
        for modifier, value in ((Qt.ShiftModifier, q.kCGEventFlagMaskShift),
                                (Qt.ControlModifier, q.kCGEventFlagMaskCommand),
                                (Qt.AltModifier, q.kCGEventFlagMaskAlternate),
                                (Qt.MetaModifier, q.kCGEventFlagMaskControl)):
            if modifiers & modifier:
                flags |= value
        q.CGEventSetFlags(event, flags)
        q.CGEventPostToPid(target['pid'], event)
        return True


class AccessibilityEvents:
    """Optional event acceleration. Refusal always leaves polling operational."""
    def __init__(self, native, changed):
        self.native, self.changed = native, changed
        self.observers = {}
        self.callback = self._changed

    def _changed(self, observer, element, notification, context):
        # An in-flight Cocoa callback can arrive during Qt shutdown. Python
        # exceptions must not unwind through the native run loop.
        try:
            self.changed()
        except Exception:
            pass

    def sync(self, pids):
        a = self.native.accessibility
        allowed = self.native.accessibility_enabled()
        wanted = set(pids) if allowed else set()
        for pid in self.observers.keys()-wanted:
            self._remove(pid)
        if not wanted:
            return
        import CoreFoundation as cf
        for pid in wanted:
            existing = self.observers.get(pid)
            if existing:
                observer, application, registered, source = existing
            else:
                error, observer = a.AXObserverCreate(pid, self.callback, None)
                if error or observer is None:
                    continue
                application = a.AXUIElementCreateApplication(pid)
                registered = []
            notifications = ('AXFocusedWindowChanged', 'AXWindowCreated', 'AXWindowMoved',
                             'AXWindowResized', 'AXWindowMiniaturized', 'AXWindowDeminiaturized')
            elements = [application]
            error, windows = a.AXUIElementCopyAttributeValue(application, 'AXWindows', None)
            if not error and windows:
                elements.extend(windows)
            # Codex can create a new window in the same process. Refresh its
            # subscriptions too, while the polling fallback remains active.
            for element, name in registered.copy():
                if element not in elements:
                    a.AXObserverRemoveNotification(observer, element, name)
                    registered.remove((element, name))
            for element in elements:
                for name in notifications:
                    if (element, name) not in registered and a.AXObserverAddNotification(observer, element, name, None) == 0:
                        registered.append((element, name))
            if registered and not existing:
                source = a.AXObserverGetRunLoopSource(observer)
                cf.CFRunLoopAddSource(cf.CFRunLoopGetMain(), source, cf.kCFRunLoopCommonModes)
                self.observers[pid] = (observer, application, registered, source)

    def _remove(self, pid):
        import CoreFoundation as cf
        observer, application, registered, source = self.observers.pop(pid)
        try:
            for element, name in registered:
                self.native.accessibility.AXObserverRemoveNotification(observer, element, name)
        finally:
            cf.CFRunLoopRemoveSource(cf.CFRunLoopGetMain(), source, cf.kCFRunLoopCommonModes)

    def close(self):
        for pid in list(self.observers):
            try:
                self._remove(pid)
            except Exception:
                pass


class MacSelectionTracker(QObject):
    observed = Signal(dict)

    def __init__(self, native, root=None):
        super().__init__()
        self.native = native
        self.log = RouteLog(root or Path.home()/'Library/Logs/com.openai.codex')
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)
        self.events = AccessibilityEvents(native, self.queue_poll)
        self.queued = False
        self.previous = None
        self.last_sent = self.next_events = 0

    def queue_poll(self):
        if not self.queued and self.isRunning():
            self.queued = True
            QTimer.singleShot(0, self.poll)

    def poll(self):
        self.queued = False
        if not self.isRunning():
            return
        now = time.monotonic()
        try:
            targets = self.native.targets()
            state = selection_state(targets, self.log, now)
            if self.native.issue:
                state['issue'] = self.native.issue
            if now >= self.next_events:
                self.next_events = now+5
                try:
                    self.events.sync(target['pid'] for target in targets)
                except Exception:
                    # AX is optional. A transient native error must not discard
                    # an otherwise valid permission-free window/log selection.
                    pass
        except (OSError, ValueError, RuntimeError):
            state = dict(target=None, selection=None, issue='창 추적이 지연됩니다. 메뉴에서 세션을 선택해 독립 패널을 여세요.')
        if state != self.previous or now-self.last_sent >= 1:
            self.previous, self.last_sent = state, now
            self.observed.emit(state)

    def start(self):
        self.timer.start()
        self.poll()

    def isRunning(self):
        return self.timer.isActive()

    def stop(self):
        self.timer.stop()
        self.events.close()
