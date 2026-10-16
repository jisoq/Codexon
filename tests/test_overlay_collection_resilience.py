"""Optional quota persistence cannot suppress observed session/overlay usage."""
import copy
import json
import sqlite3
import time
from multiprocessing import Pipe
from threading import Event, Thread

import pytest

from cachemonitor import analysis_worker, prepared_runtime
from cachemonitor.analysis_delivery import SnapshotReceiver
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.usage_collection import CollectionChannel, empty_snapshot
from test_overlay import source


class PreparedAnalysis:
    """Small normal-runtime fixture with the real transport, store, and reader."""
    def __init__(self,directory,*,at=None,quota_path=None):
        self.channel=CollectionChannel([directory/'home'],directory/'index.sqlite')
        self.channel.start_live()
        self.session=source()
        self.session.update(home=self.channel.homes[0],collection_complete=True,usage_revision=1)
        self.at=time.time() if at is None else at
        self.quota_path=quota_path or directory/'quota.sqlite'
        self.parent,self.child=Pipe()
        self.receiver=SnapshotReceiver();self.snapshot=None;self.error=None;self.sequence=0
        self.thread=Thread(target=self.run,daemon=True)

    def publish(self,*,at=None,errors=()):
        self.sequence+=1
        value=empty_snapshot(self.channel.homes,self.channel.path,self.at if at is None else at)
        value.update(sessions=[copy.deepcopy(self.session)],usage_collection_complete=True,
                     last_usage_collection_success=value['ts'],request_activity=[],quota_by_home={},errors=list(errors))
        value['index'].update(loading=False,usage_complete=True,last_usage_success=value['ts'])
        self.channel.publish(value,'prepared-test',self.sequence)

    def run(self):
        try:
            analysis_worker.process_main(self.child,self.channel.homes,self.channel.path,
                quota_path=self.quota_path,model_evidence_path=self.channel.default_evidence,cache_floor=0)
        except BaseException as error:self.error=error

    def wait_for(self,predicate,seconds=10):
        deadline=time.monotonic()+seconds
        while True:
            remaining=deadline-time.monotonic()
            assert remaining>0 and self.parent.poll(remaining),('analysis reply deadline',self.error)
            message=self.parent.recv()
            assert message['kind'] not in ('fatal','error'),message
            if message['kind']=='snapshot':
                self.snapshot=self.receiver.receive(message['value'])
                assert self.snapshot is not None,'snapshot receiver rejected normal publication'
            if predicate(message):return message

    def wait_snapshot(self,predicate=lambda value:True,seconds=10):
        if self.snapshot is not None and predicate(self.snapshot):return self.snapshot
        self.wait_for(lambda message:message['kind']=='snapshot' and predicate(self.snapshot),seconds)
        return self.snapshot

    def assert_calls_and_detail(self):
        overlay=self.snapshot['overlay_sessions'][0]
        history=self.session['history']
        assert overlay['calls']==len(history) and overlay['latest_key']==history[-1]['key']
        assert [row['key'] for row in overlay['recent']]==[row['key'] for row in history]
        reference=AnalysisEngine();reference.ingest([copy.deepcopy(self.session)])
        identity=(self.session['home'],self.session['id'],history[-1]['key'])
        self.parent.send(dict(kind='record',id=1,identity=identity,prepared_token=self.snapshot['prepared_token']))
        message=self.wait_for(lambda value:value.get('id')==1)
        assert message['kind']=='record' and message['row']==reference.record(*identity)

    def __enter__(self):
        self.publish();self.thread.start();return self

    def __exit__(self,*exc):
        try:
            if self.thread.is_alive():self.parent.send({'kind':'stop'})
            deadline=time.monotonic()+5
            while self.thread.is_alive() and time.monotonic()<deadline:
                try:
                    if self.parent.poll(.02):self.parent.recv()
                except EOFError:pass
                self.thread.join(.02)
            assert not self.thread.is_alive(),'normal analysis worker did not stop'
            assert self.error is None,self.error
        finally:self.parent.close();self.child.close();self.channel.close()


@pytest.mark.parametrize('failure_stage',['init','enrich','sync','import'])
def test_quota_storage_failure_keeps_publishing_real_calls(monkeypatch,tmp_path,failure_stage):
    failures=[]
    def fail(stage):
        if stage==failure_stage:
            failures.append(stage)
            raise sqlite3.OperationalError('optional quota storage unavailable')
    class Ledger(prepared_runtime.QuotaLedger):
        def __init__(self,*args,**kwargs):fail('init');super().__init__(*args,**kwargs)
        def enrich_modes(self,*args,**kwargs):fail('enrich');return super().enrich_modes(*args,**kwargs)
        def sync(self,*args,**kwargs):fail('sync');return super().sync(*args,**kwargs)
        def import_observations_step(self,*args,**kwargs):
            fail('import');return super().import_observations_step(*args,**kwargs)
    monkeypatch.setattr(prepared_runtime,'QuotaLedger',Ledger)
    with PreparedAnalysis(tmp_path) as worker:
        original=copy.deepcopy(worker.session)
        failed=worker.wait_snapshot(lambda value:value.get('ledger_fault',{}).get('ledger_failed'))
        assert failed.get('ledger_error') and not failed['errors']
        worker.assert_calls_and_detail()
        assert failures==[failure_stage] and worker.session==original


