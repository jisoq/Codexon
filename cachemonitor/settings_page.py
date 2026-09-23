"""Application preferences, with existing actions as the shared control surface."""
from PySide6.QtCore import Qt, QSignalBlocker
from .presentation import Button, Choice, Column, Group, Navigation, Row, Scroll, Slider, Stack, Text, Toggle
from PySide6.QtWidgets import QApplication
from .controls import Switch
from .i18n import tr


class SettingsPage(Group):
    TITLES = ('일반', '작업표시줄 위젯', '세션 오버레이', '프록시', '알림', '정보·문제 해결')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.owner = parent
        self.controls = {}
        shell = Row(self); shell.setContentsMargins(0, 0, 0, 0); shell.setSpacing(24)
        self.navigation = Navigation(); self.navigation.setObjectName('settingsNavigation')
        self.navigation.addItems(self.TITLES); self.navigation.setFixedWidth(160)
        self.navigation.setStyleSheet('Navigation { border: none; background: transparent; } '
            'Navigation::item { padding: 11px 12px; border-radius: 5px; margin-bottom: 3px; } '
            'Navigation::item:selected { background: #e8edf2; color: #233444; }')
        self.stack = Stack()
        shell.addWidget(self.navigation); shell.addWidget(self.stack, 1)
        self.layouts = []; self.scrollers = []
        for title in self.TITLES:
            area = Scroll(); area.setWidgetResizable(True)
            body = Group();body.setObjectName('settingsBody')
            body.setStyleSheet('Group#settingsBody { background: white; }')
            layout = Column(body); layout.setContentsMargins(4, 3, 12, 18)
            layout.setSpacing(0)
            heading = Text(title); heading.setStyleSheet('font-size: 19px; font-weight: 600;')
            heading.setContentsMargins(0,0,0,16)
            layout.addWidget(heading); layout.addStretch()
            area.setWidget(body); self.stack.addWidget(area)
            self.layouts.append(layout); self.scrollers.append(area)
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.navigation.setCurrentRow(max(0,min(5,parent.settings.value('settings/category',0,type=int))))
        self.navigation.currentRowChanged.connect(lambda index:parent.settings.setValue('settings/category',index))
        self.add_row(0, 'Windows 로그인 시 시작', '트레이에서 시작', self.toggle('startup'))
        self.add_row(0, '잔여량 표시', '', self.choice('quota'))
        tracking=self.toggle('weekly_tracking')
        self.add_row(0, '주간 환산 모니터링', '', tracking)
        tracking.setChecked(parent.settings.value('quota/trackingEnabled',True,type=bool))
        tracking.toggled.connect(parent.set_quota_tracking_enabled)
        self.add_row(0, '모양', '', self.choice('theme'))
        language=self.choice('language')
        self.add_row(0, '언어', '다음 실행부터 적용', language)
        for title,value in (('한국어','ko'),('English','en')):language.addItem(title,value)
        language.setCurrentIndex(max(0,language.findData(parent.settings.value('ui/language','ko'))))
        language.currentIndexChanged.connect(lambda index: parent.settings.setValue('ui/language',language.itemData(index)))
        import time
        from datetime import datetime
        from .i18n import language
        if language() == 'en':
            offset=datetime.now().astimezone().strftime('%z')
            zone='UTC'+offset[:3]+':'+offset[3:]
        else:
            zone=time.tzname[0]
        timezone=Text('시스템 시간대 · '+zone);timezone.setWordWrap(True)
        self.add_widget(0, timezone)
        self.add_row(1, '작업표시줄 위젯', '', self.toggle('widget'))
        self.add_row(1, '표시할 모니터', '연결 해제 시 주 모니터 사용', self.choice('monitor'))
        self.add_row(2, '세션 오버레이', '현재 작업의 비용·토큰', self.toggle('overlay'))
        self.controls['reset_position']=Button('위치 초기화')
        self.add_row(2, '위치', '제목을 끌어서 이동', self.controls['reset_position'])
        for key in ('startup', 'quota', 'widget', 'monitor', 'overlay', 'reset_position', 'theme'):
            self.controls[key].setEnabled(False)

    def toggle(self, key):
        control = Switch(); control.setAccessibleName(key)
        self.controls[key] = control
        return control

    def choice(self, key):
        control = Choice(); control.setMinimumWidth(240); control.setMaximumWidth(350)
        self.controls[key] = control
        return control

    def add_widget(self, category, widget):
        layout = self.layouts[category]
        layout.insertWidget(layout.count()-1, widget)

    def add_row(self, category, title, description, control):
        row = Group(); row.setObjectName('preferenceRow')
        row.setStyleSheet('Group#preferenceRow { border-bottom: 1px solid #e3e7eb; }')
        layout = Row(row); layout.setContentsMargins(0, 20, 0, 20); layout.setSpacing(24)
        copy = Column(); copy.setSpacing(5)
        name = Text(title); name.setWordWrap(True); name.setStyleSheet('font-weight: 500;')
        copy.addWidget(name)
        if description:
            detail = Text(description); detail.setWordWrap(True)
            detail.setStyleSheet('color: #66768c; font-size: 13px;')
            copy.addWidget(detail)
        layout.addLayout(copy, 1); layout.addWidget(control, 0, Qt.AlignVCenter)
        control.setAccessibleName(title)
        self.add_widget(category, row)
        return row

    def bind_action(self, key, action):
        control = self.controls[key]
        def sync():
            with QSignalBlocker(control): control.setChecked(action.isChecked())
            control.setEnabled(action.isEnabled())
        action.changed.connect(sync)
        control.toggled.connect(lambda value: action.trigger() if value != action.isChecked() else None)
        sync()

    def bind_choices(self, key, actions):
        control = self.controls[key]
        with QSignalBlocker(control):
            control.clear()
            for value, action in actions.items(): control.addItem(action.text(), value)
        def sync():
            checked = next((value for value, action in actions.items() if action.isChecked()), None)
            with QSignalBlocker(control): control.setCurrentIndex(control.findData(checked))
        for action in actions.values(): action.changed.connect(sync)
        control.currentIndexChanged.connect(lambda index: actions[control.itemData(index)].trigger() if index >= 0 else None)
        control.setEnabled(True); sync()

    def bind_tray(self, owner):
        self.bind_action('startup', owner.startup)
        self.bind_action('widget', owner.taskbar_action)
        self.bind_choices('quota', owner.quota_actions)
        self.taskbar = owner.taskbar_quota
        self.refresh_monitors()
        self.controls['monitor'].currentIndexChanged.connect(
            lambda index: self.taskbar.set_monitor(self.controls['monitor'].itemData(index)) if index >= 0 else None)
        QApplication.instance().screenAdded.connect(self.refresh_monitors)
        QApplication.instance().screenRemoved.connect(self.refresh_monitors)

    def refresh_monitors(self, *_):
        self.taskbar.refresh_monitor_menu()
        control = self.controls['monitor']
        with QSignalBlocker(control):
            control.clear()
            for value, action in self.taskbar.monitor_actions.items(): control.addItem(action.text(), value)
            control.setCurrentIndex(control.findData(self.taskbar.monitor_name))
        control.setEnabled(True)

    def bind_overlay(self, controller):
        self.controls['reset_position'].clicked.connect(controller.reset_position)
        self.controls['reset_position'].setEnabled(True)

    def reveal(self, category, widget=None):
        self.navigation.setCurrentRow(category)
        if widget is not None: self.scrollers[category].ensureWidgetVisible(widget)
