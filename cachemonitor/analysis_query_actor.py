"""One bounded read actor; only the writer loop sends messages to the GUI.

The supplied handler owns its read-only prepared connection in this thread.
An obsolete request is cancelled and cannot overwrite a newer completion.
"""
from collections import OrderedDict
from multiprocessing import Pipe
from threading import Condition, Event, Thread
import traceback
import time

from .analysis_engine import retained_bytes


KINDS = frozenset(('query', 'record', 'population', 'overlay_detail'))
RESULT_BYTES = 64 * 1024 * 1024
OVERLAY_COMPLETIONS = 8


class QueryActor:
    def __init__(self, factory):
        self.factory = factory
        self.condition = Condition()
        self.pending = OrderedDict()
        self.completed = OrderedDict()
        self.overlay_completed = OrderedDict()
        self.overlay_completed_bytes = 0
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
            if kind == 'query':
                self.completed.pop('progress', None)
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
                if item == 'overlay_detail':
                    self.overlay_completed.clear()
                    self.overlay_completed_bytes = 0
                if item == 'query':
                    self.completed.pop('progress', None)
            if self.active is not None and self.active[0] in kinds:
                self.active[-1].set()

    def drain(self):
        """Called by the sole GUI connection owner after the wake pipe fires."""
        with self.condition:
            if self.signalled:
                self.notification.recv_bytes()
                self.signalled = False
            values = list(self.completed.values())
            values.extend(value for value, _ in self.overlay_completed.values())
            self.completed.clear()
            self.overlay_completed.clear()
            self.overlay_completed_bytes = 0
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
                    last_progress=[None,0.]
                    def progress(stage):
                        value=stage if isinstance(stage,dict) else dict(stage=stage)
                        now=time.monotonic()
                        if (value.get('stage')==last_progress[0] and now-last_progress[1]<.25
                                and value.get('processed')!=value.get('total')):
                            return
                        last_progress[:]=[value.get('stage'),now]
                        self._progress(kind, serial, message, cancel, value)
                    result = handler(dict(message, _progress=progress), token, cancel.is_set)
                    size = retained_bytes(result, RESULT_BYTES)
                    if size > RESULT_BYTES:
                        raise ValueError('prepared query result exceeds the admitted delivery budget')
                except Exception:
                    result = dict(kind='error', id=message.get('id'), error=traceback.format_exc(limit=6))
                with self.condition:
                    self.active = None
                    if self.stopping:
                        return
                    if cancel.is_set() or self.latest.get(kind) != serial:
                        continue
                    if kind == 'query':
                        self.completed.pop('progress', None)
                    if kind == 'overlay_detail' and result.get('kind') == kind:
                        # A completed owner remains useful after selection changes.
                        # Active obsolete reads still follow latest-only cancellation.
                        self.overlay_completed[serial] = (result, size)
                        self.overlay_completed_bytes += size
                        while (len(self.overlay_completed) > OVERLAY_COMPLETIONS or
                               self.overlay_completed_bytes > RESULT_BYTES):
                            _, (_, old_size) = self.overlay_completed.popitem(last=False)
                            self.overlay_completed_bytes -= old_size
                    else:
                        # Other consumers retain only their latest completion.
                        self.completed[kind] = result
                    self.condition.notify_all()
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

    def _progress(self, kind, serial, message, cancel, stage):
        if kind != 'query':
            return
        with self.condition:
            if self.stopping or cancel.is_set() or self.latest.get(kind) != serial:
                return
            value=stage if isinstance(stage,dict) else dict(stage=stage)
            self.completed['progress'] = dict(kind='progress', id=message['id'], **value)
            if not self.signalled:
                self._signal.send_bytes(b'1')
                self.signalled = True

    def _failed(self):
        failure = traceback.format_exc(limit=6)
        with self.condition:
            self.failure = failure
            self.stopping = True
            self.active = None
            self.pending.clear()
            self.completed.clear()
            self.overlay_completed.clear()
            self.overlay_completed_bytes = 0
            self.completed['fatal'] = dict(kind='fatal', error=failure)
            if not self.signalled:
                self._signal.send_bytes(b'1')
                self.signalled = True

    def close(self, *, timeout=5):
        with self.condition:
            self.stopping = True
            self.pending.clear()
            self.completed.clear()
            self.overlay_completed.clear()
            self.overlay_completed_bytes = 0
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
