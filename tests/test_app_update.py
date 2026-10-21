import hashlib
import json
from pathlib import Path

import pytest

from cachemonitor import app_update


def release(tag='v2026.09.24.1'):
    prefix='https://github.com/jisoq/Codexon/releases/download/'+tag+'/'
    return dict(tag_name=tag,assets=[
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


def test_connection_closed_before_download_response_can_retry_same_release(tmp_path,monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import socket
    import threading
    import time
    from http.client import RemoteDisconnected
    from types import SimpleNamespace
    from cachemonitor.update_state import UpdateState
    from cachemonitor.update_panel import UpdatePanel
    from cachemonitor.quick_runtime import QuickHost
    from cachemonitor.quick_qa import control
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QPointF,Qt
    from PySide6.QtTest import QTest
    app=QApplication.instance() or QApplication([])
    offered=release('v2099.01.01.1')
    payload=b'payload';digest=hashlib.sha256(payload).hexdigest();downloads=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/latest':body=json.dumps(offered).encode()
            elif self.path.endswith('.sha256'):body=(digest+'  Codexon-Setup.exe').encode()
            else:
                downloads.append(True)
                if len(downloads)==1:
                    self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                body=payload
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
            self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    open_url=app_update.urllib.request.urlopen
    def local(request,**kwargs):
        url=f'http://127.0.0.1:{server.server_port}/'+request.full_url.rsplit('/',1)[1]
        return open_url(app_update.urllib.request.Request(url,headers=app_update.HEADERS),**kwargs)
    monkeypatch.setattr(app_update.urllib.request,'urlopen',local)
    calls=[];monkeypatch.setattr(app_update,'launch_installer',lambda *args:calls.append(args))
    m=SimpleNamespace(home=tmp_path/'home',evidence=tmp_path/'evidence.sqlite',url='http://127.0.0.1:18768',
                      directory=tmp_path,status=lambda:{'configured':False})
    plan=dict(kind='app',release=offered,install={'InstallRoot':str(tmp_path)},manager=m)
    monkeypatch.setattr(app_update,'installed',lambda:plan['install'])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    journal=UpdateState(tmp_path);panel=None;host=None
    def settle(predicate):
        deadline=time.monotonic()+3
        while not predicate() and time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5)
        assert predicate()
    def click(node):
        item=control(host,node)
        def ready():
            app.processEvents();host.quick.grabFramebuffer()
            return item.isVisible() and item.isEnabled() and item.width()>0 and item.height()>0
        settle(ready)
        point=item.mapToScene(QPointF(item.width()/2,item.height()/2)).toPoint()
        assert host.quick.rect().contains(point)
        QTest.mouseClick(host.quick,Qt.LeftButton,pos=point)
    try:
        with pytest.raises(RemoteDisconnected):app_update.update(plan=plan)
        failed=journal.read()
        assert failed['phase']=='failed' and failed['app']['state']=='pending' and not calls
        assert not list(tmp_path.rglob('*.exe')) and not list(tmp_path.rglob('*.partial'))
        panel=UpdatePanel(m);host=QuickHost();host.resize(640,240);host.setCentralWidget(panel);host.show()
        before=journal.path.read_bytes();click(panel.button)
        settle(lambda:panel.operation is None and bool(panel.offer))
        assert panel.offer['release']==offered and journal.path.read_bytes()==before
        click(panel.execute)
        settle(lambda:panel.operation is None and bool(calls))
        retried=journal.read()
        assert retried['operation_id']!=failed['operation_id'] and retried['phase']=='installing'
        assert len(calls)==1 and Path(calls[0][0]).read_bytes()==payload
    finally:
        if panel:
            panel.stop()
            if panel.operation:panel.operation.wait()
        if host:host.release_scene();host.close();host.deleteLater();app.processEvents()
        server.shutdown();server.server_close();thread.join(timeout=2)


def test_download_cache_keeps_recent_active_and_unknown_files(tmp_path,monkeypatch):
    import os
    from cachemonitor import install_management
    folders=[]
    for number in range(5):
        folder=tmp_path/'downloads'/f'{number:032x}';folder.mkdir(parents=True)
        setup=folder/'Codexon-Setup.exe';setup.write_bytes(b'installer')
        os.utime(setup,(100+number,100+number));folders.append(folder)
    private=tmp_path/'downloads'/('f'*32);private.mkdir()
    (private/'records.sqlite').write_bytes(b'preserve')
    monkeypatch.setattr(install_management,'processes_under',lambda root:[
        dict(ExecutablePath=str(folders[0]/'Codexon-Setup.exe'),CommandLine='')])
    (tmp_path/'app-update.json').write_text(json.dumps(dict(phase='waiting',installer=str(folders[1]/'Codexon-Setup.exe'))))
    assert app_update.prune_downloads(tmp_path)==[str(folders[2])]
    assert all(folder.exists() for folder in (folders[0],folders[1],folders[3],folders[4],private))
    monkeypatch.setattr(install_management,'processes_under',lambda root:[])
    (tmp_path/'app-update.json').write_text(json.dumps(dict(phase='complete')))
    assert set(app_update.prune_downloads(tmp_path))=={str(folders[0]),str(folders[1])}
    assert (private/'records.sqlite').read_bytes()==b'preserve'


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


def test_update_panel_reloads_atomic_journal_changes_without_idle_json_reads(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.update_panel import UpdatePanel
    import time
    app=QApplication.instance() or QApplication([])
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'));journal=panel.journal
    reads=[];read=journal.read
    monkeypatch.setattr(journal,'read',lambda:reads.append(True) or read())
    def settle(predicate):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            app.processEvents()
            if predicate():return
            QTest.qWait(5)
        raise AssertionError('Journal change did not reach the update panel')
    def write(phase):
        journal.write(dict(operation_id='a'*32,phase=phase,app={'state':'current'},proxy={}))
    try:
        for _ in range(3):
            panel.timer.timeout.emit();panel.refresh();panel.proxy_status({'update':{'phase':'off'}})
        assert not reads
        write('complete');settle(lambda:panel.record.get('phase')=='complete')
        for phase in ('failed','complete'):
            write(phase);settle(lambda:panel.record.get('phase')==phase)
        journal.path.unlink();settle(lambda:not panel.record)
        write('partial');settle(lambda:panel.record.get('phase')=='partial')
        journal.path.unlink();settle(lambda:not panel.record)
        journal.root.rmdir()
        write('complete');settle(lambda:panel.record.get('phase')=='complete')
        write('failed');settle(lambda:panel.record.get('phase')=='failed')
        reads.clear()
        for _ in range(3):panel.timer.timeout.emit()
        assert not reads and panel.execute.isVisible()
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_hidden_update_panel_recovers_after_owner_exit_without_journal_change(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor import proxy_identity,update_panel
    from cachemonitor.update_state import UpdateState
    import time
    app=QApplication.instance() or QApplication([])
    journal=UpdateState(tmp_path/'updates');record=journal.begin('app','fixture')
    record.update(phase='downloading',updated_at=0,owner={'pid':1})
    journal.path.write_text(json.dumps(record),encoding='utf-8')
    alive=[True];now=[100.];calls=[];reconcile=app_update.reconcile_update
    monkeypatch.setattr(proxy_identity,'same_process',lambda owner:alive[0])
    monkeypatch.setattr(update_panel,'time',SimpleNamespace(monotonic=lambda:now[0]))
    def recover(*args):
        calls.append(True);return reconcile(*args)
    monkeypatch.setattr(app_update,'reconcile_update',recover)
    panel=update_panel.UpdatePanel(SimpleNamespace(directory=journal.root));panel.hide()
    def settle(predicate):
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            app.processEvents()
            if predicate():return
            QTest.qWait(5)
        raise AssertionError('Hidden update recovery did not finish')
    try:
        settle(lambda:panel.operation is None)
        assert panel.record['phase']=='downloading' and len(calls)==1
        panel.refresh();assert len(calls)==1
        alive[0]=False;now[0]+=31;panel.recovery_timer.start(0)
        settle(lambda:panel.operation is None and panel.record.get('phase')=='interrupted')
        assert len(calls)==2 and journal.read()['phase']=='interrupted' and not panel.isVisible()
    finally:
        panel.stop()
        if panel.operation:panel.operation.wait()
        panel.deleteLater();app.processEvents()


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
    import time
    app=QApplication.instance() or QApplication([])
    panel=UpdatePanel(None);calls=[];gate=threading.Event()
    plan=dict(kind=kind,release=release(),connections=3)
    monkeypatch.setattr(app_update,'check_update',lambda *a:plan)
    def install(*a,**kw):
        calls.append(kw['plan']);gate.wait(2);return '설치 시작'
    monkeypatch.setattr(app_update,'update',install)
    def settle(predicate=lambda:panel.operation is None):
        deadline=time.monotonic()+1.5
        while time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5)
            if predicate():return
        raise AssertionError('Update operation did not reach the expected state')
    try:
        panel.start();settle()
        assert not calls and panel.execute.isVisible() and not panel.button.isVisible()
        if closing:panel.owner=SimpleNamespace(_closing=True)
        panel.request_install();panel.request_install();panel.start()
        if closing:assert panel.operation is None
        else:settle(lambda:bool(calls))
        assert calls==([] if closing else [plan])
        gate.set();settle()
        assert calls==([] if closing else [plan])
    finally:
        gate.set()
        if panel.operation:panel.operation.wait()
        panel.deleteLater();app.processEvents()


@pytest.mark.parametrize('health,expected', [({},None),({'active_connections':0},0),
    ({'active_connections':5},5),({'active_connections':-1},None),({'active_connections':True},None)])
def test_connection_count_preserves_unknown(health,expected):
    assert app_update.connection_count({'health':health})==expected



@pytest.mark.parametrize('phase',['failed','interrupted'])
def test_failed_app_download_can_offer_and_start_retry(tmp_path,monkeypatch,phase):
    import time
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    from cachemonitor.quick_runtime import QuickHost
    from cachemonitor.quick_qa import control
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    host=QuickHost();host.resize(640,240);host.setCentralWidget(panel);host.show()
    try:
        op=panel.journal.begin('app','fixture')['operation_id']
        panel.journal.change(op,phase=phase,message='Remote end closed connection without response')
        panel.refresh()
        assert panel.button.isVisible() and panel.button.isEnabled()
        plan=dict(kind='app',release=release('v2099.01.01.1'),connections=0)
        panel.checking=True;panel.receive_result(plan);panel.finished();panel.refresh()
        assert panel.execute.isVisible() and panel.execute.isEnabled()
        assert panel.offer is plan and panel.version.isVisible()
        assert panel.journal.read()['phase']==phase
        calls=[]
        monkeypatch.setattr(panel,'launch_operation',lambda action:calls.append(action))
        button=control(host,panel.execute)
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            app.processEvents();host.quick.grabFramebuffer()
            if button.isVisible() and button.isEnabled() and button.width()>0 and button.height()>0:break
            QTest.qWait(5)
        assert button.isVisible() and button.isEnabled() and button.width()>0 and button.height()>0
        point=button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint()
        assert host.quick.rect().contains(point)
        QTest.mouseClick(host.quick,Qt.LeftButton,pos=point)
        deadline=time.monotonic()+2
        while not calls and time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5)
        assert len(calls)==1
    finally:
        panel.stop();host.release_scene();host.close();host.deleteLater();app.processEvents()


def test_restart_replays_saved_update_failure_to_error_registry(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    from cachemonitor.update_state import UpdateState
    from cachemonitor.app_errors import ErrorState
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    journal=UpdateState(tmp_path/'updates');op=journal.begin('app','fixture')['operation_id']
    journal.fail(op,'saved network failure')
    panel=UpdatePanel(SimpleNamespace(directory=journal.root))
    class Receiver:
        error_state=ErrorState()
        receive_update_error=Dashboard.receive_update_error
        def observe_error(self,source,kind,facts,**kwargs):self.error_state.update(source,kind,facts,**kwargs)
        def refresh_errors(self):pass
    receiver=Receiver();panel.fault_changed.connect(receiver.receive_update_error)
    try:
        panel.refresh()
        assert receiver.error_state.grouped()['update'].code=='install'
        count=len(receiver.error_state.history);panel.refresh()
        assert len(receiver.error_state.history)==count
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_retry_progress_replaces_old_operation_fault_but_keeps_check_failure(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    from cachemonitor.app_errors import ErrorState
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    class Receiver:
        error_state=ErrorState()
        receive_update_error=Dashboard.receive_update_error
        def observe_error(self,source,kind,facts,**kwargs):self.error_state.update(source,kind,facts,**kwargs)
        def refresh_errors(self):pass
    receiver=Receiver();panel.fault_changed.connect(receiver.receive_update_error)
    try:
        op=panel.journal.begin('app','fixture')['operation_id'];panel.journal.fail(op,'old error');panel.refresh()
        panel.checking=False;panel.receive_result(ConnectionError('old download error'))
        receiver.receive_update_error(dict(checking_failed=True))
        op=panel.journal.begin('app','fixture')['operation_id'];panel.journal.change(op,phase='downloading');panel.refresh()
        assert set(receiver.error_state.sources)=={'update.check'}
        assert receiver.error_state.grouped()['update'].code=='check'
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_newer_app_offer_does_not_retry_previous_proxy_stage(tmp_path,monkeypatch):
    import sys
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    calls=[]
    monkeypatch.setattr(app_update,'update',lambda *a,**kw:calls.append(('app',kw['plan'])))
    monkeypatch.setattr(app_update,'retry_proxy',lambda *a,**kw:calls.append(('proxy',None)))
    monkeypatch.setattr(panel,'launch_operation',lambda action:action())
    try:
        op=panel.journal.begin('app','2026.01.01.1')['operation_id']
        panel.journal.change(op,phase='partial',app=dict(state='verified',executable=sys.executable),proxy={'phase':'failed'})
        panel.refresh()
        plan=dict(kind='app',release={'tag_name':'v2099.01.01.1'},connections=0)
        panel.show_plan(plan);panel.refresh()
        assert panel.version.isVisible()
        panel.request_install()
        assert calls==[('app',plan)]
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_offer_click_cannot_dispatch_a_different_operation(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'));calls=[]
    monkeypatch.setattr(panel,'launch_operation',lambda action:calls.append(action))
    try:
        op=panel.journal.begin('app','fixture')['operation_id'];panel.journal.fail(op,'old failure')
        panel.refresh();panel.show_plan(dict(kind='app',release=release('v2099.01.01.1')))
        other=panel.journal.begin('proxy','fixture')['operation_id'];panel.journal.fail(other,'other failure')
        panel.request_install()
        assert panel.record['operation_id']==other and not calls and panel.offer is None
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_obsolete_successful_check_resolves_only_its_check_fault(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'));events=[]
    panel.fault_changed.connect(events.append)
    try:
        op=panel.journal.begin('app','fixture')['operation_id'];panel.journal.fail(op,'old failure');panel.refresh()
        panel.checking=True;panel._check_operation_id=op
        other=panel.journal.begin('proxy','fixture')['operation_id'];panel.journal.change(other,phase='complete')
        panel.receive_result(dict(kind='none'))
        assert any(facts.get('check_healthy') for facts in events)
        panel.finished()
        assert panel.offer is None and panel.record['operation_id']==other
    finally:panel.stop();panel.deleteLater();app.processEvents()


@pytest.mark.parametrize('same_operation',[True,False])
def test_delayed_proxy_status_cannot_block_check_after_completion(tmp_path,monkeypatch,same_operation):
    import time
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'));calls=[]
    monkeypatch.setattr(panel,'launch_operation',lambda action:calls.append(action))
    try:
        op=panel.journal.begin('proxy','fixture')['operation_id']
        waiting=dict(phase='waiting',operation_id=op,updated_at=time.time())
        panel.journal.proxy_result(op,dict(phase='complete',operation_id=op,updated_at=time.time()))
        if not same_operation:
            new=panel.journal.begin('proxy','newer')['operation_id']
            panel.journal.proxy_result(new,dict(phase='complete',operation_id=new,updated_at=time.time()))
        panel.refresh();panel.proxy_status(dict(update=waiting))
        panel.start()
        assert panel.record['phase']=='complete' and len(calls)==1
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_fresh_check_error_survives_previous_verified_install(tmp_path,monkeypatch):
    from urllib.error import URLError
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    try:
        op=panel.journal.begin('app','fixture')['operation_id']
        panel.journal.change(op,phase='complete',app={'state':'verified'})
        panel.refresh();before=panel.journal.path.read_bytes()
        panel.checking=True;panel.receive_result(URLError(TimeoutError('current check failure')))
        current=panel.status.text();panel.finished();panel.refresh()
        assert panel.status.text()==current and panel.button.isVisible() and panel.button.isEnabled()
        assert panel.journal.path.read_bytes()==before
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_manual_metadata_reserves_worker_slot_against_recovery(tmp_path,monkeypatch):
    import threading,time
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'));gate=threading.Event();prepared=[]
    def reconcile(journal,*args):gate.wait(2);return journal.read()
    monkeypatch.setattr(app_update,'reconcile_update',reconcile)
    monkeypatch.setattr(app_update,'check_update',lambda *a,**kw:prepared.append(kw['release']) or dict(kind='none'))
    panel.monitor=SimpleNamespace(check_manually=lambda:None)
    try:
        panel.start();assert panel.metadata_pending
        panel.journal.begin('app','fixture');panel.refresh()
        assert panel.operation is None and panel.checking
        panel.metadata_received(release());gate.set()
        deadline=time.monotonic()+3
        while panel.operation is not None and time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5)
        assert prepared==[release()] and not panel.metadata_pending
        assert not panel.execute.isVisible()
    finally:
        gate.set();panel.stop()
        if panel.operation:panel.operation.wait()
        panel.deleteLater();app.processEvents()


def test_proxy_retry_exception_preserves_verified_app_in_fault(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    from cachemonitor.app_errors import ErrorState
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    class Receiver:
        error_state=ErrorState()
        receive_update_error=Dashboard.receive_update_error
        def observe_error(self,source,kind,facts,**kwargs):self.error_state.update(source,kind,facts,**kwargs)
        def refresh_errors(self):pass
    receiver=Receiver();panel.fault_changed.connect(receiver.receive_update_error)
    try:
        op=panel.journal.begin('proxy','fixture')['operation_id']
        panel.journal.fail(op,'proxy start failure');panel.refresh()
        panel.checking=False;panel.receive_result(ConnectionError('proxy start failure'));panel.finished()
        assert {fault.code for fault in receiver.error_state.sources.values()}=={'proxy'}
        assert {record['error_code'] for record in receiver.error_state.history}=={'proxy'}
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_update_widget_does_not_persist_inferred_interruption(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    try:
        op=panel.journal.begin('proxy','fixture')['operation_id']
        panel.journal.proxy_result(op,dict(phase='waiting'))
        before=panel.journal.path.read_bytes()
        monkeypatch.setattr(panel.journal,'proxy_result',lambda *a,**k:pytest.fail('Widget persisted inferred terminal result'))
        panel.proxy_status(dict(update=dict(phase='interrupted',operation_id=op)))
        assert panel.journal.path.read_bytes()==before and panel.journal.read()['phase']=='waiting'
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_exit_preparation_blocks_update_actions_and_keeps_journal_refresh(tmp_path,monkeypatch):
    from PySide6.QtWidgets import QApplication
    from types import SimpleNamespace
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    panel.owner=SimpleNamespace(_closing=True)
    try:
        op=panel.journal.begin('proxy','fixture')['operation_id']
        panel.journal.change(op,phase='complete')
        panel.set_exit_pending(True)
        assert not panel.button.isEnabled()
        panel.journal.change(op,phase='failed',message='durable update failure')
        panel.refresh()
        assert panel.record['phase']=='failed' and panel.status.text()=='durable update failure'
        assert not panel.execute.isEnabled() and not panel.cancel.isEnabled()
        monkeypatch.setattr(panel,'launch_operation',lambda *a:pytest.fail('Mutation started during exit preparation'))
        panel.request_install();panel.start();panel.cancel_update(True)
        panel.owner._closing=False;panel.set_exit_pending(False)
        assert panel.execute.isEnabled()
    finally:panel.stop();panel.deleteLater();app.processEvents()



def test_verified_gui_continuation_emits_only_after_validation_worker_finishes(tmp_path,monkeypatch):
    import time
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'))
    target=str(tmp_path/'versions'/'new'/'Codexon.exe');calls=[]
    try:
        record=panel.journal.begin('app','fixture');op=record['operation_id']
        panel.journal.change(op,phase='partial',app=dict(state='verified',executable=target))
        monkeypatch.setattr(app_update,'continuation_target',lambda *a:dict(kind='continue_app',executable=target,verified_operation_id=op))
        panel.continue_app_requested.connect(lambda executable:calls.append((executable,panel.operation)))
        panel.refresh();panel.request_install()
        deadline=time.monotonic()+2
        while not calls and time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5)
        assert calls==[(dict(executable=target,verified_operation_id=op),None)]
    finally:
        panel.stop()
        if panel.operation:panel.operation.wait()
        panel.deleteLater();app.processEvents()



@pytest.mark.parametrize('metadata_error',[False,True])
def test_metadata_arriving_during_exit_is_processed_after_cancel(tmp_path,monkeypatch,metadata_error):
    import time
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([]);panel=UpdatePanel(None)
    panel.owner=SimpleNamespace(_closing=True)
    calls=[]
    monkeypatch.setattr(app_update,'check_update',lambda *a,**k:calls.append(k) or dict(kind='none'))
    def settle():
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            app.processEvents()
            if panel.operation is None:return
            QTest.qWait(5)
        raise AssertionError('Metadata recovery did not finish')
    try:
        panel.checking=True;panel.metadata_pending=True;panel.button.setEnabled(False)
        panel.set_exit_pending(True)
        panel.metadata_received(RuntimeError('metadata failure') if metadata_error else release())
        assert panel.metadata_pending and panel.operation is None and not calls
        panel.owner._closing=False;panel.set_exit_pending(False);settle()
        assert not panel.metadata_pending and panel.button.isEnabled()
        assert len(calls)==int(not metadata_error)
        panel.start();settle()
        assert len(calls)==int(not metadata_error)+1 and panel.button.isEnabled()
    finally:
        panel.stop()
        if panel.operation:panel.operation.wait()
        panel.deleteLater();app.processEvents()


def test_new_gui_continuation_consumes_only_explicit_matching_intent(tmp_path,monkeypatch):
    import time
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.update_panel import UpdatePanel
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
    panel=UpdatePanel(SimpleNamespace(directory=tmp_path/'updates'));calls=[]
    def retry(manager,journal,**kwargs):calls.append(kwargs);return journal.read()
    monkeypatch.setattr(app_update,'retry_proxy',retry)
    try:
        op=panel.journal.begin('app','fixture')['operation_id']
        panel.journal.change(op,phase='deferred',app=dict(state='verified',executable=str(tmp_path/'Codexon.exe')))
        panel.refresh();app.processEvents()
        assert not calls
        assert not panel.continue_update('f'*32) and not calls
        assert panel.continue_update(op)
        assert not panel.continue_update(op)
        deadline=time.monotonic()+2
        while panel.operation and time.monotonic()<deadline:
            app.processEvents();QTest.qWait(5)
        assert panel.operation is None and calls==[dict(verified_operation_id=op)]
        panel.refresh();assert not panel.continue_update(op) and len(calls)==1
    finally:
        panel.stop()
        if panel.operation:panel.operation.wait()
        panel.deleteLater();app.processEvents()
