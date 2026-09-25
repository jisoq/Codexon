"""Shared cache analysis/settings and confirmation surface."""
import json
import time
import sqlite3
from PySide6.QtCore import QTimer,Qt,Signal,QSignalBlocker
from .cache_control import Control,control_path
from .cache_execution import Journal
from .cache_hooks import configure
from .controls import Switch
from .presentation import Group,Column,Row,Text,Button
from .pricing import usd,token_cost
from .quick_runtime import Confirmation
from .ui_details import Details
from .table_model import LazyTable
from .i18n import Verbatim,formatted,tr


def copy(text='',style='muted'):
    node=Text(text);node.setObjectName(style);node.setWordWrap(True);node.setTextFormat(Qt.PlainText)
    return node


def card(title):
    node=Group();node.setObjectName('summary');layout=Column(node)
    layout.setContentsMargins(20,18,20,18);layout.setSpacing(12)
    layout.addWidget(copy(title,'section'))
    return node,layout

REASONS={
    'no_executable_rounds':'유지하지 않음 · 동의 기간 안에 실행할 시간 부족',
    'operating_consent_required':'비활성 · 제한 운용 범위에 대한 동의 필요',
    'operating_scope_unavailable':'범위 밖 · 계정·모델·effort·Standard HTTP 확인 필요',
    'scope_mismatch':'범위 밖 · 이번 허용 대상과 현재 문맥이 다름',
    'purpose_mismatch':'범위 밖 · 허용된 요청 목적과 다름',
    'projected_cost_stop':'실행하지 않음 · 관측 합계와 다음 예상액이 비용 기준 초과',
    'fresh_context_required':'자료 수집 중 · 새 연결의 완료된 문맥 필요',
    'user_active':'사용자 작업 중 · 독립 요청 보류',
    'diagnostic_completed':'진단 완료 · 응답·사용량 회수됨',
    'diagnostic_unknown':'진단 중단 · 응답 또는 사용량 미확인',
    'diagnostic_failed':'진단 중단 · 서버 오류 응답 확인',
    'diagnostic_invalidated':'진단 취소 · 사용자 작업 우선',
    'diagnostic_ready':'진단 준비됨',
    'verification_complete':'제한 검증 종료 · 추가 호출 권한 종료',
    'operating_cost_unobserved':'현재 비교 구간의 비용 예상에 필요한 자료 대기',
    'operation_busy':'앞선 유지 요청의 사용량 회수 중',
    'operation_deferred':'공유 운용 허용량 확인 대기',
    'permission_expired':'운용 기간 종료',
    'call_limit':'허용 호출 수 소진',
    'observed_cost_stop':'관측 비용 기준 도달 · 후속 호출 중단',
    'scope_cost_increased':'예상 부담 증가 · 새 범위 동의 필요',
    'usage_unresolved':'중단 · 사용량 또는 비용 미확인 기록 확인 필요',
    'request_failed':'중단 · 요청 오류',
    'cost_above_estimate':'중단 · 관측 비용이 예상 초과',
    'output_above_observed':'중단 · 출력이 참고 관측량 초과',
    'reuse_unconfirmed':'중단 · 원래 입력 재사용 확인 불가',
    'partial_reuse':'중단 · 예상한 기존 캐시 범위보다 적게 재사용',
    'revoked':'운용 허용 철회됨',
    'output_bound_not_verified':'실행 경로 확인 필요 · 이력 추가만으로 활성화되지 않음',
    'history_scope_unobserved':'현재 문맥과 자연 작업 이력 연결 대기',
    'output_budget_unobserved':'자연 작업의 출력 사용량 수집 대기',
    'input_or_price_unobserved':'비교 가능한 입력·요금 정보 수집 대기',
    'natural_cost_bounds_unobserved':'현재 비교 구간의 필수 사용량·비용 미관측',
    'need_independent_natural_return_history':'복귀 이력 수집 중',
    'no_positive_forward_estimate':'유지하지 않음 · 후속 구간의 예상 이득 없음',
    'natural_history_bounds':'유지 예약 · 자연 작업 이력 기준',
    'unknown':'중단 · 사용량 확인 불가',
    'failed':'중단 · 서버 오류 응답 확인',
    'completed':'유지 요청 완료 · 사용자 복귀 효과는 별도 확인',
    'invalidated':'사용자 작업으로 예약 해제',
    'duplicate':'기존 회차 기록 보존',
}


