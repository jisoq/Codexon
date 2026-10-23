import sys
import threading
import time

import pytest

from cachemonitor import app_services,app_update,client_restart,install_management
from cachemonitor.observer_state import ProcessLock
from cachemonitor.quick_runtime import Confirmation
confirmation_open=Confirmation.open
from test_installed_startup import route_case,ready_installation,isolated_shell,no_running_desktop,settle


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('proxy_phase',['failed','complete'])
@pytest.mark.parametrize('via_panel',[False,True])
def test_manual_open_after_startup_failure_needs_one_confirmed_retry(tmp_path,route_case,monkeypatch,app,proxy_phase,via_panel):
    from PySide6.QtCore import QPointF,Qt
    from PySide6.QtTest import QTest
    from cachemonitor.quick_runtime import QuickHost
    from cachemonitor.quick_qa import control
    from cachemonitor.update_panel import UpdatePanel
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    bound=case.journal.bind_installed(case.operation_id,executable=sys.executable)
    case.journal.startup_result(case.operation_id,dict(state='starting',owner=state.gui))
    state.events.append('gui')
    owner=app_services.AppServices(state.manager,[],collection=False,installed_update=(case.journal,bound))
    original=dict(pid=901,created=21,executable='fixture-client.exe',session=1,app_id=client_restart.desktop.CODEX_APP_ID)
    manual={**original,'pid':902,'created':22}
    running=[original];closed=[];workers=[];panel=None;host=None
    monkeypatch.setattr(client_restart.desktop,'desktop_processes',lambda:list(running))
    monkeypatch.setattr(client_restart,'capture',lambda:dict(phase='authorized',targets=list(running),reopen_required=bool(running)))
    monkeypatch.setattr(Confirmation,'open',confirmation_open)
    def close(targets):
        closed.extend(targets);running.clear()
    monkeypatch.setattr(client_restart,'close_desktop',close)
    def activate(app_id):
        assert closed==[original,manual]
        assert case.journal.read()['app']['startup']['state']=='ready'
        state.events.append('client')
        running.append(dict(pid=903,created=int((time.time()+.01+11644473600)*10_000_000)))
    monkeypatch.setattr(client_restart.desktop,'_activate',activate)
    def start(journal,operation_id,recovery):
        if any(worker.is_alive() for worker in workers):return
        worker=threading.Thread(target=client_restart.run_restart,args=(journal,operation_id))
        workers.append(worker);worker.start()
    monkeypatch.setattr(client_restart,'start',start)
    def wait(predicate):
        deadline=time.monotonic()+5
        while not predicate():
            app.processEvents()
            assert time.monotonic()<deadline,'Confirmed retry did not settle'
    def click(window,node):
        item=control(window,node)
        wait(lambda:item.isVisible() and item.isEnabled() and item.width()>0 and item.height()>0)
        window.quick.grabFramebuffer()
        point=item.mapToScene(QPointF(item.width()/2,item.height()/2)).toPoint()
        assert window.quick.rect().contains(point)
        QTest.mouseClick(window.quick,Qt.LeftButton,pos=point)
    try:
        case.journal.client_result(case.operation_id,**client_restart.capture())
        start(case.journal,case.operation_id,case.recovery)
        settle(lambda:case.journal.read()['client']['phase']=='closed')
        resume=state.manager.resume
        def failed_resume():raise RuntimeError('Saved-route resume failed')
        state.manager.resume=failed_resume
        assert owner.execute('start').get('error')
        workers[0].join(5)
        assert not workers[0].is_alive()
        failed=case.journal.read()
        assert failed['client']['phase']=='blocked' and failed['client']['closed_at']
        assert closed==[original] and not running
        case.journal.proxy_result(case.operation_id,dict(phase=proxy_phase))
        running.append(manual)
        state.manager.resume=resume
        if via_panel:
            monkeypatch.setattr(UpdatePanel,'recover',lambda self:None)
            panel=UpdatePanel(state.manager);panel.journal=case.journal
            panel.reload_record(force=True);panel.render_record()
            host=QuickHost();host.resize(520,300);host.setCentralWidget(panel);host.show()
            click(host,panel.execute)
            wait(lambda:panel._restart_confirmation is not None)
            assert closed==[original]
            dialog=panel._restart_confirmation
            click(dialog.host,dialog.confirm)
            wait(lambda:closed==[original,manual] and panel.operation is None)
        else:
            app_update.retry_proxy(state.manager,case.journal,consent=client_restart.capture())
            assert closed==[original,manual]
        assert case.journal.read()['operation_id']==case.operation_id
        assert state.events==['gui']
        state.release.set()
        assert not owner.execute('ensure').get('error')
        for worker in workers:
            worker.join(5);assert not worker.is_alive()
        assert case.journal.read()['client']['phase']=='restarted'
        assert state.events==['gui','route','client']
        assert len(case.launches)==1
    finally:
        state.release.set()
        case.journal.startup_result(case.operation_id,dict(state='blocked'))
        case.journal.fail(case.operation_id,'Test cleanup')
        if panel:
            if panel._restart_confirmation:panel._restart_confirmation.reject();app.processEvents()
            panel.stop()
            if panel.operation:panel.operation.wait(5000)
        for worker in workers:worker.join(5)
        if host:host.release_scene();host.close();host.deleteLater();app.processEvents()


