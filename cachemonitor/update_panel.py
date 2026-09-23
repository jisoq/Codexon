"""The single desktop entry point for app and proxy updates."""
from PySide6.QtCore import QThread, Signal
from .presentation import Button, Column, Group, Text
from .installation import open_recovery


class UpdateOperation(QThread):
    progress=Signal(str)
    result=Signal(str)

    def __init__(self,parent=None,action=None):
        super().__init__(parent);self.action=action

    def run(self):
        from .app_update import update
        try:self.result.emit(self.action() if self.action else update(self.progress.emit))
        except Exception as exc:self.result.emit(str(exc))


class UpdatePanel(Group):
    def __init__(self,manager,parent=None):
        super().__init__(parent)
        self.manager=manager
        self.operation=None
        self.proxy_update={}
        layout=Column(self);layout.setContentsMargins(0,8,0,16);layout.setSpacing(10)
        self.button=Button('업데이트 확인 및 설치')
        self.button.setAccessibleName('Codexon과 프록시 업데이트 확인 및 설치')
        self.button.clicked.connect(self.start)
        layout.addWidget(self.button)
        self.status=Text('');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.recovery=Button('Codex 연결 복구 열기')
        self.recovery.clicked.connect(self.open_recovery)
        layout.addWidget(self.recovery)

    def open_recovery(self):
        try:open_recovery(self.manager.home,self.manager.directory,self.manager.url)
        except Exception as exc:self.status.setText(str(exc))

    def start(self):
        if self.operation:return
        self.button.setEnabled(False)
        action=None
        if self.proxy_update.get('phase') in ('queued','waiting'):
            action=lambda:self.manager.cancel_update().get('update',{}).get('message','예약 취소 요청 완료')
        self.operation=UpdateOperation(self,action)
        self.operation.progress.connect(self.status.setText)
        self.operation.result.connect(self.status.setText)
        self.operation.finished.connect(self.finished)
        self.operation.start()

    def finished(self):
        operation=self.operation;self.operation=None
        if operation:operation.deleteLater()
        self.button.setEnabled(True)

    def proxy_status(self,result):
        if self.operation:return
        update=result.get('update') or {}
        self.proxy_update=update
        self.button.setText('업데이트 예약 취소' if update.get('phase') in ('queued','waiting') else '업데이트 확인 및 설치')
        self.button.setEnabled(update.get('phase') not in ('switching','rollback'))
        if update.get('message'):self.status.setText(update['message'])
