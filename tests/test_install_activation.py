import json
from pathlib import Path
import subprocess
import sys

import pytest

from cachemonitor import install_activation as activation, install_management as install
from cachemonitor.launch_context import resolve_homes, save_homes, homes_from_command
from test_install_management import fixture


@pytest.mark.parametrize('phase',['shell','receipt'])
def test_failed_activation_restores_all_launch_paths_and_receipt(tmp_path,monkeypatch,phase):
    product,recovery=fixture(tmp_path)
    receipt=tmp_path/'installation.json';receipt.write_bytes(b'{"product":"old"}')
    link=tmp_path/'menu.lnk';link.write_bytes(b'old shortcut')
    registry={'app':'old','recovery':'old recovery'}
    monkeypatch.setattr(activation,'shortcuts',lambda isolated:[link])
    monkeypatch.setattr(activation,'snapshot_registry',lambda isolated:dict(registry))
    monkeypatch.setattr(activation,'restore_registry',lambda values:(registry.clear(),registry.update(values)))
    monkeypatch.setattr(install,'register',lambda *a,**k:registry.update(app='new',recovery='new recovery'))
    def run(command,**kwargs):
        Path(command[-1]).write_text(json.dumps(dict(errors=[],version='test')))
        return subprocess.CompletedProcess(command,0)
    monkeypatch.setattr(install.subprocess,'run',run)
    def shell(*a,**k):
        link.write_bytes(b'new shortcut')
        if phase=='shell':raise OSError('injected shell failure')
    monkeypatch.setattr(activation,'publish_shell',shell)
    original=install.atomic_write
    def write(path,data):
        if path==receipt:raise OSError('injected receipt failure')
        original(path,data)
    if phase=='receipt':monkeypatch.setattr(install,'atomic_write',write)
    with pytest.raises(OSError,match='injected'):install.finish(tmp_path,product,recovery,isolated=True)
    assert registry=={'app':'old','recovery':'old recovery'}
    assert receipt.read_bytes()==b'{"product":"old"}' and link.read_bytes()==b'old shortcut'
    assert not (tmp_path/'activation-pending.json').exists()


def test_interrupted_activation_is_recovered_before_next_attempt(tmp_path,monkeypatch):
    link=tmp_path/'menu.lnk';link.write_bytes(b'old')
    registry={'app':'old'}
    monkeypatch.setattr(activation,'shortcuts',lambda isolated:[link])
    monkeypatch.setattr(activation,'snapshot_registry',lambda isolated:dict(registry))
    monkeypatch.setattr(activation,'restore_registry',lambda values:(registry.clear(),registry.update(values)))
    activation.Activation(tmp_path,True)
    link.write_bytes(b'new');registry['app']='new'
    transaction=activation.Activation(tmp_path,True)
    assert link.read_bytes()==b'old' and registry=={'app':'old'}
    transaction.commit()


