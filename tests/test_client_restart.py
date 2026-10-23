import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from cachemonitor import client_restart as restart
from cachemonitor.update_state import UpdateState


def process(pid=101, created=None):
    return dict(pid=pid, created=created or int((time.time()+11644473600)*10_000_000),
                executable='owned-desktop.exe', app_id=restart.desktop.CODEX_APP_ID, session=1)


@pytest.fixture
def desktop(monkeypatch):
    state = SimpleNamespace(running=[process()], closed=[], activated=[])
    monkeypatch.setattr(restart.desktop, 'desktop_processes', lambda:list(state.running))
    def close(targets):
        state.closed.extend(targets);state.running=[]
    def activate(app_id):
        state.activated.append(app_id);state.running=[process(202)]
    monkeypatch.setattr(restart, 'close_desktop', close)
    monkeypatch.setattr(restart.desktop, '_activate', activate)
    return state


def operation(tmp_path, desktop, *, ready=True):
    journal = UpdateState(tmp_path)
    op = journal.begin('proxy', 'fixture', client=restart.capture())['operation_id']
    if ready:journal.proxy_result(op, dict(phase='off'))
    return journal, op


def settle(predicate, timeout=4):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:threading.Event().wait(.01)
    assert predicate()


def test_client_exit_and_fresh_launch_are_both_required_for_completion(tmp_path,desktop):
    journal,op=operation(tmp_path,desktop)
    assert journal.read()['phase']!='complete'
    restart.run_restart(journal,op)
    assert desktop.closed and desktop.activated==[restart.desktop.CODEX_APP_ID]
    assert journal.read()['phase']=='complete'
    restart.run_restart(journal,op)
    assert len(desktop.activated)==1


def test_new_electron_child_during_download_does_not_invalidate_app_consent(tmp_path,desktop):
    journal,op=operation(tmp_path,desktop)
    desktop.running.append(process(103))
    restart.run_restart(journal,op)
    assert {p['pid'] for p in desktop.closed}=={101,103}
    assert journal.read()['phase']=='complete'


def test_new_desktop_instance_needs_new_acknowledgement(tmp_path,desktop):
    journal,op=operation(tmp_path,desktop)
    desktop.running=[process(101,created=desktop.running[0]['created']+1)]
    restart.run_restart(journal,op)
    assert not desktop.closed and not desktop.activated
    assert journal.read()['phase']=='restart_required'


def test_close_refusal_prevents_cutover_and_never_forces_exit(tmp_path,desktop,monkeypatch):
    journal,op=operation(tmp_path,desktop)
    def refuse(targets):raise RuntimeError(restart.CLOSE_REQUIRED)
    monkeypatch.setattr(restart,'close_desktop',refuse)
    restart.run_restart(journal,op)
    assert desktop.running and not desktop.activated
    with pytest.raises(RuntimeError):restart.require_stopped(journal,op)
    assert journal.read()['phase']=='restart_required'


def test_absent_client_stays_absent_and_new_launch_blocks_activation(tmp_path,desktop):
    desktop.running=[]
    journal,op=operation(tmp_path,desktop)
    restart.run_restart(journal,op)
    assert journal.read()['phase']=='complete' and not desktop.activated
    desktop.running=[process()]
    with pytest.raises(RuntimeError):restart.require_stopped(journal,op)
    assert journal.read()['phase']=='restart_required'


@pytest.mark.parametrize('result', ['cancel','installer_failed','proxy_restored'])
def test_closed_client_reopens_only_after_success_or_verified_recovery(tmp_path,desktop,monkeypatch,result):
    journal,op=operation(tmp_path,desktop,ready=False)
    monkeypatch.setattr(restart,'restored_ready',lambda record:True)
    worker=threading.Thread(target=restart.run_restart,args=(journal,op))
    worker.start()
    try:
        settle(lambda:journal.read().get('client',{}).get('phase')=='closed')
        assert not desktop.running and not desktop.activated
        if result=='cancel':assert journal.cancel()
        elif result=='installer_failed':journal.fail(op,'Installer failed')
        else:journal.proxy_result(op,dict(phase='failed',restored=True))
        worker.join(4)
        assert not worker.is_alive() and len(desktop.activated)==1
        assert journal.read()['phase']!='complete'
        if result=='cancel':assert journal.read()['phase']=='cancelled'
    finally:
        if worker.is_alive():journal.fail(op,'Test cleanup');worker.join(4)