class CachePanel(Group):
    storage_ready=Signal()
    def __init__(self,home,index_path=None,active=True,parent=None):
        super().__init__(parent);self.home=str(home)
        self.path=':memory:' if not active and index_path is None else control_path(index_path)
        self.control=None;self.journal=None;self.open_storage()
        self.active=active;self.dialog=None;self.ticket=None;self.last_ticket=None;self.closed=False;self.proxy_ready=False;self.relay_connected=False
        self.operating_dialog=None
        layout=Column(self);layout.setContentsMargins(0,0,8,20);layout.setSpacing(20)
        layout.addWidget(copy('작업 사이의 캐시 재사용을 돕고, 모델 변경으로 입력 처리 부담이 커질 때 알려줍니다.'))
        state,body=card('현재 상태');self.status=copy('자연 작업 이력 수집 중','section');body.addWidget(self.status)
        self.next_action=copy('평소처럼 작업하면 관측 자료와 유지 판단이 갱신됩니다.');body.addWidget(self.next_action);layout.addWidget(state)
        metrics=Group();metrics.setObjectName('summary');columns=Row(metrics);columns.put(minColumnWidth=175)
        columns.setContentsMargins(20,18,20,18);columns.setSpacing(20);self.metrics={}
        for key,title,note in (('calls','독립 요청','유지와 진단 요청 합계'),('cost','관측 환산액','전체 사용량에도 포함'),
                               ('unknown','비용 미확인','확인 전 후속 실행 중단'),('saving','절감 효과','자연 복귀 후 별도 확인')):
            col=Column();col.setSpacing(6);col.addWidget(copy(title));value=copy('—','metric');value.put(fontSize=26,bold=True)
            col.addWidget(value);col.addWidget(copy(note));columns.addLayout(col,1);self.metrics[key]=value
        layout.addWidget(metrics)
        controls=Row();controls.put(collapseBelow=950,spacing=20);self.toggles={}
        for key,title,description in (
            ('automatic','자동 유지','돌아올 가능성과 예상 이득이 충분할 때, 허용된 범위에서 별도 요청으로 캐시 재사용을 돕습니다.'),
            ('guard','모델 변경 확인','다음 요청에서 모델 변경으로 입력 처리 부담이 크게 늘어날 때 진행 여부를 묻습니다. 추가 모델 요청은 보내지 않습니다.')):
            node,body=card(title);row=Row();row.addWidget(copy('개별 기능 켜기'),1)
            toggle=Switch();toggle.setAccessibleName(title)
            toggle.setChecked(self.control.get('selection:'+key,self.control.get(key,False)) if self.control else False)
            toggle.setEnabled(active and self.control is not None);self.toggles[key]=toggle
            toggle.toggled.connect(lambda value,k=key:self.set_feature(k,value));row.addWidget(toggle);body.addLayout(row)
            body.addWidget(copy(description));controls.addWidget(node,1)
        layout.addLayout(controls)
        layout.addWidget(copy('두 기능은 독립적으로 사용할 수 있습니다. 설정의 캐시 관리를 끄면 둘 다 중지됩니다.'))
        operation,body=card('자동 유지 판단과 운용 범위')
        self.operation_badge=copy('운용 허용 없음','section');body.addWidget(self.operation_badge)
        self.estimate=copy('현재 문맥의 예상 비용을 수집하고 있습니다.');body.addWidget(self.estimate)
        row=Row();row.put(flow=True)
        self.consent_button=Button('운용 범위 확인');self.revoke_button=Button('운용 허용 철회')
        self.consent_button.setEnabled(False);self.revoke_button.setEnabled(False)
        self.consent_button.clicked.connect(self.show_operating);self.revoke_button.clicked.connect(self.revoke_operating)
        row.addWidget(self.consent_button);row.addWidget(self.revoke_button);body.addLayout(row);layout.addWidget(operation)
        connection,body=card('연결과 관측')
        self.connection_status=copy('작업기 연결 확인 중','section');body.addWidget(self.connection_status)
        self.hook_status=copy('훅 실행 이력을 확인하고 있습니다.');body.addWidget(self.hook_status)
        row=Row();row.put(flow=True);self.connect_button=Button('훅 연결 갱신');self.disconnect_button=Button('훅 연결 해제')
        self.connect_button.setEnabled(active);self.disconnect_button.setEnabled(active)
        self.connect_button.clicked.connect(lambda:self.configure(True));self.disconnect_button.clicked.connect(lambda:self.configure(False))
        row.addWidget(self.connect_button);row.addWidget(self.disconnect_button);body.addLayout(row);layout.addWidget(connection)
        activity,body=card('최근 캐시 활동')
        body.addWidget(copy('관측한 변화입니다. 캐시 읽기 감소의 원인이나 절감 효과가 확정된 것은 아닙니다.'))
        self.activity=LazyTable(['작업','관측한 변화','캐시 읽기','캐시 쓰기']);self.activity.put(rowHeight=40,emptyText='아직 비교 가능한 활동이 없습니다.',leftColumns=[0,1])
        self.activity.setMinimumHeight(180);self.activity.setMaximumHeight(380)
        for column,width in enumerate((330,260,140,140)):self.activity.setColumnWidth(column,width)
        body.addWidget(self.activity);layout.addWidget(activity)
        details=Group();detail=Column(details);detail.setSpacing(14)
        self.forecast=copy();self.summary=copy();self.audit=copy();self.operating_status=copy('제한 운용 동의 없음');self.diagnostic_status=copy()
        self.activation=copy('시험한 ChatGPT HTTP 요청은 출력 상한 인자를 지원하지 않습니다. 제한 운용은 별도 동의가 필요합니다. '
            '호출 수는 제한하지만 한 요청의 출력·추론·총비용은 보장하지 않습니다. 시험 호출은 자동으로 보내지 않습니다.')
        for node in (self.forecast,self.summary,self.audit,self.operating_status,self.diagnostic_status,self.activation):detail.addWidget(node)
        detail.addWidget(copy('모델 변경 확인창을 닫거나 시간이 초과되면 그 요청은 중단됩니다. 확인 연결 장애 때는 새 요청의 확인을 건너뜁니다.'))
        layout.addWidget(Details('비용 근거와 진단 이력',details))
        self.timer=QTimer(self);self.timer.setInterval(300);self.timer.timeout.connect(self.poll)
        if active:self.timer.start()

    def open_storage(self):
        control=None
        try:
            control=Control(self.path,timeout=.05)
            journal=Journal(self.path,timeout=.05)
        except (sqlite3.Error,OSError):
            if control:control.close()
            return False
        self.control,self.journal=control,journal
        return True

    def restore_controls(self):
        if not self.control:return False
        enabled=self.control.get('enabled',self.control.get('automatic',False) or self.control.get('guard',False))
        for key,toggle in self.toggles.items():
            with QSignalBlocker(toggle):toggle.setChecked(self.control.get('selection:'+key,self.control.get(key,False)))
            toggle.setEnabled(self.active and enabled)
        return enabled

    def set_enabled(self,enabled):
        if not self.control:return
        self.control.set('enabled',bool(enabled))
        for key,toggle in self.toggles.items():
            toggle.setEnabled(self.active and enabled)
            self.set_feature(key,toggle.isChecked())
        if not enabled:
            self.revoke_operating();self.control.set('diagnostic_request',None)
            for ticket in self.control.requests():self.control.resolve(ticket,'dismiss')
        self.poll()

    def set_feature(self,key,value):
        if not self.control:return
        self.control.set('selection:'+key,bool(value))
        # Existing relays can drain without a restart: they understand these gates.
        self.control.set(key,bool(value) and self.control.get('enabled',True))
        if hasattr(self,'timer'):self.poll()

    def configure(self,enabled):
        try:
            configure(self.home,self.path,enabled)
            self.hook_status.setText('훅 파일 연결 완료 · Codex에서 신뢰 허용 필요' if enabled else 'Codexon 훅 연결 해제됨')
        except (OSError,ValueError,RuntimeError) as exc:self.hook_status.setText('훅 연결 실패: '+str(exc))

    def poll(self):
        try:
            if not self.control:
                if not self.open_storage():
                    self.status.setText('저장소 연결 재시도 중')
                    self.next_action.setText('다른 작업이 기록을 저장하고 있습니다. 연결되면 기존 설정과 기록을 불러옵니다.')
                    return
                self.restore_controls();self.storage_ready.emit()
            self.control.set('ui_heartbeat',time.time())
            waiting=self.control.requests()
            observed=self.ticket or self.last_ticket
            if observed:
                state=self.control.db.execute('SELECT state FROM cache_tickets WHERE id=?',(observed['id'],)).fetchone()
                if state and state[0]!='waiting':
                    self.hook_status.setText({'released':'확인 완료 · 훅에서 요청 진행 허용',
                        'approve':'확인 전달 중','unavailable':'확인 연결 장애 · 요청 진행 허용',
                        'timeout':'시간 초과 · 요청 중단','cancel':'취소 · 요청 중단',
                        'dismiss':'확인창 닫힘 · 요청 중단'}.get(state[0],state[0]))
                    if self.dialog:self.dialog.reject()
            if not self.dialog and waiting:self.show_ticket(waiting[0])
            states=[json.loads(r[0]) for r in self.control.db.execute('SELECT data FROM cache_status WHERE home=?',(self.home,))]
            forecasts=self.control.forecasts(self.home,time.time())
            if forecasts:
                s=max(forecasts,key=lambda s:s.get('observed_at',0))
                self.estimate.setText(formatted('유지 1회 예상 {v0} · 재사용 실패 시 {v1}\nAPI 환산 예상이며 실제 비용 상한이 아닙니다.', v0=usd(s['maintenance_expected']), v1=usd(s['maintenance_adverse'])))
                self.forecast.setText(Verbatim(formatted('현재 문맥 예상 · {v0} · {v1} · {v2}\n사용자 {v3} → 별도 유지 {v4}\n1회 예상 C {v5} · 재사용 실패 시나리오 {v6} · 2C {v7}\nAPI 환산 시나리오 · 총비용 상한이나 유지 효과 실측이 아닙니다. 관측만으로 실행되지 않습니다.', v0=s.get('model'), v1=s.get('effort'), v2=s.get('service_tier'), v3=s.get('source_transport'), v4=s.get('maintenance_transport'), v5=usd(s['maintenance_expected']), v6=usd(s['maintenance_adverse']), v7=usd(s['cost_stop_scenario']))
                    +tr(' 새 실행 경로의 문맥은 아직 미확인입니다.' if self.control.get('worker_heartbeat') and s.get('worker_revision')!=3 else
                      ' 현재 문맥은 실행 범위 밖입니다.' if not s.get('operating_scope_available') else '')))
            else:
                self.forecast.setText('');self.estimate.setText('현재 문맥의 예상 비용을 수집하고 있습니다.')
            self.poll_operating()
            heartbeat=self.control.get('worker_heartbeat',0)
            if heartbeat and time.time()-heartbeat<5:self.proxy_ready=True
            self.connection_status.setText('작업기 연결됨' if heartbeat and time.time()-heartbeat<20 else
                '프록시 연결됨 · 분석 갱신 확인 필요' if self.relay_connected else '작업기 연결 확인 필요')
            diagnostic=self.control.get('diagnostic_result',{})
            self.diagnostic_status.setText('연결 진단 · '+REASONS.get(diagnostic.get('reason'),diagnostic.get('state','')) if diagnostic else '')
            count=self.control.db.execute('SELECT COUNT(*) FROM cache_inputs WHERE home=?',(self.home,)).fetchone()[0]
            if not observed:self.hook_status.setText(formatted('실제 훅 적재 {v0}건', v0=count) if count else '훅 이벤트 미수집 · 신뢰 설정과 실제 실행은 별도입니다')
            if heartbeat and not self.control.get('worker_snapshots',0):self.status.setText('기존 연결 관측 중 · 새 작업기 문맥 수집 중')
            elif any(s.get('observation_only') for s in states):self.status.setText('관측 전용 · 추가 유지 요청 없음 · 사용자 연결 방식 변경 불필요')
            elif not self.control.enabled('automatic'):
                self.status.setText('자동 유지 꺼짐')
            elif not self.proxy_ready:self.status.setText('자동 유지 대기 · 캐시 관리를 지원하는 프록시 연결 필요')
            elif states:
                latest=max(states,key=lambda s:s.get('observed_at',0))
                self.status.setText(REASONS.get(latest.get('reason',latest.get('state')),'자료 수집 중'))
            else:self.status.setText('자료 수집 중')
            if self.control.get('collector_error') or self.control.get('worker_error'):
                self.status.setText('장애 · 관측 저장 또는 분석 처리 확인 필요')
            elif heartbeat and time.time()-heartbeat>=20:
                self.proxy_ready=False;self.status.setText('분석 갱신 멈춤 · 작업기 재시작 필요')
            if not self.control.get('enabled',True):self.status.setText('캐시 관리 꺼짐')
            self.next_action.setText('관측 자료가 모이면 자동으로 다시 판단합니다. 이력이 부족하거나 예상 이득이 없으면 유지 요청을 보내지 않습니다.'
                if not any(s.get('state')=='eligible' for s in states) else '실행 전 사용자 작업 여부와 남은 운용 범위를 다시 확인합니다.')
            if heartbeat and time.time()-heartbeat>=20:
                self.next_action.setText('현재 요청 연결은 보존합니다. 다음 Windows 시작 시 새 작업기가 적용됩니다.')
        except Exception:
            # Do not permanently lose observation after a transient database lock.
            # No successful poll means no fresh heartbeat for waiting hooks.
            self.hook_status.setText('확인 저장소 연결 재시도 중 · 확인되지 않은 요청은 기존 제한시간을 따릅니다')

    def poll_operating(self):
        grants=self.journal.operations.grants(self.home)
        self.consent_button.setEnabled(self.active and self.control.get('enabled',True) and any(p.get('purpose','maintenance')=='maintenance'
            for p in self.journal.operations.proposals(self.home)) and not self.operating_dialog)
        self.revoke_button.setEnabled(self.active and any(not g['stopped'] and g['expires']>time.time() for g in grants))
        if grants:
            g=grants[0];s=self.journal.operations.stats(g)
            reason=g['stopped'] or ('permission_expired' if g['expires']<=time.time() else None)
            maintenance=next((item for item in grants if item.get('purpose','maintenance')=='maintenance'),None)
            if maintenance:
                reason_maintenance=maintenance['stopped'] or ('permission_expired' if maintenance['expires']<=time.time() else None)
                self.operation_badge.setText(REASONS.get(reason_maintenance,'운용 종료') if reason_maintenance else
                    formatted('운용 허용됨 · 남은 {v0}회', v0=self.journal.operations.stats(maintenance)['remaining']))
            else:self.operation_badge.setText('자동 유지 운용 허용 없음')
            self.operating_status.setText(formatted('계정 {v0} · {v1} · 남은 {v2}/{v3}회\nAPI 환산 관측 확인분 {v4} / 후속 중단 기준 {v5} · 비용 미확인 {v6}회\n{v7} · 종료 {v8}', v0=g['scope']['account'][:12], v1=g['scope']['model'], v2=s['remaining'], v3=g['max_calls'], v4=usd(s['observed']), v5=usd(g['cost_stop']), v6=s['unknown'], v7=tr(REASONS.get(reason, reason) if reason else '진단만 허용됨' if g.get('purpose') == 'diagnostic' else '허용됨 · 자동 정책이 이득 있는 경우만 예약'), v8=time.strftime('%H:%M', time.localtime(g['expires']))))

    def show_operating(self):
        if self.operating_dialog:return
        proposals=[p for p in self.journal.operations.proposals(self.home) if p.get('purpose','maintenance')=='maintenance']
        if not proposals:return
        proposal=proposals[0];scope=proposal['scope']
        text=(formatted('계정 {v0}\n홈 {v1}\n{v2} · {v3} · Standard · HTTP\n사용자 대화는 기존 연결 유지 · 복원한 전체 문맥으로 별도 HTTP 유지 요청\n동의 후 60분, 모든 세션 합계 최대 2회, 순차 실행\n1회 예상 {v4}, 캐시 재사용 실패 시나리오 {v5}\n관측 API 환산 합계 {v6} 도달 시 후속 호출 중단\n\n예상치는 자연 작업의 출력·추론과 현재 문맥에 근거합니다. 실측 유지 기록이 있으면 함께 반영합니다. 서버측 출력 상한은 보장되지 않으며, 한 호출이 위 예상·시나리오·중단 기준을 초과할 수 있습니다. 30초 연결 제한도 서버 처리·비용 중단 보장이 아닙니다. 오류·미확인 비용·예상 초과·재사용 저하는 후속 실행을 중단합니다. 이는 구독 한도나 실제 청구액이 아닙니다. 자동 정책이 순이득 없음을 선택하면 호출하지 않습니다.\n이 범위를 허용할까요? 자동 유지 설정과 프록시·훅 연결은 별도로 필요합니다.', v0=scope['account'][:12], v1=scope['home'], v2=scope['model'], v3=scope['effort'], v4=usd(proposal['expected']), v5=usd(proposal['adverse']), v6=usd(proposal['cost_stop'])))
        self.operating_dialog=Confirmation(tr('총비용 상한 없는 제한 운용'),text,self,scrollable=True)
        self.operating_dialog.resize(650,580)
        self.operating_dialog.confirm.setText('이 범위 허용')
        self.operating_dialog.finished.connect(lambda result:self.operating_decided(result,proposal['id']))
        self.operating_dialog.open()

    def operating_decided(self,result,proposal_id):
        self.operating_dialog=None
        if self.closed:return
        if result==1:
            try:self.journal.operations.consent(proposal_id)
            except ValueError as exc:
                self.operating_status.setText({'usage_unresolved':'비용 미확인 기록을 먼저 확인해야 합니다. 새 동의로 무시할 수 없습니다.',
                    'existing_permission':'기존 범위가 유효합니다. 확대하려면 먼저 철회하고 새 범위를 확인하세요.',
                    'proposal_expired':'예상 자료가 오래되었습니다. 새 정책 평가 후 확인하세요.'}.get(str(exc),'운용 동의를 저장하지 못했습니다.'))
                return
        self.poll_operating()

    def revoke_operating(self):
        for grant in self.journal.operations.grants(self.home):self.journal.operations.stop(grant['id'],'revoked')
        self.poll_operating()

    def proxy_status(self,state):
        health=state.get('health') or {}
        self.proxy_ready=bool(health.get('cache_management') and state.get('configured'))
        self.relay_connected=self.proxy_ready

    def show_ticket(self,ticket):
        self.ticket=ticket;self.choice='dismiss'
        self.dialog=Confirmation(tr('모델 변경 확인'),formatted('{v0} 모델로 이번 요청을 진행할까요?\n같은 크기의 입력을 새 모델에서 처리하면 부담이 커질 수 있습니다. 기존 모델의 캐시가 삭제된다는 뜻은 아닙니다.\n60초 안에 선택하세요. 닫기·시간 초과는 이번 요청을 중단합니다.', v0=ticket['model']),self)
        self.dialog.confirm.setText('이 요청 진행')
        self.dialog.cancel.clicked.connect(lambda:setattr(self,'choice','cancel'))
        self.dialog.finished.connect(self.decided);self.dialog.open()

    def decided(self,result):
        if self.closed:return
        if self.ticket:
            changed=self.control.resolve(self.ticket,'approve' if result==1 else self.choice)
            if changed:self.hook_status.setText('확인 전달 중' if result==1 else '요청 중단 전달 중')
        self.last_ticket=self.ticket
        self.dialog=None;self.ticket=None

    def display(self,data):
        self.metrics['calls'].setText(formatted('{v0:,}회', v0=data.get('calls', 0)))
        self.metrics['cost'].setText(usd(data.get('known_cost')) if data.get('priced') else '—')
        self.metrics['unknown'].setText(formatted('{v0:,}회', v0=data.get('calls', 0) - data.get('priced', 0)))
        self.metrics['saving'].setText('미확인')
        self.summary.setText(formatted('독립 요청 {v0}회 (진단 {v1}회) · API 환산 확인분 {v2} · 비용 미확인 {v3}회\n최근 비교 가능한 읽기 감소 {v4}회 · 절감 실측: 미확인', v0=data.get('calls', 0), v1=data.get('diagnostic_calls', 0), v2=usd(data.get('known_cost')) if data.get('priced') else '—', v3=data.get('calls', 0) - data.get('priced', 0), v4=data.get('shortfalls', 0)))
        names=dict(session_start='시작',model_changed='모델 변경',effort_changed='effort 변경',
                   service_tier_changed='모드 변경',idle_over_design_lifetime='30분 이상 간격',compaction='압축')
        lines=[];activities=[]
        for row in data.get('audit',[])[-12:]:
            changes=' · '.join(tr(names.get(c,c)) for c in row['changes'])
            scope=formatted('읽기 감소 시나리오 {v0:,}토큰', v0=row['reuse_shortfall_scenario']) if row.get('reuse_shortfall_scenario') else ''
            if changes or scope:
                activities.append([Verbatim(row['title']),changes or scope,
                    f"{row['read']:,}" if row.get('read') is not None else '미관측',
                    f"{row['written']:,}" if row.get('written') is not None else '미관측'])
                lines.append(formatted('{v0} · {v1} · 읽기 {v2} / 쓰기 {v3}', v0=row['title'], v1=changes or scope, v2=tr(row.get('read') if row.get('read') is not None else '미관측'), v3=tr(row.get('written') if row.get('written') is not None else '미관측')))
        self.audit.setText(Verbatim(tr('최근 캐시 분석 · 선행 변화는 확정 원인이 아닙니다\n')+'\n'.join(lines)))
        self.activity.set_rows(list(reversed(activities)),lambda row,col,role:row[col])
        effects=[e for e in data.get('effects',[]) if e.get('user_response')]
        if effects:
            e=effects[-1]
            self.audit.setText(Verbatim(self.audit.text()+formatted('\n최근 유지 후 사용자 요청: 입력 {v0} / 캐시 읽기 {v1} · 유지 {v2}회 · 인과적 절감 미확인', v0=e['user_input'], v1=e['user_read'], v2=e['maintenance_calls'])))
        compactions=data.get('compactions',[]);delegation=data.get('delegation',{})
        auxiliary=formatted('\n보조 분석 · 압축 관측 {v0}회 · 하위 작업 {v1}호출', v0=len(compactions), v1=delegation.get('calls', 0))
        if compactions:
            c=compactions[-1];auxiliary+=formatted('\n최근 압축 전후 입력 {v0} → {v1}토큰 · 맥락 품질 별도 확인', v0=c['before'], v1=c['after'])
        if delegation.get('calls'):
            auxiliary+=formatted(' · API 환산 확인분 {v0} · 산정 {v1}호출', v0=usd(delegation.get('known_cost')), v1=delegation.get('priced', 0))
        self.audit.setText(Verbatim(self.audit.text()+auxiliary))

    def stop(self):
        self.timer.stop()
        if self.closed:return
        if self.ticket:self.control.resolve(self.ticket,'dismiss')
        self.closed=True
        if self.dialog:self.dialog.reject()
        if self.operating_dialog:self.operating_dialog.reject()
        if self.control:self.control.close()
        if self.journal:self.journal.close()
