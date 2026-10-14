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


def chart_result(view, selection=None, known_digest=None):
    """Transfer one exact chart, or its identity when the receiver already has it."""
    periods = view['periods']
    if selection != 'all' and not any(p['start'] == selection for p in periods):
        selection = periods[0]['start'] if periods else 'all'
    if selection=='all' and view['overall'] is None:
        from .quota_view import prepare_overall
        view['overall']=prepare_overall(view['_overall_parts'],view['_exclusions'])
    source = view['overall'] if selection == 'all' else next(p['series'] for p in periods if p['start'] == selection)
    digest = source.get('_content_digest')
    if digest is None:
        digest = hashlib.sha256(pickle.dumps(source, protocol=5)).hexdigest()
        source['_content_digest'] = digest
    return dict(selection=selection, digest=digest,
                series=None if digest == known_digest else source)


def compact_report(report, revision, selection=None, known_digest=None):
    from .quota_view import prepare_quota_query
    view = report['view']
    summaries = {}
    prepared = {}
    for key, summary in view.get('summaries', {}).items():
        summaries[key] = prepared.setdefault(id(summary), compact_summary(summary))
    lifetime = prepared.get(id(view['lifetime'])) or compact_summary(view['lifetime'])
    prepared_view = {key:value for key,value in view.items() if key not in ('overall','periods','five_hour') and not key.startswith('_')}
    prepared_view.update(lifetime=lifetime, summaries=summaries,
        has_history=any(p['series']['rows'] for p in view['periods']),
        chart=chart_result(view, selection, known_digest),
        periods=[{key:value for key,value in period.items() if key != 'series'} for period in view['periods']],
        default_query=prepare_quota_query(report, {}))
    prepared_view.update({
        'tracking_started': (report.get('tracking') or {}).get('started'),
    })
    return {**{key: report[key] for key in ('home', 'account', 'at', 'index_loading', 'error', 'exclusions', 'cycle_clears')
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

    def _prepare(self, report, history_revision=None, selection=None, known_digest=None):
        from .quota_view import prepare_quota_view
        report=dict(report)
        if history_revision is not None:report['_history_revision']=history_revision
        previous=next(reversed(self.reports.values()),None) if self.reports else None
        if (previous and report.get('_history_revision') is not None and
                previous.get('_history_revision')==report['_history_revision']):
            from .quota_cycles import quota_statistics
            from .quota_attribution import RequestWindows
            view=previous['view'];summaries={None:view['lifetime']}
            # Unchanged history only reads the bounded cache; a rolling-window
            # scan must not evict later hits to admit earlier misses.
            statistics_cache=view.get('_statistics_cache',{})
            earliest=min((cycle['start'] for cycle in report.get('cycles',[])),default=report['at'])
            requests=RequestWindows(report.get('request_windows',[]))
            for days in (7,30,90):
                start=report['at']-days*86400
                summaries[days]=(view['lifetime'] if start<=earliest else
                    quota_statistics(report,start,include_mode_assumptions=True,_requests=requests,
                                     _cache=statistics_cache,_cache_readonly=True))
            report['view']={**view,'summaries':summaries,'_statistics_cache':statistics_cache}
        else:report['view'] = prepare_quota_view(report,previous['view'] if previous else None,deferred=True)
        revision = uuid4().hex
        self.reports[revision] = report
        while len(self.reports) > 2:
            self.reports.popitem(last=False)
        return compact_report(report, revision, selection, known_digest)

    def execute(self, request, phase=lambda value: None):
        kind = request['kind']
        result = {key: request.get(key) for key in ('kind', 'id', 'key')}
        try:
            if kind == 'prepare':
                result['report'] = self._prepare(request['snapshot'])
            elif kind in ('report', 'exclude', 'restore', 'clear_cycle'):
                ledger = self._ledger()
                if kind in ('exclude', 'restore', 'clear_cycle'):
                    token = request['id']
                    if token not in self.committed:
                        phase('writing')
                        if kind == 'exclude':
                            record = ledger.exclude_interval(self.home, request['start'], request['end'], request.get('reason', ''))
                            identifier = record['id']
                        elif kind=='clear_cycle':
                            record=ledger.clear_selected_cycle(self.home,request['start'],request['account'],
                                                               delete=request.get('delete',False))
                            identifier=str(record['end'])
                        else:
                            identifier = request['exclusion_id']
                            ledger.restore_exclusion(self.home, identifier)
                        self.committed[token] = dict(operation=kind, ok=True, id=identifier)
                        if kind=='clear_cycle':self.committed[token]['deleted']=request.get('delete',False)
                    result['saved'] = True
                    result['exclusion_result'] = self.committed[token]
                    phase('reading')
                raw=ledger.report(self.home)
                generation=getattr(ledger,'_report_generation',None)
                result['report'] = self._prepare(raw,(self.ledger_generation,generation) if generation is not None else None,
                                                request.get('selection'),request.get('digest'))
            elif kind in ('preview', 'query', 'chart'):
                report = self.reports.get(request['revision'])
                if report is None:
                    result.update(stale=True, error='사용한도 기록이 갱신되었습니다.')
                    return result
                if kind == 'chart':
                    result['value'] = chart_result(report['view'], request.get('selection'), request.get('digest'))
                elif kind == 'preview':
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
    COALESCED = frozenset(('report', 'prepare', 'preview', 'query', 'chart'))
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
        self.cancel_report_before = 0
        self.restart_count = 0
        self.chart_selection = None
        self.chart_digest = None

    def set_paused(self, paused):
        with self.lock:
            if paused:self.cancel_report_before = self.sequence
        self.wake.set()

    def select_chart(self, selection, digest=None):
        with self.lock:
            self.chart_selection, self.chart_digest = selection, digest

    def request(self, kind, key=None, **payload):
        with self.lock:
            if self.closing.is_set():
                return None
            self.sequence += 1
            if kind in ('report','exclude','restore','clear_cycle'):
                payload.update(selection=self.chart_selection,digest=self.chart_digest)
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
        def release():
            nonlocal connection
            if connection is not None:
                try:connection.send({'kind':'stop'})
                except (EOFError,BrokenPipeError,OSError):pass
                connection.close();connection=None
            process=self.process
            if process is not None and process.pid is not None:
                process.join(.2)
                if process.is_alive():process.terminate();process.join(.5)
            self.process=None
        try:
            while not self.closing.is_set():
                if active is None:
                    active = self._next()
                if active is None:
                    self.wake.wait()
                    self.wake.clear()
                    continue
                if active['kind']=='report' and active['id']<=self.cancel_report_before:
                    self._deliver({key:active.get(key) for key in ('kind','id','key')} | dict(cancelled=True))
                    active=None
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
                    self.phase = 'pending-write' if active['kind'] in ('exclude','restore','clear_cycle') else 'reading'
                    connection.send(active)
                    while True:
                        if active['kind']=='report' and active['id']<=self.cancel_report_before:
                            release();self.phase='idle'
                            self._deliver({key:active.get(key) for key in ('kind','id','key')} | dict(cancelled=True))
                            active=None
                            break
                        if connection.poll(.02):
                            result = receive_result(connection,
                                lambda:(self.closing.is_set() and self.phase not in self.WRITING) or
                                (active['kind']=='report' and active['id']<=self.cancel_report_before))
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
                    if self.closing.is_set():return
                    release();self.phase='idle'
                    self._deliver({key:active.get(key) for key in ('kind','id','key')} | dict(cancelled=True))
                    active=None
                except (EOFError, BrokenPipeError, OSError, RuntimeError) as error:
                    if active is not None:
                        self._deliver({key: active.get(key) for key in ('kind', 'id', 'key')} | dict(
                            error=str(error) or type(error).__name__, issue='사용한도 계산 지연: 마지막 결과를 유지합니다.',
                            permanent=active['kind'] in ('exclude', 'restore','clear_cycle'), uncertain=active['kind'] in ('exclude', 'restore','clear_cycle')))
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
