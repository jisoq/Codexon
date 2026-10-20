import json
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('held',[False,True,'exited','unreadable'])
def test_collector_stale_pid_does_not_block_restart(tmp_path,monkeypatch,held):
    from cachemonitor.usage_collection import CollectionChannel
    owner=services.AppServices(None,[])
    channel=CollectionChannel([],tmp_path/'index.sqlite')
    queries=[];probes=[]
    def locked(path):
        probes.append(path)
        if held=='unreadable':raise PermissionError('lock access denied')
        return held is True or held=='exited' and len(probes)==1
    def identify(pid):
        queries.append(pid)
        raise PermissionError('process access denied')
    monkeypatch.setattr('cachemonitor.usage_collection.locked',locked)
    monkeypatch.setattr(services.identity,'process_identity',identify)
    try:
        if held is True or held=='unreadable':
            with pytest.raises(PermissionError):owner.collector_identity(channel,{'collection':{'pid':123}})
        else:assert owner.collector_identity(channel,{'collection':{'pid':123}}) is None
        assert bool(queries)==(held is True or held=='exited')
    finally:channel.close()


def test_collector_restart_requires_new_publication_and_preserves_retry_budget(tmp_path,monkeypatch):
    from cachemonitor.usage_collection import CollectionChannel
    from cachemonitor.observer_task import ObserverTask
    channel=CollectionChannel([],tmp_path/'index.sqlite')
    snapshot={'ts':1000,'collection':{'pid':123,'instance':'old','version':'2026.09.28.4'}}
    clock=[0];starts=[]
    monkeypatch.setattr('cachemonitor.usage_collection.CollectionChannel',lambda *a:channel)
    monkeypatch.setattr(channel,'close',lambda:None)
    monkeypatch.setattr(channel,'read_header',lambda:snapshot)
    monkeypatch.setattr('cachemonitor.collection_lifecycle.retire_legacy',lambda c:None)
    monkeypatch.setattr('cachemonitor.usage_collection.locked',lambda p:False)
    monkeypatch.setattr(ObserverTask,'inspect',lambda self:{'running':False})
    monkeypatch.setattr(ObserverTask,'start',lambda *a,**k:starts.append(clock[0]))
    monkeypatch.setattr(services.identity,'process_identity',lambda pid:pytest.fail('stale PID queried'))
    monkeypatch.setattr(services.time,'time',lambda:1000)
    owner=services.AppServices(None,[],channel.path,clock=lambda:clock[0])
    try:
        assert owner.start()['collection_issue']=='수집 시작 중'
        assert starts==[0]
        assert owner.poll_collection()=='수집 시작 중'
        for at in (30,90,120,150,210,500):
            clock[0]=at;owner.poll_collection()
        assert starts==[0,90,150,210]
        snapshot['collection']['instance']='new'
        assert owner.poll_collection()=='수집 결과 갱신 지연'
        assert owner.collector_pending is not None
        monkeypatch.setattr(owner,'collector_identity',lambda *a:{'instance':'new'})
        assert owner.poll_collection()==''
        assert owner.collector_pending is None
        assert owner.retries['collection']['count']==3
    finally:channel.db.close()

from cachemonitor import app_services as services
from cachemonitor.observer_control import ObserverManager
from cachemonitor.observer_state import read_json
from cachemonitor.proxy_target import ProxyTarget


def test_collector_startup_does_not_hide_new_failure_marker(tmp_path,monkeypatch):
    from cachemonitor.usage_collection import CollectionChannel,publish_collector_fault
    channel=CollectionChannel([],tmp_path/'index.sqlite')
    owner=services.AppServices(None,[],channel.path,clock=lambda:5)
    owner.collector_pending=('old',0)
    monkeypatch.setattr('cachemonitor.usage_collection.CollectionChannel',lambda *args:channel)
    monkeypatch.setattr(channel,'close',lambda:None)
    monkeypatch.setattr(channel,'read_header',lambda:{'ts':1,'collection':{'instance':'old'}})
    publish_collector_fault(channel.companion('.collector-error.json'),channel.scope,'new',OSError('failed'))
    try:
        owner.poll_collection()
        assert owner.collection_fault.get('runtime_failed')
        assert not owner.collection_healthy
    finally:channel.db.close()


