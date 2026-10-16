"""Wake source collection for changed rollouts; periodic reconciliation stays authoritative."""
import ctypes
from ctypes import wintypes as W
import math
import os
from pathlib import Path
import time


class SourceChanges:
    MAX_HANDLES = 64  # Win32 MAXIMUM_WAIT_OBJECTS, with no extra waiter thread.
    COALESCE_SECONDS = .25
    RESCAN_SECONDS = 3.

    def __init__(self, excluded=()):
        self.handles = {}; self.next_sync = 0.
        self.excluded = tuple(Path(path).resolve() for path in excluded)
        self.api = None
        if os.name != 'nt': return
        try:
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            for name, args, result in (
                ('FindFirstChangeNotificationW', [W.LPCWSTR, W.BOOL, W.DWORD], W.HANDLE),
                ('FindNextChangeNotification', [W.HANDLE], W.BOOL),
                ('FindCloseChangeNotification', [W.HANDLE], W.BOOL),
                ('WaitForMultipleObjects', [W.DWORD, ctypes.POINTER(W.HANDLE), W.BOOL, W.DWORD], W.DWORD),
                ('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD)):
                function = getattr(api, name); function.argtypes = args; function.restype = result
            self.api = api
        except (OSError, AttributeError): pass

    def sync(self, homes):
        if self.api is None: return
        now = time.monotonic()
        if now < self.next_sync: return
        self.next_sync = now + self.RESCAN_SECONDS
        wanted = {}
        for home in homes:
            for folder in ('sessions', 'archived_sessions'):
                try:
                    root = (Path(home) / folder).resolve()
                    if not root.is_dir() or any(path.is_relative_to(root) for path in self.excluded): continue
                    stat = root.stat(); wanted[root] = (stat.st_dev, stat.st_ino)
                except (OSError, RuntimeError): continue
                if len(wanted) == self.MAX_HANDLES: break
            if len(wanted) == self.MAX_HANDLES: break
        for root, (handle, identity) in tuple(self.handles.items()):
            if wanted.get(root) != identity:
                self.api.FindCloseChangeNotification(handle); self.handles.pop(root)
        for root, identity in wanted.items():
            if root in self.handles: continue
            handle = self.api.FindFirstChangeNotificationW(str(root), True, 0x1 | 0x2 | 0x8 | 0x10)
            if handle not in (None, ctypes.c_void_p(-1).value): self.handles[root] = (handle, identity)

    def wait(self, deadline):
        """Return a coalesced hint, never a claim that all source writes are durable."""
        seconds = max(0., deadline - time.monotonic())
        if not self.handles:
            if seconds: time.sleep(seconds)
            return False
        values = tuple(self.handles.values())
        handles = (W.HANDLE * len(values))(*(value[0] for value in values))
        result = self.api.WaitForMultipleObjects(len(handles), handles, False, math.ceil(seconds * 1000))
        if result == 0x102: return False
        if result >= len(handles):
            self.close(); self.next_sync = 0.
            if seconds: time.sleep(max(0., deadline - time.monotonic()))
            return False
        # A fixed window from the first signal bounds burst latency even while
        # more events arrive. No per-event queue or trailing debounce is kept.
        time.sleep(min(self.COALESCE_SECONDS, max(0., deadline - time.monotonic())))
        for root, (handle, _) in tuple(self.handles.items()):
            if self.api.WaitForSingleObject(handle, 0) == 0:
                # Rearm only a signalled handle. A timeout must not rearm it.
                if not self.api.FindNextChangeNotification(handle):
                    self.api.FindCloseChangeNotification(handle); self.handles.pop(root)
        return True

    def close(self):
        for handle, _ in self.handles.values(): self.api.FindCloseChangeNotification(handle)
        self.handles.clear()
