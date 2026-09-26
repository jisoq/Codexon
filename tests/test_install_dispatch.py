import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cachemonitor import connection_recovery as recovery, install_dispatch as dispatch


@pytest.mark.parametrize('native,packaged',[(False,True),(True,False),(True,True)])
def test_installer_mutates_only_the_native_environment(tmp_path,monkeypatch,native,packaged):
    calls=[]
    monkeypatch.setattr(dispatch,'packaged_context',lambda:packaged)
    monkeypatch.setattr(dispatch,'native_install',lambda args:(calls.append('dispatch') or dict(version='synthetic')))
    monkeypatch.setattr('cachemonitor.install_management.finish',lambda *a,**k:(calls.append('finish') or dict(version='synthetic')))
    report=tmp_path/'result.json'
    args=['--install-root',str(tmp_path),'--product-dir',str(tmp_path/'product'),'--report',str(report)]
    if native:args.append('--native-install')
    assert recovery.main(args)==int(native and packaged)
    assert calls==([] if native and packaged else ['dispatch'] if packaged else ['finish'])
    assert bool(json.loads(report.read_text(encoding='utf-8')).get('error'))==(native and packaged)


@pytest.mark.parametrize('result',[{'version':'synthetic'},{'error':'synthetic failure'},None])
def test_native_activation_requires_its_own_completed_receipt(tmp_path,monkeypatch,result):
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:tmp_path))
    calls=[]
    class Task:
        def __init__(self,scope,role):self.report=Path(scope);assert role=='InstallationActivation'
        def start(self,command):
            calls.append(command)
            assert command[command.index('--report')+1]==str(self.report)
            assert '--native-install' in command and '--no-launch' in command and '--isolated-install' in command
            if result is not None:self.report.write_text(json.dumps(result))
        def inspect(self):return dict(running=0,state=3)
        def remove(self):calls.append('removed')
    monkeypatch.setattr(dispatch,'ObserverTask',Task)
    args=SimpleNamespace(install_root=tmp_path/'installed',product_dir=tmp_path/'new',language='en',
                         no_launch=True,isolated_install=True,prepare_uninstall=False)
    if result is None:
        with pytest.raises(RuntimeError,match='결과 없이'):dispatch.native_install(args)
    else:assert dispatch.native_install(args)==result
    assert calls[-1]=='removed'


def test_shared_installation_pointer_overrides_stale_registry_view(tmp_path,monkeypatch):
    from cachemonitor import installation
    root=tmp_path/'installed'
    app=root/'versions'/'new'/'Codexon.exe';app.parent.mkdir(parents=True);app.touch()
    tool=root/'maintenance'/'new'/'CodexonRecovery.exe';tool.parent.mkdir(parents=True);tool.touch()
    shared=tmp_path/'shared.json'
    expected=dict(InstallRoot=str(root),AppPath=str(app),RecoveryPath=str(tool))
    shared.write_text(json.dumps(expected))
    monkeypatch.setattr(installation,'pointer_path',lambda:shared)
    import winreg
    monkeypatch.setattr(winreg,'OpenKey',lambda *a,**k:pytest.fail('A stale registry view must not override the committed pointer'))
    assert installation.installed()==expected
    assert not installation.valid_paths({**expected,'AppPath':str(tmp_path/'other.exe')})


def test_update_recovers_stopped_cache_worker_before_scheduling_replacement():
    from cachemonitor.install_management import activate_proxy
    calls=[]
    manager=SimpleNamespace(shared_cache_worker=True,status=lambda:dict(configured=True,health=None),
        cleanup_legacy_check=lambda:calls.append('retire-check'),adopt_registrations=lambda:calls.append('adopt'),
        resume=lambda:calls.append('resume'),update_proxy=lambda:(calls.append('update') or {'update':{'phase':'queued'}}))
    assert activate_proxy(manager)=={'phase':'queued'}
    assert calls==['retire-check','adopt','resume','update']
