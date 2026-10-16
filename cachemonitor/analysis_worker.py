"""Analyze shared collector snapshots outside Qt's UI thread."""
import multiprocessing as mp
from multiprocessing.connection import wait
import threading
import time
import traceback
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from bisect import bisect_left
from PySide6.QtCore import QThread, Signal
from .analysis_engine import AnalysisEngine
from .analytics import _local_timestamp
from .usage_collection import CollectionClient
from .quota_cycles import QuotaLedger, ledger_path
from .overlay_data import OverlaySummaries
from .analysis_delivery import SnapshotPublisher, SnapshotReceiver
from .pricing import pricing_signature


def expiry(engine,q):
    now=q.get('now',q['end'])
    candidates=[float('inf')]
    duration=1800 if q.get('period')=='30m' else None
    if q.get('period') in ('today','7d','30d') or q['page']==0:
        candidates.append(_local_timestamp(datetime.combine(datetime.fromtimestamp(now).date()+timedelta(days=1),datetime.min.time())))
    # Expiry needs only the next boundary, not a separately sorted global
    # timeline rebuilt whenever any session changes.
    for state in engine.sessions.values():
        for times in (state['timestamps'],state.get('boundaries',())):
            if duration:
                i=bisect_left(times,q['start'])
                if i<len(times):candidates.append(times[i]+duration+.002)
            if q.get('period')!='custom':
                i=bisect_left(times,q['end'])
                if i<len(times):candidates.append(times[i]+.002)
    return min(candidates)


