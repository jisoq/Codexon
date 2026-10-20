"""Exit keeps the dashboard usable until its owner confirms completion."""
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, QThread, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from cachemonitor.app_shutdown import ShutdownOperation, ShutdownProgress
from cachemonitor.shutdown_state import ExitCancelled, ExitProgress, ExitResult


def await_condition(predicate,seconds=5):
    deadline=time.monotonic()+seconds
    while not predicate():
        if time.monotonic()>=deadline:raise AssertionError('UI condition did not become ready')
        QTest.qWait(10)


class HeldWorker(QThread):
    def __init__(self):
        super().__init__();self.entered=threading.Event();self.release=threading.Event()

    def run(self):
        self.entered.set()
        assert self.release.wait(10)


class Services:
    def __init__(self):
        self.active=True;self.login_launch=None;self.collection=False
        self.closing=threading.Event();self.calls=[]
        self.state=dict(configured=True,phase='active',probe_state='healthy')

    def execute(self,name):
        self.calls.append(name)
        if name=='turn_off':self.state=dict(configured=False,phase='off')
        return self.state

    def deactivate(self):self.active=False;self.closing.set()

    def release_exit(self,*,defer_recovery=True):
        self.active=True;self.closing.clear();self.recovery_deferred=defer_recovery


@pytest.fixture
def dashboard(tmp_path,monkeypatch):
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([str(tmp_path/'home')],start_worker=False,live_limits=False,
        index_path=str(tmp_path/'index.sqlite'),
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    services=Services();window.app_services=services
    window.service_controller.services=services;window.service_controller.timer.start()
    window.observer_panel.services=services;window.observer_panel.active=True
    window.observer_panel.display(services.state)
    finished=[];warnings=[]
    monkeypatch.setattr(window,'finish_quit',lambda:finished.append(True))
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:warnings.append(args))
    window.resize(720,620);window.show();window.open_home_connection()
    pulses=[];timer=QTimer(window);timer.setInterval(5);timer.timeout.connect(lambda:pulses.append(True));timer.start()
    value=SimpleNamespace(window=window,services=services,finished=finished,warnings=warnings,pulses=pulses)
    try:yield value
    finally:
        operation=getattr(window,'shutdown_operation',None)
        if operation and operation.isRunning():
            operation.request_cancel()
            for worker in (*operation.workers,*operation.display_workers):
                if hasattr(worker,'release'):worker.release.set()
            await_condition(lambda:not operation.isRunning())
        await_condition(lambda:window.service_controller.operation is None)
        window.service_controller.stop();window.update_panel.stop();window.tick.stop();timer.stop()
        panel=window.observer_panel
        await_condition(lambda:not panel.busy())
        panel.stop();window.quitting=True;window.close();window.deleteLater();app.processEvents()
        from cachemonitor.graphics_recovery import set_restart_enabled
        set_restart_enabled(True)
        app.setProperty('cachemonitorDisableShellIntegration',previous)


def click_button(button):
    assert button.isVisible() and button.isEnabled()
    QTest.mouseClick(button,Qt.LeftButton,pos=button.rect().center())


def test_cancel_exit_restores_controls_and_keeps_refresh(dashboard,monkeypatch):
    view=dashboard;entered=threading.Event()
    def stop(*,control,publish):
        entered.set();publish(ExitProgress('waiting','',True,False,responding=1))
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            try:control.checkpoint()
            except ExitCancelled:return ExitResult('cancelled')
            if control.cancelled:return ExitResult('cancelled')
            threading.Event().wait(.01)
        raise AssertionError('Cancellation did not reach the owner')
    monkeypatch.setattr(view.services,'stop',stop,raising=False)
    view.window.quit_app()
    dialog=view.window.shutdown_dialog
    assert isinstance(dialog,ShutdownProgress) and dialog.isVisible()
    assert not dialog.isModal() and not dialog.force.isEnabled()
    assert view.window.isEnabled() and not view.window.observer_panel.toggle.isEnabled()
    await_condition(entered.is_set)
    before=len(view.pulses);await_condition(lambda:len(view.pulses)>before)
    click_button(dialog.cancel)
    await_condition(lambda:not view.window._closing)
    assert view.window.observer_panel.toggle.isEnabled()
    assert view.window.tick.isActive() and view.window.service_controller.timer.isActive()
    assert not view.window.service_controller.closed and view.services.active
    assert not view.finished and not view.warnings
    assert not any(isinstance(child,ShutdownProgress) and child.isVisible() for child in view.window.children())


