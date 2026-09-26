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
    runtime={'health':dict(instance='old',version='old',control_id='a'*32,cache_management=True), 'work':2,'starts':[],'suspended':[]}
    class Task:
        def __init__(self,*a,role='worker',**k):self.role=role
        def suspend(self):runtime['suspended'].append(self.role)
        def inspect(self):return runtime.get('registration',{'running':0,'autostart':False})
        def start(self,command,autostart=False):
            assert not runtime['health']
            runtime['starts'].append((command,autostart))
            runtime['health']=dict(instance='new',version=services_version(),control_id='b'*32,cache_management=True)
    monkeypatch.setattr('cachemonitor.observer_task.ObserverTask',Task)
    manager.task=Task();manager.legacy_task=Task(role='legacy')
    monkeypatch.setattr(manager,'cleanup_legacy_check',lambda:None)
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


def test_app_restarts_only_dead_processes_with_a_session_budget(running,monkeypatch):
    manager,runtime=running
    clock=[0];starts=[]
    owner=services.AppServices(manager,[],collection=False,clock=lambda:clock[0])
    owner.active=True
    owner.poll()
    def restart():
        starts.append(clock[0])
        runtime['health']=dict(instance='restart-'+str(len(starts)),version=services_version(),control_id='c'*32,cache_management=True)
        return manager.status()
    monkeypatch.setattr(manager,'resume',restart)
    for start in (0,60,120):
        runtime['health']=None;clock[0]=start;owner.poll()
        clock[0]=start+59;owner.poll()
        assert len(starts)==start//60
        clock[0]=start+60;owner.poll()
        owner.poll()  # Success must not replenish the retry allowance.
    assert starts==[60,120,180]
    runtime['health']=None
    for at in (181,300,1000):clock[0]=at;owner.poll()
    assert len(starts)==3
    owner.deactivate();clock[0]=2000
    assert owner.poll()=={} and len(starts)==3


@pytest.mark.parametrize('condition',['unknown','identity_mismatch','running','port','lock','off','update'])
def test_app_does_not_replace_an_unconfirmed_or_disabled_service(running,monkeypatch,condition):
    manager,runtime=running
    runtime['health']=None
    owner=services.AppServices(manager,[],collection=False)
    owner.active=True
    monkeypatch.setattr(manager,'resume',lambda:pytest.fail('unexpected restart'))
    if condition in ('unknown','identity_mismatch'):
        monkeypatch.setattr(manager,'health',lambda **_:setattr(manager,'health_state',condition))
    if condition=='running':runtime['registration']={'running':1}
    if condition=='port':monkeypatch.setattr(services.identity,'port_free',lambda _:False)
    if condition=='lock':monkeypatch.setattr(services.identity,'locks_free',lambda _:False)
    if condition=='off':manager.set_url(None)
    if condition=='update':services.atomic_write(manager.directory/'proxy-update.json',b'{"phase":"switching"}')
    for at in (0,60,120,1000):owner.clock=lambda:at;owner.poll()


def test_legacy_check_cleanup_matches_the_full_connection(tmp_path,monkeypatch):
    import subprocess
    from cachemonitor.observer_control import ObserverManager
    manager=ObserverManager(tmp_path/'home',tmp_path/'data',url='http://127.0.0.1:18972')
    command=['--check','--codex-home',str(manager.home),'--data-dir',str(manager.directory),'--proxy-url',manager.url]
    calls=[]
    task=SimpleNamespace(inspect=lambda:dict(registered=True,arguments=subprocess.list2cmdline(command)),
        suspend=lambda:calls.append('suspend'),stop=lambda:calls.append('stop'),remove=lambda:calls.append('remove'))
    monkeypatch.setattr('cachemonitor.observer_control.ObserverTask',lambda *a,**k:task)
    command[-1]='http://127.0.0.1:18973'
    manager.cleanup_legacy_check();assert calls==[]
    command[-1]=manager.url
    manager.cleanup_legacy_check();assert calls==['suspend','stop','remove']


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
    assert {'worker','legacy','ProxyUpdate'}.issubset(runtime['suspended'])
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