@pytest.mark.parametrize('startup,fail_write',[
    (None,False),('',False),('"C:\\Python\\pythonw.exe" run.py --hidden',False),
    ('"C:\\old folder\\Codexon.exe" --hidden --codex-home "C:\\first home" --codex-home C:\\second',False),
    ('"C:\\old folder\\Codexon.exe" --hidden --codex-home "C:\\first home" --codex-home C:\\second',True),
])
@pytest.mark.skipif(sys.platform != 'win32', reason='Windows registry activation contract')
def test_update_retargets_login_startup_and_rolls_back_unconfirmed_write(tmp_path,monkeypatch,startup,fail_write):
    """Exercise the production activation path using disposable real registry keys."""
    import uuid
    import winreg
    from cachemonitor import launch_context
    key_path=r'Software\Codexon-Test-'+uuid.uuid4().hex
    startup_path=key_path+r'\Run'
    approved_path=key_path+r'\StartupApproved'
    product,recovery=fixture(tmp_path)
    receipt=tmp_path/'installation.json';receipt.write_bytes(b'{"product":"old"}')
    link=tmp_path/'menu.lnk';link.write_bytes(b'old shortcut')
    monkeypatch.setattr(install,'KEY',key_path)
    monkeypatch.setattr(install,'STARTUP_KEY',startup_path)
    slots=[(key_path,name) for name in ('InstallRoot','AppPath','RecoveryPath')]+[(startup_path,'CacheMonitor')]
    monkeypatch.setattr(activation,'registry_slots',lambda isolated:slots)
    monkeypatch.setattr(activation,'shortcuts',lambda isolated:[link])
    monkeypatch.setattr(activation,'publish_shell',lambda *a,**k:link.write_bytes(b'new shortcut'))
    monkeypatch.setattr(launch_context,'preference_path',lambda:tmp_path/'launch.json')
    monkeypatch.setattr(launch_context,'running_homes',lambda root:[])
    from cachemonitor import observer_task
    retired=[]
    monkeypatch.setattr(observer_task,'retire_desktop_startups',lambda root:retired.append(root))
    def run(command,**kwargs):
        Path(command[-1]).write_text(json.dumps(dict(errors=[],version='test',phase='off')))
        return subprocess.CompletedProcess(command,0)
    monkeypatch.setattr(install.subprocess,'run',run)
    original_write=winreg.SetValueEx
    def write(key,name,reserved,kind,value):
        if fail_write and name=='CacheMonitor' and str(product) in value:return
        original_write(key,name,reserved,kind,value)
    disabled=bytes([3,0,0,0])+bytes(8)
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,key_path) as key:
            for name in ('InstallRoot','AppPath','RecoveryPath'):
                winreg.SetValueEx(key,name,0,winreg.REG_SZ,'old '+name)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,startup_path) as key:
            if startup is not None:winreg.SetValueEx(key,'CacheMonitor',0,winreg.REG_SZ,startup)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,approved_path) as key:
            winreg.SetValueEx(key,'CacheMonitor',0,winreg.REG_BINARY,disabled)
        before=activation.snapshot_registry(False)
        monkeypatch.setattr(winreg,'SetValueEx',write)
        if fail_write:
            with pytest.raises(OSError,match='로그인 시 시작'):
                install.finish(tmp_path,product,recovery,launch=False)
            assert activation.snapshot_registry(False)==before
            assert receipt.read_bytes()==b'{"product":"old"}' and link.read_bytes()==b'old shortcut'
        else:
            install.finish(tmp_path,product,recovery,launch=False)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,startup_path) as key:
                if startup is None:
                    with pytest.raises(FileNotFoundError):winreg.QueryValueEx(key,'CacheMonitor')
                else:
                    expected=(subprocess.list2cmdline([str(product/'Codexon.exe')])+
                              ' --hidden --codex-home "C:\\first home" --codex-home C:\\second'
                              if 'Codexon.exe' in startup else startup)
                    assert winreg.QueryValueEx(key,'CacheMonitor')==(expected,winreg.REG_SZ)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key_path) as key:
                assert winreg.QueryValueEx(key,'AppPath')[0]==str(product/'Codexon.exe')
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,approved_path) as key:
            assert winreg.QueryValueEx(key,'CacheMonitor')==(disabled,winreg.REG_BINARY)
        assert not (tmp_path/'activation-pending.json').exists()
        assert retired==([] if fail_write else [tmp_path])
    finally:
        for path in (startup_path,approved_path,key_path):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,path)


def test_custom_homes_survive_restart_without_login_startup(tmp_path,monkeypatch):
    path=tmp_path/'launch.json';homes=[str(tmp_path/'Work Home'),str(tmp_path/'Second Home')]
    command=subprocess.list2cmdline(['C:/old/Codexon.exe','--codex-home',homes[0],'--codex-home',homes[1]])
    assert homes_from_command(command)==homes
    save_homes(homes,path=path)
    monkeypatch.setenv('CODEX_HOME','unused-default')
    assert resolve_homes(path=path)==homes
    assert resolve_homes(['explicit'],path=path)==['explicit']
    path.write_text('{"homes":[3]}')
    assert resolve_homes(path=path)==['unused-default']


def test_legacy_gui_migration_ignores_workers_and_keeps_all_homes(tmp_path,monkeypatch):
    from cachemonitor.launch_context import running_homes
    exe=str(tmp_path/'Codexon.exe')
    homes=[str(tmp_path/'Work Home'),str(tmp_path/'Second Home')]
    commands=[['--model-proxy','--codex-home',homes[0]],
              ['--proxy-supervisor','--codex-home',homes[0]],
              ['--hidden','--codex-home',homes[0],'--codex-home',homes[1]]]
    monkeypatch.setattr(install,'processes_under',lambda root:[dict(ExecutablePath=exe,
        CommandLine=subprocess.list2cmdline([exe,*args])) for args in commands])
    assert running_homes(tmp_path)==homes


def test_recovery_translation_does_not_import_qt():
    result=subprocess.run([__import__('sys').executable,'-c',
        "from cachemonitor.translation_catalog import translate; import sys; "
        "assert translate('직접 연결로 복원','en')=='Restore direct connection'; "
        "assert not any(k.startswith('PySide6') for k in sys.modules)"],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows shell shortcut identity')
def test_shortcut_preserves_recovery_application_identity(tmp_path):
    import os
    import sys
    from cachemonitor.shell_shortcut import application_id
    link=tmp_path/'recovery.lnk'
    script='$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:CODEXON_QA_SHORTCUT);$s.TargetPath=$env:CODEXON_QA_TARGET;$s.Save()'
    subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],check=True,
        env={**os.environ,'CODEXON_QA_SHORTCUT':str(link),'CODEXON_QA_TARGET':sys.executable},
        creationflags=subprocess.CREATE_NO_WINDOW)
    application_id(link,'Codexon-QA.Recovery')
    assert application_id(link)=='Codexon-QA.Recovery'
