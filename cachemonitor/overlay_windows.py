"""Windows boundary for a non-activating companion window; no injection or reparenting."""
import ctypes
from ctypes import wintypes as W
import os
from pathlib import Path
import re
import time

from PySide6.QtCore import QThread, Signal, QTimer
from .overlay_tracking import RouteLog


def observe_selection(targets, log, now):
    """A single live process and its route evidence establish compatibility."""
    result = {'target': None, 'selection': None, 'issue': ''}
    if len(targets) == 1:
        target = targets[0]
        result.update(target=target, selection=log.poll(target['pid'], now))
        if result['selection'] is None:
            result['issue'] = '세션 화면 확인 중'
    elif targets:
        result['issue'] = 'Codex 기본 창 한 개에서 지원'
    else:
        result['issue'] = 'Codex 창 대기'
    return result


class WindowsOverlay:
    def __init__(self):
        self.companions = set()
        self.u = ctypes.WinDLL('user32', use_last_error=True)
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.d = ctypes.WinDLL('dwmapi')
        functions = {
            'GetForegroundWindow': ([], W.HWND),
            'SetForegroundWindow': ([W.HWND], W.BOOL),
            'ShowWindow': ([W.HWND, ctypes.c_int], W.BOOL),
            'PostMessageW': ([W.HWND,W.UINT,W.WPARAM,W.LPARAM], W.BOOL),
            'GetAsyncKeyState': ([ctypes.c_int], ctypes.c_short),
            'GetCursorPos': ([ctypes.POINTER(W.POINT)], W.BOOL),
            'GetWindowThreadProcessId': ([W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
            'GetWindowTextW': ([W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetClassNameW': ([W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
            'IsWindow': ([W.HWND], W.BOOL),
            'IsWindowVisible': ([W.HWND], W.BOOL),
            'IsIconic': ([W.HWND], W.BOOL),
            'GetDpiForWindow': ([W.HWND], W.UINT),
            'GetWindowRect': ([W.HWND,ctypes.POINTER(W.RECT)],W.BOOL),
            'SetWindowPos': ([W.HWND, W.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.UINT], W.BOOL),
            'GetWindowLongPtrW': ([W.HWND, ctypes.c_int], ctypes.c_ssize_t),
            'SetWindowLongPtrW': ([W.HWND, ctypes.c_int, ctypes.c_ssize_t], ctypes.c_ssize_t),
        }
        for name, (args, result) in functions.items():
            fn = getattr(self.u, name); fn.argtypes = args; fn.restype = result
        self.k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.k.OpenProcess.restype = W.HANDLE
        self.k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
        self.k.CloseHandle.argtypes = [W.HANDLE]
        self.d.DwmGetWindowAttribute.argtypes = [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD]

    def targets(self):
        found = []
        @ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        def visit(hwnd, _):
            if not self.u.IsWindowVisible(hwnd): return True
            cls = ctypes.create_unicode_buffer(128)
            self.u.GetClassNameW(hwnd, cls, len(cls))
            if cls.value != 'Chrome_WidgetWin_1': return True
            pid = W.DWORD(); self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process = self.k.OpenProcess(0x1000, False, pid.value)
            if not process: return True
            try:
                path = ctypes.create_unicode_buffer(2048); size = W.DWORD(len(path))
                if not self.k.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)): return True
            finally:
                self.k.CloseHandle(process)
            package = re.search(r'OpenAI\.Codex_([\d.]+)_', path.value)
            if not package: return True
            title = ctypes.create_unicode_buffer(512)
            self.u.GetWindowTextW(hwnd, title, len(title))
            if title.value not in ('ChatGPT', 'Codex'): return True
            found.append({'hwnd': hwnd, 'pid': pid.value, 'version': package[1]})
            return True
        self.u.EnumWindows.argtypes = [type(visit), W.LPARAM]
        self.u.EnumWindows(visit, 0)
        return found

    def frame(self, hwnd):
        rect = W.RECT(); cloaked = W.DWORD()
        if self.d.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)):
            return None
        if self.d.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) or cloaked.value:
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def visible_target(self, hwnd):
        return bool(self.u.IsWindow(hwnd) and self.u.IsWindowVisible(hwnd)
                    and not self.u.IsIconic(hwnd)
                    and self.u.GetForegroundWindow() in ({hwnd} | self.companions))

    def set_companions(self, handles):
        self.companions = {int(handle) for handle in handles}

    def activate_companion(self, hwnd):
        """Keyboard focus is granted only in response to explicit user input."""
        flags = self.u.GetWindowLongPtrW(hwnd, -20)
        self.u.SetWindowLongPtrW(hwnd, -20, flags & ~0x08000000)
        return bool(self.u.SetForegroundWindow(hwnd))

    def restore_target_focus(self, target):
        if self.u.GetForegroundWindow() in self.companions:
            self.u.SetForegroundWindow(target)

    def confirm_selection(self, target):
        """Fresh read-only route verification; an HWND alone never implies a task."""
        targets=self.targets()
        if (len(targets)!=1 or targets[0]['hwnd']!=target['hwnd']
                or targets[0]['pid']!=target['pid']):return None
        local=Path(os.environ['LOCALAPPDATA'])
        if not hasattr(self,'_navigation_log'):
            packaged=list((local/'Packages').glob('OpenAI.Codex_*/LocalCache/Local/Codex/Logs'))
            self._navigation_log=RouteLog(local/'Codex/Logs',packaged)
        try:
            # A bounded read that has not caught up fails closed.
            return observe_selection(targets,self._navigation_log,time.monotonic())['selection']
        except (OSError,ValueError):return None

    def activate_target(self, hwnd):
        if not self.u.IsWindow(hwnd):return False
        if self.u.IsIconic(hwnd):self.u.ShowWindow(hwnd,9)
        return bool(self.u.SetForegroundWindow(hwnd))

    def forward_wheel(self, hwnd, delta, position, modifiers):
        from PySide6.QtCore import Qt
        if not self.visible_target(hwnd):return False
        flags=(4 if modifiers & Qt.ShiftModifier else 0)|(8 if modifiers & Qt.ControlModifier else 0)
        x,y=self.cursor()
        message=0x20E if delta.x() else 0x20A
        amount=delta.x() if delta.x() else delta.y()
        return bool(self.u.PostMessageW(hwnd,message,flags|((amount&0xffff)<<16),(x&0xffff)|((y&0xffff)<<16)))

    def primary_down(self):
        return bool(self.u.GetAsyncKeyState(1) & 0x8000)

    def reduce_motion(self):
        enabled = W.BOOL(True)
        self.u.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
        return not enabled.value

    def configure(self, hwnd, click_through=True):
        flags = self.u.GetWindowLongPtrW(hwnd, -20)
        flags = flags | 0x08000000 | 0x00080000 | 0x80
        flags = flags | 0x20 if click_through else flags & ~0x20
        self.u.SetWindowLongPtrW(hwnd, -20, flags)
        # WS_EX_NOACTIVATE, LAYERED, TRANSPARENT, TOOLWINDOW. Text alpha remains opaque.

    def place(self, hwnd, geometry):
        visible_topmost=self.u.IsWindowVisible(hwnd) and self.u.GetWindowLongPtrW(hwnd,-20)&0x8
        if visible_topmost:
            rect=W.RECT();x,y,w,h=geometry
            if self.u.GetWindowRect(hwnd,ctypes.byref(rect)) and (rect.left,rect.top,rect.right,rect.bottom)==(x,y,x+w,y+h):
                return True
        # Repeated HWND_TOPMOST raises the panel over its controls every tick.
        # Preserve the established order while moving already-visible windows.
        flags=0x0010|0x0040|(0x0004 if visible_topmost else 0)
        return bool(self.u.SetWindowPos(hwnd, W.HWND(-1), *geometry, flags))

    def place_many(self,placements):
        placements=list(placements)
        self.u.BeginDeferWindowPos.argtypes=[ctypes.c_int]
        self.u.BeginDeferWindowPos.restype=ctypes.c_void_p
        self.u.DeferWindowPos.argtypes=[ctypes.c_void_p,W.HWND,W.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,W.UINT]
        self.u.DeferWindowPos.restype=ctypes.c_void_p
        self.u.EndDeferWindowPos.argtypes=[ctypes.c_void_p]
        self.u.EndDeferWindowPos.restype=W.BOOL
        batch=self.u.BeginDeferWindowPos(len(placements))
        if not batch:return all(self.place(hwnd,box) for hwnd,box in placements)
        for hwnd,box in placements:
            batch=self.u.DeferWindowPos(batch,hwnd,None,*box,0x0010|0x0004|0x0001)
            if not batch:return all(self.place(h,b) for h,b in placements)
        return bool(self.u.EndDeferWindowPos(batch))

    def raise_companion(self, hwnd):
        # Used once when a newly shown inline detail pane changes the stack.
        return bool(self.u.SetWindowPos(hwnd,W.HWND(-1),0,0,0,0,0x0001|0x0002|0x0010))

    def cursor(self):
        point=W.POINT()
        self.u.GetCursorPos(ctypes.byref(point))
        return point.x,point.y

