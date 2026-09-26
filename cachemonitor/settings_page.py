"""Application preferences, with existing actions as the shared control surface."""
import sys
from PySide6.QtCore import Qt, QSignalBlocker, QTimer, Signal
from .presentation import Button, Choice, Column, Group, Navigation, Row, Scroll, Slider, Stack, Text, Toggle
from PySide6.QtWidgets import QApplication
from .controls import Switch
from .i18n import tr


class SettingsPage(Group):
    restartRequested = Signal()
    TITLES = ('일반', '작업표시줄 위젯', '세션 오버레이', '프록시', '알림', '정보·문제 해결', '캐시 관리')
    ORDER = (0, 1, 2, 6, 4, 3, 5)

    def __init__(self, parent=None):
        super().__init__(parent)
        if sys.platform == 'darwin':
            self.TITLES = ('일반', '메뉴 막대', *self.TITLES[2:])
        self.owner = parent
        self.controls = {}
        shell = Row(self); shell.setContentsMargins(0, 0, 0, 0); shell.setSpacing(24)
        self.navigation = Navigation(); self.navigation.setObjectName('settingsNavigation')
        self.navigation.addItems([self.TITLES[i] for i in self.ORDER]); self.navigation.setFixedWidth(160)
        self.navigation.setStyleSheet('Navigation { border: none; background: transparent; } '
            'Navigation::item { padding: 11px 12px; border-radius: 5px; margin-bottom: 3px; } '
            'Navigation::item:selected { background: selection; color: ink; }')
        self.stack = Stack()
        shell.addWidget(self.navigation); shell.addWidget(self.stack, 1)
        self.layouts = []; self.scrollers = []
        for title in self.TITLES:
            area = Scroll(); area.setWidgetResizable(True)
            body = Group();body.setObjectName('settingsBody')
            body.put(background='surface')
            layout = Column(body); layout.setContentsMargins(4, 3, 12, 18)
            layout.setSpacing(0)
            heading = Text(title); heading.setStyleSheet('font-size: 19px; font-weight: 600;')
            heading.setContentsMargins(0,0,0,16)
            layout.addWidget(heading); layout.addStretch()
            area.setWidget(body); self.stack.addWidget(area)
            self.layouts.append(layout); self.scrollers.append(area)
        self.navigation.currentRowChanged.connect(lambda row:self.stack.setCurrentIndex(self.ORDER[row]) if row>=0 else None)
        category=max(0,min(len(self.TITLES)-1,parent.settings.value('settings/category',0,type=int)))
        self.navigation.setCurrentRow(self.ORDER.index(category))
        self.navigation.currentRowChanged.connect(lambda row:parent.settings.setValue('settings/category',self.ORDER[row]) if row>=0 else None)
        self.add_row(0, '로그인 시 시작' if sys.platform == 'darwin' else 'Windows 로그인 시 시작',
                     '메뉴 막대에서 시작' if sys.platform == 'darwin' else '트레이에서 시작', self.toggle('startup'))
        self.startup_status=Text('');self.startup_status.setWordWrap(True);self.startup_status.hide()
        self.add_widget(0,self.startup_status)
        self.add_row(0, '잔여량 표시', '', self.choice('quota'))
        tracking=self.toggle('weekly_tracking')
        self.add_row(0, '주간 환산 모니터링', '', tracking)
        tracking.setChecked(parent.settings.value('quota/trackingEnabled',True,type=bool))
        tracking.toggled.connect(parent.set_quota_tracking_enabled)
        self.add_row(0, '모양', '', self.choice('theme'))
        language_choice=self.choice('language')
        language_controls=Group();language_layout=Row(language_controls);language_layout.setContentsMargins(0,0,0,0);language_layout.setSpacing(8)
        restart=Button('Codexon 재시작');restart.put(iconName='restart');restart.setFixedSize(32,32)
        restart.setToolTip('Codexon 재시작');restart.setAccessibleName('Codexon 재시작')
        self.controls['restart']=restart
        language_layout.addWidget(language_choice);language_layout.addWidget(restart)
        self.add_row(0, '언어', '다음 실행부터 적용', language_controls)
        language_choice.setAccessibleName('언어')
        for title,value in (('한국어','ko'),('English','en')):language_choice.addItem(title,value)
        language_choice.setCurrentIndex(max(0,language_choice.findData(parent.settings.value('ui/language','ko'))))
        def save_language(index):
            parent.settings.setValue('ui/language',language_choice.itemData(index))
            self.refresh_restart()
        language_choice.currentIndexChanged.connect(save_language)
        restart.clicked.connect(self.request_restart)
        self.refresh_restart()
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
        self.add_row(1, '메뉴 막대 잔여량 표시' if sys.platform == 'darwin' else '작업표시줄 위젯', '', self.toggle('widget'))
        monitor_row = self.add_row(1, '표시할 모니터', '연결 해제 시 주 모니터 사용', self.choice('monitor'))
        if sys.platform == 'darwin':
            monitor_row.hide()
            text = Text('메뉴 막대 위치는 macOS가 관리합니다. 잔여량을 누르면 메뉴를 엽니다.')
            text.setWordWrap(True)
            self.add_widget(1, text)
        self.add_row(2, '세션 오버레이', '현재 작업의 비용·토큰', self.toggle('overlay'))
        self.controls['reset_position']=Button('위치 초기화')
        self.add_row(2, '위치', '제목을 끌어서 이동', self.controls['reset_position'])
        for key in ('startup', 'quota', 'widget', 'monitor', 'overlay', 'reset_position', 'theme'):
            self.controls[key].setEnabled(False)
        if sys.platform == 'darwin':
            self._setup_macos_permissions()

    def _setup_macos_permissions(self):
        from .macos_status import NotificationPermission
        self.notification_permission = NotificationPermission(self)
        status = Text('알림 권한을 확인합니다.');status.setWordWrap(True)
        button = Button('알림 허용')
        self.controls['notification_permission'] = button
        self.add_widget(4, status)
        self.add_row(4, 'macOS 알림 권한', '허용하지 않아도 앱 안의 알림 기록은 유지합니다.', button)
        def changed(value):
            messages = {'authorized': 'macOS 알림이 허용되어 있습니다.', 'provisional': 'macOS 알림이 조용히 전달됩니다.',
                        'not_determined': '알림을 받으려면 알림 허용을 누르세요.',
                        'denied': 'macOS에서 알림을 차단했습니다. 알림 설정에서 허용하세요.',
                        'unavailable': '알림은 설치한 Codexon 앱에서 사용할 수 있습니다. 앱 안의 알림 기록은 유지합니다.'}
            status.setText(messages[value])
            button.setText('알림 설정 열기' if value in ('denied', 'authorized', 'provisional') else '알림 허용')
            button.setEnabled(value != 'unavailable')
        self.notification_permission.changed.connect(changed)
        def request():
            if self.notification_permission.status in ('denied', 'authorized', 'provisional'):
                self.open_system_settings('com.apple.Notifications-Settings.extension')
            else:
                self.notification_permission.request()
        button.clicked.connect(request)
        QApplication.instance().applicationStateChanged.connect(lambda _: self.notification_permission.refresh())
        self.notification_permission.refresh()

    @staticmethod
    def open_system_settings(pane):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl('x-apple.systempreferences:'+pane))

    def refresh_restart(self):
        from .i18n import language
        self.controls['restart'].setEnabled(self.owner.settings.value('ui/language','ko')!=language())

    def request_restart(self):
        self.owner.settings.sync()
        self.controls['restart'].setEnabled(False)
        self.restartRequested.emit()
        QTimer.singleShot(30000,self.refresh_restart)

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
        row.setStyleSheet('Group#preferenceRow { border-bottom: 1px solid border; }')
        layout = Row(row); layout.setContentsMargins(0, 20, 0, 20); layout.setSpacing(24)
        copy = Column(); copy.setSpacing(5)
        name = Text(title); name.setWordWrap(True); name.setStyleSheet('font-weight: 500;')
        copy.addWidget(name)
        if description:
            detail = Text(description); detail.setWordWrap(True)
            detail.setStyleSheet('color: muted; font-size: 13px;')
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
        def startup_status():
            note=owner.startup.statusTip()
            self.startup_status.setText(note)
            self.startup_status.setVisible(bool(note))
            self.controls['startup'].setToolTip(note)
        owner.startup.changed.connect(startup_status)
        startup_status()
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
        if sys.platform != 'darwin':
            return
        self.overlay_controller = controller
        self.overlay_status = Text('');self.overlay_status.setWordWrap(True)
        self.add_widget(2, self.overlay_status)
        sessions = self.choice('overlay_session')
        self.add_row(2, '표시할 세션', '자동 추적이 어려우면 세션을 직접 선택하세요. 선택한 세션은 독립 패널에 고정합니다.', sessions)
        screens = self.choice('overlay_monitor')
        self.add_row(2, '독립 패널 모니터', '연결 해제 시 주 모니터를 사용합니다.', screens)
        access = Button('손쉬운 사용 설정 열기')
        access.clicked.connect(lambda: self.open_system_settings('com.apple.preference.security?Privacy_Accessibility'))
        self.add_row(2, '창 추적과 휠 전달', '손쉬운 사용을 허용하면 창 변경 감지와 Codex로 휠 전달을 지원합니다. 권한 없이도 독립 패널을 사용할 수 있습니다.', access)
        self.overlay_permission = Text('');self.overlay_permission.setWordWrap(True)
        self.add_widget(2, self.overlay_permission)
        def select(index):
            value = sessions.itemData(index)
            if value is None:
                controller.follow_codex()
            else:
                controller.set_manual_session(*value)
        sessions.currentIndexChanged.connect(select)
        screens.currentIndexChanged.connect(lambda index: controller.set_monitor(screens.itemData(index) or ''))
        self.overlay_settings_timer = QTimer(self)
        self.overlay_settings_timer.setInterval(2000)
        self.overlay_settings_timer.timeout.connect(self.refresh_overlay_settings)
        self.overlay_settings_timer.start()
        controller.changed.connect(self.refresh_overlay_settings)
        self.refresh_overlay_settings()

    def refresh_overlay_settings(self):
        controller = self.overlay_controller
        self.overlay_status.setText(controller.status_text())
        sessions = self.controls['overlay_session']
        signature = tuple((s['home'], s['id'], s.get('title', '')) for s in controller.sessions[:100])
        if signature != getattr(self, '_overlay_session_signature', None):
            self._overlay_session_signature = signature
            with QSignalBlocker(sessions):
                sessions.clear();sessions.addItem('Codex의 현재 세션 자동 추적', None)
                for home, sid, title in signature:
                    sessions.addItem(title or sid, (home, sid))
        with QSignalBlocker(sessions):
            sessions.setCurrentIndex(max(0, sessions.findData(controller.manual_session)))
        from .screens import screen_id
        screens = self.controls['overlay_monitor']
        with QSignalBlocker(screens):
            screens.clear();screens.addItem('주 모니터', '')
            for screen in QApplication.screens():
                screens.addItem(screen.name(), screen_id(screen))
            screens.setCurrentIndex(max(0, screens.findData(controller.settings.value('overlay/monitor', ''))))
        allowed = bool(controller.native and hasattr(controller.native, 'accessibility_enabled')
                       and controller.native.accessibility_enabled())
        self.overlay_permission.setText('손쉬운 사용이 허용되어 있습니다.' if allowed
                                        else '손쉬운 사용을 아직 허용하지 않았습니다. 기본 창 추적과 독립 패널은 계속 사용할 수 있습니다.')

    def reveal(self, category, widget=None):
        self.navigation.setCurrentRow(self.ORDER.index(category))
        if widget is not None: self.scrollers[category].ensureWidgetVisible(widget)