def test_usage_ledger_retries_after_transient_failure(monkeypatch,tmp_path):
    opens=[];failures=[];armed=Event()
    quota_path=tmp_path/'retained-quota.sqlite'
    class Ledger(prepared_runtime.QuotaLedger):
        def __init__(self,path):
            opens.append((time.monotonic(),path));super().__init__(path)
        def sync(self,*args,**kwargs):
            if armed.is_set() and not failures:
                failures.append(time.monotonic());raise sqlite3.OperationalError('locked')
            return super().sync(*args,**kwargs)
    monkeypatch.setattr(prepared_runtime,'QuotaLedger',Ledger)
    with PreparedAnalysis(tmp_path,quota_path=quota_path) as worker:
        worker.wait_snapshot(lambda value:value.get('ledger_status_known'))
        armed.set()
        worker.session['history'][-1]['output']+=1
        worker.session['usage_revision']+=1
        worker.publish()
        failed=worker.wait_snapshot(lambda value:value.get('ledger_error'))
        worker.assert_calls_and_detail()
        recovered=worker.wait_snapshot(lambda value:value.get('ledger_status_known') and not value.get('ledger_error'))
        assert not recovered['ledger_fault']['ledger_failed']
        assert recovered['prepared_token']==failed['prepared_token']
        assert len(opens)==2 and opens[1][0]-failures[0]>=5
        assert [path for _,path in opens]==[quota_path,quota_path]
    from cachemonitor.quota_service import QuotaService
    assert QuotaService(tmp_path/'home',tmp_path/'different-index.sqlite',live=False,quota_path=quota_path).path==quota_path


def test_history_recovery_yields_to_details_and_collection(monkeypatch,tmp_path):
    from cachemonitor.event_store import EventStore,pack_event
    from cachemonitor.quota_history_import import ObservationImport
    from test_quota_cycles import quota
    entered=[Event(),Event()];release=[Event(),Event()];steps=[]
    monkeypatch.setattr(ObservationImport,'OBSERVATIONS',1)
    class Ledger(prepared_runtime.QuotaLedger):
        def import_observations_step(self,*args):
            complete=super().import_observations_step(*args)
            index=len(steps);steps.append(complete)
            if index<2:
                entered[index].set()
                assert release[index].wait(10),'history import gate was not released'
            return complete
    monkeypatch.setattr(prepared_runtime,'QuotaLedger',Ledger)
    worker=PreparedAnalysis(tmp_path)
    with sqlite3.connect(worker.channel.path) as db:
        events=EventStore(db,worker.channel.path);events.enable_replay_tokens()
        for i in range(3):
            payload=json.dumps({'payload':{'rate_limits':quota(100+i,i)}})
            db.execute('insert into events values(?,?,?,?,?,?)',
                ('retained',i,worker.session['home'],worker.session['id'],100+i,pack_event(payload)))
    with worker:
        try:
            first=worker.wait_snapshot()
            assert not first['ledger_status_known'] and entered[0].wait(3) and steps==[False]
            worker.assert_calls_and_detail()
            worker.session['history'][-1]['output']+=1;worker.session['usage_revision']+=1
            worker.publish();release[0].set()
            changed=worker.wait_snapshot(lambda value:value['prepared_token']!=first['prepared_token'])
            assert not changed['ledger_status_known'] and entered[1].wait(3)
            worker.assert_calls_and_detail()
            assert steps==[False,False]
            release[1].set()
            final=worker.wait_snapshot(lambda value:value.get('ledger_status_known'))
            assert not final['ledger_fault']['ledger_failed'] and steps[-1] is True
        finally:
            for gate in release:gate.set()


def test_service_collects_large_record_once_for_gui_and_worker(tmp_path,monkeypatch):
    import json
    from cachemonitor.index import UsageIndex
    from cachemonitor.usage_collection import CollectionClient,CollectorService
    from test_core import event,usage,fixture_home,TID
    home,path=fixture_home(tmp_path)
    with path.open('w',encoding='utf-8') as stream:
        stream.write(json.dumps(event('session_meta',id=TID,source='cli'))+'\n')
        stream.write(json.dumps(usage('first'))+'\n')
        for _ in range(40):stream.write(' '*1024*1024+'\n')
        stream.write(json.dumps(usage('last',time=10002))+'\n')
    index=tmp_path/'index.sqlite'
    service=CollectorService([home],index)
    gui=CollectionClient([home],index)
    worker=CollectionClient([home],index)
    try:
        for _ in range(10):
            produced=service.poll(10010)
            if not produced['index']['loading']:break
        assert not produced['index']['loading']
        with pytest.raises(RuntimeError):CollectorService([home],index)
        def no_scan(*args):raise AssertionError('Consumer scanned source records')
        monkeypatch.setattr(UsageIndex,'scan_file',no_scan)
        for client in (gui,worker):
            snapshot=client.poll(10010)
            transport={'activity_cursor','activity_highwater','activity_more'}
            assert {k:v for k,v in snapshot.items() if k not in transport}==json.loads(json.dumps(produced))
            assert snapshot['activity_cursor'][1]==snapshot['activity_highwater']==0
            assert not snapshot['activity_more']
            assert {r['key'] for s in snapshot['sessions'] for r in s['history']}=={'first','last'}
            assert not hasattr(client,'index') and not hasattr(client,'collector')
            snapshot['sessions'].clear()
            assert client.poll(10010)['sessions']
        assert service.index.db.execute('select offset from files').fetchone()[0]==path.stat().st_size
    finally:gui.close();worker.close();service.close()


