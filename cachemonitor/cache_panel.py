"""Shared cache analysis/settings and confirmation surface."""
import json
import time
from PySide6.QtCore import QTimer,Qt
from .cache_control import Control,control_path
from .cache_execution import Journal
from .cache_hooks import configure
from .controls import Switch
from .presentation import Group,Column,Row,Text,Button
from .pricing import usd,token_cost
from .quick_runtime import Confirmation

REASONS={
    'operating_consent_required':'비활성 · 제한 운용 범위에 대한 동의 필요',
    'operating_scope_unavailable':'비활성 · 초기 운용 대상은 계정이 확인된 Luna·low·Standard HTTP입니다',
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
    'completed':'유지 요청 완료 · 사용자 복귀 효과는 별도 확인',
    'invalidated':'사용자 작업으로 예약 해제',
    'duplicate':'기존 회차 기록 보존',
}


class CachePanel(Group):
    def __init__(self,home,index_path=None,active=True,parent=None):
        super().__init__(parent);self.home=str(home)
        self.path=':memory:' if not active and index_path is None else control_path(index_path)
        self.control=Control(self.path);self.active=active;self.dialog=None;self.ticket=None;self.last_ticket=None;self.closed=False;self.proxy_ready=False
        self.journal=Journal(self.path);self.operating_dialog=None
        layout=Column(self);layout.setSpacing(16)
        layout.addWidget(Text('캐시 관리'))
        for key,title in (('automatic','자동 유지'),('guard','모델 변경 확인')):
            row=Row();row.addWidget(Text(title),1);toggle=Switch();toggle.setChecked(self.control.get(key,False))
            toggle.toggled.connect(lambda value,k=key:self.control.set(k,value));row.addWidget(toggle);layout.addLayout(row)
        self.status=Text('자연 작업 이력 수집 중');self.status.setWordWrap(True);layout.addWidget(self.status)
        self.summary=Text();self.summary.setWordWrap(True);self.summary.setTextFormat(Qt.PlainText);layout.addWidget(self.summary)
        self.audit=Text();self.audit.setWordWrap(True);self.audit.setTextFormat(Qt.PlainText);layout.addWidget(self.audit)
        row=Row();connect=Button('Codex 훅 연결');disconnect=Button('훅 연결 해제')
        connect.setEnabled(active);disconnect.setEnabled(active)
        connect.clicked.connect(lambda:self.configure(True));disconnect.clicked.connect(lambda:self.configure(False))
        row.addWidget(connect);row.addWidget(disconnect);layout.addLayout(row)
        self.hook_status=Text('훅 연결 후 Codex에서 신뢰를 허용하고 새 작업을 시작하세요.');self.hook_status.setWordWrap(True);layout.addWidget(self.hook_status)
        info=Text('자동 유지에는 프록시와 훅이 필요합니다. 유지 사용량은 전체 합계에 포함됩니다. '
                  '모델 변경 확인은 다음 요청에서 동작하며, 닫기·시간 초과는 그 요청을 중단합니다. 연결 장애 때는 확인을 건너뜁니다.')
        info.setWordWrap(True);layout.addWidget(info)
        self.activation=Text('시험한 ChatGPT HTTP 요청은 출력 상한 인자를 지원하지 않습니다. 제한 운용은 별도 동의가 필요합니다. '
            '호출 수는 제한하지만 한 요청의 출력·추론·총비용은 보장하지 않습니다. 시험 호출은 자동으로 보내지 않습니다.')
        self.activation.setWordWrap(True);layout.addWidget(self.activation)
        self.operating_status=Text('제한 운용 동의 없음');self.operating_status.setWordWrap(True);layout.addWidget(self.operating_status)
        row=Row();self.consent_button=Button('초기 운용 범위 확인');self.revoke_button=Button('운용 허용 철회')
        self.consent_button.setEnabled(False);self.revoke_button.setEnabled(False)
        self.consent_button.clicked.connect(self.show_operating);self.revoke_button.clicked.connect(self.revoke_operating)
        row.addWidget(self.consent_button);row.addWidget(self.revoke_button);layout.addLayout(row)
        self.timer=QTimer(self);self.timer.setInterval(300);self.timer.timeout.connect(self.poll)
        if active:self.timer.start()

    def configure(self,enabled):
        try:
            configure(self.home,self.path,enabled)
            self.hook_status.setText('훅 파일 연결 완료 · Codex에서 신뢰 허용 필요' if enabled else 'Codexon 훅 연결 해제됨')
        except (OSError,ValueError,RuntimeError) as exc:self.hook_status.setText('훅 연결 실패: '+str(exc))

    def poll(self):
        try:
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
            self.poll_operating()
            if not self.control.get('automatic',False):self.status.setText('자동 유지 꺼짐 · 사용량 분석은 계속됩니다')
            elif not self.proxy_ready:self.status.setText('자동 유지 대기 · 캐시 관리를 지원하는 프록시 연결 필요')
            elif states:
                self.status.setText(' / '.join(dict.fromkeys(REASONS.get(s.get('reason',s.get('state')),s.get('state','대기')) for s in states)))
        except Exception:
            # Stop heartbeats: outstanding hooks will follow the infrastructure policy.
            self.timer.stop();self.hook_status.setText('확인 저장소 장애 · 대기 요청은 중단될 수 있으며 새 요청은 확인을 건너뜁니다')

    def poll_operating(self):
        grants=self.journal.operations.grants(self.home)
        self.consent_button.setEnabled(self.active and bool(self.journal.operations.proposals(self.home)) and not self.operating_dialog)
        self.revoke_button.setEnabled(self.active and any(not g['stopped'] and g['expires']>time.time() for g in grants))
        if grants:
            g=grants[0];s=self.journal.operations.stats(g)
            reason=g['stopped'] or ('permission_expired' if g['expires']<=time.time() else None)
            self.operating_status.setText(f"계정 {g['scope']['account'][:12]} · {g['scope']['model']} · 남은 {s['remaining']}/{g['max_calls']}회\n"
                f"API 환산 관측 확인분 {usd(s['observed'])} / 후속 중단 기준 {usd(g['cost_stop'])} · 비용 미확인 {s['unknown']}회\n"
                f"{REASONS.get(reason,reason) if reason else '허용됨 · 자동 정책이 이득 있는 경우만 예약'} · 종료 {time.strftime('%H:%M',time.localtime(g['expires']))}")

    def show_operating(self):
        if self.operating_dialog:return
        proposals=self.journal.operations.proposals(self.home)
        if not proposals:return
        proposal=proposals[0];scope=proposal['scope']
        text=(f"계정 {scope['account'][:12]}\n홈 {scope['home']}\n"
            f"{scope['model']} · {scope['effort']} · Standard · HTTP\n"
            f"동의 후 60분, 모든 세션 합계 최대 2회, 순차 실행\n"
            f"1회 예상 {usd(proposal['expected'])}, 캐시 재사용 실패 시나리오 {usd(proposal['adverse'])}\n"
            f"관측 API 환산 합계 {usd(proposal['cost_stop'])} 도달 시 후속 호출 중단\n\n"
            '예상치는 자연 작업의 출력·추론과 현재 문맥에 근거합니다. 실측 유지 기록이 있으면 함께 반영합니다. '
            '서버측 출력 상한은 보장되지 않으며, 한 호출이 위 예상·시나리오·중단 기준을 초과할 수 있습니다. '
            '30초 연결 제한도 서버 처리·비용 중단 보장이 아닙니다. 오류·미확인 비용·예상 초과·재사용 저하는 후속 실행을 중단합니다. '
            '이는 구독 한도나 실제 청구액이 아닙니다. 자동 정책이 순이득 없음을 선택하면 호출하지 않습니다.\n'
            '이 범위를 허용할까요? 자동 유지 설정과 프록시·훅 연결은 별도로 필요합니다.')
        self.operating_dialog=Confirmation('총비용 상한 없는 제한 운용',text,self)
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

    def show_ticket(self,ticket):
        self.ticket=ticket;self.choice='dismiss'
        self.dialog=Confirmation('모델 변경 확인',f"{ticket['model']} 모델로 이번 요청을 진행할까요?\n"
            '같은 크기의 입력을 새 모델에서 처리하면 부담이 커질 수 있습니다. 기존 모델의 캐시가 삭제된다는 뜻은 아닙니다.\n'
            '60초 안에 선택하세요. 닫기·시간 초과는 이번 요청을 중단합니다.',self)
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
        self.summary.setText(f'유지 요청 {data.get("calls",0)}회 · API 환산 확인분 {usd(data.get("known_cost")) if data.get("priced") else "—"} · 비용 미확인 {data.get("calls",0)-data.get("priced",0)}회\n'
            f'최근 비교 가능한 읽기 감소 {data.get("shortfalls",0)}회 · 절감 실측: 미확인')
        names=dict(session_start='시작',model_changed='모델 변경',effort_changed='effort 변경',
                   service_tier_changed='모드 변경',idle_over_design_lifetime='30분 이상 간격',compaction='압축')
        lines=[]
        for row in data.get('audit',[])[-12:]:
            changes=' · '.join(names.get(c,c) for c in row['changes'])
            scope=f"읽기 감소 시나리오 {row['reuse_shortfall_scenario']:,}토큰" if row.get('reuse_shortfall_scenario') else ''
            if changes or scope:
                lines.append(f"{row['title']} · {changes or scope} · 읽기 {row.get('read') if row.get('read') is not None else '미관측'} / 쓰기 {row.get('written') if row.get('written') is not None else '미관측'}")
        self.audit.setText('최근 캐시 분석 · 선행 변화는 확정 원인이 아닙니다\n'+'\n'.join(lines))
        effects=[e for e in data.get('effects',[]) if e.get('user_response')]
        if effects:
            e=effects[-1]
            self.audit.setText(self.audit.text()+f"\n최근 유지 후 사용자 요청: 입력 {e['user_input']} / 캐시 읽기 {e['user_read']} · 유지 {e['maintenance_calls']}회 · 인과적 절감 미확인")
        compactions=data.get('compactions',[]);delegation=data.get('delegation',{})
        auxiliary=f"\n보조 분석 · 압축 관측 {len(compactions)}회 · 하위 작업 {delegation.get('calls',0)}호출"
        if compactions:
            c=compactions[-1];auxiliary+=f"\n최근 압축 전후 입력 {c['before']} → {c['after']}토큰 · 맥락 품질 별도 확인"
        if delegation.get('calls'):
            auxiliary+=f" · API 환산 확인분 {usd(delegation.get('known_cost'))} · 산정 {delegation.get('priced',0)}호출"
        self.audit.setText(self.audit.text()+auxiliary)

    def stop(self):
        self.timer.stop()
        if self.closed:return
        if self.ticket:self.control.resolve(self.ticket,'dismiss')
        self.closed=True
        if self.dialog:self.dialog.reject()
        if self.operating_dialog:self.operating_dialog.reject()
        self.control.close()
        self.journal.close()
