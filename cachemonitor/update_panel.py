"""The single desktop entry point for app and proxy updates."""
from PySide6.QtCore import QThread, Signal
from .quick_runtime import Confirmation
from .i18n import tr
from .presentation import Button, Column, Group, Text
from .installation import open_recovery


class UpdateOperation(QThread):
    progress=Signal(str)
    result=Signal(object)

    def __init__(self,parent=None,action=None,manager=None):
        super().__init__(parent);self.action=action;self.manager=manager

    def run(self):
        from .app_update import check_update
        try:self.result.emit(self.action() if self.action else check_update(self.progress.emit,self.manager))
        except Exception as exc:self.result.emit(str(exc))


class UpdatePanel(Group):
    def __init__(self,manager,parent=None):
        super().__init__(parent)
        self.manager=manager
        self.owner=parent
        self.operation=None
        self.proxy_update={}
        self.pending_plan=None
        self.confirming=False
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
        from .proxy_update import BUSY
        if self.operation or self.confirming:return
        self.button.setEnabled(False)
        action=None
        if self.proxy_update.get('phase') in BUSY:
            action=lambda:self.manager.cancel_update().get('update',{}).get('message','예약 취소 요청 완료')
        self.run_operation(action)

    def run_operation(self,action=None):
        self.button.setEnabled(False)
        self.pending_plan=None
        self.operation=UpdateOperation(self,action,self.manager)
        self.operation.progress.connect(self.status.setText)
        self.operation.result.connect(self.receive_result)
        self.operation.finished.connect(self.finished)
        self.operation.start()

    def receive_result(self,result):
        if isinstance(result,dict):self.pending_plan=result
        else:self.status.setText(result)

    def confirm_install(self,plan):
        app_update=plan['kind']=='app'
        text=(tr('새 버전 {version}을 설치하시겠습니까?').format(version=plan['release']['tag_name']) if app_update
              else tr('앱은 최신 버전입니다. 연결 구성요소를 확인하고 적용하시겠습니까?'))
        count=plan.get('connections')
        text+='\n\n'+(tr('확인 시점에 프록시 연결 {count}개가 열려 있습니다. 대기 중인 연결도 포함됩니다.').format(count=count)
                       if count is not None else tr('현재 프록시 연결 수를 확인하지 못했습니다.'))
        text+='\n\n'+tr('업데이트 중 Codex 연결이 끊길 수 있습니다. 진행 중인 응답과 서버 호출이 모두 끝난 뒤 진행해 주세요. 계속하시겠습니까?')
        self.dialog=Confirmation(tr('업데이트 확인'),text,self)
        self.dialog.resize(560,350)
        self.dialog.confirm.setText('설치' if app_update else '적용')
        self.dialog.cancel.setText('나중에')
        self.dialog.finished.connect(lambda result:self.decided(plan,result))
        self.dialog.open()

    def closing(self):
        if getattr(self.owner,'_closing',False):return True
        parent=self.parent()
        while parent is not None:
            if getattr(parent,'_closing',False):return True
            parent=parent.parent()
        return False

    def decided(self,plan,result):
        dialog=self.dialog;self.dialog=None;self.confirming=False
        if dialog:dialog.deleteLater()
        if result==1 and not self.closing():
            from .app_update import update
            self.run_operation(lambda:update(self.status_progress,self.manager,plan=plan))
            return
        self.status.setText('업데이트를 취소했습니다.')
        self.button.setEnabled(True)

    def finished(self):
        operation=self.operation;self.operation=None
        if operation:operation.deleteLater()
        plan=self.pending_plan;self.pending_plan=None
        if plan and plan['kind']=='none':self.status.setText('설치할 새 버전이 없습니다.')
        elif plan and not self.closing():
            self.confirming=True
            self.confirm_install(plan)
            return
        self.button.setEnabled(True)

    def status_progress(self,text):
        self.operation.progress.emit(text)

    def proxy_status(self,result):
        from .proxy_update import BUSY
        if self.operation or self.confirming:return
        update=result.get('update') or {}
        self.proxy_update=update
        self.button.setText('업데이트 취소' if update.get('phase') in BUSY else '업데이트 확인 및 설치')
        self.button.setEnabled(True)
        if update.get('message') and update.get('phase')!='off':self.status.setText(update['message'])
