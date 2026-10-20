import hashlib
import json
from pathlib import Path

import pytest

from cachemonitor import install_management as install


@pytest.fixture(autouse=True)
def isolated_shell(tmp_path,monkeypatch):
    from cachemonitor import install_activation as activation
    monkeypatch.setattr(activation,'shortcuts',lambda isolated:[tmp_path/'menu.lnk'])
    monkeypatch.setattr(activation,'snapshot_registry',lambda isolated:[])
    monkeypatch.setattr(activation,'restore_registry',lambda values:None)
    monkeypatch.setattr(activation,'publish_shell',lambda *a,**k:None)
    monkeypatch.setattr(activation,'remove_shortcuts',lambda *a:None)


def fixture(root):
    product=root/'versions'/'new'/'Codexon';product.mkdir(parents=True)
    recovery=root/'maintenance'/'new'/'CodexonRecovery.exe';recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b'recovery')
    (product/'Codexon.exe').write_bytes(b'new')
    (product/'build-manifest.json').write_text(json.dumps(dict(product='Codexon',version='test',commit='a'*40,
        sha256=hashlib.sha256(b'new').hexdigest(),recovery_sha256=hashlib.sha256(b'recovery').hexdigest())))
    return product,recovery


@pytest.fixture
def committed_installation(tmp_path,monkeypatch,request):
    """Crash on either side of the verified installation journal write."""
    import subprocess
    from cachemonitor import install_cleanup, installation, launch_context, observer_task
    from cachemonitor.update_state import UpdateState
    product,recovery=fixture(tmp_path)
    scope=dict(home=str(tmp_path/'custom home'),evidence=str(tmp_path/'custom evidence.sqlite'),
               url='http://127.0.0.1:19876',index=None)
    journal=UpdateState(tmp_path)
    record=journal.begin('app','test',scope=scope)
    journal.change(record['operation_id'],phase='installing')
    record=journal.read()
    monkeypatch.setattr(install,'register',lambda *a,**k:None)
    monkeypatch.setattr(install,'migrate_startup',lambda *a:None)
    monkeypatch.setattr(installation,'pointer_path',lambda:tmp_path/'shared-installation.json')
    monkeypatch.setattr(launch_context,'preference_path',lambda:tmp_path/'launch.json')
    monkeypatch.setattr(launch_context,'running_homes',lambda root:[])
    monkeypatch.setattr(launch_context,'resolve_homes',lambda homes:[scope['home']])
    monkeypatch.setattr(observer_task,'retire_desktop_startups',lambda root:None)
    after_installed=getattr(request,'param',False)
    class InstallerCrash(BaseException):pass
    def verify(command,**kwargs):
        if '--complete-install' in command:raise InstallerCrash()
        Path(command[-1]).write_text(json.dumps(dict(errors=[],version='test')))
        return subprocess.CompletedProcess(command,0)
    monkeypatch.setattr(install.subprocess,'run',verify)
    def crash(root):raise InstallerCrash()
    monkeypatch.setattr(install_cleanup,'queue',(lambda root:None) if after_installed else crash)
    with pytest.raises(InstallerCrash):
        install.finish(tmp_path,product,recovery,launch=False,update_id=record['operation_id'])
    assert not (tmp_path/'activation-pending.json').exists()
    assert journal.read()['phase']==('waiting' if after_installed else 'installing')
    assert json.loads((tmp_path/'installation.json').read_text())['product']==str(product)
    return journal,record,product,recovery


def test_committed_installation_recovers_after_result_writer_crash(committed_installation):
    journal,record,product,recovery=committed_installation
    assert install.recover_installed(journal,record)==record['operation_id']
    result=journal.read()
    assert result['phase']=='waiting' and result['app']['state']=='verified'
    assert result['scope']==record['scope']
    assert result['app']['executable']==str(product/'Codexon.exe')
    journal.change(record['operation_id'],phase='complete')
    completed=journal.read()
    assert install.recover_installed(journal,record)==record['operation_id']
    assert journal.read()==completed


