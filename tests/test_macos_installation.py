import json
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace

import pytest

from cachemonitor import macos_installation as mac


def source_at(path):
    manifest=dict(product='Codexon',platform='darwin',version='2026.09.26.1',commit='a'*40,architecture='arm64')
    for name,(binary,identifier) in mac.BUNDLES.items():
        bundle=path/name
        (bundle/'Contents/MacOS').mkdir(parents=True)
        (bundle/'Contents/Resources').mkdir()
        (bundle/'Contents/Info.plist').write_bytes(plistlib.dumps(dict(CFBundleIdentifier=identifier,CFBundleExecutable=binary)))
        (bundle/'Contents/MacOS'/binary).write_bytes(b'fixture executable')
        (bundle/'Contents/Resources/build-manifest.json').write_text(json.dumps(manifest))
    return path


@pytest.fixture
def local_install(tmp_path,monkeypatch):
    if sys.platform!='darwin':pytest.skip('macOS bundle symlink transaction')
    source=source_at(tmp_path/'source')
    def verify(bundle,identifier,**kwargs):
        return dict(team=None,ad_hoc=True,executable=str(mac.executable(bundle)),sha256='fixture')
    monkeypatch.setattr(mac,'verify_bundle',verify)
    def run(command,**kwargs):
        assert command[1]=='--verify-runtime'
        Path(command[2]).write_text(json.dumps(dict(errors=[],version='2026.09.26.1')))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(mac.subprocess,'run',run)
    options=dict(root=tmp_path/'installation',applications=tmp_path/'Applications',
                 isolated=True,launch=False,allow_ad_hoc=True)
    return source,options


def test_two_installs_keep_the_old_binary_and_rollback_receipt(local_install):
    source,options=local_install
    first=mac.install_source(source,**options)
    second=mac.install_source(source,**options)
    assert second['previous']==first['product']
    assert Path(first['AppPath']).read_bytes()==b'fixture executable'
    assert (options['applications']/'Codexon.app').resolve()==Path(second['AppPath']).parents[2]
    assert mac.installed(options['root'])['AppPath']==second['AppPath']


def test_receipt_failure_restores_both_launch_links_and_previous_receipt(local_install,monkeypatch):
    source,options=local_install
    before=mac.install_source(source,**options)
    receipt=(options['root']/'installation.json').read_bytes()
    original=mac.atomic_write;failed=[]
    def write(path,data):
        if path.name=='installation.json' and not failed:
            failed.append(True);raise OSError('injected write failure')
        return original(path,data)
    monkeypatch.setattr(mac,'atomic_write',write)
    with pytest.raises(OSError,match='injected'):mac.install_source(source,**options)
    assert json.loads((options['root']/'installation.json').read_bytes())==json.loads(receipt)
    assert (options['applications']/'Codexon.app').resolve()==Path(before['AppPath']).parents[2]
    assert (options['applications']/'Codexon Recovery.app').resolve()==Path(before['RecoveryPath']).parents[2]
    assert not (options['root']/'activation-pending.json').exists()


def test_runtime_failure_does_not_publish_or_replace_existing_application(local_install,monkeypatch):
    source,options=local_install
    before=mac.install_source(source,**options)
    monkeypatch.setattr(mac.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError,match='실행 검사'):mac.install_source(source,**options)
    assert mac.installed(options['root'])['AppPath']==before['AppPath']


def test_foreign_application_and_foreign_symlink_are_preserved(local_install,tmp_path):
    source,options=local_install
    options['applications'].mkdir()
    link=options['applications']/'Codexon.app'
    external=tmp_path/'other-app';external.mkdir()
    link.symlink_to(external)
    with pytest.raises(ValueError,match='다른 설치'):mac.install_source(source,**options)
    assert link.resolve()==external and not (options['root']/'installation.json').exists()


def test_product_and_recovery_must_belong_to_same_release(local_install):
    source,options=local_install
    path=source/'Codexon Recovery.app/Contents/Resources/build-manifest.json'
    value=json.loads(path.read_text());value['version']='2026.09.26.2';path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='배포 정보가 다릅니다'):mac.install_source(source,**options)