class WindowEvents:
    """Out-of-context hooks delivered on the owning thread's Qt message loop."""
    def __init__(self,native,changed):
        self.api=native.u;self.handles=[];self.targets=set();self.closed=False
        prototype=ctypes.WINFUNCTYPE(None,W.HANDLE,W.DWORD,W.HWND,W.LONG,W.LONG,W.DWORD,W.DWORD)
        def callback(hook,event,hwnd,obj,child,thread,stamp):
            if self.closed:return
            if event==3 or (obj==0 and child==0 and (hwnd in self.targets or event in (0x8000,0x8002))):
                changed()
        self.callback=prototype(callback)
        self.api.SetWinEventHook.argtypes=[W.DWORD,W.DWORD,W.HMODULE,prototype,W.DWORD,W.DWORD,W.DWORD]
        self.api.SetWinEventHook.restype=W.HANDLE
        self.api.UnhookWinEvent.argtypes=[W.HANDLE];self.api.UnhookWinEvent.restype=W.BOOL
        for low,high in ((3,3),(0x8000,0x8003),(0x800B,0x800B)):
            handle=self.api.SetWinEventHook(low,high,None,self.callback,0,0,0)
            if handle:self.handles.append(handle)

    def close(self):
        self.closed=True
        for handle in self.handles:self.api.UnhookWinEvent(handle)
        self.handles.clear()


