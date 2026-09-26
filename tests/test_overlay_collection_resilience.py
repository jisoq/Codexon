"""Optional quota persistence cannot suppress observed session/overlay usage."""
import copy
import sqlite3

import pytest

from cachemonitor import analysis_worker
from test_overlay import source


@pytest.mark.parametrize('failure_stage',['init','enrich','sync','import'])
def test_quota_storage_failure_keeps_publishing_real_calls(monkeypatch,tmp_path,failure_stage):
    session=source()
    original=copy.deepcopy(session)
    snapshots=[]
    index_paths=[]
    quota_paths=[]
    class Index:
        path='unused-index.sqlite'
        def __init__(self,*args):index_paths.append(args[1])
        def poll(self):
            return dict(ts=200,sessions=[copy.deepcopy(session)],homes=[session['home']],
                        errors=[],unassigned=[],index={'loading':False})
        def close(self):pass
    class Ledger:
        def __init__(self,*args):
            quota_paths.append(args[0])
            self.db=self
            if failure_stage=='init':raise sqlite3.DatabaseError('database disk image is malformed')
        def enrich_modes(self,snapshot):
            if failure_stage=='enrich':raise sqlite3.DatabaseError('database disk image is malformed')
        def sync(self,*args):
            if failure_stage=='sync':raise sqlite3.DatabaseError('database disk image is malformed')
        def import_observations(self,*args):
            if failure_stage=='import':raise sqlite3.DatabaseError('database disk image is malformed')
        def rollback(self):pass
        def close(self):pass
    class Connection:
        def poll(self,*args):return bool(snapshots)
        def recv(self):return {'kind':'stop'}
        def send(self,value):snapshots.append(value)
        def close(self):pass
    monkeypatch.setattr(analysis_worker,'CollectionClient',Index)
    monkeypatch.setattr(analysis_worker,'QuotaLedger',Ledger)
    analysis_worker.process_main(Connection(),['home'],tmp_path/'index.sqlite')
    assert index_paths==[tmp_path/'index.sqlite']
    assert quota_paths==[analysis_worker.ledger_path(tmp_path/'index.sqlite')]
    assert len(snapshots)==1 and snapshots[0]['kind']=='snapshot'
    snapshot=snapshots[0]['value']
    assert snapshot['ledger_error']=='한도 기록 확인 지연 · 자동 재시도 중'
    assert not snapshot['errors']
    overlay=snapshot['overlay_sessions'][0]
    assert overlay['calls']==len(original['history']) and overlay['latest_key']==original['history'][-1]['key']
    assert [row['key'] for row in overlay['recent']]==[row['key'] for row in original['history']]
    assert session==original


def test_usage_ledger_retries_after_transient_failure(monkeypatch,tmp_path):
    from types import SimpleNamespace
    clock=[100.0]
    monkeypatch.setattr(analysis_worker,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    snapshots=[]
    writes=[]
    opens=[]
    paths=[]
    quota_path=tmp_path/'retained-quota.sqlite'
    session=source()
    class Index:
        path='unused'
        def __init__(self,*args):pass
        def poll(self):return dict(ts=clock[0],sessions=[copy.deepcopy(session)],homes=[session['home']],
            errors=[],unassigned=[],index={'loading':False})
        def close(self):pass
    class Ledger:
        def __init__(self,*args):self.db=self;opens.append(clock[0]);paths.append(args[0])
        def enrich_modes(self,*args):pass
        def sync(self,*args):
            writes.append(clock[0])
            if len(writes)==1:raise sqlite3.OperationalError('locked')
        def import_observations(self,*args):pass
        def rollback(self):pass
        def close(self):pass
    class Connection:
        def poll(self,timeout=0):
            if timeout:clock[0]+=1
            return len(writes)>=2
        def recv(self):return {'kind':'stop'}
        def send(self,value):snapshots.append(value)
        def close(self):pass
    monkeypatch.setattr(analysis_worker,'CollectionClient',Index)
    monkeypatch.setattr(analysis_worker,'QuotaLedger',Ledger)
    analysis_worker.process_main(Connection(),['h'],tmp_path/'index.sqlite',quota_path=quota_path)
    assert len(opens)==2 and opens[1]-opens[0]>=5
    assert paths==[quota_path,quota_path]
    from cachemonitor.quota_service import QuotaService
    assert QuotaService(tmp_path/'home',tmp_path/'different-index.sqlite',live=False,quota_path=quota_path).path==quota_path
    assert snapshots[0]['value']['ledger_error']
    assert 'ledger_error' not in snapshots[-1]['value']


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
            assert snapshot==json.loads(json.dumps(produced))
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
        assert snapshot['sessions'] and snapshot['errors']
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
        assert stale['errors'] and not hasattr(gui,'index')
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
def test_snapshot_command_cleans_only_its_own_collector(tmp_path,borrowed):
    import json,subprocess,sys
    from pathlib import Path
    from cachemonitor.usage_collection import locked,CollectorService
    from cachemonitor.observer_task import ObserverTask
    from test_core import fixture_home
    home,_=fixture_home(tmp_path);path=tmp_path/'index.sqlite'
    task=ObserverTask(str(path),role='UsageCollector')
    before=task.inspect() if sys.platform=='win32' else None
    service=CollectorService([home],path) if borrowed else None
    try:
        if service:service.poll()
        result=subprocess.run([sys.executable,'-X','utf8',str(Path(__file__).resolve().parents[1]/'run.py'),
            '--snapshot','--codex-home',str(home),'--index-path',str(path)],capture_output=True,text=True,encoding='utf-8',timeout=40)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['sessions']
        assert locked(path.with_name(path.name+'.codexon-collector.lock'))==borrowed
        if service:assert not service.stopping()
        if before is not None:assert task.inspect()==before
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
        assert service.channel.db.execute('SELECT count(*) FROM consumers').fetchone()[0]==1
    finally:client.close();service.close()