def test_invalid_or_different_team_signature_never_passes(tmp_path,monkeypatch):
    source=source_at(tmp_path/'source');bundle=source/'Codexon.app'
    calls=[]
    def run(command,**kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0,stderr=b'TeamIdentifier=FOREIGN\nAuthority=Developer ID Application: Other\n')
    monkeypatch.setattr(mac.subprocess,'run',run)
    with pytest.raises(ValueError,match='개발자 서명이 다릅니다'):
        mac.verify_bundle(bundle,mac.BUNDLES['Codexon.app'][1],expected_team='EXPECTED')
    assert not any(command[0].endswith('spctl') for command in calls)
    monkeypatch.setattr(mac.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    with pytest.raises(ValueError,match='서명 검증'):mac.verify_bundle(bundle,mac.BUNDLES['Codexon.app'][1])


def test_ad_hoc_package_cannot_launch_downloaded_update(local_install,monkeypatch):
    source,options=local_install
    mac.install_source(source,**options)
    monkeypatch.setattr(mac.subprocess,'run',lambda *a,**k:pytest.fail('No disk image or executable may launch'))
    with pytest.raises(RuntimeError,match='로컬 빌드'):mac.launch_update(Path('download.dmg'),options['root'])


def test_installed_receipt_cannot_point_outside_owned_version_directory(local_install):
    source,options=local_install
    receipt=mac.install_source(source,**options)
    receipt['AppPath']=str(mac.executable(source/'Codexon.app'))
    (options['root']/'installation.json').write_text(json.dumps(receipt))
    assert mac.installed(options['root'])=={}


@pytest.mark.parametrize('fail',[False,True])
def test_install_preserves_login_preferences_and_rolls_them_back_on_failure(local_install,monkeypatch,fail):
    source,options=local_install
    first=mac.install_source(source,**options)
    before=dict(registered=True,command=[first['AppPath'],'--hidden','--codex-home','/fixture/Work Home'],
                autostart=False,enabled=False)
    state=dict(before)
    class Task:
        def inspect(self):return dict(state)
        def configure(self,command,*,autostart):state.update(command=command,autostart=autostart,enabled=True)
        def suspend(self):state['enabled']=False
    from cachemonitor import macos_startup
    monkeypatch.setattr(macos_startup,'MacStartup',lambda:SimpleNamespace(task=Task()))
    options['isolated']=False
    original_run=mac.subprocess.run
    monkeypatch.setattr(mac.subprocess,'run',lambda command,**kw:SimpleNamespace(returncode=0)
        if command[1]=='--complete-install' else original_run(command,**kw))
    original_write=mac.atomic_write;failed=[]
    def write(path,data):
        if fail and path.name=='installation.json' and not failed:
            failed.append(True);raise OSError('fixture write failure')
        return original_write(path,data)
    monkeypatch.setattr(mac,'atomic_write',write)
    if fail:
        with pytest.raises(OSError):mac.install_source(source,**options)
        assert state==before
    else:
        second=mac.install_source(source,**options)
        assert state==dict(before,command=[second['AppPath'],*before['command'][1:]])


def test_recovery_notification_uses_current_installed_helper_without_url_registration(monkeypatch):
    from cachemonitor import installation,macos_notifications
    calls=[]
    monkeypatch.setattr(installation,'recovery_command',lambda:['/owned/current/Recovery'])
    monkeypatch.setattr('subprocess.Popen',lambda command,**options:calls.append((command,options)))
    assert not macos_notifications.open_recovery_uri('https://example.invalid/')
    assert macos_notifications.open_recovery_uri('codexon-recovery:fixture')
    assert calls[0][0]==['/owned/current/Recovery','codexon-recovery:fixture']
    assert calls[0][1]['env']['PYINSTALLER_RESET_ENVIRONMENT']=='1' and len(calls)==1