@pytest.mark.parametrize('committed_installation',[True],indirect=True)
def test_installed_journal_without_proxy_dispatch_exposes_continuation(committed_installation,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor import app_update,observer_task,proxy_identity
    journal,record,product,recovery=committed_installation
    waiting=journal.read();waiting['updated_at']=0
    journal.path.write_text(json.dumps(waiting),encoding='utf-8')
    scope=record['scope']
    manager=SimpleNamespace(home=Path(scope['home']),evidence=Path(scope['evidence']),
                            directory=journal.root/'observer',url=scope['url'])
    monkeypatch.setattr(observer_task.ObserverTask,'inspect',lambda self:{'registered':False})
    monkeypatch.setattr(proxy_identity,'same_process',lambda owner:False)
    result=app_update.reconcile_update(journal,manager)
    assert result['phase']=='partial' and result['app']==waiting['app']
    assert result['operation_id']==record['operation_id']
    assert not (manager.directory/'proxy-update.json').exists()


@pytest.mark.parametrize('changed',[
    'operation','scope','version','manifest','binary','recovery','pending','pointer','busy'])
def test_installation_recovery_requires_matching_committed_proof(committed_installation,changed):
    from contextlib import nullcontext
    from cachemonitor.observer_state import ProcessLock
    journal,record,product,recovery=committed_installation
    receipt_path=journal.root/'installation.json'
    receipt=json.loads(receipt_path.read_text())
    if changed=='operation':receipt['operation_id']='f'*32
    elif changed=='scope':receipt['scope']={**record['scope'],'home':str(journal.root/'other home')}
    elif changed=='version':receipt['version']='other'
    elif changed=='manifest':
        path=product/'build-manifest.json'
        manifest=json.loads(path.read_text());manifest['commit']='b'*40
        path.write_text(json.dumps(manifest))
    elif changed=='binary':(product/'Codexon.exe').write_bytes(b'changed')
    elif changed=='recovery':recovery.write_bytes(b'changed')
    elif changed=='pending':(journal.root/'activation-pending.json').write_text('{}')
    elif changed=='pointer':
        (journal.root/'shared-installation.json').write_text('{}')
    receipt_path.write_text(json.dumps(receipt))
    before=journal.read()
    guard=ProcessLock(journal.root/'install.lock') if changed=='busy' else nullcontext()
    with guard:
        assert install.recover_installed(journal,record) is None
    assert journal.read()==before


@pytest.mark.parametrize('same_request',[True,False])
@pytest.mark.parametrize('callers',[[42],[42,43]])
def test_native_uninstall_exempts_only_its_waiting_dispatcher(tmp_path,monkeypatch,same_request,callers):
    import sys
    from cachemonitor import observer_task
    exe=tmp_path/'maintenance'/'new'/'CodexonRecovery.exe'
    monkeypatch.setattr(sys,'executable',str(exe))
    root=tmp_path if same_request else tmp_path/'other'
    command=f'"{exe}" --prepare-uninstall --install-root "{root}"'
    monkeypatch.setattr(install,'processes_under',lambda root:[dict(ExecutablePath=str(exe),ProcessId=pid,CommandLine=command) for pid in callers])
    monkeypatch.setattr(observer_task,'remove_installation_collectors',lambda root:None)
    if same_request:assert install.prepare_uninstall(tmp_path,isolated=True,caller_pid=callers)['ready']
    else:
        with pytest.raises(RuntimeError,match='끝난 뒤'):install.prepare_uninstall(tmp_path,isolated=True,caller_pid=callers)


@pytest.mark.parametrize('busy',[False,True])
def test_uninstall_stops_only_collector_after_other_components_exit(tmp_path,monkeypatch,busy):
    from cachemonitor import observer_task
    index=tmp_path/'data with spaces'/'index.sqlite';index.parent.mkdir();index.write_bytes(b'preserve records')
    exe=tmp_path/'versions'/'Codexon.exe'
    processes=[dict(ExecutablePath=str(exe),ProcessId=42,
        CommandLine=f'"{exe}" --usage-collector --index-path "{index}"')]
    if busy:processes.append(dict(ExecutablePath=str(exe),ProcessId=43,CommandLine=f'"{exe}" --model-proxy'))
    monkeypatch.setattr(install,'processes_under',lambda root:list(processes))
    calls=[]
    class Task:
        def __init__(self,scope,role):calls.append((scope,role))
        def stop(self):processes.clear();calls.append('stop')
        def remove(self):calls.append('remove')
    monkeypatch.setattr(observer_task,'ObserverTask',Task)
    if busy:
        with pytest.raises(RuntimeError,match='끝난 뒤'):install.prepare_uninstall(tmp_path,isolated=True)
        assert not calls
    else:
        assert install.prepare_uninstall(tmp_path,isolated=True)['ready']
        assert calls==[(str(index),'UsageCollector'),'stop','remove']
    assert index.read_bytes()==b'preserve records'


@pytest.mark.integration
def test_uninstall_removes_idle_owned_collector_task_only(tmp_path,monkeypatch):
    import sys
    if sys.platform!='win32':pytest.skip('Windows task definitions')
    from cachemonitor.observer_task import ObserverTask
    root=tmp_path/'installed'
    owned=ObserverTask(str(tmp_path/'owned-index.sqlite'),role='UsageCollector')
    foreign=ObserverTask(str(tmp_path/'other-index.sqlite'),role='UsageCollector')
    try:
        owned.configure([str(root/'versions'/'old'/'Codexon.exe'),'--usage-collector','--index-path',str(tmp_path/'owned-index.sqlite')],False)
        foreign.configure([str(tmp_path/'another-install'/'Codexon.exe'),'--usage-collector','--index-path',str(tmp_path/'other-index.sqlite')],False)
        monkeypatch.setattr(install,'processes_under',lambda root:[])
        assert owned.inspect()['registered'] and foreign.inspect()['registered']
        assert install.prepare_uninstall(root,isolated=True)['ready']
        assert not owned.inspect()['registered'] and foreign.inspect()['registered']
    finally:owned.remove();foreign.remove()


def test_product_cannot_escape_installation_root(tmp_path):
    with pytest.raises(ValueError):install.contained(tmp_path/'outside',tmp_path/'versions')


def test_login_startup_preserves_custom_home_and_disabled_or_unrelated_values():
    command='"C:\\old folder\\CacheMonitor.exe" --hidden --codex-home "C:\\custom home"'
    changed=install.startup_replacement(command,Path('C:/new/Codexon.exe'))
    assert changed.endswith('--hidden --codex-home "C:\\custom home"')
    assert 'old folder' not in changed
    for unrelated in ('', '"C:\\pythonw.exe" run.py --hidden'):
        assert install.startup_replacement(unrelated,Path('C:/new/Codexon.exe'))==unrelated


def test_chatgpt_startup_migrates_existing_command_without_losing_paths(monkeypatch):
    import sys
    from types import SimpleNamespace
    from cachemonitor.tray import TrayWindow
    command='"D:\\custom folder\\Codexon.exe" --hidden --codex-home "E:\\my --windows-startup home" --index-path "E:\\data\\index.sqlite"'
    stored={'command':command}
    class Key:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    registry=SimpleNamespace(HKEY_CURRENT_USER=1,REG_SZ=1,CreateKey=lambda *args:Key(),
        QueryValueEx=lambda *args:(stored['command'],1),
        SetValueEx=lambda *args:stored.update(command=args[-1]))
    monkeypatch.setitem(sys.modules,'winreg',registry)
    assert TrayWindow.set_startup(SimpleNamespace(),True,preserve_existing=True)
    assert stored['command']==command+' --windows-startup'
    assert TrayWindow.set_startup(SimpleNamespace(),True,preserve_existing=True)
    assert stored['command']==command+' --windows-startup'


def test_login_startup_command_carries_marker_and_custom_homes(monkeypatch):
    import sys
    from types import SimpleNamespace
    from cachemonitor.tray import TrayWindow
    stored={}
    class Key:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    registry=SimpleNamespace(HKEY_CURRENT_USER=1,REG_SZ=1,CreateKey=lambda *args:Key(),
        SetValueEx=lambda *args:stored.update(command=args[-1]))
    monkeypatch.setitem(sys.modules,'winreg',registry)
    owner=SimpleNamespace(snapshot={'homes':['E:\\my home','F:\\other home']})
    assert TrayWindow.set_startup(owner,True)
    assert '--windows-startup' in stored['command']
    assert '--codex-home "E:\\my home" --codex-home "F:\\other home"' in stored['command']


def test_connection_launch_paths_survive_home_update(tmp_path):
    from cachemonitor.launch_context import save_connection_paths,connection_paths,save_homes,resolve_homes
    path=tmp_path/'launch.json'
    index=tmp_path/'observation'/'index.sqlite';quota=tmp_path/'original-quota.sqlite';evidence=tmp_path/'evidence.sqlite'
    save_connection_paths(index,evidence,quota,path=path)
    save_homes([tmp_path/'home'],path=path)
    assert connection_paths(path=path)==dict(index_path=str(index),evidence_path=str(evidence),quota_path=str(quota))
    assert resolve_homes(path=path)==[str(tmp_path/'home')]


def test_connection_launch_paths_are_shared_across_msix_localappdata_views(tmp_path,monkeypatch):
    from cachemonitor import launch_context as launch
    monkeypatch.setattr(launch.Path,'home',lambda:tmp_path)
    first=tmp_path/'msix';second=tmp_path/'ordinary'
    monkeypatch.setenv('LOCALAPPDATA',str(first))
    legacy=first/'CacheMonitor'/'launch.json';legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({'homes':['legacy-home']}))
    assert launch.resolve_homes()==['legacy-home']
    launch.save_connection_paths(tmp_path/'index.sqlite',tmp_path/'e.sqlite',tmp_path/'quota.sqlite')
    monkeypatch.setenv('LOCALAPPDATA',str(second))
    assert launch.resolve_homes()==['legacy-home']
    assert launch.connection_paths()['quota_path']==str(tmp_path/'quota.sqlite')


