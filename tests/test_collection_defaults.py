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


def test_failed_migration_preserves_original_and_releases_collector_lock(default_home):
    from cachemonitor.usage_collection import locked
    legacy_index_location().parent.mkdir(parents=True)
    with sqlite3.connect(legacy_index_location()) as db:db.execute('CREATE TABLE unrelated(value TEXT)')
    with pytest.raises(RuntimeError,match='형식'):CollectorService([default_home])
    assert not index_location().exists() and legacy_index_location().exists()
    assert not list(index_location().parent.glob('*.migration-*.tmp'))
    assert not locked(index_location().with_name(index_location().name+'.codexon-collector.lock'))


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
        assert after['sessions'][0]['totals']==before['sessions'][0]['totals']
        assert operations==(['configure','start'] if alive else ['start'])
        current[-1].close();current.clear()
        with monkeypatch.context() as patch:
            patch.setattr(services.identity,'process_identity',lambda _:process)
            restarted=CollectorService([default_home])
        current.append(restarted);restarted.poll(10004)
        assert client.poll(10004)['sessions'][0]['totals']==after['sessions'][0]['totals']
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
