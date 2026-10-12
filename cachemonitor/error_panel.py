"""Current errors and their actions, opened from the sidebar footer."""
from PySide6.QtCore import Signal
from .app_errors import TITLES, strongest
from .i18n import formatted,tr
from .presentation import Group, Column, Row, Text, Button


class ErrorPanel(Group):
    action_requested=Signal(str,str)

    def __init__(self):
        super().__init__();self.setObjectName('current-errors')
        self.faults={};self.selected=None
        body=Column(self);body.setContentsMargins(0,12,0,12);body.setSpacing(8)
        self.heading=Text('현재 오류');self.heading.put(fontSize=14,bold=True);body.addWidget(self.heading)
        self.options={}
        for kind,title in TITLES.items():
            button=Button(title);button.put(role='quiet',wrap=True);button.setFixedHeight(32)
            button.clicked.connect(lambda selected=kind:self.select(selected))
            body.addWidget(button);button.hide();self.options[kind]=button
        self.empty=Text('현재 발생한 오류 없음');self.empty.put(color='muted',fontSize=13);body.addWidget(self.empty)
        self.detail=Group();self.detail.put(background='secondary',border='border',radius=8)
        content=Column(self.detail);content.setContentsMargins(16,12,16,12);content.setSpacing(8)
        header=Row();header.put(flow=True);header.setSpacing(8)
        self.title=Text();self.title.put(fontSize=15,bold=True,wrap=True);header.addWidget(self.title,1)
        self.action=Button();self.action.put(role='quiet');self.action.clicked.connect(self.invoke);header.addWidget(self.action)
        content.addLayout(header)
        self.explanation=Text();self.explanation.put(wrap=True,fontSize=13);content.addWidget(self.explanation)
        self.guidance=Text();self.guidance.put(wrap=True,fontSize=13,color='muted');content.addWidget(self.guidance)
        self.detail.hide();body.addWidget(self.detail)

    def apply(self,faults):
        self.faults=dict(faults);self.historical=None
        if self.selected not in faults:
            selected=strongest(faults.values()) or next(iter(faults.values()),None)
            self.selected=selected.kind if selected else None
        self.heading.setText(formatted('현재 오류 {count}개',count=len(faults)) if faults else '현재 오류')
        self.empty.setVisible(not faults)
        for kind,button in self.options.items():
            button.setVisible(kind in faults);button.put(bold=kind==self.selected)
        self.detail.setVisible(bool(faults))
        if not faults:return
        fault=faults[self.selected]
        self.title.setText(fault.title);self.title.put(color=fault.severity)
        self.explanation.setText(fault.explanation);self.guidance.setText(fault.guidance);self.action.setText(fault.action)

    def select(self,kind):
        if kind not in self.faults:return
        self.selected=kind;self.apply(self.faults)
        parent=self.parent()
        while parent is not None:
            if hasattr(parent,'ensureWidgetVisible'):
                parent.ensureWidgetVisible(self.detail);break
            parent=parent.parent()

    def invoke(self):
        fault=self.faults.get(self.selected)
        if fault:self.action_requested.emit(fault.kind,fault.page)
        elif getattr(self,'historical',None):
            self.action_requested.emit(self.historical['error_kind'],self.historical['page'])

    def show_record(self,event):
        kind=event['error_kind']
        if kind in self.faults:
            self.select(kind);return
        self.selected=None;self.historical=event;self.detail.show()
        self.title.setText(formatted('{title} / {status}',title=tr(event['title']),
            status=tr('해결됨' if event.get('resolution') else '지난 기록')));self.title.put(color='muted')
        explanation,_,guidance=event['detail'].partition('\n')
        self.explanation.setText(explanation);self.guidance.setText(guidance)
        self.action.setText(event['action'])
