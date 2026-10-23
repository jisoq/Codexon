import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tests.restart_fixture import no_running_desktop
from test_install_management import ready_installation,isolated_shell
from cachemonitor import app_services,client_restart,install_management,proxy_identity
from cachemonitor.observer_state import read_json
from cachemonitor.proxy_target import ProxyTarget
from cachemonitor.proxy_websocket import POLICY
from cachemonitor.version import PROXY_VERSION


def settle(predicate):
    deadline=time.monotonic()+5
    waiter=threading.Event()
    while not predicate():
        assert time.monotonic()<deadline,'Installed startup did not settle'
        waiter.wait(.01)


@pytest.fixture
def route_case(tmp_path,monkeypatch,ready_installation):
    case=ready_installation
    manager=case.manager
    route=[None];events=[];entered=threading.Event();release=threading.Event();gui_live=[True]
    executable=str(case.product/'Codexon.exe')
    gui=dict(pid=os.getpid(),created=10,executable=executable)
    proxy=dict(pid=701,created=20,executable=executable)
    command=[executable,'--model-proxy','--managed','--codex-home',str(manager.home),
             '--evidence-path',str(manager.evidence),'--port','19876','--upstream','chatgpt']
    manifest=read_json(case.product/'build-manifest.json')
    health=dict(pid=701,instance='updated-proxy',status='ok',version=PROXY_VERSION,
        role='observer',home=str(manager.home),evidence_path=str(manager.evidence),index_path=None,
        upstream='https://chatgpt.com/backend-api/codex',listen_url=manager.url,
        executable_sha256=manifest['sha256'],deployment_commit=manifest['commit'],
        process_created=proxy['created'],websocket_policy=vars(POLICY),lifecycle='managed')
    manager.control_lock=tmp_path/'manager.lock'
    manager.config=lambda:('',dict(openai_base_url=route[0]) if route[0] is not None else {})
    manager.state=lambda:dict(enabled=route[0]==manager.url,phase='active' if route[0]==manager.url else 'off')
    manager.status=lambda:dict(configured=route[0]==manager.url,health=health if route[0]==manager.url else None)
    manager.runtime=lambda:dict(pid=701,phase='active')
    from cachemonitor.observer_control import ObserverManager
    manager.local_ready=ObserverManager.local_ready
    manager.task=SimpleNamespace(inspect=lambda:{})
    manager.cleanup_legacy_check=lambda:None
    manager.adopt_registrations=lambda:None
    manager.update_proxy=lambda **kwargs:dict(update=dict(phase='complete'))
    def resume():
        entered.set()
        assert release.wait(5),'Proxy readiness barrier was not released'
        session=read_json(app_services.session_path(manager))
        if session.get('resume') and route[0]==session.get('direct_url'):
            route[0]=manager.url
        session['phase']='active'
        app_services.session_path(manager).write_text(json.dumps(session))
        events.append('route')
        return manager.status()
    manager.resume=resume
    monkeypatch.setattr(proxy_identity,'process_identity',lambda pid:gui if pid==gui['pid'] else proxy if pid==701 else None)
    monkeypatch.setattr(proxy_identity,'process_command',lambda pid:command)
    monkeypatch.setattr(proxy_identity,'listener_pids',lambda url:[701])
    monkeypatch.setattr(proxy_identity,'same_process',lambda value:value==proxy or value==gui and gui_live[0])
    monkeypatch.setattr('cachemonitor.retired_cache.retire',lambda manager:None)
    monkeypatch.setattr('cachemonitor.install_cleanup.after_services',lambda owner:None)
    monkeypatch.setattr(sys,'executable',executable)
    monkeypatch.setattr('cachemonitor.connection_recovery.target',lambda *args:manager)
    running=[]
    monkeypatch.setattr(client_restart.desktop,'desktop_processes',lambda:list(running))
    def activate(app_id):
        assert events==['gui','route']
        events.append('client')
        running.append(dict(pid=900,created=int((time.time()+.01+11644473600)*10_000_000)))
    monkeypatch.setattr(client_restart.desktop,'_activate',activate)
    case.journal.client_result(case.operation_id,phase='closed',closed_at=time.time(),reopen_required=True)
    session=read_json(app_services.session_path(manager));session.update(resume=True,direct_url=None)
    app_services.session_path(manager).write_text(json.dumps(session))
    return SimpleNamespace(case=case,manager=manager,route=route,events=events,entered=entered,
                           release=release,gui=gui,gui_live=gui_live)


