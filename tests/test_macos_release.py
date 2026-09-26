import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from tools import mac_release, mac_signing, mac_package


def release_tree(tmp_path):
    for architecture in mac_release.ARCHITECTURES:
        root=tmp_path/architecture;root.mkdir()
        name=f'Codexon-macOS-{architecture}.dmg'
        image=root/name;image.write_bytes(b'signed fixture '+architecture.encode())
        image.with_suffix('.dmg.sha256').write_text(mac_release.digest(image)+'  '+name+'\n')
        report=dict(validated=True,commit='a'*40,version='2026.09.26.1',architecture=architecture,
                    team='TEAM123456',signing='Developer ID',source_dirty=False,notarized=True,minimum_macos='13.0',
                    stapled=['Codexon.app','Codexon Recovery.app','Install Codexon.app',name],
                    source_checks_passed=True,native_desktop={'passed':True,'platform':'cocoa'},
                    installation={'passed':True},handoff={'passed':True})
        (root/'verification.json').write_text(json.dumps(report))
    sources=tmp_path/'sources';sources.mkdir()
    image=sources/'Codexon-third-party-source-6.11.2.zip';image.write_bytes(b'source archive')
    image.with_suffix('.zip.sha256').write_text(mac_release.digest(image)+'  '+image.name+'\n')
    return tmp_path


@pytest.mark.parametrize('field,value',[('commit','b'*40),('team','WRONG'),('notarized',False),
    ('source_dirty',True),('stapled',[]),('native_desktop',{'passed':True,'platform':'offscreen'}),
    ('installation',{'passed':False}),('handoff',{'passed':False}),('minimum_macos','26.0')])
def test_any_missing_release_gate_prevents_publication(tmp_path,monkeypatch,field,value):
    root=release_tree(tmp_path);path=root/'x86_64/verification.json'
    report=json.loads(path.read_text());report[field]=value;path.write_text(json.dumps(report))
    monkeypatch.setattr(mac_release,'api',lambda *a,**k:pytest.fail('No GitHub request before local gates pass'))
    with pytest.raises(ValueError):
        mac_release.publish(root,commit='a'*40,tag='v2026.09.26.1',team='TEAM123456',repository='jisoq/Codexon')


def test_asset_digest_and_existing_tag_are_checked_before_mutation(tmp_path,monkeypatch):
    root=release_tree(tmp_path)
    monkeypatch.setattr(mac_release,'remote_tag',lambda *a:'b'*40)
    monkeypatch.setattr(mac_release,'run',lambda *a:pytest.fail('No asset can be published'))
    with pytest.raises(ValueError,match='different commit'):
        mac_release.publish(root,commit='a'*40,tag='v2026.09.26.1',team='TEAM123456',repository='jisoq/Codexon')
    image=root/'arm64/Codexon-macOS-arm64.dmg';image.write_bytes(b'changed after verification')
    with pytest.raises(ValueError,match='checksum'):
        mac_release.release_files(root,commit='a'*40,version='2026.09.26.1',team='TEAM123456')


def test_existing_windows_release_is_only_attached_and_never_made_latest(tmp_path,monkeypatch):
    root=release_tree(tmp_path);calls=[]
    monkeypatch.setattr(mac_release,'remote_tag',lambda *a:'a'*40)
    monkeypatch.setattr(mac_release,'api',lambda *a,**k:dict(draft=False,prerelease=False,assets=[
        dict(name='Codexon-third-party-source-6.11.2.zip'),dict(name='Codexon-third-party-source-6.11.2.zip.sha256')]))
    monkeypatch.setattr(mac_release,'run',lambda args:calls.append(args))
    result=mac_release.publish(root,commit='a'*40,tag='v2026.09.26.1',team='TEAM123456',repository='jisoq/Codexon')
    assert result['attached'] and len(calls)==1 and calls[0][:3]==['gh','release','upload']
    assert all('--clobber' not in call and '--latest' not in call for call in calls)


def test_mac_only_release_is_draft_until_commit_is_reverified(tmp_path,monkeypatch):
    root=release_tree(tmp_path);calls=[];refs=iter([None,'b'*40])
    monkeypatch.setattr(mac_release,'remote_tag',lambda *a:next(refs))
    monkeypatch.setattr(mac_release,'api',lambda *a,**k:None)
    monkeypatch.setattr(mac_release,'run',lambda args:calls.append(args))
    with pytest.raises(ValueError,match='remains a draft'):
        mac_release.publish(root,commit='a'*40,tag='v2026.09.26.1',team='TEAM123456',repository='jisoq/Codexon')
    assert len(calls)==1 and calls[0][:3]==['gh','release','create']
    assert '--draft' in calls[0] and '--latest=false' in calls[0]
    assert any(str(path).endswith('.zip') for path in calls[0])


def test_signing_credentials_never_enter_arguments_and_cleanup_runs_on_failure(tmp_path,monkeypatch):
    p12=b'fixture private P12';private=b'fixture private API key';password='fixture private password'
    environment=dict(CODEXON_DEVELOPER_ID_P12_BASE64=base64.b64encode(p12).decode(),
        CODEXON_DEVELOPER_ID_P12_PASSWORD=password,CODEXON_NOTARY_KEY_BASE64=base64.b64encode(private).decode(),
        CODEXON_NOTARY_KEY_ID='PUBLICKEY1',CODEXON_NOTARY_ISSUER_ID='public-issuer',
        CODEXON_TEAM_ID='TEAM123456',RUNNER_TEMP=str(tmp_path))
    calls=[]
    def command(arguments,*,env,input=None):
        calls.append(arguments)
        assert not set(mac_signing.SECRET_NAMES)&env.keys()
        assert all(password not in item and p12.decode() not in item and private.decode() not in item for item in arguments)
        if 'pkcs12' in arguments:assert input==password.encode()+b'\n'
        if 'create-keychain' in arguments:Path(arguments[-1]).touch()
        if 'list-keychains' in arguments and '-s' not in arguments:return '"/existing/login.keychain-db"\n'
        if 'find-identity' in arguments:return '1) '+('A'*40)+' "Developer ID Application: Fixture (TEAM123456)"\n'
        return ''
    monkeypatch.setattr(mac_signing,'command',command)
    with pytest.raises(RuntimeError,match='build failure'):
        with mac_signing.signing_environment(environment) as env:
            assert env['CODEXON_SIGNING_IDENTITY']=='A'*40
            assert not list(tmp_path.rglob('*.p12')) and not list(tmp_path.rglob('*.pem')) and not list(tmp_path.rglob('*.p8'))
            raise RuntimeError('build failure')
    assert calls[-2]==['/usr/bin/security','list-keychains','-d','user','-s','/existing/login.keychain-db']
    assert calls[-1][1]=='delete-keychain' and not list(tmp_path.iterdir())


def test_native_load_commands_override_optimistic_bundle_minimum(tmp_path,monkeypatch):
    (tmp_path/'Python').write_bytes(b'\xcf\xfa\xed\xfe'+b'fixture')
    def command(args,**kwargs):
        return SimpleNamespace(stdout='arm64\n' if '-archs' in args else
            'Load command 10\n cmd LC_BUILD_VERSION\n platform MACOS\n minos 26.0\n sdk 26.4\n')
    monkeypatch.setattr(mac_package.subprocess,'run',command)
    assert mac_package.native_requirements(tmp_path,'arm64')==dict(minimum_macos='26.0',native_binaries=1)
    with pytest.raises(ValueError,match='architecture'):mac_package.native_requirements(tmp_path,'x86_64')
