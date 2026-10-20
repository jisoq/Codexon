"""Analyze shared collector snapshots outside Qt's UI thread."""
import multiprocessing as mp
from multiprocessing.connection import wait
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from bisect import bisect_left
from PySide6.QtCore import QThread, Signal
from .analysis_engine import AnalysisEngine
from .analytics import _local_timestamp
from .quota_cycles import ledger_path
from .overlay_data import OverlaySummaries
from .analysis_delivery import SnapshotPublisher, SnapshotReceiver


def expiry(engine,q):
    now=q.get('now',q['end'])
    candidates=[float('inf')]
    duration=1800 if q.get('period')=='30m' else None
    if q.get('period') in ('today','7d','30d') or q['page']==0:
        candidates.append(_local_timestamp(datetime.combine(datetime.fromtimestamp(now).date()+timedelta(days=1),datetime.min.time())))
    if hasattr(engine,'boundary_times'):
        if duration:candidates.extend(value+duration+.002 for value in engine.boundary_times(q['start']))
        if q.get('period')!='custom':candidates.extend(value+.002 for value in engine.boundary_times(q['end']))
        return min(candidates)
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


def process_main(connection,homes,index_path,static_snapshot=None,model_evidence_path=None,quota_path=None,cache_floor=None):
    """Use prepared storage normally; explicit snapshots remain fixture inputs."""
    if static_snapshot is None:
        from .prepared_runtime import process_main as prepared_process_main
        try:
            replacement=prepared_process_main(connection,homes,index_path,model_evidence_path,
                                              quota_path or ledger_path(index_path),cache_floor)
            if replacement is not None:
                return process_main(connection,homes,index_path,replacement,model_evidence_path,quota_path,cache_floor)
            return
        except Exception as error:
            from pathlib import Path
            from .quota_diagnostics import record_failure
            record_failure(Path(quota_path or ledger_path(index_path)).with_suffix('.analysis.sqlite'),
                           'prepared process', error)
            try:connection.send(dict(kind='fatal',error=traceback.format_exc(limit=6)))
            except (OSError,EOFError):pass
            return
        finally:
            connection.close()
    try:
        engine=AnalysisEngine()
        overlays=OverlaySummaries()
        publisher=SnapshotPublisher()
        snapshot=static_snapshot
        engine.ingest(snapshot['sessions'])
        pending=ready=None
        details={}

        def publish():
            connection.send(dict(kind='snapshot',value=publisher.publish(snapshot,engine,overlays.collect(engine))))

        def send_details(all_pending=True):
            while details:
                message=details.pop(next(iter(details)))
                if message['kind']=='population':
                    from .dashboard_views import population_summary
                    connection.send(dict(kind='population',id=message['id'],
                        summary=population_summary(engine,message['population'],message.get('assumptions'))))
                elif message['kind']=='overlay_detail':
                    connection.send(dict(kind='overlay_detail',id=message['id'],summary=message['summary']))
                else:
                    connection.send(dict(kind='record',id=message['id'],row=engine.record(*message['identity'])))
                if not all_pending:break

        publish()
        while True:
            while connection.poll():
                message=connection.recv();kind=message['kind']
                if kind=='stop':return
                if kind in ('freeze','replace','sync'):send_details()
                if kind=='replace':
                    ready=None;snapshot=message['snapshot']
                    engine.ingest(snapshot['sessions']);publish()
                elif kind=='query':pending=message;ready=None
                elif kind=='sync':
                    publisher=SnapshotPublisher();publish()
                elif kind in ('population','record','overlay_detail'):
                    details.pop(kind,None);details[kind]=message
            send_details(False)
            if ready is not None:
                connection.send(ready);ready=None
            if pending is not None:
                request=pending;pending=None
                try:
                    connection.send(dict(kind='progress',id=request['id'],stage='query'))
                    result=engine.page_query(request['query'])
                    ready=dict(kind='result',id=request['id'],logical=request['logical'],result=result,
                               valid_until=result['valid_until'] if 'valid_until' in result else expiry(engine,request['query']),
                               metrics=dict(engine.metrics))
                    continue
                except Exception:
                    connection.send(dict(kind='error',id=request['id'],error=traceback.format_exc(limit=6)))
            if not details:connection.poll(1.)
    except (EOFError,BrokenPipeError,OSError):
        pass
    except Exception:
        try:connection.send(dict(kind='fatal',error=traceback.format_exc(limit=6)))
        except (OSError,EOFError):pass
    finally:
        connection.close()


