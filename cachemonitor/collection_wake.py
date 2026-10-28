"""Wake source collection for changed rollouts; periodic reconciliation stays authoritative."""
import ctypes
from ctypes import wintypes as W
from dataclasses import dataclass
import math
import os
from pathlib import Path
import time


@dataclass(frozen=True)
class SourceHint:
    roots: frozenset[Path] = frozenset()
    uncertain: bool = False
    metadata: frozenset[Path] = frozenset()

    def __bool__(self):
        return bool(self.roots) or self.uncertain or bool(self.metadata)


class SourceChanges:
    MAX_HANDLES = 64  # Win32 MAXIMUM_WAIT_OBJECTS, with no extra waiter thread.
    COALESCE_SECONDS = .25
    RESCAN_SECONDS = 3.
    METADATA_SECONDS = .25

    def __init__(self, excluded=(), *, evidence=None, demand=None):
        self.handles = {}; self.next_sync = 0.
        self.metadata = {}; self.next_metadata = 0.
        self.last_wake = float('-inf')
        self.excluded = tuple(Path(path).resolve() for path in excluded)
        evidence = Path(evidence).resolve() if evidence is not None else None
        self.evidence = (evidence, evidence.with_name(evidence.name+'-wal')) if evidence is not None else ()
        self.evidence_signature = self._signature(self.evidence)
        self.demand=demand
        self.demand_revision=demand() if demand is not None else None
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
        homes = tuple(Path(home).resolve() for home in homes)
        self.metadata = {home:(self.metadata[home] if home in self.metadata else
                               self._metadata_signature(home)) for home in homes}
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

    @staticmethod
    def _signature(paths):
        result = []
        for path in paths:
            try:
                value = path.stat()
                result.append((value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns))
            except OSError:
                result.append(None)
        return tuple(result)

    @classmethod
    def _metadata_signature(cls, home):
        return cls._signature(home/name for name in
            ('state_5.sqlite', 'state_5.sqlite-wal', '.codex-global-state.json'))

    def _changes(self, now):
        if now < self.next_metadata: return SourceHint()
        self.next_metadata = now + self.METADATA_SECONDS
        changed = set()
        for home, previous in self.metadata.items():
            current = self._metadata_signature(home)
            if current != previous:
                changed.add(home); self.metadata[home] = current
        current = self._signature(self.evidence)
        evidence_changed = current != self.evidence_signature
        self.evidence_signature = current
        revision=self.demand() if self.demand is not None else None
        demand_changed=revision!=self.demand_revision
        self.demand_revision=revision
        return SourceHint(uncertain=evidence_changed or demand_changed, metadata=frozenset(changed))

    def wait(self, deadline):
        """Return a coalesced hint, never a claim that all source writes are durable."""
        while True:
            now = time.monotonic()
            hint = self._changes(now)
            if hint: return hint
            seconds = max(0., deadline - now)
            interval = min(seconds, max(0., self.next_metadata-now)) if self.metadata or self.evidence else seconds
            if not self.handles:
                if interval: time.sleep(interval)
                if time.monotonic() >= deadline: return SourceHint(uncertain=True)
                continue
            entries = tuple(self.handles.items())
            handles = (W.HANDLE * len(entries))(*(value[0] for _, value in entries))
            result = self.api.WaitForMultipleObjects(len(handles), handles, False, math.ceil(interval * 1000))
            if result != 0x102: break
            if time.monotonic() >= deadline: return SourceHint()
        if result >= len(handles):
            self.close(); self.next_sync = 0.
            if seconds: time.sleep(max(0., deadline - time.monotonic()))
            return SourceHint(uncertain=True)
        roots = {entries[result][0]}; uncertain = False
        # An isolated write wakes immediately. Continuous bursts still receive
        # at most one wake per window, including time spent collecting them.
        now = time.monotonic()
        pause = min(max(0., self.last_wake+self.COALESCE_SECONDS-now), max(0., deadline-now))
        if pause: time.sleep(pause)
        for root, (handle, _) in tuple(self.handles.items()):
            state = self.api.WaitForSingleObject(handle, 0)
            if state == 0:
                roots.add(root)
                # Rearm only a signalled handle. A timeout must not rearm it.
                if not self.api.FindNextChangeNotification(handle):
                    self.api.FindCloseChangeNotification(handle); self.handles.pop(root)
                    uncertain = True; self.next_sync = 0.
            elif state != 0x102:
                self.api.FindCloseChangeNotification(handle); self.handles.pop(root)
                uncertain = True; self.next_sync = 0.
        self.last_wake = time.monotonic()
        hint = self._changes(self.last_wake)
        return SourceHint(frozenset(roots), uncertain or hint.uncertain, hint.metadata)

    def close(self):
        for handle, _ in self.handles.values(): self.api.FindCloseChangeNotification(handle)
        self.handles.clear()
        self.metadata.clear()
