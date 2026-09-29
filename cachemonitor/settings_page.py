"""Application preferences, with existing actions as the shared control surface."""
from PySide6.QtCore import Qt, QSignalBlocker, QTimer, Signal
from .presentation import Button, Choice, Column, Group, Navigation, Row, Scroll, Input, Stack, Text
from PySide6.QtWidgets import QApplication
from .controls import Switch
from .i18n import tr


class SettingsPage(Group):
    restartRequested = Signal()
    IDS = ('general', 'display', 'notifications', 'integration', 'troubleshooting', 'about')
    TITLES = ('일반', '화면 표시', '알림', 'Codex 연동', '문제 해결', '앱 정보')
    ENTRY_IDS = {
        '언어':'language','시스템 시간대':'timezone','위치':'overlay-position',
        '프록시 사용':'proxy','캐시 갱신':'cache-refresh','캐시 관리':'cache-management',
        '알림 표시':'notifications-enabled','HTTP 전환':'http-fallback',
        '캐시 저하 의심':'cache-drop','모델명 불일치':'model-mismatch','프록시 장애':'proxy-failure',
        '캐시 알림 범위':'cache-scope','최근 알림':'notification-history','현재 버전':'version',
        '업데이트 확인':'update','연결 복구':'recovery','저장소':'repository',
        '수집 상태':'collection-status','진단 상세':'diagnostics',
    }
    LEGACY = ('general', 'display', 'display', 'integration', 'notifications', 'about', 'integration')

    @classmethod
    def category_id(cls, value):
        if value in cls.IDS:return value
        try:return cls.LEGACY[int(value)] if 0 <= int(value) < len(cls.LEGACY) else 'general'
        except (ValueError, TypeError):return 'general'

    def __init__(self, parent=None):
        super().__init__(parent)
        self.owner=parent;self.controls={};self.entries=[];self.sections={}
        root=Column(self);root.setContentsMargins(0,0,0,0);root.setSpacing(18)
        search_shell=Row();search_row=Row();search_row.setMaximumWidth(1024);self.search=Input();self.search.setPlaceholderText('설정 검색')
        self.search.setAccessibleName('설정 검색');self.search.put(clearOnEscape=True)
        clear=Button('검색 지우기');self.clear_search=clear;clear.clicked.connect(lambda:self.search.setText(''))
        search_row.addWidget(self.search,1);search_row.addWidget(clear)
        search_shell.addLayout(search_row,1);search_shell.addStretch();root.addLayout(search_shell)
        shell=Row();shell.setSpacing(24);root.addLayout(shell,1)
        self.navigation=Navigation();self.navigation.setObjectName('settingsNavigation')
        self.navigation.addItems(self.TITLES);self.navigation.setFixedWidth(160)
        self.stack=Stack();self.stack.put(deferPages=True);self.stack.setMaximumWidth(840);shell.addWidget(self.navigation);shell.addWidget(self.stack,1);shell.addStretch()
        self.layouts={};self.scrollers={}
        for category,title in zip(self.IDS,self.TITLES):
            area=Scroll();area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body=Group();layout=Column(body);layout.setContentsMargins(4,3,12,18);layout.setSpacing(0)
            heading=Text(title);heading.put(fontSize=19,bold=True);heading.setContentsMargins(0,0,0,16)
            layout.addWidget(heading);layout.addStretch();area.setWidget(body);self.stack.addWidget(area)
            self.layouts[category]=layout;self.scrollers[category]=area
        self.results=Scroll();self.result_body=Column();self.results.setWidget(self.result_body);self.stack.addWidget(self.results)
        self.navigation.currentRowChanged.connect(self.select_category)
        self.navigation.activated.connect(lambda row:self.select_category(row) if self.search.text() else None)
        self.search.textChanged.connect(self.filter_settings)
        self.reveal(self.category_id(parent.settings.value('settings/category','general')))
        self.add_row('general', 'Windows 로그인 시 시작', 'Windows에 로그인하면 Codexon이 트레이에서 실행됩니다.', self.toggle('startup'), section='시작')
        tracking=self.toggle('weekly_tracking')
        self.add_row('general', '주간 환산 모니터링', '주간 사용 한도와 사용량을 함께 기록해 한도 소모 추이를 확인합니다.', tracking, section='사용량')
        tracking.setChecked(parent.settings.value('quota/trackingEnabled',True,type=bool))
        tracking.toggled.connect(parent.set_quota_tracking_enabled)
        self.add_row('display', '테마', '', self.choice('theme'), section='모양')
        self.add_row('display', '잔여량 표시', '', self.choice('quota'), section='잔여량')
        language_choice=self.choice('language')
        language_controls=Group();language_layout=Row(language_controls);language_layout.setContentsMargins(0,0,0,0);language_layout.setSpacing(8);language_layout.put(flow=True)
        restart=Button('Codexon 재시작')
        restart.setToolTip('Codexon 재시작');restart.setAccessibleName('Codexon 재시작')
        self.controls['restart']=restart
        language_layout.addWidget(language_choice);language_layout.addWidget(restart)
        self.add_row('general', '언어', '언어 변경은 Codexon을 다시 시작하면 적용됩니다.', language_controls, section='언어')
        self.entries[-1]['target']=language_choice
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
        timezone=Text(tr('시스템 시간대')+': '+zone);timezone.setWordWrap(True)
        self.add_widget('general', timezone,section='언어',title='시스템 시간대')
        self.add_row('display', '작업표시줄 위젯', '마우스로 끌어 작업표시줄 안에서 위치를 조절할 수 있습니다.', self.toggle('widget'), section='작업표시줄 위젯')
        self.add_row('display', '표시할 모니터', '선택한 모니터의 연결이 끊기면 주 모니터에 표시합니다.', self.choice('monitor'), section='작업표시줄 위젯')
        self.add_row('display', '세션 오버레이', 'Codex 작업 중 현재 세션의 비용과 토큰 사용량을 작은 창에서 확인할 수 있습니다.', self.toggle('overlay'), section='세션 오버레이')
        self.controls['reset_position']=Button('위치 초기화')
        self.add_row('display', '위치', '오버레이 위치를 기본 위치로 되돌립니다.', self.controls['reset_position'], section='세션 오버레이')
        for key in ('startup', 'quota', 'widget', 'monitor', 'overlay', 'reset_position', 'theme'):
            self.controls[key].setEnabled(False)

    def refresh_restart(self):
        from .i18n import language
        pending=self.owner.settings.value('ui/language','ko')!=language()
        self.controls['restart'].setEnabled(pending);self.controls['restart'].setVisible(pending)

    def request_restart(self):
        self.owner.settings.sync()
        self.controls['restart'].setEnabled(False)
        self.restartRequested.emit()
        QTimer.singleShot(30000,self.refresh_restart)

    def toggle(self, key):
        control = Switch(); control.setAccessibleName(key)
        control.setObjectName('setting-'+key)
        self.controls[key] = control
        return control

    def choice(self, key):
        control = Choice(); control.setMinimumWidth(240); control.setMaximumWidth(350)
        control.setObjectName('setting-'+key)
        self.controls[key] = control
        return control

    def add_widget(self, category, widget, section=None, title=None, description='', target=None, aliases=''):
        category=self.category_id(category)
        section=section or {'general':'앱 설정','display':'화면 설정','notifications':'알림 설정',
            'integration':'연결과 캐시','troubleshooting':'상태 확인','about':'제품 정보'}[category]
        key=(category,section)
        if key not in self.sections:
            group=Column();group.setSpacing(0)
            if any(existing_category==category for existing_category,_ in self.sections):
                divider=Group();divider.setFixedHeight(1);divider.put(background='border')
                group.addWidget(divider)
            heading=Text(section);heading.put(fontSize=15,bold=True,color='muted');heading.setContentsMargins(0,20,0,8)
            group.addWidget(heading);self.sections[key]=group
            layout=self.layouts[category];layout.insertWidget(layout.count()-1,group)
        self.sections[key].addWidget(widget)
        if title:
            entry_id=self.ENTRY_IDS.get(title) or (target or widget).objectName()
            if not entry_id:raise ValueError('Missing settings entry ID: '+title)
            self.entries.append(dict(id=entry_id,category=category,section=section,title=title,description=description,
                widget=widget,target=target or widget,aliases=aliases))

    def add_row(self, category, title, description, control, section=None, aliases=''):
        row=Group();row.setObjectName('settingRow')
        outer=Column(row);outer.setContentsMargins(0,16,0,16)
        layout=Row();layout.put(collapseBelow=360 if control.kind=='switch' else 620);layout.setSpacing(12);outer.addLayout(layout)
        copy=Column();copy.setSpacing(5)
        name=Text(title);name.setWordWrap(True);name.put(bold=True);copy.addWidget(name)
        if description:
            detail=Text(description);detail.setWordWrap(True);detail.put(color='muted',fontSize=13);copy.addWidget(detail)
        layout.addLayout(copy,2)
        holder=Row();holder.addStretch();holder.addWidget(control);layout.addLayout(holder,1)
        control.setAccessibleName(title)
        self.add_widget(category,row,section,title,description,control,aliases)
        return row

    def current_category(self):return self.IDS[max(0,self.navigation.currentRow())]

    def select_category(self,row):
        if row < 0:return
        self.search.setText('');self.stack.setCurrentIndex(row)
        self.owner.settings.setValue('settings/category',self.IDS[row])

    def capture_scrolls(self):return {key:area.verticalPosition.value() for key,area in self.scrollers.items()}

    def restore_scrolls(self,values):
        if isinstance(values,dict):
            for key,value in values.items():
                if key in self.scrollers:self.scrollers[key].verticalPosition.setValue(value)

    def filter_settings(self,query):
        for node in self.result_body._nodes:node.deleteLater()
        self.result_body._nodes=[];self.result_body.structureChanged.emit()
        words=query.casefold().split()
        if not words:self.stack.setCurrentIndex(self.navigation.currentRow());return
        self.stack.setCurrentIndex(len(self.IDS))
        from .translation_catalog import CATALOG
        for entry in sorted(self.entries,key=lambda e:(self.IDS.index(e['category']), list(self.sections).index((e['category'],e['section'])))):
            title=self.TITLES[self.IDS.index(entry['category'])]
            fields=[title,entry['section'],entry['title'],entry['description'],entry['aliases']]
            haystack=' '.join(fields+[CATALOG.get(v,v) for v in fields]).casefold()
            if not all(word in haystack for word in words):continue
            group=Column();group.setSpacing(6);group.setContentsMargins(0,10,0,14)
            path=Text(tr(title)+' / '+tr(entry['section']));path.put(color='muted',fontSize=12);group.addWidget(path)
            button=Button(entry['title']);button.put(activateOnReturn=True);button.clicked.connect(lambda key=entry['id']:self.reveal_item(key));group.addWidget(button)
            if entry['description']:
                description=Text(entry['description']);description.setWordWrap(True);description.put(color='muted');group.addWidget(description)
            self.result_body.addWidget(group)
        if not self.result_body.count():self.result_body.addWidget(Text('검색 결과가 없습니다. 다른 단어로 검색해 보세요.'))
        self.results.verticalPosition.setValue(0)

    def reveal_item(self,entry_id):
        entry=next(entry for entry in self.entries if entry['id']==entry_id)
        from .ui_details import Details
        target=entry['target']
        if isinstance(entry['widget'],Details):
            entry['widget'].toggle.setChecked(True);target=entry['widget'].toggle
        self.reveal(entry['category'],entry['widget'])
        QTimer.singleShot(0,lambda:target.focusRequested.emit())

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
        category=self.category_id(category);self.search.setText('')
        self.navigation.setCurrentRow(self.IDS.index(category));self.stack.setCurrentIndex(self.IDS.index(category))
        self.owner.settings.setValue('settings/category',category)
        if widget is not None:self.scrollers[category].ensureWidgetVisible(widget)
