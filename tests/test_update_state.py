"""Durable update handoff, cancellation boundaries and stale result isolation."""
from types import SimpleNamespace
from pathlib import Path
import hashlib
import io
import pytest
from cachemonitor.update_state import UpdateState,scope_for
from cachemonitor import app_update


def manager(root):
    return SimpleNamespace(home=root/'custom-home',evidence=root/'records.sqlite',directory=root/'observer',
                           url='http://127.0.0.1:18768',status=lambda:{'configured':False})


def test_install_handoff_survives_restart_and_rejects_previous_results(tmp_path):
    journal=UpdateState(tmp_path);scope=scope_for(manager(tmp_path))
    first=journal.begin('app','2026.10.09.1',scope=scope)
    product=tmp_path/'versions'/'new'/'Codexon'
    manifest=dict(version='2026.10.09.1',sha256='a'*64,commit='b'*40)
    journal.installed(first['operation_id'],product,manifest)
    resumed=UpdateState(tmp_path)
    resumed.proxy_result(first['operation_id'],dict(phase='waiting',required_action='close_client',unknown_connections=1,scope=scope))
    assert resumed.read()['phase']=='needs_exit'
    assert resumed.read()['app']['executable']==str(product/'Codexon.exe')
    # A button or timeout never asserts connection closure.
    assert resumed.read()['proxy']['unknown_connections']==1
    resumed.proxy_result(first['operation_id'],dict(phase='failed',restored=True,scope=scope))
    assert resumed.read()['phase']=='partial'
    second=resumed.begin('proxy','2026.10.09.1',scope=scope)
    assert not journal.proxy_result(first['operation_id'],dict(phase='complete'))
    assert not journal.change(first['operation_id'],phase='complete')
    assert resumed.read()['operation_id']==second['operation_id'] and resumed.read()['phase']=='preparing'


@pytest.mark.parametrize('cancel_first',[True,False])
def test_cancel_and_swap_have_one_atomic_winner(tmp_path,cancel_first):
    journal=UpdateState(tmp_path);record=journal.begin('proxy','2026.10.09.1');op=record['operation_id']
    journal.proxy_result(op,dict(phase='waiting'))
    if cancel_first:
        assert journal.cancel(later=True)
        assert not journal.begin_switch(op)
        journal.proxy_result(op,dict(phase='waiting'))
        assert journal.read()['cancel_requested']
    else:
        assert journal.begin_switch(op)
        assert not journal.cancel(later=True)
        journal.proxy_result(op,dict(phase='waiting',cutover_started=True,required_action='close_client'))
        assert not journal.cancel()


def test_partial_retry_keeps_app_and_disabled_proxy(tmp_path,monkeypatch):
    from cachemonitor import install_management,retired_cache
    journal=UpdateState(tmp_path);m=manager(tmp_path)
    m.cleanup_legacy_check=lambda:None;m.adopt_registrations=lambda:None
    monkeypatch.setattr(retired_cache,'retire',lambda m:None)
    record=journal.begin('app','2026.10.09.1',scope=scope_for(m));op=record['operation_id']
    journal.installed(op,tmp_path/'versions'/'new'/'Codexon',dict(version='2026.10.09.1',sha256='a'*64,commit='b'*40))
    journal.proxy_result(op,dict(phase='failed',restored=True))
    app=journal.read()['app']
    monkeypatch.setattr(app_update,'launch_installer',lambda *a:pytest.fail('Must not reinstall the app'))
    result=app_update.retry_proxy(m,journal)
    assert result['phase']=='complete' and result['app']==app and result['proxy']['phase']=='off'
    assert result['operation_id']==op


def test_download_cancel_never_launches_installer_or_changes_auth(tmp_path,monkeypatch):
    journal=UpdateState(tmp_path);m=manager(tmp_path)
    auth=m.home/'auth.json';auth.parent.mkdir();auth.write_bytes(b'preserve authentication')
    payload=b'payload';digest=hashlib.sha256(payload).hexdigest()
    from tests.test_app_update import release
    offered=release();record=journal.begin('app',offered['tag_name'].lstrip('v'))
    monkeypatch.setattr(app_update,'read_url',lambda *a:(digest+'  Codexon-Setup.exe').encode())
    class Download(io.BytesIO):
        def read(self,size):
            journal.cancel();return super().read(size)
    monkeypatch.setattr(app_update.urllib.request,'urlopen',lambda *a,**k:Download(payload))
    monkeypatch.setattr(app_update,'launch_installer',lambda *a:pytest.fail('Cancelled installer launched'))
    with pytest.raises(app_update.UpdateCancelled):
        app_update.download_and_install(offered,dict(InstallRoot=str(tmp_path)),lambda _:None,journal,record['operation_id'])
    assert not list(tmp_path.rglob('*.exe')) and not list(tmp_path.rglob('*.partial'))
    assert auth.read_bytes()==b'preserve authentication'


