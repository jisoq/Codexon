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
    monkeypatch.setattr(analysis_worker,'UsageIndex',Index)
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
    monkeypatch.setattr(analysis_worker,'UsageIndex',Index)
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
    gui=CollectionClient([home],index,autostart=False)
    worker=CollectionClient([home],index,worker=True,autostart=False)
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


def test_service_adapts_legacy_worker_without_gui_index_access(tmp_path,monkeypatch):
    from cachemonitor.index import UsageIndex
    from cachemonitor.usage_collection import CollectionClient,CollectorService
    from cachemonitor.observer_state import ProcessLock
    from test_core import fixture_home
    home,_=fixture_home(tmp_path);path=tmp_path/'index.sqlite'
    legacy=UsageIndex([home],path);legacy.poll(10010)
    service=CollectorService([home],path);gui=CollectionClient([home],path,autostart=False)
    try:
        with ProcessLock(tmp_path/'cache-worker.lock'):
            snapshot=service.poll(10011)
            assert snapshot['collection']['mode']=='indexed_compatibility'
            assert service.index.read_only and service.index.bytes_read==0
            with pytest.raises(sqlite3.OperationalError):service.index.db.execute('DELETE FROM events')
            def no_index(*args,**kwargs):raise AssertionError('GUI opened source index')
            with monkeypatch.context() as m:
                m.setattr(UsageIndex,'__init__',no_index)
                assert gui.poll(10011)['sessions']
        legacy.close()
        assert service.poll(10012)['collection']['mode']=='dedicated'
        assert not service.index.read_only
    finally:legacy.close();gui.close();service.close()


def test_missing_or_stalled_service_only_requests_service_restart(tmp_path,monkeypatch):
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
        assert starts[0][0]=='UsageCollector' and '--usage-collector' in starts[0][1]
        gui.poll(10046);assert len(starts)==1
        assert not hasattr(gui,'index') and not hasattr(gui,'collector')
    finally:gui.close()


def test_service_collects_all_requested_homes(tmp_path):
    from cachemonitor.usage_collection import CollectionClient,CollectorService
    from test_core import fixture_home
    (tmp_path/'one').mkdir();(tmp_path/'two').mkdir()
    first,_=fixture_home(tmp_path/'one');second,_=fixture_home(tmp_path/'two')
    path=tmp_path/'index.sqlite'
    service=CollectorService([first],path)
    gui=CollectionClient([second,first],path,autostart=False)
    worker=CollectionClient([first],path,worker=True,autostart=False)
    try:
        service.poll(10010);assert gui.poll(10010)['index']['loading']
        service.poll(10011);both=gui.poll(10011);own=worker.poll(10011)
        assert {s['home'] for s in both['sessions']}=={str(first),str(second)}
        assert {s['home'] for s in own['sessions']}=={str(first)}
        assert not both['index']['loading']
    finally:gui.close();worker.close();service.close()


def _collect_in_process(home,path):
    import time
    from cachemonitor.usage_collection import CollectorService
    service=CollectorService([home],path)
    try:
        while True:
            service.poll();time.sleep(.05)
    finally:service.close()


def test_service_survives_gui_close_and_restarts_as_separate_process(tmp_path,monkeypatch):
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
    producer=launch();gui=CollectionClient([home],path,autostart=False)
    try:
        until=time.monotonic()+10
        while time.monotonic()<until:
            first=gui.poll()
            if first.get('collection',{}).get('pid')==producer.pid:break
            time.sleep(.05)
        assert first['collection']['pid']==producer.pid
        gui.close()
        with record.open('a',encoding='utf-8') as stream:stream.write(json.dumps(usage('after-gui-close',time=10020))+'\n')
        gui=CollectionClient([home],path,autostart=False)
        until=time.monotonic()+5
        while time.monotonic()<until:
            snapshot=gui.poll()
            if any(r['key']=='after-gui-close' for s in snapshot['sessions'] for r in s['history']):break
            time.sleep(.05)
        assert any(r['key']=='after-gui-close' for s in snapshot['sessions'] for r in s['history'])
        assert snapshot['collection']['pid']==producer.pid
        producer.terminate();producer.join(5)
        monkeypatch.setattr(ObserverTask,'start',lambda *args,**kwargs:launch())
        gui.autostart=True;stale=gui.poll(time.time()+31)
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
