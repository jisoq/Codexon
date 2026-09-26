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
        import threading
        self.force_requested=threading.Event()

    def request_force(self):
        self.force_requested.set()

    def run(self):
        try:
            self.progress.emit('분석과 설정 변경 마무리 중…')
            for worker in self.workers:
                if worker and isValid(worker) and worker.isRunning():worker.wait()
            self.services.stop(force_requested=self.force_requested.is_set)
        except Exception as error:
            self.error=str(error)
            self.failed.emit(self.error)


class ExitConnectionCheck(QThread):

    def __init__(self,manager,parent):
        super().__init__(parent);self.manager=manager;self.count=None;self.health={}

    def run(self):
        try:
            health=self.manager.health(timeout=3)
            self.health=health or {}
            count=self.health.get('active_connections')
            if type(count) is int and count>=0:self.count=count
            elif health is None and self.manager.health_state=='refused':self.count=0
        except (OSError,ValueError,RuntimeError):pass



from .quick_runtime import Dialog
from .presentation import Column, Row, Button, Text, Scroll
from .i18n import tr, Verbatim


def exit_message(check,snapshot):
    count=check.count
    text=(tr('확인 시점에 프록시 연결 {count}개가 열려 있습니다. 대기 중인 연결도 포함됩니다.').format(count=count)
          if count is not None else tr('현재 프록시 연결 수를 확인하지 못했습니다.'))
    from .model_evidence import home_key
    titles={s['id']:s.get('title') or s['id'] for s in snapshot.get('sessions',[])
            if s.get('id') and home_key(s.get('home',''))==home_key(check.manager.home)}
    for item in check.health.get('connection_sessions',[]):
        sid=item.get('session_id','')
        title=' '.join(str(titles.get(sid,sid) or tr('세션 확인 불가')).split())
        text+='\n• '+tr('{title} · 연결 {count}개').format(title=title,count=item['connections'])
    text+='\n\n'+tr('안전 종료는 응답 완료를 기다립니다. 강제 종료는 진행 중 연결과 캐시 요청을 끊고 기록 저장 후 종료합니다.')
    if not check.health.get('supports_force_shutdown'):
        text+='\n'+tr('강제 종료를 지원하려면 연결 구성요소를 업데이트하세요.')
    return Verbatim(text)


class ExitConfirmation(Dialog):
    def __init__(self,check,snapshot,parent):
        super().__init__(parent);self.setWindowTitle(tr('Codexon 종료 확인'));self.resize(600,380)
        layout=Column(self);layout.setContentsMargins(22,20,22,20)
        text=Text(exit_message(check,snapshot));text.setWordWrap(True)
        scroll=Scroll();scroll.setWidget(text);layout.addWidget(scroll,1)
        row=Row();row.addStretch()
        self.confirm=Button('안전 종료');self.force=Button('강제 종료');self.cancel=Button('취소')
        self.cancel.put(defaultFocus=True)
        self.force.setEnabled(check.health.get('supports_force_shutdown') is True)
        self.confirm.clicked.connect(lambda:self.finish(1))
        self.force.clicked.connect(lambda:self.finish(2));self.cancel.clicked.connect(self.reject)
        for button in (self.confirm,self.force,self.cancel):row.addWidget(button)
        layout.addLayout(row)


from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QPushButton, QProgressBar


class ShutdownProgress(QDialog):
    def __init__(self,parent,supported):
        super().__init__(parent)
        self.setWindowTitle(tr('Codexon 종료 중'));self.setWindowModality(Qt.ApplicationModal)
        self.setWindowFlag(Qt.WindowCloseButtonHint,False)
        layout=QVBoxLayout(self)
        self.label=QLabel(tr('관련 작업 마무리 중…'));layout.addWidget(self.label)
        progress=QProgressBar();progress.setRange(0,0);layout.addWidget(progress)
        warning=QLabel(tr('강제 종료하면 진행 중인 응답이 중단됩니다.'));layout.addWidget(warning)
        self.force=QPushButton(tr('강제 종료'));self.force.setEnabled(supported)
        self.force.setAutoDefault(False);self.force.setDefault(False)
        self.force.clicked.connect(lambda:self.force.setEnabled(False));layout.addWidget(self.force)
        if not supported:layout.addWidget(QLabel(tr('강제 종료를 지원하려면 연결 구성요소를 업데이트하세요.')))

    def setLabelText(self,text):self.label.setText(text)
    def reject(self):pass