def test_missing_or_stalled_service_is_read_only_for_consumers(tmp_path,monkeypatch):
    from cachemonitor.index import UsageIndex
    from cachemonitor.usage_collection import CollectionClient,CollectorService
    from cachemonitor.observer_task import ObserverTask
    from test_core import fixture_home
    home,_=fixture_home(tmp_path);path=tmp_path/'index.sqlite';starts=[]
    service=CollectorService([home],path);service.poll(10010);service.close()
    gui=CollectionClient([home],path)
    def no_index(*args,**kwargs):raise AssertionError('GUI became a collector')
    monkeypatch.setattr(UsageIndex,'__init__',no_index)
    monkeypatch.setattr(ObserverTask,'start',lambda self,command,autostart:starts.append((self.role,command)))
    try:
        snapshot=gui.poll(10045)
        assert snapshot['sessions'] and snapshot['collection_stale'] and not snapshot['errors']
        assert not snapshot['usage_collection_complete'] and snapshot['ts']==10010
        assert starts==[]
        gui.poll(10046);assert starts==[]
        assert not hasattr(gui,'index') and not hasattr(gui,'collector')
    finally:gui.close()


def test_service_collects_all_requested_homes(tmp_path):
    from cachemonitor.usage_collection import CollectionClient,CollectorService
    from test_core import fixture_home
    (tmp_path/'one').mkdir();(tmp_path/'two').mkdir()
    first,_=fixture_home(tmp_path/'one');second,_=fixture_home(tmp_path/'two')
    path=tmp_path/'index.sqlite'
    service=CollectorService([first],path)
    gui=CollectionClient([second,first],path)
    worker=CollectionClient([first],path)
    try:
        service.poll(10010);assert gui.poll(10010)['index']['loading']
        produced=service.poll(10011)
        produced['unassigned']=[dict(home=str(first),turn='one'),dict(home=str(second),turn='two')]
        produced['quota_by_home']={str(first):{'account':'first'},str(second):{'account':'second'}}
        service.channel.publish(produced,service.epoch,service.sequence+1)
        both=gui.poll(10011);own=worker.poll(10011)
        assert {s['home'] for s in both['sessions']}=={str(first),str(second)}
        assert {s['home'] for s in own['sessions']}=={str(first)}
        assert not both['index']['loading']
        assert both['homes']==[str(second),str(first)] and both['quota_by_home'][str(second)]['account']=='second'
        assert own['quota_by_home']=={str(first):{'account':'first'}}
        assert own['unassigned']==[dict(home=str(first),turn='one')]
    finally:gui.close();worker.close();service.close()


def test_subscription_merge_preserves_primary_home(tmp_path):
    from cachemonitor.usage_collection import CollectionChannel
    homes=[tmp_path/'z-primary',tmp_path/'a-secondary'];extra=tmp_path/'b-extra'
    service=CollectionChannel(homes,tmp_path/'index.sqlite')
    consumer=CollectionChannel([extra,homes[1]],tmp_path/'index.sqlite')
    try:
        consumer.subscribe(100)
        assert service.requested_homes(101)==[str(h.resolve()) for h in [*homes,extra]]
    finally:consumer.close();service.close()


def test_conflicting_evidence_scope_is_rejected_without_replacing_collector(tmp_path,monkeypatch):
    from cachemonitor.usage_collection import CollectionClient,CollectorService,empty_snapshot
    from cachemonitor.observer_task import ObserverTask
    home=tmp_path/'home';path=tmp_path/'index.sqlite'
    service=CollectorService([home],path,tmp_path/'first.sqlite')
    client=CollectionClient([home],path,tmp_path/'second.sqlite')
    def unexpected(*args,**kwargs):raise AssertionError('Conflicting client started a collector')
    monkeypatch.setattr(ObserverTask,'start',unexpected)
    try:
        service.channel.publish(empty_snapshot([str(home)],path,100),service.epoch,1)
        rejected=client.poll(101)
        assert rejected['sessions']==[] and not rejected['index']['loading']
        assert '다른 관측 DB' in rejected['errors'][0]
        assert service.channel.db.execute('SELECT count(*) FROM consumers').fetchone()[0]==0
        assert service.channel.read()['errors']==[] and not service.stopping()
    finally:client.close();service.close()


def _collect_in_process(home,path,version=None):
    import time
    from cachemonitor import usage_collection
    if version:usage_collection.VERSION=version
    from cachemonitor.usage_collection import CollectorService
    service=CollectorService([home],path)
    try:
        while not service.stopping():
            service.poll();time.sleep(.05)
    finally:service.close()


