"""Home summaries reuse the owned-call projection and accepted quota evidence."""
from PySide6.QtCore import Signal

from .core import day_start
from .app_errors import detect, collection_phase
from .i18n import formatted
from .presentation import Button, Column, Group, Row, Text
from .pricing import usd
from .quota import quota_payload_display
from .quota_sidebar import QuotaBar, _duration, reset_time
from PySide6.QtCore import QTimeZone


def text(value='',size=14,color='ink',bold=False):
    node=Text(value);node.put(fontSize=size,color=color,bold=bold,wrap=True)
    return node


class HomeLink(Button):
    # Ordinary buttons elide a single caption. This row retains a separate,
    # wrapping description while making its entire surface keyboard clickable.
    kind='homeLink'

    def __init__(self,title,description):
        super().__init__(title);self.put(description=description,minHeight=84)
        self.setAccessibleName(title);self.setToolTip(description)


class HomePanel(Group):
    navigate=Signal(int)
    connection_requested=Signal()

    def __init__(self):
        super().__init__();self.summary=None;self.analysis={};self.readiness='collecting'
        body=Column(self);body.setSpacing(22)
        self.readiness_title=text('기록 준비 중',16,bold=True)
        self.readiness_note=text('Codex 사용 기록을 읽고 있습니다.',13,'muted')
        readiness=Group();ready=Column(readiness);ready.setSpacing(5)
        ready.addWidget(self.readiness_title);ready.addWidget(self.readiness_note);body.addWidget(readiness)
        summary_panel=Group();summary_panel.setObjectName('panel');summary_layout=Column(summary_panel);summary_layout.setSpacing(0)
        summaries=Row();summaries.put(collapseBelow=760,columns=2,spacing=0,dividers=True);summary_layout.addLayout(summaries)
        quota=Group();ql=Column(quota);ql.setContentsMargins(20,18,20,18);ql.setSpacing(16)
        qh=Row();qh.addWidget(text('남은 사용량',16,bold=True),1);self.plan=text('',12,'muted');qh.addWidget(self.plan);ql.addLayout(qh)
        windows=Row();windows.setSpacing(22);self.quota_cards={}
        for mode,title in (('five_hour','5시간'),('weekly','주간')):
            card=Group();cl=Column(card);cl.setSpacing(5);cl.addWidget(text(title,12,'muted'))
            value=text('—',28,bold=True);cl.addWidget(value)
            bar=QuotaBar();cl.addWidget(bar);note=text('',12,'muted');cl.addWidget(note)
            windows.addWidget(card,1);self.quota_cards[mode]=dict(value=value,bar=bar,note=note)
        ql.addLayout(windows);summaries.addWidget(quota,1)
        self.today_summary=Group()
        tl=Column(self.today_summary);tl.setContentsMargins(20,18,20,18);tl.setSpacing(16)
        tl.addWidget(text('오늘 사용 기록',16,bold=True));metrics=Row();metrics.setSpacing(20)
        calls=Group();c=Column(calls);c.setSpacing(5);c.addWidget(text('호출 수',12,'muted'))
        self.calls=text('—',28,bold=True);c.addWidget(self.calls);self.sessions=text('',12,'muted');c.addWidget(self.sessions);metrics.addWidget(calls,1)
        cost=Group();c=Column(cost);c.setSpacing(5);c.addWidget(text('구독 가치 환산액',12,'muted'))
        self.cost=text('—',28,bold=True);c.addWidget(self.cost);self.coverage=text('',12,'muted');c.addWidget(self.coverage);metrics.addWidget(cost,1)
        tl.addLayout(metrics);tl.addWidget(text('환산액은 실제 청구 금액이 아닙니다.',12,'muted'));summaries.addWidget(self.today_summary,1)
        body.addWidget(summary_panel);body.addWidget(text('자세히 살펴보기',16,bold=True));self.links={}
        listing=Column();listing.setSpacing(0)
        for page,title,description in (
            (3,'구독 모니터링','남은 사용량과 초기화 시각을 확인하고, 구독 사용 기록을 살펴봅니다.'),
            (1,'지표 비교','프로젝트와 모델별 호출 수, 토큰, 환산액을 비교합니다.'),
            (5,'성능 추이','응답시간과 출력 속도의 변화를 살펴보고, 모델별 차이를 비교합니다.')):
            link=HomeLink(title,description);link.clicked.connect(lambda value=page:self.navigate.emit(value));listing.addWidget(link);self.links[page]=link
        body.addLayout(listing)
        connection=Row();connection.put(collapseBelow=620,columns=2,spacing=12)
        intro=Group();cl=Column(intro);cl.setSpacing(5);cl.addWidget(text('요청 모델과 응답 모델 비교',14,bold=True))
        cl.addWidget(text('선택 기능입니다. 기본 사용량 확인에는 필요하지 않습니다.',12,'muted'));connection.addWidget(intro,1)
        self.connection=Button('연결 설정 보기');self.connection.put(role='quiet');self.connection.clicked.connect(self.connection_requested.emit);connection.addWidget(self.connection)
        body.addLayout(connection);body.addStretch()

    @staticmethod
    def query(now):
        return dict(page=6,category_usage=True,presentation=True,include_whole_history=False,
            start=day_start(now),end=now+.000001,now=now,period='today',home='',project='',source='',
            archived=True,model='',effort='',service_tier='',usage=dict(dimension='source',metric='cost'))

    def apply_usage(self,analysis,summary):
        self.analysis=analysis;self.summary=summary
        self.calls.setText(formatted('{count}회',count=f"{analysis['response_count']:,}"))
        self.sessions.setText(formatted('{count}개 세션',count=f"{analysis['session_count']:,}"))
        self.cost.setText(usd(summary['value']))
        self.coverage.setText(formatted('환산 가능 {known} / {total}호출',known=f"{summary['known']:,}",total=f"{summary['total']:,}")
                              if summary['partial'] else '이 기기에서 수집한 기록')

    def apply_status(self,evidence,quota_payload,now):
        faults=dict(evidence.get('collection_fault') or {})
        if faults.get('snapshot_at') is None:
            faults['snapshot_at']=evidence.get('collection_snapshot_at',evidence.get('ts'))
        fault=detect('collection',faults,now)
        phase=collection_phase(evidence,now,started_at=evidence.get('collection_started_at'))
        count=self.analysis.get('response_count',0)
        historical=any(item.get('count',0)>0 for item in evidence.get('filter_choices',{}).get('combinations',[])) or any(
            session.get('coverage_available') for session in evidence.get('sessions',[]))
        if faults.get('expected_stop') or faults.get('disabled'):
            self.readiness='stopped';title='사용 기록 수집 중지';note='자동 수집이 중지되었습니다. 확인된 기록은 계속 표시합니다.'
        elif phase in ('failed','delayed'):
            self.readiness='failed';title='사용 기록 수집 확인 필요'
            note=fault.explanation+'\n'+fault.guidance if fault else '수집 상태를 확인하세요. 확인된 기록은 계속 표시합니다.'
        elif phase=='preparing':
            self.readiness='collecting';title='기록 준비 중';note='Codex 사용 기록을 읽고 있습니다.'
        elif count:
            self.readiness='ready';title='사용 기록 수집 정상';note='이 기기에서 수집한 오늘의 사용 기록입니다.'
        elif historical:
            self.readiness='no_today';title='오늘 사용 기록이 없습니다';note='이전 기록은 지표 비교와 성능 추이에서 확인할 수 있습니다.'
        else:
            self.readiness='empty';title='첫 사용 기록을 기다리고 있습니다';note='Codex에서 작업을 시작하면 사용 기록이 자동으로 표시됩니다.'
        self.readiness_title.setText(title);self.readiness_note.setText(note)
        quota=(quota_payload or {}).get('quota') or {};self.plan.setText((quota.get('plan_type') or '').title())
        zone=QTimeZone.systemTimeZone()
        for mode,card in self.quota_cards.items():
            display=quota_payload_display(quota_payload,mode,now);remaining=display['remaining']
            card['value'].setText(f'{remaining:.1f}%' if remaining is not None else '∞' if display['state']=='제한 없음' else '—')
            card['value'].setToolTip(display['tooltip']);card['bar'].set_value(remaining);card['bar'].setVisible(remaining is not None)
            reset=(quota.get('windows',{}).get(mode) or {}).get('resets_at')
            stamp=reset_time(reset,zone)
            note=formatted('{duration} 후 초기화\n{date}',duration=_duration(reset-now),date=stamp.toString('MM/dd HH:mm')) if stamp and remaining is not None and reset>now else display['state']
            card['note'].setText(note);card['note'].setToolTip(display['tooltip'])
