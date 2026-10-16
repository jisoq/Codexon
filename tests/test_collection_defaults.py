"""Default installation paths, upgrade recovery, and preserved usage history."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest

from cachemonitor import index as index_module
from cachemonitor.app_services import AppServices
from cachemonitor.index import UsageIndex
from cachemonitor.usage_collection import CollectionClient,CollectorService,CollectionScopeError,collector_command
from cachemonitor.usage_paths import index_location,legacy_index_location
from test_core import fixture_home


@pytest.fixture
def default_home(tmp_path,monkeypatch):
    profile=tmp_path/'profile';profile.mkdir()
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    monkeypatch.setenv('USERPROFILE',str(profile))
    monkeypatch.setenv('HOME',str(profile))
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:profile))
    return fixture_home(tmp_path)[0]


@pytest.mark.integration
def test_default_cli_publishes_usage_and_preserves_shared_evidence(default_home):
    from cachemonitor.model_evidence import default_path
    client=CollectionClient([default_home]);child=None
    try:
        command=collector_command(client.channel)
        assert '--default-index' in command
        child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        deadline=time.monotonic()+20;snapshot={}
        while time.monotonic()<deadline:
            snapshot=client.poll()
            if snapshot.get('usage_collection_complete'):break
            if child.poll() is not None:pytest.fail(child.communicate()[1].decode(errors='replace'))
            time.sleep(.05)
        assert snapshot['usage_collection_complete'] and snapshot['sessions']
        assert snapshot['index']['path']==str(index_location())
        assert snapshot['last_usage_collection_success'] and not snapshot['usage_errors']
        assert client.channel.scope==os.path.normcase(str(default_path().resolve()))
        with sqlite3.connect(client.channel.snapshot_path) as db:
            db.execute('INSERT INTO control VALUES(?,?)',(snapshot['collection']['instance'],'stop'))
        assert child.wait(timeout=10)==0
    finally:
        if child and child.poll() is None:child.terminate();child.wait(timeout=10)
        client.close()


def test_conflicting_default_cli_path_fails_before_creating_files(default_home,tmp_path):
    target=tmp_path/'conflicting.sqlite'
    result=subprocess.run([sys.executable,'run.py','--usage-collector','--codex-home',str(default_home),
        '--index-path',str(target),'--default-index'],capture_output=True,timeout=20)
    assert result.returncode==2 and b'--default-index requires' in result.stderr
    assert not target.exists() and not index_location().exists()


def test_default_migration_preserves_wal_records_and_never_overwrites(default_home):
    from cachemonitor.model_evidence import default_path
    old=UsageIndex([default_home],legacy_index_location())
    try:
        old.poll(10002)
        old.db.execute("INSERT INTO metadata VALUES('retained','session','{}')");old.db.commit()
        service=CollectorService([default_home]);client=CollectionClient([default_home])
        try:
            snapshot=service.poll(10003)
            assert client.poll(10003)['sessions'] and not snapshot['usage_errors']
            assert service.index.path==client.path==index_location()
            assert service.index.model_evidence.path==default_path()
            assert not default_path().with_name('cache-control.sqlite').exists()
            assert service.index.db.execute("SELECT data FROM metadata WHERE home='retained'").fetchone()==('{}',)
            assert legacy_index_location().exists()
            service.index.db.execute("UPDATE metadata SET data='[]' WHERE home='retained'");service.index.db.commit()
        finally:client.close();service.close()
        resumed=CollectorService([default_home])
        try:
            resumed.poll(10004)
            assert resumed.index.db.execute("SELECT data FROM metadata WHERE home='retained'").fetchone()==('[]',)
            assert old.db.execute("SELECT data FROM metadata WHERE home='retained'").fetchone()==('{}',)
        finally:resumed.close()
    finally:old.close()


def test_custom_index_does_not_import_default_history(default_home,tmp_path):
    legacy_index_location().parent.mkdir(parents=True)
    legacy_index_location().write_bytes(b'not a database')
    path=tmp_path/'custom.sqlite';service=CollectorService([default_home],path)
    try:
        service.poll(10002)
        assert service.index.path==path
        assert service.index.model_evidence.path==path.with_name('model-evidence.sqlite')
    finally:service.close()


def test_closed_collection_reuses_bodies_and_replays_only_changed_owners(tmp_path,monkeypatch):
    from cachemonitor.model_evidence import EvidenceStore
    from cachemonitor.usage_collection import CollectionChannel
    from test_core import TID,usage
    home,path=fixture_home(tmp_path)
    other='other';second=home/'second.jsonl'
    second.write_text(json.dumps(usage('second',tid=other))+'\n',encoding='utf8')
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('INSERT INTO threads(id,rollout_path,name) VALUES(?,?,?)',(other,str(second),'Second'))
    evidence=tmp_path/'evidence.sqlite';store=EvidenceStore(evidence)
    store.write(home,'attempt',10000,'WebSocket','r1','model-a','model-a','completed')
    store.close()
    target=tmp_path/'index.sqlite';rebuilt=[]
    original=UsageIndex.rebuild_steps
    def replay(index,key,now):
        rebuilt.append(key[1])
        yield from original(index,key,now)
    monkeypatch.setattr(UsageIndex,'rebuild_steps',replay)
    def collect(service,now):
        deadline=time.monotonic()+5
        while True:
            value=service.poll(now)
            if value['usage_collection_complete'] and not value['index']['loading']:break
            assert time.monotonic()<deadline
        reader=CollectionChannel([home],target,evidence)
        try:value=reader.read(shared=True)
        finally:reader.close()
        return {row['id']:{k:v for k,v in row.items() if k not in ('usage_revision','request_revision')}
                for row in value['sessions']}
    service=CollectorService([home],target,evidence)
    try:
        first=collect(service,20000)
        assert service.index.replay.db.execute('SELECT count(*) FROM owners').fetchone()[0]==2
        assert all(not session.requests for session in service.index.monitor.sessions.values())
    finally:service.close()
    rebuilt.clear();service=CollectorService([home],target,evidence)
    try:
        assert collect(service,20001)==first and not rebuilt
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET name=? WHERE id=?',('Renamed',other))
        renamed=collect(service,20012)
        assert set(rebuilt)=={other} and renamed[other]['title']=='Renamed'
        assert renamed[other]['history']==first[other]['history']
        first[other]=renamed[other];rebuilt.clear()
        # An active request resumes just its owner, including preserved history.
        with path.open('a',encoding='utf8') as output:output.write(json.dumps(usage('new',time=20014))+'\n')
        changed=collect(service,20015)
        assert set(rebuilt)=={TID} and len(changed[TID]['history'])==2
        assert changed[other]==first[other]
    finally:service.close()
    # A past observation edit with an unchanged maximum sequence is still seen.
    with sqlite3.connect(evidence) as db:db.execute("UPDATE model_observations SET requested_model='model-b' WHERE response_id='r1'")
    rebuilt.clear();service=CollectorService([home],target,evidence)
    try:
        changed=collect(service,24000)
        assert set(rebuilt)=={TID}
        assert next(row for row in changed[TID]['history'] if row['key']=='r1')['model']=='model-b'
    finally:service.close()
    # A malformed derived header is ignored without blocking source collection.
    with sqlite3.connect(service.channel.companion('.collection-replay.sqlite')) as db:
        db.execute('UPDATE owners SET header=CAST(header AS TEXT) WHERE sid=?',(other,))
    rebuilt.clear();service=CollectorService([home],target,evidence)
    try:
        assert collect(service,24001)==changed and set(rebuilt)=={other}
    finally:service.close()
    # Deleted source records and a damaged derived body both use ordinary replay.
    path.write_text(json.dumps(usage('new',time=20014))+'\n',encoding='utf8')
    with sqlite3.connect(service.channel.snapshot_path) as db:
        db.execute('UPDATE sessions SET payload=? WHERE sid=?',(b'corrupt',other))
    rebuilt.clear();service=CollectorService([home],target,evidence)
    try:
        changed=collect(service,24002)
        assert set(rebuilt)=={TID,other}
        assert [row['key'] for row in changed[TID]['history']]==['new']
        assert changed[other]==first[other]
        # Losing a not-yet-committed derived proof cannot lose public history.
        service.index.replay.db.rollback()
    finally:service.close()
    rebuilt.clear();service=CollectorService([home],target,evidence)
    try:
        assert collect(service,24003)==changed
        assert TID in rebuilt
    finally:service.close()


def test_failed_migration_preserves_original_and_releases_collector_lock(default_home):
    from cachemonitor.usage_collection import locked
    legacy_index_location().parent.mkdir(parents=True)
    with sqlite3.connect(legacy_index_location()) as db:db.execute('CREATE TABLE unrelated(value TEXT)')
    with pytest.raises(RuntimeError,match='형식'):CollectorService([default_home])
    assert not index_location().exists() and legacy_index_location().exists()
    assert not list(index_location().parent.glob('*.migration-*.tmp'))
    assert not locked(index_location().with_name(index_location().name+'.codexon-collector.lock'))


def _interrupt_index_copy(home,control):
    class Interrupted(sqlite3.Connection):
        def backup(self,target):
            # Force destination spill with a small fixture, so termination
            # interrupts actual file writes rather than an in-memory copy.
            target.execute('PRAGMA cache_size=-64')
            def copied(status,remaining,total):
                if 0<remaining<=total//2:
                    stage=index_location().with_name(index_location().name+'.migration-stage.tmp')
                    control.send(('copy transaction',stage.stat().st_size));control.recv()
            return super().backup(target,pages=1,progress=copied)
    original=sqlite3.connect
    def connect(*args,**kwargs):return original(*args,**kwargs,factory=Interrupted)
    sqlite3.connect=connect
    service=CollectorService([home])
    service.close()


@pytest.mark.integration
def test_interrupted_default_migration_reuses_one_owned_slot(default_home):
    import hashlib
    import multiprocessing as mp
    old=UsageIndex([default_home],legacy_index_location())
    payload=json.dumps({'retained':'x'*524288})
    try:
        old.poll(10002)
        old.db.execute('INSERT INTO metadata VALUES(?,?,?)',('retained','copy-proof',payload))
        old.db.commit()
        original_events=old.db.execute('SELECT * FROM events ORDER BY path,pos').fetchall()
    finally:old.close()
    source=legacy_index_location();original=hashlib.sha256(source.read_bytes()).digest()
    destination=index_location();stage=destination.with_name(destination.name+'.migration-stage.tmp')
    receipt=destination.with_name(destination.name+'.migration-stage.json')
    for _ in range(3):
        context=mp.get_context('spawn');parent,pipe=context.Pipe()
        child=context.Process(target=_interrupt_index_copy,args=(str(default_home),pipe))
        child.start();pipe.close()
        try:
            assert parent.poll(20),f'copy did not start; exit={child.exitcode}'
            phase,written=parent.recv()
            assert phase=='copy transaction' and written>0
            child.kill();child.join(10)
            assert not child.is_alive() and child.exitcode!=0
            assert stage.exists() and receipt.exists() and not destination.exists()
            assert list(destination.parent.glob('*.migration-*.tmp'))==[stage]
            assert hashlib.sha256(source.read_bytes()).digest()==original
        finally:
            if child.is_alive():child.kill()
            child.join(10);parent.close()
    resumed=CollectorService([default_home])
    try:
        assert resumed.poll(10003)['sessions']
        assert resumed.index.db.execute('SELECT * FROM events ORDER BY path,pos').fetchall()==original_events
        assert resumed.index.db.execute("SELECT data FROM metadata WHERE home='retained' AND tid='copy-proof'").fetchone()[0]==payload
        assert resumed.index.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not stage.exists() and not receipt.exists()
        assert hashlib.sha256(source.read_bytes()).digest()==original
    finally:resumed.close()


def test_default_migration_preserves_unowned_staging_file(default_home):
    old=UsageIndex([default_home],legacy_index_location());old.close()
    destination=index_location();destination.parent.mkdir(parents=True,exist_ok=True)
    stage=destination.with_name(destination.name+'.migration-stage.tmp');stage.write_bytes(b'unowned')
    with pytest.raises(RuntimeError,match='소유권'):CollectorService([default_home])
    assert stage.read_bytes()==b'unowned' and not destination.exists()


@pytest.mark.parametrize('alive',[False,True])
def test_broken_default_snapshot_is_replaced_and_restart_keeps_usage(default_home,monkeypatch,alive):
    from cachemonitor import app_services as services
    from cachemonitor.observer_task import ObserverTask
    old=CollectorService([default_home]);current=[];operations=[]
    with monkeypatch.context() as patch:
        patch.setattr(index_module,'index_location',lambda path=None:legacy_index_location())
        before=old.poll(10002)
    before['collection']['version']='2026.09.27.1'
    old.channel.publish(before,old.epoch,2)
    client=CollectionClient([default_home]);process={'pid':os.getpid(),'executable':before['collection']['executable'],'created':1}
    live=[alive]
    if not alive:old.close()
    def identify(pid):
        if not live[0]:raise PermissionError('recycled PID access denied')
        return process
    monkeypatch.setattr(services.identity,'process_identity',identify)
    monkeypatch.setattr(services.identity,'same_process',lambda _:live[0])
    monkeypatch.setattr(services.identity,'process_command',lambda _:collector_command(client.channel))
    monkeypatch.setattr(ObserverTask,'inspect',lambda self:{'running':0})
    monkeypatch.setattr(ObserverTask,'configure',lambda *a,**k:operations.append('configure'))
    def start(self,command,autostart=False):
        assert not live[0]
        operations.append('start')
        with monkeypatch.context() as patch:
            patch.setattr(services.identity,'process_identity',lambda _:process)
            current.append(CollectorService([default_home]))
        current[-1].poll(10003)
    monkeypatch.setattr(ObserverTask,'start',start)
    def wait(done,message,seconds=30):
        assert old.stopping();old.close();live[0]=False;assert done()
    owner=AppServices(None,[str(default_home)]);monkeypatch.setattr(owner,'wait',wait)
    try:
        rejected=client.poll(10002)
        assert not rejected['sessions'] and rejected['usage_errors']==rejected['errors']
        assert rejected['usage_errors'] and not rejected['index']['loading']
        with pytest.raises(CollectionScopeError):client.channel.read_header()
        owner.start_collection()
        after=client.poll(10003)
        assert after['sessions'] and not after['usage_errors']
        assert after['sessions'][0]['history']==before['sessions'][0]['history']
        assert operations==(['configure','start'] if alive else ['start'])
        current[-1].close();current.clear()
        with monkeypatch.context() as patch:
            patch.setattr(services.identity,'process_identity',lambda _:process)
            restarted=CollectorService([default_home])
        current.append(restarted);restarted.poll(10004)
        assert client.poll(10004)['sessions'][0]['history']==after['sessions'][0]['history']
    finally:
        client.close()
        if old.channel.db:old.close()
        for service in current:service.close()


@pytest.mark.parametrize('failure',['home','evidence','index','command','executable','timeout'])
def test_broken_default_recovery_preserves_unowned_or_busy_collector(default_home,monkeypatch,failure):
    from cachemonitor import app_services as services
    from cachemonitor.observer_task import ObserverTask
    old=CollectorService([default_home])
    with monkeypatch.context() as patch:
        patch.setattr(index_module,'index_location',lambda path=None:legacy_index_location())
        snapshot=old.poll(10002)
    snapshot['collection']['version']='2026.09.27.1'
    if failure=='home':snapshot['homes']=[str(default_home.parent/'other')]
    if failure=='index':snapshot['index']['path']=str(default_home.parent/'unrelated.sqlite')
    if failure=='executable':snapshot['collection']['executable']=str(default_home.parent/'other.exe')
    old.channel.publish(snapshot,old.epoch,2)
    if failure=='evidence':old.channel.db.execute("UPDATE snapshot SET scope='other'");old.channel.db.commit()
    process={'pid':os.getpid(),'executable':old.executable,'created':1}
    monkeypatch.setattr(services.identity,'process_identity',lambda _:process)
    monkeypatch.setattr(services.identity,'same_process',lambda _:True)
    command=collector_command(old.channel)
    if failure=='command':command.remove('--default-index')
    monkeypatch.setattr(services.identity,'process_command',lambda _:command)
    monkeypatch.setattr(ObserverTask,'configure',lambda *a,**k:None)
    monkeypatch.setattr(ObserverTask,'start',lambda *a,**k:pytest.fail('must not start a replacement'))
    owner=AppServices(None,[str(default_home)])
    def timeout(*a,**k):raise RuntimeError('종료 대기')
    monkeypatch.setattr(owner,'wait',timeout)
    try:
        with pytest.raises(RuntimeError):owner.start_collection()
        assert not index_location().exists()
        assert old.stopping()==(failure=='timeout')
    finally:old.close()