def test_new_gui_and_verified_saved_route_precede_client_reopening(tmp_path,route_case):
    state=route_case;case=state.case
    receipt=install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    assert case.launches==[[str(case.product/'Codexon.exe'),'--replace-gui','--finish-install',case.operation_id]]
    assert receipt['connection']['phase']=='waiting'
    assert read_json(tmp_path/'installation.json')['launch_requested'] is True
    assert case.journal.read()['app']['startup']==dict(state='waiting',policy='requested')
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    case.journal.startup_result(case.operation_id,dict(state='starting',owner=state.gui))
    state.events.append('gui')
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    result=[]
    startup=threading.Thread(target=lambda:result.append(owner.execute('start')))
    restart=threading.Thread(target=client_restart.run_restart,args=(case.journal,case.operation_id))
    startup.start();restart.start()
    try:
        assert state.entered.wait(5)
        assert state.events==['gui']
        assert case.journal.read()['client']['phase']=='closed'
        state.release.set()
        startup.join(5);restart.join(5)
        assert not startup.is_alive() and not restart.is_alive()
        assert not result[0].get('error'),result
        assert state.events==['gui','route','client']
        assert case.journal.read()['phase']=='complete'
        assert case.journal.read()['app']['startup']['state']=='ready'
    finally:
        state.release.set();case.journal.fail(case.operation_id,'Test cleanup')
        startup.join(5);restart.join(5)


@pytest.mark.parametrize('resume,external,reason',[(False,None,'off'),(True,'https://external.example/v1','external_edit')])
def test_route_readiness_preserves_explicit_off_and_external_edit(route_case,resume,external,reason):
    state=route_case;path=app_services.session_path(state.manager)
    session=read_json(path);session['resume']=resume;path.write_text(json.dumps(session))
    state.route[0]=external
    readiness=app_services.route_readiness(state.manager,dict(scope=ProxyTarget(state.manager).scope))
    assert readiness==dict(phase='off',route=dict(kind='direct',url=external,reason=reason))
    assert state.route[0]==external and read_json(path)==session


def test_dead_gui_cannot_authorize_reopen_after_readiness(tmp_path,route_case):
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    case.journal.startup_result(case.operation_id,dict(state='ready',owner=state.gui))
    case.journal.proxy_result(case.operation_id,dict(phase='off'))
    state.gui_live[0]=False
    worker=threading.Thread(target=client_restart.run_restart,args=(case.journal,case.operation_id))
    worker.start();worker.join(5)
    assert not worker.is_alive()
    assert state.events==[]
    assert case.journal.read()['client']['phase']=='blocked'


def test_stale_operation_and_wrong_gui_build_cannot_bind(tmp_path,route_case):
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    with pytest.raises(RuntimeError):case.journal.bind_installed('f'*32,executable=sys.executable)
    with pytest.raises(RuntimeError):case.journal.bind_installed(case.operation_id,executable=tmp_path/'other.exe')
    assert not case.journal.startup_result('f'*32,dict(state='ready',owner=state.gui))
    (case.product/'Codexon.exe').write_bytes(b'changed build')
    with pytest.raises(RuntimeError):case.journal.bind_installed(case.operation_id,executable=sys.executable)


