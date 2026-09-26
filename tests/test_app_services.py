import json
import sys
from types import SimpleNamespace

import pytest

from cachemonitor import app_services as services
from cachemonitor.cache_worker_control import CacheWorkerManager
from cachemonitor.observer_state import read_json
from cachemonitor.proxy_target import ProxyTarget


@pytest.fixture
def running(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir()
    manager=CacheWorkerManager(home,tmp_path/'data'/'index.sqlite',tmp_path/'data'/'evidence.sqlite')
    manager.set_url(manager.url)
    command=[sys.executable,str(tmp_path/'run.py'),'--model-proxy','--cache-observe-only',
             '--codex-home',str(home),'--evidence-path',str(manager.evidence),
             '--observation-index',str(manager.index),'--port',manager.url.rsplit(':',1)[1]]
    runtime={'health':dict(instance='old',version='old',control_id='a'*32), 'work':2,'starts':[],'suspended':[]}
    class Task:
        def __init__(self,*a,role='worker',**k):self.role=role
        def suspend(self):runtime['suspended'].append(self.role)
        def inspect(self):return {'running':0,'autostart':False}
        def start(self,command,autostart=False):
            assert not runtime['health']
            runtime['starts'].append((command,autostart))
            runtime['health']=dict(instance='new',version=services_version(),control_id='b'*32)
    monkeypatch.setattr('cachemonitor.observer_task.ObserverTask',Task)
    manager.task=Task();manager.legacy_task=Task(role='legacy');manager.check_task=Task(role='check')
    def health(**kwargs):
        current=runtime['health']
        control=read_json(manager.directory/('proxy-control-'+'a'*32+'.json'))
        if current and current['instance']=='old' and control.get('action')=='drain':
            if runtime['work']:runtime['work']-=1
            else:runtime['health']=None
        manager.health_state='healthy' if runtime['health'] else 'refused'
        return runtime['health']
    monkeypatch.setattr(manager,'health',health)
    monkeypatch.setattr(ProxyTarget,'capture',lambda self,h:dict(command=command,instance=h['instance'],
        version=h['version'],control_id=h['control_id'],registration={'autostart':True},processes=['old']))
    monkeypatch.setattr(ProxyTarget,'stopped',lambda self,s:not runtime['health'])
    monkeypatch.setattr(ProxyTarget,'exited',lambda self,s:not runtime['health'])
    monkeypatch.setattr(ProxyTarget,'ready',lambda self,h,s,c,v,d=None:bool(h and h['instance']=='new' and h['version']==v))
    monkeypatch.setattr(services.identity,'locks_free',lambda _:True)
    monkeypatch.setattr(services.identity,'port_free',lambda _:True)
    monkeypatch.setattr(services.time,'sleep',lambda _:None)
    return manager,runtime


def services_version():
    from cachemonitor.version import PROXY_VERSION
    return PROXY_VERSION


def test_exit_drains_then_restores_observation_role_without_revoking_preferences(running):
    manager,runtime=running
    from cachemonitor.cache_control import Control
    control=Control(manager.index.with_name('cache-control.sqlite'))
    control.set('automatic',True);control.set('selection:automatic',True)
    messages=[]
    lifecycle=services.AppServices(manager,[str(manager.home)],manager.index,manager.evidence,progress=messages.append)
    lifecycle.stop_proxy()
    assert runtime['health'] is None and runtime['work']==0
    assert manager.config()[1].get('openai_base_url') is None
    assert services.suspended(manager) and not ProxyTarget(manager).enabled()
    assert '진행 중 응답 및 캐시 작업 정산 대기' in messages
    assert {'worker','legacy','check','ProxyUpdate'}.issubset(runtime['suspended'])
    assert control.get('automatic') and control.get('selection:automatic')
    assert services.resume_proxy(manager)
    assert len(runtime['starts'])==1
    command,autostart=runtime['starts'][0]
    assert '--cache-observe-only' in command and '--managed' not in command and not autostart
    assert manager.config()[1]['openai_base_url']==manager.url
    assert not services.suspended(manager)
    assert not services.resume_proxy(manager)
    control.close()


@pytest.mark.parametrize('action',['off','custom-route'])
def test_explicit_off_or_external_route_wins_over_resume(running,action):
    manager,runtime=running
    services.AppServices(manager,[]).stop_proxy()
    if action=='off':services.disable_resume(manager)
    else:manager.set_url('https://example.invalid/v1')
    services.resume_proxy(manager)
    assert not runtime['starts']
    assert manager.config()[1].get('openai_base_url')!=manager.url


def test_update_and_manual_start_cannot_revive_closing_service(running):
    manager,runtime=running
    services.AppServices(manager,[]).save(phase='closing',resume=True)
    from cachemonitor.proxy_update import ProxyUpdate,UpdateCancelled
    with pytest.raises(UpdateCancelled):ProxyUpdate(manager).allowed()
    with pytest.raises(RuntimeError,match='종료'):manager.turn_on()
    assert not runtime['starts'] and runtime['health']


def test_gui_start_during_update_does_not_register_the_old_worker(running,monkeypatch):
    manager,runtime=running
    services.atomic_write(manager.directory/'proxy-update.json',b'{"phase":"starting"}')
    monkeypatch.setattr(manager,'status',lambda:{'update':'starting'})
    assert manager.resume()=={'update':'starting'}
    assert not runtime['starts']


def test_collection_stays_stopped_until_explicit_gui_launch(tmp_path,monkeypatch):
    from cachemonitor.usage_collection import CollectionClient,CollectorService,resume_collection
    home=tmp_path/'home';path=tmp_path/'index.sqlite'
    collector=CollectorService([home],path);client=CollectionClient([home],path)
    calls=[]
    monkeypatch.setattr('cachemonitor.observer_task.ObserverTask.start',lambda *a,**k:calls.append('start'))
    try:
        services.atomic_write(client.channel.companion('.session.json'),json.dumps(
            dict(scope=client.channel.scope,stopped=True)).encode())
        assert collector.stopping()
        client.ensure_service(100,None);assert not calls
        resume_collection([home],path)
        assert not collector.stopping()
        client.ensure_service(101,None);assert calls==['start']
    finally:client.close();collector.close()


def test_reopened_observer_keeps_owned_custom_port(tmp_path):
    from cachemonitor.observer_control import ObserverManager
    from cachemonitor.model_evidence import home_key
    home=tmp_path/'home';directory=tmp_path/'data'
    manager=ObserverManager(home,directory,url='http://127.0.0.1:18782')
    manager.write_state(dict(home=home_key(home),url=manager.url,enabled=True))
    assert ObserverManager(home,directory).url==manager.url
    assert ObserverManager(tmp_path/'other-home',directory).url!=manager.url


def test_exit_receipt_uses_actual_legacy_role_capability(tmp_path,monkeypatch):
    from cachemonitor.observer_control import ObserverManager
    manager=ObserverManager(tmp_path/'home',tmp_path/'data')
    target=ProxyTarget(manager)
    monkeypatch.setattr(target,'exited',lambda _:True)
    monkeypatch.setattr(services.identity,'port_free',lambda _:True)
    monkeypatch.setattr(services.identity,'locks_free',lambda _:True)
    source=dict(lifecycle_revision=2,control_id='a'*32,command=['old.exe','--proxy-supervisor'])
    assert target.stopped(source)  # Legacy standalone child never wrote receipts.
    source['flush_receipt_required']=True
    assert not target.stopped(source)
    services.atomic_write(manager.directory/('proxy-control-'+'a'*32+'.json'),json.dumps(
        dict(action='stopped',storage_flushed=True)).encode())
    assert target.stopped(source)


@pytest.mark.parametrize('wrong_index',[False,True])
def test_legacy_collector_control_checks_real_index_before_retirement(tmp_path,monkeypatch,wrong_index):
    import sqlite3,zlib
    from cachemonitor.collection_lifecycle import retire_legacy
    from cachemonitor.usage_collection import CollectionChannel
    from cachemonitor.observer_state import ProcessLock
    channel=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    path=channel.path.with_suffix('.collection.sqlite')
    snapshot=dict(index={'path':str(tmp_path/'other.sqlite' if wrong_index else channel.path)},
        homes=channel.homes,collection=dict(pid=123,instance='legacy'))
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE snapshot(id INTEGER,scope TEXT,payload BLOB)')
        db.execute('CREATE TABLE control(instance TEXT PRIMARY KEY,action TEXT)')
        db.execute('INSERT INTO snapshot VALUES(1,?,?)',(str(channel.default_evidence),zlib.compress(json.dumps(snapshot).encode())))
    owner=ProcessLock(channel.path.with_suffix('.collector.lock'));owner.__enter__()
    process=dict(pid=123,executable=str(tmp_path/'old.exe'),created=1)
    alive=[True]
    monkeypatch.setattr(services.identity,'process_identity',lambda _:process)
    monkeypatch.setattr(services.identity,'same_process',lambda _:alive[0])
    monkeypatch.setattr(services.identity,'process_command',lambda _:[process['executable'],'--usage-collector',
        '--index-path',str(channel.path),'--codex-home',channel.homes[0]])
    def finish(_):
        with sqlite3.connect(path) as db:assert db.execute('SELECT action FROM control').fetchone()==('stop',)
        alive[0]=False;owner.__exit__(None,None,None)
    try:
        if wrong_index:
            with pytest.raises(RuntimeError,match='다른 색인'):retire_legacy(channel,sleep=finish)
            with sqlite3.connect(path) as db:assert not db.execute('SELECT * FROM control').fetchall()
            assert alive[0]
        else:assert retire_legacy(channel,sleep=finish) and not alive[0]
    finally:owner.__exit__(None,None,None);channel.close()


@pytest.mark.parametrize('changed',['created','home','evidence'])
def test_collector_control_rejects_reused_pid_and_wrong_command_scope(tmp_path,monkeypatch,changed):
    from cachemonitor.collection_lifecycle import collector_process
    from cachemonitor.usage_collection import CollectionChannel
    channel=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    process=dict(pid=123,created=2,executable=str(tmp_path/'collector'))
    command=[process['executable'],'--usage-collector','--index-path',str(channel.path),
             '--codex-home',channel.homes[0],'--evidence-path',str(channel.default_evidence)]
    snapshot=dict(homes=channel.homes,collection=dict(pid=123,instance='old',
        executable=process['executable'],process_created=2))
    if changed=='created':snapshot['collection']['process_created']=1
    if changed=='home':command[command.index('--codex-home')+1]=str(tmp_path/'other-home')
    if changed=='evidence':command[-1]=str(tmp_path/'other-evidence.sqlite')
    monkeypatch.setattr(services.identity,'process_identity',lambda _:process)
    monkeypatch.setattr(services.identity,'process_command',lambda _:command)
    monkeypatch.setattr(services.identity,'same_process',lambda _:True)
    try:
        with pytest.raises(RuntimeError,match='소유권'):collector_process(channel,snapshot)
        assert not channel.db.execute('SELECT * FROM control').fetchall()
    finally:channel.close()


def test_quit_waits_responsively_and_handoff_does_not_stop_services(tmp_path,monkeypatch):
    import threading
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration',True)
    release=threading.Event();entered=threading.Event();finished=[]
    def stop(self):entered.set();assert release.wait(10)
    monkeypatch.setattr(services.AppServices,'stop',stop)
    window=Dashboard([str(tmp_path/'home')],start_worker=False,live_limits=False,
        index_path=str(tmp_path/'index.sqlite'),settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    monkeypatch.setattr(window,'finish_quit',lambda:finished.append(True))
    try:
        window.manage_observer=True
        window.quit_app()
        assert entered.wait(2) and not finished and not window.quitting
        for _ in range(3):app.processEvents();QTest.qWait(10)
        assert window.shutdown_operation.isRunning()
        window.quit_app();release.set()
        for _ in range(200):
            app.processEvents();QTest.qWait(10)
            if finished:break
        assert finished==[True]
        window._closing=False;entered.clear()
        window.quit_app(handoff=True)
        assert finished==[True,True] and not entered.is_set()
    finally:
        release.set()
        if getattr(window,'shutdown_operation',None):window.shutdown_operation.wait()
        window.quitting=True;window.close();window.deleteLater();app.processEvents()
