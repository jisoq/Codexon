"""Compact, keyboard-accessible disclosure for calculation and collection details."""
from html import escape
from PySide6.QtCore import Qt
from .presentation import Button, Column, Group, Scroll, Text


def recorded(value):
    """Missing evidence is not a negative observation or an application error."""
    return value is not None and value != '' and value not in ('미확인','확인 불가','unknown')


def observed_transport(row):
    """A nearby transport log does not identify this response's transport."""
    return row.get('transport') if row.get('transport_source')=='response_id' and row.get('transport') in ('WebSocket','HTTP/SSE') else None


def model_comparison(row):
    """Compare only a completed, response-linked request/response observation."""
    requested,response=row.get('requested_model'),row.get('response_model')
    if (not recorded(requested) or not recorded(response) or row.get('response_status')!='completed'
            or row.get('observation_missing') or row.get('model_conflict')
            or '충돌' in str(row.get('model_evidence') or '')):
        return ''
    if requested==response and row.get('model_match')=='일치':
        return f'모델 일치 ({requested})'
    if requested!=response and row.get('model_match')=='불일치' and row.get('model_alert_confirmed'):
        return f'모델 불일치 (요청:{requested}, 응답:{response})'
    return ''


def record_issues(row):
    """Only actionable data problems, not absence of optional wire telemetry."""
    issues=[]
    for key,title in (('model_conflict','요청 모델 기록 충돌'),('mode_conflict','요청 모드 기록 충돌'),
                      ('cache_policy_conflict','캐시 정책 기록 충돌'),('input_conflict','입력 토큰 구성 충돌'),
                      ('output_conflict','출력 토큰 구성 충돌')):
        if row.get(key):issues.append(title)
    if row.get('model_alert_confirmed'):issues.append('요청 모델과 응답 모델이 다릅니다.')
    if row.get('observation_missing'):issues.append('요청·응답 관측 기록 저장 실패 · 모델 응답 대조에 사용할 수 없습니다.')
    if row.get('transport_source')=='conflict':issues.append('통신 방식 기록이 서로 다릅니다.')
    missing=[name for key,name in (('input','입력'),('output','출력')) if row.get(key) is None]
    if missing:issues.append('로컬 사용 기록에 '+'·'.join(missing)+' 토큰 수가 없어 합계·환산에서 제외됩니다.')
    return list(dict.fromkeys(issues))


def price_reason(row):
    """Explain absent source fields and unsupported prices separately from errors."""
    replacements={
        '요청 모드 미확인':'요청 모드가 기록되지 않음',
        '입력 토큰 미확인':'입력 토큰 수가 기록되지 않음',
        '출력 토큰 미확인':'출력 토큰 수가 기록되지 않음',
        '캐시 읽기 미확인':'캐시 읽기 토큰 수가 기록되지 않음',
        '캐시 쓰기 미확인':'캐시 쓰기 토큰 수가 기록되지 않음',
        '모델 미확인':'모델이 기록되지 않음',
        '분석 모델 미확인':'모델이 기록되지 않음',
        '모델 기준 단가 미확인':'이 모델의 환산 단가가 등록되지 않음',
        '기준 단가 미확인':'이 모델의 환산 단가가 등록되지 않음',
        '모델 단가 미확인':'이 모델의 환산 단가가 등록되지 않음',
        'Fast 장문 단가 미확인':'Fast 장문 요청의 공식 단가가 제공되지 않음',
    }
    if row.get('mode_conflict'):
        replacements['요청 모드 미확인']='요청 모드 기록이 서로 다름'
    if row.get('model_conflict'):
        for key in ('모델 미확인','분석 모델 미확인'):
            replacements[key]='요청 모델 기록이 서로 다름'
    reasons=row.get('price_issues') or [row.get('price_issue')]
    return ' · '.join(dict.fromkeys(replacements.get(x,x) for x in reasons if x)) or '환산에 필요한 사용 기록이 없습니다.'


def strong(value):
    return '<b>'+escape(str(value))+'</b>'


class Details(Group):
    def __init__(self, title, content=None, compact=False):
        super().__init__()
        self.compact=compact
        layout=Column(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(8)
        self.toggle=Button()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.put(disclosure=True)
        self.toggle.setStyleSheet('Button { border: none; padding: 4px 0; color: #526278; } '
                                 'Button:hover { color: #263344; } Button:focus { outline: 1px solid #3466a3; }')
        self.body=content if content is not None else Text()
        if isinstance(self.body,Text):
            if self.body.kind != 'textarea':self.body.setTextFormat(Qt.RichText)
            self.body.setWordWrap(True)
            self.body.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard)
            if compact:
                self.content=self.body
            else:
                self.content=Scroll()
                self.content.setWidgetResizable(True)
                self.content.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
                self.content.setMinimumHeight(180)
                self.content.setMaximumHeight(240)
                self.content.setWidget(self.body)
        else:
            self.content=self.body
        self.body.setFocusPolicy(Qt.StrongFocus)
        self.content.hide()
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)
        self.toggle.toggled.connect(self.set_expanded)

    def set_expanded(self, expanded):
        self.content.setVisible(expanded)

    def set_sections(self, sections):
        self.body.setText(''.join('<p style="margin: 0 0 8px">'+strong(title)+(' &nbsp; ' if self.compact else '<br>')+
                                 escape(str(text)).replace('\n','<br>')+'</p>' for title,text in sections if text))