def test_retry_recovers_a_dead_gui_before_reopening_the_closed_client(tmp_path,route_case,monkeypatch):
    from cachemonitor import app_update,observer_task
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    case.journal.startup_result(case.operation_id,dict(state='blocked',owner=state.gui))
    case.journal.client_result(case.operation_id,phase='blocked')
    state.gui_live[0]=False
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:dict(running=False))
    monkeypatch.setattr(client_restart,'recovery_path',lambda:case.recovery)
    workers=[]
    def start(journal,operation_id,recovery):
        if any(worker.is_alive() for worker in workers):return
        worker=threading.Thread(target=client_restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    monkeypatch.setattr(client_restart,'start',start)
    app_update.retry_proxy(state.manager,case.journal)
    assert len(case.launches)==2 and not state.events
    assert case.journal.read()['app']['startup']['state']=='waiting'
    state.gui_live[0]=True
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    case.journal.startup_result(case.operation_id,dict(state='starting',owner=state.gui))
    state.events.append('gui');state.release.set()
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    try:
        result=owner.execute('start')
        assert not result.get('error'),result
        settle(lambda:case.journal.read()['client']['phase']=='restarted')
        assert state.events==['gui','route','client']
    finally:
        case.journal.fail(case.operation_id,'Test cleanup')
        for worker in workers:worker.join(5)


def test_failed_initial_resume_retries_through_same_gui_owner(tmp_path,route_case):
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    case.journal.startup_result(case.operation_id,dict(state='starting',owner=state.gui))
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    resume=state.manager.resume
    def fail():raise RuntimeError('resume failed')
    state.manager.resume=fail
    assert owner.execute('start').get('error')
    assert case.journal.read()['app']['startup']['state']=='blocked'
    state.manager.resume=resume
    state.events.append('gui');state.release.set()
    assert not owner.execute('ensure').get('error')
    assert case.journal.read()['app']['startup']['state']=='ready'
    client_restart.run_restart(case.journal,case.operation_id)
    assert state.events==['gui','route','client']


def test_installed_reconciliation_dispatches_the_gui_without_resuming_route(tmp_path,route_case,monkeypatch):
    from cachemonitor import app_update,observer_task
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    record=case.journal.read();record['updated_at']=0
    case.journal.path.write_text(json.dumps(record))
    state.gui_live[0]=False
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:dict(running=False,registered=False))
    recovered=app_update.reconcile_update(case.journal,state.manager)
    assert len(case.launches)==2
    assert case.launches[-1][-2:]==['--finish-install',case.operation_id]
    assert not state.events and state.route[0] is None
    assert recovered['app']['startup']['state']=='waiting'


def test_proxy_rollback_accepts_verified_previous_version_but_not_pending_resume(route_case):
    state=route_case;state.release.set()
    state.manager.resume()
    state.manager.status()['health']['version']='2026.10.14.1'
    record=dict(scope=ProxyTarget(state.manager).scope,proxy=dict(restored=True))
    assert app_services.route_readiness(state.manager,record)['phase']=='complete'
    session=read_json(app_services.session_path(state.manager))
    session.update(phase='stopped',resume=True)
    app_services.session_path(state.manager).write_text(json.dumps(session))
    state.route[0]=None
    assert app_services.route_readiness(state.manager,record)['phase']=='pending'

@pytest.fixture
def asynchronous_proxy(route_case,monkeypatch):
    from cachemonitor import observer_control,observer_task,proxy_update
    state=route_case;manager=state.manager;case=state.case
    status=manager.status
    scheduled=[]
    manager.status=lambda:{**status(),'update':read_json(manager.directory/'proxy-update.json')}
    manager.health=lambda **kwargs:status()['health']
    manager.command=lambda upstream:[sys.executable,'--model-proxy','--upstream',upstream]
    manager.upstream=lambda:'chatgpt'
    manager.update_proxy=lambda **kwargs:observer_control.ObserverManager.update_proxy(manager,**kwargs)
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(proxy_update,'VERSION','test')
    monkeypatch.setattr(observer_task.ObserverTask,'start',lambda self,command,**kwargs:scheduled.append(command))
    updater=proxy_update.ProxyUpdate(manager)
    app=read_json(case.product/'build-manifest.json')
    distribution=dict(executable=str(case.product/'Codexon.exe'),sha256=app['sha256'],commit=app['commit'])
    return SimpleNamespace(state=state,updater=updater,scheduled=scheduled,distribution=distribution)


