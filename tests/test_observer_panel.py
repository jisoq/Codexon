from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThread
from PySide6.QtTest import QTest
from cachemonitor.observer_panel import ObserverPanel


def settle(app,panel):
    for _ in range(200):
        app.processEvents();QTest.qWait(10)
        if not panel.busy() and panel.pending is None:return
    raise AssertionError('Proxy operation did not finish')


def test_opening_panel_resumes_app_owned_services_once(tmp_path,monkeypatch):
    from cachemonitor.observer_control import ObserverManager
    app=QApplication.instance() or QApplication([])
    calls=[]
    monkeypatch.setattr(ObserverManager,'ensure',lambda self:(calls.append('ensure') or {'configured':False,'phase':'off'}))
    monkeypatch.setattr(ObserverManager,'resume',lambda self:(calls.append('resume') or {'configured':False,'phase':'off'}))
    panel=ObserverPanel(tmp_path/'home',tmp_path/'data',active=True)
    try:
        settle(app,panel)
        assert calls==[]  # The app owns startup; a panel never starts a service.
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_single_switch_runs_off_gui_thread_and_recovers_inconsistent_state(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=ObserverPanel(tmp_path/'home',tmp_path/'data',active=False)
    panel.active=True;panel.update_controls()
    calls=[]
    def activate():
        assert QThread.currentThread()!=app.thread()
        calls.append('on');return {'configured':True,'phase':'active'}
    panel.manager.turn_on=activate
    panel.manager.turn_off=lambda:(calls.append('off') or {'configured':False,'phase':'off'})
    try:
        panel.display({'configured':False,'phase':'off'})
        assert not hasattr(panel,'test_button')
        panel.toggle.setChecked(True);settle(app,panel)
        assert calls==['on'] and panel.toggle.isChecked()
        panel.display({'error':'복원 확인 필요','observer_status':{'configured':True,'phase':'recovery_required'}})
        assert panel.toggle.isEnabled() and panel.toggle.isChecked()
        panel.toggle.setChecked(False);settle(app,panel)
        assert calls==['on','off'] and not panel.toggle.isChecked()
    finally:panel.stop();panel.deleteLater()


def test_switch_off_during_activation_cancels_then_reconciles_off(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=ObserverPanel(tmp_path/'home',tmp_path/'data',active=False)
    panel.active=True;panel.update_controls();calls=[]
    def activate():
        panel.manager.cancelled.wait(2)
        assert panel.manager.cancelled.is_set()
        calls.append('cancelled')
        raise RuntimeError('취소됨')
    panel.manager.turn_on=activate
    panel.manager.status=lambda:{'configured':False,'phase':'off'}
    panel.manager.turn_off=lambda:(calls.append('off') or {'configured':False,'phase':'off'})
    try:
        panel.toggle.setChecked(True);QTest.qWait(30)
        panel.toggle.setChecked(False);settle(app,panel)
        assert calls==['cancelled','off']
        assert not panel.toggle.isChecked()
    finally:panel.stop();panel.deleteLater()

def test_unified_update_button_render_and_recovery_remains_available(tmp_path,monkeypatch):
    from cachemonitor.quick_qa import mount,control,click,dispose
    from cachemonitor.fonts import load_bundled_fonts
    from PySide6.QtGui import QFont
    app=QApplication.instance() or QApplication([])
    load_bundled_fonts();app.setFont(QFont('Pretendard JP',10))
    from cachemonitor.update_panel import UpdatePanel
    from cachemonitor.observer_control import ObserverManager
    panel=UpdatePanel(ObserverManager(tmp_path/'home',tmp_path/'data'))
    calls=[]
    initial={'configured':True,'phase':'active','version_mismatch':True,'health':{'version':'old'}}
    waiting={**initial,'update':{'phase':'waiting','message':'진행 중 응답 및 캐시 작업 정산 대기'}}
    monkeypatch.setattr('cachemonitor.app_update.check_update',lambda progress,manager:dict(kind='proxy',manager=manager))
    monkeypatch.setattr('cachemonitor.app_update.update',lambda progress,manager,**kw:(calls.append('update') or '설치 시작'))
    monkeypatch.setattr('cachemonitor.update_panel.open_recovery',lambda *args:calls.append('recovery'))
    panel.manager.cancel_update=lambda:(calls.append('cancel') or initial)
    panel.proxy_status(initial);host=mount(panel,680,350)
    def finish():
        for _ in range(200):
            app.processEvents();QTest.qWait(10)
            if panel.operation is None:return
        raise AssertionError('update not finished')
    try:
        click(host,control(host,panel.button));finish()
        assert calls==[] and panel.confirming
        dialog=panel.dialog
        click(dialog.host,control(dialog.host,dialog.confirm));finish()
        assert calls==['update']
        panel.proxy_status(waiting)
        assert panel.button.text()=='업데이트 취소'
        QTest.qWait(100)
        assert host.grab().save(str(tmp_path/'update-panel.png'))
        import os
        if os.environ.get('CACHEMONITOR_UPDATE_CAPTURE'):
            assert host.grab().save(os.environ['CACHEMONITOR_UPDATE_CAPTURE'])
        click(host,control(host,panel.button));finish()
        assert calls==['update','cancel']
        panel.proxy_status({**initial,'update':{'phase':'switching','message':'업데이트 중'}})
        assert panel.button.isEnabled() and panel.recovery.isEnabled()
        click(host,control(host,panel.recovery))
        assert calls[-1]=='recovery'
        panel.proxy_status({'configured':True,'phase':'active','update':{'phase':'complete','message':'업데이트 완료'}})
        assert panel.button.isEnabled()
    finally:dispose(host)