@pytest.mark.integration
def test_consumer_close_does_not_own_service_and_app_restarts_dead_collector(tmp_path,monkeypatch):
    import json,multiprocessing as mp,time
    from cachemonitor.usage_collection import CollectionClient
    from cachemonitor.observer_task import ObserverTask
    from test_core import fixture_home,usage
    home,record=fixture_home(tmp_path);path=tmp_path/'index.sqlite'
    context=mp.get_context('spawn')
    processes=[]
    def launch():
        process=context.Process(target=_collect_in_process,args=(str(home),str(path)))
        process.start();processes.append(process);return process
    producer=launch();gui=CollectionClient([home],path)
    try:
        until=time.monotonic()+10
        while time.monotonic()<until:
            first=gui.poll()
            if first.get('collection',{}).get('pid')==producer.pid:break
            time.sleep(.05)
        assert first['collection']['pid']==producer.pid
        gui.close()
        with record.open('a',encoding='utf-8') as stream:stream.write(json.dumps(usage('after-gui-close',time=10020))+'\n')
        gui=CollectionClient([home],path)
        until=time.monotonic()+5
        while time.monotonic()<until:
            snapshot=gui.poll()
            if any(r['key']=='after-gui-close' for s in snapshot['sessions'] for r in s['history']):break
            time.sleep(.05)
        assert any(r['key']=='after-gui-close' for s in snapshot['sessions'] for r in s['history'])
        assert snapshot['collection']['pid']==producer.pid
        producer.terminate();producer.join(5)
        monkeypatch.setattr(ObserverTask,'start',lambda *args,**kwargs:launch())
        from cachemonitor.app_services import AppServices
        owner=AppServices(None,[str(home)],path)
        monkeypatch.setattr(ObserverTask,'inspect',lambda self:{'running':0})
        clock=[0];owner.clock=lambda:clock[0]
        owner.poll_collection();assert len(processes)==1
        clock[0]=60;owner.poll_collection()
        stale=gui.poll(time.time()+31)
        assert stale['collection_stale'] and not hasattr(gui,'index')
        until=time.monotonic()+10
        while time.monotonic()<until:
            restarted=gui.poll()
            if restarted.get('collection',{}).get('pid')==processes[-1].pid:break
            time.sleep(.05)
        assert restarted['collection']['pid']==processes[-1].pid!=producer.pid
    finally:
        gui.close()
        for process in processes:
            if process.is_alive():process.terminate()
            process.join(3)


@pytest.mark.integration
def test_fresh_old_collector_is_replaced_cooperatively(tmp_path,monkeypatch):
    import multiprocessing as mp,time
    from cachemonitor.usage_collection import CollectionClient,VERSION
    from cachemonitor.observer_task import ObserverTask
    from test_core import fixture_home
    home,_=fixture_home(tmp_path);path=tmp_path/'index.sqlite'
    context=mp.get_context('spawn');processes=[];configured=[]
    def launch(version=None):
        process=context.Process(target=_collect_in_process,args=(str(home),str(path),version))
        process.start();processes.append(process);return process
    old=launch('2026.09.25.5');client=CollectionClient([home],path)
    monkeypatch.setattr(ObserverTask,'configure',lambda self,command,autostart:configured.append(command))
    monkeypatch.setattr(ObserverTask,'start',lambda *args,**kwargs:launch())
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            before=client.poll()
            if before.get('collection',{}).get('pid')==old.pid:break
            time.sleep(.05)
        assert before['collection']['version']=='2026.09.25.5'
        from cachemonitor.app_services import AppServices
        import sys
        owner=AppServices(None,[str(home)],path)
        monkeypatch.setattr(ObserverTask,'inspect',lambda self:{'running':0})
        monkeypatch.setattr('cachemonitor.app_services.identity.process_command',lambda pid:[sys.executable,'run.py','--usage-collector','--index-path',str(path),'--codex-home',str(home)])
        owner.start_collection()
        old.join(3);assert not old.is_alive() and old.exitcode==0
        assert configured and len(processes)==2
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            after=client.poll()
            if after.get('collection',{}).get('pid')==processes[-1].pid:break
            time.sleep(.05)
        assert after['collection']['version']==VERSION and after['sessions']
        assert after['collection']['instance']!=before['collection']['instance']
    finally:
        client.close()
        for process in processes:
            if process.is_alive():process.terminate()
            process.join(3)


@pytest.mark.parametrize('borrowed',[False,True])
@pytest.mark.integration
def test_snapshot_command_cleans_only_its_own_collector(tmp_path,borrowed):
    import json,subprocess,sys
    from pathlib import Path
    from cachemonitor.usage_collection import locked,CollectorService
    from test_core import fixture_home
    home,_=fixture_home(tmp_path);path=tmp_path/'index.sqlite'
    from cachemonitor.model_evidence import EvidenceStore
    store=EvidenceStore(tmp_path/'model-evidence.sqlite')
    try:
        for number in range(513):store.write(home,f'attempt-{number}',10000,'WebSocket',f'response-{number}',status='completed')
    finally:store.close()
    bootstrap="""import sys,runpy
from cachemonitor.observer_task import ObserverTask
def forbidden(*args,**kwargs): raise AssertionError('Snapshot must not access Task Scheduler')
ObserverTask.call=forbidden
sys.argv=sys.argv[1:]
runpy.run_path(sys.argv[0],run_name='__main__')
"""
    service=CollectorService([home],path) if borrowed else None
    try:
        if service:service.poll();epoch=service.epoch
        result=subprocess.run([sys.executable,'-X','utf8','-c',bootstrap,str(Path(__file__).resolve().parents[1]/'run.py'),
            '--snapshot','--codex-home',str(home),'--index-path',str(path)],capture_output=True,text=True,encoding='utf-8',timeout=40)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['sessions']
        assert len(json.loads(result.stdout)['request_activity'])==513
        assert locked(path.with_name(path.name+'.codexon-collector.lock'))==borrowed
        if service:assert not service.stopping()
        if service:assert service.epoch==epoch
    finally:
        if service:service.close()