@pytest.mark.parametrize('old_proxy,terminal,restored',[(True,'complete',False),(False,'failed',False),(True,'failed',True)])
def test_proxy_dispatch_stays_pending_until_matching_terminal_result(tmp_path,asynchronous_proxy,monkeypatch,old_proxy,terminal,restored):
    fixture=asynchronous_proxy;state=fixture.state;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    fixture.scheduled.clear()
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    state.events.append('gui');state.release.set()
    health=state.manager.status
    state.route[0]=state.manager.url
    original=dict(health()['health'])
    if old_proxy:
        health()['health'].update(version='2026.10.14.1',executable_sha256='0'*64)
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    restart=None
    try:
        started=owner.execute('start')
        assert not started.get('error'),started
        assert case.journal.read()['app']['startup']['state']=='starting'
        assert case.journal.read()['proxy']['phase']=='queued'
        restart=threading.Thread(target=client_restart.run_restart,args=(case.journal,case.operation_id))
        restart.start()
        fixture.updater.publish('verifying',distribution=fixture.distribution)
        assert not owner.execute('ensure').get('error')
        assert len(fixture.scheduled)==1
        assert state.events==['gui','route']
        assert case.journal.read()['client']['phase']=='closed'
        if terminal=='complete':health()['health'].update(original)
        observed=threading.Event()
        read=case.journal.read
        def read_terminal():
            value=read()
            if threading.current_thread() is restart and value['proxy'].get('phase')==terminal:
                observed.set()
            return value
        monkeypatch.setattr(case.journal,'read',read_terminal)
        fixture.updater.publish(terminal,distribution=fixture.distribution,restored=restored,message='Terminal verification result')
        assert observed.wait(5),'Restart worker did not observe the terminal result'
        result=owner.execute('ensure')
        restart.join(5)
        assert not restart.is_alive()
        record=case.journal.read()
        if terminal=='complete' or restored:
            assert not result.get('error'),result
            assert record['client']['phase']=='restarted'
            assert state.events==['gui','route','client']
        else:
            assert result.get('error')
            assert record['app']['startup']['state']=='blocked'
            assert record['client']['phase']=='blocked'
            owner.execute('ensure')
            assert len(fixture.scheduled)==1
            assert state.events==['gui','route']
    finally:
        case.journal.fail(case.operation_id,'Test cleanup')
        if restart is not None:restart.join(5)


