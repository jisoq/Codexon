import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cachemonitor import install_cleanup as cleanup, install_tasks, install_management
from cachemonitor.observer_state import read_json, ProcessLock
from cachemonitor.observer_task import ObserverTask


@pytest.fixture
def installation(tmp_path,monkeypatch):
    root=tmp_path/'installed';root.mkdir()
    records=[]
    for version in ('old','middle','current'):
        product=root/'versions'/version/'Codexon';product.mkdir(parents=True)
        for name in ('Codexon.exe','CodexonHook.exe'):(product/name).write_bytes(b'payload')
        recovery=root/'maintenance'/version/'CodexonRecovery.exe';recovery.parent.mkdir(parents=True)
        recovery.write_bytes(b'recovery')
        record=dict(product=str(product),recovery=str(recovery),version=version)
        records.append(record)
        (root/('installation.json' if version=='current' else f'installation-{version}.json')).write_text(json.dumps(record))
    home=tmp_path/'home';home.mkdir()
    services=SimpleNamespace(homes=[str(home)],index=tmp_path/'index.sqlite',target=None,collection=True)
    monkeypatch.setattr(install_management,'processes_under',lambda root:[])
    monkeypatch.setattr(install_tasks,'inventory',lambda:[])
    monkeypatch.setattr(cleanup,'external_references',lambda homes:[])
    return root,records,services


def run(fixture):
    root,records,services=fixture
    return cleanup.cleanup(root,Path(records[-1]['product'])/'Codexon.exe',services)


def test_cumulative_cleanup_preserves_current_records_and_all_downloads(installation):
    root,records,_=installation
    download=root/'downloads'/('a'*32);download.mkdir(parents=True)
    for name in ('Codexon-Setup.exe','Codexon-Setup.partial','setup.log'):(download/name).write_bytes(b'keep')
    (root/'user.sqlite').write_bytes(b'records')
    # Even a journal from a previous implementation must not authorize downloads.
    (root/'cleanup.json').write_text(json.dumps({'items':[dict(kind='download',path=str(download))]}))
    report=run(installation)
    assert len(report['items'])==4 and all(i['status']=='removed' for i in report['items'])
    assert Path(records[-1]['product']).is_dir() and Path(records[-1]['recovery']).is_file()
    assert (root/'user.sqlite').read_bytes()==b'records'
    assert len(list(download.iterdir()))==3
    assert len(list(root.glob('installation-*.json')))==2


def test_process_and_hook_references_defer_without_stopping(installation,monkeypatch):
    root,records,_=installation
    exe=str(Path(records[0]['product'])/'Codexon.exe')
    monkeypatch.setattr(install_management,'processes_under',lambda root:[dict(ExecutablePath=exe)])
    hook=str(Path(records[1]['product'])/'CodexonHook.exe')
    monkeypatch.setattr(cleanup,'external_references',lambda homes:[json.dumps({'command':hook})])
    report=run(installation)
    assert Path(records[0]['product']).exists() and Path(records[1]['product']).exists()
    assert {i['reason'] for i in report['items']} >= {'process-running','startup-or-hook-reference'}
    monkeypatch.setattr(install_management,'processes_under',lambda root:[])
    monkeypatch.setattr(cleanup,'external_references',lambda homes:[])
    assert all(i['status']=='removed' for i in run(installation)['items'])


def test_failed_inspection_and_pending_activation_preserve_payload(installation,monkeypatch):
    root,records,_=installation
    monkeypatch.setattr(install_tasks,'inventory',lambda:(_ for _ in ()).throw(RuntimeError('unavailable')))
    assert run(installation)['error']=='unavailable'
    assert all(Path(r['product']).exists() for r in records)
    (root/'activation-pending.json').write_text('{}')
    with pytest.raises(RuntimeError,match='activation-pending'):run(installation)
    assert all(Path(r['product']).exists() for r in records)


def test_lock_and_stale_gui_cannot_clean(installation):
    root,records,services=installation
    with ProcessLock(root/'install.lock'):
        with pytest.raises(RuntimeError):run(installation)
    with pytest.raises(RuntimeError,match='not-current'):
        cleanup.cleanup(root,Path(records[0]['product'])/'Codexon.exe',services)
    assert all(Path(r['product']).exists() for r in records)