def test_index_suffixes_have_independent_channels_and_locks(tmp_path):
    from cachemonitor.usage_collection import CollectionClient,CollectorService,empty_snapshot
    services=[];clients=[];home=tmp_path/'home'
    try:
        for name in ('index','index.db','index.sqlite'):
            path=tmp_path/name
            service=CollectorService([home],path);services.append(service)
            client=CollectionClient([home],path);clients.append(client)
            service.channel.publish(empty_snapshot([str(home)],path,100),service.epoch,1)
        assert len({service.channel.snapshot_path for service in services})==3
        assert len({service.lock.path for service in services})==3
        for client,service in zip(clients,services):
            assert client.poll(100)['index']['path']==str(service.channel.path)
    finally:
        for client in clients:client.close()
        for service in services:service.close()


@pytest.mark.parametrize('explicit_index',[False,True])
def test_implicit_and_explicit_effective_evidence_share_scope(tmp_path,monkeypatch,explicit_index):
    from cachemonitor import model_evidence
    from cachemonitor.usage_collection import CollectionClient,CollectorService,empty_snapshot
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    monkeypatch.setattr(model_evidence,'default_path',lambda:tmp_path/'default-evidence.sqlite')
    path=tmp_path/'index.sqlite' if explicit_index else None;home=tmp_path/'home'
    service=CollectorService([home],path)
    client=CollectionClient([home],path,service.channel.default_evidence)
    try:
        service.channel.publish(empty_snapshot([str(home)],service.channel.path,100),service.epoch,1)
        assert client.channel.scope==service.channel.scope
        assert client.poll(100)['errors']==[]
        assert service.channel.live.requested_homes(100)==[str(home)]
        assert service.channel.db.execute('SELECT count(*) FROM consumers').fetchone()[0]==0
    finally:client.close();service.close()


def test_collection_reuses_history_and_atomically_replaces_current_sessions(tmp_path):
    import copy
    import json
    import zlib
    from cachemonitor.usage_collection import CollectionChannel, empty_snapshot
    path=tmp_path/'index.sqlite'
    writer=CollectionChannel([],path);reader=CollectionChannel([],path)
    snapshot=empty_snapshot([],path,100)
    snapshot['sessions']=[dict(home='h',id='s',history=[dict(key='a',output=1)],remaining=10,
                               usage_revision=1,turn_records={'turn':{'state':'running'}})]
    snapshot['request_activity']=[dict(home='h',attempt='a',status='started'),dict(home='h',attempt='b',status='started')]
    try:
        writer.publish(snapshot,'first',1)
        first=reader.read(shared=True)
        assert first==snapshot
        snapshot['ts']=101;snapshot['sessions'][0]['remaining']=9
        writer.publish(snapshot,'first',2)
        second=reader.read(shared=True)
        assert second['sessions'][0]['history'] is first['sessions'][0]['history']
        assert second==snapshot
        assert second['request_activity'][0] is first['request_activity'][0]
        header=reader.read_header()
        assert header['ts']==101 and 'sessions' not in header
        isolated=reader.read();isolated['sessions'][0]['history'][0]['output']=99
        assert reader.read(shared=True)['sessions'][0]['history'][0]['output']==1
        snapshot['sessions'][0]['history'][0]['output']=2
        snapshot['sessions'][0]['turn_records']['turn']['state']='completed'
        snapshot['sessions'][0]['provider']='changed'
        snapshot['request_activity'][0]['status']='completed'
        snapshot['request_activity'].reverse()
        writer.publish(snapshot,'first',4)  # Consumers may miss intermediate revisions.
        assert reader.read()==snapshot
        assert first['sessions'][0]['history'][0]['output']==1
        assert first['sessions'][0]['turn_records']['turn']['state']=='running'
        # Preserve the existing JSON payload contract for dict subclasses.
        from collections import OrderedDict
        snapshot['sessions'][0]['turn_records']=OrderedDict(snapshot['sessions'][0]['turn_records'])
        writer.publish(snapshot,'restart',1)
        assert reader.read()==snapshot
        snapshot['sessions']=[];snapshot['request_activity']=[];writer.publish(snapshot,'restart',2)
        assert reader.read()['sessions']==[]
        assert writer.db.execute('SELECT count(*) FROM sessions').fetchone()[0]==0
        # Upgrade begins with a previous full-blob publication.
        legacy=copy.deepcopy(snapshot);legacy['ts']=102
        with writer.db:
            writer.db.execute('UPDATE snapshot SET epoch=?,sequence=?,payload=?',
                ('legacy',1,zlib.compress(json.dumps(legacy).encode())))
        assert reader.read()==legacy
    finally:reader.close();writer.close()


