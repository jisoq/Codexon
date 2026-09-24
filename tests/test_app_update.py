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