def test_outside_receipt_never_deleted(installation,tmp_path):
    root,_,_=installation
    outside=tmp_path/'outside';outside.mkdir();(outside/'keep').touch()
    (root/'installation-foreign.json').write_text(json.dumps({'product':str(outside)}))
    report=run(installation)
    assert (outside/'keep').exists()
    assert any(i['reason']=='outside-installation' for i in report['items'])


def test_previous_pointer_is_history_but_does_not_guess_recovery_path(installation):
    root,records,_=installation
    (root/'installation-old.json').unlink()
    current=records[-1];current['previous']=records[0]['product']
    (root/'installation.json').write_text(json.dumps(current))
    run(installation)
    assert not Path(records[0]['product']).exists()
    assert Path(records[0]['recovery']).exists()


@pytest.mark.skipif(os.name!='nt',reason='Windows case-insensitive paths')
def test_history_case_alias_cannot_delete_current_recovery(installation):
    root,records,_=installation
    alias={k:v.replace('current','CURRENT') for k,v in records[-1].items()}
    (root/'installation-alias.json').write_text(json.dumps(alias))
    report=run(installation)
    assert len(report['items'])==4
    assert Path(records[-1]['product']).exists() and Path(records[-1]['recovery']).is_file()


def test_reparse_point_is_not_traversed(installation,monkeypatch):
    root,records,_=installation
    old=Path(records[0]['product'])
    real=cleanup.linked
    monkeypatch.setattr(cleanup,'linked',lambda path:path==old or real(path))
    report=run(installation)
    assert old.exists() and any(i['reason']=='reparse-point' for i in report['items'])


@pytest.mark.skipif(os.name!='nt',reason='Windows file-sharing semantics')
def test_real_file_lock_and_partial_deletion_retry(installation):
    import ctypes
    from ctypes import wintypes as W
    _,records,_=installation
    path=Path(records[0]['product'])/'CodexonHook.exe'
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    api.CreateFileW.restype=W.HANDLE
    api.CloseHandle.argtypes=[W.HANDLE]
    handle=api.CreateFileW(str(path),0x80000000,1,None,3,0,None)
    assert handle!=W.HANDLE(-1).value
    try:
        report=run(installation)
        assert path.exists() and any(i['status']=='deferred' for i in report['items'])
    finally:api.CloseHandle(handle)
    assert all(i['status']=='removed' for i in run(installation)['items'])
    assert not path.exists()


def task(scope,role,exe,args='',**extra):
    owner=ObserverTask(str(scope),role=role)
    return dict(name=owner.name,marker=owner.marker,xml='original',count=1,running=0,state=3,
                actions=[dict(executable=str(exe),arguments=args)],**extra)


def test_owned_idle_task_migrates_and_unknown_or_live_tasks_block(installation,monkeypatch):
    _,records,services=installation
    exe=Path(records[0]['product'])/'Codexon.exe'
    owned=task(services.index,'UsageCollector',exe,
        f'--usage-collector --index-path "{services.index}" --codex-home "{services.homes[0]}"')
    foreign=task('another-scope','UsageCollector',Path(records[1]['product'])/'Codexon.exe')
    tasks=[owned,foreign];changes=[]
    monkeypatch.setattr(install_tasks,'inventory',lambda:list(tasks))
    def change(item,command):
        changes.append(command)
        item['actions']=[dict(executable=command[0],arguments=' '.join(command[1:]))]
    monkeypatch.setattr(install_tasks,'change',change)
    report=run(installation)
    assert len(changes)==1 and changes[0][0]==str(Path(records[-1]['product'])/'Codexon.exe')
    assert changes[0][1:]==['--usage-collector','--index-path',str(services.index),'--codex-home',services.homes[0]]
    assert not exe.exists() and Path(records[1]['product']).exists()
    assert any(i['reason']=='task-reference' for i in report['items'])