def test_collection_activity_reordering_preserves_bodies_and_cached_records(tmp_path):
    import json,zlib
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    path=tmp_path/'index.sqlite';writer=CollectionChannel([],path);reader=CollectionChannel([],path)
    original=empty_snapshot([],path,100.)
    original['request_activity']=[dict(home='h',attempt=key,status='started') for key in ('a','b')]
    try:
        writer.publish(original,'epoch',1);first=reader.read(shared=True)
        before=writer.db.execute('SELECT attempt,revision,payload FROM activity ORDER BY attempt').fetchall()
        writer.db.executescript('''
            CREATE TRIGGER keep_activity_body BEFORE UPDATE OF payload,revision ON activity
              BEGIN SELECT RAISE(ABORT,'unchanged activity body was updated'); END;
            CREATE TRIGGER keep_activity_row BEFORE INSERT ON activity
              BEGIN SELECT RAISE(ABORT,'unchanged activity row was replaced'); END;
            CREATE TRIGGER keep_activity_key BEFORE DELETE ON activity
              BEGIN SELECT RAISE(ABORT,'unchanged activity row was deleted'); END;
        ''')
        reordered=copy.deepcopy(original);reordered['ts']=101.;reordered['request_activity'].reverse()
        writer.publish(reordered,'epoch',2);second=reader.read(shared=True)
        assert second==reordered
        assert second['request_activity'][0] is first['request_activity'][1]
        assert second['request_activity'][1] is first['request_activity'][0]
        assert writer.db.execute('SELECT attempt,revision,payload FROM activity ORDER BY attempt').fetchall()==before
        header=json.loads(zlib.decompress(writer.db.execute('SELECT payload FROM snapshot').fetchone()[0]))
        assert header['activity_revision']==2
        unchanged=copy.deepcopy(reordered);unchanged['ts']=102.;writer.publish(unchanged,'epoch',3)
        assert reader.read()==unchanged
        assert writer.activity_revision==2
    finally:reader.close();writer.close()


def test_collection_activity_restart_reuses_matching_rows_and_repairs_corrupt_payload(tmp_path):
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    path=tmp_path/'index.sqlite';writer=CollectionChannel([],path);reader=CollectionChannel([],path)
    original=empty_snapshot([],path,100.)
    original['request_activity']=[dict(home='h',attempt=key,status='started') for key in ('a','b','c','removed')]
    try:
        writer.publish(original,'first',100);assert reader.read()==original
        before=writer.db.execute("SELECT revision,payload FROM activity WHERE attempt='a'").fetchone()
        with writer.db:writer.db.execute("UPDATE activity SET payload=? WHERE attempt='c'",(b'broken-zlib',))
        writer.close();writer=CollectionChannel([],path)
        writer.db.executescript('''
            CREATE TRIGGER keep_activity_body BEFORE UPDATE OF payload,revision ON activity WHEN OLD.attempt='a'
              BEGIN SELECT RAISE(ABORT,'matching activity body was updated'); END;
            CREATE TRIGGER keep_activity_row BEFORE INSERT ON activity WHEN NEW.attempt='a'
              BEGIN SELECT RAISE(ABORT,'matching activity row was replaced'); END;
            CREATE TRIGGER keep_activity_key BEFORE DELETE ON activity WHEN OLD.attempt='a'
              BEGIN SELECT RAISE(ABORT,'matching activity row was deleted'); END;
        ''')
        restarted=copy.deepcopy(original);restarted['ts']=103.
        restarted['request_activity']=[dict(home='h',attempt='b',status='completed'),
            original['request_activity'][0],original['request_activity'][2],dict(home='h',attempt='new',status='started')]
        writer.publish(restarted,'second',1)
        assert reader.read()==restarted
        assert writer.db.execute("SELECT revision,payload FROM activity WHERE attempt='a'").fetchone()==before
        assert {key for key, in writer.db.execute('SELECT attempt FROM activity')}=={'a','b','c','new'}
        assert all(revision>before[0] for revision, in writer.db.execute("SELECT revision FROM activity WHERE attempt<>'a'"))
        # A consumer can miss deletion and re-addition. The restarted sequence
        # must not collide with the retained body's old row revision.
        writer.db.executescript('DROP TRIGGER keep_activity_body; DROP TRIGGER keep_activity_row; DROP TRIGGER keep_activity_key;')
        without=copy.deepcopy(restarted);without['ts']=104.
        without['request_activity']=[row for row in without['request_activity'] if row['attempt']!='a']
        writer.publish(without,'second',2)
        restored=copy.deepcopy(restarted);restored['ts']=105.;restored['request_activity'][1]['status']='failed'
        writer.publish(restored,'second',100)
        assert reader.read()==restored
        fresh=CollectionChannel([],path)
        try:assert fresh.read()==restored
        finally:fresh.close()
    finally:reader.close();writer.close()


@pytest.mark.parametrize('restart',[False,True])
def test_collection_activity_transaction_failure_preserves_prior_publication(tmp_path,restart):
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    path=tmp_path/'index.sqlite';writer=CollectionChannel([],path);reader=CollectionChannel([],path)
    original=empty_snapshot([],path,100.)
    original['request_activity']=[dict(home='h',attempt=key,status='started') for key in ('a','b','c')]
    try:
        writer.publish(original,'first',10);assert reader.read()==original
        before=writer.db.execute('SELECT * FROM activity ORDER BY attempt').fetchall()
        if restart:writer.close();writer=CollectionChannel([],path)
        prior_cache=writer.published_activity.copy()
        writer.db.execute("CREATE TRIGGER interrupt_snapshot BEFORE INSERT ON snapshot BEGIN SELECT RAISE(ABORT,'interrupted activity publication'); END")
        changed=copy.deepcopy(original);changed['ts']=101.
        changed['request_activity']=[dict(home='h',attempt='c',status='completed'),original['request_activity'][0]]
        epoch='second' if restart else 'first';sequence=1 if restart else 11
        with pytest.raises(sqlite3.IntegrityError,match='interrupted activity publication'):
            writer.publish(changed,epoch,sequence)
        assert writer.published_activity==prior_cache
        assert writer.db.execute('SELECT * FROM activity ORDER BY attempt').fetchall()==before
        assert reader.read()==original
        writer.db.execute('DROP TRIGGER interrupt_snapshot')
        writer.publish(changed,epoch,sequence)
        assert reader.read()==changed
        fresh=CollectionChannel([],path)
        try:assert fresh.read()==changed
        finally:fresh.close()
    finally:reader.close();writer.close()