def test_normal_launch_resolves_saved_homes_after_restoring_connection_paths(tmp_path,monkeypatch):
    from cachemonitor import app,launch_context as launch
    import sys
    monkeypatch.setattr(sys,'argv',['Codexon.exe'])
    monkeypatch.setattr(launch,'connection_paths',lambda:dict(index_path=str(tmp_path/'index.sqlite'),quota_path=str(tmp_path/'quota.sqlite')))
    calls=[]
    monkeypatch.setattr(launch,'resolve_homes',lambda explicit:(calls.append(explicit) or [str(tmp_path/'custom home')]))
    class BeforeGUI(Exception):pass
    monkeypatch.setattr(app,'configure_font_rendering',lambda:(_ for _ in ()).throw(BeforeGUI()))
    with pytest.raises(BeforeGUI):app.main()
    assert calls==[None]


def test_restart_keeps_custom_paths_and_service_ownership(tmp_path,monkeypatch):
    from cachemonitor import app_restart
    captured=[]
    monkeypatch.setattr(app_restart.subprocess,'Popen',lambda command,**kwargs:captured.append(command))
    app_restart.launch_replacement([tmp_path/'home'],index_path=tmp_path/'index.sqlite',
        evidence_path=tmp_path/'evidence.sqlite',managed_services=True)
    assert '--managed-services' in captured[0] and '--cache-control' not in captured[0]
    assert str(tmp_path/'index.sqlite') in captured[0] and str(tmp_path/'evidence.sqlite') in captured[0]