@pytest.fixture
def login_services(tmp_path, monkeypatch):
    from cachemonitor.model_evidence import home_key
    from cachemonitor import retired_cache, install_cleanup
    manager = ObserverManager(tmp_path/'home', tmp_path/'data', url='http://127.0.0.1:18989')
    manager.set_url(manager.url)
    manager.write_state(dict(home=home_key(manager.home), enabled=True, pending=False, phase='active'))
    events = []
    health = dict(instance='login', status='ok', version=services_version(), observation_enabled=True)
    status = dict(phase='active', configured=True, health=health)
    monkeypatch.setattr(retired_cache, 'retire', lambda _: None)
    monkeypatch.setattr(install_cleanup, 'after_services', lambda _: events.append('cleanup'))
    monkeypatch.setattr(manager, 'cleanup_legacy_check', lambda: None)
    monkeypatch.setattr(manager, 'adopt_registrations', lambda: None)
    monkeypatch.setattr(ProxyTarget, 'capture', lambda self, value: {})
    def resume():
        events.append('route')
        return status
    monkeypatch.setattr(manager, 'resume', resume)
    owner = services.AppServices(manager, [], login_launch=lambda: events.append('launch') or 'started')
    monkeypatch.setattr(owner, 'start_collection', lambda: events.append('collection'))
    return owner, manager, status, events


def test_login_launch_runs_after_route_before_collection_and_only_once(login_services):
    owner, manager, status, events = login_services
    assert owner.execute('start')['chatgpt_startup'] == 'started'
    assert events == ['route', 'launch', 'collection', 'cleanup']
    owner.execute('start')
    assert events.count('launch') == 1
    assert events[-3:] == ['collection', 'route', 'cleanup']


def test_login_without_proxy_does_not_wait_for_service_probes(login_services):
    owner, manager, status, events = login_services
    manager.set_url(None)
    manager.write_state({**manager.state(), 'enabled':False, 'phase':'off'})
    assert owner.execute('start')['chatgpt_startup'] == 'started'
    assert events == ['launch', 'collection', 'route', 'cleanup']


def test_login_waits_for_suspended_route_to_be_restored(login_services, monkeypatch):
    owner, manager, status, events = login_services
    manager.set_url(None)
    manager.write_state({**manager.state(), 'enabled':False})
    journal = dict(scope=owner.target.scope, phase='stopped', resume=True)
    services.atomic_write(services.session_path(manager), json.dumps(journal).encode())
    def resume():
        assert not events
        manager.set_url(manager.url)
        manager.write_state({**manager.state(), 'enabled':True})
        services.atomic_write(services.session_path(manager), json.dumps({**journal, 'phase':'active'}).encode())
        events.append('route')
        return status
    monkeypatch.setattr(manager, 'resume', resume)
    owner.execute('start')
    assert events[:3] == ['route', 'launch', 'collection']


@pytest.mark.parametrize('issue', ['missing', 'draining', 'observation_off', 'wrong_route', 'update'])
def test_login_never_launches_before_required_connection_is_ready(login_services, issue):
    owner, manager, status, events = login_services
    if issue == 'missing': status['health'] = None
    elif issue == 'draining': status['health']['draining'] = True
    elif issue == 'observation_off': status['health']['observation_enabled'] = False
    elif issue == 'wrong_route': manager.set_url(None)
    elif issue == 'update': status['update'] = {'phase':'switching'}
    result = owner.execute('start')
    assert result['chatgpt_startup_error']
    assert events == ['route', 'collection']
    assert owner.login_launch is None


def test_login_failure_preserves_collection_and_does_not_retry_activation(login_services, monkeypatch):
    owner, manager, status, events = login_services
    def fail():
        events.append('launch')
        raise OSError('missing app')
    owner.login_launch = fail
    assert owner.execute('start')['chatgpt_startup_error'] == 'missing app'
    owner.execute('start')
    assert events.count('launch') == 1
    assert events.count('collection') == 2


