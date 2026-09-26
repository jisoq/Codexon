"""Qt adapters for the app-owned service lifetime."""
from PySide6.QtCore import QObject, QThread, QTimer, Signal
from shiboken6 import isValid
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
        self.services=services;self.operation=None;self.closed=False
        self.timer=QTimer(self);self.timer.setInterval(15000)
        self.timer.timeout.connect(self.poll)

    def start(self):
        if self.closed:return
        self.timer.start();self.invoke('start')

    def poll(self):
        panel=getattr(self.parent(),'observer_panel',None)
        if panel and panel.busy():return
        self.invoke('ensure')

    def invoke(self,name):
        if self.closed or self.operation:return
        self.operation=ServiceOperation(self.services,name,self)
        self.operation.result.connect(self.result)
        self.operation.finished.connect(self.finished)
        self.operation.start()

    def finished(self):
        operation=self.operation;self.operation=None
        operation.deleteLater()

    def stop(self):
        self.closed=True;self.timer.stop();self.services.deactivate()


class ShutdownOperation(QThread):
    progress=Signal(str)
    failed=Signal(str)
    def __init__(self,services,workers,parent):
        super().__init__(parent)
        self.services=services
        self.services.progress=self.progress.emit
        self.workers=workers
        self.error=None

    def run(self):
        try:
            self.progress.emit('분석과 설정 변경 마무리 중…')
            for worker in self.workers:
                if worker and isValid(worker) and worker.isRunning():worker.wait()
            self.services.stop()
        except Exception as error:
            self.error=str(error)
            self.failed.emit(self.error)
