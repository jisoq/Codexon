"""One proxy switch; preparation, verification and recovery stay in the controller."""
from PySide6.QtCore import Qt,QThread, QTimer, QSignalBlocker, Signal
from .presentation import Button, Column, Form, Group, Row, Text
from .controls import Switch
from .observer_control import ObserverManager
from .ui_details import Details


class ObserverOperation(QThread):
    result=Signal(dict)
    def __init__(self,manager,operation,parent):
        super().__init__(parent);self.manager=manager;self.operation=operation

    def run(self):
        try:self.result.emit(getattr(self.manager,self.operation)())
        except Exception as exc:
            result={'error':str(exc)}
            try:result['observer_status']=self.manager.status()
            except (OSError,ValueError,RuntimeError):pass
            self.result.emit(result)


class ObserverPanel(Group):
    status_observed=Signal(dict)
    def __init__(self,home,directory=None,active=False,parent=None):
        super().__init__(parent)
        self.manager=ObserverManager(home,directory)
        self.operation=None;self.active=active;self.enabled=False
        self.pending=None;self.last_result={};self.operation_name=''
        layout=Column(self);layout.setContentsMargins(0,0,0,10);layout.setSpacing(16)
        row=Row();copy=Column()
        title=Text('프록시 사용');title.setStyleSheet('font-weight: 500;');copy.addWidget(title)
        description=Text('호출·응답 모델 확인 · 켤 때 검증 호출 1회')
        description.setWordWrap(True);description.setStyleSheet('color: #6b737c; font-size: 12px;')
        copy.addWidget(description);row.addLayout(copy,1)
        self.toggle=Switch();self.toggle.setAccessibleName('프록시 사용');row.addWidget(self.toggle)
        layout.addLayout(row)
        self.status_label=Text('꺼짐');self.status_label.setWordWrap(False)
        self.status_label.setStyleSheet('font-size: 15px; font-weight: 600; padding: 12px 0;')
        layout.addWidget(self.status_label)
        self.error_detail=Details('오류 상세',Text(),compact=True)
        self.error_detail.body.setTextFormat(Qt.PlainText)
        self.error_detail.hide();layout.addWidget(self.error_detail)
        self.update_button=Button('프록시 업데이트');self.update_button.hide()
        self.update_button.clicked.connect(lambda:self.invoke('cancel_update' if self.last_result.get('update',{}).get('phase') in ('queued','waiting') else 'update_proxy'))
        layout.addWidget(self.update_button)
        self.update_status=Text();self.update_status.setWordWrap(True);self.update_status.hide()
        layout.addWidget(self.update_status)
        details=Form();details.setVerticalSpacing(12);details.setHorizontalSpacing(24)
        self.runtime_values={}
        for key,title in (('guard','자동 복구'),('startup','로그인 시 실행'),('version','버전'),('path','실행 파일')):
            name=Text(title);name.setStyleSheet('color: #6b737c;')
            value=Text('확인 중');value.setWordWrap(True);value.setTextFormat(Qt.PlainText)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.runtime_values[key]=value;details.addRow(name,value)
        layout.addWidget(Details('연결 정보',details))
        self.restart_label=Text();self.restart_label.setWordWrap(False);self.restart_label.setFixedHeight(24)
        self.restart_label.setStyleSheet('color: #946b2c;');layout.addWidget(self.restart_label)
        self.toggle.toggled.connect(self.request)
        self.timer=QTimer(self);self.timer.setInterval(15000)
        self.timer.timeout.connect(lambda:self.invoke('ensure'))
        self.update_controls()
        if active:self.timer.start();QTimer.singleShot(0,lambda:self.invoke('ensure'))

    def busy(self):return self.operation is not None
    def update_controls(self):
        switching=self.last_result.get('update',{}).get('phase') in ('switching','rollback')
        self.toggle.setEnabled(self.active and not switching)
        self.update_button.setEnabled(self.active and not self.busy() and not switching)

    def request(self,enabled):
        if not self.active:return
        if self.busy():
            self.pending=enabled
            if not enabled and self.operation_name in ('turn_on','resume'):
                self.manager.cancelled.set();self.status_label.setText('취소 중…')
            return
        self.invoke('turn_on' if enabled else 'turn_off')

    def invoke(self,operation):
        if not self.active or self.busy():return
        self.operation_name=operation;self.manager.cancelled.clear()
        if operation=='turn_on':self.status_label.setText('연결 확인 중…')
        elif operation=='turn_off':self.status_label.setText('직접 연결로 복원 중…')
        self.operation=ObserverOperation(self.manager,operation,self)
        self.operation.result.connect(self.display);self.operation.finished.connect(self.finished)
        self.operation.start()
        self.update_controls()

    def finished(self):
        operation=self.operation;self.operation=None
        if operation:operation.deleteLater()
        self.update_controls()
        if self.pending is not None and self.active:
            pending=self.pending;self.pending=None
            self.invoke('turn_on' if pending else 'turn_off')

    def display(self,result):
        error=result.get('error');state=result.get('observer_status',{}) if error else result
        self.last_result=state;self.enabled=state.get('configured',False)
        update=state.get('update') or {};phase_update=update.get('phase')
        self.update_button.setText('업데이트 예약 취소' if phase_update in ('queued','waiting') else '프록시 업데이트')
        self.update_button.setVisible(bool(state.get('version_mismatch') and self.enabled) or phase_update in ('queued','waiting','switching','rollback','interrupted'))
        self.update_status.setText(update.get('message',''))
        self.update_status.setVisible(bool(update.get('message')))
        self.update_controls()
        if self.pending is None:
            with QSignalBlocker(self.toggle):self.toggle.setChecked(self.enabled)
        phase=state.get('phase','off')
        text={'off':'꺼짐','starting':'연결 확인 중…','prepared':'꺼짐',
              'validated':'꺼짐 · 연결 시험 완료','active':'켜짐','draining':'꺼짐 · 기존 연결 마무리 중',
              'faulted':'보호 정지','failed':'켜지 못했습니다','recovery_failed':'보호 정지 · 설정 복구 필요',
              'recovery_required':'연결 확인 필요'}.get(phase,phase)
        incident=state.get('incident') or {}
        reason=error or incident.get('reason') or state.get('last_error') or state.get('service_issue') or state.get('cleanup_warning')
        if incident.get('recovery_error'):reason=(reason+'\n' if reason else '')+'설정 복구 실패: '+incident['recovery_error']
        self.status_label.setText(text)
        self.error_detail.body.setText(str(reason or ''))
        self.error_detail.setVisible(bool(reason))
        self.status_label.setStyleSheet('font-size: 15px; font-weight: 600; padding: 12px 0; color: '
            +('#a24d3d' if error or phase in ('faulted','failed','recovery_failed','recovery_required') else '#327159' if self.enabled else '#6b737c')+';')
        health=state.get('health') or {};runtime=state.get('runtime') or {};registration=state.get('registration')
        version=health.get('version') or '실행 안 됨'
        versions=f"앱 {state.get('app_version','—')} · 프록시 {version}"
        if state.get('version_mismatch'):
            versions+='\n프록시 업데이트 필요'+(' · 현재 연결 유지 중' if self.enabled else ' · 프록시 사용 꺼짐')
        elif health and version!=state.get('app_version'):
            versions+=' · 호환됨'
        self.runtime_values['version'].setText(versions)
        self.runtime_values['guard'].setText('작동 중' if runtime.get('phase') in ('ready','active','draining') else '실행 안 됨')
        if health and not health.get('control_id') and runtime.get('phase') in ('ready','active','draining'):
            self.runtime_values['guard'].setText('작동 중 · 이전 프록시는 연결 장애만 감시')
        startup=('켜짐' if registration.get('autostart') else '꺼짐') if registration is not None else '확인 중'
        if state.get('registration_issue'):startup='확인 필요: '+state['registration_issue']
        self.runtime_values['startup'].setText(startup)
        self.runtime_values['path'].setText(str((registration or {}).get('executable') or state.get('app_path','—')))
        restart=bool(state.get('restart_required') or self.enabled)
        self.restart_label.setText('연결 변경 적용: Codex 재시작' if restart else '')
        self.restart_label.setVisible(restart)
        self.status_observed.emit(state)

    def stop(self):
        self.active=False;self.timer.stop();self.pending=None;self.manager.cancelled.set()
        # Never destroy a running QThread while its transaction is rolling back.
        # Network and scheduler operations have their own bounded timeouts.
        if self.operation and self.operation.isRunning():self.operation.wait()