@pytest.mark.parametrize('committed',[False,True])
def test_failed_exit_leaves_rendered_connection_control_usable(dashboard,monkeypatch,committed):
    from cachemonitor.quick_qa import click,control,render_plot
    view=dashboard
    monkeypatch.setattr(view.services,'stop',lambda **kw:ExitResult('failed',committed=committed,error='synthetic'),raising=False)
    view.window.quit_app();await_condition(lambda:not view.window._closing)
    assert view.warnings and not view.finished
    assert view.window.tick.isActive() and not view.window.service_controller.closed
    await_condition(lambda:view.window.service_controller.operation is None)
    render_plot(view.window,view.window.observer_panel.toggle)
    toggle=control(view.window,view.window.observer_panel.toggle)
    assert toggle.isEnabled()
    click(view.window,toggle)
    await_condition(lambda:not view.window.observer_panel.busy())
    assert 'turn_off' in view.services.calls and not view.window.observer_panel.toggle.isChecked()
    before=len(view.pulses);await_condition(lambda:len(view.pulses)>before)


def test_handoff_waits_off_gui_thread_and_keeps_services(dashboard,monkeypatch):
    view=dashboard;worker=HeldWorker();view.window.worker=worker
    worker.start();await_condition(worker.entered.is_set)
    monkeypatch.setattr(view.services,'stop',lambda **kw:pytest.fail('Handoff stopped services'),raising=False)
    try:
        view.window.quit_app(handoff=True)
        await_condition(lambda:worker.isInterruptionRequested())
        before=len(view.pulses);await_condition(lambda:len(view.pulses)>before)
        assert not view.finished and view.services.active and not view.services.closing.is_set()
        assert view.window.tick.isActive() and view.window.shutdown_operation.isRunning()
        worker.release.set();await_condition(lambda:bool(view.finished))
        assert view.services.active and not view.services.closing.is_set()
    finally:worker.release.set();await_condition(lambda:not worker.isRunning())


def test_completed_exit_reports_unconfirmed_storage_before_closing(dashboard,monkeypatch):
    view=dashboard;events=[]
    monkeypatch.setattr(view.services,'stop',lambda **kw:
        ExitResult('completed',committed=True,warning='storage unconfirmed'),raising=False)
    monkeypatch.setattr(QMessageBox,'warning',lambda *_:events.append('warning'))
    monkeypatch.setattr(view.window,'finish_quit',lambda:events.append('closed'))
    view.window.quit_app();await_condition(lambda:'closed' in events)
    assert events==['warning','closed']


def test_force_reaches_service_before_unrelated_display_finishes(dashboard,monkeypatch):
    view=dashboard;worker=HeldWorker();view.window.worker=worker
    worker.start();await_condition(worker.entered.is_set)
    forced=threading.Event()
    def stop(*,control,publish):
        publish(ExitProgress('waiting','',True,True,responding=1))
        deadline=time.monotonic()+5
        while not control.force_requested:
            if time.monotonic()>=deadline:raise AssertionError('Force did not reach service')
            threading.Event().wait(.01)
        forced.set();control.commit();return ExitResult('completed',committed=True)
    monkeypatch.setattr(view.services,'stop',stop,raising=False)
    try:
        view.window.quit_app();await_condition(lambda:view.window.shutdown_dialog.force.isEnabled())
        click_button(view.window.shutdown_dialog.force);await_condition(forced.is_set)
        assert worker.isRunning() and not view.finished
        worker.release.set();await_condition(lambda:bool(view.finished))
    finally:worker.release.set();await_condition(lambda:not worker.isRunning())


def test_local_finalization_failure_releases_controls_without_restarting_services(dashboard,monkeypatch):
    from cachemonitor import app_shutdown
    view=dashboard;worker=HeldWorker();view.window.worker=worker
    worker.start();await_condition(worker.entered.is_set)
    original=app_shutdown.ShutdownOperation
    monkeypatch.setattr(app_shutdown,'ShutdownOperation',lambda *args,**kw:original(*args,**kw,wait_timeout=.05))
    def stop(*,control,publish):
        control.commit();view.services.deactivate()
        return ExitResult('completed',committed=True)
    monkeypatch.setattr(view.services,'stop',stop,raising=False)
    try:
        view.window.quit_app();await_condition(lambda:not view.window._closing)
        assert view.warnings and not view.finished
        assert view.services.active and not view.services.closing.is_set() and view.services.recovery_deferred
        assert view.window.observer_panel.toggle.isEnabled() and view.window.tick.isActive()
        assert 'turn_on' not in view.services.calls and worker.isRunning()
    finally:worker.release.set();await_condition(lambda:not worker.isRunning())


def test_cancel_during_settings_wait_does_not_stop_services():
    app=QApplication.instance() or QApplication([])
    worker=HeldWorker();worker.start();await_condition(worker.entered.is_set)
    calls=[];services=SimpleNamespace(stop=lambda **kw:calls.append(True))
    operation=ShutdownOperation(services,[worker],None)
    try:
        operation.start()
        assert operation.request_cancel()
        await_condition(lambda:not operation.isRunning())
        assert operation.outcome.status=='cancelled' and not calls and worker.isRunning()
    finally:
        worker.release.set();await_condition(lambda:not worker.isRunning())
        operation.deleteLater();worker.deleteLater();app.processEvents()