def test_managed_restart_resolves_saved_homes_with_explicit_index(tmp_path,monkeypatch):
    from cachemonitor import app,launch_context
    import sys
    monkeypatch.setattr(sys,'argv',['Codexon.exe','--managed-services','--index-path',str(tmp_path/'index.sqlite')])
    calls=[]
    monkeypatch.setattr(launch_context,'resolve_homes',lambda explicit:calls.append(explicit) or [str(tmp_path/'custom home')])
    class BeforeGUI(Exception):pass
    monkeypatch.setattr(app,'configure_font_rendering',lambda:(_ for _ in ()).throw(BeforeGUI()))
    with pytest.raises(BeforeGUI):app.main()
    assert calls==[None]
@pytest.mark.parametrize('reply,expected',[(b'ready-to-exit',True),(b'busy',False),(b'ready-',False)])
def test_gui_handoff_reads_an_already_buffered_reply(reply,expected):
    import uuid
    from PySide6.QtWidgets import QApplication
    from PySide6.QtNetwork import QLocalServer,QLocalSocket
    from cachemonitor.app_restart import handoff_ready
    app=QApplication.instance() or QApplication([])
    server=QLocalServer();client=QLocalSocket();peer=None
    try:
        assert server.listen('codexon-handoff-test-'+uuid.uuid4().hex)
        client.connectToServer(server.serverName())
        assert client.waitForConnected(1000)
        server.waitForNewConnection(1000);peer=server.nextPendingConnection()
        assert peer is not None
        peer.write(reply);peer.waitForBytesWritten(1000)
        assert client.bytesAvailable() or client.waitForReadyRead(1000)
        assert handoff_ready(client,timeout=20) is expected
    finally:
        client.close()
        if peer is not None:peer.close()
        server.close()

