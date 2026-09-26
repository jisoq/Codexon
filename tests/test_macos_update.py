import pytest
import json
from types import SimpleNamespace
from cachemonitor.app_update import release_asset
from cachemonitor import app_update


def release(*names):
    prefix='https://github.com/jisoq/Codexon/releases/download/v2026.09.26.1/'
    return dict(assets=[dict(name=name,browser_download_url=prefix+name) for name in names])


def test_native_macos_asset_is_selected_without_windows_fallback():
    value=release('Codexon-Setup.exe','Codexon-Setup.exe.sha256',
                  'Codexon-macOS-arm64.dmg','Codexon-macOS-arm64.dmg.sha256',
                  'Codexon-macOS-x86_64.dmg','Codexon-macOS-x86_64.dmg.sha256')
    assert release_asset(value,platform_name='darwin',architecture='arm64')[0]['name']=='Codexon-macOS-arm64.dmg'
    assert release_asset(value,platform_name='darwin',architecture='x86_64')[0]['name']=='Codexon-macOS-x86_64.dmg'
    with pytest.raises(ValueError):release_asset(release('Codexon-Setup.exe'),platform_name='darwin',architecture='arm64')


def test_universal_asset_requires_matching_checksum_and_unambiguous_pair():
    value=release('Codexon-macOS-universal2.dmg','Codexon-macOS-universal2.dmg.sha256')
    assert release_asset(value,platform_name='darwin',architecture='arm64')[0]['name']=='Codexon-macOS-universal2.dmg'
    value['assets'].append(value['assets'][0])
    with pytest.raises(ValueError):release_asset(value,platform_name='darwin',architecture='arm64')


def test_complete_universal_pair_is_usable_during_incomplete_native_upload():
    value=release('Codexon-macOS-arm64.dmg','Codexon-macOS-universal2.dmg','Codexon-macOS-universal2.dmg.sha256')
    assert release_asset(value,platform_name='darwin',architecture='arm64')[0]['name']=='Codexon-macOS-universal2.dmg'


def test_newer_windows_release_does_not_hide_latest_compatible_mac_offer(tmp_path,monkeypatch):
    windows=release('Codexon-Setup.exe','Codexon-Setup.exe.sha256');windows['tag_name']='v2026.09.27.1'
    native=release('Codexon-macOS-arm64.dmg','Codexon-macOS-arm64.dmg.sha256');native['tag_name']='v2026.09.26.2'
    intel=release('Codexon-macOS-x86_64.dmg','Codexon-macOS-x86_64.dmg.sha256');intel['tag_name']='v2026.09.26.3'
    preview=dict(native,prerelease=True,tag_name='v2026.09.29.1')
    calls=[]
    monkeypatch.setattr(app_update.platform,'machine',lambda:'arm64')
    monkeypatch.setattr(app_update,'installed',lambda:dict(platform='darwin',InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'VERSION','2026.09.26.1')
    monkeypatch.setattr(app_update,'read_url',lambda url:(calls.append(url) or json.dumps([windows,intel,preview,native]).encode()))
    plan=app_update.check_update(manager=SimpleNamespace(status=lambda:{}))
    assert plan['kind']=='app' and plan['release']==native
    assert len(calls)==1 and '/releases?per_page=100&page=1' in calls[0]


def test_mac_release_selection_follows_pagination_and_skips_incomplete_upload(monkeypatch):
    windows=release('Codexon-Setup.exe');windows['tag_name']='v2026.09.27.1'
    pending=release('Codexon-macOS-arm64.dmg');pending['tag_name']='v2026.09.28.1'
    compatible=release('Codexon-macOS-arm64.dmg','Codexon-macOS-arm64.dmg.sha256');compatible['tag_name']='v2026.09.26.1'
    pages=[[pending,*[windows]*99],[compatible]]
    monkeypatch.setattr(app_update.platform,'machine',lambda:'arm64')
    monkeypatch.setattr(app_update,'read_url',lambda url:json.dumps(pages.pop(0)).encode())
    assert app_update.mac_release()==compatible and not pages
