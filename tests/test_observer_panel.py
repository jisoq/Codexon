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


def test_intentional_off_is_distinct_from_failed_activation(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=ObserverPanel(tmp_path/'home',tmp_path/'data',active=False)
    try:
        panel.display({'configured':True,'phase':'active','probe_state':'healthy','runtime':{'phase':'ready'}})
        assert panel.status_label.text()=='실행 중'
        from PySide6.QtGui import QColor
        assert 80<QColor(panel.status_label.state['color']).hue()<160
        off={'configured':False,'phase':'off','registration':{'autostart':False}}
        panel.display(off)
        assert panel.status_label.text()=='꺼짐'
        assert QColor(panel.status_label.state['color']).saturation()==0
        assert not hasattr(panel,'connection_details')
        panel.display({'error':'synthetic failure','observer_status':{**off,'phase':'failed'}})
        assert panel.status_label.text()=='켜지 못했습니다'
        assert panel.error_detail.isVisible()
        panel.display({'error':'synthetic settings failure','observer_status':off})
        assert panel.status_label.text()=='프록시 설정 변경 실패'
        panel.display(off)
        assert not panel.error_detail.isVisible()
        assert panel.status_label.text()=='꺼짐'
    finally:panel.stop();panel.deleteLater();app.processEvents()

def test_background_recovery_clears_only_background_operation_failure(tmp_path):
    from cachemonitor.app_errors import ErrorState
    app=QApplication.instance() or QApplication([])
    panel=ObserverPanel(tmp_path/'home',tmp_path/'data')
    errors=ErrorState()
    panel.status_observed.connect(lambda value:errors.update('connection.operation','connection',value,
        healthy=value.get('operation_healthy',False)))
    healthy=dict(configured=False,phase='off',probe_state='refused')
    try:
        panel.display(dict(error='temporary failure',observer_status=healthy,background_operation='start'))
        assert errors.grouped()['connection'].code=='configuration'
        panel.display(dict(background_operation='ensure'))
        assert 'connection' in errors.grouped()
        panel.display(dict(healthy,background_operation='ensure'))
        assert 'connection' not in errors.grouped()
        # A failed user request cannot be declared successful by a healthy poll.
        panel.display(dict(error='setting failed',observer_status=healthy))
        panel.display(dict(healthy,background_operation='ensure'))
        assert 'connection' in errors.grouped()
    finally:panel.deleteLater();app.processEvents()


def test_failed_activation_shows_failure_instead_of_restart_guidance(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=ObserverPanel(tmp_path/'home',tmp_path/'data',active=False)
    try:
        panel.display(dict(configured=False,phase='off',restart_required=True,
            update=dict(phase='failed',message='shutdown receipt missing')))
        assert panel.error_detail.isVisible()
        assert not panel.restart_label.isVisible()
        assert not panel.guidance_heading.isVisible()
        panel.display(dict(configured=True,phase='active',probe_state='healthy'))
        connected_text=panel.status_label.text();connected_color=panel.status_label.state['color']
        panel.display(dict(configured=True,phase='active',probe_state='healthy',
            update=dict(phase='failed',message='shutdown receipt missing')))
        assert panel.status_label.text()==connected_text
        assert panel.status_label.state['color']==connected_color
        assert panel.toggle.isChecked()
        assert not panel.error_detail.isVisible()
        assert panel.progress_link.isVisible()
    finally:panel.stop();panel.deleteLater();app.processEvents()


def test_update_recheck_cannot_skip_connection_exit(tmp_path,monkeypatch):
    from cachemonitor.update_panel import UpdatePanel
    from cachemonitor.observer_control import ObserverManager
    from cachemonitor.update_state import scope_for
    app=QApplication.instance() or QApplication([])
    manager=ObserverManager(tmp_path/'home',tmp_path/'data')
    panel=UpdatePanel(manager);journal=panel.journal
    record=journal.begin('proxy','2026.10.09.1',scope=scope_for(manager));operation=record['operation_id']
    journal.proxy_result(operation,dict(phase='waiting',required_action='close_client',unknown_connections=1))
    calls=[];manager.cancel_update=lambda:calls.append('cancel') or {}
    manager.status=lambda:{'configured':True,'update':dict(phase='waiting',required_action='close_client',unknown_connections=1)}
    try:
        panel.refresh()
        assert panel.recheck.isVisible() and panel.later.isVisible() and not panel.button.isVisible()
        assert panel.button.text()=='업데이트 확인'
        panel.recheck.clicked.emit()
        for _ in range(100):
            app.processEvents();QTest.qWait(5)
            if panel.operation is None:break
        assert journal.read()['phase']=='needs_exit' and not calls
        panel.cancel_update(True)
        import time
        deadline=time.monotonic()+5
        while panel.operation is not None:
            assert time.monotonic()<deadline
            app.processEvents();QTest.qWait(10)
        assert calls==['cancel'] and journal.read()['defer_requested']
        journal.proxy_result(operation,dict(phase='cancelled'))
        panel.refresh();assert panel.execute.isVisible()
        journal.change(operation,phase='switching',proxy={'cutover_started':True})
        panel.refresh();panel.cancel_update(False)
        assert calls==['cancel'] and not panel.later.isVisible() and not panel.cancel.isVisible()
    finally:
        import time
        deadline=time.monotonic()+5
        while panel.operation is not None:
            assert time.monotonic()<deadline
            app.processEvents();QTest.qWait(10)
        panel.stop();panel.deleteLater();app.processEvents()


def test_requested_recording_and_live_connection_have_distinct_states(tmp_path):
    from PySide6.QtGui import QColor
    from cachemonitor.connection_recovery import assess
    app=QApplication.instance() or QApplication([])
    panel=ObserverPanel(tmp_path/'home',tmp_path/'data',active=True)
    try:
        healthy=dict(configured=True,phase='active',probe_state='healthy')
        panel.display(healthy)
        healthy_color=panel.status_label.state['color'];healthy_text=panel.status_label.text()
        panel.display({**healthy,'probe_state':'unknown'})
        waiting_color=panel.status_label.state['color']
        assert panel.toggle.isChecked() and waiting_color!=healthy_color
        assert panel.status_label.text()!=healthy_text
        panel.display({**healthy,'update':dict(phase='waiting',responding=1)})
        assert panel.status_label.state['color']==healthy_color
        assert panel.status_label.text()==healthy_text and panel.progress_link.isVisible()
        assert assess(panel.last_result)['code']=='responding'
        panel.display(dict(configured=False,phase='draining',health=dict(draining=True)))
        assert not panel.toggle.isChecked() and panel.status_label.state['color']==waiting_color
        assert assess(panel.last_result)['code']=='draining'
        panel.display(dict(configured=False,phase='off'))
        assert not panel.toggle.isChecked()
        assert QColor(panel.status_label.state['color']).saturation()==0
        panel.display({**healthy,'phase':'preparing','service_phase':'preparing'})
        assert assess(panel.last_result)['can_recover'] is False
        assert panel.status_label.state['color']==waiting_color
        panel.set_exit_pending(True);assert not panel.toggle.isEnabled()
        panel.set_exit_pending(False);assert panel.toggle.isEnabled()
    finally:panel.stop();panel.deleteLater();app.processEvents()
