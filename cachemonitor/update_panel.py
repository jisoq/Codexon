"""The single desktop entry point for app and proxy updates."""
from PySide6.QtCore import QThread, Signal
from .i18n import tr
from .presentation import Button, Column, Group, Text, Row
from .quick_runtime import Dialog
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
        self.offer=None
        self.heading=Text('업데이트');self.heading.put(fontSize=18,bold=True);layout.addWidget(self.heading)
        self.button=Button('업데이트 확인')
        self.button.setAccessibleName('Codexon과 프록시 업데이트 확인 및 설치')
        self.button.clicked.connect(self.start)
        layout.addWidget(self.button)
        self.status=Text('');self.status.setWordWrap(True);layout.addWidget(self.status)
        from .version import VERSION
        self.app_state=Text('확인 전');self.proxy_state=Text('확인 전')
        self.proxy_detail=Text('Codex 통신 중계')
        for name,detail,state in [('Codexon 앱',Text(VERSION),self.app_state),('Codex 연결 프록시',self.proxy_detail,self.proxy_state)]:
            group=Group();body=Column(group);body.setContentsMargins(0,14,0,14)
            row=Row();row.put(collapseBelow=420);title=Text(name);title.put(bold=True);row.addWidget(title,2)
            state.setWordWrap(True);row.addWidget(state,1);body.addLayout(row)
            detail.put(fontSize=13,color='muted');detail.setWordWrap(True);body.addWidget(detail);layout.addWidget(group)
        self.checked=Text('');self.checked.put(fontSize=13,color='muted');layout.addWidget(self.checked)
        self.execute=Button('');self.execute.setVisible(False);self.execute.clicked.connect(self.request_install);layout.addWidget(self.execute)

    def open_recovery(self):
        try:open_recovery(self.manager.home,self.manager.directory,self.manager.url)
        except Exception as exc:getattr(self,'recovery_status',self.status).setText(str(exc))

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
        self.execute.setEnabled(False)
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
        title='업데이트 설치' if app_update else '프록시 재시작'
        self.dialog=Dialog(self);self.dialog.setWindowTitle(tr(title));self.dialog.resize(560,420)
        layout=Column(self.dialog);layout.setContentsMargins(24,24,24,24);layout.setSpacing(16)
        heading=Text(title);heading.put(fontSize=18,bold=True);layout.addWidget(heading)
        rows=[('변경 대상','Codexon 앱' if app_update else 'Codex 연결 프록시'),
              ('버전 변경' if app_update else '재시작 이유',
               plan['release']['tag_name'] if app_update else plan.get('reason','프록시 업데이트 필요')),
              ('현재 연결',str(plan['connections']) if plan.get('connections') is not None else tr('확인 불가'))]
        for name,value in rows:
            row=Row();row.addWidget(Text(name),1);item=Text(value);item.setWordWrap(True);row.addWidget(item,2);layout.addLayout(row)
        note=Text('확인 시점 기준, 대기 연결 포함');note.put(fontSize=13,color='muted');layout.addWidget(note)
        warning=Text('연결 영향');warning.put(bold=True,color='warning');layout.addWidget(warning)
        warning=Text('재시작 중 Codex 연결 일시 중단 가능\n응답 완료 후 실행 권장');warning.setWordWrap(True);layout.addWidget(warning)
        row=Row();row.addStretch();self.dialog.cancel=Button('나중에');self.dialog.cancel.put(defaultFocus=True)
        self.dialog.confirm=Button(title);self.dialog.cancel.clicked.connect(self.dialog.reject)
        self.dialog.confirm.clicked.connect(lambda:self.dialog.finish(1))
        row.addWidget(self.dialog.cancel);row.addWidget(self.dialog.confirm);layout.addLayout(row)
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
        if plan:
            from datetime import datetime
            from .version import VERSION
            self.offer=plan
            self.app_state.setText('↻ '+tr('업데이트 가능') if plan['kind']=='app' else '✓ '+tr('최신 버전'))
            self.app_state.put(color='warning' if plan['kind']=='app' else 'success')
            self.proxy_state.setText(tr(plan.get('reason','앱 업데이트 후 프록시 확인')))
            self.proxy_state.put(color='warning' if plan['kind']=='proxy' else 'muted')
            self.checked.setText(tr('최근 확인')+' '+datetime.now().strftime('%H:%M'))
            self.execute.setText('업데이트 설치' if plan['kind']=='app' else '프록시 재시작')
            self.execute.setVisible(plan['kind'] in ('app','proxy'))
            self.status.setText(plan.get('reason','설치할 새 버전이 없습니다.'))
        self.button.setEnabled(True)
        self.execute.setEnabled(True)

    def request_install(self):
        if self.operation or self.confirming or not self.offer:return
        self.confirming=True
        self.button.setEnabled(False)
        self.confirm_install(self.offer)

    def status_progress(self,text):
        self.operation.progress.emit(text)

    def proxy_status(self,result):
        from .proxy_update import BUSY
        if self.operation or self.confirming:return
        update=result.get('update') or {}
        self.proxy_update=update
        self.button.setText('업데이트 취소' if update.get('phase') in BUSY else '업데이트 확인')
        self.button.setEnabled(True)
        if update.get('message') and update.get('phase')!='off':self.status.setText(update['message'])