def test_collection_interrupted_transaction_preserves_published_revision(tmp_path):
    import copy,sqlite3
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    path=tmp_path/'custom.sqlite';writer=CollectionChannel([],path);reader=CollectionChannel([],path)
    original=empty_snapshot([],path,100)
    original['sessions']=[dict(home='h',id='s',history=[dict(key='one',output=1)])]
    try:
        writer.publish(original,'first',1);assert reader.read()==original
        updated=copy.deepcopy(original);updated['sessions'][0]['history'][0]['output']=2
        db=writer.db
        class Interrupted:
            @property
            def in_transaction(self):return db.in_transaction
            def __enter__(self):return db.__enter__()
            def __exit__(self,*args):return db.__exit__(*args)
            def executemany(self,*args):return db.executemany(*args)
            def execute(self,sql,*args):
                if 'INSERT OR REPLACE INTO snapshot' in sql:raise sqlite3.OperationalError('interrupted publication')
                return db.execute(sql,*args)
        writer.db=Interrupted()
        with pytest.raises(sqlite3.OperationalError):writer.publish(updated,'first',2)
        writer.db=db
        assert reader.read()==original
        writer.publish(updated,'first',3);assert reader.read()==updated
    finally:
        if 'db' in locals():writer.db=db
        reader.close();writer.close()


def test_idle_collection_preserves_fresh_clocks_without_rewriting_the_large_header(tmp_path):
    import hashlib,json,zlib
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    path=tmp_path/'index.sqlite';writer=CollectionChannel([],path);reader=CollectionChannel([],path)
    snapshot=empty_snapshot([],path,9.5)
    snapshot.update(usage_collection_complete=True,last_usage_collection_success=9.5,
        unassigned=[{'turn':hashlib.sha256(str(i).encode()).hexdigest()} for i in range(2000)])
    snapshot['index'].update(loading=False,done=350,files=350,bytes_read=1048576,version=3,
        usage_complete=True,last_usage_success=9.5)
    try:
        writer.db.execute('PRAGMA wal_autocheckpoint=0')
        writer.publish(snapshot,'running',1000)
        before=reader.read()
        payload_size=writer.db.execute('SELECT length(payload) FROM snapshot WHERE id=1').fetchone()[0]
        wal=writer.snapshot_path.with_name(writer.snapshot_path.name+'-wal')
        initial_size=wal.stat().st_size
        for sequence,(now,success) in enumerate(((10.125,10.),(99.999,99.999),(100.125,None),
                                                (1000.000001,999.5),(100000.125,100000.125)),1001):
            snapshot.update(ts=now,last_usage_collection_success=success)
            snapshot['index']['last_usage_success']=success
            writer.publish(snapshot,'running',sequence)
            assert reader.read()==snapshot
            assert reader.read_header()['ts']==now
            # Released readers only need the existing six columns and zlib JSON.
            row=writer.db.execute('SELECT * FROM snapshot WHERE id=1').fetchone()
            legacy_header=json.loads(zlib.decompress(row[5]))
            assert row[3:5]==(sequence,now)
            assert legacy_header['ts']==now and legacy_header['last_usage_collection_success']==success
            assert legacy_header['index']==snapshot['index']
            assert legacy_header['collection_schema']==2
        assert before['ts']==9.5 and before['last_usage_collection_success']==9.5
        # All five fresh publications together write less than one full header.
        assert wal.stat().st_size-initial_size<payload_size
        # SQLite may change the record width for an integral observed time;
        # freshness must remain exact across that full-record rewrite too.
        snapshot.update(ts=100001.,last_usage_collection_success=100001.)
        snapshot['index']['last_usage_success']=100001.
        writer.publish(snapshot,'running',1006)
        assert reader.read()==snapshot
    finally:reader.close();writer.close()


