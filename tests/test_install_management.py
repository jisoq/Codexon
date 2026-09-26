import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

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


def test_runtime_failure_keeps_previous_launch_pointer_and_files(tmp_path,monkeypatch):
    product,recovery=fixture(tmp_path)
    old={'product':'old','version':'old'}
    (tmp_path/'installation.json').write_text(json.dumps(old))
    monkeypatch.setattr(install,'register',lambda *a,**k:pytest.fail('Must not activate failed runtime'))
    monkeypatch.setattr(install.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError,match='실행 검사'):install.finish(tmp_path,product,recovery,isolated=True)
    assert json.loads((tmp_path/'installation.json').read_text())==old


def test_success_preserves_previous_receipt_and_unrelated_records(tmp_path,monkeypatch):
    product,recovery=fixture(tmp_path)
    old=tmp_path/'versions'/'old';old.mkdir();(old/'Codexon.exe').write_bytes(b'old')
    (tmp_path/'installation.json').write_text(json.dumps({'product':str(old)}))
    (tmp_path/'record.sqlite').write_bytes(b'user record')
    def run(command,**_):
        Path(command[-1]).write_text(json.dumps({'errors':[],'version':'test'}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(install.subprocess,'run',run)
    calls=[];monkeypatch.setattr(install,'register',lambda *a,**k:calls.append(a))
    install.finish(tmp_path,product,recovery,isolated=True)
    assert calls and (old/'Codexon.exe').read_bytes()==b'old'
    assert (tmp_path/'record.sqlite').read_bytes()==b'user record'
    assert list(tmp_path.glob('installation-*.json'))


def test_uninstall_defers_when_any_payload_process_is_in_use(tmp_path,monkeypatch):
    monkeypatch.setattr(install,'processes_under',lambda root:[dict(ExecutablePath=str(root/'versions'/'Codexon.exe'),ProcessId=42)])
    with pytest.raises(RuntimeError,match='끝난 뒤'):install.prepare_uninstall(tmp_path,isolated=True)


@pytest.mark.parametrize('same_request',[True,False])
def test_native_uninstall_exempts_only_its_waiting_dispatcher(tmp_path,monkeypatch,same_request):
    import sys
    from cachemonitor import observer_task
    exe=tmp_path/'maintenance'/'new'/'CodexonRecovery.exe'
    monkeypatch.setattr(sys,'executable',str(exe))
    root=tmp_path if same_request else tmp_path/'other'
    command=f'"{exe}" --prepare-uninstall --install-root "{root}"'
    monkeypatch.setattr(install,'processes_under',lambda root:[dict(ExecutablePath=str(exe),ProcessId=42,CommandLine=command)])
    monkeypatch.setattr(observer_task,'remove_installation_collectors',lambda root:None)
    if same_request:assert install.prepare_uninstall(tmp_path,isolated=True,caller_pid=42)['ready']
    else:
        with pytest.raises(RuntimeError,match='끝난 뒤'):install.prepare_uninstall(tmp_path,isolated=True,caller_pid=42)


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
def test_cache_launch_paths_survive_home_update(tmp_path):
    from cachemonitor.launch_context import save_cache_paths,cache_paths,save_homes,resolve_homes
    path=tmp_path/'launch.json'
    index=tmp_path/'observation'/'index.sqlite';quota=tmp_path/'original-quota.sqlite';evidence=tmp_path/'evidence.sqlite'
    save_cache_paths(index,evidence,quota,path=path)
    save_homes([tmp_path/'home'],path=path)
    assert cache_paths(path=path)==dict(index_path=str(index),evidence_path=str(evidence),quota_path=str(quota))
    assert resolve_homes(path=path)==[str(tmp_path/'home')]


def test_cache_launch_paths_are_shared_across_msix_localappdata_views(tmp_path,monkeypatch):
    from cachemonitor import launch_context as launch
    monkeypatch.setattr(launch.Path,'home',lambda:tmp_path)
    first=tmp_path/'msix';second=tmp_path/'ordinary'
    monkeypatch.setenv('LOCALAPPDATA',str(first))
    legacy=first/'CacheMonitor'/'launch.json';legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({'homes':['legacy-home']}))
    assert launch.resolve_homes()==['legacy-home']
    launch.save_cache_paths(tmp_path/'index.sqlite',tmp_path/'e.sqlite',tmp_path/'quota.sqlite')
    monkeypatch.setenv('LOCALAPPDATA',str(second))
    assert launch.resolve_homes()==['legacy-home']
    assert launch.cache_paths()['quota_path']==str(tmp_path/'quota.sqlite')


def test_normal_launch_resolves_saved_homes_after_restoring_cache_paths(tmp_path,monkeypatch):
    from cachemonitor import app,launch_context as launch
    import sys
    monkeypatch.setattr(sys,'argv',['Codexon.exe'])
    monkeypatch.setattr(launch,'cache_paths',lambda:dict(index_path=str(tmp_path/'index.sqlite'),quota_path=str(tmp_path/'quota.sqlite')))
    calls=[]
    monkeypatch.setattr(launch,'resolve_homes',lambda explicit:(calls.append(explicit) or [str(tmp_path/'custom home')]))
    class BeforeGUI(Exception):pass
    monkeypatch.setattr(app,'configure_font_rendering',lambda:(_ for _ in ()).throw(BeforeGUI()))
    with pytest.raises(BeforeGUI):app.main()
    assert calls==[None]


def test_uninstall_removes_only_own_installation_hooks_and_uses_saved_worker(tmp_path,monkeypatch):
    from cachemonitor import cache_hooks,launch_context,connection_recovery,installation
    root=tmp_path/'installed';home=tmp_path/'custom home';home.mkdir()
    owned=root/'versions'/'old'/'CodexonHook.exe'
    cache_hooks.configure(home,tmp_path/'cache.sqlite',True,executable=owned)
    path=home/'hooks.json';document=json.loads(path.read_text())
    external={'description':'another hook','hooks':[{'command':'external-tool'}]}
    document['hooks']['UserPromptSubmit'].append(external)
    path.write_text(json.dumps(document));auth=home/'auth.json';auth.write_text('preserve')
    monkeypatch.setattr(launch_context,'cache_paths',lambda:dict(index_path=str(tmp_path/'index.sqlite'),evidence_path=str(tmp_path/'evidence.sqlite')))
    monkeypatch.setattr(launch_context,'resolve_homes',lambda *a:[str(home)])
    from cachemonitor.cache_worker_control import CacheWorkerManager
    assert isinstance(install.connection_manager(),CacheWorkerManager)
    assert install.connection_manager().home==home
    manager=SimpleNamespace(home=home,shared_cache_worker=True,health=lambda **kw:None)
    monkeypatch.setattr(install,'connection_manager',lambda:manager)
    monkeypatch.setattr(installation,'installed',lambda:dict(InstallRoot=str(root)))
    restored=[];monkeypatch.setattr(connection_recovery,'restore',lambda m:restored.append(m))
    monkeypatch.setattr(install,'processes_under',lambda r:[dict(ExecutablePath=str(root/'Codexon.exe'),ProcessId=42)])
    with pytest.raises(RuntimeError,match='끝난 뒤'):install.prepare_uninstall(root)
    assert restored==[manager] and auth.read_text()=='preserve'
    remaining=json.loads(path.read_text())['hooks']
    assert remaining['UserPromptSubmit']==[external] and remaining['Stop']==[]
    assert (home/'hooks.before-codexon-uninstall.json').is_file()
    other=tmp_path/'other'/'CodexonHook.exe'
    cache_hooks.configure(home,tmp_path/'cache.sqlite',True,executable=other)
    before=path.read_bytes();assert not cache_hooks.remove_installation(home,root)
    assert path.read_bytes()==before
