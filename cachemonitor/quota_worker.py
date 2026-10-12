"""Quota accounting in a spawned process, with bounded, coalesced delivery."""
from collections import OrderedDict, deque
import multiprocessing as mp
import hashlib
import pickle
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from PySide6.QtCore import QObject, Signal
from .quota_transport import TransferCancelled, receive_result, send_result


def compact_summary(summary):
    """Keep display fields, without duplicating the underlying call graph."""
    return {**summary, 'intervals': []}


def compact_report(report, revision):
    from .quota_view import prepare_quota_query
    view = report['view']
    summaries = {}
    prepared = {}
    for key, summary in view.get('summaries', {}).items():
        summaries[key] = prepared.setdefault(id(summary), compact_summary(summary))
    lifetime = prepared.get(id(view['lifetime'])) or compact_summary(view['lifetime'])
    def series(value):
        return {**value, '_content_digest': hashlib.sha256(pickle.dumps(value, protocol=5)).hexdigest()}
    prepared_view = {**view, 'lifetime': lifetime, 'summaries': summaries,
        'default_query': prepare_quota_query(report, {}), 'overall': series(view['overall']),
        'periods': [{**period, 'series': series(period['series'])} for period in view['periods']],
        'five_hour': series(view['five_hour'])}
    return {**{key: report[key] for key in ('home', 'account', 'at', 'index_loading', 'error', 'exclusions')
               if key in report},
            'worker_revision': revision,
            'view': prepared_view}


class QuotaComputation:
    """Own database connections and raw reports only inside the child process."""
    def __init__(self, path=None, home=None, ledger_factory=None):
        self.path, self.home = path, home
        self.ledger_factory = ledger_factory
        self.ledger = None
        self.ledger_generation = 0
        self.reports = OrderedDict()
        self.committed = {}

    def _ledger(self):
        if self.ledger is None:
            from .quota_cycles import QuotaLedger
            self.ledger = (self.ledger_factory or QuotaLedger)(self.path)
            self.ledger_generation += 1
        return self.ledger

    def _prepare(self, report, history_revision=None):
        from .quota_view import prepare_quota_view
        report=dict(report)
        if history_revision is not None:report['_history_revision']=history_revision
        previous=next(reversed(self.reports.values()),None) if self.reports else None
        if (previous and report.get('_history_revision') is not None and
                previous.get('_history_revision')==report['_history_revision']):
            from .quota_cycles import quota_statistics
            from .quota_attribution import RequestWindows
            view=previous['view'];summaries={None:view['lifetime']}
            earliest=min((cycle['start'] for cycle in report.get('cycles',[])),default=report['at'])
            requests=RequestWindows(report.get('request_windows',[]))
            for days in (7,30,90):
                start=report['at']-days*86400
                summaries[days]=(view['lifetime'] if start<=earliest else
                    quota_statistics(report,start,include_mode_assumptions=True,_requests=requests))
            report['view']={**view,'summaries':summaries}
        else:report['view'] = prepare_quota_view(report)
        revision = uuid4().hex
        self.reports[revision] = report
        while len(self.reports) > 2:
            self.reports.popitem(last=False)
        return compact_report(report, revision)

    def execute(self, request, phase=lambda value: None):
        kind = request['kind']
        result = {key: request.get(key) for key in ('kind', 'id', 'key')}
        try:
            if kind == 'prepare':
                result['report'] = self._prepare(request['snapshot'])
            elif kind in ('report', 'exclude', 'restore'):
                ledger = self._ledger()
                if kind in ('exclude', 'restore'):
                    token = request['id']
                    if token not in self.committed:
                        phase('writing')
                        if kind == 'exclude':
                            record = ledger.exclude_interval(self.home, request['start'], request['end'], request.get('reason', ''))
                            identifier = record['id']
                        else:
                            identifier = request['exclusion_id']
                            ledger.restore_exclusion(self.home, identifier)
                        self.committed[token] = dict(operation=kind, ok=True, id=identifier)
                    result['saved'] = True
                    result['exclusion_result'] = self.committed[token]
                    phase('reading')
                raw=ledger.report(self.home)
                generation=getattr(ledger,'_report_generation',None)
                result['report'] = self._prepare(raw,(self.ledger_generation,generation) if generation is not None else None)
            elif kind in ('preview', 'query'):
                report = self.reports.get(request['revision'])
                if report is None:
                    result.update(stale=True, error='사용한도 기록이 갱신되었습니다.')
                    return result
                if kind == 'preview':
                    from .quota_exclusions import preview_exclusion
                    result['value'] = preview_exclusion(report, request['start'], request['end'])
                else:
                    from .quota_view import prepare_quota_query
                    result['value'] = prepare_quota_query(report, request['query'])
            else:
                raise ValueError('Unknown quota request: ' + str(kind))
        except Exception as error:
            from .quota_diagnostics import record_failure, storage_issue
            if self.path:
                record_failure(self.path, 'quota ' + kind, error)
            code = (getattr(error, 'sqlite_errorcode', 0) or 0) & 255
            permanent = isinstance(error, (ValueError, PermissionError)) or code in (
                sqlite3.SQLITE_PERM, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_FULL,
                sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB, sqlite3.SQLITE_CANTOPEN)
            result.update(error=str(error) or type(error).__name__, issue='' if isinstance(error, ValueError) else storage_issue(error), permanent=permanent,
                          saved=request['id'] in self.committed)
            if self.ledger is not None:
                try:
                    self.ledger.db.rollback()
                finally:
                    self.ledger.close()
                    self.ledger = None
        return result

    def close(self):
        if self.ledger is not None:
            self.ledger.close()
            self.ledger = None


