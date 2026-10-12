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
from .quota_backlog import ObservationBacklog


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
        self.ingress_lock=threading.Lock()
        self.ingress_closed=False
        self.tracking_revision=0
        self.local_observed_at=None
        self.observation_issue=''
        self.control_issue=''
        self.backlog=ObservationBacklog(self.path,self.home)
        if not self.backlog.append('control',(self.tracking_enabled,self.tracking_after)):
            self.control_issue=self.backlog.issue or '추적 설정을 저장하지 못했습니다. 설정을 다시 적용하세요.'
        self.backlog_closed=False
        self.local_observations=queue.Queue(maxsize=1)
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
        """Persist each new observation before replacing the displayed gauge."""
        with self.ingress_lock:
            if self.ingress_closed:return False
            if not quota or (self.local_observed_at is not None and quota['observed_at']<=self.local_observed_at):
                return True
            local={**quota,'source':'local','max_age':120}
            if self.tracking_enabled and not self.backlog.append('local',local):
                self.observation_issue=self.backlog.issue or '한도 관측을 저장하지 못했습니다. 앱을 다시 열어 재시도하세요.'
                self.wake.set()
                return False
            self.local_observed_at=quota['observed_at']
            try:self.local_observations.get_nowait()
            except queue.Empty:pass
            self.local_observations.put_nowait(local)
        self.wake.set()
        return True

    def set_tracking_enabled(self, enabled):
        with self.ingress_lock:
            if self.ingress_closed:return False
            enabled=bool(enabled)
            at=time.time()
            if not self.backlog.append('control',(enabled,at)):
                self.control_issue=self.backlog.issue or '추적 설정을 저장하지 못했습니다. 설정을 다시 적용하세요.'
                self.wake.set()
                return False
            self.control_issue=''
            self.tracking_enabled=enabled
            if enabled:self.tracking_after=at
            self.tracking_revision+=1
        self.wake.set()
        return True

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
        pending = self.backlog
        tracking_revision=self.tracking_revision
        activity_summary = tracking_store.ActivitySummary()
        exclusions = deque()
        next_storage = next_report = next_history = 0
        next_aggregate = 0
        aggregate_failures = 0
        report_request = edit_request = None
        edit_active = False
        storage_failures = 0
        lookup_issue = database_issue = aggregation_issue = ''
        lookup_reason='';database_fault={};calculation_fault={}
        emitted_issue = None;emitted_faults=None
        report = None
        stage = 'initialize'
        was_reporting=self.reports_enabled

        def save_pending():
            # Bound recovery work independently of the retained backlog. An ack
            # follows committed effects; replay after an interrupted ack is safe.
            deadline = time.monotonic()+.05
            for _ in range(pending.BATCH_SIZE):
                entry = pending.first()
                if entry is None:break
                sequence, kind, value = entry
                if kind == 'control':
                    enabled, at = value
                    tracking_store.enable(ledger.db, self.home, at=at, enabled=enabled)
                elif kind == 'failure':
                    tracking_store.failed_observation(ledger.db, self.home, value)
                else:
                    ledger.observe(self.home, value)
                    if kind == 'quota':tracking_store.observe(ledger.db, self.home, value)
                pending.acknowledge(sequence)
                if time.monotonic() >= deadline:break

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
            nonlocal ledger, database_issue, next_storage, storage_failures, database_fault
            record_failure(self.path, stage, exc)
            database_issue = storage_issue(exc)
            from .app_errors import exception_facts
            sqlite_code=(getattr(exc,'sqlite_errorcode',0) or 0)&255
            database_fault=dict(storage_failed=True,
                temporary=sqlite_code in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED),
                **exception_facts(exc))
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
                with self.ingress_lock:
                    current_tracking_revision=self.tracking_revision
                    tracking_enabled=self.tracking_enabled
                if tracking_revision!=current_tracking_revision:
                    tracking_revision=current_tracking_revision
                    next_report = next_history = 0
                    if tracking_enabled and not polling.failures:
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
                        calculation_fault=dict(calculation_failed=True,saved=bool(computed.get('saved')),
                            uncertain=bool(computed.get('uncertain')))
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
                        calculation_fault={}
                        report_ready=True
                        if kind!='report':
                            # Preserve an immediate refresh requested by changed settings.
                            if next_history:next_history=time.monotonic()+5
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
                            next_report = next_history = 0
                        stage = 'recover observations'
                        save_pending()
                        control = ledger.db.execute(
                            'select at from tracking_controls where home=? order by at desc limit 1',
                            (self.home,)).fetchone()
                        if control:self.tracking_after = max(self.tracking_after, control[0])
                        stage = 'activity'
                        # Old interrupted tasks can lack an end forever. Use recent
                        # activity for polling speed without altering their history.
                        signature = activity_summary.read(ledger.db, self.home, now-300)
                        activity, wire = signature[:4], signature[4:]
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
                with self.ingress_lock:
                    tracking_at_request=self.tracking_enabled
                    can_observe = not (self.observation_issue or self.control_issue) and (not tracking_at_request or pending.available())
                if self.live and can_observe and polling.ready(time.monotonic()):
                    try:
                        direct = client.fetch()
                        if tracking_at_request:
                            with self.ingress_lock:
                                if not pending.append('quota', direct):
                                    self.observation_issue=pending.issue or '한도 관측을 저장하지 못했습니다. 앱을 다시 열어 재시도하세요.'
                        lookup_issue = ''
                        lookup_reason=''
                        polling.finish(time.monotonic(), True, direct, time.time())
                    except Exception as exc:
                        record_failure(self.path, 'account lookup', exc)
                        lookup_reason=getattr(exc,'code','timeout' if isinstance(exc,TimeoutError) else 'lookup')
                        lookup_issue = ('Codex 한도 조회 지연: 자동 재시도 중' if isinstance(exc, (TimeoutError, ConnectionError))
                                        else 'Codex 한도 조회 실패: 자동 재시도 중')
                        if tracking_at_request:
                            with self.ingress_lock:
                                if not pending.append('failure', time.time()):
                                    self.observation_issue=pending.issue or '한도 조회 실패 기록을 저장하지 못했습니다. 앱을 다시 열어 재시도하세요.'
                        polling.finish(time.monotonic(), False)
                    next_report = 0
                while True:
                    try:quota = self.local_observations.get_nowait()
                    except queue.Empty:break
                    if quota and (local is None or quota['observed_at'] > local['observed_at']):
                        local = {**quota, 'source': 'local', 'max_age': 120}
                        next_report = 0
                publish = (self.reports_enabled and (report is None or time.monotonic()>=next_history)
                           or time.monotonic()>=next_report or report_ready)
                if ledger is not None:
                    try:
                        stage = 'save observations'
                        save_pending()
                        database_issue = ''
                        database_fault={}
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
                issue = ', '.join(dict.fromkeys(v for v in (lookup_issue, database_issue, aggregation_issue,
                    pending.issue, self.observation_issue, self.control_issue) if v))
                faults=dict(lookup=dict(lookup_failed=bool(lookup_issue),reason=lookup_reason),
                    storage=database_fault or dict(storage_failed=bool(pending.issue or self.observation_issue or self.control_issue)),
                    calculation=dict(calculation_fault))
                if publish or issue != emitted_issue or faults!=emitted_faults or exclusion_results:
                    value={'home':self.home,'quota':latest,'issue':issue,'quota_faults':faults,'report_refreshed':report_ready}
                    if self.reports_enabled or exclusion_results:
                        if report is not None:
                            value['report']=report
                        elif database_issue or aggregation_issue:
                            value['report']={'home':self.home,'cycles':[],'error':True,'index_loading':False}
                    for result in exclusion_results or [None]:
                        self.updated.emit({**value, 'exclusion_result':result} if result else value)
                    emitted_issue = issue;emitted_faults=faults
                    next_report = time.monotonic()+5
                self.wake.wait(1)
        finally:
            with self.ingress_lock:self.ingress_closed=True
            self.report_worker.close()
            client.close()
            if ledger:
                ledger.close()
            with self.ingress_lock:
                saved=pending.close()
                self.backlog_closed=True
            if not saved:
                record_failure(self.path, 'pending observations shutdown', OSError('uncommitted quota observation'))
                self.updated.emit({'home':self.home,'quota':latest,'issue':pending.issue})

    def stop(self):
        with self.ingress_lock:self.ingress_closed=True
        self.requestInterruption()
        self.wake.set()
        client = getattr(self,'client',None)
        process = getattr(client,'process',None)
        if process and process.poll() is None:
            try: process.terminate()
            except OSError: pass
        self.wait()
        with self.ingress_lock:
            if not self.backlog_closed:
                self.backlog.close()
                self.backlog_closed=True
