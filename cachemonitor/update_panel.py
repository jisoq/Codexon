"""One compact settings row backed by a durable update operation."""
from PySide6.QtCore import QThread, QTimer, Signal
from .i18n import formatted, tr
from .presentation import Button, Column, Group, Text, Row
from .installation import open_recovery
import time


def failure_message(error,checking):
    from urllib.error import HTTPError, URLError
    if isinstance(error,HTTPError):
        if error.code==429:return '업데이트 서버가 요청을 제한하고 있습니다. 잠시 후 다시 시도해 주세요.'
        return '업데이트 서버에서 정보를 받지 못했습니다. 잠시 후 다시 시도해 주세요.'
    reason=error.reason if isinstance(error,URLError) else error
    if isinstance(reason,TimeoutError):return '업데이트 서버 응답이 늦어지고 있습니다. 잠시 후 다시 시도해 주세요.'
    if isinstance(error,(URLError,ConnectionError)) or checking and isinstance(error,OSError):
        return '업데이트 서버에 연결하지 못했습니다. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.'
    if isinstance(error,OSError):return '업데이트 파일을 준비하지 못했습니다. 저장 공간과 파일 접근 권한을 확인해 주세요.'
    if str(error).startswith('설치형 Codexon에서 업데이트할 수 있습니다.'):return str(error)
    return '업데이트 정보를 확인하지 못했습니다. 다시 시도해 주세요.' if checking else '업데이트를 완료하지 못했습니다. 다시 시도해 주세요.'


class UpdateOperation(QThread):
    progress=Signal(str)
    result=Signal(object)
    def __init__(self,parent=None,action=None,manager=None):
        super().__init__(parent);self.action=action;self.manager=manager
    def run(self):
        from .app_update import check_update
        try:self.result.emit(self.action() if self.action else check_update(self.progress.emit,self.manager))
        except Exception as exc:self.result.emit(exc)


