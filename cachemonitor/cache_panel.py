"""Shared cache analysis/settings and confirmation surface."""
import json
import time
from PySide6.QtCore import QTimer,Qt
from .cache_control import Control,control_path
from .cache_hooks import configure
from .controls import Switch
from .presentation import Group,Column,Row,Text,Button
from .pricing import usd,token_cost
from .quick_runtime import Confirmation

REASONS={
    'output_bound_not_verified':'출력·추론 상한 지원 확인 대기',
    'output_budget_unobserved':'자연 작업의 출력 사용량 수집 대기',
    'input_or_price_unobserved':'비교 가능한 입력·요금 정보 수집 대기',
    'natural_cost_bounds_unobserved':'자연 작업의 비교 자료 수집 대기',
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
            if not self.control.get('automatic',False):self.status.setText('자동 유지 꺼짐 · 사용량 분석은 계속됩니다')
            elif not self.proxy_ready:self.status.setText('자동 유지 대기 · 캐시 관리를 지원하는 프록시 연결 필요')
            elif states:
                self.status.setText(' / '.join(dict.fromkeys(REASONS.get(s.get('reason',s.get('state')),s.get('state','대기')) for s in states)))
        except Exception:
            # Stop heartbeats: outstanding hooks will follow the infrastructure policy.
            self.timer.stop();self.hook_status.setText('확인 저장소 장애 · 대기 요청은 중단될 수 있으며 새 요청은 확인을 건너뜁니다')

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
        self.control.close()
