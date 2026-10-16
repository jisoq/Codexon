"""One bounded read actor; only the writer loop sends messages to the GUI.

The supplied handler owns its read-only prepared connection in this thread.
An obsolete request is cancelled and cannot overwrite a newer completion.
"""
from collections import OrderedDict
from multiprocessing import Pipe
from threading import Condition, Event, Thread
import traceback

from .analysis_engine import retained_bytes


KINDS = frozenset(('query', 'record', 'population', 'overlay_detail'))
RESULT_BYTES = 64 * 1024 * 1024


class QueryActor:
    def __init__(self, factory):
        self.factory = factory
        self.condition = Condition()
        self.pending = OrderedDict()
        self.completed = OrderedDict()
        self.serial = 0
        self.latest = {}
        self.active = None
        self.failure = None
        self.stopping = False
        self.signalled = False
        self.notification, self._signal = Pipe(duplex=False)
        self.thread = Thread(target=self._run, name='prepared-query', daemon=True)
        self.thread.start()

    def submit(self, message, token):
        kind = message['kind']
        if kind not in KINDS:
            raise ValueError('query actor accepts only page and detail requests')
        with self.condition:
            if self.stopping:
                raise RuntimeError('query actor has stopped')
            self.serial += 1
            serial = self.serial
            self.latest[kind] = serial
            self.pending.pop(kind, None)
            self.completed.pop(kind, None)
            self.pending[kind] = (serial, dict(message), token)
            if self.active is not None:
                active_kind, active_serial, active_message, active_token, cancel = self.active
                if active_kind == kind:
                    cancel.set()
                elif kind in ('record','overlay_detail') and active_kind == 'query':
                    # Exact keyboard selection can interrupt a large page. Retry
                    # that page only while it is still this consumer's latest.
                    cancel.set()
                    if self.latest.get(active_kind) == active_serial:
                        self.pending.setdefault(active_kind, (active_serial, active_message, active_token))
            self.condition.notify()
        return serial

    def cancel(self, kind=None):
        with self.condition:
            kinds = KINDS if kind is None else (kind,)
            for item in kinds:
                self.latest.pop(item, None)
                self.pending.pop(item, None)
                self.completed.pop(item, None)
            if self.active is not None and self.active[0] in kinds:
                self.active[-1].set()

    def drain(self):
        """Called by the sole GUI connection owner after the wake pipe fires."""
        with self.condition:
            if self.signalled:
                self.notification.recv_bytes()
                self.signalled = False
            values = list(self.completed.values())
            self.completed.clear()
            return values

    def _run(self):
        handler = None
        try:
            # SQLite connections are created, used and closed by this actor.
            handler = self.factory()
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopping or self.pending)
                    if self.stopping:
                        return
                    kind = ('record' if 'record' in self.pending else
                            'overlay_detail' if 'overlay_detail' in self.pending else next(iter(self.pending)))
                    serial, message, token = self.pending.pop(kind)
                    cancel = Event()
                    self.active = (kind, serial, message, token, cancel)
                try:
                    result = handler(message, token, cancel.is_set)
                    if retained_bytes(result, RESULT_BYTES) > RESULT_BYTES:
                        raise ValueError('prepared query result exceeds the admitted delivery budget')
                except Exception:
                    result = dict(kind='error', id=message.get('id'), error=traceback.format_exc(limit=6))
                with self.condition:
                    self.active = None
                    if self.stopping:
                        return
                    if cancel.is_set() or self.latest.get(kind) != serial:
                        continue
                    # At most one completion per consumer, including an old
                    # completion waiting while the writer commits its current slice.
                    self.completed[kind] = result
                    if not self.signalled:
                        self._signal.send_bytes(b'1')
                        self.signalled = True
        except Exception:
            self._failed()
        finally:
            if handler is not None:
                try:
                    handler.close()
                except Exception:
                    self._failed()

    def _failed(self):
        failure = traceback.format_exc(limit=6)
        with self.condition:
            self.failure = failure
            self.stopping = True
            self.active = None
            self.pending.clear()
            self.completed.clear()
            self.completed['fatal'] = dict(kind='fatal', error=failure)
            if not self.signalled:
                self._signal.send_bytes(b'1')
                self.signalled = True

    def close(self, *, timeout=5):
        with self.condition:
            self.stopping = True
            self.pending.clear()
            self.completed.clear()
            if self.active is not None:
                self.active[-1].set()
            self.condition.notify()
        self.thread.join(timeout)
        if self.thread.is_alive():
            # Do not close a connection owned by a live reader or pretend the
            # shutdown completed. The worker's shutdown policy handles this.
            raise TimeoutError('prepared read actor did not release its query lease')
        self.notification.close()
        self._signal.close()
