"""One proxy switch; preparation, verification and recovery stay in the controller."""
from PySide6.QtCore import Qt,QThread, QSignalBlocker, Signal
from .presentation import Button, Column, Group, Row, Text
from .controls import Switch
from .observer_control import ObserverManager


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
    progress_requested=Signal()
    def __init__(self,home,directory=None,active=False,parent=None,manager=None):
        super().__init__(parent)
        self.manager=manager or ObserverManager(home,directory)
        self.services=None
        self.operation=None;self.active=active;self.enabled=False
        self.pending=None;self.last_result={};self.operation_name=''
        self.inactive=False
        layout=Column(self);layout.setContentsMargins(0,12,0,12);layout.setSpacing(4)
        row=Row();row.put(collapseBelow=620,minHeight=36);row.setSpacing(8);copy=Column();copy.setSpacing(4)
        title=Text('프록시 사용');title.setStyleSheet('font-weight: 500;');copy.addWidget(title)
        description=Text('Codex 연결을 통해 응답의 사용량을 관측합니다.')
        self.description=description
        description.setWordWrap(True);description.put(color='muted',fontSize=13)
        copy.addWidget(description);row.addLayout(copy,2)
        controls=Row();controls.setSpacing(8)
        self.toggle=Switch();self.toggle.setAccessibleName('프록시 사용');controls.addWidget(self.toggle)
        self.status_label=Text('꺼짐');self.status_label.put(fontSize=13);controls.addWidget(self.status_label)
        self.progress_link=Button('진행 보기');self.progress_link.put(iconName='right',role='quiet')
        self.progress_link.setFixedSize(32,32);self.progress_link.setAccessibleName('업데이트 진행 보기')
        self.progress_link.setToolTip('업데이트 진행 보기');self.progress_link.hide()
        self.progress_link.clicked.connect(self.progress_requested)
        controls.addWidget(self.progress_link);controls.addStretch();row.addLayout(controls,1)
        layout.addLayout(row)
        self.error_detail=Text();self.error_detail.setWordWrap(True);self.error_detail.put(color='error',fontSize=13)
        self.error_detail.hide();layout.addWidget(self.error_detail)
        self.restart_label=Text();self.restart_label.setWordWrap(False);self.restart_label.setFixedHeight(24)
        self.restart_label.setStyleSheet('color: warning;');layout.addWidget(self.restart_label)
        self.toggle.toggled.connect(self.request)
        from .theme import shared_theme
        shared_theme().changed.connect(self.refresh_color)
        self.refresh_color()
        self.update_controls()

    def busy(self):return self.operation is not None
    def update_controls(self):
        phase=(self.last_result.get('update') or {}).get('phase')
        update=self.last_result.get('update') or {}
        critical=update.get('cutover_started') and phase in ('queued','waiting','switching','stopping','starting','verifying','rollback')
        self.toggle.setEnabled(self.active and not critical and phase not in ('switching','stopping','starting','verifying','rollback'))

    def refresh_color(self):
        from .theme import shared_theme
        theme=shared_theme()
        color=('#77D69B' if theme.dark else '#16803D') if self.enabled else ('#AAAAAA' if theme.dark else '#666666')
        if (self.last_result.get('update') or {}).get('phase') not in (None,'off','complete','cancelled'):
            color='#F1BD63' if theme.dark else '#8A5500'
        if self.error_detail.isVisible():color='#EA9A92' if theme.dark else '#A3261B'
        self.status_label.put(color=theme.readableText(color,'surface'))

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
        if operation=='turn_on':self.status_label.setText('로컬 프록시 준비 확인 중…')
        elif operation=='turn_off':self.status_label.setText('직접 연결로 복원 중…')
        if self.services:
            from .app_shutdown import ServiceOperation
            self.operation=ServiceOperation(self.services,operation,self)
        else:self.operation=ObserverOperation(self.manager,operation,self)
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
        self.progress_link.setVisible(phase_update not in (None,'off','complete','cancelled'))
        self.update_controls()
        if self.pending is None:
            with QSignalBlocker(self.toggle):self.toggle.setChecked(self.enabled)
        phase=state.get('phase','off')
        text={'off':'꺼짐','starting':'시작 중…','prepared':'꺼짐',
              'validated':'꺼짐','active':'실행 중','draining':'연결 마무리 중',
              'faulted':'보호 정지','failed':'켜지 못했습니다','recovery_failed':'보호 정지 , 설정 복구 필요',
              'recovery_required':'복구 필요'}.get(phase,phase)
        if error and not state:
            text='복구 필요'
        elif state.get('configured') and state.get('probe_state'):
            from .connection_recovery import assess
            assessed=assess(state)
            if assessed['code'] not in ('responding','updating'):
                text=assessed['title']
        incident=state.get('incident') or {}
        reason=error or incident.get('reason') or state.get('last_error') or state.get('service_issue') or state.get('cleanup_warning')
        if incident.get('recovery_error'):reason=(reason+'\n' if reason else '')+'설정 복구 실패: '+incident['recovery_error']
        inactive=not self.enabled and phase in ('off','prepared','validated') and not reason and not state.get('registration_issue')
        if inactive:
            text='꺼짐'
        elif error and phase in ('off','prepared','validated') and state:
            text='프록시 설정 변경 실패'
        self.inactive=inactive
        if phase_update in ('queued','waiting','interrupted','failed'):text='업데이트 적용 대기'
        elif phase_update in ('switching','stopping','starting','verifying','rollback'):text='연결 적용 중'
        self.status_label.setText(text)
        self.error_detail.setText(str(reason or ''))
        self.error_detail.setVisible(bool(reason))
        self.refresh_color()
        restart=bool(state.get('restart_required')) and phase_update!='complete'
        self.restart_label.setText('변경을 적용하려면 연결한 앱을 다시 여세요.' if restart else '')
        self.restart_label.setVisible(restart)
        self.status_observed.emit(state)

    def stop(self):
        self.active=False;self.pending=None;self.manager.cancelled.set()
        # Never destroy a running QThread while its transaction is rolling back.
        # Network and scheduler operations have their own bounded timeouts.
        if self.operation and self.operation.isRunning():self.operation.wait()