def process_main(connection, path, home):
    computation = QuotaComputation(path, home)
    try:
        while True:
            request = connection.recv()
            if request['kind'] == 'stop':
                return
            def phase(value):
                connection.send(dict(kind='phase', value=value))
            send_result(connection, computation.execute(request, phase))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        computation.close()
        connection.close()


class QuotaWorker(QObject):
    """Only the transport thread serializes requests or receives Python objects."""
    result = Signal(object)
    COALESCED = frozenset(('report', 'prepare', 'preview', 'query'))
    WRITING = frozenset(('pending-write', 'writing'))

    def __init__(self, path=None, home=None, *, on_result=None, process_target=None):
        super().__init__()
        self.path = str(Path(path).resolve()) if path is not None else None
        self.home = home
        self.on_result = on_result
        self.process_target = process_target or process_main
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closing = threading.Event()
        self.pending = OrderedDict()
        self.commands = deque()
        self.latest = {}
        self.sequence = 0
        self.thread = None
        self.process = None
        self.phase = 'idle'
        self.restart_count = 0

    def request(self, kind, key=None, **payload):
        with self.lock:
            if self.closing.is_set():
                return None
            self.sequence += 1
            request = dict(kind=kind, id=self.sequence, key=key, **payload)
            self._queue(request)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='quota-transport', daemon=True)
                self.thread.start()
        self.wake.set()
        return request['id']

    def retry(self, request):
        """A known failed commit uses the same id, so it cannot be duplicated."""
        with self.lock:
            if self.closing.is_set():
                return
            self._queue(request)
        self.wake.set()

    def _queue(self, request):
        kind = request['kind']
        if kind in self.COALESCED:
            self.pending[kind] = request
            self.latest[kind] = request['id']
        else:
            self.commands.append(request)

    def _next(self):
        with self.lock:
            if self.commands:
                return self.commands.popleft()
            if self.pending:
                _, request = self.pending.popitem(last=False)
                return request
        return None

    def _deliver(self, result):
        with self.lock:
            if result['kind'] in self.COALESCED and self.latest.get(result['kind']) != result['id']:
                return
        if self.closing.is_set():
            return
        if self.on_result:
            self.on_result(result)
        self.result.emit(result)

    def _run(self):
        connection = None
        active = None
        try:
            while not self.closing.is_set():
                if active is None:
                    active = self._next()
                if active is None:
                    self.wake.wait(.05)
                    self.wake.clear()
                    continue
                try:
                    if connection is None:
                        if self.restart_count > 2:
                            raise RuntimeError('사용한도 계산 프로세스를 시작할 수 없습니다.')
                        context = mp.get_context('spawn')
                        connection, child = context.Pipe()
                        self.process = context.Process(target=self.process_target,
                            args=(child, self.path, self.home), daemon=True)
                        try:
                            self.process.start()
                        finally:
                            child.close()
                    # Protect the commit before the child can publish its first
                    # phase message. Closing immediately after send is common.
                    self.phase = 'pending-write' if active['kind'] in ('exclude','restore') else 'reading'
                    connection.send(active)
                    while True:
                        if connection.poll(.02):
                            result = receive_result(connection,
                                lambda:self.closing.is_set() and self.phase not in self.WRITING)
                            if result['kind'] == 'phase':
                                self.phase = result['value']
                                continue
                            self.phase = 'idle'
                            self._deliver(result)
                            active = None
                            break
                        if not self.process.is_alive():
                            raise EOFError('사용한도 계산 프로세스가 종료되었습니다.')
                        if self.closing.is_set() and self.phase not in self.WRITING:
                            return
                except TransferCancelled:
                    return
                except (EOFError, BrokenPipeError, OSError, RuntimeError) as error:
                    if active is not None:
                        self._deliver({key: active.get(key) for key in ('kind', 'id', 'key')} | dict(
                            error=str(error) or type(error).__name__, issue='사용한도 계산 지연: 마지막 결과를 유지합니다.',
                            permanent=active['kind'] in ('exclude', 'restore'), uncertain=active['kind'] in ('exclude', 'restore')))
                    active = None
                    if connection is not None:
                        connection.close()
                        connection = None
                    if self.process is not None and self.process.pid is not None:
                        self.process.join(.1)
                        if self.process.is_alive():
                            if self.phase not in self.WRITING:
                                self.process.terminate()
                                self.process.join(.5)
                            else:
                                # A broken transport cannot establish whether a
                                # commit finished. Closing the pipe asks the child
                                # to exit after that operation; retain a reaper.
                                threading.Thread(target=self.process.join,
                                    name='quota-commit-reaper',daemon=True).start()
                    self.restart_count += 1
        finally:
            if connection is not None:
                try:
                    connection.send({'kind': 'stop'})
                except (EOFError, BrokenPipeError, OSError):
                    pass
            process = self.process
            if process is not None and process.pid is not None:
                process.join(.2)
                if process.is_alive() and self.phase not in self.WRITING:
                    process.terminate()
                    process.join(.5)
            if connection is not None:
                connection.close()

    def close(self, timeout=2):
        self.closing.set()
        self.wake.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        return self.thread is None or not self.thread.is_alive()