def test_wrong_target_install_is_rejected_before_activation(tmp_path,monkeypatch):
    from tests.test_install_management import fixture
    from cachemonitor import install_management
    product,recovery=fixture(tmp_path)
    journal=UpdateState(tmp_path);op=journal.begin('app','another-version')['operation_id']
    monkeypatch.setattr(install_management,'register',lambda *a,**k:pytest.fail('Incorrect version activated'))
    with pytest.raises(RuntimeError,match='다릅니다'):
        install_management.finish(tmp_path,product,recovery,isolated=True,update_id=op)
    assert not (tmp_path/'installation.json').exists()


def test_abandoned_download_requires_owner_exit_not_elapsed_time(tmp_path,monkeypatch):
    from cachemonitor import proxy_identity
    journal=UpdateState(tmp_path);op=journal.begin('app','2026.10.09.1')['operation_id']
    record=journal.read();record.update(phase='downloading',updated_at=0)
    import json
    journal.path.write_text(json.dumps(record),encoding='utf-8')
    monkeypatch.setattr(proxy_identity,'same_process',lambda owner:True)
    assert app_update.reconcile_update(journal)['phase']=='downloading'
    monkeypatch.setattr(proxy_identity,'same_process',lambda owner:False)
    assert app_update.reconcile_update(journal)['phase']=='interrupted'


def test_previous_app_worker_completion_is_not_new_deployment_completion(tmp_path,monkeypatch):
    from cachemonitor import install_management
    m=manager(tmp_path);journal=UpdateState(tmp_path);op=journal.begin('app','2026.10.09.1',scope=scope_for(m))['operation_id']
    journal.installed(op,tmp_path/'versions'/'new'/'Codexon',dict(version='2026.10.09.1',sha256='a'*64,commit='b'*40))
    journal.proxy_result(op,dict(phase='waiting',source_instance='previous-worker'))
    m.update_status=lambda:dict(phase='complete',source_instance='previous-worker')
    calls=[]
    def apply(manager,**kwargs):
        calls.append(kwargs)
        return dict(phase='queued',operation_id=op)
    monkeypatch.setattr(install_management,'activate_proxy',apply)
    result=app_update.reconcile_update(journal,m)
    assert result['phase']=='waiting' and calls==[dict(operation_id=op,update_root=tmp_path)]


def linked_proxy(tmp_path):
    from cachemonitor.version import VERSION, PROXY_VERSION
    journal=UpdateState(tmp_path/'installed');m=manager(tmp_path)
    scope=scope_for(m)
    op=journal.begin('app',VERSION,scope=scope)['operation_id']
    product=journal.root/'versions'/'new'/'Codexon'
    journal.installed(op,product,dict(version=VERSION,sha256='a'*64,commit='b'*40))
    distribution=dict(executable=str(product/'Codexon.exe'),sha256='a'*64,commit='b'*40)
    result=dict(phase='verifying',operation_id=op,scope=scope,target_app_version=VERSION,
                target_version=PROXY_VERSION,distribution=distribution,update_root=str(journal.root),updated_at=1)
    journal.proxy_result(op,result)
    m.directory.mkdir(parents=True,exist_ok=True)
    return journal,m,result