class UpdatePanel(Group):
    attention_changed=Signal(bool)

    def __init__(self,manager,parent=None):
        super().__init__(parent)
        self.manager=manager;self.owner=parent;self.operation=None;self.monitor=None
        self.metadata_pending=False;self.checking=False;self.operation_error=False
        self.offer=None;self.discovered_offer=None;self.proxy_update={};self.record={}
        self.pending_plan=None;self.auto_start=False
        self.next_recovery_check=0
        from .installation import installed
        from .update_state import state_for
        import sys
        self.journal=state_for(manager,installed() if getattr(sys,'frozen',False) else None) if manager and getattr(manager,'directory',None) else None
        layout=Column(self);layout.setContentsMargins(0,12,0,12);layout.setSpacing(4)
        row=Row();row.put(collapseBelow=620,minHeight=36);row.setSpacing(8)
        copy=Column();copy.setSpacing(4)
        self.heading=Text('업데이트');self.heading.put(fontSize=14,bold=True);copy.addWidget(self.heading)
        self.status=Text();self.status.setWordWrap(True);self.status.put(fontSize=13,color='muted');copy.addWidget(self.status)
        self.components=Text();self.components.setWordWrap(True);self.components.put(fontSize=13,color='muted');copy.addWidget(self.components)
        self.status.hide();self.components.hide()
        row.addLayout(copy,2)
        controls=Column();controls.setSpacing(8)
        actions=Row();actions.put(flow=True);actions.setSpacing(8)
        self.version=Button();self.version.put(role='quiet');self.version.hide()
        self.version.clicked.connect(self.open_release);actions.addWidget(self.version)
        self.button=Button('업데이트 확인');self.button.clicked.connect(self.start);actions.addWidget(self.button)
        self.execute=Button('업데이트 시작');self.execute.put(role='primary');self.execute.clicked.connect(self.request_install)
        actions.addWidget(self.execute);self.execute.hide()
        self.recheck=Button('종료 여부 다시 확인');self.recheck.clicked.connect(self.recheck_connections);actions.addWidget(self.recheck);self.recheck.hide()
        actions.addStretch();controls.addLayout(actions)
        secondary=Row();secondary.setSpacing(8)
        self.cancel=Button('취소');self.cancel.clicked.connect(lambda:self.cancel_update(False));secondary.addWidget(self.cancel);self.cancel.hide()
        self.later=Button('나중에 적용');self.later.clicked.connect(lambda:self.cancel_update(True));secondary.addWidget(self.later);self.later.hide()
        secondary.addStretch();controls.addLayout(secondary);row.addLayout(controls,1);layout.addLayout(row)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.refresh);self.timer.start()
        from .theme import shared_theme
        shared_theme().changed.connect(self.refresh)
        self.refresh()

    def status_color(self,attention=False):
        if not attention:return 'muted'
        from .theme import shared_theme
        theme=shared_theme()
        return theme.readableText('#F1BD63' if theme.dark else '#8A5500','surface')

    def open_release(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        if self.version.text():QDesktopServices.openUrl(QUrl('https://github.com/jisoq/Codexon/releases/tag/v'+self.version.text()))

    def open_recovery(self):
        try:open_recovery(self.manager.home,self.manager.directory,self.manager.url)
        except Exception as exc:getattr(self,'recovery_status',self.status).setText(str(exc))

    def busy(self):
        from .update_state import ACTIVE
        from .proxy_update import BUSY
        return bool(self.operation or self.metadata_pending or self.record.get('phase') in ACTIVE
                    or self.proxy_update.get('phase') in BUSY)

    def recheck_connections(self):
        if not self.operation and self.manager:
            self.checking=False;self.launch_operation(self.manager.status)

    def closing(self):
        return bool(getattr(self.owner,'_closing',False))

    def start(self):
        if self.busy() or self.closing():return
        self.offer=None;self.discovered_offer=None;self.checking=True;self.operation_error=False
        self.status.setText('업데이트 확인 중…');self.heading.setText('업데이트')
        self.status.show()
        self.version.hide();self.execute.hide();self.components.hide();self.button.setEnabled(False)
        if self.monitor:
            self.metadata_pending=True;self.monitor.check_manually()
        else:self.launch_operation(None)

    def launch_operation(self,action):
        if self.operation or self.closing():return
        self.button.setEnabled(False);self.execute.setEnabled(False)
        self.operation=UpdateOperation(self,action,self.manager)
        self.operation.progress.connect(self.status.setText)
        self.operation.result.connect(self.receive_result)
        self.operation.finished.connect(self.finished);self.operation.start()

    def bind_monitor(self,monitor):
        if self.monitor:
            self.monitor.manual_finished.disconnect(self.metadata_received);self.monitor.available.disconnect(self.release_discovered)
        self.monitor=monitor;monitor.manual_finished.connect(self.metadata_received);monitor.available.connect(self.release_discovered)

    def release_discovered(self,release):
        if self.busy() or self.closing():return
        if not release:
            if self.offer is self.discovered_offer:self.offer=None;self.discovered_offer=None;self.render_offer()
            return
        self.discovered_offer=dict(kind='app',release=release,connections=None)
        self.show_plan(self.discovered_offer)

    def metadata_received(self,result):
        if not self.metadata_pending:return
        self.metadata_pending=False
        if self.closing():return
        if isinstance(result,Exception):self.receive_result(result);self.finished();return
        from .app_update import check_update
        self.launch_operation(lambda:check_update(self.status_progress,self.manager,release=result))

    def status_progress(self,text):
        operation=self.operation
        if operation:operation.progress.emit(text)

    def receive_result(self,result):
        if self.closing():return
        if isinstance(result,Exception):
            self.auto_start=False;self.operation_error=True;self.offer=None
            self.status.setText(failure_message(result,self.checking));self.status.setToolTip(str(result));self.status.put(color=self.status_color(True))
            self.execute.hide();self.version.hide();self.components.hide();self.button.show()
        elif isinstance(result,dict) and 'operation_id' in result:
            self.record=result;self.offer=None
        elif isinstance(result,dict) and ('configured' in result or 'health' in result):self.proxy_status(result)
        elif isinstance(result,dict):self.pending_plan=result
        else:self.status.setText(str(result))

    def finished(self):
        operation=self.operation;self.operation=None
        if operation:operation.deleteLater()
        plan=self.pending_plan;self.pending_plan=None
        if plan:self.show_plan(plan)
        if self.auto_start:
            self.auto_start=False
            if plan and plan['kind'] in ('app','proxy'):self.request_install();return
        self.button.setEnabled(True);self.execute.setEnabled(True);self.refresh()

    def show_plan(self,plan,**_):
        self.offer=plan;self.operation_error=False;self.status.put(color='muted');self.render_offer()

    def render_offer(self):
        plan=self.offer or {};available=plan.get('kind') in ('app','proxy')
        self.heading.setText('새 버전' if plan.get('kind')=='app' else '업데이트')
        self.button.setVisible(not available);self.execute.setVisible(available);self.execute.setText('업데이트 시작')
        self.version.setVisible(plan.get('kind')=='app')
        version=plan.get('release',{}).get('tag_name','').lstrip('v')
        self.version.setText(version);self.version.setToolTip(tr('변경 내역')+' '+version)
        self.status.setText('교체할 때 연결이 잠시 중단됩니다.' if available else '설치할 새 버전이 없습니다.' if plan else '')
        if available and plan.get('connection_enabled') is False:self.status.setText('새 앱을 설치한 뒤 다시 실행합니다.')
        self.status.setVisible(bool(self.status.text()))
        if plan.get('proxy',{}).get('state') in ('unknown','recovery'):
            self.status.setText('앱은 최신 버전입니다. Codex 연결 상태를 확인하세요.')
        self.components.hide()

    def request_install(self):
        if self.busy() or self.closing():return
        if self.record.get('phase') in ('partial','failed','deferred','interrupted') and self.record.get('app',{}).get('state') in ('verified','current'):
            from .app_update import retry_proxy
            self.checking=False;self.operation_error=False
            self.launch_operation(lambda:retry_proxy(self.manager,self.journal));return
        if not self.offer:return
        if self.offer is self.discovered_offer:
            from .app_update import check_update
            release=self.offer['release'];self.auto_start=True;self.checking=True
            self.launch_operation(lambda:check_update(self.status_progress,self.manager,release=release));return
        from .app_update import update
        plan=self.offer;self.checking=False;self.operation_error=False
        self.launch_operation(lambda:update(self.status_progress,self.manager,plan=plan))

    def cancel_update(self,later):
        if not self.journal or not self.journal.cancel(later=later):return
        phase=self.journal.read().get('phase')
        if phase in ('waiting','needs_exit') and self.manager:self.manager.cancel_update()
        self.cancel.setEnabled(False);self.later.setEnabled(False)
        self.status.setText('나중에 적용하도록 마무리합니다.' if later else '업데이트 취소 중…')

    def proxy_status(self,result):
        self.proxy_update=result.get('update') or {}
        unknown=((result.get('health') or {}).get('websocket_states') or {}).get('unknown',0)
        if self.proxy_update.get('phase')=='waiting' and unknown:
            self.proxy_update={**self.proxy_update,'required_action':'close_client','unknown_connections':unknown}
        if self.journal and self.proxy_update.get('phase')=='interrupted':
            operation_id=self.proxy_update.get('operation_id')
            if operation_id:self.journal.proxy_result(operation_id,self.proxy_update)
        self.refresh()

    def refresh(self):
        from .update_state import ACTIVE,CRITICAL
        if self.closing():return
        if self.journal:self.record=self.journal.read()
        record=self.record;phase=record.get('phase');proxy=record.get('proxy',{})
        legacy_wait=phase in ('waiting','needs_exit') and proxy.get('source_instance') and not proxy.get('operation_id')
        if self.journal and (phase in ('preparing','downloading','installing') or legacy_wait) and not self.operation and time.monotonic()>self.next_recovery_check:
            self.next_recovery_check=time.monotonic()+30
            from .app_update import reconcile_update
            self.checking=False;self.launch_operation(lambda:reconcile_update(self.journal,self.manager))
        if not record and self.proxy_update.get('phase') in ('queued','waiting','switching','stopping','starting','verifying','rollback','interrupted','failed'):
            proxy=self.proxy_update;phase=proxy.get('phase')
            if phase=='waiting' and proxy.get('required_action')=='close_client':phase='needs_exit'
        active=phase in ACTIVE
        critical=phase in CRITICAL or (active and proxy.get('cutover_started'))
        attention=phase in ('needs_exit','partial','failed','interrupted','deferred')
        self.attention_changed.emit(attention)
        if self.metadata_pending or (self.operation and self.checking):return
        self.recheck.setVisible(phase=='needs_exit')
        self.cancel.setVisible(phase in ('preparing','downloading') and not critical)
        self.later.setVisible(phase in ('waiting','needs_exit') and not critical)
        for control in (self.cancel,self.later):control.setEnabled(not record.get('cancel_requested'))
        if not phase or (phase in ('complete','cancelled') and self.offer):return
        if self.operation_error and not active and record.get('app',{}).get('state') not in ('verified','current'):return
        self.button.setVisible(not active and not attention)
        self.button.setEnabled(not self.operation and not self.metadata_pending)
        self.execute.setVisible(attention and not active and record.get('app',{}).get('state') in ('verified','current'))
        self.execute.setText('다시 적용');self.execute.setEnabled(not self.operation)
        self.version.hide()
        headings={'preparing':'업데이트 준비','downloading':'업데이트 준비','installing':'앱 설치 중',
                  'queued':'작업 마무리 대기','waiting':'작업 마무리 대기','needs_exit':'연결한 앱 종료 필요',
                  'switching':'교체 중','stopping':'교체 중','starting':'교체 중','verifying':'검증 중',
                  'rollback':'이전 프록시 복원 중','complete':'업데이트 완료',
                  'partial':'앱 설치 완료, 연결 적용 대기','deferred':'연결 적용 대기',
                  'failed':'업데이트 실패','interrupted':'업데이트 중단','cancelled':'업데이트 취소됨'}
        self.heading.setText(headings.get(phase,'업데이트'))
        message=record.get('message') or proxy.get('message','')
        if phase=='needs_exit':
            message=formatted('소유자를 확인하지 못한 연결 {count}개가 남아 있습니다. 연결한 앱을 정상 종료하세요. Codexon은 켜 두세요. 연결이 종료되면 자동으로 계속합니다.',count=proxy.get('unknown_connections',0))
        elif phase=='waiting':
            count=proxy.get('responding',0)
            message=formatted('진행 중인 응답 {count}개가 끝나기를 기다립니다. 새 작업은 업데이트 완료 후 시작하세요.',count=count) if count else tr('연결을 마무리하고 기록을 저장합니다.')
        elif phase=='downloading':message='설치 파일을 다운로드하고 검증합니다. 현재 설치본은 유지됩니다.'
        elif phase=='installing':message='새 앱을 설치하고 실행 상태를 확인합니다.'
        elif phase=='verifying':message='새 연결 구성요소의 실행을 확인합니다.'
        elif phase=='complete':message='업데이트를 적용했습니다. 종료했던 앱을 다시 여세요.'
        elif phase in ('switching','stopping','starting'):message='연결을 잠시 사용할 수 없습니다. 교체가 끝날 때까지 기다리세요.'
        elif phase in ('partial','failed') and proxy.get('restored'):message='이전 프록시를 복원했습니다. 연결 적용을 다시 시도하세요.'
        self.status.setText(message);self.status.setVisible(bool(message));self.status.put(color=self.status_color(attention))
        partial=record.get('app',{}).get('state')=='verified' and phase not in ('complete','partial')
        self.components.setVisible(partial);self.components.setText('앱 설치 완료 / Codex 연결 적용 대기' if partial else '')
        if attention and not active and not self.execute.isVisible():self.button.show()