def test_background_wait_has_a_deadline_without_terminating_worker():
    app=QApplication.instance() or QApplication([])
    worker=HeldWorker();worker.start();await_condition(worker.entered.is_set)
    operation=ShutdownOperation(None,[worker],None,handoff=True,wait_timeout=.05)
    try:
        operation.start();await_condition(lambda:not operation.isRunning())
        assert operation.outcome.status=='failed' and not operation.outcome.committed
        assert worker.isRunning() and not worker.isInterruptionRequested()
    finally:
        worker.release.set();await_condition(lambda:not worker.isRunning())
        operation.deleteLater();worker.deleteLater();app.processEvents()


def test_progress_escape_requests_cancel_only_before_commit():
    app=QApplication.instance() or QApplication([])
    dialog=ShutdownProgress(None);requests=[];dialog.cancel_requested.connect(lambda:requests.append(True))
    dialog.show();await_condition(dialog.isVisible)
    try:
        QTest.keyClick(dialog,Qt.Key_Escape)
        assert requests==[True] and dialog.isVisible()
        dialog.display(ExitProgress('stopping','',False,True))
        QTest.keyClick(dialog,Qt.Key_Escape)
        assert requests==[True] and dialog.isVisible() and not dialog.cancel.isEnabled()
        assert dialog.force.isEnabled()
    finally:dialog.accept();dialog.deleteLater();app.processEvents()


def test_installed_gui_handoff_preserves_service_scope_and_paths(tmp_path,monkeypatch):
    from cachemonitor import app_restart
    captured=[]
    monkeypatch.setattr(app_restart.subprocess,'Popen',lambda command,**kwargs:captured.append(command))
    executable=tmp_path/'verified app'/'Codexon.exe'
    home=tmp_path/'home';index=tmp_path/'custom index.sqlite'
    evidence=tmp_path/'custom evidence.sqlite';quota=tmp_path/'custom quota.sqlite'
    app_restart.launch_replacement([home],executable=executable,index_path=index,
        evidence_path=evidence,quota_path=quota,managed_services=True)
    command=captured[0]
    assert command[:2]==[str(executable.resolve()),'--replace-gui']
    assert '--managed-services' in command
    for flag,path in (('--codex-home',home),('--index-path',index),
                      ('--evidence-path',evidence),('--quota-path',quota)):
        assert command[command.index(flag)+1]==str(path)


def test_handoff_uses_one_deadline_for_settings_and_display_workers(monkeypatch):
    from cachemonitor import app_shutdown
    app=QApplication.instance() or QApplication([])
    clock=[0.0];monkeypatch.setattr(app_shutdown,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    settings=HeldWorker();display=HeldWorker()
    settings.start();display.start()
    await_condition(lambda:settings.entered.is_set() and display.entered.is_set())
    services=SimpleNamespace(stop=lambda **kw:pytest.fail('Handoff stopped services'))
    operation=ShutdownOperation(services,[settings],None,handoff=True,display_workers=[display],wait_timeout=10)
    try:
        operation.start()
        assert not display.isInterruptionRequested()
        clock[0]=6;settings.release.set()
        await_condition(display.isInterruptionRequested)
        clock[0]=11
        await_condition(lambda:not operation.isRunning())
        assert operation.outcome.status=='failed' and display.isRunning()
        assert operation.handoff_expired()
    finally:
        settings.release.set();display.release.set()
        await_condition(lambda:not settings.isRunning() and not display.isRunning() and not operation.isRunning())
        operation.deleteLater();settings.deleteLater();display.deleteLater();app.processEvents()


def test_expired_handoff_completion_keeps_old_dashboard_usable(dashboard):
    view=dashboard
    operation=ShutdownOperation(view.services,[],view.window,handoff=True)
    operation.outcome=ExitResult('completed',committed=True)
    operation.handoff_deadline=time.monotonic()-1
    view.window.set_exit_pending(True)
    view.window.shutdown_operation=operation
    view.window.shutdown_dialog=ShutdownProgress(view.window,handoff=True)
    view.window.shutdown_dialog.show()
    view.window.shutdown_finished()
    assert not view.finished and view.warnings
    assert not view.window._closing and view.services.active
    assert view.window.observer_panel.toggle.isEnabled() and view.window.tick.isActive()


def test_replacement_carries_only_valid_explicit_update_operation(tmp_path,monkeypatch):
    from cachemonitor import app_restart
    calls=[]
    monkeypatch.setattr(app_restart.subprocess,'Popen',lambda command,**kwargs:calls.append(command))
    operation='a'*32
    app_restart.launch_replacement([tmp_path/'home'],executable=tmp_path/'Codexon.exe',continue_update=operation)
    command=calls[0]
    assert command[command.index('--continue-update')+1]==operation
    app_restart.launch_replacement([tmp_path/'home'])
    assert '--continue-update' not in calls[1]
    with pytest.raises(ValueError):app_restart.launch_replacement([],continue_update='invalid')
    assert len(calls)==2
