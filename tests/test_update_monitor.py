"""Release scheduling and GUI contracts using a local asynchronous HTTP server."""
import json
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtNetwork import QHostAddress, QTcpServer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from PySide6.QtTest import QTest

from cachemonitor import app_update, update_monitor


def release(tag='v2099.01.01.1'):
    prefix='https://github.com/jisoq/Codexon/releases/download/'+tag+'/'
    return dict(tag_name=tag,assets=[
        dict(name='Codexon-Setup.exe',browser_download_url=prefix+'Codexon-Setup.exe',size=7),
        dict(name='Codexon-Setup.exe.sha256',browser_download_url=prefix+'Codexon-Setup.exe.sha256')])


def settle(predicate):
    until=time.monotonic()+3
    while not predicate() and time.monotonic()<until:QTest.qWait(5)
    assert predicate()


@pytest.fixture
def endpoint(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    server=QTcpServer();assert server.listen(QHostAddress.LocalHost,0)
    state=SimpleNamespace(app=app,requests=[],responses=[],sockets=[],now=1_000_000.,monitors=[])
    state.settings=QSettings(str(tmp_path/'updates.ini'),QSettings.IniFormat)
    def accepted():
        socket=server.nextPendingConnection();state.sockets.append(socket);buffer=bytearray()
        def read():
            buffer.extend(bytes(socket.readAll()))
            if b'\r\n\r\n' not in buffer:return
            socket.readyRead.disconnect(read)
            state.requests.append(bytes(buffer))
            response=state.responses.pop(0) if state.responses else None
            if response is None:return  # Intentionally stalled request.
            status,headers,body=response
            body=json.dumps(body).encode() if isinstance(body,dict) else body
            header=f'HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\nConnection: close\r\n'
            header+=''.join(f'{key}: {value}\r\n' for key,value in headers.items())
            socket.write(header.encode()+b'\r\n'+body);socket.disconnectFromHost()
        socket.readyRead.connect(read)
    server.newConnection.connect(accepted)
    monkeypatch.setattr(update_monitor,'RELEASE_URL',f'http://127.0.0.1:{server.serverPort()}/latest')
    def make():
        monitor=update_monitor.UpdateMonitor(state.settings,clock=lambda:state.now,jitter=lambda low,high:low)
        state.monitors.append(monitor);return monitor
    state.make=make
    yield state
    for monitor in state.monitors:monitor.stop();monitor.deleteLater()
    for socket in state.sockets:socket.abort()
    server.close();server.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def test_day_schedule_restart_sleep_and_manual_share_one_request(endpoint,monkeypatch):
    e=endpoint
    # Metadata checks cannot perform any of these synchronous side effects.
    for name in ('inspect_proxy','read_url','launch_installer','installed'):
        monkeypatch.setattr(app_update,name,lambda *a,**k:pytest.fail('Automatic check touched local services/downloads'))
    monkeypatch.setattr('cachemonitor.index.UsageIndex',lambda *a,**k:pytest.fail('Automatic database scan'))
    monitor=e.make();monitor.start();assert monitor.timer.isSingleShot()
    assert monitor.timestamp('nextCheck')==e.now+15
    beginning=e.now
    for index in range(4):
        e.now=monitor.timestamp('nextCheck')
        # Real Qt transport is stalled long enough to join the pending request.
        monitor.due();settle(lambda:len(e.requests)==index+1)
        result=[];monitor.manual_finished.connect(result.append)
        monitor.check_manually();monitor.due()
        assert len(e.requests)==index+1
        socket=e.sockets[-1];body=json.dumps(release()).encode()
        socket.write(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
        socket.disconnectFromHost();settle(lambda:bool(result))
        assert result[0]['tag_name']=='v2099.01.01.1'
        due=monitor.timestamp('nextCheck');monitor.stop()
        monitor=e.make();monitor.start();assert monitor.timestamp('nextCheck')==due
        monitor.due();assert len(e.requests)==index+1
    e.now=beginning+86400;monitor.due();assert len(e.requests)==4
    # Resume after multiple missed intervals: one request, no catch-up burst.
    e.now+=3*86400;e.responses.append((200,{},release()))
    monitor.due();settle(lambda:len(e.requests)==5 and monitor.reply is None)
    monitor.due();assert len(e.requests)==5


def test_etag_cached_304_and_current_version(endpoint):
    e=endpoint;monitor=e.make();found=[];monitor.available.connect(found.append)
    e.responses.append((200,{'ETag':'"release-one"'},release()))
    monitor.check_manually();settle(lambda:monitor.reply is None)
    assert found[-1]['tag_name']=='v2099.01.01.1'
    assert monitor.timestamp('lastSuccess')==e.now
    monitor.stop();monitor=e.make();monitor.available.connect(found.append)
    e.responses.append((304,{},b''));monitor.check_manually();settle(lambda:monitor.reply is None)
    assert b'If-None-Match: "release-one"' in e.requests[-1]
    assert found[-1]['tag_name']=='v2099.01.01.1'
    e.responses.append((200,{},release('v'+app_update.VERSION)))
    monitor.check_manually();settle(lambda:monitor.reply is None)
    assert found[-1] is None


@pytest.mark.parametrize('bad', ['json','deep-json','tag','draft','assets','size','digest','source','mismatched-tag','304'])
def test_invalid_cache_and_release_never_offer_update(endpoint,bad):
    e=endpoint;e.settings.setValue('updates/cache','{"etag":"bad","release":{"tag_name":"v2099.01.01.1"}}')
    value=release();status=200
    if bad=='json':value=b'broken json'
    elif bad=='deep-json':
        value=('['*2000+'0'+']'*2000).encode()
        e.settings.setValue('updates/cache',value.decode())
    elif bad=='tag':value['tag_name']='unstable'
    elif bad=='draft':value['draft']=True
    elif bad=='assets':value['assets']=[None]
    elif bad=='size':value['assets'][0]['size']=True
    elif bad=='digest':value['assets'][0]['digest']='md5:bad'
    elif bad=='source':value['assets'][0]['browser_download_url']='https://evil.example/installer.exe'
    elif bad=='mismatched-tag':value['tag_name']='v2099.02.01.1'
    else:status=304;value=b''
    monitor=e.make();found=[];results=[]
    monitor.available.connect(found.append);monitor.manual_finished.connect(results.append)
    e.responses.append((status,{},value));monitor.check_manually();settle(lambda:bool(results))
    assert isinstance(results[0],Exception) and not any(found)
    assert b'If-None-Match' not in e.requests[0]
    assert monitor.timestamp('lastSuccess')==0
    assert monitor.timestamp('nextCheck')==e.now+21600


@pytest.mark.parametrize('headers',[
    {'Retry-After':'90000'},
    {'X-RateLimit-Remaining':'0','X-RateLimit-Reset':'1090000'},
    {'Retry-After':'Tue, 13 Jan 1970 14:46:40 GMT'},
])
def test_rate_limit_persists_and_gates_manual_requests(endpoint,headers):
    e=endpoint;monitor=e.make();results=[];monitor.manual_finished.connect(results.append)
    e.responses.append((429,headers,b''));monitor.check_manually();settle(lambda:bool(results))
    assert monitor.timestamp('retryAfter')==e.now+90000
    monitor.stop();monitor=e.make();monitor.start();monitor.check_manually();monitor.due()
    assert monitor.reply is None and len(e.requests)==1
    e.now+=90001;e.responses.append((200,{},release()))
    monitor.due();settle(lambda:len(e.requests)==2 and monitor.reply is None)


def test_slow_request_responsive_cancel_cleanup_and_manual_ownership(endpoint):
    from shiboken6 import isValid
    e=endpoint;monitor=e.make();monitor.start();e.now+=15;monitor.due()
    settle(lambda:len(e.requests)==1)
    reply,network=monitor.reply,monitor.network
    assert monitor.deadline.interval()==10000
    # Event-loop responsiveness during the deliberately stalled network request.
    from PySide6.QtCore import QTimer
    fired=[];QTimer.singleShot(0,lambda:fired.append(True));settle(lambda:bool(fired))
    started=time.perf_counter();monitor.set_enabled(False)
    assert time.perf_counter()-started<.25 and monitor.reply is None and not monitor.timer.isActive()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
    assert not isValid(reply) and not isValid(network)
    monitor.set_enabled(True);e.now=monitor.timestamp('nextCheck');monitor.due()
    settle(lambda:len(e.requests)==2);monitor.check_manually();monitor.set_enabled(False)
    assert monitor.reply is not None  # The explicit request now owns the shared transport.
    results=[];monitor.manual_finished.connect(results.append);monitor.timed_out()
    settle(lambda:bool(results));assert isinstance(results[0],TimeoutError)
    assert monitor.reply is None and not monitor.timer.isActive()
    monitor.check_manually();settle(lambda:len(e.requests)==3)
    started=time.perf_counter();monitor.stop()
    assert time.perf_counter()-started<.25 and monitor.reply is None


def test_disconnect_and_oversized_response_wait_until_next_cycle(endpoint):
    e=endpoint;monitor=e.make();monitor.start();e.now+=15;monitor.due()
    settle(lambda:len(e.requests)==1)
    # A truncated HTTP response exercises an actual mid-transfer connection loss.
    e.sockets[-1].write(b'HTTP/1.1 200 OK\r\nContent-Length: 900\r\n\r\n{')
    e.sockets[-1].disconnectFromHost()
    settle(lambda:monitor.reply is None)
    monitor.due();assert len(e.requests)==1 and monitor.timestamp('lastSuccess')==0
    e.now=monitor.timestamp('nextCheck')
    e.responses.append((200,{},b'x'*(update_monitor.MAX_BODY+1)))
    monitor.due();settle(lambda:monitor.reply is None)
    assert monitor.cache is None and monitor.timestamp('lastSuccess')==0


def test_production_gate_excludes_source_qa_and_other_installations(tmp_path,monkeypatch):
    import sys
    from cachemonitor import installation
    exe=tmp_path/'versions'/'1'/'Codexon.exe';exe.parent.mkdir(parents=True);exe.touch()
    recovery=tmp_path/'maintenance'/'CodexonRecovery.exe';recovery.parent.mkdir();recovery.touch()
    monkeypatch.setattr(installation,'installed',lambda:dict(InstallRoot=str(tmp_path),AppPath=str(exe),RecoveryPath=str(recovery)))
    monkeypatch.setattr(sys,'executable',str(exe));monkeypatch.setattr(sys,'frozen',False,raising=False)
    assert not update_monitor.production_updates_allowed()
    monkeypatch.setattr(sys,'frozen',True)
    assert update_monitor.production_updates_allowed()
    assert not update_monitor.production_updates_allowed(isolated=True)
    (tmp_path/'installation.json').write_text('{"isolated":true}')
    assert not update_monitor.production_updates_allowed()
    (tmp_path/'installation.json').write_text('{}')
    monkeypatch.setattr(sys,'executable',str(tmp_path/'QA.exe'))
    assert not update_monitor.production_updates_allowed()


@pytest.mark.parametrize('language',['ko','en'])
def test_dashboard_badge_notification_routes_mute_and_explicit_check(endpoint,tmp_path,monkeypatch,language):
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.i18n import set_language
    from cachemonitor.quick_qa import control,click,walk
    e=endpoint;set_language(language)
    previous=e.app.property('cachemonitorDisableShellIntegration');e.app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([],start_worker=False,settings=e.settings,live_limits=False,manage_observer=False,
                     collection_autostart=False,index_path=str(tmp_path/'index.sqlite'),quota_path=str(tmp_path/'quota.sqlite'))
    monitor=e.make();window.bind_update_monitor(monitor)
    messages=[];monkeypatch.setattr(window.tray,'showMessage',lambda *args:messages.append(args))
    status_calls=[];window.update_panel.manager=SimpleNamespace(status=lambda:status_calls.append(True) or {})
    monkeypatch.setattr(app_update,'installed',lambda:dict(InstallRoot=str(tmp_path)))
    monkeypatch.setattr(app_update,'read_url',lambda *a,**k:pytest.fail('Manual check must share release transport'))
    try:
        monitor.start();window.notification_master.setChecked(False)
        monitor.cache=release();monitor.publish()
        assert window.update_available.isVisible() and not messages
        window.notification_master.setChecked(True)
        assert len(messages)==1
        monitor.publish();assert len(messages)==1
        monitor.cache=release('v2098.01.01.1');monitor.publish()
        monitor.cache=release();monitor.publish();assert len(messages)==1
        window.show();QTest.qWait(80);click(window,control(window,window.update_available))
        assert window.settings_page.current_category()=='about'
        window.open_settings();window.tray.messageClicked.emit()
        assert window.settings_page.current_category()=='about'
        window.show_tray_notification('details','Proxy failure','Details',QSystemTrayIcon.Warning)
        window.tray.messageClicked.emit()
        assert window.settings_page.current_category()=='notifications'
        assert window.notification_details.toggle.isChecked()
        # Start auto transport, then join it through the real UpdatePanel.
        e.now=monitor.timestamp('nextCheck');monitor.due();settle(lambda:len(e.requests)==1)
        assert not status_calls
        window.open_updates();window.update_panel.start()
        assert window.update_panel.metadata_pending
        current=window.update_panel.status.text();monitor.publish()
        window.update_panel.proxy_status({'update':{'message':'must not overwrite'}})
        assert window.update_panel.status.text()==current
        assert len(e.requests)==1
        data=json.dumps(release()).encode();socket=e.sockets[-1]
        socket.write(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(data)).encode()+b'\r\nConnection: close\r\n\r\n'+data)
        socket.disconnectFromHost()
        settle(lambda:not window.update_panel.metadata_pending and window.update_panel.operation is None)
        assert window.update_panel.offer['kind']=='app' and status_calls==[True]
        from cachemonitor.i18n import tr
        settle(lambda:any(item.isVisible() and item.property('text')==tr('업데이트 가능')
                          for item in walk(control(window,window.update_panel.status))))
        assert len(messages)==2
        monitor.publish();assert window.update_panel.offer['kind']=='app'
        click(window,control(window,window.auto_update))
        assert not monitor.enabled and not monitor.timer.isActive()
        assert window.grab().save(str(tmp_path/('automatic-update-'+language+'.png')))
        # Restart with persisted notification marker cannot show it again.
        count=len(messages);monitor.stop();e.settings.setValue('updates/automatic',True)
        restarted=e.make();window.bind_update_monitor(restarted);restarted.start()
        assert window.update_available.isVisible() and len(messages)==count
        restarted.check_manually();settle(lambda:len(e.requests)==2)
        started=time.perf_counter();window.quit_app()
        assert time.perf_counter()-started<.5 and restarted.reply is None
    finally:
        monitor.stop()
        if window.update_panel.operation:window.update_panel.operation.wait()
        window.quit_app();set_language('ko');e.app.setProperty('cachemonitorDisableShellIntegration',previous)
