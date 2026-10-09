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
    from types import SimpleNamespace
    plan=app_update.check_update(manager=SimpleNamespace(home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768',status=lambda:{'health':{'active_connections':7}}))
    assert plan['connections']==7
    assert calls==[] and not (tmp_path/'downloads').exists()
    read_url=app_update.read_url
    monkeypatch.setattr(app_update,'read_url',lambda url,*args:pytest.fail('Confirmed release must not change') if url.endswith('latest') else read_url(url,*args))
    result=app_update.update(plan=plan)
    assert result['phase']=='installing' and calls[0][2]==result['operation_id']
    with pytest.raises(RuntimeError,match='이미'):app_update.update(plan=plan)
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


def test_current_release_updates_the_active_observation_proxy(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor import install_management
    calls=[]
    manager=SimpleNamespace(home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768',status=lambda:{'configured':True,'health':{'status':'ok'}},
        cleanup_legacy_check=lambda:None,adopt_registrations=lambda:None,
        update_proxy=lambda **kw:calls.append(kw) or {'update':{'phase':'queued','message':'연결 종료 후 적용'}})
    monkeypatch.setattr(app_update,'VERSION',release()['tag_name'].lstrip('v'))
    monkeypatch.setattr(app_update,'installed',lambda:dict(InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'read_url',lambda *a:json.dumps(release()).encode())
    monkeypatch.setattr(install_management,'connection_manager',lambda:pytest.fail('Explicit manager must be retained'))
    monkeypatch.setattr('cachemonitor.retired_cache.retire',lambda m:False)
    monkeypatch.setattr(app_update,'inspect_proxy',lambda m:dict(state='required',reason='프록시 업데이트 필요',connections=0,instance='test'))
    plan=app_update.check_update(manager=manager)
    assert calls==[]
    result=app_update.update(plan=plan)
    assert result['phase']=='waiting' and result['message']=='연결 종료 후 적용'
    assert len(calls)==1 and calls[0]['operation_id']==result['operation_id']


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


@pytest.mark.parametrize('previous_kind',[None,'none','app'])
def test_failed_update_recheck_labels_previous_result_and_recovers(monkeypatch,previous_kind):
    from urllib.error import URLError
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    panel=UpdatePanel(None)
    def settle():
        for _ in range(300):
            app.processEvents();QTest.qWait(10)
            if panel.operation is None:return
        raise AssertionError('Update check did not finish')
    try:
        if previous_kind:
            panel.pending_plan=dict(kind=previous_kind,reason='프록시 사용 안 함')
            panel.finished()
        def fail(*args):raise URLError(TimeoutError('synthetic timeout'))
        monkeypatch.setattr(app_update,'check_update',fail)
        panel.start();settle()
        assert '응답이 늦어지고 있습니다' in panel.status.text()
        assert 'synthetic timeout' not in panel.status.text()
        assert not panel.execute.isVisible() and panel.offer is None
        panel.proxy_status({'update':{'phase':'complete','message':'이전 업데이트 완료'}})
        assert '응답이 늦어지고 있습니다' in panel.status.text()
        monkeypatch.setattr(app_update,'check_update',lambda *args:dict(kind='none',reason='프록시 사용 안 함'))
        panel.start();settle()
        assert panel.status.text()=='설치할 새 버전이 없습니다.' and panel.button.isEnabled()
    finally:
        if panel.operation:panel.operation.wait()
        panel.deleteLater();app.processEvents()


def test_update_failure_explanations_do_not_confuse_file_and_network_errors():
    from urllib.error import HTTPError, URLError
    from cachemonitor.update_panel import failure_message
    assert '요청을 제한' not in failure_message(HTTPError('https://example.test',403,'Forbidden',None,None),True)
    assert '요청을 제한' in failure_message(HTTPError('https://example.test',429,'Too Many Requests',None,None),True)
    assert '연결하지 못했습니다' in failure_message(URLError(OSError('synthetic refusal')),True)
    assert '파일 접근 권한' in failure_message(PermissionError('synthetic file denial'),False)

@pytest.mark.parametrize('kind',['app','proxy'])
@pytest.mark.parametrize('closing',[False,True])
def test_explicit_start_is_single_flight_and_check_remains_read_only(tmp_path,monkeypatch,kind,closing):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.update_panel import UpdatePanel
    from types import SimpleNamespace
    import threading
    app=QApplication.instance() or QApplication([])
    panel=UpdatePanel(None);calls=[];gate=threading.Event()
    plan=dict(kind=kind,release=release(),connections=3)
    monkeypatch.setattr(app_update,'check_update',lambda *a:plan)
    def install(*a,**kw):
        calls.append(kw['plan']);gate.wait(2);return '설치 시작'
    monkeypatch.setattr(app_update,'update',install)
    def settle():
        for _ in range(300):
            app.processEvents();QTest.qWait(5)
            if panel.operation is None:return
        raise AssertionError('Update check did not finish')
    try:
        panel.start();settle()
        assert not calls and panel.execute.isVisible() and not panel.button.isVisible()
        if closing:panel.owner=SimpleNamespace(_closing=True)
        panel.request_install();panel.request_install();panel.start()
        QTest.qWait(40)
        assert calls==([] if closing else [plan])
        gate.set();settle()
    finally:
        gate.set()
        if panel.operation:panel.operation.wait()
        panel.deleteLater();app.processEvents()


@pytest.mark.parametrize('health,expected', [({},None),({'active_connections':0},0),
    ({'active_connections':5},5),({'active_connections':-1},None),({'active_connections':True},None)])
def test_connection_count_preserves_unknown(health,expected):
    assert app_update.connection_count({'health':health})==expected