@pytest.mark.parametrize('role',['--cache-worker','--cache-observe-only'])
def test_gui_launch_restarts_configured_worker_after_reboot(running,monkeypatch,role):
    manager,runtime=running
    command=ProxyTarget(manager).capture(runtime['health'])['command']
    command[command.index('--cache-observe-only')]=role
    runtime['health']=None
    runtime['registration']=dict(registered=True,executable=command[0],arguments='synthetic arguments',running=0)
    monkeypatch.setattr('cachemonitor.launch_context.command_arguments',lambda _:['worker',*command[1:]])
    before=manager.config_path.read_bytes()
    assert manager.ensure()['phase']=='recovery_required' and not runtime['starts']
    assert manager.resume()['phase']=='active'
    assert runtime['starts']==[(command,False)]
    assert manager.config_path.read_bytes()==before


def test_reboot_after_update_uses_new_app_without_stale_drain_controls(running,monkeypatch):
    manager,runtime=running
    command=ProxyTarget(manager).capture(runtime['health'])['command']
    legacy=[*command[1:],'--control-file',str(manager.directory/'old-control.json'),'--control-id','a'*32]
    runtime['health']=None
    runtime['registration']=dict(registered=True,executable=str(manager.directory/'removed-old'/'Codexon.exe'),arguments='',running=0)
    monkeypatch.setattr('cachemonitor.launch_context.command_arguments',lambda _:['worker',*legacy])
    assert manager.resume()['phase']=='active'
    assert runtime['starts']==[(command,False)]


@pytest.mark.parametrize('route,state',[('direct','refused'),('custom','refused'),('owned','unknown')])
def test_gui_launch_does_not_enable_an_off_or_unconfirmed_connection(running,monkeypatch,route,state):
    manager,runtime=running
    runtime['health']=None
    if route!='owned':manager.set_url(None if route=='direct' else 'https://example.invalid/v1')
    def health(**_):manager.health_state=state;return None
    monkeypatch.setattr(manager,'health',health)
    manager.resume()
    assert not runtime['starts']


@pytest.mark.parametrize('blocked',['port','lock','wrong-home','running'])
def test_reboot_recovery_preserves_an_unconfirmed_worker(running,monkeypatch,blocked):
    manager,runtime=running
    command=ProxyTarget(manager).capture(runtime['health'])['command']
    if blocked=='wrong-home':command[command.index('--codex-home')+1]=str(manager.home/'other')
    runtime['health']=None
    runtime['registration']=dict(registered=True,executable=command[0],arguments='',running=int(blocked=='running'))
    monkeypatch.setattr('cachemonitor.launch_context.command_arguments',lambda _:['worker',*command[1:]])
    monkeypatch.setattr(services.identity,'port_free',lambda _:blocked!='port')
    monkeypatch.setattr(services.identity,'locks_free',lambda _:blocked!='lock')
    if blocked=='running':manager.resume()
    else:
        with pytest.raises(RuntimeError):manager.resume()
    assert not runtime['starts']


def test_collection_client_never_starts_or_resumes_service(tmp_path,monkeypatch):
    from cachemonitor.usage_collection import CollectionClient,CollectorService
    home=tmp_path/'home';path=tmp_path/'index.sqlite'
    collector=CollectorService([home],path);client=CollectionClient([home],path)
    monkeypatch.setattr('cachemonitor.observer_task.ObserverTask.start',lambda *a,**k:pytest.fail('consumer started service'))
    try:
        services.atomic_write(client.channel.companion('.session.json'),json.dumps(
            dict(scope=client.channel.scope,stopped=True)).encode())
        assert collector.stopping()
        for at in (100,160,1000):client.poll(at)
        assert collector.stopping()
        assert not hasattr(client,'ensure_service')
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


