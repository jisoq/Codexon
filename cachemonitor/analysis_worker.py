"""Analyze shared collector snapshots outside Qt's UI thread."""
import multiprocessing as mp
import threading
import time
import traceback
import sqlite3
from datetime import datetime, timedelta
from bisect import bisect_left
from PySide6.QtCore import QThread, Signal
from .analysis_engine import AnalysisEngine
from .usage_collection import CollectionClient
from .quota_cycles import QuotaLedger, ledger_path
from .overlay_data import OverlaySummaries
from .analysis_delivery import SnapshotPublisher, SnapshotReceiver


def expiry(engine,q):
    now=q.get('now',q['end'])
    candidates=[float('inf')]
    duration=1800 if q.get('period')=='30m' else None
    if duration:
        i=bisect_left(engine.timestamps,q['start'])
        if i<len(engine.timestamps): candidates.append(engine.timestamps[i]+duration+0.002)
        for state in engine.sessions.values():
            for request in state['prepared'].get('turn_records',{}).values():
                for stamp in (request.get('started_at'),request.get('ended_at')):
                    if stamp is not None and stamp>=q['start']:candidates.append(stamp+duration+.002)
    if q.get('period') in ('today','7d','30d') or q['page']==0:
        candidates.append(datetime.combine(datetime.fromtimestamp(now).date()+timedelta(days=1),datetime.min.time()).timestamp())
    i=bisect_left(engine.timestamps,q['end'])
    if q.get('period')!='custom' and i<len(engine.timestamps): candidates.append(engine.timestamps[i]+0.002)
    if q.get('period')!='custom':
        for state in engine.sessions.values():
            boundaries=state.get('boundaries',())
            i=bisect_left(boundaries,q['end'])
            if i<len(boundaries):candidates.append(boundaries[i]+.002)
    return min(candidates)


def process_main(connection,homes,index_path,static_snapshot=None,model_evidence_path=None,quota_path=None,collection_autostart=True):
    quota_path=quota_path or ledger_path(index_path)
    collector=None
    ledger=None
    ledger_issue=None
    ledger_retry=0
    ledger_failures=0
    imported=False
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
        snapshot=None
        frozen=static_snapshot is not None
        next_poll=0
        pending=None
        if static_snapshot is not None:
            snapshot=static_snapshot
            engine.ingest(snapshot['sessions'])
        else:
            collector=CollectionClient(homes,index_path,model_evidence_path)
            collector.autostart=collection_autostart
            try:ledger=QuotaLedger(quota_path)
            except sqlite3.Error as error:disable_ledger(error)
        def publish():
            light=publisher.publish(snapshot,engine,overlays.collect(engine))
            connection.send({'kind':'snapshot','value':light})
        if snapshot is not None: publish()
        while True:
            while connection.poll():
                message=connection.recv()
                if message['kind']=='stop': return
                if message['kind']=='freeze': frozen=True
                elif message['kind']=='replace':
                    snapshot=message['snapshot']; engine.ingest(snapshot['sessions']); publish()
                elif message['kind']=='query': pending=message
                elif message['kind']=='sync':
                    publisher=SnapshotPublisher()
                    if snapshot is not None:publish()
                elif message['kind']=='record':
                    connection.send({'kind':'record','id':message['id'],
                                     'row':engine.record(*message['identity'])})
            if not frozen and time.monotonic()>=next_poll:
                snapshot=collector.poll()
                if ledger is None and time.monotonic()>=ledger_retry:
                    try:ledger=QuotaLedger(quota_path)
                    except sqlite3.Error as error:disable_ledger(error)
                if ledger:
                    try:ledger.enrich_modes(snapshot)
                    except sqlite3.Error as error:disable_ledger(error)
                engine.ingest(snapshot['sessions'])
                if ledger:
                    try:
                        ledger.sync(engine,snapshot)
                        if not imported and not snapshot['index']['loading']:
                            for home in snapshot['homes']: ledger.import_observations(collector.path,home)
                            imported=True
                        ledger_issue=None
                        ledger_failures=0
                    except sqlite3.Error as error:disable_ledger(error)
                # The quota ledger is independent of observed per-call usage.
                # Keep its diagnostics without turning valid session values into
                # missing data or replacing/repairing the user's quota history.
                if ledger_issue:snapshot={**snapshot,'ledger_error':ledger_issue}
                publish()
                next_poll=time.monotonic()+(.1 if snapshot['index']['loading'] else 2)
            if pending is not None and snapshot is not None:
                request=pending; pending=None
                try:
                    result=engine.query(request['query'])
                    connection.send({'kind':'result','id':request['id'],'logical':request['logical'],
                                     'result':result,'valid_until':expiry(engine,request['query']),
                                     'metrics':dict(engine.metrics)})
                except Exception:
                    connection.send({'kind':'error','id':request['id'],'error':traceback.format_exc(limit=6)})
            connection.poll(.02)
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
    record=Signal(object)
    def __init__(self,homes,index_path=None,static_snapshot=None,model_evidence_path=None,quota_path=None,collection_autostart=True):
        super().__init__()
        self.homes,self.index_path,self.static_snapshot=homes,index_path,static_snapshot
        self.model_evidence_path=model_evidence_path
        self.quota_path=quota_path
        self.collection_autostart=collection_autostart
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
    def freeze_collection(self):
        with self.lock: self.commands.append({'kind':'freeze'})
    def request_record(self,request_id,identity):
        with self.lock:
            self.last_record={'kind':'record','id':request_id,'identity':identity}
            self.commands.append(self.last_record)
    def replace_snapshot(self,snapshot):
        with self.lock:
            self.static_snapshot=snapshot
            self.commands.append({'kind':'replace','snapshot':snapshot})
    def run(self):
        while not self.isInterruptionRequested():
            receiver=SnapshotReceiver()
            context=mp.get_context('spawn')
            parent,child=context.Pipe()
            self.process=context.Process(target=process_main,args=(child,self.homes,self.index_path,self.static_snapshot,self.model_evidence_path,self.quota_path,self.collection_autostart),daemon=True)
            try:
                self.process.start()
            except (OSError,RuntimeError) as error:
                parent.close(); child.close()
                self.restart_count=3
                self.failure.emit('분석 프로세스 시작 실패: '+str(error))
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
                        elif message['kind']=='result': self.result.emit(message)
                        elif message['kind'] in ('error','fatal'):
                            self.failure.emit(message['error'])
                            if message['kind']=='fatal': break
                    if not self.process.is_alive(): break
            except (EOFError,BrokenPipeError,OSError) as error:
                if not self.isInterruptionRequested(): self.failure.emit(str(error))
            finally:
                try: parent.send({'kind':'stop'})
                except (OSError,EOFError): pass
                # Keep draining the pipe so a final snapshot cannot block the
                # child before it commits/closes its ledger and subscription.
                while self.process.is_alive():
                    if parent.poll(.05):
                        try:parent.recv()
                        except (EOFError,OSError):break
                    self.process.join(.05)
                self.process.join()
                parent.close()
            if self.isInterruptionRequested(): break
            self.restart_count+=1
            if self.restart_count>2:
                self.failure.emit('분석 프로세스를 시작할 수 없습니다. 앱을 다시 열어 주세요.')
                break
            self.failure.emit('분석 프로세스를 다시 시작합니다. 마지막 결과를 유지합니다.')
            with self.lock:
                self.pending=self.last_request
                if self.last_record:self.commands.append(self.last_record)
            self.msleep(250)
