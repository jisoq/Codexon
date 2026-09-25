import hashlib
import json
from pathlib import Path

import pytest

from cachemonitor import app_update


def release():
    prefix='https://github.com/jisoq/Codexon/releases/download/v2026.09.24.1/'
    return dict(tag_name='v2026.09.24.1',assets=[
        dict(name='Codexon-Setup.exe',browser_download_url=prefix+'Codexon-Setup.exe',size=7),
        dict(name='Codexon-Setup.exe.sha256',browser_download_url=prefix+'Codexon-Setup.exe.sha256')])


def test_release_assets_cannot_mix_sources_or_be_ambiguous():
    value=release();assert app_update.release_asset(value)[0]['size']==7
    value['assets'][0]['browser_download_url']='https://evil.example/setup.exe'
    with pytest.raises(ValueError):app_update.release_asset(value)
    value=release();value['assets'].append(value['assets'][0])
    with pytest.raises(ValueError):app_update.release_asset(value)
    value=release();value['prerelease']=True
    with pytest.raises(ValueError):app_update.release_asset(value)


def test_failed_download_never_launches_installer(tmp_path,monkeypatch):
    import io
    monkeypatch.setattr(app_update,'VERSION','2026.09.23.9')
    value=release()
    monkeypatch.setattr(app_update,'installed',lambda:dict(InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'read_url',lambda url,*a:json.dumps(value).encode() if url.endswith('latest') else (('0'*64)+'  Codexon-Setup.exe').encode())
    monkeypatch.setattr(app_update.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(b'corrupt'))
    monkeypatch.setattr(app_update,'launch_installer',lambda *a:pytest.fail('Unverified installer executed'))
    with pytest.raises(ValueError,match='검증'):app_update.update()
    assert not list(tmp_path.rglob('*.exe')) and not list(tmp_path.rglob('*.partial'))


def test_verified_release_launches_one_installer_and_preserves_settings(tmp_path,monkeypatch):
    import io
    monkeypatch.setattr(app_update,'VERSION','2026.09.23.9')
    value=release();data=b'payload';digest=hashlib.sha256(data).hexdigest()
    value['assets'][0]['digest']='sha256:'+digest
    monkeypatch.setattr(app_update,'installed',lambda:dict(InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'read_url',lambda url,*a:json.dumps(value).encode() if url.endswith('latest') else (digest+'  Codexon-Setup.exe').encode())
    monkeypatch.setattr(app_update.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(data))
    calls=[];monkeypatch.setattr(app_update,'launch_installer',lambda *a:calls.append(a))
    assert '설치' in app_update.update()
    assert len(calls)==1 and Path(calls[0][0]).read_bytes()==data


@pytest.mark.parametrize('current', ['2026.09.24.1', '2026.09.25.6'])
def test_no_new_release_succeeds_with_proxy_off(tmp_path,monkeypatch,current):
    from types import SimpleNamespace
    from cachemonitor import install_management
    manager=SimpleNamespace(status=lambda:{'configured':False})
    monkeypatch.setattr(app_update,'VERSION',current)
    monkeypatch.setattr(app_update,'installed',lambda:dict(InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'read_url',lambda *a:json.dumps(release()).encode())
    monkeypatch.setattr(install_management,'connection_manager',lambda:manager)
    monkeypatch.setattr(app_update,'launch_installer',lambda *a:pytest.fail('No newer release'))
    assert app_update.update()=='설치할 새 버전이 없습니다.'


def test_current_release_updates_the_active_cache_worker(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor import install_management
    calls=[]
    manager=SimpleNamespace(shared_cache_worker=True,status=lambda:{'configured':True},
        update_proxy=lambda:calls.append(True) or {'update':{'phase':'queued','message':'연결 종료 후 적용'}})
    monkeypatch.setattr(app_update,'VERSION',release()['tag_name'].lstrip('v'))
    monkeypatch.setattr(app_update,'installed',lambda:dict(InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'read_url',lambda *a:json.dumps(release()).encode())
    monkeypatch.setattr(install_management,'connection_manager',lambda:pytest.fail('Explicit manager must be retained'))
    assert '연결 종료 후 적용' in app_update.update(manager=manager)
    assert calls==[True]


def test_update_panel_keeps_success_when_proxy_is_off():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    panel=UpdatePanel(None)
    panel.status.setText('설치할 새 버전이 없습니다.')
    panel.proxy_status({'update':{'phase':'off','message':'프록시 사용 꺼짐'}})
    assert panel.button.isEnabled()
    assert panel.status.text()=='설치할 새 버전이 없습니다.'
    panel.deleteLater()
    app.processEvents()