def process_main(connection,homes,index_path,static_snapshot=None,model_evidence_path=None,quota_path=None):
    quota_path=quota_path or ledger_path(index_path)
    collector=None
    ledger=None
    ledger_issue=None
    ledger_retry=0
    ledger_failures=0
    imported=False
    import_started=False
    next_import=0
    last_slow_report=float('-inf')
    diagnostic_path=Path(quota_path).with_suffix('.analysis.sqlite')
    def record_timing(stages,source_age=0):
        nonlocal last_slow_report
        from .quota_diagnostics import record_slow_cycle
        last_slow_report=record_slow_cycle(diagnostic_path,stages,source_age,last_slow_report)
    def disable_ledger(error):
        nonlocal ledger,ledger_issue,ledger_retry,ledger_failures
        from .quota_diagnostics import record_failure, storage_issue
        record_failure(quota_path, 'usage ledger sync', error)
        ledger_issue=storage_issue(error)
        ledger_failures+=1
        ledger_retry=time.monotonic()+min(60,5*2**min(ledger_failures-1,4))
        if ledger is not None:
            try:ledger.db.rollback()
            except (sqlite3.Error,AttributeError):pass
            try:ledger.close()
            except sqlite3.Error:pass
        ledger=None
    try:
        engine=AnalysisEngine()
        overlays=OverlaySummaries()
        publisher=SnapshotPublisher()
        analysis_identity=None
        snapshot=None
        frozen=static_snapshot is not None
        next_poll=0
        pending=None
        ready=None
        notification=None
        if static_snapshot is not None:
            snapshot=static_snapshot
            engine.ingest(snapshot['sessions'])
        else:
            collector=CollectionClient(homes,index_path,model_evidence_path)
            try:ledger=QuotaLedger(quota_path)
            except sqlite3.Error as error:disable_ledger(error)
        def publish(heartbeat=False):
            light=(publisher.heartbeat(snapshot,engine) if heartbeat else
                   publisher.publish(snapshot,engine,overlays.collect(engine)))
            connection.send({'kind':'snapshot','value':light})
        def import_history():
            nonlocal imported,next_import,ledger_issue,ledger_failures
            started=time.monotonic()
            try:
                if hasattr(ledger,'import_observations_step'):
                    imported=ledger.import_observations_step(collector.path,snapshot['homes'])
                else:
                    # Compatibility for external ledger adapters.
                    for home in snapshot['homes']:ledger.import_observations(collector.path,home)
                    imported=True
            except sqlite3.Error as error:
                disable_ledger(error)
            if ledger and imported:
                ledger_issue=None;ledger_failures=0
            next_import=time.monotonic()+.05
            record_timing({'history_import':time.monotonic()-started})
        details={}
        def send_details(all_pending=True):
            while details:
                detail_started=time.monotonic()
                message=details.pop(next(iter(details)))
                if message['kind']=='population':
                    from .dashboard_views import population_summary
                    connection.send(dict(kind='population',id=message['id'],
                        summary=population_summary(engine,message['population'],message.get('assumptions'))))
                else:
                    connection.send({'kind':'record','id':message['id'],
                                     'row':engine.record(*message['identity'])})
                record_timing({'detail':time.monotonic()-detail_started})
                if not all_pending:break
        if snapshot is not None: publish()
        while True:
            while connection.poll():
                message=connection.recv()
                if message['kind']=='stop': return
                # One replaceable request per detail consumer, within ordered
                # snapshot/control segments. Never move work across a barrier.
                if message['kind'] in ('freeze','replace','sync'):send_details()
                if message['kind']=='freeze':
                    frozen=True
                    if collector:collector.stop_notifications()
                    notification=None
                elif message['kind']=='replace':
                    analysis_identity=None
                    ready=None;snapshot=message['snapshot']; engine.ingest(snapshot['sessions']); publish()
                elif message['kind']=='query': pending=message;ready=None
                elif message['kind']=='sync':
                    publisher=SnapshotPublisher()
                    if snapshot is not None:publish()
                elif message['kind'] in ('population','record'):
                    details.pop(message['kind'],None)
                    details[message['kind']]=message
            send_details(False)
            if ready is not None:
                connection.send(ready);ready=None
            if not frozen and time.monotonic()>=next_poll and (snapshot is None or pending is None or time.monotonic()>=next_poll+1):
                if ledger is None and time.monotonic()>=ledger_retry:
                    try:ledger=QuotaLedger(quota_path)
                    except sqlite3.Error as error:disable_ledger(error)
                if ledger is not None and hasattr(ledger,'activity_cursor') and not getattr(ledger,'_activity_bound',False):
                    try:
                        cursor=ledger.activity_cursor()
                        if cursor:collector.channel.activity_ack=cursor
                        ledger._activity_bound=True
                    except sqlite3.Error as error:disable_ledger(error)
                stages={};stage_started=time.monotonic()
                def completed(stage):
                    nonlocal stage_started
                    at=time.monotonic();stages[stage]=at-stage_started;stage_started=at
                snapshot=collector.poll()
                completed('collection_read')
                mode_identity=None
                if ledger:
                    try:mode_identity=ledger.enrich_modes(snapshot)
                    except sqlite3.Error as error:disable_ledger(error)
                completed('mode_evidence')
                catalog_identity=getattr(collector,'analysis_identity',None)
                identity=((catalog_identity,mode_identity,pricing_signature())
                          if catalog_identity is not None and mode_identity is not None else None)
                reusable=identity is not None and identity==analysis_identity
                # The committed catalog covers owner state except remaining,
                # which publication omits. Envelope clocks and ledger duties
                # still consume the newly read snapshot on every heartbeat.
                if not reusable:engine.ingest(snapshot['sessions'])
                analysis_identity=identity
                completed('analysis')
                if ledger:
                    try:
                        ledger.sync(engine,snapshot)
                        if snapshot.get('activity_cursor'):collector.channel.acknowledge_activity(snapshot)
                    except sqlite3.Error as error:disable_ledger(error)
                completed('quota_sync')
                if ledger and not imported and not snapshot['index']['loading']:
                    # Verified session usage is independent of historical quota
                    # recovery. Leave earlier ledger faults unresolved until it ends.
                    if snapshot['homes'] and not import_started:
                        snapshot={**snapshot,'ledger_status_known':False}
                        publish()
                        completed('initial_publication')
                    import_started=True
                    import_history()
                    completed('history_import')
                if ledger and imported:
                    ledger_issue=None
                    ledger_failures=0
                # The quota ledger is independent of observed per-call usage.
                # Keep its diagnostics without turning valid session values into
                # missing data or replacing/repairing the user's quota history.
                snapshot={**snapshot,'ledger_status_known':imported or ledger is None,
                    'ledger_fault':dict(ledger_failed=bool(ledger_issue))}
                if ledger_issue:snapshot={**snapshot,'ledger_error':ledger_issue}
                publish(heartbeat=reusable)
                completed('publication')
                record_timing(stages,max(0,time.time()-snapshot['ts']))
                notification=collector.notification_connection()
                delay=.1 if snapshot['index']['loading'] else 3
                if notification is not None and not snapshot['index']['loading']:
                    age=time.time()-snapshot['ts']
                    delay=3 if age>30 else min(10,max(.01,30-age+.01))
                if ledger is None:delay=min(delay,max(.01,ledger_retry-time.monotonic()))
                next_poll=time.monotonic()+delay
            if pending is not None and snapshot is not None:
                request=pending; pending=None
                query_started=time.monotonic()
                try:
                    result=engine.page_query(request['query'])
                    ready={'kind':'result','id':request['id'],'logical':request['logical'],
                           'result':result,'valid_until':result.get('valid_until',expiry(engine,request['query'])),
                           'metrics':dict(engine.metrics)}
                    continue
                except Exception:
                    connection.send({'kind':'error','id':request['id'],'error':traceback.format_exc(limit=6)})
                finally:
                    record_timing({'query':time.monotonic()-query_started})
            if (not frozen and ledger is not None and not imported and snapshot is not None
                    and not snapshot['index']['loading'] and time.monotonic()>=next_import):
                import_history()
                if imported or ledger is None:
                    snapshot={**snapshot,'ledger_status_known':True,
                              'ledger_fault':dict(ledger_failed=bool(ledger_issue))}
                    if ledger_issue:snapshot={**snapshot,'ledger_error':ledger_issue}
                    else:snapshot.pop('ledger_error',None)
                    publish(heartbeat=True)
            # Incoming UI requests wake this wait immediately. Only collection
            # needs a timer; an idle analyzer does not need 50 wakeups a second.
            if details:continue
            delay=1. if frozen else max(0.,next_poll-time.monotonic())
            if not frozen and ledger is not None and not imported and import_started:
                delay=min(delay,max(0.,next_import-time.monotonic()))
            if notification is None:connection.poll(delay)
            elif notification in wait([connection,notification],timeout=delay):
                notice=collector.consume_notification()
                if notice is True:next_poll=0
                elif notice is None:next_poll=min(next_poll,time.monotonic()+3)
                notification=collector.notification_connection() if notice is False else None
    except (EOFError,BrokenPipeError,OSError):
        pass
    except Exception:
        try: connection.send({'kind':'fatal','error':traceback.format_exc(limit=6)})
        except (OSError,EOFError): pass
    finally:
        if collector: collector.close()
        if ledger: ledger.close()
        connection.close()


