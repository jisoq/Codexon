"""Full dashboard: shared evidence, explicit populations and reversible navigation."""
from __future__ import annotations
from .tooltips import TEXT as TIPS

import copy
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QSettings, QDate, QEvent, QUrl
from PySide6.QtGui import QFont, QFontMetricsF, QDesktopServices
from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon
from .presentation import batched_updates, Button, Choice, Column, DateInput, Group, Input, Navigation, Row, Scroll, Split, Stack, Text, TextArea, Toggle
from .table_model import Cell, Header, Table, LazyTable
from .quick_runtime import Dialog, DialogButtons
from .tray import TrayWindow, tray_icon
from .analytics import analyze, stats, session_summary, cache_rows, observation_flags, project_choices, project_key, effort_key
from .analysis_engine import AnalysisEngine, BoundedCache
from .analysis_worker import AnalysisBridge
from .core import day_start, summarize, transport_label
from .pricing import usd, RATES, FAST_RATES, VERIFIED, SUPPORTED_MODELS, supported_model, request_tier, display_tier, token_cost, sum_cost
from .session_costs import own_costs, session_costs
from .charts import UsageTrend, SourceBars, ComparisonChart, TokenComposition, value_text, COLORS
from .observer_panel import ObserverPanel
from .notifications import ConfirmedNotifications
from .ui_details import Details, strong, recorded, record_issues, price_reason, observed_transport, model_comparison
from .theme import shared_theme
from .version import VERSION
from .i18n import tr, Verbatim
from .workload import call_count
from .history_navigation import HistoryNavigation, HistoryWorkspace, HistoryTree

STYLE = ''
TITLES = ('사용 현황','조건 비교','세션 기록','사용 한도','설정')
PERIODS = [('최근 30분','30m'),('오늘','today'),('최근 7일','7d'),('최근 30일','30d'),('전체 기록','all'),('직접 지정','custom')]
INPUT_BANDS = [('전체 입력 길이',''),('10k 미만','0:10000'),('10–50k','10000:50000'),('50–100k','50000:100000'),('100–200k','100000:200000'),('200–272k','200000:272001'),('272k 초과','272001:inf')]
CALL_FILTERS = [('미산정','unpriced'),('모드 기록 없음','unknown_mode'),('모델명 불일치','model_mismatch'),('기록 누락·충돌','observation_problem'),('캐시 읽기 0','cache_zero'),('캐시 저하 의심','cache_degradation'),('HTTP/SSE','http')]
CALL_COLUMNS = [('ts','기록 시각'),('model','모델 · 추론 · 모드'),('cost','환산액'),('input','입력'),('output','출력'),('cache_ratio','캐시 적중률'),('completion_latency_ms','소요시간')]
EXTRA_COLUMNS = [('input','입력'),('cached','캐시 읽기'),('written','캐시 쓰기'),('output','출력'),('reasoning','추론'),('non_reasoning','추론 외'),('output_speed','평균 출력 속도'),('response_model','응답 모델'),('response_service_tier','응답 등급'),('transport','통신 방식')]


def number(value, decimals=0): return '—' if value is None else f'{value:,.{decimals}f}'
def average_tokens(group, metric='total'):
    n=(group.get('count') or 0)-group.get('missing',{}).get(metric,0)
    return '—' if not n or group.get(metric) is None else number(group[metric]/n,1)
def date_time(ts): return datetime.fromtimestamp(ts).strftime('%Y/%m/%d %H:%M:%S') if ts is not None else '—'
def label(text, style='', wrap=False):
    node=Text(text);node.setWordWrap(wrap)
    if style:node.setObjectName(style)
    return node
def combo(items):
    node=Choice()
    for title,value in items:node.addItem(title,value)
    return node
def table(headers):
    node=LazyTable(headers);node.put(rowHeight=40,rowDetails=True);return node
def fill(node, rows, numeric_from=1):
    node.set_rows(rows,lambda row,col,role:str(row[col]));node.put(leftColumns=list(range(numeric_from)))
def panel(title, chart, subtitle=''):
    node=Group();layout=Column(node);layout.setSpacing(12)
    layout.addWidget(label(title,'section'))
    if subtitle:layout.addWidget(label(subtitle,'muted',True))
    layout.addWidget(chart);return node
def choose(node,value):
    if value and node.findData(value)<0 and hasattr(node,'_value_aliases'):
        value=node._value_aliases.get(project_key(value),value)
    blocked=node.blockSignals(True);node.setCurrentIndex(max(0,node.findData(value)));node.blockSignals(blocked)
def action(icon, title, handler):
    button=Button('');button.put(iconName=icon,flat=True);button.setFixedSize(36,36)
    button.setAccessibleName(title);button.setToolTip(title);button.clicked.connect(handler)
    return button

def call_id(row):return row.get('call_id') or row.get('key') or row.get('response_id')
def identity(row):return (row.get('home'),row.get('sid'),call_id(row))
def cache_ratio(row):
    i,r=row.get('input'),row.get('cached')
    return 100*r/i if type(i) is int and type(r) is int and i>0 and 0<=r<=i else None
def observation(row):
    if row.get('model_alert_confirmed'):return '모델명 불일치'
    flags=observation_flags(row)
    if flags['conflict']:return '관측 충돌'
    if flags['missing']:return '관측 누락'
    return row.get('model_match') or '미확인'

def record_summary(rows):
    priced=[r['cost'] for r in rows if r.get('cost') is not None]
    pairs=cache_rows(rows);total=sum(r['input'] for r in pairs)
    return {'cost':{'sum':sum(priced) if priced or not rows else None,'partial':0<len(priced)<len(rows)},
            'cache':{'value':100*sum(r['cached'] for r in pairs)/total if total else None}}