@pytest.mark.parametrize('failure',['busy','timeout','listen'])
def test_rejected_gui_handoff_blocks_startup_and_releases_restart_lock(tmp_path,route_case,monkeypatch,app,failure):
    from cachemonitor import app as application,app_restart
    state=route_case;case=state.case
    install_management.finish(tmp_path,case.product,case.recovery,update_id=case.operation_id)
    startup=case.journal.read()['app']['startup']
    assert startup['state']=='waiting' and not startup.get('owner')
    home=state.manager.home;home.mkdir(exist_ok=True)
    (home/'codexon-test-home').touch()
    monkeypatch.setattr(sys,'argv',['Codexon.exe','--replace-gui','--finish-install',case.operation_id,
        '--managed-services','--codex-home',str(home),'--index-path',str(tmp_path/'index.sqlite'),
        '--verify-handoff',str(tmp_path/'handoff.json')])
    monkeypatch.setattr(application,'QApplication',lambda *args:app)
    monkeypatch.setattr(application,'load_bundled_fonts',lambda:[])
    monkeypatch.setattr(application,'configure_font_rendering',lambda:None)
    monkeypatch.setattr(application,'configure_high_dpi',lambda:None)
    messages=[]
    def message(*args):
        messages.append(args)
        assert case.journal.read()['app']['startup']['state']=='blocked'
        worker.join(2)
        assert not worker.is_alive(),'Failure dialog retained the client restart lock'
    monkeypatch.setattr(application.QMessageBox,'information',message)
    monkeypatch.setattr(application.QMessageBox,'warning',message)
    class Socket:
        def connectToServer(self,name):pass
        def waitForConnected(self,timeout):return failure!='listen'
        def write(self,data):assert data==b'update-exit'
        def waitForBytesWritten(self,timeout):return True
        def bytesAvailable(self):return True
        def read(self,size):return b'busy' if failure=='busy' else b'ready-to-exit'
        def waitForDisconnected(self,timeout):return False
    class Server:
        @staticmethod
        def removeServer(name):pass
        def listen(self,name):return False
    monkeypatch.setattr(application,'QLocalSocket',Socket)
    monkeypatch.setattr(application,'QLocalServer',Server)
    monkeypatch.setattr(app_restart,'REPLACEMENT_TIMEOUT',0)
    worker=threading.Thread(target=client_restart.run_restart,args=(case.journal,case.operation_id))
    worker.start()
    try:
        assert application.main()==(1 if failure=='listen' else 4)
        worker.join(2)
        assert not worker.is_alive(),'Rejected GUI handoff retained the client restart lock'
        result=case.journal.read()
        assert messages and result['app']['startup']['state']=='blocked'
        assert result['app']['startup']['owner']==state.gui
        assert result['client']['phase']=='blocked' and result['phase']=='restart_required'
        with ProcessLock(case.journal.root/'client-restart.lock',timeout=.1):pass
        assert not state.events
    finally:
        case.journal.startup_result(case.operation_id,dict(state='blocked'))
        worker.join(5)