def test_one_retry_restores_live_blocked_gui_and_reopens_client(tmp_path,route_case,monkeypatch):
    from cachemonitor import app_update
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    case.journal.startup_result(case.operation_id,dict(state='starting',owner=state.gui))
    state.events.append('gui')
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    resume=state.manager.resume
    def fail_resume():raise RuntimeError('Saved-route resume failed')
    state.manager.resume=fail_resume
    assert owner.execute('start').get('error')
    case.journal.proxy_result(case.operation_id,dict(phase='failed'))
    client_restart.run_restart(case.journal,case.operation_id)
    state.manager.resume=resume
    app=case.journal.read()['app']
    failed=dict(phase='failed',operation_id=case.operation_id,scope=ProxyTarget(state.manager).scope,
                target_app_version='test',updated_at=time.time(),
                distribution={key:app[key] for key in ('executable','sha256','commit')})
    (state.manager.directory/'proxy-update.json').write_text(json.dumps(failed))
    workers=[]
    def start(journal,operation_id,recovery):
        if any(worker.is_alive() for worker in workers):return
        worker=threading.Thread(target=client_restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    monkeypatch.setattr(client_restart,'start',start)
    resumed=[]
    ensure=threading.Thread(target=lambda:resumed.append(owner.execute('ensure')))
    try:
        app_update.retry_proxy(state.manager,case.journal)
        app_update.reconcile_update(case.journal,state.manager)
        ensure.start()
        assert state.entered.wait(5)
        assert case.journal.read()['app']['startup']['state']=='starting'
        assert state.events==['gui']
        state.release.set();ensure.join(5)
        assert not ensure.is_alive()
        for worker in workers:
            worker.join(5);assert not worker.is_alive()
        assert not resumed[0].get('error'),resumed
        assert case.journal.read()['client']['phase']=='restarted'
        assert len(case.launches)==1
        assert state.events==['gui','route','client']
    finally:
        state.release.set();case.journal.fail(case.operation_id,'Test cleanup')
        if ensure.ident is not None:ensure.join(5)
        for worker in workers:worker.join(5)


def test_connection_retry_closes_authorized_client_and_reuses_ready_gui(tmp_path,route_case,monkeypatch):
    from cachemonitor import app_update
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    state.events.append('gui');state.release.set()
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    assert not owner.execute('start').get('error')
    case.journal.client_result(case.operation_id,phase='restarted')
    case.journal.proxy_result(case.operation_id,dict(phase='failed'))
    targets=[dict(pid=901,created=21,executable='fixture-client.exe',session=1,app_id=client_restart.desktop.CODEX_APP_ID)]
    running=list(targets);closed=[];workers=[]
    monkeypatch.setattr(client_restart.desktop,'desktop_processes',lambda:list(running))
    def close(values):
        closed.extend(values);running.clear()
    monkeypatch.setattr(client_restart,'close_desktop',close)
    def activate(app_id):
        assert closed==targets
        state.events.append('client')
        running.append(dict(pid=902,created=int((time.time()+.01+11644473600)*10_000_000)))
    monkeypatch.setattr(client_restart.desktop,'_activate',activate)
    def start(journal,operation_id,recovery):
        if any(worker.is_alive() for worker in workers):return
        worker=threading.Thread(target=client_restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    monkeypatch.setattr(client_restart,'start',start)
    try:
        app_update.retry_proxy(state.manager,case.journal,consent=dict(phase='authorized',targets=targets,reopen_required=True))
        assert closed==targets
        assert case.journal.read()['operation_id']==case.operation_id
        assert not owner.execute('ensure').get('error')
        for worker in workers:
            worker.join(5);assert not worker.is_alive()
        assert case.journal.read()['client']['phase']=='restarted'
        assert len(case.launches)==1
        assert state.events[-1]=='client'
    finally:
        case.journal.fail(case.operation_id,'Test cleanup')
        for worker in workers:worker.join(5)


def test_gui_death_before_readiness_releases_restart_worker(tmp_path,route_case):
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    case.journal.startup_result(case.operation_id,dict(state='starting',owner=state.gui))
    case.journal.proxy_result(case.operation_id,dict(phase='complete'))
    state.gui_live[0]=False
    worker=threading.Thread(target=client_restart.run_restart,args=(case.journal,case.operation_id))
    worker.start()
    try:
        worker.join(2)
        assert not worker.is_alive(),'Dead pending GUI retained the client restart lock'
        assert case.journal.read()['client']['phase']=='blocked'
        assert case.journal.read()['app']['startup']['state']=='blocked'
        assert not state.events
    finally:
        case.journal.fail(case.operation_id,'Test cleanup');worker.join(5)


def test_terminal_proxy_replay_recovers_gui_that_died_before_readiness(tmp_path,route_case,monkeypatch):
    from cachemonitor import app_update,observer_task
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    case.journal.startup_result(case.operation_id,dict(state='starting',owner=state.gui))
    state.gui_live[0]=False
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:dict(running=False,registered=False))
    app=case.journal.read()['app']
    terminal=dict(phase='complete',operation_id=case.operation_id,scope=ProxyTarget(state.manager).scope,
                  target_app_version='test',distribution={key:app[key] for key in ('executable','sha256','commit')})
    (state.manager.directory/'proxy-update.json').write_text(json.dumps(terminal))
    recovered=app_update.reconcile_update(case.journal,state.manager)
    assert len(case.launches)==2
    assert case.launches[-1][-2:]==['--finish-install',case.operation_id]
    assert recovered['app']['startup']['state']=='waiting'
    assert recovered['proxy']==terminal
    assert state.route[0] is None and not state.events


def test_retained_gui_binding_does_not_act_on_a_new_update(tmp_path,route_case):
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    state.events.append('gui');state.release.set()
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    assert not owner.execute('start').get('error')
    client_restart.run_restart(case.journal,case.operation_id)
    case.journal.begin('proxy','next-version',scope=ProxyTarget(state.manager).scope)
    before=case.journal.path.read_bytes()
    assert not owner.execute('ensure').get('error')
    assert case.journal.path.read_bytes()==before
    assert state.events==['gui','route','client']


def test_client_only_retry_reuses_fresh_client_without_replacing_verified_route(tmp_path,route_case,monkeypatch):
    from cachemonitor import app_update
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    state.events.append('gui');state.release.set()
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    assert not owner.execute('start').get('error')
    def interrupted(app_id):raise RuntimeError('Activation result was lost')
    monkeypatch.setattr(client_restart.desktop,'_activate',interrupted)
    client_restart.run_restart(case.journal,case.operation_id)
    failed=case.journal.read()
    fresh=[dict(pid=903,created=int((failed['client']['ready_at']+.01+11644473600)*10_000_000))]
    monkeypatch.setattr(client_restart.desktop,'desktop_processes',lambda:fresh)
    monkeypatch.setattr(client_restart,'start',lambda journal,operation_id,recovery:client_restart.run_restart(journal,operation_id))
    state.manager.update_proxy=lambda **kwargs:pytest.fail('A client-only retry replaced the verified route')
    app_update.retry_proxy(state.manager,case.journal)
    assert case.journal.read()['client']['phase']=='restarted'
    assert case.journal.read()['client']['result']==fresh
    assert state.events==['gui','route']
