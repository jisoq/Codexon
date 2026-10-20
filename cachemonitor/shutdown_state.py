"""The GUI exit operation's cancellation and durable cutover decision."""
from dataclasses import dataclass
import threading


class ExitCancelled(Exception):
    pass


class ExitControl:
    def __init__(self):
        self._lock = threading.Lock()
        self._committed = False
        self._cancelled = False
        self._force_requested = False

    @property
    def committed(self):
        with self._lock:
            return self._committed

    @property
    def cancelled(self):
        with self._lock:
            return self._cancelled

    @property
    def force_requested(self):
        with self._lock:
            return self._force_requested

    def request_cancel(self):
        with self._lock:
            if self._committed:
                return False
            self._cancelled = True
            return True

    def request_force(self):
        with self._lock:
            if self._cancelled:
                return False
            self._force_requested = True
            return True

    def checkpoint(self):
        with self._lock:
            if self._cancelled:
                raise ExitCancelled()

    def commit(self, persist_callback=None):
        with self._lock:
            if self._cancelled:
                raise ExitCancelled()
            if self._committed:
                return
            if persist_callback:
                persist_callback()
            self._committed = True


@dataclass(frozen=True)
class ExitProgress:
    stage: str
    message: str
    can_cancel: bool
    can_force: bool = False
    responding: int = 0
    unknown: int = 0


@dataclass(frozen=True)
class ExitResult:
    status: str
    committed: bool = False
    error: str = ''
    warning: str = ''