class SelectionTracker(QThread):
    observed = Signal(dict)

    def run(self):
        native = WindowsOverlay()
        local = Path(os.environ['LOCALAPPDATA'])
        # Codex children see a redirected LocalAppData view. The standalone tray
        # process needs the physical MSIX location, even when its alias is absent.
        packaged = list((local/'Packages').glob('OpenAI.Codex_*/LocalCache/Local/Codex/Logs'))
        log = RouteLog(local/'Codex/Logs', packaged)
        next_targets = 0
        targets = []
        previous = None
        dirty=True
        queued=False
        def changed():
            nonlocal dirty,queued
            dirty=True
            if not queued:
                queued=True;QTimer.singleShot(0,poll)
        hooks=WindowEvents(native,changed)
        def poll():
            nonlocal next_targets,targets,previous,dirty,queued
            queued=False
            if self.isInterruptionRequested():
                self.quit();return
            try:
                now = time.monotonic()
                event_changed=dirty
                if dirty or now >= next_targets:
                    targets = native.targets(); next_targets = now + 5;dirty=False
                    hooks.targets={t['hwnd'] for t in targets}
                result = observe_selection(targets, log, now)
            except (OSError, ValueError) as error:
                result = {'target': None, 'selection': None, 'issue': '화면 추적 오류: ' + str(error)}
            # Heartbeats permit the UI to hide if this worker stalls or exits.
            if event_changed or result != previous or now - getattr(self, '_sent', 0) >= 1:
                self.observed.emit(result); previous = result; self._sent = now
        timer=QTimer();timer.timeout.connect(poll);timer.start(500)
        try:
            poll()
            if not self.isInterruptionRequested():self.exec()
        finally:
            timer.stop();hooks.close()

    def stop(self):
        self.requestInterruption()
        self.quit()
        self.wait()