@pytest.mark.parametrize('phase,summary',[('complete','complete'),('off','complete'),('cancelled','deferred'),('failed','partial')])
def test_terminal_proxy_result_replays_after_crash_between_journals(tmp_path,monkeypatch,phase,summary):
    from cachemonitor.proxy_update import ProxyUpdate
    journal,m,result=linked_proxy(tmp_path)
    updater=ProxyUpdate(m,target=SimpleNamespace(scope=result['scope']))
    updater.publish('verifying',**{key:value for key,value in result.items()
                                  if key not in ('phase','updated_at','target_version','target_app_version')})
    original=UpdateState.proxy_result
    class Crash(BaseException):pass
    def interrupted(self,operation_id,value,**kwargs):
        if value.get('phase')==phase:raise Crash('proxy result committed, app result missing')
        return original(self,operation_id,value,**kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(UpdateState,'proxy_result',interrupted)
        with pytest.raises(Crash):updater.publish(phase,message='terminal result')
    assert journal.read()['phase']=='verifying'
    updater.replace()
    assert journal.read()['phase']==summary
    settled=journal.path.read_bytes()
    updater.replace()
    assert journal.path.read_bytes()==settled


@pytest.mark.parametrize('mismatch',['operation','scope','app_version','executable','hash','commit','unverified'])
def test_reconciliation_rejects_unrelated_or_unverified_proxy_completion(tmp_path,mismatch):
    import copy,json
    journal,m,result=linked_proxy(tmp_path)
    result=copy.deepcopy(result);result.update(phase='complete',updated_at=2)
    if mismatch=='operation':result['operation_id']='f'*32
    elif mismatch=='scope':result['scope']['url']='http://127.0.0.1:19999'
    elif mismatch=='app_version':result['target_app_version']='2026.01.01.1'
    elif mismatch=='executable':result['distribution']['executable']=str(tmp_path/'other'/'Codexon.exe')
    elif mismatch=='hash':result['distribution']['sha256']='c'*64
    elif mismatch=='commit':result['distribution']['commit']='d'*40
    else:result.pop('distribution')
    (m.directory/'proxy-update.json').write_text(json.dumps(result),encoding='utf-8')
    before=journal.path.read_bytes()
    assert app_update.reconcile_update(journal,m)['phase']=='verifying'
    assert journal.path.read_bytes()==before


def test_app_owner_replays_matching_proxy_result_without_owner_probes(tmp_path,monkeypatch):
    import json
    from cachemonitor import observer_task
    journal,m,result=linked_proxy(tmp_path);result.update(phase='complete',updated_at=2)
    (m.directory/'proxy-update.json').write_text(json.dumps(result),encoding='utf-8')
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:pytest.fail('Terminal evidence needs no task probe'))
    assert app_update.reconcile_update(journal,m)['phase']=='complete'
    before=journal.path.read_bytes()
    assert app_update.reconcile_update(journal,m)['phase']=='complete'
    assert journal.path.read_bytes()==before
    newer=journal.begin('proxy','2026.10.20.2',scope=scope_for(m))
    assert app_update.reconcile_update(journal,m)['operation_id']==newer['operation_id']
    assert journal.read()['phase']=='preparing'


@pytest.mark.parametrize('ownership',['live_task','queued_task','unknown_task','live_owner','unknown_owner','held_lock','released'])
def test_stale_proxy_requires_proven_task_process_and_lock_release(tmp_path,monkeypatch,ownership):
    import json
    from cachemonitor import observer_task,proxy_identity
    from cachemonitor.observer_state import ProcessLock
    from contextlib import nullcontext
    journal,m,result=linked_proxy(tmp_path)
    result.update(phase='waiting',updated_at=0,worker_owner={'pid':123,'created':1,'executable':'updater.exe'})
    journal.change(result['operation_id'],phase='waiting',proxy=result)
    (m.directory/'proxy-update.json').write_text(json.dumps(result),encoding='utf-8')
    task=dict(registered=True,running=0,state=3)
    if ownership=='live_task':task.update(running=1,state=4)
    elif ownership=='queued_task':task.update(state=2)
    elif ownership=='unknown_task':task={}
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:dict(task))
    def same(owner):
        if ownership=='unknown_owner':raise OSError('identity unavailable')
        return ownership=='live_owner'
    monkeypatch.setattr(proxy_identity,'same_process',same)
    guard=ProcessLock(m.directory/'proxy-update.lock') if ownership=='held_lock' else nullcontext()
    with guard:
        recovered=app_update.reconcile_update(journal,m)
    assert recovered['phase']==('interrupted' if ownership=='released' else 'waiting')
    saved=json.loads((m.directory/'proxy-update.json').read_text(encoding='utf-8'))
    assert saved['phase']=='waiting'
    assert bool(saved.get('owner_interrupted'))==(ownership=='released')


