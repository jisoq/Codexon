"""Compact, keyboard-accessible disclosure for calculation and collection details."""
from html import escape
from PySide6.QtCore import Qt
from .presentation import Button, Column, Group, Scroll, Text
from .i18n import formatted


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
        self.toggle.setStyleSheet('Button { border: none; padding: 4px 0; color: muted; } '
                                 'Button:hover { color: ink; } Button:focus { outline: 1px solid accent; }')
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


def history_duration(value):
    if value is None:return '—'
    minutes,centiseconds=divmod(round(value*100),6000)
    seconds=f'{centiseconds/100:.2f}'
    if minutes>=60:
        hours,minutes=divmod(minutes,60)
        return formatted('{hours}시간 {minutes}분 {seconds}초',hours=hours,minutes=minutes,seconds=seconds)
    if minutes:return formatted('{minutes}분 {seconds}초',minutes=minutes,seconds=seconds)
    return formatted('{seconds}초',seconds=seconds)
