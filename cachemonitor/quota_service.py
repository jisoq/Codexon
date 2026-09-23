"""Independent live quota polling and ledger reader; never waits for dashboard analysis."""
from pathlib import Path
import threading
import time
import queue
from collections import deque

from PySide6.QtCore import QThread, Signal
from .quota_cycles import QuotaLedger, ledger_path
from .quota_live import AccountClient
from .quota_reader import QuotaReader
from .quota import select_current_quota
from . import quota_tracking_store as tracking_store
from .quota_polling import QuotaPolling
from .quota_diagnostics import record_failure, storage_issue


class QuotaService(QThread):
    # Reports contain the historical call graph; keep it as a Python object,
    # as AnalysisBridge does, rather than recursively copying QVariant maps.
    updated = Signal(object)

    def __init__(self, home, index_path=None, live=True, tracking_enabled=True):
        super().__init__()
        self.home = str(Path(home).resolve())
        self.path = ledger_path(index_path)
        self.live = live
        self.wake = threading.Event()
        self.tracking_enabled=tracking_enabled
        self.tracking_after=time.time()
        self.tracking_changes=queue.SimpleQueue()

    def set_tracking_enabled(self, enabled):
        self.tracking_enabled=bool(enabled)
        at=time.time()
        if enabled:self.tracking_after=at
        self.tracking_changes.put((bool(enabled),at))
        self.wake.set()

    def run(self):
        ledger = None
        client = AccountClient(self.home)
        client.cache_seconds = QuotaPolling.MINIMUM
        self.client = client
        reader = QuotaReader(self.home)
        latest = direct = local = None
        polling = QuotaPolling()
        pending = deque()
        controls = deque()
        next_storage = next_report = next_local = 0
        storage_failures = 0
        lookup_issue = database_issue = ''
        emitted_issue = None
        report = None
        stage = 'initialize'

        def storage_failed(exc):
            nonlocal ledger, database_issue, next_storage, storage_failures
            record_failure(self.path, stage, exc)
            database_issue = storage_issue(exc)
            storage_failures += 1
            next_storage = time.monotonic()+min(60, 5*2**min(storage_failures-1, 4))
            if ledger is not None:
                try:
                    ledger.db.rollback()
                finally:
                    ledger.close()
                    ledger = None

        try:
            while not self.isInterruptionRequested():
                self.wake.clear()
                while not self.tracking_changes.empty():
                    change = self.tracking_changes.get_nowait()
                    controls.append(change)
                    next_report = 0
                    if change[0] and not polling.failures:
                        polling.next_read = min(polling.next_read, time.monotonic())
                now = time.time()
                if polling.tick(time.monotonic(), now):
                    client.close()
                    next_storage = next_local = next_report = 0
                if time.monotonic() >= next_storage:
                    try:
                        stage = 'open ledger'
                        if ledger is None:
                            ledger = QuotaLedger(self.path)
                            # Apply queued controls at their actual timestamps first.
                            if not controls:
                                tracking_store.enable(ledger.db, self.home, at=self.tracking_after,
                                                      enabled=self.tracking_enabled)
                            next_report = 0
                        stage = 'tracking controls'
                        while controls:
                            enabled, at = controls[0]
                            tracking_store.enable(ledger.db, self.home, at=at, enabled=enabled)
                            controls.popleft()
                        self.tracking_after = max(self.tracking_after, ledger.db.execute(
                            'select at from tracking_controls where home=? order by at desc limit 1',
                            (self.home,)).fetchone()[0])
                        stage = 'activity'
                        # Old interrupted tasks can lack an end forever. Use recent
                        # activity for polling speed without altering their history.
                        activity = ledger.db.execute('select count(*),max(start),max(end),sum(end is null and start>=?) '
                            'from tracking_tasks where home=?', (now-300, self.home)).fetchone()
                        wire = ledger.db.execute('select count(*),max(start),max(end),sum(end is null and start>=?) '
                            'from tracking_wire where home=?', (now-300, self.home)).fetchone()
                        signature = tuple(activity)+tuple(wire)
                        if self.tracking_enabled:
                            if signature != polling.signature:
                                next_report = 0
                            polling.activity(signature, bool(activity[3] or wire[3]), time.monotonic())
                        else:
                            polling.active = False
                        client.after = max(value for value in (activity[1], activity[2], wire[1], wire[2],
                            self.tracking_after) if value is not None)
                    except Exception as exc:
                        storage_failed(exc)
                if self.live and polling.ready(time.monotonic()):
                    try:
                        direct = client.fetch()
                        if self.tracking_enabled:
                            pending.append(('quota', direct))
                        lookup_issue = ''
                        polling.finish(time.monotonic(), True, direct, time.time())
                    except Exception as exc:
                        record_failure(self.path, 'account lookup', exc)
                        lookup_issue = ('Codex 한도 조회 지연 · 자동 재시도 중' if isinstance(exc, (TimeoutError, ConnectionError))
                                        else 'Codex 한도 조회 실패 · 자동 재시도 중')
                        if self.tracking_enabled:
                            pending.append(('failure', time.time()))
                        polling.finish(time.monotonic(), False)
                    next_report = 0
                if (not direct or time.time()-direct['observed_at'] >= 90) and time.monotonic() >= next_local:
                    try:
                        local_result = reader.poll()
                        quota = local_result['quota']
                        if local_result.get('errors'):
                            lookup_issue = lookup_issue or '로컬 한도 기록 읽기 지연'
                        if quota:
                            local = {**quota, 'source': 'local', 'max_age': 120}
                            if self.tracking_enabled:
                                pending.append(('local', local))
                    except Exception as exc:
                        record_failure(self.path, 'local lookup', exc)
                        lookup_issue = lookup_issue or '로컬 한도 기록 읽기 지연'
                    next_local = time.monotonic()+15
                publish = report is None or time.monotonic() >= next_report
                if ledger is not None:
                    try:
                        stage = 'save observations'
                        while pending:
                            kind, value = pending[0]
                            if kind == 'failure':
                                tracking_store.failed_observation(ledger.db, self.home, value)
                            else:
                                ledger.observe(self.home, value)
                                if kind == 'quota':
                                    tracking_store.observe(ledger.db, self.home, value)
                            pending.popleft()
                        if publish:
                            stage = 'report'
                            updated_report = ledger.report(self.home)
                            from .quota_view import prepare_quota_view
                            updated_report['view'] = prepare_quota_view(updated_report)
                            report = updated_report
                        database_issue = ''
                        storage_failures = 0
                    except Exception as exc:
                        storage_failed(exc)
                        publish = True
                latest = select_current_quota(direct, local, time.time())
                issue = ' · '.join(v for v in (lookup_issue, database_issue) if v)
                if publish or issue != emitted_issue:
                    self.updated.emit({'home': self.home, 'quota': latest,
                        'issue': issue,
                        'report': report or {'home': self.home, 'cycles': [], 'error': True, 'index_loading': False}})
                    emitted_issue = issue
                    next_report = time.monotonic()+5
                self.wake.wait(1)
        finally:
            client.close()
            if ledger:
                ledger.close()

    def stop(self):
        self.requestInterruption()
        self.wake.set()
        client = getattr(self,'client',None)
        process = getattr(client,'process',None)
        if process and process.poll() is None:
            try: process.terminate()
            except OSError: pass
        self.wait()
