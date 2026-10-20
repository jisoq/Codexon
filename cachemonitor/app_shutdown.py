"""Qt adapters for the app-owned service lifetime."""
from PySide6.QtCore import QObject, QThread, QTimer, Signal
from shiboken6 import isValid
import time
from .shutdown_state import ExitCancelled, ExitControl, ExitProgress, ExitResult
from .app_restart import HANDOFF_TIMEOUT
class ServiceOperation(QThread):
    result=Signal(dict)

    def __init__(self,services,operation,parent):
        super().__init__(parent)
        self.services,self.operation=services,operation

    def run(self):
        try:self.result.emit(self.services.execute(self.operation))
        except Exception as exc:
            result={'error':str(exc)}
            try:result['observer_status']=self.services.status()
            except (OSError,ValueError,RuntimeError):pass
            self.result.emit(result)


class ServiceController(QObject):
    """One app timer; panels only consume its result or request explicit actions."""
    result=Signal(dict)

    def __init__(self,services,parent):
        super().__init__(parent)
        self.services=services;self.operation=None;self.closed=False;self.exit_pending=False
        self.timer=QTimer(self);self.timer.setInterval(15000)
        self.timer.timeout.connect(self.poll)

    def start(self):
        if self.closed or self.exit_pending:return
        self.timer.start()
        if not self.services.active:self.invoke('start')

    def request_login_launch(self, launch):
        if self.closed or self.exit_pending:
            launch.cancel()
            return
        self.services.login_launch = launch
        if self.services.active:self.invoke('login')
        else:self.start()

    def poll(self):
        panel=getattr(self.parent(),'observer_panel',None)
        if panel and panel.busy():return
        self.invoke('ensure')

    def invoke(self,name):
        if self.closed or self.exit_pending or self.operation:return
        self.operation=ServiceOperation(self.services,name,self)
        self.operation.result.connect(lambda value,operation=name:
            self.result.emit({**value,'background_operation':operation}))
        self.operation.finished.connect(self.finished)
        self.operation.start()

    def finished(self):
        operation=self.operation;self.operation=None
        if operation:operation.deleteLater()
        if self.services.login_launch is not None and not self.closed and not self.exit_pending:
            self.invoke('login' if self.services.active else 'start')

    def set_exit_pending(self,pending):
        self.exit_pending=bool(pending)
        if not pending and not self.closed:self.poll()

    def stop(self):
        self.closed=True;self.timer.stop();self.services.deactivate()


class ShutdownOperation(QThread):
    progress=Signal(object)

    def __init__(self,services,workers,parent,*,handoff=False,display_workers=(),wait_timeout=HANDOFF_TIMEOUT,handoff_deadline=None):
        super().__init__(parent)
        self.services=services;self.workers=tuple(workers)
        self.handoff=handoff;self.display_workers=tuple(display_workers)
        self.wait_timeout=wait_timeout;self.control=ExitControl()
        self.handoff_deadline=(handoff_deadline if handoff_deadline is not None else time.monotonic()+wait_timeout) if handoff else None
        self.outcome=None;self.error=None;self.display_stopping=False

    def request_cancel(self):return self.control.request_cancel()
    def request_force(self):return self.control.request_force()

    @staticmethod
    def running(worker):
        try:return bool(worker and isValid(worker) and worker.isRunning())
        except RuntimeError:return False

    def handoff_expired(self):
        return self.handoff_deadline is not None and time.monotonic()>=self.handoff_deadline

    def wait_workers(self,workers,*,cancellable):
        deadline=self.handoff_deadline if self.handoff else time.monotonic()+self.wait_timeout
        for worker in workers:
            while self.running(worker):
                if cancellable:self.control.checkpoint()
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    raise RuntimeError('앱의 백그라운드 작업이 아직 마무리되지 않았습니다. 잠시 후 다시 시도하세요.')
                try:worker.wait(max(1,min(50,int(remaining*1000))))
                except RuntimeError:break
        if self.handoff_expired():raise RuntimeError('앱 다시 시작 대기 시간이 지났습니다. 기존 Codexon을 계속 사용할 수 있습니다.')
        if cancellable:self.control.checkpoint()

    def run(self):
        try:
            self.progress.emit(ExitProgress('preparing','진행 중인 설정 변경을 확인하고 있습니다.',True))
            self.wait_workers(self.workers,cancellable=True)
            if not self.handoff and self.services is not None:
                self.outcome=self.services.stop(control=self.control,publish=self.progress.emit)
                if self.outcome is None:self.outcome=ExitResult('completed',committed=self.control.committed)
                if self.outcome.status!='completed':return
            else:
                self.control.commit()
                self.outcome=ExitResult('completed',committed=True)
            self.progress.emit(ExitProgress('finishing','화면의 백그라운드 작업을 마무리하고 있습니다.',False))
            self.display_stopping=True
            for worker in self.display_workers:
                if self.running(worker):
                    worker.requestInterruption()
                    wake=getattr(worker,'wake',None)
                    if wake is not None:wake.set()
            self.wait_workers(self.display_workers,cancellable=False)
        except ExitCancelled:
            self.outcome=ExitResult('cancelled',committed=self.control.committed)
        except Exception as error:
            self.error=str(error)
            self.outcome=ExitResult('failed',committed=self.control.committed,error=self.error)