def test_login_service_failure_or_shutdown_never_opens_app(login_services, monkeypatch):
    owner, manager, status, events = login_services
    def fail(): raise RuntimeError('failed readiness')
    monkeypatch.setattr(manager, 'resume', fail)
    result = owner.execute('start')
    assert result['chatgpt_startup_error'] == 'failed readiness'
    assert events == ['collection']
    owner.login_launch = lambda: events.append('unexpected launch')
    owner.deactivate()
    assert owner.execute('start') == {}
    assert events == ['collection']


def test_only_enabled_login_launch_uses_saved_app_identifier(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from cachemonitor import app, chatgpt_startup
    settings = QSettings(str(tmp_path/'startup.ini'), QSettings.IniFormat)
    assert app.login_launcher(settings, windows_startup=True) is None
    settings.setValue(chatgpt_startup.ENABLED_KEY, True)
    settings.setValue(chatgpt_startup.APP_ID_KEY, 'registered-app')
    calls = []
    monkeypatch.setattr(chatgpt_startup, 'launch_chatgpt', lambda app_id: calls.append(app_id))
    assert app.login_launcher(settings) is None
    assert app.login_launcher(settings, windows_startup=True, isolated=True) is None
    launch = app.login_launcher(settings, windows_startup=True)
    settings.setValue(chatgpt_startup.APP_ID_KEY, 'changed-later')
    launch()
    assert calls == ['registered-app']


def test_login_cancelled_during_connection_recovery_keeps_collection(login_services, monkeypatch):
    owner, manager, status, events = login_services
    launch = services.LoginLaunch(lambda: events.append('launch'))
    owner.login_launch = launch
    def resume():
        events.append('route')
        launch.cancel()
        return status
    monkeypatch.setattr(manager, 'resume', resume)
    result = owner.execute('start')
    assert result['chatgpt_startup'] == 'cancelled'
    assert events == ['route', 'collection', 'cleanup']
    assert not result.get('chatgpt_startup_error')


@pytest.mark.integration
@pytest.mark.parametrize('already_started', [False, True])
def test_login_message_runs_once_without_losing_initial_service_start(login_services, tmp_path, monkeypatch, already_started):
    import threading
    import time
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtTest import QTest
    from cachemonitor import app, chatgpt_startup
    from cachemonitor.app_shutdown import ServiceController
    qt = QApplication.instance() or QApplication([])
    owner, manager, status, events = login_services
    owner.login_launch = None
    owner.active = already_started
    entered, release = threading.Event(), threading.Event()
    def poll():
        entered.set()
        assert release.wait(5)
        events.append('poll')
        return status
    monkeypatch.setattr(owner, 'poll', poll)
    monkeypatch.setattr(chatgpt_startup, 'launch_chatgpt', lambda _: events.append('launch') or 'started')
    window = QWidget()
    window.settings = QSettings(str(tmp_path/'queued-login.ini'), QSettings.IniFormat)
    window.settings.setValue(chatgpt_startup.ENABLED_KEY, True)
    window.settings.setValue(chatgpt_startup.APP_ID_KEY, 'registered-app')
    for name in ('startup', 'chatgpt_startup'):
        action = QAction(window);action.setCheckable(True);action.setChecked(True)
        setattr(window, name, action)
    controller = ServiceController(owner, window)
    window.service_controller = controller
    results = []
    controller.result.connect(results.append)
    def wait_until(predicate):
        deadline = time.monotonic()+5
        while not predicate():
            assert time.monotonic()<deadline, 'Login service operation did not complete'
            QTest.qWait(10)
    try:
        if already_started:
            controller.invoke('ensure')
            wait_until(entered.is_set)
        app.request_login_startup(window)
        app.request_login_startup(window)
        if already_started:
            assert 'launch' not in events
            release.set()
        else:
            # The login IPC can arrive before the dashboard's 0ms startup timer.
            controller.start()
        wait_until(lambda: any(result.get('chatgpt_startup')=='started' for result in results) and controller.operation is None)
        assert owner.active
        assert events.count('launch') == 1
        assert events.count('collection') == (0 if already_started else 1)
        if already_started:assert events.index('poll')<events.index('launch')
        app.request_login_startup(window)
        assert controller.operation is None
    finally:
        release.set();controller.stop()
        if controller.operation:
            assert controller.operation.wait(5000)
        window.deleteLater();qt.processEvents()


@pytest.mark.parametrize('name',['custom index.collection.sqlite','custom index.sqlite.codexon-collection.sqlite'])
@pytest.mark.parametrize('schema',[1,2])
def test_package_collector_reader_restores_both_released_ipc_formats(tmp_path,monkeypatch,name,schema):
    import sqlite3,zlib
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'tools'))
    from verify_app_services import read_collection_snapshot
    path=tmp_path/name
    encode=lambda value:zlib.compress(json.dumps(value).encode())
    sessions=[dict(home='fixture',id='one',history=[dict(key='call-one',input=100,output=5)],remaining=7),
              dict(home='fixture',id='two',history=[dict(key='call-two',input=200,output=9)],remaining=3)]
    activity=[dict(home='fixture',attempt='first',status='completed'),
              dict(home='fixture',attempt='second',status='created')]
    expected=dict(collection={'version':'released'},sessions=sessions,request_activity=activity)
    header=dict(expected)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE snapshot(id INTEGER PRIMARY KEY,payload BLOB)')
        if schema==2:
            header.pop('sessions');header.pop('request_activity')
            header.update(collection_schema=2,activity_revision=4,
                session_manifest=[dict(home=s['home'],sid=s['id'],revision=4,state={'remaining':s['remaining']}) for s in sessions])
            db.execute('CREATE TABLE sessions(home TEXT,sid TEXT,revision INTEGER,payload BLOB)')
            db.executemany('INSERT INTO sessions VALUES(?,?,?,?)',
                [(s['home'],s['id'],4,encode({**s,'remaining':99})) for s in reversed(sessions)])
            db.execute('CREATE TABLE activity(position INTEGER,payload BLOB)')
            db.executemany('INSERT INTO activity VALUES(?,?)',[(i,encode(value)) for i,value in reversed(list(enumerate(activity)))])
            expected['collection_schema']=2
        db.execute('INSERT INTO snapshot VALUES(1,?)',(encode(header),))
    original=path.read_bytes()
    restored=read_collection_snapshot(path)
    assert restored==expected
    assert sum(len(session['history']) for session in restored['sessions'])==2
    assert path.read_bytes()==original