class AnalysisBridge(QThread):
    # These Python payloads are consumed as read-only values on the GUI thread.
    # Avoid recursive QVariantMap conversion of the complete analysis graph.
    snapshot=Signal(object)
    result=Signal(object)
    failure=Signal(str)
    fault=Signal(object)
    record=Signal(object)
    population=Signal(object)
    def __init__(self,homes,index_path=None,static_snapshot=None,model_evidence_path=None,quota_path=None):
        super().__init__()
        self.homes,self.index_path,self.static_snapshot=homes,index_path,static_snapshot
        self.model_evidence_path=model_evidence_path
        self.quota_path=quota_path
        self.lock=threading.Lock()
        self.pending=None
        self.commands=[]
        self.process=None
        self.restart_count=0
        self.last_request=None
        self.last_record=None
    def request(self,request_id,query,logical):
        with self.lock:
            self.pending={'kind':'query','id':request_id,'query':query,'logical':logical}
            self.last_request=self.pending
    def enqueue_latest(self,command):
        # Each consumer accepts only its newest request id. Keep one pending
        # command per kind while the child is busy, including a single snapshot.
        self.commands[:]=[old for old in self.commands if old['kind']!=command['kind']]
        self.commands.append(command)
    def freeze_collection(self):
        with self.lock: self.enqueue_latest({'kind':'freeze'})
    def request_record(self,request_id,identity):
        with self.lock:
            self.last_record={'kind':'record','id':request_id,'identity':identity}
            self.enqueue_latest(self.last_record)
    def request_population(self,request_id,population,assumptions=None):
        with self.lock:self.enqueue_latest(dict(kind='population',id=request_id,population=population,assumptions=assumptions))

    def replace_snapshot(self,snapshot):
        with self.lock:
            self.static_snapshot=snapshot
            self.enqueue_latest({'kind':'replace','snapshot':snapshot})
    def run(self):
        while not self.isInterruptionRequested():
            receiver=SnapshotReceiver()
            context=mp.get_context('spawn')
            parent,child=context.Pipe()
            self.process=context.Process(target=process_main,args=(child,self.homes,self.index_path,self.static_snapshot,self.model_evidence_path,self.quota_path),daemon=True)
            try:
                self.process.start()
            except (OSError,RuntimeError) as error:
                parent.close(); child.close()
                self.restart_count=3
                self.failure.emit('분석 프로세스 시작 실패: '+str(error))
                self.fault.emit(dict(start_failed=True,retrying=False))
                return
            child.close()
            try:
                while not self.isInterruptionRequested():
                    with self.lock:
                        commands,self.commands=self.commands,[]
                        pending,self.pending=self.pending,None
                    for command in commands: parent.send(command)
                    if pending: parent.send(pending)
                    if parent.poll(.02):
                        message=parent.recv()
                        if message['kind']=='snapshot':
                            try:value=receiver.receive(message['value'])
                            except ValueError:
                                parent.send({'kind':'sync'});receiver=SnapshotReceiver();continue
                            if value is not None:self.snapshot.emit(value)
                        elif message['kind']=='record':
                            with self.lock:
                                if self.last_record and self.last_record['id']==message['id']:self.last_record=None
                            self.record.emit(message)
                        elif message['kind']=='population':self.population.emit(message)
                        elif message['kind']=='result': self.result.emit(message)
                        elif message['kind'] in ('error','fatal'):
                            self.failure.emit(message['error'])
                            self.fault.emit(dict(query_failed=message['kind']=='error',
                                fatal=message['kind']=='fatal',retrying=message['kind']=='fatal',
                                request_id=message.get('id')))
                            if message['kind']=='fatal': break
                    if not self.process.is_alive(): break
            except (EOFError,BrokenPipeError,OSError) as error:
                if not self.isInterruptionRequested():
                    self.failure.emit(str(error))
                    self.fault.emit(dict(process_exited=True,retrying=True))
            finally:
                try: parent.send({'kind':'stop'})
                except (OSError,EOFError): pass
                # Keep draining the pipe so a final snapshot cannot block the
                # child before it commits/closes its ledger and subscription.
                while self.process.is_alive():
                    try:
                        if parent.poll(.05):parent.recv()
                    except (EOFError,OSError):break
                    self.process.join(.05)
                self.process.join()
                parent.close()
            if self.isInterruptionRequested(): break
            self.restart_count+=1
            if self.restart_count>2:
                self.failure.emit('분석 프로세스를 시작할 수 없습니다. 앱을 다시 열어 주세요.')
                self.fault.emit(dict(process_exited=True,retry_exhausted=True))
                break
            self.failure.emit('분석 프로세스를 다시 시작합니다. 마지막 결과를 유지합니다.')
            self.fault.emit(dict(process_exited=True,retrying=True))
            with self.lock:
                self.pending=self.last_request
                if self.last_record:self.enqueue_latest(self.last_record)
            self.msleep(250)