def test_quit_waits_responsively_and_handoff_does_not_stop_services(tmp_path,monkeypatch):
    import threading
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration',True)
    release=threading.Event();entered=threading.Event();finished=[]
    def stop(self,**kwargs):entered.set();assert release.wait(10)
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


def test_exit_inventory_uses_exact_home_and_session(tmp_path):
    from cachemonitor.app_shutdown import ExitConnectionCheck,exit_message
    manager=SimpleNamespace(home=tmp_path/'home',health=lambda **kw:dict(active_connections=4,
        supports_force_shutdown=True,connection_sessions=[dict(session_id='a',connections=2),
        dict(session_id='b',connections=1),dict(session_id='',connections=1)]))
    check=ExitConnectionCheck(manager,None);check.run()
    text=exit_message(check,dict(sessions=[dict(home=str(manager.home),id='a',title='My chat'),
        dict(home=str(tmp_path/'other'),id='b',title='Wrong home')]))
    assert check.count==4 and 'My chat' in text and 'Wrong home' not in text
    assert 'b · 연결 1개' in text and '세션 확인 불가' in text
    manager.health=lambda **kw:None;manager.health_state='unknown'
    unknown=ExitConnectionCheck(manager,None);unknown.run();assert unknown.count is None
    manager.health_state='refused'
    empty=ExitConnectionCheck(manager,None);empty.run();assert empty.count==0


@pytest.mark.parametrize('initial',[True,False])
def test_force_exit_can_escalate_unknown_connections(running,initial):
    manager,runtime=running
    runtime['health'].update(supports_force_shutdown=True,websocket_states={'unknown':1})
    requested=[initial];commands=[]
    def health(**kw):
        control=read_json(manager.directory/('proxy-control-'+'a'*32+'.json'))
        commands.append(control.get('action'))
        if control.get('action')=='force_shutdown':runtime['health']=None
        manager.health_state='healthy' if runtime['health'] else 'refused'
        return runtime['health']
    manager.health=health
    lifecycle=services.AppServices(manager,[],collection=False,sleep=lambda _:requested.__setitem__(0,True))
    lifecycle.stop(force_requested=lambda:requested[0])
    assert 'force_shutdown' in commands and runtime['health'] is None
    assert read_json(services.session_path(manager))['phase']=='stopped'


def test_exit_dialog_buttons_and_escape(tmp_path):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from cachemonitor.app_shutdown import ExitConfirmation,ShutdownProgress
    app=QApplication.instance() or QApplication([])
    check=SimpleNamespace(count=1,manager=SimpleNamespace(home=tmp_path),
        health=dict(supports_force_shutdown=True,connection_sessions=[dict(session_id='session',connections=1)]))
    for choice in ('cancel','confirm','force','escape'):
        dialog=ExitConfirmation(check,{},None);results=[];dialog.finished.connect(results.append);dialog.open()
        app.processEvents();QTest.qWait(30)
        assert dialog.host.quick.status().name=='Ready' and not dialog.host.qml_errors
        if choice=='escape':QTest.keyClick(dialog.host,Qt.Key_Escape)
        else:
            from cachemonitor.quick_qa import click,control
            click(dialog.host,control(dialog.host,getattr(dialog,choice)))
        for _ in range(5):app.processEvents();QTest.qWait(5)
        assert results==[dict(cancel=0,confirm=1,force=2,escape=0)[choice]]
    progress=ShutdownProgress(None,True);progress.show();app.processEvents()
    QTest.keyClick(progress,Qt.Key_Escape);assert progress.isVisible()
    progress.force.click();assert not progress.force.isEnabled()
    progress.accept();progress.deleteLater();app.processEvents()


def test_force_rejects_unsupported_worker(running):
    manager,runtime=running
    runtime['work']=100
    with pytest.raises(RuntimeError,match='업데이트'):
        services.AppServices(manager,[],collection=False).stop(force_requested=lambda:True)
    assert runtime['health'] is not None