class Dashboard(TrayWindow):
    @property
    def record_view(self):return self.history_navigation.view
    @record_view.setter
    def record_view(self,value):self.history_navigation.view=value

    @property
    def selected_session(self):return self.history_navigation.session
    @selected_session.setter
    def selected_session(self,value):self.history_navigation.session=value

    @property
    def selected_turn(self):return self.history_navigation.request
    @selected_turn.setter
    def selected_turn(self,value):self.history_navigation.request=value

    @property
    def selected_call(self):return self.history_navigation.call
    @selected_call.setter
    def selected_call(self,value):self.history_navigation.call=value

    @property
    def selected_event(self):return self.history_navigation.event
    @selected_event.setter
    def selected_event(self,value):self.history_navigation.event=value

    def __init__(self, homes, start_worker=True, settings=None, index_path=None, static_snapshot=None, live_limits=True, manage_observer=False,model_evidence_path=None,quota_path=None,collection_autostart=True):
        self.quota_path=quota_path
        super().__init__()
        self.settings=settings or QSettings('CacheMonitor','CacheMonitor')
        self.observer_home=homes[0] if homes else str(Path.home()/'.codex')
        self.observer_directory=Path(index_path).parent if index_path else None
        self.manage_observer=manage_observer;self.quota_service=None;self.quota_issue=''
        self.index_path=index_path;self.live_limits=live_limits
        self.model_evidence_path=model_evidence_path
        self.snapshot={'ts':time.time(),'sessions':[],'homes':homes,'unassigned':[],'errors':[]}
        self.quitting=False;self.worker=None;self.engine=AnalysisEngine();self.async_mode=start_worker
        self.analysis=analyze([]);self.analysis_pending=False;self.analysis_errors=[];self.analysis_error_history=[]
        self.client_cache=BoundedCache(6,160000);self.request_id=0;self.view_result=None;self.applied_key=None
        self.lookup={'sessions':{},'turns':{},'session_turns':{}};self.page_frames={}
        self.started_at=time.time();self.alerted={};self.confirmed_notifications=ConfirmedNotifications()
        self.current_page=0;self.restoring=True;self.back_stack=[];self.temporary_context=None;self.navigation_signature=None
        self.page_filters={0:{'model':'','effort':'','service_tier':''},2:{'model':'','effort':'','service_tier':''}}
        self.targets=[];self.comparison_edited=False;self.baseline='A';self.filter_values={}
        self.history_navigation=HistoryNavigation()
        self.selected_session=None;self.selected_turn=None;self.selected_call=None;self.selected_event=None;self.selected_call_scope=None
        self.record_section='identity';self.record_view='projects';self.record_rows=[];self.exact_record=None
        self.population_request=0;self.pending_population=None
        self.record_request=0;self.record_status='';self.aggregate_records=[];self.aggregate_selection=None
        self.interaction_time=0;self.deferred_result=None;self.defer_timer=QTimer(self)
        self.defer_timer.setSingleShot(True);self.defer_timer.timeout.connect(self.apply_deferred)
        self.pending_timer=QTimer(self);self.pending_timer.setSingleShot(True);self.pending_timer.setInterval(250)
        self.pending_timer.timeout.connect(self.show_analysis_pending)
        QApplication.instance().installEventFilter(self)
        shared_theme().configure(self.settings.value('ui/theme',self.settings.value('overlay/theme','codex')))
        self.setWindowTitle('Codexon');self.setWindowIcon(tray_icon());self.resize(1440,940)
        self.fit_screen()
        geometry=self.settings.value('dashboard/geometry')
        if geometry:self.restoreGeometry(geometry)
        else:
            work=self.screen().availableGeometry();self.resize(min(1440,work.width()),min(940,work.height()))
        self.fit_screen()
        root=Group();root.setObjectName('workspace');shell=Row(root);shell.setContentsMargins(0,0,0,0);shell.setSpacing(0)
        side=Group();side.setFixedWidth(200);side.setObjectName('sidebar');side_layout=Column(side)
        side_layout.setContentsMargins(16,24,16,20);side_layout.setSpacing(24)
        side_layout.addWidget(label('CODEX·ON','brand'))
        self.nav=Navigation();self.nav.addItems(TITLES[:4]);self.nav.setObjectName('navigation');self.nav.setMaximumHeight(220)
        side_layout.addWidget(self.nav);side_layout.addStretch()
        self.settings_button=Button('설정');self.settings_button.put(flat=True);self.settings_button.clicked.connect(self.open_settings)
        side_layout.addWidget(self.settings_button);self.index_status=label('수집 중','muted',True);self.index_status.put(fontSize=12);side_layout.addWidget(self.index_status)
        shell.addWidget(side)
        content=Group();self.content=content;content.put(contentWidth=1808);shell.addWidget(content,1);layout=Column(content)
        layout.setContentsMargins(24,20,24,20);layout.setSpacing(10)
        header=Row();self.back_button=action('back','뒤로',self.go_back)
        self.home_button=action('home','현재 탭 처음으로',self.go_home)
        self.navigation_row=Row();self.navigation_row.setSpacing(4)
        self.navigation_row.addWidget(self.home_button);self.navigation_row.addWidget(self.back_button)
        self.path_label=label('','muted');self.path_label.put(noElide=False);self.navigation_row.addWidget(self.path_label,1)
        self.history_path=Row();self.history_path.setSpacing(4);self.navigation_row.addWidget(self.history_path,1)
        self.heading=label(TITLES[0],'heading');header.addWidget(self.heading);header.addStretch()
        self.pending_label=label('','muted');self.pending_label.setFixedHeight(36);self.pending_label.setMaximumWidth(280);header.addWidget(self.pending_label)
        self.price_button=Button('기준 환산 단가');self.price_button.clicked.connect(self.show_prices);header.addWidget(self.price_button);layout.addLayout(header)
        self.common_filters=Row();self.common_filters.put(flow=True,spacing=8)
        self.home=combo([('모든 Codex 홈','')]+[(Verbatim(h),h) for h in homes]);self.home.setMinimumWidth(205)
        self.period=combo(PERIODS);choose(self.period,'30d')
        self.date_start=DateInput(QDate.currentDate().addDays(-29));self.date_end=DateInput(QDate.currentDate())
        self.project=combo([('모든 프로젝트','')]);self.project.setMinimumWidth(220)
        self.source=combo([('모든 작업 종류','')]);self.archive=Toggle('보관 세션 포함');self.archive.setChecked(True)
        for node in (self.home,self.period,self.date_start,self.date_end,self.project,self.source,self.archive):self.common_filters.addWidget(node)
        layout.addLayout(self.navigation_row);layout.addLayout(self.common_filters)
        self.model=combo([('모든 모델','')]);self.model.setMinimumWidth(230)
        self.effort=combo([('모든 추론 설정','')]);self.mode=combo([('모든 요청 모드','')])
        for node in (self.model,self.effort,self.mode):self.common_filters.addWidget(node)
        for node,width in ((self.period,150),(self.project,190),(self.source,155),(self.model,190),(self.effort,150),(self.mode,150)):
            node.setFixedWidth(width)
        self.scope_note=label('수집 중','muted');self.scope_note.put(fontSize=12);self.scope_note.setFixedHeight(18);layout.addWidget(self.scope_note)
        self.pages=Stack();self.pages.put(deferPages=True);layout.addWidget(self.pages,1);self.scrollers={}
        self.build_overview();self.build_compare();self.build_explorer()
        from .quota_panel import QuotaPanel
        self.quota_panel=QuotaPanel(self.settings);quota_scroll=Scroll();quota_scroll.put(fillViewport=True)
        quota_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);quota_scroll.setWidget(self.quota_panel);self.pages.addWidget(quota_scroll)
        self.scrollers[3]=quota_scroll;self.build_settings()
        self.health=label('','muted',True);self.health.hide()
        self.setup_tray();self.settings_page.bind_tray(self);self.tray.messageClicked.connect(self.open_notification_details)
        self.tick=QTimer(self);self.tick.timeout.connect(self.check_stale);self.tick.start(1000)
        self.nav.currentRowChanged.connect(lambda row:self.change_page(5 if row==4 else row) if row>=0 else None)
        for node in (self.home,self.period,self.project,self.source,self.model,self.effort,self.mode):node.currentIndexChanged.connect(self.filter_changed)
        for node in (self.date_start,self.date_end):node.dateChanged.connect(self.filter_changed)
        self.archive.toggled.connect(self.filter_changed)
        self.restore_preferences();self.restoring=False;self.change_page(self.current_page)
        if start_worker:
            if homes and static_snapshot is None:self.start_quota_service(homes[0])
            self.worker=AnalysisBridge(homes,index_path,static_snapshot,model_evidence_path,quota_path)
            self.worker.snapshot.connect(self.receive);self.worker.result.connect(self.analysis_ready)
            self.worker.failure.connect(self.analysis_failed);self.worker.record.connect(self.record_ready);self.worker.population.connect(self.population_ready);self.worker.start();self.render()
        elif static_snapshot is not None:self.receive(static_snapshot)
        self.setCentralWidget(root)
        if hasattr(self.quota_panel,'set_homes'):self.quota_panel.set_homes(homes)
        if hasattr(self.quota_panel,'home_selected'):self.quota_panel.home_selected.connect(self.start_quota_service)
        from .app_services import AppServices
        from .app_shutdown import ServiceController
        self.app_services=AppServices(self.observer_panel.manager if manage_observer else None,
            homes,index_path,model_evidence_path,collection=bool(start_worker and collection_autostart and static_snapshot is None))
        self.service_controller=ServiceController(self.app_services,self)
        self.observer_panel.services=self.app_services if manage_observer else None
        self.service_controller.result.connect(self.observer_panel.display)
        if manage_observer or self.app_services.collection:
            QTimer.singleShot(0,self.service_controller.start)

    def fit_screen(self):
        screen=self.screen() or QApplication.primaryScreen()
        if screen is None:return
        work=screen.availableGeometry()
        self.setMinimumSize(min(1000,work.width()),min(680,work.height()))
        self.resize(min(self.width(),work.width()),min(self.height(),work.height()))
        frame=self.frameGeometry()
        x=max(work.left(),min(frame.left(),work.right()-frame.width()+1))
        y=max(work.top(),min(frame.top(),work.bottom()-frame.height()+1))
        self.move(x,y)

    def showEvent(self,event):
        super().showEvent(event)
        if not getattr(self,'_screen_bound',False) and self.windowHandle():
            self.windowHandle().screenChanged.connect(lambda _screen:self.fit_screen())
            self._screen_bound=True
        self.fit_screen()

    def start_quota_service(self,home):
        from .quota_service import QuotaService
        services=getattr(self,'quota_services',{})
        self.quota_services=services
        for candidate in self.snapshot.get('homes',[home]):
            if candidate not in services:
                service=QuotaService(candidate,self.index_path,live=self.live_limits,quota_path=self.quota_path,
                    tracking_enabled=self.settings.value('quota/trackingEnabled',True,type=bool),reports_enabled=False)
                services[candidate]=service
                service.updated.connect(lambda value,expected=service: self.receive_quota(value) if self.quota_service is expected else None)
                service.start()
        if home not in services:
            services[home]=QuotaService(home,self.index_path,live=self.live_limits,quota_path=self.quota_path,
                tracking_enabled=self.settings.value('quota/trackingEnabled',True,type=bool),reports_enabled=False)
            services[home].updated.connect(lambda value,expected=services[home]:self.receive_quota(value) if self.quota_service is expected else None)
            services[home].start()
        services[home].supply_local(self.snapshot.get('quota_by_home',{}).get(services[home].home))
        self.live_quota=None;self.quota_service=services[home]
        self.sync_report_demand()
        self.refresh_tray()
        self.quota_service.wake.set()

    def set_quota_tracking_enabled(self, enabled):
        self.settings.setValue('quota/trackingEnabled',bool(enabled))
        for service in getattr(self,'quota_services',{}).values():
            service.set_tracking_enabled(enabled)

    def receive_quota(self,value):
        self.quota_panel.defer_render=self.current_page!=3 or not self.isVisible() or self.isMinimized()
        if self.quota_panel.receive(value) is False:return
        self.live_quota=value.get('quota');self.quota_issue=value.get('issue','');self.refresh_tray()

    def sync_report_demand(self):
        visible=self.isVisible() and not self.isMinimized()
        page=getattr(self,'current_page',0)
        for service in getattr(self,'quota_services',{}).values():
            service.set_reports_enabled(visible and page==3 and service is self.quota_service)

    def scroll_page(self,index):
        area=Scroll();area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);body=Group();layout=Column(body)
        layout.setContentsMargins(0,0,8,0);layout.setSpacing(24);area.setWidget(body);self.pages.addWidget(area);self.scrollers[index]=area
        return layout

    def build_overview(self):
        layout=self.scroll_page(0)
        summary=Group();summary.setObjectName('summary');columns=Row(summary);columns.setContentsMargins(16,16,16,16);columns.setSpacing(20)
        columns.put(minColumnWidth=180)
        self.metrics=[];self.metric_captions=[];self.metric_notes=[]
        for i,title in enumerate(('비용','평균 호출 비용','완료 요청당 평균','캐시 적중률','평균 출력 속도')):
            col=Column();caption=label(title,'muted');value=Button('—');value.put(flat=True,fontSize=30,bold=True,noElide=True)
            if i==4:value.put(fontSize=24)
            tip=TIPS[('cost','mean','request_mean','cache_total','speed_total')[i]]
            caption.setToolTip(tip);value.setToolTip(tip)
            value.clicked.connect(lambda index=i:self.open_summary(index));note=label('','muted',True);note.put(fontSize=12)
            for node in (caption,value,note):col.addWidget(node)
            columns.addLayout(col,1);self.metrics.append(value);self.metric_captions.append(caption);self.metric_notes.append(note)
        layout.addWidget(summary)
        controls=Row();controls.put(flow=True);controls.addWidget(label('시간 추이','section'))
        self.overview_metric=combo([('환산액','cost'),('호출 수','count'),('캐시 적중률','cache_ratio')])
        self.overview_basis=combo([('합계','total'),('평균 호출 비용','call_mean'),('완료 요청당 평균','turn_mean')])
        self.overview_bucket=combo([('자동','auto'),('5분','5min'),('시간','hour'),('일','day'),('주','week'),('월','month')])
        for node in (self.overview_metric,self.overview_basis,self.overview_bucket):controls.addWidget(node);node.currentIndexChanged.connect(self.render)
        layout.addLayout(controls);self.timeline=UsageTrend();self.timeline.selected.connect(self.select_aggregate);layout.addWidget(self.timeline)
        pair=Row();pair.put(collapseBelow=1000,spacing=24)
        sources=Group();left=Column(sources);left.setSpacing(12);head=Row();head.addWidget(label('환산액 발생처','section'))
        self.group_by=combo([('프로젝트','project'),('모델','model'),('작업 종류','source'),('세션','session')]);head.addWidget(self.group_by)
        self.group_by.currentIndexChanged.connect(self.render);left.addLayout(head)
        self.source_bars=SourceBars();self.source_bars.selected.connect(self.select_aggregate);left.addWidget(self.source_bars)
        self.component_bars=SourceBars();self.component_bars.selected.connect(self.select_aggregate)
        pair.addWidget(sources,3);pair.addWidget(panel('환산액 구성',self.component_bars),2);layout.addLayout(pair)
        self.attention=Row();self.attention.put(flow=True);self.attention_buttons={}
        for title,key in (('미산정 호출','unpriced'),('모델명 불일치','model_mismatch'),('캐시 저하 의심','cache_degradation'),('기록 누락','observation_missing')):
            button=Button(title);button.put(flat=True);button.setToolTip(TIPS['unpriced'] if key=='unpriced' else '');button.clicked.connect(lambda k=key:self.open_attention(k));self.attention_buttons[key]=button;self.attention.addWidget(button)
        layout.addLayout(self.attention);self.overview_detail=self.aggregate_panel(layout)

    def aggregate_panel(self,layout):
        group=Group();group.setObjectName('panel');body=Column(group);body.setContentsMargins(16,16,16,16)
        heading=Row();title=label('선택 상세','section');heading.addWidget(title);heading.addStretch();close=action('close','선택 상세 닫기',lambda:self.close_aggregate(group));heading.addWidget(close);body.addLayout(heading)
        text=label('','',True);text.setTextFormat(Qt.RichText);text.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard);body.addWidget(text)
        assumptions=Details('모드 기록 없는 호출의 환산 가정',compact=True);body.addWidget(assumptions)
        records=Button('기록 보기');records.clicked.connect(self.open_aggregate_records);body.addWidget(records)
        apply_band=Button('이 입력 구간으로 비교');apply_band.clicked.connect(self.apply_selected_band);body.addWidget(apply_band);apply_band.hide()
        group.hide();layout.addWidget(group);return dict(group=group,title=title,text=text,records=records,assumptions=assumptions,apply_band=apply_band)

    def build_compare(self):
        layout=self.scroll_page(1);header=Row();header.put(flow=True,spacing=12)
        self.compare_model=combo([]);self.compare_model.setMinimumWidth(230)
        self.compare_model.setAccessibleName('비교 모델');header.addWidget(self.compare_model)
        self.compare_model.currentIndexChanged.connect(self.comparison_type_changed)
        self.compare_type=combo([('추론별 비교','effort'),('Fast 비교','mode')])
        self.compare_type.hide();self.compare_type.currentIndexChanged.connect(self.comparison_type_changed)
        self.comparison_tabs={}
        for text,key in [('추론별 비교','effort'),('Fast 비교','mode')]:
            tab=Button(text);tab.setCheckable(True);tab.put(flat=True,selectionTab=True)
            tab.clicked.connect(lambda value=key:self.select_compare_type(value))
            self.comparison_tabs[key]=tab;header.addWidget(tab)
        layout.addLayout(header)
        self.comparison_note=label('비교할 조건을 선택하세요','muted',True);layout.addWidget(self.comparison_note)
        options=Group();options_layout=Column(options)
        conditions=Row();conditions.put(flow=True,spacing=8)
        self.band=combo(INPUT_BANDS);self.cache_band=combo([('전체 캐시 적중률',''),('0%','zero'),('0% 초과–25% 미만','0:25'),('25–50% 미만','25:50'),('50–75% 미만','50:75'),('75–100% 미만','75:100'),('100%','full')])
        self.transport=combo([('모든 통신 방식',''),('WebSocket','WebSocket'),('HTTP/SSE','HTTP/SSE'),('통신 기록 없음','미확인')])
        self.transport_source=combo([('관측·추정 전체',''),('관측','observed'),('추정','estimated')]);self.cache_policy=combo([('모든 캐시 정책','')])
        for node in (self.band,self.cache_band,self.transport,self.transport_source,self.cache_policy):conditions.addWidget(node);node.currentIndexChanged.connect(self.render)
        options_layout.addLayout(conditions)
        self.unit=combo([('호출','response'),('완료 요청','turn')]);self.comparison_metric=combo([('비용','cost'),('입력 토큰','input'),('출력','output'),('추론 토큰','reasoning'),('캐시 적중률','cache_ratio')])
        self.method=combo([('평균','mean'),('중앙값','median'),('P90','p90')]);self.comparison_mode=combo([('관측 비교','observed'),('동일 토큰 환산','repricing')])
        for node in (self.unit,self.comparison_metric):header.addWidget(node)
        for node in (self.method,self.comparison_mode):options_layout.addWidget(node)
        for node in (self.unit,self.comparison_metric,self.method,self.comparison_mode):node.currentIndexChanged.connect(self.comparison_metric_changed)
        self.result_view=combo([('분포','distribution')]);self.result_view.currentIndexChanged.connect(self.render)
        self.result_view.hide()
        self.comparison_options=Details('세부 조건·계산 방식',options,compact=True);layout.addWidget(self.comparison_options)
        self.comparison_chart=ComparisonChart();self.comparison_chart.selected.connect(self.select_comparison);layout.addWidget(self.comparison_chart)
        self.difference_note=label('','muted',True);layout.addWidget(self.difference_note)
        self.matrix_body=Group();ml=Column(self.matrix_body);self.matrix_by=combo([('입력 길이','input'),('캐시 적중률','cache'),('작업 종류','source'),('프로젝트','project')]);self.matrix_by.currentIndexChanged.connect(self.render)
        ml.addWidget(self.matrix_by);self.matrix=table(['대상','조건','대표값','유효 / 대상']);self.matrix.put(inline=True);self.matrix.cellClicked.connect(self.select_matrix);ml.addWidget(self.matrix)
        self.condition_details=Details('조건별 보기',self.matrix_body,compact=True);layout.addWidget(self.condition_details)
        self.budget_details=Details('평균 환산액 구성',compact=True);layout.addWidget(self.budget_details)
        self.decomposition_details=Details('요청당 환산액 분해',compact=True);layout.addWidget(self.decomposition_details)
        self.comparison_unknown=Button('');self.comparison_unknown.put(flat=True);self.comparison_unknown.clicked.connect(self.open_unknown_comparison)
        layout.addWidget(self.comparison_unknown)
        self.comparison_detail=self.aggregate_panel(layout)

    def build_explorer(self):
        page=Group();layout=Column(page);layout.setSpacing(12)
        self.breadcrumb=self.path_label
        self.session_scope=label('','muted');self.session_scope.setMinimumWidth(0);self.session_scope.setFixedHeight(42);self.session_scope.put(fontSize=12)
        self.residual_details=Details('누계 차액·미분류',compact=True);self.residual_details.hide();layout.addWidget(self.residual_details)
        controls=Row();controls.put(flow=True,spacing=8);controls.addWidget(self.session_scope);self.record_view_choice=combo([('세션','sessions'),('호출','calls')]);self.record_view_choice.hide()
        self.record_tabs=[]
        for i in range(2):
            tab=Button();tab.setCheckable(True);tab.put(flat=True,selectionTab=True)
            tab.clicked.connect(lambda index=i:self.select_record_view(index));controls.addWidget(tab);self.record_tabs.append(tab)
        self.record_view_choice.currentIndexChanged.connect(self.record_view_changed)
        self.search=Input();self.search.setPlaceholderText('작업명 · 프로젝트명 · 세션 ID');self.search.setFixedWidth(260);self.search_timer=QTimer(self);self.search_timer.setSingleShot(True);self.search_timer.setInterval(120)
        self.search_timer.timeout.connect(self.render_explorer)
        self.search.textChanged.connect(lambda:self.search_timer.start());controls.addWidget(self.search)
        self.sort=combo([('최신 기록 순','time_desc'),('오래된 기록 순','time_asc'),('환산액 높은 순','cost_desc'),('환산액 낮은 순','cost_asc')]);self.sort.currentIndexChanged.connect(self.render_explorer);controls.addWidget(self.sort)
        layout.addLayout(controls)
        filter_body=Group();fl=Column(filter_body);filter_row=Row();filter_row.put(flow=True);self.call_filter_controls={}
        for title,key in CALL_FILTERS:
            node=Toggle(title);node.toggled.connect(self.call_filter_changed);filter_row.addWidget(node);self.call_filter_controls[key]=node
        fl.addLayout(filter_row);self.outside=Toggle('범위 밖 호출 표시');self.outside.toggled.connect(self.render_explorer);fl.addWidget(self.outside)
        extra_row=Row();extra_row.put(flow=True);self.call_columns_row=extra_row;self.extra_column_controls={}
        for key,title in EXTRA_COLUMNS:
            node=Toggle(title);node.toggled.connect(self.render_explorer);extra_row.addWidget(node);self.extra_column_controls[key]=node
        fl.addLayout(extra_row);self.record_filters=Details('필터·표시 항목',filter_body,compact=True);layout.addWidget(self.record_filters)
        self.record_filters.toggle.toggled.connect(lambda _:self.sync_history_filters())
        self.record_message=label('','muted');self.record_message.setFixedHeight(24);self.record_message.setMaximumWidth(300);self.navigation_row.addWidget(self.record_message)
        self.records_body=HistoryWorkspace();self.records_body.resized.connect(self.layout_record_detail)
        self.record_parent=Group();parent_layout=Column(self.record_parent)
        self.record_parent.setFixedWidth(248)
        self.record_parent_title=label('프로젝트 · 세션','section');parent_layout.addWidget(self.record_parent_title)
        self.parent_stack=Stack();parent_layout.addWidget(self.parent_stack,1)
        self.session_parent_table=HistoryTree([])
        for parent_table in (self.session_parent_table,):
            parent_table.setMinimumHeight(200);self.parent_stack.addWidget(parent_table)
            parent_table.cellClicked.connect(self.activate_record_parent);parent_table.cellActivated.connect(lambda index,*_:self.activate_record_parent(index,1))
        self.parent_table=self.session_parent_table
        self.records_body.addWidget(self.record_parent)
        self.record_current=Group();current_layout=Column(self.record_current)
        self.record_current_title=label('요청','section');current_layout.addWidget(self.record_current_title)
        self.history_tables={};self.history_table_stack=Stack();current_layout.addWidget(self.history_table_stack,1)
        for kind in ('projects','sessions','children','requests','calls'):
            node=table([]);node.setMinimumHeight(200)
            node.cellClicked.connect(lambda row,col:self.activate_record(row) if col==0 else None)
            node.cellActivated.connect(self.activate_record)
            self.history_tables[kind]=node;self.history_table_stack.addWidget(node)
        self.table=self.history_tables['projects']
        self.records_body.addWidget(self.record_current)
        self.history_summary=label('','muted',True);self.records_body.addWidget(Details('집계 상세',self.history_summary,compact=True))
        self.range_timer=QTimer(self);self.range_timer.setSingleShot(True);self.range_timer.timeout.connect(self.render_explorer)
        for node in (*self.history_tables.values(),self.session_parent_table):
            node.rangeRequested.connect(lambda:self.range_timer.start(0) if not self.analysis_pending else None)
        self.parent_rows=[];self.parent_kind='tree'
        self.detail_scroll=Scroll();self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);detail=Group();detail_layout=Column(detail);detail_layout.setSpacing(24)
        dh=Row();dh.addWidget(label('호출 상세','section'));detail_layout.addLayout(dh)
        self.close_record_button=action('close','호출 상세 닫기',self.close_record_detail);self.close_record_button.hide();self.navigation_row.addWidget(self.close_record_button)
        self.detail_sections={}
        for key,title in (('identity','작업'),('conditions','조건'),('usage','토큰'),('pricing','환산'),('time','시간'),('evidence','데이터 문제')):
            body=Group();bl=Column(body);bl.addWidget(label(title,'section'));text=label('','',True);text.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard);bl.addWidget(text);detail_layout.addWidget(body);self.detail_sections[key]=(body,text)
            if key=='usage':
                self.input_composition=TokenComposition('input');self.output_composition=TokenComposition('output')
                bl.insertWidget(1,self.input_composition);bl.insertWidget(2,self.output_composition)
            if key=='evidence':
                self.incident_button=Button('캐시 사건 보기');self.incident_button.clicked.connect(self.open_call_incident);bl.addWidget(self.incident_button);self.incident_button.hide()
        self.detail_scroll.setWidget(detail);self.detail_scroll.hide();self.records_body.addWidget(self.detail_scroll,0);layout.addLayout(self.records_body,1);self.pages.addWidget(page)

    def restore_preferences(self):
        try:
            saved=json.loads(self.settings.value('dashboard/state','{}'))
            if saved:self.restore_state(saved,initial=True)
        except (ValueError,TypeError,KeyError):pass

    def capture_state(self):
        return dict(design_version=4,history_navigation=self.history_navigation.capture(),comparison_selection=getattr(self,'comparison_selection',None),compare_model=self.compare_model.currentData(),page=self.current_page,settings_category=self.settings_page.current_category(),
            common={key:getattr(self,key).currentData() for key in ('home','period','project','source')},
            archive=self.archive.isChecked(),dates=[self.date_start.text(),self.date_end.text()],
            page_filters=copy.deepcopy(self.page_filters),targets=copy.deepcopy(self.targets),baseline=self.baseline,
            comparison_edited=self.comparison_edited,
            choices={key:getattr(self,key).currentData() for key in ('compare_type','unit','comparison_metric','method','band','cache_band','transport','transport_source','cache_policy','comparison_mode','result_view','matrix_by','overview_metric','overview_basis','overview_bucket','group_by','sort')},
            selected_session=self.selected_session,selected_turn=self.selected_turn,selected_call=self.selected_call,selected_call_scope=self.selected_call_scope,
            selected_event=self.selected_event,record_section=self.record_section,record_view=self.record_view,
            search=self.search.text(),call_filters=[k for k,n in self.call_filter_controls.items() if n.isChecked()],
            extras=[k for k,n in self.extra_column_controls.items() if n.isChecked()],outside=self.outside.isChecked(),
            scrolls={str(k):v.verticalPosition.value() for k,v in self.scrollers.items()},
            record_scroll=self.table.verticalScrollBar().value(),detail_scroll=self.detail_scroll.verticalPosition.value(),
            record_horizontal=self.table.horizontalScrollBar().value(),settings_scrolls=self.settings_page.capture_scrolls(),
            parent_scroll=self.parent_table.verticalScrollBar().value(),parent_horizontal=self.parent_table.horizontalScrollBar().value(),
            record_selection=self.table.row_key(self.record_rows[self.table.currentRow()]) if 0<=self.table.currentRow()<len(self.record_rows) else None,
            widths=self.table.state['widths'],temporary=copy.deepcopy(self.temporary_context))

    def restore_state(self,state,initial=False):
        previous=self.restoring;self.restoring=True
        self.history_navigation.restore({} if initial else state.get('history_navigation',{}))
        for key,value in state.get('common',{}).items():
            node=getattr(self,key)
            if value and node.findData(value)<0:node.addItem(str(value),value)
            choose(node,value)
        self.archive.setChecked(state.get('archive',True))
        dates=state.get('dates',[])
        if len(dates)==2:self.date_start.setText(dates[0]);self.date_end.setText(dates[1])
        self.page_filters={int(k):v for k,v in state.get('page_filters',self.page_filters).items()}
        for values in self.page_filters.values():
            for key,attr in (('model','model'),('effort','effort'),('service_tier','mode')):
                value=values.get(key);node=getattr(self,attr)
                if value and node.findData(value)<0:node.addItem(str(value),value)
        self.targets=copy.deepcopy(state.get('targets',[]));self.baseline=state.get('baseline','A')
        self.comparison_edited=True
        self.comparison_selection=state.get('comparison_selection')
        if not self.comparison_selection:self.comparison_detail['group'].hide()
        model=state.get('compare_model') or next((t.get('model') for t in self.targets if t.get('model')),'')
        if supported_model(model) and self.compare_model.findData(model)<0:self.compare_model.addItem(model,model)
        choose(self.compare_model,model)
        for key,value in state.get('choices',{}).items():
            if key=='compare_type' and value not in ('effort','mode'):value='effort'
            if key=='result_view':value='distribution'
            choose(getattr(self,key),value)
        self.refresh_target_editors()
        self.selected_session=tuple(state['selected_session']) if state.get('selected_session') else None
        self.selected_turn=state.get('selected_turn');self.selected_call=state.get('selected_call');self.selected_event=state.get('selected_event')
        self.selected_call_scope=tuple(state['selected_call_scope']) if state.get('selected_call_scope') else self.selected_session
        self.record_section=state.get('record_section','identity');self.record_view=state.get('record_view','projects')
        if initial:
            self.record_view='projects';self.selected_session=self.selected_turn=self.selected_call=self.selected_event=self.selected_call_scope=None
        self.search.setText(state.get('search',''));self.outside.setChecked(state.get('outside',False))
        for key,node in self.call_filter_controls.items():node.setChecked(key in state.get('call_filters',[]))
        for key,node in self.extra_column_controls.items():node.setChecked(key in state.get('extras',[]))
        self.temporary_context=None if initial else state.get('temporary');self.current_page=max(0,min(4,state.get('page',0)))
        if initial and self.current_page==4:self.current_page=0
        self.exact_record=None;self.record_request+=1;self._revealed_call=None
        self.settings_page.reveal(state.get('settings_category','general'))
        for key,attr in (('model','model'),('effort','effort'),('service_tier','mode')):
            choose(getattr(self,attr),self.page_filters.get(self.current_page,{}).get(key,''))
        if initial:state={**state,'record_scroll':0,'record_horizontal':0,'record_selection':None,'parent_scroll':0,'parent_horizontal':0}
        self._restore_positions=state
        self.parent_table.verticalScrollBar().setValue(state.get('parent_scroll',0));self.parent_table.horizontalScrollBar().setValue(state.get('parent_horizontal',0))
        self.restoring=previous
        if not initial:
            self.change_page(self.current_page)
            for key,value in state.get('scrolls',{}).items():
                if int(key) in self.scrollers:self.scrollers[int(key)].verticalPosition.setValue(value)
            self.table.verticalScrollBar().setValue(state.get('record_scroll',0));self.detail_scroll.verticalPosition.setValue(state.get('detail_scroll',0))
            self.table.horizontalScrollBar().setValue(state.get('record_horizontal',0))
            self.settings_page.restore_scrolls(state.get('settings_scrolls',{}))
            if len(state.get('widths',[]))==self.table.columnCount():self.table.put(widths=state['widths'])
            if self.current_page>=3:self._restore_positions=None

    def save_preferences(self):
        if self.restoring or self.temporary_context:return
        state=self.capture_state();state['temporary']=None
        if state['page']==4:state['page']=0
        encoded=json.dumps(state,ensure_ascii=False)
        if encoded!=getattr(self,'_saved_state',None):
            self.settings.setValue('dashboard/state',encoded);self._saved_state=encoded

    def push_state(self):
        state=self.capture_state()
        if not self.back_stack or self.back_stack[-1]!=state:self.back_stack.append(state)
        self.back_button.setEnabled(True)

    def go_back(self):
        if self.current_page==1 and self.comparison_detail['group'].isVisible():
            self.close_aggregate(self.comparison_detail['group']);return
        if self.back_stack:
            state=self.back_stack.pop();self.navigation_signature=None;self.restore_state(state)
        elif self.selected_call:self.close_record_detail()
        elif self.selected_turn:self.selected_turn=None;self.record_view='requests';self.render_explorer()
        elif self.selected_session:self.selected_session=None;self.record_view='sessions';self.render_explorer()
        self.update_navigation()

    def close_aggregate(self,group):
        group.hide()
        if group is self.comparison_detail['group']:self.comparison_selection=None
        self.update_navigation()

    def update_navigation(self):
        if self.current_page==2:
            session=next((s for s in self.snapshot['sessions'] if self.selected_session==(s['home'],s['id'])),None)
            path=tr('세션 기록')
            if self.history_navigation.project:
                labels=self.snapshot.get('filter_choices',{}).get('project_labels',{})
                path+=' / '+labels.get(self.history_navigation.project,self.history_navigation.project)
            if session:path+=' / '+(session.get('title') or session['id'])
            if self.selected_turn:path+=' / '+self.selected_turn
            if self.selected_call:path+=' / '+tr('호출 상세')
            deep=bool(self.history_navigation.project or self.selected_turn or self.selected_call or self.temporary_context or self.record_view=='calls')
        else:
            path=TITLES[self.current_page];deep=False
            if self.current_page==1:
                path+=' / '+(self.compare_model.currentText() or '모델 선택')
                if self.comparison_detail['group'].isVisible():path+=' / 선택 상세';deep=True
        self.navigation_row.setVisible(self.current_page in (1,2) or bool(self.back_stack))
        self.history_path.setVisible(self.current_page==2);self.path_label.setVisible(self.current_page!=2)
        self.path_label.setText(Verbatim(path) if self.current_page==2 else path);self.path_label.setToolTip(Verbatim(path) if self.current_page==2 else path)
        self.close_record_button.setVisible(self.current_page==2 and self.detail_scroll.isVisible())
        self.record_message.setVisible(self.current_page==2)
        self.home_button.setEnabled(deep)
        self.back_button.setEnabled(bool(self.back_stack or deep))

    def go_home(self):
        self.push_state()
        if self.current_page==2:
            self.selected_session=self.selected_turn=self.selected_call=self.selected_event=None
            self.selected_call_scope=None;self.exact_record=None;self.temporary_context=None
            self.history_navigation.project='';self.record_view='projects';self.record_request+=1;self.render()
        elif self.current_page==1:self.close_aggregate(self.comparison_detail['group'])
        self.update_navigation()

    def bounds(self):
        now=self.snapshot['ts'];key=self.period.currentData();today=datetime.fromtimestamp(now).date()
        if key=='custom':
            start=self.date_start.date().toPython();end=self.date_end.date().addDays(1).toPython()
            return datetime.combine(start,datetime.min.time()).timestamp(),min(now+.000001,datetime.combine(end,datetime.min.time()).timestamp())
        start={'all':0,'30m':now-1800,'today':day_start(now),
            '7d':datetime.combine(today-timedelta(days=6),datetime.min.time()).timestamp(),
            '30d':datetime.combine(today-timedelta(days=29),datetime.min.time()).timestamp()}.get(key,0)
        return start,now+.000001

    def filter_changed(self,*_):
        if self.restoring:return
        if self.current_page in (0,2):self.page_filters[self.current_page]={key:getattr(self,attr).currentData() for key,attr in (('model','model'),('effort','effort'),('service_tier','mode'))}
        self.render();self.save_preferences()

    def refresh_choices(self):
        choices=self.snapshot.get('filter_choices',{})
        rows=[r for s in self.snapshot['sessions'] for r in s.get('history',[])]
        sources=self.snapshot['sessions']
        projects=project_choices(sources)
        project_labels=choices.get('project_labels',projects['project_labels'])
        project_paths=choices.get('project_paths',projects['project_paths'])
        path_keys=defaultdict(set)
        for key,paths in project_paths.items():
            for path in paths:path_keys[project_key(path)].add(key)
        self.project._value_aliases={path:next(iter(keys)) for path,keys in path_keys.items() if len(keys)==1}
        self.combinations=choices.get('combinations') or [dict(model=r.get('model') or '미확인',effort=r.get('effort') or '미확인',service_tier=request_tier(r)) for r in rows]
        self.filter_values={
            'model':choices.get('models') or self.snapshot.get('models') or sorted({r.get('model') or '미확인' for r in rows}),
            'effort':choices.get('efforts') or sorted({r.get('effort') or '미확인' for r in rows}),
            'service_tier':choices.get('modes') or sorted({request_tier(r) for r in rows}),
            'project':choices.get('projects') or projects['projects'],
            'source':choices.get('sources') or sorted({s.get('source') or 'unknown' for s in sources}),
            'home':choices.get('homes') or self.snapshot['homes'],
            'cache_policy':choices.get('cache_policies') or sorted({r.get('cache_policy') or '미확인' for r in rows})}
        names={'model':'모든 모델','effort':'모든 추론 설정','service_tier':'모든 요청 모드','project':'모든 프로젝트','source':'모든 작업 종류','home':'모든 Codex 홈','cache_policy':'모든 캐시 정책'}
        source_names={'user':'직접 작업','subagent':'하위 에이전트','guardian_review':'내부 검토','maintenance':'캐시 유지','unknown':'기타'}
        def caption(key,value):
            if key=='source':return source_names.get(value,value)
            if key=='project':return project_labels.get(value,Path(value).name or '프로젝트 없는 작업')
            if not recorded(value):return {'model':'모델 기록 없음','effort':'추론 설정 기록 없음','service_tier':'모드 기록 없음','cache_policy':'정책 기록 없음'}.get(key,'기록 없음')
            return value
        for key,values in self.filter_values.items():
            node=getattr(self,'mode' if key=='service_tier' else key);current=node.currentData()
            if key=='project' and current and current not in values:current=node._value_aliases.get(project_key(current),current)
            entries=[(names[key],'')]+[(caption(key,v),v) for v in values]
            if [(node.itemText(i),node.itemData(i)) for i in range(node.count())]==entries:continue
            blocked=node.blockSignals(True);node.clear()
            for title,value in entries:
                node.addItem(Verbatim(title) if key=='project' else str(title),value)
                if key=='project' and value:node.setItemData(node.count()-1,'\n'.join(project_paths.get(value,[])),Qt.ToolTipRole)
            # An explicit selection survives zero samples, even when the source disappears.
            if current and node.findData(current)<0:node.addItem(str(caption(key,current)),current)
            choose(node,current);node.blockSignals(blocked)
        self.home.setVisible(len(self.snapshot['homes'])>1)
        transports=choices.get('transports',[r.get('transport') for r in rows])
        has_transport=any(recorded(value) for value in transports)
        self.transport.setVisible(has_transport or bool(self.transport.currentData()))
        self.transport_source.setVisible(has_transport or bool(self.transport_source.currentData()))
        self.cache_policy.setVisible(any(recorded(value) for value in self.filter_values['cache_policy']) or bool(self.cache_policy.currentData()))
        self.refresh_target_editors()

    def change_page(self,index):
        if self.restoring:return
        if self.current_page in (0,2):
            self.page_filters[self.current_page]={key:getattr(self,attr).currentData() for key,attr in (('model','model'),('effort','effort'),('service_tier','mode'))}
        self.current_page=max(0,min(4,index));self.pages.setCurrentIndex(self.current_page);self.heading.setText(TITLES[self.current_page])
        self.sync_report_demand()
        self.restoring=True
        self.nav.setCurrentRow(self.current_page if self.current_page<4 else -1)
        for key,attr in (('model','model'),('effort','effort'),('service_tier','mode')):
            if self.current_page in (0,2):choose(getattr(self,attr),self.page_filters[self.current_page].get(key,''))
        self.restoring=False
        self.content.put(contentWidth=0 if index==2 else 1808)
        self.project.setVisible(index!=2)
        self.sync_history_filters()
        self.common_filters.setVisible(index<3);self.scope_note.setVisible(index<3)
        for node in (self.model,self.effort,self.mode):node.setVisible(index in (0,2))
        self.price_button.setVisible(index!=4);self.pending_timer.stop();self.pending_label.hide();self.pages.show()
        self.update_navigation()
        self.render();self.save_preferences()

    def open_settings(self):self.show_window();self.change_page(4)
    def open_notification_details(self):
        self.open_settings();self.notification_details.toggle.setChecked(True);self.settings_page.reveal('notifications',self.notification_details)

    def query(self):
        start,end=self.bounds();page=self.current_page
        conditions={}
        for key,node in (('input_band',self.band),('cache_band',self.cache_band)):
            value=node.currentData()
            if value:conditions[key]=[float(x) for x in value.split(':')] if ':' in value else value
        for key in ('transport','transport_source','cache_policy'):
            if getattr(self,key).currentData():conditions[key]=getattr(self,key).currentData()
        q=dict(page=page,include_whole_history=page==2,start=start,end=end,home=self.home.currentData(),project=self.project.currentData(),source=self.source.currentData(),
            archived=self.archive.isChecked(),period=self.period.currentData(),now=self.snapshot['ts'],loading=bool(self.snapshot.get('index',{}).get('loading')),
            unit=self.unit.currentData() if page==1 else 'response',method=self.method.currentData() if page==1 else 'mean',
            metric=self.comparison_metric.currentData() if page==1 else self.overview_metric.currentData() if page==0 else 'cost',
            granularity=self.overview_bucket.currentData(),basis=self.overview_basis.currentData(),group_by=self.group_by.currentData(),
            targets=copy.deepcopy(self.targets) if self.targets or self.comparison_edited else None,baseline=self.baseline,conditions=conditions if page==1 else {},matrix_by=self.matrix_by.currentData(),
            comparison_type=self.compare_type.currentData(),repricing=self.comparison_mode.currentData()=='repricing')
        if page in (0,2):q.update(self.page_filters[page])
        if self.temporary_context and page==2:
            q.update(start=0,end=self.snapshot['ts']+.000001,period='all',home=self.temporary_context.get('home',''),sid=self.temporary_context.get('sid',''),project='',source='',archived=True,model='',effort='',service_tier='')
        q.update(presentation=True,result_view=self.result_view.currentData(),compare_model=self.compare_model.currentData())
        if page==2:
            self.use_history_table()
            q['project']=''
            scope=self.selected_call_scope or self.selected_session
            q['explorer']=dict(project=self.history_navigation.project,expanded=sorted(self.history_navigation.expanded),view=self.record_view,session=self.selected_session,turn=self.selected_turn,
                event=self.selected_event,search=self.search.text(),sort=self.sort.currentData(),
                filters=[k for k,n in self.call_filter_controls.items() if n.isChecked()],
                outside=self.outside.isChecked(),context=self.temporary_context,
                call_identity=(*scope,self.selected_call) if scope and self.selected_call else None)
            q['table_windows']={name:dict(start=max(0,node.first_visible-32),size=max(128,min(512,(node.last_visible-node.first_visible+1)*3)))
                for name,node in (('records',self.table),('parents',self.parent_table))}
        return q

    def render(self,*_,automatic=False):
        if self.restoring or not hasattr(self,'diagnostics'):return
        if automatic and (not self.isVisible() or self.isMinimized()):
            self._display_dirty=True;return
        custom=self.period.currentData()=='custom';self.date_start.setVisible(custom);self.date_end.setVisible(custom)
        if self.current_page>=3:
            if self.current_page==3:self.quota_panel.refresh_status()
            else:self.render_diagnostics()
            return
        self.overview_basis.setVisible(self.overview_metric.currentData()=='cost')
        q=self.query()
        if automatic and q['page']==2:
            for name,node in (('records',self.table),('parents',self.parent_table)):
                i=node.first_visible
                if 0<=i<node.rowCount():
                    row=node.model().rows[i]
                    if not row.get('_placeholder'):
                        window=q['table_windows'][name]
                        window.update(anchor=list(node.row_key(row)),anchor_offset=i-window['start'])
        if self.async_mode:
            # Retain exact overview bucket endpoints; record/comparison populations
            # are reusable until the worker's next call or request boundary.
            key_query={k:v for k,v in q.items() if k!='now' and (q['period']=='custom' or k not in ('start','end'))}
            logical=(self.snapshot.get('data_revision',0),json.dumps(key_query,sort_keys=True))
            if automatic and self.analysis_pending:
                self._refresh_after_pending=getattr(self,'pending_logical',None)!=logical
                return
            self.request_id+=1
            if not automatic:self.deferred_result=None;self.defer_timer.stop()
            self.pending_query=q;self.pending_automatic=automatic;self.pending_logical=logical
            cached=self.client_cache.get(logical)
            if cached and q['now']<cached[1]:
                from .dashboard_views import refresh_bounds
                value=refresh_bounds(cached[0],q)
                if self.view_result is value and automatic:
                    self.analysis_pending=False
                    return
                self.apply_result(value,q,automatic)
                return
            self.analysis_pending=True
            if not automatic:
                self.pending_timer.start()
            if self.worker:self.worker.request(self.request_id,q,logical)
        else:
            self.apply_result(self.engine.page_query(q),q,automatic)

    def show_analysis_pending(self):
        if self.analysis_pending:
            self.pending_label.setText('계산 중');self.pending_label.setToolTip('');self.pending_label.show()

    def scope_text(self,q):
        if self.temporary_context and q['page']==2:
            ctx=self.temporary_context;names=[ctx.get('home',''),ctx.get('sid',''),tr('전체 기록 · 자체 호출')]
            names += list(ctx.get('filters',[]))
            return Verbatim(' · '.join(str(x) for x in names if x))
        names=[Verbatim(q['home']) if q.get('home') else ('모든 Codex 홈' if len(self.snapshot['homes'])>1 else (Verbatim(self.snapshot['homes'][0]) if self.snapshot['homes'] else '로컬 기록')),
            self.period.currentText(),Verbatim(q['project']) if q.get('project') else '모든 프로젝트',self.source.currentText()]
        if q['page'] in (0,2):names += [q.get(k) for k in ('model','effort','service_tier') if q.get(k)]
        return Verbatim(' · '.join(tr(x) for x in names if x))

    def receive(self,value):
        if value.get('data_revision')!=self.snapshot.get('data_revision'):
            self.client_cache.clear()
        if not self.async_mode:
            self.engine.ingest(value['sessions'])
            value={**value,'sessions':[s['source'] for s in self.engine.sessions.values()],
                   'internal_review_calls':sum(len(s['prepared']['history']) for s in self.engine.internal_sessions.values())}
        self.snapshot=value
        for service in getattr(self,'quota_services',{}).values():
            service.supply_local(value.get('quota_by_home',{}).get(service.home))
        overlay=getattr(self,'overlay',None)
        if overlay:overlay.receive_snapshot(value)
        self.refresh_choices();index=value.get('index',{})
        self.index_status.setText('수집 중' if index.get('loading') else '수집 확인 필요' if value.get('errors') else '로컬 기록 수집 중')
        self.show_confirmed_events(self.confirmed_notifications.cache(value.get('cache_candidates',value['sessions'])))
        self.show_confirmed_events(self.confirmed_notifications.http(value['sessions']))
        self.show_confirmed_events(self.confirmed_notifications.model_candidates(value['model_candidates']) if 'model_candidates' in value else self.confirmed_notifications.models(value['sessions']))
        self.refresh_tray();self.render(automatic=True)

    def analysis_ready(self,message):
        result=message['result']
        if message['id']!=self.request_id:return
        self.client_cache.put(message['logical'],(result,message['valid_until']),result['analysis']['response_count']+1)
        self.analysis_errors.clear();self.worker_metrics=message.get('metrics',{})
        if self.pending_automatic and time.monotonic()-self.interaction_time<.15:
            self.deferred_result=(message['id'],message['result'],self.pending_query)
            self.defer_timer.start(160)
        else:self.apply_result(message['result'],self.pending_query,self.pending_automatic)

    @batched_updates
    def apply_result(self,result,query,automatic=False):
        if query['page']!=self.current_page:return
        if automatic and (not self.isVisible() or self.isMinimized()):
            self.analysis_pending=False;self._display_dirty=True;return
        self.analysis_pending=False;self.pending_timer.stop();self.pending_label.hide();self.pages.show()
        self.view_result=result;self.applied_key=result['key'];self.analysis=result['analysis'];self.lookup=result['lookup']
        self.table.live_update=automatic
        self.parent_table.live_update=automatic
        a=self.analysis;count=a['response_count'];sessions=a['session_count']
        self.scope_note.setText(Verbatim(self.scope_text(query)+tr(f' · 관측 {count:,}호출 · {sessions:,}세션'+(' · 수집 중' if query.get('loading') else ''))))
        if not hasattr(self,'_applied_pages'):self._applied_pages={}
        display=tuple(k for k,n in self.extra_column_controls.items() if n.isChecked()) if self.current_page==2 else ()
        previous=self._applied_pages.get(self.current_page)
        detail_changed=self.current_page==2 and bool(self.selected_call or self.selected_event)!=self.detail_scroll.isVisible()
        if previous is None or previous[0] is not result or previous[1]!=display or detail_changed or getattr(self,'_restore_positions',None):
            if self.current_page==0:self.render_overview()
            elif self.current_page==1:
                comparison=result.get('comparison',{})
                if not self.comparison_edited and not self.targets:
                    self.targets=copy.deepcopy(comparison.get('targets',[]))
                    if self.targets and not comparison.get('mode_comparison'):choose(self.compare_type,'effort')
                    self.refresh_target_editors()
                self.render_comparison()
            elif self.current_page==2:self.apply_explorer()
            self._applied_pages[self.current_page]=(result,display)
        state=getattr(self,'_restore_positions',None)
        if state:
            self._restore_positions=None
            for key,value in state.get('scrolls',{}).items():
                if int(key) in self.scrollers:self.scrollers[int(key)].verticalPosition.setValue(value)
            self.table.verticalScrollBar().setValue(state.get('record_scroll',0));self.detail_scroll.verticalPosition.setValue(state.get('detail_scroll',0))
            self.table.horizontalScrollBar().setValue(state.get('record_horizontal',0))
            self.settings_page.restore_scrolls(state.get('settings_scrolls',{}))
            if len(state.get('widths',[]))==self.table.columnCount():self.table.put(widths=state['widths'])
            selected=state.get('record_selection')
            if selected is not None:
                wanted=tuple(selected) if isinstance(selected,(list,tuple)) else selected
                self.table.select_row(next((i for i,row in enumerate(self.record_rows,getattr(self.record_rows,'start',0)) if self.table.row_key(row)==wanted),-1))
        self.save_preferences()
        if getattr(self,'_refresh_after_pending',False):
            self._refresh_after_pending=False;QTimer.singleShot(0,lambda:self.render(automatic=True))

    def analysis_failed(self,error):
        self.client_cache.clear();self.pending_logical=None
        self.pending_timer.stop()
        self.analysis_pending=False;self.analysis_errors=[error];self.analysis_error_history=(self.analysis_error_history+[error])[-10:]
        self.pending_label.setText('갱신 실패 · 이전 결과 표시' if self.view_result else '계산 실패 · 수집 상태 확인');self.pending_label.setToolTip('');self.pending_label.show();self.pages.show()
        self.render_diagnostics()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Wheel,QEvent.MouseButtonPress,QEvent.KeyPress):self.interaction_time=time.monotonic()
        if event.type()==QEvent.KeyPress and event.key()==Qt.Key_Escape and watched is self.quick and self.current_page==2 and self.detail_scroll.isVisible():
            self.close_record_detail();return True
        return super().eventFilter(watched,event)
    def apply_deferred(self):
        pending=self.deferred_result;self.deferred_result=None
        if pending and pending[0]==self.request_id:
            self.apply_result(pending[1],pending[2],True)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'detail_scroll'):self.layout_record_detail()
    def showEvent(self,event):
        super().showEvent(event)
        self.sync_report_demand()
        if getattr(self,'_display_dirty',False):
            self._display_dirty=False;QTimer.singleShot(0,lambda:self.render(automatic=True))

    def hideEvent(self,event):
        super().hideEvent(event);self.sync_report_demand()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.WindowStateChange:self.sync_report_demand()

    def closeEvent(self,event):
        self.settings.setValue('dashboard/geometry',self.saveGeometry());self.save_preferences();super().closeEvent(event)
    def check_stale(self):
        self.refresh_tray()
        if self.isVisible() and not self.isMinimized():
            if self.current_page==3:self.quota_panel.refresh_status()
        if int(time.time())%10==0:shared_theme().configure(self.settings.value('ui/theme',self.settings.value('overlay/theme','codex')))

    def render_overview(self,*_):
        view=self.view_result.get('overview',{});self.overview=view;rows=self.analysis['response_count'];cost=view['call_stats']
        turn_cost=view['turn_stats']
        cache=view['summary']['cache'];inputs=cache['input'];rate=cache['value']
        total=view['total']
        speed=view['summary']['output_speed']
        values=[usd(total),usd(cost['mean']),usd(turn_cost['mean']),value_text(rate,'cache_ratio'),value_text(speed['value'],'output_speed')]
        notes=[f"산정 {cost['n']:,} / 관측 {rows:,}" if cost['n']<rows else f"관측 {rows:,}호출",
            f"유효 {cost['n']:,} / 대상 {rows:,}",
            f"유효 {turn_cost['n']:,}요청 · 포함 {view['request_calls']:,}호출",
            f"유효 {view['cache_known']:,} / 대상 {rows:,} · 입력 {inputs:,}",
            f"측정 {speed['n']:,} / 대상 {speed['N']:,} · 추론·대기 포함"]
        loading=self.snapshot.get('index',{}).get('loading') and not rows
        for node,value,note,text in zip(self.metrics,values,self.metric_notes,notes):node.setText('수집 중' if loading else value if rows else '—');note.setText(text)
        metric=self.overview_metric.currentData();basis=self.overview_basis.currentData();timeline=[]
        for row in view.get('timeline',[]):
            item=dict(row);item['value']=row.get('value',row.get('total'));item['n']=row.get('n',row.get('known',0))
            item['N']=row.get('N',0)
            item['sample_unit']='요청' if metric=='cost' and basis=='turn_mean' else '호출';item['metric']=metric;timeline.append(item)
        self.timeline.metric=metric;self.timeline.line=metric=='cache_ratio' or (metric=='cost' and basis!='total');self.timeline.samples=metric!='count'
        self.timeline.empty_text='수집 중' if loading else '조건에 맞는 기록 없음' if self.snapshot['sessions'] else '사용 기록 없음'
        self.timeline.set_rows([] if loading or not rows else timeline)
        self.source_bars.empty_text=self.timeline.empty_text;self.component_bars.empty_text=self.timeline.empty_text
        self.source_bars.setFixedHeight(max(200,40*len(view.get('sources',[]))));self.source_bars.set_rows([{**r,'metric':'cost'} for r in view.get('sources',[])])
        component_colors={'cost_uncached':'uncached','cost_cached':'cached','cost_written':'written','cost_unclassified':'unknown','cost_output':'output'}
        self.component_bars.set_rows([{**r,'color':component_colors.get(r['key'],'unknown'),'metric':'cost','population':self.view_result['all_population'],'population_count':rows,'assumptions':view['assumptions'],'known':r.get('n',0),'calls':r.get('N',0),'missing':r.get('N',0)-r.get('n',0)} for r in view.get('components',[])] if rows else [])
        attention=view.get('attention',{})
        self.attention_rows={}
        for key,button in self.attention_buttons.items():
            items=attention.get(key,{'count':0})
            self.attention_rows[key]=items
            button.setText(dict((v,k) for k,v in [('미산정 호출','unpriced'),('모델명 불일치','model_mismatch'),('캐시 저하 의심','cache_degradation'),('기록 누락','observation_missing')])[key]+f" {items['count']:,}")
            button.setVisible(bool(items['count']))
        self.attention.setVisible(any(item['count'] for item in self.attention_rows.values()))
        if self.aggregate_selection and self.overview_detail['group'].isVisible():
            selected=self.aggregate_selection
            if '_summary_index' in selected:self.open_summary(selected['_summary_index'],reveal=False)
            else:
                if selected.get('start_ts') is not None:
                    fresh=next((r for r in self.timeline.rows if r.get('start_ts')==selected['start_ts']),None)
                elif selected.get('dimension'):
                    fresh=next((r for r in self.source_bars.rows if r.get('key')==selected.get('key') and r.get('dimension')==selected['dimension']),None)
                else:fresh=next((r for r in self.component_bars.rows if r.get('key')==selected.get('key')),None)
                if fresh:self.select_aggregate(fresh,reveal=False)
                else:self.overview_detail['group'].hide();self.aggregate_selection=None;self.aggregate_records=[]

    def open_summary(self,index,*,reveal=True):
        row=self.view_result['summaries'][index]
        self.select_aggregate(dict(row,label=self.metric_captions[index].text(),
            metric='output_speed' if index==4 else 'cache_ratio' if index==3 else 'cost',
            amount=index not in (3,4),_summary_index=index),reveal=reveal)

    def assumption_text(self,assumptions):
        lines=[]
        for mode,result in assumptions.items():
            value=usd(result['total']) if result['total'] is not None else usd(result['partial_sum'])
            lines.append(f"{mode} 가정 추가액 {value} · 포함 {result['n']:,} / 산정 불가 {result['missing']:,}")
        return '<br>'.join(lines)

    def population_ready(self,message):
        pending=self.pending_population
        if not pending or pending[0]!=message['id'] or pending[3]!=self.current_page:return
        self.pending_population=None
        self.select_aggregate({**pending[1],**message['summary']},reveal=pending[2])

    def select_aggregate(self,row,*,reveal=True):
        if row.get('population') and 'assumptions' not in row:
            self.population_request+=1;self.pending_population=(self.population_request,row,reveal,self.current_page)
            if self.async_mode:
                self.worker.request_population(self.population_request,row['population'],row.get('assumption_population'))
            else:
                from .dashboard_views import population_summary
                self.population_ready(dict(id=self.population_request,summary=population_summary(self.engine,row['population'],row.get('assumption_population'))))
            return
        if self.current_page==1:self.comparison_selection={'id':row.get('id'),'matrix_label':row.get('matrix_label')}
        self.aggregate_selection=dict(row);panel=self.comparison_detail if self.current_page==1 else self.overview_detail
        self.aggregate_records=row.get('population')
        panel['title'].setText(row.get('label',row.get('id','선택 상세')))
        count=row.get('population_count',row.get('N',0))
        n=row.get('known',row.get('n',count));N=row.get('N',row.get('calls',count));value=row.get('value',row.get('total'))
        metric=row.get('metric',self.comparison_metric.currentData() if self.current_page==1 else self.overview_metric.currentData())
        lines=[value_text(value,metric),f'유효 {n:,} / 대상 {N:,}',row.get('extra','')]
        if row.get('start_ts') is not None:lines.append(date_time(row['start_ts'])+' ≤ 기록 시각 < '+date_time(row['end_ts']))
        if row.get('exclusions'):lines.append(' · '.join(f'{k} {v:,}' for k,v in row['exclusions'].items()))
        if row.get('request_exclusions'):lines.append('요청 제외 · '+' · '.join(f'{k} {v:,}' for k,v in row['request_exclusions'].items()))
        panel['text'].setText('<br>'.join(escape(str(x)) for x in lines if x));panel['group'].show()
        panel['records'].setEnabled(bool(self.aggregate_records) and bool(count));panel['apply_band'].setVisible(bool(row.get('input_band')))
        assumptions=row.get('assumptions',{})
        amount=row.get('amount',metric=='cost') and any(v.get('N') for v in assumptions.values())
        panel['assumptions'].setVisible(amount)
        if amount:panel['assumptions'].body.setText(self.assumption_text(assumptions))
        if reveal:self.scrollers[self.current_page].ensureWidgetVisible(panel['group'])
        self.update_navigation()

    def open_aggregate_records(self):
        if not self.aggregate_records:return
        self.push_state();self.temporary_context={'population':self.aggregate_records,'label':'선택 집계의 호출','home':'','sid':''}
        self.selected_session=None;self.selected_turn=None;self.selected_call=None;self.record_view='calls';self.search.clear();self.change_page(2)

    def open_attention(self,key):
        self.aggregate_records=self.attention_rows[key].get('population');self.open_aggregate_records()

    def apply_selected_band(self):
        band=(self.aggregate_selection or {}).get('input_band')
        if band:
            value=':'.join(str(int(x)) if x!=float('inf') else 'inf' for x in band);choose(self.band,value);self.render()

    def refresh_target_editors(self):
        if not hasattr(self,'compare_model'):return
        old=self.restoring;self.restoring=True
        current=self.compare_model.currentData()
        values=[m for m in self.filter_values.get('model',[]) if supported_model(m)]
        if supported_model(current) and current not in values:values.append(current)
        if current not in values:current=None
        if not current:current='gpt-6-astra' if 'gpt-6-astra' in values else (values[0] if values else '')
        if [self.compare_model.itemData(i) for i in range(self.compare_model.count())]!=values:
            self.compare_model.clear()
            for value in values:self.compare_model.addItem(value if recorded(value) else '모델 기록 없음',value)
        choose(self.compare_model,current)
        mode=self.compare_type.currentData()=='mode'
        for key,tab in self.comparison_tabs.items():tab.setChecked(key==self.compare_type.currentData())
        self.comparison_mode.setVisible(mode)
        if not mode:choose(self.comparison_mode,'observed')
        self.apply_type_constraints()
        self.restoring=old

    def observed_targets(self):
        keys=('model','effort','service_tier');seen=set();result=[]
        for observed in getattr(self,'combinations',[]):
            item={k:observed.get(k) or '미확인' for k in keys};key=tuple(item.values())
            if key not in seen:result.append(item);seen.add(key)
        return result

    def apply_type_constraints(self):
        model=self.compare_model.currentData();mode=self.compare_type.currentData()=='mode'
        candidates=[t for t in self.observed_targets() if t['model']==model]
        efforts=sorted({t['effort'] for t in candidates},key=effort_key)
        self.targets=[]
        for effort in efforts:
            for tier in (('Standard','Fast') if mode else ('Standard',)):
                label=effort if recorded(effort) else '추론 설정 기록 없음'
                self.targets.append(dict(id=label+' · '+tier if mode else label,model=model,effort=effort,service_tier=tier))
        if self.baseline not in {t['id'] for t in self.targets}:self.baseline=self.targets[0]['id'] if self.targets else ''
        self.comparison_edited=True

    def select_compare_type(self,value):
        choose(self.compare_type,value);self.comparison_type_changed()

    def comparison_type_changed(self,*_):
        if self.restoring:return
        self.close_aggregate(self.comparison_detail['group'])
        self.refresh_target_editors();self.comparison_metric_changed();self.update_navigation()

    def comparison_metric_changed(self,*_):
        if self.restoring:return
        self.restoring=True
        unit=self.unit.currentData();current=self.comparison_metric.currentData();self.comparison_metric.clear()
        items=[('비용','cost'),('호출 소요시간' if unit=='response' else '요청 경과시간','duration'),
            ('입력 토큰' if unit=='response' else '입력 토큰 합계','input'),('출력' if unit=='response' else '출력 합계','output'),('추론 토큰' if unit=='response' else '추론 토큰 합계','reasoning'),('캐시 적중률','cache_ratio')]
        if unit=='turn':items.append(('요청당 호출 수','responses'))
        for text,key in items:self.comparison_metric.addItem(text,key)
        choose(self.comparison_metric,current)
        if self.comparison_mode.currentData()=='repricing':choose(self.comparison_metric,'cost');choose(self.result_view,'distribution')
        self.comparison_metric.setEnabled(self.comparison_mode.currentData()!='repricing');self.result_view.setEnabled(self.comparison_mode.currentData()!='repricing')
        self.sync_comparison_labels()
        self.band.setAccessibleName('시작 입력 길이' if unit=='turn' else '입력 길이');self.cache_band.setAccessibleName('시작 캐시 적중률' if unit=='turn' else '캐시 적중률')
        self.restoring=False;self.render()

    def sync_comparison_labels(self):
        weighted=self.comparison_metric.currentData()=='cache_ratio';current=self.method.currentData()
        if (current=='weighted')!=weighted:
            self.method.blockSignals(True);self.method.clear()
            for title,key in ([('입력 가중','weighted')] if weighted else [('평균','mean'),('중앙값','median'),('P90','p90')]):self.method.addItem(title,key)
            choose(self.method,'weighted' if weighted else 'mean');self.method.blockSignals(False)
        self.method.setEnabled(not weighted)
        request=self.unit.currentData()=='turn'
        self.band.setItemText(0,'전체 시작 입력 길이' if request else '전체 입력 길이')
        self.cache_band.setItemText(0,'전체 시작 캐시 적중률' if request else '전체 캐시 적중률')
        self.matrix_by.setItemText(0,'시작 입력 길이' if request else '입력 길이');self.matrix_by.setItemText(1,'시작 캐시 적중률' if request else '캐시 적중률')

    def render_comparison(self,*_):
        if self.restoring or not self.view_result or self.current_page!=1:return
        self.sync_comparison_labels()
        comparison=self.view_result.get('comparison',{});groups=comparison.get('groups',[])
        if self.comparison_mode.currentData()=='repricing':
            groups=comparison.get('repricing_groups',[])
        self.comparison_unknown_rows=self.view_result['unknown_population']
        self.comparison_unknown.setVisible(bool(self.comparison_unknown_rows['count']))
        self.comparison_unknown.setText(f'모드 기록 없음 {self.comparison_unknown_rows['count']:,}호출 · 별도 기록 보기')
        self.compare_groups=groups;plot=[];lines=[];metric=self.comparison_metric.currentData();view=self.result_view.currentData()
        self.comparison_note.setText(('동일 토큰 환산 · 같은 표본의 Standard/Fast 단가 적용' if self.comparison_mode.currentData()=='repricing' else '같은 추론 설정의 Standard · Fast 분포' if self.compare_type.currentData()=='mode' else 'Standard · 기록된 모든 추론 설정') if self.targets else '선택한 모델의 기록이 없습니다')
        self.update_navigation()
        for i,group in enumerate(groups):
            item={**group.get('stats',{}),**group};item.setdefault('id',chr(65+i));item.setdefault('N',item.get('n',0)+item.get('missing',0));item['target_index']=(1 if item.get('service_tier')=='Fast' else 0) if self.compare_type.currentData()=='mode' else i
            item['value']=item.get('value',item.get('mean'));item['suppressed']=metric!='cache_ratio' and self.method.currentData()=='p90' and item.get('n',0)<10
            if view=='scatter':
                samples=group.get('scatter',[])
                if isinstance(samples,dict):samples=samples.get('rows',[])
                for sample in samples:
                    x=sample.get('duration');x=x*1000 if x is not None else sample.get('completion_latency_ms',sample.get('elapsed_ms'))
                    plot.append({**sample,'x':x,'y':sample.get('cost'),'id':item['id'],'target_index':item['target_index']})
            else:plot.append(item)
            delta=item.get('delta');relative=item.get('relative');delta_text=value_text(delta,metric)
            if metric=='cache_ratio' and delta is not None:delta_text=f'{delta:+.1f}%p'
            lines.append(f"{item['id']} · 기준 대비 {delta_text}"+(f' · 상대 차이 {relative:+.1f}%' if relative is not None else ''))
        self.comparison_chart.view=view;self.comparison_chart.metric=metric
        self.comparison_chart.setFixedHeight(320 if view=='scatter' else max(112,len(plot)*64+40));self.comparison_chart.set_rows(plot)
        self.difference_note.setVisible(view!='difference')
        self.difference_note.setText('\n'.join(lines) if view=='difference' else ('개별 '+('요청' if self.unit.currentData()=='turn' else '호출')+' 캐시율 분포 · 대표값은 입력 가중\n' if metric=='cache_ratio' else '')+'P25–P75 상자 · P10–P90 구간 · 중앙값 선 · 선택 대표값 점' if view=='distribution' else f'환산액·시간이 모두 확인된 교집합 {len(plot):,}표본')
        matrix=[{**cell,'id':row['id'],'input_band':cell.get('band')} for row in comparison.get('matrix',[]) for cell in row.get('cells',[])]
        labels=list(dict.fromkeys(cell.get('label','미확인') for cell in matrix))
        self.matrix_rows=[dict(id=target['id'],cells={c['label']:c for c in matrix if c['id']==target['id']}) for target in self.targets]
        maximum=max((c['value'] for c in matrix if c.get('value') is not None),default=0)
        for row in self.matrix_rows:row['bars']=[None]+[(row['cells'].get(name,{}).get('value') or 0)/maximum if maximum else 0 for name in labels]
        self.matrix_labels=labels;self.matrix.setHorizontalHeaderLabels(['대상']+labels)
        self.matrix.put(rowHeight=64,leftColumns=[0],noElideColumns=list(range(len(labels)+1)),dataBars=True)
        self.matrix.setColumnWidth(0,230)
        for column in range(1,len(labels)+1):self.matrix.setColumnWidth(column,170)
        def matrix_cell(row,column,role):
            if column==0:return row['id']
            cell=row['cells'].get(labels[column-1],{});value=value_text(cell.get('value'),metric)
            return value+f"\n유효 {cell.get('n',0):,} / 대상 {cell.get('N',0):,}"
        self.matrix.set_rows(self.matrix_rows,matrix_cell);self.matrix.setFixedHeight(64+64*len(self.matrix_rows))
        observed=self.comparison_mode.currentData()!='repricing'
        self.condition_details.setVisible(observed)
        self.budget_details.setVisible(observed and metric=='cost');self.decomposition_details.setVisible(observed and self.unit.currentData()=='turn' and metric=='cost')
        budgets=[];decompositions=[]
        for i,group in enumerate(groups):
            budget=group.get('mean_budget',{});decomp=group.get('decomposition',{})
            if budget:
                from .analytics import COMPONENT_LABELS
                from .pricing import COST_COMPONENTS
                values=[f'{name} {usd(budget.get(key))}' for key,name in zip(COST_COMPONENTS,COMPONENT_LABELS)]
                values.append(f"평균 합계 {usd(budget.get('cost'))} · 유효 {budget['n']:,} / 대상 {budget['N']:,}")
                budgets.append((group.get('id',chr(65+i)),' · '.join(values)))
            if decomp:
                text=f"{usd(decomp['request_mean'])} = 호출 평균 {usd(decomp['call_mean'])} × 요청당 {number(decomp['calls_per_request'],2)}호출"
                text+=f" · 동일 {decomp['requests']:,}요청 / {decomp['calls']:,}호출"
                decompositions.append((group.get('id',chr(65+i)),text))
        self.budget_details.set_sections(budgets);self.decomposition_details.set_sections(decompositions)
        selection=getattr(self,'comparison_selection',None)
        if selection:
            fresh=next((row for row in plot if row.get('id')==selection['id']),None)
            if selection.get('matrix_label'):
                matrix_row=next((row for row in self.matrix_rows if row['id']==selection['id']),None)
                cell=matrix_row['cells'].get(selection['matrix_label']) if matrix_row else None
                fresh={**cell,**selection,'label':selection['id']+' · '+selection['matrix_label']} if cell else None
            if fresh:self.select_aggregate(fresh,reveal=False)
            else:self.close_aggregate(self.comparison_detail['group'])


    def open_unknown_comparison(self):
        self.aggregate_records=self.comparison_unknown_rows['population'];self.open_aggregate_records()

    def select_comparison(self,row):
        if row.get('kind')=='scatter_cell':
            self.select_scatter(row['records'],'산점도 선택 구간');return
        if row.get('home') and row.get('sid'):
            self.select_scatter([row],('요청 '+str(row.get('turn',''))) if row.get('sample_unit')=='turn' else '호출 '+str(call_id(row)))
            return
        self.select_aggregate({**row,'label':row.get('id','대상'),'metric':self.comparison_metric.currentData()});self.update_navigation()

    def select_scatter(self,records,title):
        pieces=['소요시간 '+value_text(stats(records,'duration')['mean'],'duration'),
                '입력 '+number(stats(records,'input')['mean'],1),'출력 '+number(stats(records,'output')['mean'],1),
                '캐시 적중률 '+value_text(stats(records,'rate')['value'],'cache_ratio')]
        for key,name in (('cwd','프로젝트'),('source','작업 종류')):
            values={str(r.get(key) or r.get('project') or '미확인') for r in records}
            pieces.append(name+' · '+(next(iter(values)) if len(values)==1 else str(len(values))+'개'))
        self.select_aggregate(dict(label=title,population={'union':[r['population'] for r in records]},population_count=len(records),n=len(records),N=len(records),value=stats(records,'cost')['mean'],metric='cost',extra=' · '.join(pieces)))
    def select_matrix(self,index,column=1,*_):
        if 0<=index<len(self.matrix_rows) and 1<=column<=len(self.matrix_labels):
            row=self.matrix_rows[index];cell=row['cells'].get(self.matrix_labels[column-1])
            if cell:self.select_aggregate({**cell,'id':row['id'],'matrix_label':cell['label'],'label':row['id']+' · '+cell['label']})

    def matches_call_filter(self,row,key):
        status=observation(row)
        if key=='unpriced':return row.get('cost') is None
        if key=='unknown_mode':return request_tier(row)=='미확인'
        if key=='model_mismatch':return bool(row.get('model_alert_confirmed')) or status=='모델명 불일치'
        if key=='observation_missing':return observation_flags(row)['missing']
        if key=='observation_conflict':return observation_flags(row)['conflict']
        if key=='observation_problem':return self.matches_call_filter(row,'observation_missing') or self.matches_call_filter(row,'observation_conflict')
        if key=='cache_zero':return type(row.get('input')) is int and row['input']>0 and type(row.get('cached')) is int and row['cached']==0
        if key=='cache_degradation':return bool(row.get('cache_degradation') or row.get('cache_incident_id'))
        if key=='http':return row.get('transport')=='HTTP/SSE'
        return False

    def select_record_view(self,index):
        if self.record_view_choice.currentIndex()==index:
            self.record_tabs[index].setChecked(True);return
        self.push_state();self.record_view_choice.setCurrentIndex(index)

    def record_view_changed(self,*_):
        if self.restoring:return
        self.record_view=self.record_view_choice.currentData();self.selected_turn=None;self.selected_call=None;self.selected_event=None;self.exact_record=None;self.render_explorer()

    def call_filter_changed(self,checked):
        if self.restoring:return
        # Filters narrow the current level without changing the route.
        self.render_explorer()

    def sync_history_filters(self):
        show=self.current_page!=2 or self.record_filters.toggle.isChecked()
        for node in (self.home,self.source,self.archive,self.model,self.effort,self.mode):
            node.setVisible(show and self.current_page<3)

    def render_history_path(self,data):
        entries=[dict(title='프로젝트',action='root')]+[dict(item,action='scope') for item in data.get('path',[])]
        if self.selected_turn:entries.append(dict(title='요청',action='request'))
        if self.selected_call:entries.append(dict(title='호출 상세',action='leaf'))
        buttons=getattr(self,'_history_path_buttons',[])
        while len(buttons)<len(entries):
            button=Button();button.setMaximumWidth(240)
            button.clicked.connect(lambda node=button:self.activate_history_path(node))
            self.history_path.addWidget(button);buttons.append(button)
        self._history_path_buttons=buttons
        for i,button in enumerate(buttons):
            button.setVisible(i<len(entries))
            if i<len(entries):
                button.destination=entries[i];text=('› ' if i else '')+entries[i]['title'];button.setText(Verbatim(text))
                font=QFont();font.setPixelSize(14)
                button.setFixedWidth(min(240,max(72,int(QFontMetricsF(font).horizontalAdvance(text))+28)))
                button.setToolTip(Verbatim(entries[i]['title']))
                button.setEnabled(entries[i]['action']!='leaf')

    def activate_history_path(self,button):
        item=button.destination
        if item['action']=='root':self.go_home()
        elif item['action']=='scope':self.enter_history(item)
        elif item['action']=='request':self.close_record_detail()

    def render_record_parent(self,data):
        self.parent_kind='tree';self.parent_table=self.session_parent_table
        self.parent_stack.setCurrentIndex(0)
        if self.parent_table.model().headers!=['','프로젝트 · 세션']:
            self.parent_table.setHorizontalHeaderLabels(['','프로젝트 · 세션'])
        self.parent_table.put(widths=[32,216],rowHeight=40,leftColumns=[0,1],stretchVisual=1,treeNavigation=True)
        self.parent_rows=self.parent_table.set_window(data['parents'],self.history_tree_cell,('tree',))
        from .history_projection import node_key
        active=node_key('session',*self.selected_session) if self.selected_session else node_key('project',self.history_navigation.project)
        self.parent_table.select_row(next((data['parents']['start']+i for i,r in enumerate(data['parents']['rows']) if r['key']==active),-1))
        self.record_current_title.setText({'projects':'프로젝트','sessions':'세션','children':'하위 세션','requests':'요청','calls':'호출'}.get(self.record_view,''))

    def history_tree_cell(self,row,column,role):
        if column==0:return ('▾' if row.get('expanded') else '▸') if row.get('expandable') else ''
        return '  '*row.get('depth',0)+row['title']

    def activate_record_parent(self,index,column=1,*_):
        if not 0<=index<len(self.parent_rows):return
        row=self.parent_rows[index]
        if row.get('_placeholder'):return
        if column==0:
            if row.get('expandable'):self.history_navigation.toggle(row['key']);self.render_explorer()
            return
        self.enter_history(row)

    def enter_history(self,row):
        self.push_state()
        self.selected_call=self.selected_event=self.selected_turn=None;self.exact_record=None
        self.history_navigation.project=row['project_id']
        from .history_projection import node_key
        self.history_navigation.expanded.add(row.get('key') or (node_key('session',row['home'],row['sid']) if row.get('sid') else node_key('project',row['project_id'])))
        self.selected_session=(row['home'],row['sid']) if row.get('sid') else None
        self.record_view='requests' if self.selected_session else 'sessions'
        choose(self.sort,'time_asc' if self.selected_session else 'time_desc')
        self.table.verticalScrollBar().setValue(0)
        self.render_explorer()

    def use_history_table(self):
        kind=self.record_view if self.record_view in self.history_tables else 'projects'
        self.table=self.history_tables[kind]
        self.history_table_stack.setCurrentIndex(list(self.history_tables).index(kind))

    def render_explorer(self,*_):
        if self.restoring or self.current_page!=2:return
        self.render()

    def apply_explorer(self):
        data=self.view_result['explorer'];ctx=self.temporary_context or {}
        self.selected_session=tuple(data['session']) if data['session'] else None
        self.selected_turn=data['turn'];self.record_view=data['view'];self.record_status=''
        self.use_history_table()
        session=data['metadata'];records=data['records']['rows']
        self.render_history_path(data)
        self.table.put(highlightZeroCache=self.record_view=='calls',navigationColumn=0)
        summary=data.get('summary',{})
        self.history_summary.setText(f"환산액 {'확인분 ' if summary.get('partial') and summary.get('cost') is not None else ''}{usd(summary.get('cost'))} / 호출 {summary.get('calls',0):,} / 캐시 {value_text(summary.get('cache_ratio'),'cache_ratio')}\n입력 {number(summary.get('input'))} / 출력 {number(summary.get('output'))} / 구독 가치 기준 / 청구액 아님")
        self.update_navigation()
        self.outside.setVisible(self.record_view=='calls');self.call_columns_row.setVisible(self.record_view=='calls')
        self.record_filters.toggle.setText('필터·표시 항목')
        self.session_scope.setVisible(bool(session));self.sort.setVisible(True)
        current=self.record_view
        wide=self.records_body.available_width>=1800
        self.restoring=True
        items=[('요청','requests'),(f"하위 세션({data.get('child_count',0)})",'children')] if self.selected_session else [('프로젝트','projects'),('세션','sessions')]
        if [(self.record_view_choice.itemText(i),self.record_view_choice.itemData(i)) for i in range(self.record_view_choice.count())]!=items:
            self.record_view_choice.clear()
            for title,key in items:self.record_view_choice.addItem(title,key)
        choose(self.record_view_choice,current)
        for i,tab in enumerate(self.record_tabs):
            tab.setVisible(bool(self.selected_session) and self.record_view in ('requests','children') and (i==0 or data.get('child_count',0)>0));tab.setText(self.record_view_choice.itemText(i));tab.setChecked(i==self.record_view_choice.currentIndex())
        self.restoring=False
        self.sync_history_filters()
        if not session:
            summary=data.get('summary',{})
            self.session_scope.setText(f"환산액 {usd(summary.get('cost'))} / {summary.get('calls',0):,}호출 / 구독 가치 기준 / 청구액 아님")
        residual=(session or {}).get('unclassified');self.residual_details.setVisible(bool(residual))
        if residual:self.residual_details.set_sections([('별도 누계', ' · '.join(f'{name} {number(residual.get(key))}' for key,name in (('input','입력'),('cached','캐시 읽기'),('written','캐시 쓰기'),('output','출력'),('reasoning','추론'),('total','Total')))),('집계','호출·요청 통계에 포함하지 않음')])
        if session:
            group=data['group']
            cost=('확인분 ' if group['partial'] and group['cost'] is not None else '')+usd(group['cost'])
            detail=(f" · 자체 {usd(group['own_cost'])} + 하위 {usd(group['child_cost'])}"
                    if group['descendants'] else '')
            project=session.get('project_name') or Path(session.get('cwd','')).name or tr('프로젝트 없음')
            count=('산정 ' if group['priced']<group['calls'] else '')+call_count(group['priced'],group['calls'])+'호출'
            self.session_scope.setText(Verbatim(project+' · '+tr(self.period.currentText() if not ctx else '전체 기록')+'\n'+tr(f"비용 {cost}{detail} · {count}")))
        if self.record_view in ('projects','sessions','children'):
            headers=['프로젝트' if self.record_view=='projects' else '세션','요청 / 호출','환산액 · 하위 포함','캐시 적중률','최근 활동'];widths=[260,110,140,110,155]
            self.table.put(leftColumns=[0,4],rowHeight=56)
            if wide:headers+=['입력','출력'];widths+=[110,110]
            def formatter(r,c,role,view=self.record_view):
                title=r['title']
                if view=='projects':title+=f"\n작업 {r['roots']} · 하위 {r['children']}"
                elif r.get('descendants'):title+=f"\n하위 {r['descendants']}"
                return [Verbatim(title),f"{r.get('requests',0)} / {r.get('calls',0)}",('확인분 ' if r.get('partial') and r.get('cost') is not None else '')+usd(r.get('cost')),value_text(r.get('cache_ratio'),'cache_ratio'),date_time(r.get('ts')),number(r.get('input')),number(r.get('output'))][c]
        elif self.record_view=='requests':
            headers=['요청 · 시작','상태','소요시간','선택 / 전체 호출','환산액'];widths=[260,110,110,130,140];self.table.put(leftColumns=[0,1],rowHeight=48)
            if wide:headers+=['완료 시각','입력 / 출력','캐시 적중률'];widths+=[155,130,100]
            def formatter(r,c,role):
                whole=r.get('total_responses',r.get('responses',0))
                return [f"요청 {r.get('ordinal') or '미연결'} · "+date_time(r.get('started_at')),r.get('state') or '완료 기록 없음',value_text(r.get('duration'),'duration'),f"{r.get('responses',0):,} / {whole:,}",usd(r.get('cost')),date_time(r.get('ended_at')),number(r.get('input'))+' / '+number(r.get('output')),value_text(r.get('rate'),'cache_ratio')][c]
        else:
            self.table.put(leftColumns=[0,1],rowHeight=56)
            available=set(data['available'])
            for key,node in self.extra_column_controls.items():node.setVisible(key in available and key not in {k for k,_ in CALL_COLUMNS})
            self.active_columns=CALL_COLUMNS+[c for c in EXTRA_COLUMNS if c[0] not in {key for key,_ in CALL_COLUMNS} and c[0] in available and (self.extra_column_controls[c[0]].isChecked() or wide and c[0]=='output_speed')]
            headers=[title for key,title in self.active_columns];widths=[155,220,100,95,95,105,95]+[150]*(len(headers)-len(CALL_COLUMNS))
            formatter=self.response_cell
        if headers!=self.table.model().headers:
            self.table.setHorizontalHeaderLabels(headers)
            self.table.put(widths=widths)
        self.table.put(columnStretchWeights=[3]+[1]*(len(headers)-1))
        numeric={'환산액','환산액 · 하위 포함','소요시간','비용 · 하위 포함','비용','평균 호출 비용','호출 수','산정 / 전체 호출','캐시 적중률','선택 / 전체 호출','입력','캐시 읽기','캐시 쓰기','출력','추론','추론 외','평균 출력 속도'}
        self.table.put(noElideColumns=[i for i,title in enumerate(headers) if title in numeric])
        self.record_rows=self.table.set_window(data['records'],formatter,(self.record_view,tuple(headers)))
        self.render_record_parent(data)
        self.update_navigation()
        if ctx and ctx.get('sid') and session is None:self.record_status='대상 기록을 찾을 수 없음'
        elif not records:self.record_status='조건에 맞는 기록 없음' if self.snapshot['sessions'] else '사용 기록 없음'
        if self.selected_event:self.render_event_detail()
        elif self.selected_call:
            self.exact_record=data['detail']
            if self.exact_record:self.render_record_detail(self.exact_record)
            else:self.request_exact_record()
            if self.exact_record and not data['detail_in_scope']:self.record_status='기록 갱신으로 현재 조건에서 제외됨'
        else:self.detail_scroll.hide()
        self.record_message.setText(self.record_status);self.layout_record_detail()

    def response_cell(self,row,column,role):
        key=self.active_columns[column][0];value=row.get(key)
        if role==Qt.ToolTipRole:
            if key=='cost' and value is None:return '환산 제외 · '+price_reason(row)
            if key=='service_tier' and not recorded(request_tier(row)):return '이 호출의 요청 모드가 로컬 기록에 저장되지 않았습니다.'
            if key in ('response_model','response_service_tier','transport') and not recorded(value):return '이 호출에는 해당 정보가 기록되지 않았습니다.'
            if key=='model':
                basis='실제 요청 기록' if row.get('model_source')=='wire' else '당시 로컬 요청 설정' if row.get('model_source')=='settings' else '모델 기록 없음'
                return '\n'.join(x for x in [basis,model_comparison(row),*record_issues(row)] if x)
        if key=='model':
            model=str(value or '—')+(f" ({row['model_match']})" if model_comparison(row) else '')
            return Verbatim(model+'\n'+str(row.get('effort') or '—')+' · '+request_tier(row))
        if key=='ts':return date_time(value)
        if key=='cost':return usd(value) if value is not None else '미산정'
        if key=='cache_ratio':return value_text(cache_ratio(row),'cache_ratio')
        if key=='completion_latency_ms':return value_text(row.get('duration'),'duration')
        if key=='output_speed':return value_text(value,'output_speed') if value is not None else '측정 불가'
        if key=='observation':return ((row['_incident_phase']+' · ') if row.get('_incident_phase') else '')+('범위 밖 · ' if row.get('_outside') else '')+('캐시 읽기 0 · ' if self.matches_call_filter(row,'cache_zero') else '')+observation(row)
        if key=='service_tier':return request_tier(row) if recorded(request_tier(row)) else '—'
        if key=='transport':return observed_transport(row) or ('동시간대 통신 로그: '+value if value in ('WebSocket','HTTP/SSE') and row.get('transport_source')!='conflict' else '—')
        if key in ('input','cached','written','output','reasoning'):return number(value)
        return Verbatim(value) if recorded(value) else '—'

    def activate_record(self,index,*_):
        if not 0<=index<len(self.record_rows):return
        row=self.record_rows[index]
        if row.get('_placeholder'):return
        if self.record_view in ('projects','sessions','children'):
            self.enter_history(row);return
        if self.record_view=='sessions':
            self.push_state();self.selected_session=(row['home'],row['sid']);self.selected_turn=None;self.record_view='requests';self.render_explorer()
        elif self.record_view=='requests':
            self.push_state();self.selected_turn=row.get('turn');self.record_view='calls';self.render_explorer()
        else:
            if not self.selected_call:self.push_state()
            self.selected_call=call_id(row);self.selected_call_scope=(row['home'],row['sid']);self.selected_event=None;self.record_section='identity';self.exact_record=row
            self.render_record_detail(row);self.layout_record_detail()

    def open_record(self,row):
        from .overlay_navigation import NavigationTarget
        self.navigate(NavigationTarget(row['home'],row['sid'],view='calls',request_id=row.get('turn'),call_id=call_id(row)))

    def navigate(self,target):
        from .overlay_navigation import NavigationTarget
        target=NavigationTarget.from_value(target);signature=target.as_dict()
        destination_page=4 if target.tab=='settings' else 2
        if (signature==self.navigation_signature and self.current_page==destination_page and self.temporary_context==signature
            and self.selected_call==target.call_id and self.selected_event==target.event_id and not self.search.text()
            and not any(node.isChecked() for node in self.call_filter_controls.values())):
            self.show_window();return
        self.push_state();self.navigation_signature=signature
        self.temporary_context=signature;self.selected_session=(target.home,target.sid);self.selected_turn=target.request_id
        self.selected_call=target.call_id;self.selected_event=target.event_id;self.record_section=target.section
        self.selected_call_scope=(target.home,target.sid)
        self.record_view='requests' if target.view=='requests' else 'calls';self.exact_record=None;self.record_request+=1;self.detail_scroll.hide()
        self.restoring=True;self.search.clear();choose(self.sort,target.sort)
        for node in self.call_filter_controls.values():node.setChecked(False)
        self.outside.setChecked(False);self.restoring=False
        if target.tab=='settings':
            self.change_page(4);self.settings_page.reveal('troubleshooting',self.diagnostic_details)
        else:
            self.record_message.setText('대상 기록을 읽는 중 · '+target.sid);self.change_page(2)
            if target.call_id:self.request_exact_record()
        self.show_window()

    def request_exact_record(self):
        scope=self.selected_call_scope or self.selected_session
        if not scope or not self.selected_call:return
        self.record_request+=1;identity_value=(*scope,self.selected_call)
        if self.async_mode and self.worker:self.worker.request_record(self.record_request,identity_value)
        else:self.record_ready({'id':self.record_request,'row':self.engine.record(*identity_value)})

    def record_ready(self,value):
        if value['id']!=self.record_request:return
        self.exact_record=value.get('row')
        if self.exact_record:
            self.render_record_detail(self.exact_record)
            self.record_message.setText('' if any(identity(r)==identity(self.exact_record) for r in self.record_rows) else '기록 갱신으로 현재 조건에서 제외됨')
        else:
            self.record_status='대상 기록을 찾을 수 없음';self.record_message.setText(self.record_status);self.detail_scroll.hide()
        self.layout_record_detail()

    def render_record_detail(self,row):
        from .core import token_parts
        def entries(values, literal=False):
            text='\n'.join(f'{tr(name) if literal else name}  {value}' for name,value in values if recorded(value))
            return Verbatim(text) if literal else text
        section={key:'' for key in self.detail_sections}
        section['identity']=entries([('작업',row.get('title')),('프로젝트',row.get('project_name')),
            ('기록 시각',date_time(row['ts']) if row.get('ts') is not None else None),
            ('세션 ID',row.get('sid')),('요청 ID',row.get('turn')),('호출 ID',call_id(row))],literal=True)
        parent=next((session for session in self.snapshot.get('sessions',[]) if session['id']==row.get('parent_thread_id') and session['home']==row['home']),None)
        if parent:section['identity']=Verbatim(section['identity']+'\n'+tr('상위 작업')+'  '+parent['title'])
        mode=request_tier(row)
        mode_source={'applied_settings':'당시 적용 설정','turn_context':'로컬 턴 기록','wire':'실제 요청'}.get(row.get('request_mode_source'))
        conditions=[('요청 모델',row.get('model')),('추론 설정',row.get('effort')),
                    ('요청 모드',mode if recorded(mode) else None)]
        if mode_source and recorded(mode):conditions.append(('모드 근거',mode_source))
        for key,name in [('requested_model','요청 모델'),('response_model','응답 모델')]:
            if recorded(row.get(key)) and row[key]!=row.get('model'):conditions.append((name,row[key]))
        if model_comparison(row):conditions.append(('모델 일치성',row['model_match']))
        if recorded(row.get('response_service_tier')):conditions.append(('응답 등급',row['response_service_tier']))
        if observed_transport(row):conditions.append(('통신 방식',observed_transport(row)))
        if recorded(row.get('cache_policy')):conditions.append(('캐시 정책',row['cache_policy']))
        section['conditions']=entries(conditions)
        parts=token_parts(row)
        for chart,key in ((self.input_composition,'input'),(self.output_composition,'output')):
            chart.setVisible(parts[key].get('total') is not None)
            chart.set_parts(parts[key])
        lines=[entries([('입력',number(row['input']) if row.get('input') is not None else None),
            ('출력',number(row['output']) if row.get('output') is not None else None),
            ('캐시 적중률',value_text(cache_ratio(row),'cache_ratio') if cache_ratio(row) is not None else None)])]
        if row.get('reported_total') is not None and row['reported_total']!=row.get('total'):lines.append(f"총량 관측 차이 · 보고 총량 {number(row['reported_total'])}")
        section['usage']='\n'.join(lines)
        section['usage']+='\n평균 출력 속도  '+(value_text(row['output_speed'],'output_speed') if row.get('output_speed') is not None else '측정 불가')
        if row.get('output_speed') is not None:
            section['time']=entries([('호출 소요시간',value_text(row.get('duration'),'duration'))])+'\n출력 / 요청부터 완료까지 · 대기·통신 포함'
        price_model=row.get('price_model',row.get('model'));rate=(FAST_RATES if request_tier(row)=='Fast' else RATES if request_tier(row)=='Standard' else {}).get(price_model)
        lines=[VERIFIED+' 기준 / 구독 가치 환산',entries([('가격 모델',price_model)])]
        if rate:
            for label_,tokens,price in [('일반 입력',row.get('ordinary_input'),rate.input),('캐시 읽기',row.get('cached'),rate.cached),('캐시 쓰기',row.get('written'),rate.written if rate.written is not None else rate.input),('출력',row.get('output'),rate.output)]:
                if tokens is not None and price is not None:lines.append(f'{label_}  {number(tokens)} × {usd(price)} / 1,000,000')
        if row.get('cost') is not None:lines.append('비용  '+usd(row['cost']))
        else:lines=['환산 제외 · '+price_reason(row)]
        section['pricing']='\n'.join(x for x in lines if x)
        section['evidence']='\n'.join(record_issues(row))
        for key,text in section.items():
            self.detail_sections[key][1].setText(text)
            self.detail_sections[key][0].setVisible(bool(text) or key=='usage' or key=='evidence' and bool(row.get('cache_incident_id')))
        self.incident_button.setVisible(bool(row.get('cache_incident_id')))
        self.detail_scroll.show();self.layout_record_detail()
        if getattr(self,'_revealed_call',None)!=(identity(row),self.record_section):
            self._revealed_call=(identity(row),self.record_section)
            target=self.detail_sections.get(self.record_section,self.detail_sections['identity'])[0]
            if not target.isVisible():target=self.detail_sections['conditions'][0]
            self.detail_scroll.ensureWidgetVisible(target)

    def render_event_detail(self):
        self.input_composition.hide();self.output_composition.hide();self.incident_button.hide()
        session=next((s for s in self.analysis['sessions'] if (s['home'],s['id'])==self.selected_session),None)
        events=(session or {}).get('cache_health',{}).get('events',[])
        event=next((e for e in events if str(e.get('id'))==str(self.selected_event)),None)
        if event is None:self.record_status='대상 기록을 찾을 수 없음';self.detail_scroll.hide();return
        for body,text in self.detail_sections.values():body.hide();text.setText('')
        self.detail_sections['identity'][1].setText('캐시 사건 '+str(event['id']))
        segment=event.get('segment',());names=('분석 모델','추론 설정','요청 모드','캐시 정책')
        self.detail_sections['conditions'][1].setText('\n'.join(f'{name}  {value}' for name,value in zip(names,segment) if recorded(value)))
        fields=[]
        fields.append('기준 캐시 적중률  '+value_text(event.get('baseline_rate',0)*100,'cache_ratio'))
        fields.append('기준 캐시 읽기 평균  '+number(event.get('baseline_read'),1))
        for label_,keys in [('기준 호출',event.get('baseline_keys',[])),('발생 호출',event.get('occurrence_keys',[])),('회복 호출',event.get('recovery_keys',[]))]:
            if not keys:continue
            fields.append(label_+' · '+', '.join(map(str,keys)))
            for key in keys:
                row=next((r for r in self.analysis['responses'] if (r['home'],r['sid'])==self.selected_session and str(call_id(r))==str(key)),None)
                if row:fields.append(f"{key} · 입력 {number(row.get('input'))} · 읽기 {number(row.get('cached'))} · {value_text(cache_ratio(row),'cache_ratio')}")
        fields.append('회복' if event.get('resolved') else '미해결')
        self.detail_sections['evidence'][1].setText('\n'.join(fields))
        self.detail_sections['time'][1].setText('\n'.join(name+'  '+date_time(event[key]) for name,key in [('발생','ts'),('최근 저하','updated_at'),('회복','ended_at')] if event.get(key) is not None))
        for body,text in self.detail_sections.values():body.setVisible(bool(text.text()))
        self.detail_scroll.show();self.detail_scroll.ensureWidgetVisible(self.detail_sections['evidence'][0])

    def open_call_incident(self):
        row=self.exact_record
        if row and row.get('cache_incident_id'):
            from .overlay_navigation import NavigationTarget
            self.navigate(NavigationTarget(row['home'],row['sid'],view='incident',event_id=row['cache_incident_id'],section='evidence'))

    def close_record_detail(self):
        if self.back_stack:
            previous=self.back_stack[-1]
            if (previous.get('page')==2 and previous.get('record_view')==self.record_view and not previous.get('selected_call') and not previous.get('selected_event')
                and previous.get('temporary')==self.temporary_context and previous.get('selected_session')==self.selected_session and previous.get('selected_turn')==self.selected_turn):self.back_stack.pop()
        self.selected_call=None;self.selected_event=None;self.exact_record=None;self._revealed_call=None;self.detail_scroll.hide();self.table.show();self.layout_record_detail();self.update_navigation()
    def layout_record_detail(self):
        active=bool(self.selected_call or self.selected_event) and self.detail_scroll.isVisible()
        self.close_record_button.setVisible(active and self.current_page==2)
        wide=self.records_body.available_width>=1800
        if wide!=getattr(self,'_history_wide',False):
            self._history_wide=wide
            if self.current_page==2 and self.view_result and 'explorer' in self.view_result and not self.analysis_pending:QTimer.singleShot(0,self.apply_explorer)
        self.records_body.put(detailed=active,routeToken=str((self.record_view,self.selected_session,self.selected_turn)))
        self.table.setVisible(not active or wide);self.detail_scroll.put(width=-1,minWidth=0,maxWidth=16777215,stretch=1)
        self.record_parent.setVisible(True);self.record_current.setVisible(self.table.isVisible())
    def render_diagnostics(self):
        if not hasattr(self,'diagnostics'):return
        index=self.snapshot.get('index',{});errors=self.snapshot.get('usage_errors',self.snapshot.get('errors',[]))
        complete=self.snapshot.get('usage_collection_complete',self.snapshot.get('usage_complete',index.get('usage_complete',False)))
        last=self.snapshot.get('last_usage_collection_success',self.snapshot.get('last_usage_success'))
        self.diagnostic_summary.setText('사용량 수집 · '+('확인 필요' if errors else '정상' if complete else '수집 중')+'<br>마지막 성공 수집 · '+date_time(last)+'<br>모델 관측 · '+('확인 필요' if self.snapshot.get('model_errors') else '수신 대기' if not self.snapshot['sessions'] else '기록 확인')+'<br>한도 조회 · '+(self.quota_issue or ('수신 대기' if not getattr(self,'live_quota',None) else '정상')))
        text='수집 대상\n'+'\n'.join(self.snapshot.get('homes',[]))+'\n\n'+('수집 오류\n'+'\n'.join(map(str,errors)) if errors else '')
        text+='\n한도 원장 · '+str(self.snapshot.get('ledger_error') or '정상')
        text+='\n자동 승인 검토 · '+str(self.snapshot.get('internal_review_calls',0))+'호출 (작업 집계에서 제외)'
        if self.analysis_errors:text+='\n분석 오류\n'+'\n'.join(self.analysis_errors)
        self.diagnostics.setPlainText(text)

    def show_prices(self):
        dialog=Dialog(self);dialog.setWindowTitle('기준 환산 단가 / '+VERIFIED+' 기준');dialog.resize(980,720)
        body=Column(dialog);body.setContentsMargins(20,20,20,20);body.setSpacing(16);body.addWidget(label(VERIFIED+' 기준 / Standard 기준 단가 / Fast 2.5배 / 청구액 아님','section'))
        prices=table(['가격 모델 / 모드','일반 입력','캐시 읽기','캐시 쓰기','출력'])
        for i,width in enumerate((320,145,145,145,145)):prices.setColumnWidth(i,width)
        rows=[]
        for model in SUPPORTED_MODELS:
            standard=RATES[model]
            for mode,rate in (('Standard',standard),('Fast',FAST_RATES.get(model))):
                if rate:rows.append([model+' / '+mode,usd(rate.input),usd(rate.cached) if rate.cached is not None else '미지원',usd(rate.written) if rate.written is not None else '입력과 동일',usd(rate.output)])
        fill(prices,rows);body.addWidget(prices,1)
        body.addWidget(label('Standard 기준 단가에 구독 소모 배율을 적용합니다. Fast 환산액은 같은 토큰 Standard 대비 2.5배입니다.','muted',True))
        body.addWidget(label('구독 가치 환산은 API 장문 할증을 반영하지 않습니다.','muted',True))
        body.addWidget(label('USD / 100만 토큰 / GPT-5.6 Sol 프로모션 확인 기한 2026-11-21','muted',True))
        body.addWidget(label('가격 별칭: gpt-5.6, gpt-daybreak-blue-latest → gpt-5.6-sol / gpt-5.5-2026-04-23 → gpt-5.5','muted',True))
        buttons=DialogButtons(DialogButtons.Close);buttons.rejected.connect(dialog.reject);body.addWidget(buttons);dialog.open();self.price_dialog=dialog





    def build_settings(self):
        from .settings_page import SettingsPage
        from .controls import Switch
        self.settings_page = SettingsPage(self)
        self.pages.addWidget(self.settings_page)
        from .connection_recovery import target
        manager=target(self.observer_home,self.observer_directory,index=self.index_path)
        if self.model_evidence_path:manager.evidence=Path(self.model_evidence_path)
        self.observer_panel=ObserverPanel(self.observer_home,self.observer_directory,active=self.manage_observer,parent=self,manager=manager)
        self.settings_page.add_widget('integration', self.observer_panel,section='프록시',title='프록시 사용',description='요청 모델과 응답 모델을 비교할 수 있도록 Codex 연결을 중계합니다.',target=self.observer_panel.toggle,aliases='proxy connection')
        self.settings_page.entries[-1]['description']=self.observer_panel.description.text()
        self.settings_page.entries.append(dict(id='connection-details',category='integration',section='프록시',title='연결 정보',description='',widget=self.observer_panel.connection_details,target=self.observer_panel.connection_details.toggle,aliases='connection information'))
        legacy_notifications = self.settings.value('notifications',True,type=bool)
        self.notification_master = Switch()
        self.notification_master.setChecked(self.settings.value('notifications/enabled', legacy_notifications, type=bool))
        self.notification_master.toggled.connect(lambda value: self.settings.setValue('notifications/enabled', value))
        self.settings_page.add_row('notifications', '알림 표시', '알림을 꺼도 연결 상태 확인과 보호 기능은 계속 작동합니다.', self.notification_master,section='전체 알림')
        self.notification_options = {}
        for kind,key,description in (
            ('HTTP 전환','http_fallback','연결 문제가 생긴 뒤 통신 방식이 바뀌었는지 확인할 수 있도록 전환 기록을 알립니다.'),
            ('캐시 저하 의심','cache_drop','캐시 재사용이 줄어든 상황을 확인할 수 있도록 반복 미적중이나 읽기량 감소를 알립니다. 판단 기준은 잠정값입니다.'),
            ('모델명 불일치','model_mismatch','요청한 모델과 응답에 기록된 모델이 다른 경우 알립니다. 완료된 응답에서 차이가 확인된 경우만 알립니다.'),
            ('프록시 장애','proxy_failure','연결 복구가 필요한 상황을 놓치지 않도록 프록시 장애와 자동 보호 정지를 알립니다. 일시적인 지연은 제외합니다.'),
        ):
            setting='notifications/'+key
            option=Switch()
            option.setToolTip(description)
            enabled=self.settings.value(setting,legacy_notifications,type=bool)
            option.setChecked(enabled)
            if not self.settings.contains(setting):self.settings.setValue(setting,enabled)
            option.toggled.connect(lambda value,key=setting:self.settings.setValue(key,value))
            self.notification_options[kind]=option
            self.settings_page.add_row('notifications', kind, description, option,section='알림 종류')
        cache_enabled=lambda *_:self.confirmed_notifications.enable_cache(self.notification_master.isChecked() and self.notification_options['캐시 저하 의심'].isChecked())
        self.notification_master.toggled.connect(cache_enabled)
        self.notification_options['캐시 저하 의심'].toggled.connect(cache_enabled)
        cache_enabled()
        http_enabled=lambda *_:self.confirmed_notifications.enable_http(self.notification_master.isChecked() and self.notification_options['HTTP 전환'].isChecked())
        self.notification_master.toggled.connect(http_enabled);self.notification_options['HTTP 전환'].toggled.connect(http_enabled);http_enabled()
        self.cache_scope=Choice()
        self.cache_scope.addItem('전체 세션','all');self.cache_scope.addItem('현재 선택 세션','selected')
        self.cache_scope.setCurrentIndex(max(0,self.cache_scope.findData(self.settings.value('notifications/cacheScope','all'))))
        self.cache_scope.currentIndexChanged.connect(lambda *_:self.settings.setValue('notifications/cacheScope',self.cache_scope.currentData()))
        self.settings_page.add_row('notifications','캐시 알림 범위','현재 선택 세션으로 좁히면 다른 작업의 캐시 알림은 표시하지 않습니다. 현재 세션을 확인할 수 없을 때도 표시하지 않습니다.',self.cache_scope,section='적용 범위')
        for kind,handler in (('모델명 불일치',self.confirmed_notifications.enable_model),
                             ('프록시 장애',self.confirmed_notifications.enable_proxy)):
            handler(self.notification_options[kind].isChecked())
            self.notification_options[kind].toggled.connect(handler)
        self.observer_panel.status_observed.connect(self.receive_proxy_status)
        self.notification_log=table(['시각','알림','건수','확인 근거'])
        self.notification_log.setMinimumHeight(140);self.notification_log.setMaximumHeight(280)
        self.notification_log.put(leftColumns=[0,1,3],emptyText='이번 실행에서 발생한 알림 없음')
        for column,width in enumerate((175,160,70,430)):self.notification_log.setColumnWidth(column,width)
        self.notification_log.cellClicked.connect(self.open_notification_record);self.notification_log.cellActivated.connect(self.open_notification_record)
        self.notification_details=Details('최근 알림',self.notification_log)
        self.settings_page.add_widget('notifications', self.notification_details,section='최근 알림',title='최근 알림',aliases='notification history')
        def sync_notification_controls(*_):
            for option in self.notification_options.values():option.setEnabled(self.notification_master.isChecked())
            self.cache_scope.setEnabled(self.notification_master.isChecked() and self.notification_options['캐시 저하 의심'].isChecked())
        self.notification_master.toggled.connect(sync_notification_controls)
        self.notification_options['캐시 저하 의심'].toggled.connect(sync_notification_controls)
        sync_notification_controls()
        self.settings_page.add_widget('about', label('Codexon '+VERSION, 'section'),section='버전',title='현재 버전')
        from .update_panel import UpdatePanel
        self.update_panel=UpdatePanel(self.observer_panel.manager,self)
        self.update_panel.heading.hide()
        self.settings_page.add_widget('about',self.update_panel,section='업데이트',title='업데이트 확인',target=self.update_panel.button,aliases='정보 문제 해결 update')
        self.recovery_button=Button('Codex 연결 복구 열기')
        self.recovery_button.clicked.connect(self.update_panel.open_recovery)
        self.recovery_status=Text();self.recovery_status.setWordWrap(True)
        self.update_panel.recovery_status=self.recovery_status
        self.settings_page.add_row('troubleshooting','연결 복구','Codex 연결에 문제가 있으면 복구 도구에서 연결 상태를 확인하고 설정을 복원할 수 있습니다.',self.recovery_button,section='연결 복구',aliases='recovery repair')
        self.settings_page.add_widget('troubleshooting',self.recovery_status,section='연결 복구')
        self.observer_panel.status_observed.connect(self.update_panel.proxy_status)
        attribution=Row();attribution.setContentsMargins(0,8,0,16)
        attribution.addWidget(label('제작자: jisoq'),1)
        self.repository_link=Button('GitHub: jisoq/Codexon')
        self.repository_link.setAccessibleName('Codexon GitHub 저장소 열기')
        self.repository_link.setToolTip('https://github.com/jisoq/Codexon')
        self.repository_link.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl('https://github.com/jisoq/Codexon')))
        attribution.addWidget(self.repository_link)
        self.settings_page.add_widget('about', attribution,section='제작자',title='저장소',target=self.repository_link,aliases='GitHub')
        self.settings_page.add_widget('troubleshooting', label('집계 범위: 이 PC에 저장된 Codex 작업 기록입니다. '
            '기록 파일을 만들지 않는 임시 사이드 채팅과 다른 PC의 작업은 포함되지 않습니다.', 'muted', True))
        self.diagnostic_summary=label('','',True)
        self.diagnostic_summary.setTextFormat(Qt.RichText)
        self.settings_page.add_widget('troubleshooting', self.diagnostic_summary,title='수집 상태')
        self.diagnostics = TextArea()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMinimumHeight(230)
        self.diagnostic_details=Details('수집 상세',self.diagnostics)
        self.settings_page.add_widget('troubleshooting', self.diagnostic_details,title='진단 상세',aliases='수집 상세 diagnostics')

    def receive_proxy_status(self,result):
        incident=result.get('incident') or {}
        if incident.get('id') and incident['id'] != self.settings.value('notifications/lastProtectionId',''):
            self.settings.setValue('notifications/lastProtectionId',incident['id'])
            event=self.confirmed_notifications.record('protection','프록시 보호 정지',incident.get('reason','설정에서 확인하세요.'))
            self.show_confirmed_events([event] if self.notification_options['프록시 장애'].isChecked() else [])
        self.show_confirmed_events(self.confirmed_notifications.proxy(result))

    def show_confirmed_events(self,events):
        for event in events:
            count=f" · {event['count']}건" if event['count']>1 else ''
            if self.notification_master.isChecked():
                if event['kind']=='cache_miss':
                    overlay=getattr(self,'overlay',None)
                    scope=self.settings.value('notifications/cacheScope','all')
                    if scope=='selected' and (not overlay or not overlay.selection_confirmed() or not overlay.selected_scope(event.get('home'),event.get('sid'))):
                        continue
                    if overlay and overlay.can_present(event.get('home'),event.get('sid')):
                        continue
                self.tray.showMessage('Codexon · '+tr(event['title']),
                    event['detail'].split('\n')[0]+count,QSystemTrayIcon.Warning,6000)
        records=[dict(r) for r in self.confirmed_notifications.records]
        if records==getattr(self,'_notification_rows',None):return
        self._notification_rows=records
        self.notification_log.set_rows(list(self.confirmed_notifications.records),lambda e,c,role:
            [date_time(e['at']),e['title'],str(e['count']),e.get('resolution') or e['detail']][c])

    def open_notification_record(self,index,*_):
        if not 0<=index<len(self.notification_log.model().rows):return
        event=self.notification_log.model().rows[index]
        if event.get('target'):self.navigate(event['target'])
        else:self.settings_page.reveal('troubleshooting',self.diagnostic_details)