def test_interrupted_collection_clock_write_rolls_back_the_complete_snapshot(tmp_path):
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    path=tmp_path/'index.sqlite';writer=CollectionChannel([],path);reader=CollectionChannel([],path)
    original=empty_snapshot([],path,100.)
    try:
        writer.publish(original,'running',1000)
        updated={**original,'ts':101.,'last_usage_collection_success':101.}
        db=writer.db
        class InterruptedBlob:
            def __enter__(self):self.blob=db.blobopen('snapshot','payload',1);return self
            def __exit__(self,*args):self.blob.close()
            def seek(self,offset):self.blob.seek(offset)
            def write(self,data):
                self.blob.write(data[:len(data)//2])
                raise sqlite3.OperationalError('interrupted clock publication')
        class Interrupted:
            @property
            def in_transaction(self):return db.in_transaction
            def __enter__(self):return db.__enter__()
            def __exit__(self,*args):return db.__exit__(*args)
            def executemany(self,*args):return db.executemany(*args)
            def execute(self,*args):return db.execute(*args)
            def blobopen(self,*args):return InterruptedBlob()
        writer.db=Interrupted()
        with pytest.raises(sqlite3.OperationalError):writer.publish(updated,'running',1001)
        writer.db=db
        assert reader.read()==original
        assert db.execute('SELECT sequence,observed FROM snapshot WHERE id=1').fetchone()==(1000,100.)
        writer.publish(updated,'running',1002)
        assert reader.read()==updated
    finally:
        if 'db' in locals():writer.db=db
        reader.close();writer.close()


def test_collection_reader_sees_committed_snapshot_during_tail_publication(tmp_path):
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    path=tmp_path/'index.sqlite';writer=CollectionChannel([],path);observer=CollectionChannel([],path)
    original=empty_snapshot([],path,100.)
    original['sessions']=[dict(home='h',id='s',history=[dict(key='one',output=1)])]
    seen=[]
    try:
        writer.publish(original,'running',1000)
        writer.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        updated={**original,'ts':101.,'last_usage_collection_success':101.,
                 'index':{**original['index'],'last_usage_success':101.}}
        db=writer.db
        class ObservedBlob:
            def __enter__(self):self.blob=db.blobopen('snapshot','payload',1);return self
            def __exit__(self,*args):self.blob.close()
            def seek(self,offset):self.blob.seek(offset)
            def write(self,data):
                self.blob.write(data)
                # This observer has no cached header: it decodes the committed row.
                seen.append(observer.read())
        class Observed:
            @property
            def in_transaction(self):return db.in_transaction
            def __enter__(self):return db.__enter__()
            def __exit__(self,*args):return db.__exit__(*args)
            def executemany(self,*args):return db.executemany(*args)
            def execute(self,*args):return db.execute(*args)
            def blobopen(self,*args):return ObservedBlob()
        writer.db=Observed()
        writer.publish(updated,'running',1001)
        writer.db=db
        assert seen==[original]
        assert observer.read()==updated
    finally:
        if 'db' in locals():writer.db=db
        observer.close();writer.close()


def test_idle_collector_keeps_nested_freshness_without_republishing_log_history(tmp_path):
    import hashlib
    from cachemonitor.usage_collection import CollectionClient,CollectorService
    from test_core import fixture_home
    home,_=fixture_home(tmp_path);path=tmp_path/'index.sqlite';now=1800000000.125
    with sqlite3.connect(home/'logs_2.sqlite') as db:
        db.executemany('INSERT INTO logs VALUES(?,?,?,?,?,?,?)',
            [(i+2,int(now),0,'feedback_tags','',hashlib.sha256(str(i).encode()).hexdigest(),
              'request{transport="responses_websocket" api.path="/responses"}: auth_header_attached=true')
             for i in range(2000)])
    service=CollectorService([home],path);reader=CollectionClient([home],path)
    try:
        service.channel.db.execute('PRAGMA wal_autocheckpoint=0')
        service.poll(now);initial=service.poll(now+2)
        assert initial['usage_collection_complete'] and len(initial['unassigned'])==2000
        payload_size=service.channel.db.execute('SELECT length(payload) FROM snapshot WHERE id=1').fetchone()[0]
        wal=service.channel.snapshot_path.with_name(service.channel.snapshot_path.name+'-wal')
        initial_size=wal.stat().st_size
        for tick in range(2,7):
            current=now+tick*2;produced=service.poll(current)
            assert produced['last_usage_collection_success']==current
            assert produced['index']['last_usage_success']==current
            received=reader.poll(current)
            assert {k:v for k,v in received.items() if k not in ('activity_cursor','activity_highwater','activity_more')}==produced
        assert wal.stat().st_size-initial_size<payload_size
    finally:reader.close();service.close()


@pytest.mark.parametrize('replacement',['old_writer','damaged_payload','damaged_tail','short_payload'])
def test_collection_clock_update_recovers_a_replaced_payload(tmp_path,replacement):
    from contextlib import closing
    import hashlib,json,zlib
    from cachemonitor.usage_collection import CollectionChannel,empty_snapshot
    path=tmp_path/'index.sqlite';writer=CollectionChannel([],path);reader=CollectionChannel([],path)
    snapshot=empty_snapshot([],path,100.)
    snapshot['unassigned']=[{'turn':hashlib.sha256(str(i).encode()).hexdigest()} for i in range(1000)]
    try:
        writer.publish(snapshot,'running',1000)
        stored=writer.db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0]
        writer.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        if replacement=='old_writer':payload=zlib.compress(json.dumps(snapshot).encode())
        elif replacement=='damaged_payload':payload=b'bad'+stored[3:]
        elif replacement=='short_payload':payload=stored[:-1]
        else:
            payload=bytearray(stored);payload[len(writer.published_header.prefix)]^=1;payload=bytes(payload)
        # A separate writer retains the metadata and the released INSERT contract.
        with closing(sqlite3.connect(writer.snapshot_path)) as external:
            with external:
                external.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?,?,?,?,?)',
                    (writer.scope,'running',1000,100.,payload))
            assert external.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]==0
        snapshot['ts']=101.
        writer.publish(snapshot,'running',1001)
        assert reader.read()==snapshot
    finally:reader.close();writer.close()