def test_owner_recovery_preserves_cancellation_published_before_journal_lock(tmp_path,monkeypatch):
    import json
    from contextlib import contextmanager
    from cachemonitor import observer_task,proxy_update
    from cachemonitor.observer_state import ProcessLock
    journal,m,record=linked_proxy(tmp_path)
    record.update(phase='waiting',updated_at=0)
    path=m.directory/'proxy-update.json'
    path.write_text(json.dumps(record),encoding='utf-8')
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:{'registered':False})
    injected=False
    @contextmanager
    def locking(path,**kwargs):
        nonlocal injected
        if Path(path).name=='proxy-update-journal.lock' and not injected:
            injected=True
            proxy_update.ProxyUpdate(m).publish('waiting',cancel_requested=True)
        with ProcessLock(path,**kwargs):
            yield
    monkeypatch.setattr(proxy_update,'ProcessLock',locking)
    result=proxy_update.reconcile_owner(m,record)
    assert injected and result['cancel_requested'] and result['phase']=='waiting'
    assert json.loads(path.read_text(encoding='utf-8'))==result


def test_stale_download_keeps_active_worker_lock_and_unknown_owner(tmp_path,monkeypatch):
    import json
    from cachemonitor import proxy_identity
    from cachemonitor.observer_state import ProcessLock
    journal=UpdateState(tmp_path);op=journal.begin('app','fixture')['operation_id']
    record=journal.read();record.update(phase='downloading',updated_at=0)
    journal.path.write_text(json.dumps(record),encoding='utf-8')
    monkeypatch.setattr(proxy_identity,'same_process',lambda owner:False)
    with ProcessLock(tmp_path/'app-update-worker.lock'):
        assert app_update.reconcile_update(journal)['phase']=='downloading'
    def unknown(owner):raise OSError('Cannot prove process exit')
    monkeypatch.setattr(proxy_identity,'same_process',unknown)
    assert app_update.reconcile_update(journal)['phase']=='downloading'


def test_stale_proxy_status_is_read_only_and_does_not_probe_scheduler(tmp_path,monkeypatch):
    import json
    from cachemonitor.observer_control import ObserverManager
    from cachemonitor import observer_task
    m=ObserverManager(tmp_path/'home',tmp_path/'data')
    m.directory.mkdir(parents=True,exist_ok=True)
    record=dict(phase='waiting',updated_at=0)
    path=m.directory/'proxy-update.json';path.write_text(json.dumps(record),encoding='utf-8')
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:pytest.fail('UI read probed scheduler'))
    assert m.update_status()==record
    assert json.loads(path.read_text(encoding='utf-8'))==record


def test_recovered_installation_cannot_reset_terminal_or_newer_journal(tmp_path):
    journal,m,result=linked_proxy(tmp_path);record=journal.read();op=record['operation_id']
    app=record['app'];manifest=dict(version=app['version'],sha256=app['sha256'],commit=app['commit'])
    journal.proxy_result(op,{**result,'phase':'complete','updated_at':2})
    before=journal.path.read_bytes()
    assert journal.installed(op, Path(app['executable']).parent,manifest,
                             scope=record['scope'],recovered=True)==op
    assert journal.path.read_bytes()==before
    newer=journal.begin('proxy','another',scope=record['scope'])
    assert journal.installed(op,Path(app['executable']).parent,manifest,
                             scope=record['scope'],recovered=True) is None
    assert journal.read()['operation_id']==newer['operation_id']


@pytest.mark.parametrize('continuation',['terminal','current_gui','older_gui'])
def test_installer_reconciliation_composes_with_proxy_result_or_action(tmp_path,monkeypatch,continuation):
    import json,sys
    from cachemonitor import install_management,observer_task
    journal,m,result=linked_proxy(tmp_path)
    record=journal.read();app=record['app'];op=record['operation_id']
    manifest=dict(version=app['version'],sha256=app['sha256'],commit=app['commit'])
    record.update(phase='installing',app={'state':'pending'},updated_at=0)
    journal.path.write_text(json.dumps(record),encoding='utf-8')
    if continuation=='terminal':
        (m.directory/'proxy-update.json').write_text(json.dumps({**result,'phase':'complete','updated_at':2}),encoding='utf-8')
    calls=[]
    def recover(journal,record):
        return journal.installed(op,Path(app['executable']).parent,manifest,scope=record['scope'],recovered=True)
    monkeypatch.setattr(install_management,'recover_installed',recover,raising=False)
    monkeypatch.setattr(install_management,'activate_proxy',lambda *a,**k:calls.append(k) or dict(phase='off'))
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:dict(registered=False))
    monkeypatch.setattr(sys,'frozen',continuation=='current_gui',raising=False)
    if continuation=='current_gui':monkeypatch.setattr(sys,'executable',app['executable'])
    recovered=app_update.reconcile_update(journal,m)
    assert recovered['app']==app
    assert recovered['phase']==('partial' if continuation=='older_gui' else 'complete')
    assert len(calls)==int(continuation=='current_gui')


