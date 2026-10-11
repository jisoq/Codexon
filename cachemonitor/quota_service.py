"""Independent live quota polling and ledger reader; never waits for dashboard analysis."""
from pathlib import Path
import threading
import time
import queue
import sqlite3
from collections import deque

from PySide6.QtCore import QThread, Signal
from .quota_cycles import QuotaLedger, ledger_path
from .quota_live import AccountClient
from .quota import select_current_quota
from . import quota_tracking_store as tracking_store
from .quota_polling import QuotaPolling
from .quota_diagnostics import record_failure, storage_issue
from .quota_worker import QuotaWorker


class QuotaService(QThread):
    # Reports contain the historical call graph; keep it as a Python object,
    # as AnalysisBridge does, rather than recursively copying QVariant maps.
    updated = Signal(object)
    EXCLUSION_TIMEOUT = 30

    def __init__(self, home, index_path=None, live=True, tracking_enabled=True,quota_path=None,reports_enabled=True):
        super().__init__()
        self.home = str(Path(home).resolve())
        self.path = Path(quota_path) if quota_path else ledger_path(index_path)
        self.live = live
        self.wake = threading.Event()
        self.tracking_enabled=tracking_enabled
        self.tracking_after=time.time()
        self.tracking_changes=queue.SimpleQueue()
        self.local_observations=queue.SimpleQueue()
        self.exclusion_changes=queue.SimpleQueue()
        self.reports_enabled=reports_enabled
        self.worker_results=queue.SimpleQueue()
        self.report_worker=QuotaWorker(self.path,self.home,on_result=self._receive_worker)

    def _receive_worker(self, result):
        if result['kind'] in ('report','exclude','restore'):
            self.worker_results.put(result)
            self.wake.set()

    def set_reports_enabled(self,enabled):
        enabled=bool(enabled)
        if enabled!=self.reports_enabled:
            self.reports_enabled=enabled
            self.wake.set()

    def supply_local(self, quota):
        """Receive the collector's observation without changing its timestamp."""
        self.local_observations.put(quota)
        self.wake.set()

    def set_tracking_enabled(self, enabled):
        self.tracking_enabled=bool(enabled)
        at=time.time()
        if enabled:self.tracking_after=at
        self.tracking_changes.put((bool(enabled),at))
        self.wake.set()

    def request_exclusion(self, start, end, reason=''):
        """Queue edits for the ledger thread without requesting account data."""
        self.exclusion_changes.put(('exclude', (start, end, reason), time.monotonic()+self.EXCLUSION_TIMEOUT))
        self.wake.set()

    def restore_exclusion(self, exclusion_id):
        self.exclusion_changes.put(('restore', (exclusion_id,), time.monotonic()+self.EXCLUSION_TIMEOUT))
        self.wake.set()

    def run(self):
        ledger = None
        client = AccountClient(self.home)
        client.cache_seconds = QuotaPolling.MINIMUM
        self.client = client
        latest = direct = local = None
        polling = QuotaPolling()
        pending = deque()
        controls = deque()
        exclusions = deque()
        next_storage = next_report = next_history = 0
        next_aggregate = 0
        aggregate_failures = 0
        report_request = edit_request = None
        edit_active = False
        storage_failures = 0
        lookup_issue = database_issue = aggregation_issue = ''
        emitted_issue = None
        report = None
        stage = 'initialize'
        was_reporting=self.reports_enabled

        def fail_exclusions(permanent=False):
            """A failed user edit stops retrying; committed edits are never replayed."""
            now = time.monotonic()
            nonlocal edit_request
            while exclusions and not edit_active and (permanent or exclusions[0][2] <= now):
                operation, _, _ = exclusions.popleft()
                saved=bool(edit_request and edit_request.get('saved'))
                exclusion_results.append(dict(operation=operation, ok=False, saved=saved,
                    error=('제외 내역은 저장했지만 재집계를 완료하지 못했습니다. 기록 확인 후 자동으로 갱신합니다.'
                           if saved else '제외 내역을 저장하지 못했습니다. 잠시 뒤 다시 시도하세요.')))
                edit_request=None

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
            code = (getattr(exc, 'sqlite_errorcode', 0) or 0) & 255
            fail_exclusions(isinstance(exc, PermissionError) or code in (
                sqlite3.SQLITE_PERM, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_FULL,
                sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CANTOPEN))

        try:
            while not self.isInterruptionRequested():
                self.wake.clear()
                if self.reports_enabled!=was_reporting:
                    was_reporting=self.reports_enabled
                    if was_reporting:next_report=next_history=0
                while not self.tracking_changes.empty():
                    change = self.tracking_changes.get_nowait()
                    controls.append(change)
                    next_report = next_history = 0
                    if change[0] and not polling.failures:
                        polling.next_read = min(polling.next_read, time.monotonic())
                while not self.exclusion_changes.empty():
                    exclusions.append(self.exclusion_changes.get_nowait())
                    next_report = 0
                exclusion_results = []
                report_ready = False
                while not self.worker_results.empty():
                    computed=self.worker_results.get_nowait()
                    kind=computed['kind']
                    if kind=='report':
                        if computed['id']!=report_request:continue
                        report_request=None
                    elif edit_request is None or computed['id']!=edit_request['id']:
                        continue
                    else:
                        edit_active=False
                    if computed.get('error'):
                        aggregate_failures+=1
                        aggregation_issue=computed.get('issue') or '사용한도 계산 지연: 마지막 결과를 유지합니다.'
                        next_aggregate=time.monotonic()+min(60,5*2**min(aggregate_failures-1,4))
                        if kind!='report':
                            edit_request['saved']=bool(computed.get('saved'))
                            if computed.get('permanent'):
                                exclusions.popleft()
                                exclusion_results.append(dict(operation=kind,ok=False,
                                    saved=bool(computed.get('saved')),uncertain=bool(computed.get('uncertain')),
                                    error=('제외 내역의 저장 결과를 확인하지 못했습니다. 기록을 확인한 뒤 다시 시도하세요.'
                                           if computed.get('uncertain') else computed['error'])))
                                edit_request=None
                                if not computed.get('issue'):aggregation_issue=''
                    else:
                        report={**computed['report'],'_worker':self.report_worker}
                        aggregation_issue='';aggregate_failures=0;next_aggregate=0
                        report_ready=True
                        if kind!='report':
                            exclusions.popleft()
                            exclusion_results.append(computed['exclusion_result'])
                            edit_request=None
                fail_exclusions()
                now = time.time()
                if polling.tick(time.monotonic(), now):
                    client.close()
                    next_storage = next_report = next_history = 0
                if time.monotonic() >= next_storage:
                    try:
                        stage = 'open ledger'
                        if ledger is None:
                            ledger = QuotaLedger(self.path)
                            # Apply queued controls at their actual timestamps first.
                            if not controls:
                                tracking_store.enable(ledger.db, self.home, at=self.tracking_after,
                                                      enabled=self.tracking_enabled)
                            next_report = next_history = 0
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
                        lookup_issue = ('Codex 한도 조회 지연: 자동 재시도 중' if isinstance(exc, (TimeoutError, ConnectionError))
                                        else 'Codex 한도 조회 실패: 자동 재시도 중')
                        if self.tracking_enabled:
                            pending.append(('failure', time.time()))
                        polling.finish(time.monotonic(), False)
                    next_report = 0
                while not self.local_observations.empty():
                    quota = self.local_observations.get()
                    if quota and (local is None or quota['observed_at'] > local['observed_at']):
                        local = {**quota, 'source': 'local', 'max_age': 120}
                        if self.tracking_enabled:
                            pending.append(('local', local))
                        next_report = 0
                publish = (self.reports_enabled and (report is None or time.monotonic()>=next_history)
                           or time.monotonic()>=next_report or report_ready)
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
                        database_issue = ''
                        storage_failures = 0
                    except Exception as exc:
                        storage_failed(exc)
                        publish = True
                # A slow history rebuild never postpones the next account lookup
                # or observation save. Only small requests cross this boundary.
                if ledger is not None and time.monotonic()>=next_aggregate:
                    if exclusions and not edit_active:
                        if edit_request is None:
                            operation,arguments,_=exclusions[0]
                            payload=(dict(start=arguments[0],end=arguments[1],reason=arguments[2])
                                     if operation=='exclude' else dict(exclusion_id=arguments[0]))
                            identifier=self.report_worker.request(operation,**payload)
                            edit_request=dict(kind=operation,id=identifier,key=None,**payload)
                        else:
                            self.report_worker.retry(edit_request)
                        edit_active=True
                    elif (self.reports_enabled and report_request is None and not exclusions
                          and (report is None or time.monotonic()>=next_history)):
                        report_request=self.report_worker.request('report')
                        next_history=time.monotonic()+5
                latest = select_current_quota(direct, local, time.time())
                issue = ', '.join(v for v in (lookup_issue, database_issue, aggregation_issue) if v)
                if publish or issue != emitted_issue or exclusion_results:
                    value={'home':self.home,'quota':latest,'issue':issue}
                    if self.reports_enabled or exclusion_results:
                        if report is not None:
                            value['report']=report
                        elif database_issue or aggregation_issue:
                            value['report']={'home':self.home,'cycles':[],'error':True,'index_loading':False}
                    for result in exclusion_results or [None]:
                        self.updated.emit({**value, 'exclusion_result':result} if result else value)
                    emitted_issue = issue
                    next_report = time.monotonic()+5
                self.wake.wait(1)
        finally:
            self.report_worker.close()
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