def test_completed_desktop_removed_but_running_task_preserved(installation,monkeypatch):
    _,records,_=installation
    exe=Path(records[0]['product'])/'Codexon.exe'
    idle=task(exe,'Desktop',exe,'--replace-gui')
    active_exe=Path(records[1]['product'])/'Codexon.exe'
    active=task(active_exe,'Desktop',active_exe);active['running']=1
    tasks=[idle,active]
    monkeypatch.setattr(install_tasks,'inventory',lambda:list(tasks))
    def remove(item,command):
        assert command is None
        tasks.remove(item)
    monkeypatch.setattr(install_tasks,'change',remove)
    run(installation)
    assert tasks==[active] and not exe.exists() and active_exe.exists()


def test_source_and_unready_gui_cannot_cleanup(monkeypatch):
    import sys
    monkeypatch.setattr(cleanup,'cleanup',lambda *a:pytest.fail('must not delete'))
    monkeypatch.setattr(sys,'frozen',False,raising=False)
    cleanup.after_services(SimpleNamespace())
    monkeypatch.setattr(sys,'frozen',True)
    cleanup.after_services(SimpleNamespace(gui_ready=False))


@pytest.mark.parametrize('phase,restored,ready,expected',[
    ('waiting',False,True,0),('rollback',False,True,0),('failed',False,True,0),
    ('complete',False,False,0),('complete',False,True,1),('failed',True,True,1)])
def test_service_readiness_and_rollback_gate_cleanup_once(installation,monkeypatch,phase,restored,ready,expected):
    import sys,threading
    from cachemonitor import install_dispatch,proxy_identity
    root,records,services=installation
    exe=Path(records[-1]['product'])/'Codexon.exe'
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(exe))
    monkeypatch.setattr(install_dispatch,'packaged_context',lambda:False)
    monkeypatch.setattr(proxy_identity,'deployment',lambda path:{})
    directory=root/'service';directory.mkdir()
    (directory/'proxy-update.json').write_text(json.dumps(dict(phase=phase,restored=restored)))
    services.gui_ready=True;services.closing=threading.Event();services.collection=False
    services.manager=SimpleNamespace(directory=directory,control_lock=directory/'control.lock',health=lambda **kw:{'status':'ok'})
    services.target=SimpleNamespace(enabled=lambda:True,
        capture=lambda h:dict(instance='new',command=[str(exe)]),ready=lambda *args:ready)
    calls=[];monkeypatch.setattr(cleanup,'cleanup',lambda *args:calls.append(args) or {})
    cleanup.after_services(services);cleanup.after_services(services)
    assert len(calls)==expected


def test_hook_and_process_references_are_case_insensitive():
    assert cleanup.referenced('C:/Install/versions/old',r'"c:\INSTALL\versions\OLD\CodexonHook.exe" --cache-hook')
    assert not cleanup.referenced('C:/Install/versions/old',r'C:\Install\versions\older\Codexon.exe')


def test_task_scope_uses_windows_path_case_but_exact_ownership_hash():
    item=task('C:/users/test/.codex','ProxySupervisor','C:/old/Codexon.exe')
    assert install_tasks.owned_role(item,{'C:/Users/Test/.codex'})=='ProxySupervisor'
    item['name']+='-foreign'
    assert install_tasks.owned_role(item,{'C:/Users/Test/.codex'}) is None


@pytest.mark.skipif(os.name!='nt',reason='Windows Task Scheduler')
def test_real_task_retarget_preserves_disabled_state_and_arguments(tmp_path):
    owner=ObserverTask(str(tmp_path/'index.sqlite'),role='UsageCollector')
    command=[str(tmp_path/'old'/'Codexon.exe'),'--usage-collector','--index-path',str(tmp_path/'index.sqlite')]
    try:
        owner.configure(command,False)
        owner.suspend()
        original=next(t for t in install_tasks.inventory() if t['name']==owner.name)
        updated=[str(tmp_path/'new'/'Codexon.exe'),*command[1:]]
        install_tasks.change(original,updated)
        state=owner.inspect()
        assert state['executable']==updated[0] and not state['enabled']
        assert not state['autostart'] and not state['periodic'] and not state['restartCount']
        from cachemonitor.launch_context import command_arguments
        assert command_arguments('worker '+state['arguments'])[1:]==command[1:]
        with pytest.raises(RuntimeError):install_tasks.change(original,None)
        current=next(t for t in install_tasks.inventory() if t['name']==owner.name)
        install_tasks.change(current,None)
        assert not owner.inspect()['registered']
    finally:owner.remove()