from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QProgressBar, QHBoxLayout
from .i18n import tr


class ShutdownProgress(QDialog):
    cancel_requested=Signal()
    force_requested=Signal()

    def __init__(self,parent,*,handoff=False):
        super().__init__(parent)
        self.setWindowTitle(tr('Codexon 안전 종료'));self.setWindowModality(Qt.NonModal)
        self.setMinimumWidth(420)
        layout=QVBoxLayout(self)
        self.label=QLabel(tr('앱 종료를 준비하고 있습니다.'));self.label.setWordWrap(True);layout.addWidget(self.label)
        self.activity=QLabel();self.activity.setWordWrap(True);layout.addWidget(self.activity)
        progress=QProgressBar();progress.setRange(0,0);layout.addWidget(progress)
        warning=QLabel(tr('응답을 중단하고 종료하면 진행 중인 응답을 끊고 기록 저장을 시도합니다.'));warning.setWordWrap(True);layout.addWidget(warning)
        warning.setVisible(not handoff)
        row=QHBoxLayout();row.addStretch()
        self.force=QPushButton(tr('응답 중단 후 종료'));self.force.setEnabled(False)
        self.force.setVisible(not handoff)
        self.cancel=QPushButton(tr('취소'));self.cancel.setDefault(True)
        self.force.setAutoDefault(False)
        self.cancel.clicked.connect(self.cancel_requested)
        self.force.clicked.connect(self.force_requested)
        row.addWidget(self.force);row.addWidget(self.cancel);layout.addLayout(row)
        self.can_cancel=True;self.cancel_pending=False;self.force_pending=False

    def display(self,progress):
        self.can_cancel=progress.can_cancel
        if not self.cancel_pending:self.label.setText(tr(progress.message))
        counts=[]
        if progress.responding:counts.append(tr('응답 중 {count}개').format(count=progress.responding))
        if progress.unknown:counts.append(tr('상태를 확인하지 못한 연결 {count}개').format(count=progress.unknown))
        self.activity.setText(', '.join(counts));self.activity.setVisible(bool(counts))
        self.cancel.setEnabled(progress.can_cancel and not self.cancel_pending)
        self.force.setEnabled(progress.can_force and not self.force_pending and not self.cancel_pending)

    def cancellation_requested(self):
        self.cancel_pending=True;self.cancel.setEnabled(False);self.force.setEnabled(False)
        self.label.setText(tr('종료 취소를 확인하고 있습니다.'))

    def forcing_requested(self):
        self.force_pending=True;self.force.setEnabled(False)

    def reject(self):
        if self.can_cancel and not self.cancel_pending:self.cancel_requested.emit()

    def closeEvent(self,event):
        event.ignore();self.reject()
