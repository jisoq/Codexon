"""One compact settings row backed by a durable update operation."""
from PySide6.QtCore import QFileSystemWatcher, QThread, QTimer, Signal
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
    fault_changed=Signal(object)
    continue_app_requested=Signal(object)

    def __init__(self,manager,parent=None):
        super().__init__(parent)
        self.manager=manager;self.owner=parent;self.operation=None;self.monitor=None
        self.metadata_pending=False;self.checking=False;self.operation_error=False
        self.offer=None;self.discovered_offer=None;self.proxy_update={};self.record={}
        self.pending_plan=None;self.auto_start=False;self._continue_app=None
        self._continue_pending=None;self._continued_operations=set();self._deferred_metadata=[]
        self.next_recovery_check=0;self._stopped=False;self._exit_pending=False;self._journal_signature=None
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
        self.cancel=Button('취소');self.cancel.put(role='quiet');self.cancel.clicked.connect(lambda:self.cancel_update(False));secondary.addWidget(self.cancel);self.cancel.hide()
        self.later=Button('나중에 적용');self.later.put(role='quiet');self.later.clicked.connect(lambda:self.cancel_update(True));secondary.addWidget(self.later);self.later.hide()
        secondary.addStretch();controls.addLayout(secondary);row.addLayout(controls,1);layout.addLayout(row)
        self.journal_watcher=QFileSystemWatcher(self)
        self.journal_watcher.fileChanged.connect(self.refresh)
        self.journal_watcher.directoryChanged.connect(self.refresh)
        self.timer=QTimer(self);self.timer.setInterval(30000);self.timer.timeout.connect(self.refresh)
        self.recovery_timer=QTimer(self);self.recovery_timer.setSingleShot(True);self.recovery_timer.timeout.connect(self.recover)
        if self.journal:self.timer.start()
        from .theme import shared_theme
        shared_theme().changed.connect(self.render_record)
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
        return self._stopped or self._exit_pending or bool(getattr(self.owner,'_closing',False))

    def set_exit_pending(self, pending):
        self._exit_pending=bool(pending)
        if not pending:
            self.button.setEnabled(not self.operation and not self.metadata_pending)
            self.execute.setEnabled(not self.operation)
            self.recheck.setEnabled(not self.operation)
        self.refresh()

    def stop(self):
        self._stopped=True;self.timer.stop();self.recovery_timer.stop()
        paths=self.journal_watcher.files()+self.journal_watcher.directories()
        if paths:self.journal_watcher.removePaths(paths)

    def setVisible(self,value):
        super().setVisible(value)
        if value:self.refresh()

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
        if self.closing():
            self._deferred_metadata=[result]
            return
        self.metadata_pending=False
        if isinstance(result,Exception):self.receive_result(result);self.finished();return
        from .app_update import check_update
        self.launch_operation(lambda:check_update(self.status_progress,self.manager,release=result))

    def status_progress(self,text):
        operation=self.operation
        if operation:operation.progress.emit(text)

    def receive_result(self,result):
        if self._stopped:return
        if isinstance(result,Exception):
            self.auto_start=False;self.operation_error=True;self.offer=None
            self.status.setText(failure_message(result,self.checking));self.status.setToolTip(str(result));self.status.put(color=self.status_color(True))
            self.execute.hide();self.version.hide();self.components.hide();self.button.show()
            self.fault_changed.emit(dict(checking_failed=bool(self.checking),
                phase=None if self.checking else 'failed',operation_error=True))
        elif isinstance(result,dict) and result.get('kind')=='continue_app':
            self._continue_app=result
        elif isinstance(result,dict) and 'operation_id' in result:
            self.record=result;self.offer=None
        elif isinstance(result,dict) and ('configured' in result or 'health' in result):self.proxy_status(result)
        elif isinstance(result,dict):
            self.pending_plan=result
            if self.checking and self.monitor is None:self.fault_changed.emit(dict(check_healthy=True))
        else:self.status.setText(str(result))

    def finished(self):
        operation=self.operation;self.operation=None
        if operation:operation.deleteLater()
        plan=self.pending_plan;self.pending_plan=None
        if plan:self.show_plan(plan)
        if self.auto_start:
            self.auto_start=False
            if plan and plan['kind'] in ('app','proxy'):self.request_install();return
        self.button.setEnabled(True);self.execute.setEnabled(True)
        self.reload_record(force=True);self.render_record()
        continuation=self._continue_app;self._continue_app=None
        if (continuation and not self.closing()
                and continuation['verified_operation_id']==self.record.get('operation_id')
                and continuation['executable']==self.record.get('app',{}).get('executable')):
            self.continue_app_requested.emit(dict(executable=continuation['executable'],
                                                 verified_operation_id=continuation['verified_operation_id']))
        self._start_continuation()

    def continue_update(self,operation_id):
        from .update_state import valid_id
        if self._stopped or not valid_id(operation_id) or operation_id in self._continued_operations:
            return False
        self._continue_pending=operation_id
        return self._start_continuation()

    def _start_continuation(self):
        operation_id=self._continue_pending
        if not operation_id or self.closing():return False
        if self.operation or self.metadata_pending:return True
        self.reload_record(force=True)
        if (self.record.get('operation_id')!=operation_id
                or self.record.get('phase') not in ('partial','failed','deferred','interrupted')
                or self.record.get('app',{}).get('state')!='verified'):
            self._continue_pending=None
            return False
        self._continue_pending=None;self._continued_operations.add(operation_id)
        from .app_update import retry_proxy
        self.checking=False;self.operation_error=False
        self.launch_operation(lambda:retry_proxy(self.manager,self.journal,verified_operation_id=operation_id))
        return self.operation is not None

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

    def needs_new_app(self):
        import sys
        from pathlib import Path
        app=self.record.get('app',{})
        return (self.record.get('phase') in ('partial','failed','deferred','interrupted')
                and app.get('state')=='verified' and app.get('executable')
                and Path(app['executable']).resolve()!=Path(sys.executable).resolve())

    def request_install(self):
        if self.busy() or self.closing():return
        if self.needs_new_app():
            from .app_update import continuation_target
            self.checking=False;self.operation_error=False
            self.launch_operation(lambda:continuation_target(self.journal,self.manager));return
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
        if self.closing() or not self.journal or not self.journal.cancel(later=later):return
        self.reload_record(force=True);phase=self.record.get('phase')
        if phase in ('waiting','needs_exit') and self.manager and not self.operation:
            self.checking=False;self.launch_operation(self.manager.cancel_update)
        self.render_record()
        self.cancel.setEnabled(False);self.later.setEnabled(False)
        self.status.setText('나중에 적용하도록 마무리합니다.' if later else '업데이트 취소 중…')

    def proxy_status(self,result):
        self.proxy_update=result.get('update') or {}
        unknown=((result.get('health') or {}).get('websocket_states') or {}).get('unknown',0)
        if self.proxy_update.get('phase')=='waiting' and unknown:
            self.proxy_update={**self.proxy_update,'required_action':'close_client','unknown_connections':unknown}
        self.refresh()

    def reload_record(self,*,force=False):
        if not self.journal or self._stopped:return
        path=self.journal.path
        try:
            state=path.stat()
            signature=(str(path),state.st_size,state.st_mtime_ns,state.st_ctime_ns,state.st_ino)
        except OSError:signature=(str(path),None)
        changed=signature!=self._journal_signature
        watcher=self.journal_watcher;file=str(path);directory=path.parent
        if changed and file in watcher.files():watcher.removePath(file)
        while not directory.is_dir() and directory!=directory.parent:directory=directory.parent
        directory=str(directory)
        if directory not in watcher.directories():watcher.addPath(directory)
        if directory in watcher.directories():
            obsolete=[entry for entry in watcher.directories() if entry!=directory]
            if obsolete:watcher.removePaths(obsolete)
        if signature[1] is not None and file not in watcher.files():watcher.addPath(file)
        if force or changed:
            self.record=self.journal.read()
            self._journal_signature=signature if self.record or signature[1] is None else None

    def refresh(self,*_):
        if self._stopped:return
        if self._deferred_metadata and not self.closing():
            result=self._deferred_metadata.pop()
            self.metadata_received(result)
        self.reload_record();self.render_record()

    def recover(self):
        if self.closing():return
        record=self.record;phase=record.get('phase');proxy=record.get('proxy',{})
        legacy_wait=phase in ('waiting','needs_exit') and proxy.get('source_instance') and not proxy.get('operation_id')
        self.recovery_timer.stop()
        from .update_state import ACTIVE
        if not self.journal or self.operation or not (phase in ACTIVE or legacy_wait):return
        remaining=self.next_recovery_check-time.monotonic()
        if remaining>0:
            self.recovery_timer.start(max(1,int(remaining*1000)+1));return
        self.next_recovery_check=time.monotonic()+30
        from .app_update import reconcile_update
        self.checking=False;self.launch_operation(lambda:reconcile_update(self.journal,self.manager))

    def render_record(self):
        self._render_record()
        if self.closing():
            for control in (self.button,self.execute,self.recheck,self.cancel,self.later):
                control.setEnabled(False)

    def _render_record(self):
        from .update_state import ACTIVE,CRITICAL
        if self._stopped:return
        self.recover()
        record=self.record;phase=record.get('phase');proxy=record.get('proxy',{})
        if not record and self.proxy_update.get('phase') in ('queued','waiting','switching','stopping','starting','verifying','rollback','interrupted','failed'):
            proxy=self.proxy_update;phase=proxy.get('phase')
            if phase=='waiting' and proxy.get('required_action')=='close_client':phase='needs_exit'
        active=phase in ACTIVE
        critical=phase in CRITICAL or (active and proxy.get('cutover_started'))
        attention=phase in ('needs_exit','partial','failed','interrupted','deferred')
        fault=dict(phase=phase,proxy=dict(phase=proxy.get('phase')),app=dict(state=record.get('app',{}).get('state')),
            cancel_requested=record.get('cancel_requested',False),
            status_healthy=phase in ('complete','cancelled'))
        if fault!=getattr(self,'_reported_fault',None):
            self._reported_fault=fault;self.fault_changed.emit(fault)
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
        self.execute.setText('새 앱으로 계속' if self.needs_new_app() else '다시 적용');self.execute.setEnabled(not self.operation)
        self.version.hide()
        headings={'preparing':'업데이트 준비','downloading':'업데이트 준비','installing':'앱 설치 중',
                  'queued':'응답 마무리 대기','waiting':'응답 마무리 대기','needs_exit':'연결한 앱 종료 필요',
                  'switching':'프록시 교체 중','stopping':'프록시 교체 중','starting':'프록시 교체 중','verifying':'검증 중',
                  'rollback':'이전 프록시 복원 중','complete':'업데이트 적용 완료',
                  'partial':'앱 설치 완료, 연결 적용 대기','deferred':'연결 적용 대기',
                  'failed':'업데이트 실패','interrupted':'업데이트 중단','cancelled':'업데이트 취소됨'}
        self.heading.setText(headings.get(phase,'업데이트'))
        self.heading.put(fontSize=14,bold=False,color='muted')
        self.heading.parent().setSpacing(8)
        message=record.get('message') or proxy.get('message','')
        if phase=='needs_exit':
            message='연결한 앱이 종료되기를 기다리고 있습니다.\n사용 중인 ChatGPT 앱과 Codex CLI를 종료하세요.\nCodexon은 켜 두세요.'
        elif phase=='waiting':
            count=proxy.get('responding',0)
            message=('진행 중인 응답이 끝나기를 기다리고 있습니다.\n새 요청은 업데이트 완료 후 보내세요.' if count else
                     '기존 연결의 응답 전달과 기록 저장이 끝나기를 기다리고 있습니다.')
        elif phase=='downloading':message='설치 파일을 다운로드하고 무결성을 확인하고 있습니다.'
        elif phase=='installing':message='새 버전을 설치하고 실행 상태를 확인하고 있습니다.'
        elif phase=='verifying':message='새 프록시가 정상적으로 실행됐는지 확인하고 있습니다.'
        elif phase=='complete':message='업데이트 적용을 마쳤습니다.'
        elif phase=='switching':message='기존 연결을 마무리하고 프록시를 교체하고 있습니다.'
        elif phase=='stopping':message='기존 프록시가 종료되기를 기다리고 있습니다.'
        elif phase=='starting':message='새 프록시를 실행하고 있습니다.'
        elif phase in ('partial','failed') and proxy.get('restored'):
            self.heading.setText('업데이트 실패 / 이전 프록시 복원')
            message='ChatGPT 앱을 다시 여세요.'
        self.status.setText(message);self.status.setVisible(bool(message));self.status.put(fontSize=16,bold=True,color='ink')
        self.components.hide()
        if attention and not active and not self.execute.isVisible():self.button.show()
