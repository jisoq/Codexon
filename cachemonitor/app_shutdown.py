"""Qt adapter for cooperative application service shutdown."""
from PySide6.QtCore import QThread, Signal
from shiboken6 import isValid
from .app_services import AppServices


class ShutdownOperation(QThread):
    progress=Signal(str)
    failed=Signal(str)
    def __init__(self,manager,homes,index,evidence,workers,parent):
        super().__init__(parent)
        self.services=AppServices(manager,homes,index,evidence,progress=self.progress.emit)
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