@pytest.mark.parametrize('mismatch',[None,'pointer','hash','scope','pending_activation'])
def test_new_gui_continuation_requires_verified_journal_pointer_and_payload(tmp_path,monkeypatch,mismatch):
    import json
    journal,m,result=linked_proxy(tmp_path);record=journal.read();op=record['operation_id']
    product=Path(record['app']['executable']).parent;product.mkdir(parents=True)
    executable=product/'Codexon.exe';executable.write_bytes(b'verified executable')
    recovery=journal.root/'maintenance'/'new'/'CodexonRecovery.exe';recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b'recovery')
    sha=hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest=dict(product='Codexon',version=record['target_version'],commit='b'*40,
                  sha256=sha,recovery_sha256=hashlib.sha256(recovery.read_bytes()).hexdigest())
    (product/'build-manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    journal.change(op,phase='partial',app={**record['app'],'sha256':sha})
    pointer=dict(InstallRoot=str(journal.root),AppPath=str(executable),RecoveryPath=str(recovery))
    if mismatch=='pointer':pointer['AppPath']=str(product/'other.exe')
    elif mismatch=='hash':executable.write_bytes(b'changed')
    elif mismatch=='scope':journal.change(op,scope={'url':'other'})
    elif mismatch=='pending_activation':(journal.root/'activation-pending.json').write_text('{}')
    monkeypatch.setattr(app_update,'installed',lambda:pointer)
    if mismatch:
        with pytest.raises(RuntimeError):app_update.continuation_target(journal,m)
    else:
        assert app_update.continuation_target(journal,m)==dict(kind='continue_app',executable=str(executable),verified_operation_id=op)


@pytest.mark.parametrize('action',['resume','old_gui','cancel','defer'])
def test_verified_install_waiting_without_dispatch_resumes_or_settles(tmp_path,monkeypatch,action):
    import json,sys
    from cachemonitor import install_management,observer_task,proxy_identity
    journal,m,result=linked_proxy(tmp_path);record=journal.read();op=record['operation_id'];app=record['app']
    record.update(phase='waiting',proxy={'state':'pending'},updated_at=0,
                  installer_owner={'pid':77,'created':1,'executable':'finished-installer.exe'})
    journal.path.write_text(json.dumps(record),encoding='utf-8')
    if action in ('cancel','defer'):assert journal.cancel(later=action=='defer')
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:{'registered':False})
    monkeypatch.setattr(proxy_identity,'same_process',lambda owner:False)
    monkeypatch.setattr(sys,'frozen',action=='resume',raising=False)
    if action=='resume':monkeypatch.setattr(sys,'executable',app['executable'])
    calls=[]
    monkeypatch.setattr(install_management,'activate_proxy',lambda *a,**k:calls.append(k) or dict(phase='off'))
    recovered=app_update.reconcile_update(journal,m)
    expected='complete' if action=='resume' else 'partial' if action=='old_gui' else 'deferred'
    assert recovered['phase']==expected and recovered['app']==app and recovered['operation_id']==op
    assert not recovered['cancel_requested']
    assert len(calls)==int(action=='resume')
    assert not (m.directory/'proxy-update.json').exists()
    assert app_update.reconcile_update(journal,m)==recovered
    assert len(calls)==int(action=='resume')


@pytest.mark.parametrize('lock_name',['app-update-worker.lock','install.lock','proxy-update.lock'])
def test_orphan_installed_wait_preserves_unknown_lock_owner(tmp_path,monkeypatch,lock_name):
    import json,sys
    from cachemonitor import install_management,observer_task
    from cachemonitor.observer_state import ProcessLock
    journal,m,result=linked_proxy(tmp_path);record=journal.read()
    record.update(phase='waiting',proxy={},updated_at=0)
    journal.path.write_text(json.dumps(record),encoding='utf-8')
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:{'registered':False})
    monkeypatch.setattr(install_management,'activate_proxy',lambda *a,**k:pytest.fail('Unknown owner was replaced'))
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',record['app']['executable'])
    directory=m.directory if lock_name=='proxy-update.lock' else journal.root
    with ProcessLock(directory/lock_name):
        assert app_update.reconcile_update(journal,m)==record
    assert journal.read()==record