def test_failed_reopen_survives_late_success_and_retry_does_not_reclose(tmp_path,desktop,monkeypatch):
    journal,op=operation(tmp_path,desktop)
    activate=restart.desktop._activate
    monkeypatch.setattr(restart.desktop,'_activate',lambda app_id:(_ for _ in ()).throw(OSError('Activation failed')))
    restart.run_restart(journal,op)
    assert journal.read()['phase']=='restart_required'
    closed=list(desktop.closed)
    journal.fail(op,'Late installer error')
    journal.proxy_result(op,dict(phase='off'))
    assert journal.read()['phase']=='restart_required'
    monkeypatch.setattr(restart,'start',lambda *a:None)
    restart.retry(journal,op,tmp_path/'recovery.exe')
    monkeypatch.setattr(restart.desktop,'_activate',activate)
    restart.run_restart(journal,op)
    assert desktop.closed==closed and len(desktop.activated)==1
    assert journal.read()['phase']=='complete'


def test_resume_adopts_fresh_launch_without_duplicate_activation(tmp_path,desktop):
    journal,op=operation(tmp_path,desktop)
    ready_at=time.time()-.1
    journal.client_result(op,phase='reopening',ready_at=ready_at)
    desktop.running=[process(202)]
    restart.run_restart(journal,op)
    assert journal.read()['phase']=='complete' and not desktop.activated


def test_interrupted_close_observes_without_reusing_old_close_authorization(tmp_path,desktop):
    journal,op=operation(tmp_path,desktop)
    journal.client_result(op,phase='closing')
    restart.run_restart(journal,op)
    assert not desktop.closed and not desktop.activated
    assert journal.read()['phase']=='restart_required'


def test_missing_consent_cannot_activate_proxy_or_complete(tmp_path,desktop,monkeypatch):
    from cachemonitor import install_management
    journal=UpdateState(tmp_path);op=journal.begin('proxy','fixture')['operation_id']
    manager=SimpleNamespace()
    with pytest.raises(RuntimeError):install_management.activate_proxy(manager,operation_id=op,update_root=tmp_path)
    journal.proxy_result(op,dict(phase='off'))
    assert journal.read()['phase']!='complete' and not desktop.closed