@dataclass
class _CommandSendState:
    closing: bool = False
    error: Exception | None = None


class AnalysisBridge(QThread):
    # These Python payloads are consumed as read-only values on the GUI thread.
    # Avoid recursive QVariantMap conversion of the complete analysis graph.
    snapshot=Signal(object)
    result=Signal(object)
    progress=Signal(object)
    failure=Signal(str)
    fault=Signal(object)
    record=Signal(object)
    population=Signal(object)
    overlay_detail=Signal(object)
    stale=Signal(object)
    def __init__(self,homes,index_path=None,static_snapshot=None,model_evidence_path=None,quota_path=None,cache_floor=None):
        super().__init__()
        self.homes,self.index_path,self.static_snapshot=homes,index_path,static_snapshot
        self.model_evidence_path=model_evidence_path
        self.quota_path=quota_path
        self.cache_floor=cache_floor
        self.lock=threading.Lock()
        self.command_ready=threading.Condition(self.lock)
        self._wake_reader,self._wake_writer=mp.Pipe(duplex=False)
        self._wake_armed=False
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
            self._wake_locked()
    def enqueue_latest(self,command):
        # Each consumer accepts only its newest request id. Keep one pending
        # command per kind while the child is busy, including a single snapshot.
        self.commands[:]=[old for old in self.commands if old['kind']!=command['kind']]
        self.commands.append(command)
        self._wake_locked()

    def _wake_locked(self):
        self.command_ready.notify()
        # One token covers the whole mailbox, so producers cannot fill the pipe.
        if self._wake_armed or self._wake_writer.closed:return
        self._wake_writer.send_bytes(b'\0')
        self._wake_armed=True

    def _consume_wake(self):
        with self.lock:
            if self._wake_armed:
                self._wake_reader.recv_bytes()
                self._wake_armed=False

    def _send_commands(self,connection,state):
        # A blocked command must never prevent the bridge from draining results.
        try:
            while True:
                with self.command_ready:
                    self.command_ready.wait_for(lambda:state.closing or self.isInterruptionRequested()
                                                or self.commands or self.pending is not None)
                    if state.closing or self.isInterruptionRequested():command={'kind':'stop'}
                    elif self.commands:command=self.commands.pop(0)
                    else:command,self.pending=self.pending,None
                connection.send(command)
                if command['kind']=='stop':return
                command=None
        except Exception as error:
            with self.command_ready:
                state.error=error
                self._wake_locked()

    def requestInterruption(self):
        super().requestInterruption()
        with self.lock:self._wake_locked()

    def freeze_collection(self):
        with self.lock: self.enqueue_latest({'kind':'freeze'})
    def request_snapshot(self):
        with self.lock: self.enqueue_latest({'kind':'sync'})
    def set_cache_floor(self,value):
        with self.lock:
            if value==self.cache_floor:return
            self.cache_floor=value
            self.enqueue_latest({'kind':'cache_floor','value':value})
    def request_record(self,request_id,identity,prepared_token=None):
        with self.lock:
            self.last_record={'kind':'record','id':request_id,'identity':identity}
            if prepared_token is not None:self.last_record['prepared_token']=prepared_token
            self.enqueue_latest(self.last_record)
    def request_population(self,request_id,population,assumptions=None):
        with self.lock:self.enqueue_latest(dict(kind='population',id=request_id,population=population,assumptions=assumptions))

    def request_overlay_detail(self,request_id,summary,health=False,finance=False):
        with self.lock:self.enqueue_latest(dict(kind='overlay_detail',id=request_id,summary=summary,
            health=health,finance=finance,prepared_token=summary.get('_prepared_token')))

    def replace_snapshot(self,snapshot):
        with self.lock:
            self.static_snapshot=snapshot
            self.enqueue_latest({'kind':'replace','snapshot':snapshot})

    def _deliver_result(self,message):
        kind=message['kind']
        consumer=message.get('consumer') if kind=='stale' else kind
        if consumer=='record':
            with self.lock:
                if self.last_record and self.last_record['id']==message['id']:self.last_record=None
        if consumer=='overlay_detail':self.overlay_detail.emit(message);return
        {'record':self.record,'population':self.population,'result':self.result,'stale':self.stale}[kind].emit(message)

    def run(self):
        with self.lock:
            if self._wake_reader.closed:
                self._wake_reader,self._wake_writer=mp.Pipe(duplex=False)
        try:self._run()
        finally:
            with self.lock:
                self._wake_reader.close();self._wake_writer.close()
                self._wake_armed=False

    def _run(self):
        while not self.isInterruptionRequested():
            receiver=SnapshotReceiver()
            context=mp.get_context('spawn')
            parent,child=context.Pipe()
            self.process=context.Process(target=process_main,args=(child,self.homes,self.index_path,self.static_snapshot,self.model_evidence_path,self.quota_path,self.cache_floor),daemon=True)
            try:
                self.process.start()
            except (OSError,RuntimeError) as error:
                parent.close(); child.close()
                self.restart_count=3
                from pathlib import Path
                from .quota_diagnostics import record_failure
                record_failure(Path(self.quota_path or ledger_path(self.index_path)).with_suffix('.analysis.sqlite'),
                               'analysis process start', error)
                self.failure.emit('분석 프로세스 시작 실패: '+str(error))
                self.fault.emit(dict(start_failed=True,retrying=False))
                return
            child.close()
            send_state=_CommandSendState()
            sender=threading.Thread(target=self._send_commands,args=(parent,send_state),
                                    name='analysis-command-sender',daemon=True)
            sender_started=False
            try:
                sender.start()
                sender_started=True
                while not self.isInterruptionRequested():
                    ready=wait((parent,self._wake_reader,self.process.sentinel))
                    if self._wake_reader in ready:self._consume_wake()
                    if send_state.error is not None:raise OSError(str(send_state.error)) from send_state.error
                    if self.isInterruptionRequested():break
                    if parent in ready:
                        message=parent.recv()
                        if message['kind']=='snapshot':
                            try:value=receiver.receive(message['value'])
                            except ValueError:
                                with self.lock:self.enqueue_latest({'kind':'sync'})
                                receiver=SnapshotReceiver();continue
                            if value is not None:self.snapshot.emit(value)
                        elif message['kind'] in ('record','population','overlay_detail','result','stale'):self._deliver_result(message)
                        elif message['kind']=='progress':self.progress.emit(message)
                        elif message['kind'] in ('error','fatal'):
                            self.failure.emit(message['error'])
                            self.fault.emit(dict(query_failed=message['kind']=='error' and message.get('consumer')!='publication',
                                publication_failed=message.get('consumer')=='publication',
                                fatal=message['kind']=='fatal',retrying=message['kind']=='fatal',
                                request_id=message.get('id')))
                            if message['kind']=='fatal': break
                    if self.process.sentinel in ready: break
            except (EOFError,BrokenPipeError,OSError,RuntimeError) as error:
                if not self.isInterruptionRequested():
                    self.failure.emit(str(error))
                    self.fault.emit(dict(process_exited=True,retrying=True))
            finally:
                with self.command_ready:
                    send_state.closing=True
                    self.command_ready.notify()
                # Drain before joining the sole sender, including its stop frame.
                while True:
                    if not sender_started or send_state.error is not None:
                        if sender_started:sender.join()
                        # A failed sender cannot deliver stop. EOF releases the
                        # child's existing cleanup without killing its writer.
                        parent.close()
                        break
                    try:
                        ready=wait((parent,self._wake_reader,self.process.sentinel))
                        if self._wake_reader in ready:self._consume_wake()
                        if parent in ready:parent.recv()
                    except (EOFError,OSError):break
                    if self.process.sentinel in ready:break
                self.process.join()
                if sender_started:sender.join()
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
                if self.pending:self._wake_locked()
                if self.last_record:self.enqueue_latest(self.last_record)
            self.msleep(250)