def test_retry_preserves_original_source_and_rollback_phase_after_owner_interruption(tmp_path,monkeypatch):
    import json,sys
    from cachemonitor import observer_control,observer_task,proxy_update
    m=observer_control.ObserverManager(tmp_path/'home',tmp_path/'data')
    m.directory.mkdir(parents=True,exist_ok=True)
    source=dict(instance='original-A',command=['A.exe','--model-proxy'],version='old')
    record=dict(phase='rollback',source=source,command=['failed-B.exe','--model-proxy'],
                scope=scope_for(m),cutover_started=True,updated_at=0)
    path=m.directory/'proxy-update.json';path.write_text(json.dumps(record),encoding='utf-8')
    starts=[]
    class Task:
        def __init__(self,*a,**k):pass
        def inspect(self):return {'registered':False}
        def start(self,command,**kwargs):starts.append(command)
    monkeypatch.setattr(observer_task,'ObserverTask',Task)
    monkeypatch.setattr(observer_control,'ObserverTask',Task)
    interrupted=proxy_update.reconcile_owner(m,record)
    assert interrupted['phase']=='interrupted' and interrupted['resume_phase']=='rollback'
    persisted=json.loads(path.read_text(encoding='utf-8'))
    assert persisted['phase']=='rollback' and persisted['source']==source and persisted['owner_interrupted']
    monkeypatch.setattr(m,'health',lambda **k:dict(instance='failed-B',draining=False))
    monkeypatch.setattr(m,'state',lambda:dict(enabled=True,upstream='chatgpt'))
    monkeypatch.setattr(m,'config',lambda:(None,{'openai_base_url':m.url}))
    monkeypatch.setattr(m,'status',lambda:dict(update=m.update_status()))
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    m.update_proxy(operation_id='a'*32,update_root=tmp_path/'installed')
    resumed=json.loads(path.read_text(encoding='utf-8'))
    assert len(starts)==1 and resumed['phase']=='rollback' and resumed['source']==source
    assert resumed['command']==record['command'] and resumed['rollback_started']
    assert not resumed['owner_interrupted']


def test_restart_during_rollback_drain_restores_original_command(tmp_path,monkeypatch):
    import json
    from tests.test_proxy_update import Manager,Target
    from cachemonitor import observer_task,proxy_update
    m=Manager(tmp_path);m.home=tmp_path/'home'
    now=[0.]
    updater=proxy_update.ProxyUpdate(m,target=Target(m),clock=lambda:now[0],sleep=lambda n:now.__setitem__(0,now[0]+n))
    monkeypatch.setattr(updater,'preflight',lambda:{'sha256':'verified'})
    source=updater.target.capture(m.health());command=updater.target.replacement(source)
    updater.publish('rollback',source=source,command=command,cutover_started=True)
    updater.publish('stopping')
    record=json.loads(updater.path.read_text(encoding='utf-8'));record['updated_at']=0
    updater.path.write_text(json.dumps(record),encoding='utf-8')
    m.version=None
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:{'registered':False})
    interrupted=proxy_update.reconcile_owner(m,record)
    assert interrupted['phase']=='interrupted' and interrupted['rollback_started']
    updater.replace()
    assert m.task.starts==[(source['command'],False)] and m.version=='old'
    saved=json.loads(updater.path.read_text(encoding='utf-8'))
    assert saved['phase']=='failed' and saved['restored'] and saved['source']==source


def test_explicit_new_gui_intent_retries_exact_operation_once(tmp_path,monkeypatch):
    import sys
    from cachemonitor import install_management
    journal,m,result=linked_proxy(tmp_path);record=journal.read();op=record['operation_id']
    journal.change(op,phase='deferred')
    target=record['app']['executable'];calls=[]
    monkeypatch.setattr(app_update,'continuation_target',lambda *a:dict(executable=target,verified_operation_id=op))
    monkeypatch.setattr(sys,'frozen',True,raising=False);monkeypatch.setattr(sys,'executable',target)
    monkeypatch.setattr(install_management,'activate_proxy',lambda *a,**k:calls.append(k) or dict(phase='off'))
    with pytest.raises(RuntimeError):app_update.retry_proxy(m,journal,verified_operation_id='f'*32)
    assert not calls and journal.read()['phase']=='deferred'
    assert app_update.retry_proxy(m,journal,verified_operation_id=op)['phase']=='complete'
    with pytest.raises(RuntimeError):app_update.retry_proxy(m,journal,verified_operation_id=op)
    assert len(calls)==1