def test_proxy_runtime_failure_preserves_desktop_and_connection(tmp_path,desktop,monkeypatch):
    from cachemonitor import app_update,install_management
    from cachemonitor.proxy_update import ProxyUpdate
    manager=SimpleNamespace(directory=tmp_path,home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768')
    monkeypatch.setattr(app_update,'installed',lambda:dict(InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'inspect_proxy',lambda manager:dict(state='required',instance='owned'))
    monkeypatch.setattr(ProxyUpdate,'preflight',lambda self:(_ for _ in ()).throw(RuntimeError('Runtime failed')))
    monkeypatch.setattr(install_management,'activate_proxy',lambda *a,**kw:pytest.fail('Connection changed after failed preflight'))
    plan=dict(kind='proxy',manager=manager,proxy=dict(instance='owned'))
    with pytest.raises(RuntimeError,match='Runtime failed'):app_update.update(plan=plan,consent=restart.capture())
    assert desktop.running and not desktop.closed and not desktop.activated


def test_cancel_and_install_activation_share_one_boundary(tmp_path,desktop):
    journal,op=operation(tmp_path,desktop,ready=False)
    journal.client_result(op,phase='closed')
    assert journal.cancel() and not journal.begin_activation(op)
    journal.change(op,cancel_requested=False)
    assert journal.begin_activation(op) and not journal.cancel()


@pytest.mark.skipif(os.name!='nt',reason='Windows Task Scheduler')
def test_restart_task_survives_the_launching_gui_process(tmp_path,desktop):
    from cachemonitor.observer_task import ObserverTask
    journal,op=operation(tmp_path,desktop,ready=False)
    worker=tmp_path/'restart-worker.py';launcher=tmp_path/'gui-surrogate.py'
    root=Path(__file__).resolve().parents[1]
    worker.write_text('''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from cachemonitor import client_restart as restart
from cachemonitor.update_state import UpdateState
journal=UpdateState(sys.argv[2]);op=sys.argv[3]
running=journal.read()['client']['targets']
restart.desktop.desktop_processes=lambda:list(running)
def close(targets):running.clear()
def activate(app_id):
    running.append(dict(pid=202,created=int((time.time()+11644473600)*10_000_000),executable='owned',session=1,app_id=app_id))
restart.close_desktop=close;restart.desktop._activate=activate
restart.restored_ready=lambda record:True
restart.run_restart(journal,op)
''',encoding='utf-8')
    launcher.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from cachemonitor import client_restart as restart
from cachemonitor.update_state import UpdateState
from cachemonitor.observer_task import ObserverTask
original=ObserverTask.start
def start(self,command,autostart=False):
    assert '--restart-client' in command
    return original(self,[sys.executable,sys.argv[4],sys.argv[1],sys.argv[2],sys.argv[3]])
ObserverTask.start=start
restart.start(UpdateState(sys.argv[2]),sys.argv[3],Path(sys.argv[4]))
''',encoding='utf-8')
    task=ObserverTask(str(journal.root)+':'+op,role='ClientRestart')
    try:
        launched=subprocess.run([sys.executable,str(launcher),str(root),str(journal.root),op,str(worker)],timeout=25)
        assert launched.returncode==0
        settle(lambda:journal.read().get('client',{}).get('phase')=='closed',timeout=15)
        assert task.inspect().get('running')
        journal.proxy_result(op,dict(phase='off'))
        settle(lambda:journal.read().get('phase')=='complete',timeout=10)
        settle(lambda:not task.inspect().get('running'),timeout=15)
    finally:
        journal.fail(op,'Test cleanup')
        settle(lambda:not task.inspect().get('running'),timeout=15)
        task.remove()


@pytest.mark.parametrize('silent,app_initiated', [(True,False),(False,False),(True,True)])
def test_upgrade_requires_consent_after_preflight_before_activation(tmp_path,desktop,monkeypatch,silent,app_initiated):
    from cachemonitor import install_management as install
    from tests.test_install_management import fixture
    product,recovery=fixture(tmp_path)
    (tmp_path/'installation.json').write_text(json.dumps(dict(product='old')))
    journal=UpdateState(tmp_path)
    request=journal.begin('app','test') if app_initiated else {}
    if request:
        request.pop('client');journal.write(request)
    verified=[];prompts=[]
    def runtime(command,**kwargs):
        verified.append(True);Path(command[-1]).write_text(json.dumps(dict(errors=[],version='test')))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(install.subprocess,'run',runtime)
    monkeypatch.setattr(restart,'confirm_native',lambda language:prompts.append(bool(verified)) or False)
    monkeypatch.setattr(restart,'prepare_restart',lambda *a:pytest.fail('Restart preparation preceded consent'))
    from cachemonitor import install_activation
    monkeypatch.setattr(install_activation,'Activation',lambda *a:pytest.fail('Activation preceded consent'))
    with pytest.raises(RuntimeError,match='재시작'):
        install.finish(tmp_path,product,recovery,silent=silent,update_id=request.get('operation_id'))
    assert verified and prompts==([True] if app_initiated or not silent else [])
    assert not desktop.closed and not desktop.activated
    assert not journal.read().get('activation_started')


@pytest.mark.parametrize('supplied_id,failure,legacy', [(False,'register',False),(False,'constructor',False),(True,'register',False),(True,'register',True)])
def test_install_failure_after_closure_reports_its_operation_and_reopens(tmp_path,desktop,monkeypatch,supplied_id,failure,legacy):
    from cachemonitor import install_activation,install_management as install,launch_context
    from tests.test_install_management import fixture
    product,recovery=fixture(tmp_path)
    (tmp_path/'installation.json').write_text(json.dumps(dict(product='old')))
    manager=SimpleNamespace(directory=tmp_path,home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768')
    journal=UpdateState(tmp_path);workers=[];restored=[];prompts=[]
    op=journal.begin('app','test',client=restart.capture())['operation_id'] if supplied_id else None
    if legacy:
        request=journal.read();request.pop('client');journal.write(request)
    def runtime(command,**kwargs):
        Path(command[-1]).write_text(json.dumps(dict(errors=[],version='test')))
        return SimpleNamespace(returncode=0)
    def start(journal,operation_id,recovery):
        worker=threading.Thread(target=restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    class Activation:
        def __init__(self,*args):
            if failure=='constructor':restored.append(True);raise OSError('Activation failed')
        def rollback(self):restored.append(True)
    monkeypatch.setattr(install.subprocess,'run',runtime)
    monkeypatch.setattr(install,'connection_manager',lambda:manager)
    monkeypatch.setattr(restart,'confirm_native',lambda language:prompts.append(not workers and not desktop.closed and not restored) or True)
    monkeypatch.setattr(restart,'start',start)
    monkeypatch.setattr(restart,'restored_ready',lambda record:bool(restored))
    monkeypatch.setattr(install_activation,'Activation',Activation)
    monkeypatch.setattr(launch_context,'running_homes',lambda root:[])
    monkeypatch.setattr(launch_context,'resolve_homes',lambda homes:[])
    monkeypatch.setattr(install,'register',lambda *a,**kw:(_ for _ in ()).throw(OSError('Activation failed')))
    try:
        with pytest.raises(OSError,match='Activation failed'):
            install.finish(tmp_path,product,recovery,launch=False,update_id=op,silent=legacy)
        assert prompts==([True] if legacy or not supplied_id else [])
        assert restored and desktop.closed
        assert journal.read()['component_phase']=='failed'
        settle(lambda:all(not worker.is_alive() for worker in workers))
        assert len(desktop.activated)==1 and journal.read()['client']['phase']=='restarted'
    finally:
        if workers:
            journal.fail(journal.read()['operation_id'],'Test cleanup')
            for worker in workers:worker.join(4)


@pytest.mark.parametrize('recovered', [False,True])
def test_proxy_retry_resets_previous_failure_before_reclosing_and_applies_connection(tmp_path,desktop,monkeypatch,recovered):
    from cachemonitor import app_update,install_management
    from cachemonitor.proxy_update import ProxyUpdate
    from cachemonitor.update_state import scope_for
    manager=SimpleNamespace(directory=tmp_path,home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768')
    journal=UpdateState(tmp_path)
    op=journal.begin('proxy','fixture',scope=scope_for(manager),client=restart.capture())['operation_id']
    journal.proxy_result(op,dict(phase='failed',restored=True,cutover_started=True))
    if recovered:restart.run_restart(journal,op)
    else:journal.client_result(op,phase='blocked',message=restart.CLOSE_REQUIRED)
    journal.change(op,cancel_requested=True,defer_requested=True)
    workers=[];applied=[]
    def start(journal,operation_id,recovery):
        record=journal.read()
        assert record['component_phase']=='waiting' and not record['cancel_requested'] and not record['defer_requested']
        assert not record['proxy'].get('restored') and record['proxy'].get('phase')!='failed'
        assert not record['client'].get('ready_at') and not record['client'].get('result')
        worker=threading.Thread(target=restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    def activate(manager,**kwargs):
        restart.require_stopped(journal,op);applied.append(True)
        return dict(phase='off')
    monkeypatch.setattr(restart,'start',start)
    monkeypatch.setattr(restart,'recovery_path',lambda *a:tmp_path/'recovery.exe')
    monkeypatch.setattr(ProxyUpdate,'preflight',lambda self:{})
    monkeypatch.setattr(install_management,'activate_proxy',activate)
    try:
        app_update.retry_proxy(manager,journal,consent=restart.capture())
        settle(lambda:journal.read().get('phase')=='complete')
        assert applied==[True] and len(desktop.activated)==(2 if recovered else 1)
    finally:
        journal.fail(op,'Test cleanup')
        for worker in workers:worker.join(4)


@pytest.mark.parametrize('offered', [None,'app','discovered'])
def test_rendered_install_rollback_retries_only_failed_reopening(tmp_path,desktop,monkeypatch,offered):
    from PySide6.QtCore import QPointF,Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from cachemonitor import app_update,install_activation,install_management as install,installation,launch_context
    from cachemonitor.proxy_update import ProxyUpdate
    from cachemonitor.quick_runtime import QuickHost
    from cachemonitor.quick_qa import control
    from cachemonitor.update_panel import UpdatePanel
    from tests.test_install_management import fixture
    app=QApplication.instance() or QApplication([])
    product,recovery=fixture(tmp_path);receipt=tmp_path/'installation.json'
    previous=dict(product='old');receipt.write_text(json.dumps(previous))
    manager=SimpleNamespace(directory=tmp_path,home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768')
    journal=UpdateState(tmp_path);workers=[];installs=[];launches=[]
    activate=restart.desktop._activate
    def runtime(command,**kwargs):
        Path(command[-1]).write_text(json.dumps(dict(errors=[],version='test')))
        return SimpleNamespace(returncode=0)
    def start(journal,operation_id,recovery):
        worker=threading.Thread(target=restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    def register(*args,**kwargs):
        installs.append(True);receipt.write_text(json.dumps(dict(product='broken')))
        raise OSError('Activation failed')
    def reopen(app_id):
        launches.append(app_id)
        if len(launches)==1:raise OSError('First reopening failed')
        activate(app_id)
    monkeypatch.setattr(install.subprocess,'run',runtime)
    monkeypatch.setattr(install,'connection_manager',lambda:manager)
    monkeypatch.setattr(install,'register',register)
    monkeypatch.setattr(install_activation,'shortcuts',lambda isolated:[])
    monkeypatch.setattr(install_activation,'snapshot_registry',lambda isolated:[])
    monkeypatch.setattr(install_activation,'restore_registry',lambda values:None)
    monkeypatch.setattr(installation,'pointer_path',lambda:tmp_path/'pointer.json')
    monkeypatch.setattr(launch_context,'preference_path',lambda:tmp_path/'launch.json')
    monkeypatch.setattr(launch_context,'running_homes',lambda root:[])
    monkeypatch.setattr(launch_context,'resolve_homes',lambda homes:[])
    monkeypatch.setattr(restart,'confirm_native',lambda language:True)
    monkeypatch.setattr(restart,'start',start)
    monkeypatch.setattr(restart,'recovery_path',lambda *a:recovery)
    monkeypatch.setattr(restart,'restored_ready',lambda record:json.loads(receipt.read_text())==previous and not (tmp_path/'activation-pending.json').exists())
    monkeypatch.setattr(restart.desktop,'_activate',reopen)
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    monkeypatch.setattr(app_update,'update',lambda *a,**kw:pytest.fail('Reopening attempted another installation'))
    monkeypatch.setattr(app_update,'continuation_target',lambda *a:pytest.fail('Reopening required another app'))
    monkeypatch.setattr(install,'activate_proxy',lambda *a,**kw:pytest.fail('Reopening changed the connection'))
    monkeypatch.setattr(ProxyUpdate,'preflight',lambda self:pytest.fail('Reopening retried the connection'))
    panel=None;host=None
    try:
        with pytest.raises(OSError,match='Activation failed'):
            install.finish(tmp_path,product,recovery,launch=False)
        settle(lambda:all(not worker.is_alive() for worker in workers))
        record=journal.read();op=record['operation_id'];closed=list(desktop.closed)
        assert json.loads(receipt.read_text())==previous and not (tmp_path/'activation-pending.json').exists()
        assert record['app']['state']=='pending' and record['component_phase']=='failed'
        assert record['client']['phase']=='blocked' and record['client']['ready_at']
        assert closed and not desktop.running and len(launches)==1
        panel=UpdatePanel(manager)
        panel.receive_result(OSError('Activation failed'))
        if offered=='app':panel.show_plan(dict(kind='app',release=dict(tag_name='v2099.01.01.1')))
        elif offered=='discovered':panel.release_discovered(dict(tag_name='v2099.01.01.1'))
        else:panel.render_record()
        host=QuickHost();host.resize(520,300);host.setCentralWidget(panel);host.show()
        item=control(host,panel.execute)
        deadline=time.monotonic()+4
        while not (item.isVisible() and item.isEnabled() and item.width()>0 and item.height()>0) and time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5)
        assert item.isVisible() and item.isEnabled() and item.width()>0 and item.height()>0
        host.quick.grabFramebuffer()
        point=item.mapToScene(QPointF(item.width()/2,item.height()/2)).toPoint()
        assert host.quick.rect().contains(point)
        QTest.mouseClick(host.quick,Qt.LeftButton,pos=point)
        deadline=time.monotonic()+4
        while not (panel.operation is None and journal.read().get('client',{}).get('phase')=='restarted') and time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5)
        record=journal.read()
        assert panel.operation is None and record['client']['phase']=='restarted'
        assert not panel._restart_confirmation and installs==[True] and len(launches)==2
        assert record['operation_id']==op and record['app']['state']=='pending' and record['component_phase']=='failed'
        assert desktop.closed==closed and desktop.running and len(desktop.activated)==1
        with pytest.raises(RuntimeError,match='앱 설치가 완료되지 않았습니다'):
            app_update.retry_proxy(manager,journal,consent=restart.capture())
    finally:
        if panel:
            if panel._restart_confirmation:panel._restart_confirmation.reject();app.processEvents()
            panel.stop()
            if panel.operation:panel.operation.wait()
        if journal.read():journal.fail(journal.read()['operation_id'],'Test cleanup')
        for worker in workers:worker.join(4)
        if host:host.release_scene();host.close();host.deleteLater();app.processEvents()


@pytest.mark.parametrize('full_update', [False,True])
def test_manual_exit_after_close_refusal_preserves_reopening_on_retry(tmp_path,desktop,monkeypatch,full_update):
    from cachemonitor import app_update,install_management
    from cachemonitor.proxy_update import ProxyUpdate
    from cachemonitor.update_state import scope_for
    manager=SimpleNamespace(directory=tmp_path,home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768')
    journal=UpdateState(tmp_path);version='2099.01.01.1'
    op=journal.begin('app' if full_update else 'proxy',version,scope=scope_for(manager),client=restart.capture())['operation_id']
    close=restart.close_desktop
    monkeypatch.setattr(restart,'close_desktop',lambda targets:(_ for _ in ()).throw(RuntimeError(restart.CLOSE_REQUIRED)))
    restart.run_restart(journal,op)
    journal.fail(op,'Close refused')
    assert desktop.running and journal.read()['phase']=='restart_required'
    desktop.running=[];workers=[];applied=[]
    monkeypatch.setattr(restart,'close_desktop',close)
    def start(journal,operation_id,recovery):
        worker=threading.Thread(target=restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    def activate(manager,**kwargs):
        restart.require_stopped(journal,kwargs['operation_id']);applied.append(True)
        return dict(phase='off')
    def install(release,installed,progress,journal,operation_id):
        restart.prepare_restart(journal,operation_id,tmp_path/'recovery.exe')
        journal.change(operation_id,app=dict(state='verified'))
        journal.proxy_result(operation_id,activate(manager,operation_id=operation_id))
    monkeypatch.setattr(restart,'start',start)
    monkeypatch.setattr(restart,'recovery_path',lambda *a:tmp_path/'recovery.exe')
    monkeypatch.setattr(ProxyUpdate,'preflight',lambda self:{})
    monkeypatch.setattr(install_management,'activate_proxy',activate)
    monkeypatch.setattr(app_update,'installed',lambda:dict(InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'download_and_install',install)
    try:
        if full_update:
            app_update.update(plan=dict(kind='app',release=dict(tag_name='v'+version)),manager=manager,consent=restart.capture())
        else:app_update.retry_proxy(manager,journal,consent=restart.capture())
        settle(lambda:journal.read().get('phase')=='complete')
        assert applied==[True] and desktop.activated==[restart.desktop.CODEX_APP_ID]
        assert desktop.closed==[]
    finally:
        journal.fail(journal.read()['operation_id'],'Test cleanup')
        for worker in workers:worker.join(4)


@pytest.mark.parametrize('manual_exit', [False,True])
def test_rendered_proxy_retry_confirms_then_reapplies_and_reopens(tmp_path,desktop,monkeypatch,manual_exit):
    from PySide6.QtCore import QPointF,Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from cachemonitor import install_management
    from cachemonitor.proxy_update import ProxyUpdate
    from cachemonitor.quick_runtime import QuickHost
    from cachemonitor.quick_qa import control
    from cachemonitor.update_panel import UpdatePanel
    from cachemonitor.update_state import scope_for
    app=QApplication.instance() or QApplication([])
    manager=SimpleNamespace(directory=tmp_path,home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768')
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(manager);journal=panel.journal;workers=[];applied=[]
    op=journal.begin('proxy','fixture',scope=scope_for(manager),client=restart.capture())['operation_id']
    journal.proxy_result(op,dict(phase='failed',restored=True))
    if manual_exit:
        close=restart.close_desktop
        monkeypatch.setattr(restart,'close_desktop',lambda targets:(_ for _ in ()).throw(RuntimeError(restart.CLOSE_REQUIRED)))
        restart.run_restart(journal,op)
        assert journal.read()['client']['phase']=='blocked'
        desktop.running=[]
        monkeypatch.setattr(restart,'close_desktop',close)
    else:
        restart.run_restart(journal,op)
        assert journal.read()['client']['phase']=='restarted'
    def start(journal,operation_id,recovery):
        worker=threading.Thread(target=restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    def activate(manager,**kwargs):
        restart.require_stopped(journal,op);applied.append(True)
        return dict(phase='off')
    monkeypatch.setattr(restart,'start',start)
    monkeypatch.setattr(restart,'recovery_path',lambda *a:tmp_path/'recovery.exe')
    monkeypatch.setattr(ProxyUpdate,'preflight',lambda self:{})
    monkeypatch.setattr(install_management,'activate_proxy',activate)
    host=QuickHost();host.resize(520,300);host.setCentralWidget(panel);host.show()
    def wait(predicate):
        deadline=time.monotonic()+4
        while not predicate() and time.monotonic()<deadline:app.processEvents();QTest.qWait(5)
        assert predicate()
    def click(window,node):
        item=control(window,node)
        wait(lambda:item.isVisible() and item.isEnabled() and item.width()>0 and item.height()>0)
        window.quick.grabFramebuffer()
        point=item.mapToScene(QPointF(item.width()/2,item.height()/2)).toPoint()
        assert window.quick.rect().contains(point)
        QTest.mouseClick(window.quick,Qt.LeftButton,pos=point)
    try:
        panel.refresh();click(host,panel.execute)
        wait(lambda:panel._restart_confirmation is not None)
        assert not applied
        dialog=panel._restart_confirmation
        click(dialog.host,dialog.confirm)
        wait(lambda:panel.operation is None and journal.read().get('phase')=='complete')
        assert applied==[True] and len(desktop.activated)==(1 if manual_exit else 2)
    finally:
        if panel._restart_confirmation:panel._restart_confirmation.reject();app.processEvents()
        panel.stop()
        if panel.operation:panel.operation.wait()
        journal.fail(op,'Test cleanup')
        for worker in workers:worker.join(4)
        host.release_scene();host.close();host.deleteLater();app.processEvents()


@pytest.mark.skipif(os.name!='nt',reason='Native save-work prompt')
def test_native_confirmation_enter_cancels_owned_prompt(tmp_path):
    helper=tmp_path/'confirmation.py';result=tmp_path/'confirmation.json';ready=tmp_path/'confirmation-ready.json'
    helper.write_text('''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from cachemonitor.client_restart import confirm_native
from cachemonitor.observer_control import atomic_write
atomic_write(Path(sys.argv[3]),json.dumps({'pid':os.getpid()}).encode())
Path(sys.argv[2]).write_text(json.dumps(confirm_native('en')))
''',encoding='utf-8')
    user=C.WinDLL('user32',use_last_error=True)
    callback_type=C.WINFUNCTYPE(W.BOOL,W.HWND,W.LPARAM)
    user.EnumWindows.argtypes=[callback_type,W.LPARAM]
    user.GetWindowThreadProcessId.argtypes=[W.HWND,C.POINTER(W.DWORD)]
    user.PostMessageW.argtypes=[W.HWND,W.UINT,W.WPARAM,W.LPARAM]
    worker=subprocess.Popen([sys.executable,str(helper),str(Path(__file__).resolve().parents[1]),str(result),str(ready)])
    windows=[];owned_pid=None
    @callback_type
    def owned(hwnd,_):
        pid=W.DWORD();user.GetWindowThreadProcessId(hwnd,C.byref(pid))
        if pid.value==owned_pid:windows.append(hwnd)
        return True
    def visible():
        windows.clear();user.EnumWindows(owned,0)
        return bool(windows)
    try:
        settle(ready.exists)
        owned_pid=json.loads(ready.read_text())['pid']
        settle(visible)
        assert user.PostMessageW(windows[0],0x100,13,0)
        assert user.PostMessageW(windows[0],0x101,13,0)
        assert worker.wait(5)==0 and json.loads(result.read_text()) is False
    finally:
        if worker.poll() is None:
            visible()
            for hwnd in windows:user.PostMessageW(hwnd,0x10,0,0)
            worker.wait(5)


@pytest.mark.skipif(os.name!='nt',reason='Native desktop process handles')
@pytest.mark.parametrize('refuse',[False,True])
def test_native_normal_close_uses_only_owned_exact_handles(tmp_path,monkeypatch,refuse):
    from cachemonitor.proxy_identity import process_identity
    helper=tmp_path/'window.py'
    helper.write_text('''import json,os,sys,tkinter as tk
from pathlib import Path
root=tk.Tk();root.geometry('240x100')
stop=Path(sys.argv[1]);ready=Path(sys.argv[2])
root.protocol('WM_DELETE_WINDOW',lambda:None if sys.argv[3]=='refuse' else root.destroy())
def poll():
    if stop.exists():root.destroy()
    else:root.after(10,poll)
root.update();ready.write_text(json.dumps({'hwnd':root.winfo_id(),'pid':os.getpid()}));poll();root.mainloop()
''',encoding='utf-8')
    owned=[]
    try:
        for name in ('target','cli-surrogate'):
            ready=tmp_path/(name+'.json');stop=tmp_path/(name+'.stop')
            worker=subprocess.Popen([sys.executable,str(helper),str(stop),str(ready),'refuse' if refuse else 'close'])
            owned.append((worker,stop));settle(ready.exists)
        target=process_identity(json.loads((tmp_path/'target.json').read_text())['pid'])
        kernel=restart.desktop._process_api();session=W.DWORD()
        assert kernel.ProcessIdToSessionId(os.getpid(),C.byref(session))
        target.update(app_id=restart.desktop.CODEX_APP_ID,session=session.value)
        if refuse:
            with pytest.raises(RuntimeError):restart.close_desktop([target],timeout=.3)
            assert owned[0][0].poll() is None
        else:
            restart.close_desktop([target],timeout=4)
            assert owned[0][0].wait(2)==0
        assert owned[1][0].poll() is None
    finally:
        for worker,stop in owned:stop.touch()
        for worker,_ in owned:worker.wait(4)


@pytest.mark.parametrize('language', ['ko','en'])
@pytest.mark.parametrize('discovered', [False,True])
def test_rendered_confirmation_cancel_then_update_preserves_exact_offer(tmp_path,monkeypatch,language,discovered):
    from PySide6.QtCore import QPointF,Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from cachemonitor import app_update,i18n
    from cachemonitor.quick_runtime import QuickHost
    from cachemonitor.quick_qa import control
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([]);i18n.set_language(language)
    calls=[];checks=[]
    plan=dict(kind='app',release=dict(tag_name='v2099.01.01.1'))
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    monkeypatch.setattr(restart,'capture',lambda:dict(phase='authorized',targets=[]))
    monkeypatch.setattr(app_update,'update',lambda *a,**kw:calls.append(kw) or 'queued')
    monkeypatch.setattr(app_update,'check_update',lambda *a,**kw:checks.append(kw['release']) or plan)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    host=QuickHost();host.resize(520,300);host.setCentralWidget(panel);host.show()
    def wait(predicate):
        deadline=time.monotonic()+4
        while not predicate() and time.monotonic()<deadline:app.processEvents();QTest.qWait(5)
        assert predicate()
    def click(window,node):
        item=control(window,node)
        wait(lambda:item.isVisible() and item.isEnabled() and item.width()>0 and item.height()>0)
        window.quick.grabFramebuffer()
        point=item.mapToScene(QPointF(item.width()/2,item.height()/2)).toPoint()
        assert window.quick.rect().contains(point)
        QTest.mouseClick(window.quick,Qt.LeftButton,pos=point)
    try:
        if discovered:panel.release_discovered(plan['release'])
        else:panel.show_plan(plan)
        click(host,panel.execute)
        wait(lambda:panel._restart_confirmation is not None)
        dialog=panel._restart_confirmation
        assert not calls and bool(checks)==discovered
        wait(lambda:dialog.host.quick.isVisible())
        output=Path(__file__).resolve().parents[1]/'artifacts/update-restart/implementation-checks'
        output.mkdir(parents=True,exist_ok=True)
        image=dialog.host.quick.grabFramebuffer()
        assert not image.isNull()
        image.save(str(output/('confirmation-'+language+('-discovered' if discovered else '')+'.png')))
        QTest.keyClick(dialog.host.quick,Qt.Key_Return);app.processEvents()
        assert not calls
        if panel._restart_confirmation:click(dialog.host,dialog.cancel)
        wait(lambda:panel._restart_confirmation is None)
        assert not calls and panel.offer is plan
        click(host,panel.execute);wait(lambda:panel._restart_confirmation is not None)
        dialog=panel._restart_confirmation
        click(dialog.host,dialog.confirm)
        wait(lambda:panel.operation is None and len(calls)==1)
        assert calls[0]['plan'] is plan and calls[0]['consent']['phase']=='authorized'
    finally:
        if panel._restart_confirmation:panel._restart_confirmation.reject();app.processEvents()
        panel.stop()
        if panel.operation:panel.operation.wait()
        host.release_scene();host.close();host.deleteLater();app.processEvents();i18n.set_language('ko')