@pytest.mark.parametrize('stored_revision',[None,3])
def test_package_collector_reader_rejects_missing_or_mismatched_session_revision(tmp_path,monkeypatch,stored_revision):
    import sqlite3,zlib
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'tools'))
    from verify_app_services import read_collection_snapshot
    path=tmp_path/'collection.sqlite'
    encode=lambda value:zlib.compress(json.dumps(value).encode())
    header=dict(collection_schema=2,session_manifest=[dict(home='fixture',sid='session',revision=4,state={})])
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE snapshot(id INTEGER PRIMARY KEY,payload BLOB)')
        db.execute('CREATE TABLE sessions(home TEXT,sid TEXT,revision INTEGER,payload BLOB)')
        db.execute('INSERT INTO snapshot VALUES(1,?)',(encode(header),))
        if stored_revision is not None:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',('fixture','session',stored_revision,encode({'history':[]})))
    with pytest.raises(ValueError,match='session revision'):
        read_collection_snapshot(path)


@pytest.fixture
def running(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir()
    manager=ObserverManager(home,tmp_path/'data');manager.index=tmp_path/'data'/'index.sqlite'
    manager.set_url(manager.url)
    from cachemonitor.model_evidence import home_key
    manager.write_state(dict(home=home_key(home),enabled=True,phase='active',url=manager.url,previous_url=None))
    command=[sys.executable,str(tmp_path/'run.py'),'--model-proxy','--managed',
             '--codex-home',str(home),'--evidence-path',str(manager.evidence),
             '--port',manager.url.rsplit(':',1)[1]]
    runtime={'health':dict(instance='old',version='old',control_id='a'*32,lifecycle='managed'), 'work':2,'starts':[],'suspended':[]}
    class Task:
        def __init__(self,*a,role='worker',**k):self.role=role
        def suspend(self):runtime['suspended'].append(self.role)
        def configure(self,command,autostart=False):runtime['registration']={'registered':True,'executable':command[0],'arguments':'','running':0}
        def inspect(self):return runtime.get('registration',{'running':0,'autostart':False})
        def start(self,command,autostart=False):
            assert not runtime['health']
            runtime['starts'].append((command,autostart))
            runtime['health']=dict(instance='new',version=services_version(),control_id='b'*32,lifecycle='managed')
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
        runtime['health']=dict(instance='restart-'+str(len(starts)),version=services_version(),control_id='c'*32,lifecycle='managed')
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


@pytest.mark.parametrize('resume', [False, True])
def test_restart_recovers_exited_proxy_without_claiming_storage_was_flushed(running, monkeypatch, resume):
    manager, runtime = running
    owner = services.AppServices(manager, [], collection=False)
    source = owner.target.capture(runtime['health'])
    owner.save(phase='closing', resume=resume, state=manager.state(), source=source, direct_url=None)
    runtime['health'] = None
    manager.set_url(None)
    monkeypatch.setattr(ProxyTarget, 'stopped', lambda self, source: False)
    monkeypatch.setattr('cachemonitor.proxy_drain.ProxyDrain.run',
                        lambda *args: pytest.fail('An exited process cannot write a receipt'))
    services.resume_proxy(manager)
    journal = read_json(services.session_path(manager))
    assert journal['phase'] == 'active'
    assert journal['shutdown_incomplete'] is True
    assert bool(runtime['starts']) is resume
    assert not (manager.directory / ('proxy-control-' + source['control_id'] + '.json')).exists()
    services.require_running(manager)


@pytest.mark.parametrize('held', ['process', 'port', 'lock'])
def test_missing_flush_receipt_cannot_bypass_live_proxy_ownership(running, monkeypatch, held):
    manager, runtime = running
    owner = services.AppServices(manager, [], collection=False)
    source = owner.target.capture(runtime['health'])
    owner.save(phase='closing', resume=True, state=manager.state(), source=source, direct_url=None)
    if held != 'process':
        runtime['health'] = None
    if held == 'port':
        monkeypatch.setattr(services.identity, 'port_free', lambda _: False)
    if held == 'lock':
        monkeypatch.setattr(services.identity, 'locks_free',
                            lambda paths: manager.directory/'proxy-supervisor.lock' not in paths)
    monkeypatch.setattr(ProxyTarget, 'stopped', lambda self, source: False)
    def blocked(*args):
        raise RuntimeError('still owned')
    monkeypatch.setattr('cachemonitor.proxy_drain.ProxyDrain.run', blocked)
    with pytest.raises(RuntimeError, match='still owned'):
        services.resume_proxy(manager)
    assert not runtime['starts']
    assert not read_json(services.session_path(manager)).get('shutdown_incomplete')


def test_gui_start_during_update_does_not_register_the_old_worker(running,monkeypatch):
    manager,runtime=running
    services.atomic_write(manager.directory/'proxy-update.json',b'{"phase":"starting"}')
    monkeypatch.setattr(manager,'status',lambda:{'update':'starting'})
    assert manager.resume()['update']=='starting'
    assert not runtime['starts']






@pytest.mark.parametrize('route,state',[('direct','refused'),('custom','refused'),('owned','unknown')])
def test_gui_launch_does_not_enable_an_off_or_unconfirmed_connection(running,monkeypatch,route,state):
    manager,runtime=running
    runtime['health']=None
    if route!='owned':manager.set_url(None if route=='direct' else 'https://example.invalid/v1')
    def health(**_):manager.health_state=state;return None
    monkeypatch.setattr(manager,'health',health)
    manager.resume()
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


def test_force_rejects_unsupported_worker(running):
    manager,runtime=running
    runtime['work']=100
    result=services.AppServices(manager,[],collection=False).stop(force_requested=lambda:True)
    assert result.status=='failed' and not result.committed and result.error
    assert runtime['health'] is not None
