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
from .pricing import (usd, RATES, ALIASES, VERIFIED, MULTIPLIER_VERIFIED, MODEL_VERIFIED,
                      standard_rate, subscription_multiplier, rate_verified, SUPPORTED_MODELS, supported_model,
                      request_tier, display_tier, token_cost, sum_cost)
from .session_costs import own_costs, session_costs
from .charts import UsageTrend, SourceBars, ComparisonChart, TokenComposition, value_text, COLORS
from .observer_panel import ObserverPanel
from .notifications import ConfirmedNotifications
from .ui_details import Details, strong, recorded, record_issues, price_reason, observed_transport, model_comparison
from .theme import shared_theme
from .version import VERSION
from .i18n import tr, Verbatim, formatted
from .workload import call_count
from .history_navigation import HistoryNavigation, HistoryWorkspace, HistoryTable, HistoryFilters, history_duration
from .history_timeline import SessionTimeline
from .dashboard_presentation import Breadcrumbs, RecordFields, labeled_control, move_control

STYLE = ''
TITLES = ('Home','지표 비교','세션 기록','구독 모니터링','설정','성능 추이')
PERIODS = [('최근 30분','30m'),('오늘','today'),('최근 7일','7d'),('최근 30일','30d'),('전체 기록','all'),('직접 지정','custom')]
INPUT_BANDS = [('전체 입력 길이',''),('10k 미만','0:10000'),('10–50k','10000:50000'),('50–100k','50000:100000'),('100–200k','100000:200000'),('200–272k','200000:272001'),('272k 초과','272001:inf')]
CALL_FILTERS = [('환산 불가','unpriced'),('모드 기록 없음','unknown_mode'),('모델명 불일치','model_mismatch'),('관측 정보 누락 또는 충돌','observation_problem'),('캐시 읽기 0','cache_zero'),('캐시 저하 의심','cache_degradation'),('HTTP/SSE','http')]
CALL_COLUMNS = [('ts','기록 시각'),('model','모델 / 추론 / 모드'),('cost','환산액'),('input','입력'),('output','출력'),('cache_ratio','캐시 적중률'),('completion_latency_ms','소요시간')]
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
def table(headers, *, readable=True):
    node=LazyTable(headers);node.put(rowHeight=40,rowDetails=True,readableColumns=readable);return node
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
    NAVIGATION_LIMIT=64

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
        self.observer_directory=Path(model_evidence_path).parent if model_evidence_path else Path(index_path).parent if index_path else None
        self.manage_observer=manage_observer;self.quota_service=None;self.quota_issue=''
        self.quota_paused=self.settings.value('quota/paused',False,type=bool)
        self.quota_paused_at=self.settings.value('quota/pausedAt',0,type=float) or None
        self.quota_pause_pending=False
        self.index_path=index_path;self.live_limits=live_limits
        self.model_evidence_path=model_evidence_path
        self.snapshot={'ts':time.time(),'sessions':[],'homes':homes,'unassigned':[],'errors':[]}
        self.quitting=False;self.worker=None;self.engine=AnalysisEngine();self.async_mode=start_worker
        self.analysis=analyze([]);self.analysis_pending=False;self.analysis_errors=[];self.analysis_error_history=[]
        self.client_cache=BoundedCache(6,160000);self.request_id=0;self.view_result=None;self.applied_key=None
        self.display_identity=None;self.pending_retained=False
        self.lookup={'sessions':{},'turns':{},'session_turns':{}};self.page_frames={}
        self.started_at=time.time();self.alerted={};self.confirmed_notifications=ConfirmedNotifications()
        from .app_errors import ErrorState
        self.error_state=ErrorState(history=self.confirmed_notifications.records)
        self._error_revision=-1;self.collection_starting=False;self.collection_stopped=False;self._observation_counters=None
        self.collection_snapshot_at=None
        self.current_page=0;self.restoring=True;self.back_stack=[];self.forward_stack=[];self.temporary_context=None;self.navigation_signature=None
        self.page_filters={0:{'model':'','effort':'','service_tier':''},2:{'model':'','effort':'','service_tier':''}}
        self.filter_values={}
        self.history_navigation=HistoryNavigation()
        self.selected_session=None;self.selected_turn=None;self.selected_call=None;self.selected_event=None;self.selected_call_scope=None
        self.record_section='identity';self.record_view='sessions';self.record_rows=[];self.exact_record=None
        self.history_display='table';self.history_hovered_call=None
        self.page_scopes={};self._history_chart_restore=None
        self.record_request=0;self.record_status='';self.aggregate_records=[]
        self.interaction_time=0;self.deferred_result=None;self.defer_timer=QTimer(self)
        self.defer_timer.setSingleShot(True);self.defer_timer.timeout.connect(self.apply_deferred)
        self.pending_timer=QTimer(self);self.pending_timer.setSingleShot(True);self.pending_timer.setInterval(150)
        self.pending_timer.timeout.connect(self.show_analysis_pending)
        self.slow_timer=QTimer(self);self.slow_timer.setSingleShot(True);self.slow_timer.setInterval(5000)
        self.slow_timer.timeout.connect(self.show_analysis_slow)
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
        side_layout.addWidget(label('CODEXON','brand'))
        self.navigation_pages=(0,1,5,2,3)
        self.nav=Navigation();self.nav.addItems(tuple(TITLES[i] for i in self.navigation_pages));self.nav.setObjectName('navigation');self.nav.setMaximumHeight(220)
        side_layout.addWidget(self.nav);side_layout.addStretch()
        self.update_available=Button('업데이트 가능');self.update_available.hide()
        self.update_available.clicked.connect(self.open_updates);side_layout.addWidget(self.update_available)
        from .quota_sidebar import QuotaSidebar
        footer=Group();footer_layout=Column(footer);footer_layout.setSpacing(8)
        self.quota_sidebar=QuotaSidebar();footer_layout.addWidget(self.quota_sidebar)
        self.settings_button=Button('설정');self.settings_button.put(role='navigation');self.settings_button.clicked.connect(self.open_settings)
        footer_layout.addWidget(self.settings_button);self.index_status=label('수집 중','muted',True);self.index_status.put(fontSize=12);footer_layout.addWidget(self.index_status)
        self.error_button=Button();self.error_button.put(role='quiet',wrap=True,fontSize=12)
        self.error_button.hide();self.error_button.clicked.connect(self.open_errors);footer_layout.addWidget(self.error_button)
        side_layout.addWidget(footer)
        self.sidebar_layout=side_layout;self.sidebar_footer_layout=footer_layout;self.layout_sidebar()
        shell.addWidget(side)
        content=Group();self.content=content;content.put(contentWidth=1808);shell.addWidget(content,1);layout=Column(content)
        layout.setContentsMargins(24,20,24,20);layout.setSpacing(10)
        header=Row();self.back_button=action('back','뒤로',self.go_back)
        self.home_button=action('home','현재 탭 처음으로',self.go_home)
        self.navigation_row=Row();self.navigation_row.setSpacing(4)
        self.navigation_row.addWidget(self.home_button);self.navigation_row.addWidget(self.back_button)
        self.path_label=label('','muted');self.path_label.put(noElide=False);self.navigation_row.addWidget(self.path_label,1)
        self.history_path=Breadcrumbs();self.history_path.activated.connect(self.activate_history_path);self.navigation_row.addWidget(self.history_path,1)
        self.heading=label(TITLES[0],'heading');header.addWidget(self.heading);header.addStretch()
        self.pending_label=label('','muted');self.pending_label.setFixedHeight(36);self.pending_label.setMaximumWidth(280);header.addWidget(self.pending_label)
        self.retry_button=Button('다시 시도');self.retry_button.clicked.connect(self.retry_analysis);self.retry_button.hide();header.addWidget(self.retry_button)
        self.price_button=Button('API 가격표');self.price_button.put(role='quiet');self.price_button.clicked.connect(self.show_prices);header.addWidget(self.price_button);layout.addLayout(header)
        self.common_filters=Row();self.common_filters.put(flow=True,spacing=8)
        self.home=combo([('모든 Codex 홈','')]+[(Verbatim(h),h) for h in homes]);self.home.setMinimumWidth(205)
        self.period=combo(PERIODS);choose(self.period,'30d')
        self.date_start=DateInput(QDate.currentDate().addDays(-29));self.date_end=DateInput(QDate.currentDate())
        self.project=combo([('모든 프로젝트','')]);self.project.setMinimumWidth(220)
        self.source=combo([('모든 작업 종류','')]);self.archive=Toggle('보관 세션 포함');self.archive.setChecked(True)
        layout.addLayout(self.navigation_row);layout.addLayout(self.common_filters)
        self.model=combo([('모든 모델','')]);self.model.setMinimumWidth(230)
        self.effort=combo([('모든 추론 설정','')]);self.mode=combo([('모든 요청 모드','')])
        for node,width in ((self.period,150),(self.project,190),(self.source,155),(self.model,190),(self.effort,150),(self.mode,150)):
            node.setFixedWidth(width)
        self.common_fields={}
        for title,node in (('수집 위치',self.home),('조회 기간',self.period),('시작일',self.date_start),('종료일',self.date_end),
                           ('프로젝트',self.project),('작업 종류',self.source),('보관 기록',self.archive),
                           ('모델',self.model),('추론 설정',self.effort),('요청 모드',self.mode)):
            field=labeled_control(title,node);self.common_fields[node]=field;self.common_filters.addWidget(field)
        self.scope_note=label('수집 중','muted');self.scope_note.put(fontSize=12);self.scope_note.setFixedHeight(18);layout.addWidget(self.scope_note)
        self.pages=Stack();self.pages.put(deferPages=True);layout.addWidget(self.pages,1);self.scrollers={}
        self.analysis_notice=Group();self.analysis_notice.setObjectName('analysis-notice');self.analysis_notice.hide()
        notice_layout=Column(self.analysis_notice);notice_layout.addStretch()
        self.analysis_notice_body=Group();notice_layout.addWidget(self.analysis_notice_body);notice_layout.addStretch()
        notice_body=Column(self.analysis_notice_body);notice_body.setSpacing(12)
        self.analysis_notice_title=label('','section',True);self.analysis_notice_title.setAlignment(Qt.AlignHCenter)
        self.analysis_notice_detail=label('','muted',True);self.analysis_notice_detail.setAlignment(Qt.AlignHCenter)
        notice_body.addWidget(self.analysis_notice_title);notice_body.addWidget(self.analysis_notice_detail)
        notice_actions=Row();notice_actions.addStretch()
        self.notice_retry=Button('다시 시도');self.notice_retry.clicked.connect(self.retry_analysis);notice_actions.addWidget(self.notice_retry);notice_actions.addStretch()
        notice_body.addLayout(notice_actions);layout.addWidget(self.analysis_notice,1)
        self.build_overview();self.build_compare();self.build_explorer()
        from .quota_panel import QuotaPanel
        self.quota_panel=QuotaPanel(self.settings);quota_scroll=Scroll();quota_scroll.put(fillViewport=True)
        self.quota_panel.centralized_errors=True
        self.quota_panel.fault_changed.connect(lambda facts:self.observe_error('quota.query','quota',facts,
            healthy=bool(facts.get('query_healthy')),resolution='사용 한도 분석 결과 갱신 확인'))
        quota_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);quota_scroll.setWidget(self.quota_panel);self.pages.addWidget(quota_scroll)
        self.scrollers[3]=quota_scroll;self.build_settings()
        from .performance_panel import PerformancePanel
        self.performance_panel=PerformancePanel(self)
        self.pages.addWidget(self.performance_panel);self.scrollers[5]=self.performance_panel.chart_scroll
        self.health=label('','muted',True);self.health.hide()
        self.setup_tray();self.settings_page.bind_tray(self);self.tray.messageClicked.connect(self.open_tray_notification)
        self.tick=QTimer(self);self.tick.timeout.connect(self.check_stale);self.tick.start(1000)
        self.nav.currentRowChanged.connect(lambda row:self.change_page(self.navigation_pages[row]) if row>=0 else None)
        for node in (self.home,self.period,self.project,self.source,self.model,self.effort,self.mode):node.currentIndexChanged.connect(self.filter_changed)
        for node in (self.date_start,self.date_end):node.dateChanged.connect(self.filter_changed)
        self.archive.toggled.connect(self.filter_changed)
        from .quota_layout import configure_monitoring_controls
        configure_monitoring_controls(self)
        self.restore_preferences();self.restoring=False;self.change_page(self.current_page)
        if start_worker:
            if homes and static_snapshot is None:self.start_quota_service(homes[0])
            self.worker=AnalysisBridge(homes,index_path,static_snapshot,model_evidence_path,quota_path)
            self.worker.snapshot.connect(self.receive);self.worker.result.connect(self.analysis_ready)
            self.worker.fault.connect(self.analysis_failed);self.worker.record.connect(self.record_ready);self.worker.start();self.render()
        elif static_snapshot is not None:self.receive(static_snapshot)
        self.setCentralWidget(root)
        if hasattr(self.quota_panel,'set_homes'):self.quota_panel.set_homes(homes)
        if hasattr(self.quota_panel,'home_selected'):self.quota_panel.home_selected.connect(self.start_quota_service)
        self.quota_panel.exclusion_requested.connect(self.exclude_quota_interval)
        self.quota_panel.exclusion_restore_requested.connect(self.restore_quota_exclusion)
        self.quota_panel.cycle_clear_requested.connect(self.clear_quota_cycle)
        from .app_services import AppServices
        from .app_shutdown import ServiceController
        self.app_services=AppServices(self.observer_panel.manager if manage_observer else None,
            homes,index_path,model_evidence_path,collection=bool(start_worker and collection_autostart and static_snapshot is None))
        self.service_controller=ServiceController(self.app_services,self)
        self.observer_panel.services=self.app_services if manage_observer else None
        self.service_controller.result.connect(self.observer_panel.display)
        self.service_controller.result.connect(self.receive_collection_status)
        if manage_observer or self.app_services.collection:
            QTimer.singleShot(0,self.service_controller.start)

    def layout_sidebar(self):
        compact=self.height()<800
        self.sidebar_layout.setSpacing(6 if compact else 24)
        self.sidebar_layout.setContentsMargins(16,8 if compact else 24,16,8 if compact else 20)
        self.sidebar_footer_layout.setSpacing(4 if compact else 8)

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
                    tracking_enabled=self.settings.value('quota/trackingEnabled',True,type=bool),reports_enabled=False,
                    paused=self.quota_paused,paused_at=self.quota_paused_at)
                services[candidate]=service
                service.updated.connect(lambda value,expected=service: self.receive_quota(value) if self.quota_service is expected else None)
                service.start()
        if home not in services:
            services[home]=QuotaService(home,self.index_path,live=self.live_limits,quota_path=self.quota_path,
                tracking_enabled=self.settings.value('quota/trackingEnabled',True,type=bool),reports_enabled=False,
                paused=self.quota_paused,paused_at=self.quota_paused_at)
            services[home].updated.connect(lambda value,expected=services[home]:self.receive_quota(value) if self.quota_service is expected else None)
            services[home].start()
        services[home].supply_local(self.snapshot.get('quota_by_home',{}).get(services[home].home))
        self.live_quota=None;self.quota_service=services[home]
        self.quota_sidebar.receive({'quota':None})
        self.sync_report_demand()
        self.refresh_tray()
        self.quota_service.wake.set()

    def set_quota_tracking_enabled(self, enabled):
        self.settings.setValue('quota/trackingEnabled',bool(enabled))
        for service in getattr(self,'quota_services',{}).values():
            service.set_tracking_enabled(enabled)

    def refresh_quota_monitoring(self):
        from .quota_layout import refresh_monitoring_controls
        refresh_monitoring_controls(self)

    def set_quota_paused(self,paused):
        paused=bool(paused)
        if paused==self.quota_paused or self.quota_pause_pending:return
        changed=[]
        for service in getattr(self,'quota_services',{}).values():
            if not service.set_paused(paused):
                for applied in changed:applied.set_paused(self.quota_paused)
                self.quota_panel.monitoring_note.setText('모니터링 상태를 저장하지 못했습니다. 다시 시도하세요.')
                self.quota_panel.monitoring_note.show()
                return
            changed.append(service)
        self.quota_paused=paused
        if paused:self.quota_paused_at=time.time()
        self.settings.setValue('quota/paused',paused)
        self.settings.setValue('quota/pausedAt',self.quota_paused_at or 0)
        self.settings.sync()
        self.quota_pause_pending=bool(self.quota_service and self.quota_service.isRunning())
        self.refresh_quota_monitoring()

    def exclude_quota_interval(self, value):
        if self.quota_service:
            self.quota_service.request_exclusion(value['start'], value['end'], value.get('reason', ''))

    def restore_quota_exclusion(self, exclusion_id):
        if self.quota_service:
            self.quota_service.restore_exclusion(exclusion_id)

    def clear_quota_cycle(self, value):
        if self.quota_service:
            self.quota_service.clear_selected_cycle(value['start'],value['account'],delete=value['delete'])

    def receive_quota(self,value):
        if 'paused' in value:
            if value['paused']!=self.quota_paused:return
            if self.quota_service and value.get('monitoring_revision')!=self.quota_service.monitoring_revision:return
            if self.quota_pause_pending:
                self.quota_pause_pending=False
                self.refresh_quota_monitoring()
        self.quota_panel.defer_render=self.current_page!=3 or not self.isVisible() or self.isMinimized()
        if self.quota_panel.receive(value) is False:return
        self.quota_sidebar.receive(value)
        self.live_quota=value.get('quota');self.quota_issue=value.get('issue','');self.refresh_tray()
        facts=value.get('quota_faults')
        if isinstance(facts,dict):
            lookup=facts.get('lookup') or {};storage=facts.get('storage') or {};calculation=facts.get('calculation') or {}
            self.observe_error('quota.live','quota',lookup,healthy=not lookup.get('lookup_failed'))
            self.observe_error('quota.ledger','quota',storage,healthy=not storage.get('storage_failed'))
            self.observe_error('quota.calculation','quota',calculation,
                healthy=not calculation.get('calculation_failed') and not calculation.get('uncertain'))
        edited=value.get('exclusion_result')
        if edited:
            self._quota_edit_waiting_for_report=bool(not edited.get('ok') and edited.get('saved') and not edited.get('uncertain'))
            self.observe_error('quota.edit','quota',dict(calculation_failed=not edited.get('ok'),
                saved=bool(edited.get('saved')),uncertain=bool(edited.get('uncertain'))),healthy=bool(edited.get('ok')))
        elif (getattr(self,'_quota_edit_waiting_for_report',False) and value.get('report_refreshed')
                and isinstance(facts,dict) and not (facts.get('calculation') or {}).get('calculation_failed')
                and not (facts.get('calculation') or {}).get('uncertain')):
            self._quota_edit_waiting_for_report=False
            self.error_state.clear('quota.edit',resolution='저장된 변경의 재집계 완료 확인');self.refresh_errors()

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
        from .usage_panel import UsagePanel
        self.home_panel=UsagePanel(self,home=True);self.pages.addWidget(self.home_panel)


    def build_compare(self):
        from .usage_panel import UsagePanel
        self.metric_panel=UsagePanel(self);self.pages.addWidget(self.metric_panel)

    def build_explorer(self):
        page=Group();layout=Column(page);layout.setSpacing(12)
        self.breadcrumb=self.path_label
        self.session_scope=label('','',True);self.session_scope.put(fontSize=14,bold=True)
        self.residual_details=Details('누계 차액 (미분류)',compact=True);self.residual_details.hide();layout.addWidget(self.residual_details)
        controls=Row();controls.put(flow=True,spacing=12);self.history_toolbar=controls
        self.history_period_fields=Row();self.history_period_fields.put(flow=True,spacing=12)
        self.search=Input();self.search.put(searchIcon=True);self.search.setPlaceholderText('세션 제목, 프로젝트, 세션 ID');self.search.setFixedWidth(320);self.search_timer=QTimer(self);self.search_timer.setSingleShot(True);self.search_timer.setInterval(120)
        self.search_timer.timeout.connect(self.render_explorer)
        self.search.textChanged.connect(self.history_search_changed);controls.addWidget(self.history_period_fields)
        self.sort=combo([('최근 기록부터','time_desc'),('오래된 기록부터','time_asc'),('환산액 높은 순','cost_desc'),('환산액 낮은 순','cost_asc')]);self.sort.currentIndexChanged.connect(self.render_explorer);self.sort.setAccessibleName('정렬 기준')
        self.history_projects_expanded=None
        self.history_projects_button=Button('프로젝트 목록');self.history_projects_button.setCheckable(True)
        self.history_projects_button.put(disclosure=True);self.history_projects_button.toggled.connect(self.toggle_history_projects)
        controls.addWidget(labeled_control('',self.history_projects_button))
        layout.addLayout(controls)
        filter_body=Group();filter_body.put(background='secondary',radius=6)
        fl=Column(filter_body);fl.setContentsMargins(12,12,12,12);fl.setSpacing(12)
        self.history_query_fields=Row();self.history_query_fields.put(flow=True,spacing=12);fl.addWidget(self.history_query_fields)
        fl.addWidget(label('호출 상태','muted'));filter_row=Row();filter_row.put(flow=True);self.call_filter_controls={}
        for title,key in CALL_FILTERS:
            node=Toggle(title);node.toggled.connect(self.call_filter_changed);filter_row.addWidget(node);self.call_filter_controls[key]=node
        fl.addLayout(filter_row);self.outside=Toggle('범위 밖 호출 표시');self.outside.toggled.connect(self.render_explorer);fl.addWidget(self.outside)
        extra_row=Row();extra_row.put(flow=True);self.call_columns_row=extra_row;self.extra_column_controls={}
        for key,title in EXTRA_COLUMNS:
            node=Toggle(title);node.toggled.connect(self.render_explorer);extra_row.addWidget(node);self.extra_column_controls[key]=node
        self.record_filters=HistoryFilters('조회 조건',filter_body)
        controls.addWidget(labeled_control('',self.record_filters.toggle))
        self.record_columns=Details('표시할 열',extra_row,compact=True)
        controls.addWidget(labeled_control('',self.record_columns.toggle))
        layout.addWidget(self.record_filters);layout.addWidget(self.record_columns)
        self.history_active_filters=Row();self.history_active_filters.put(flow=True,spacing=8)
        self.history_filter_chips={}
        for key in ('home','source','archive','model','effort','mode',*self.call_filter_controls,'outside'):
            chip=Button();chip.put(role='quiet',leadingIcon='close');chip.hide()
            chip.clicked.connect(lambda value=key:self.clear_history_filter(value))
            self.history_filter_chips[key]=chip;self.history_active_filters.addWidget(chip)
        layout.addWidget(self.history_active_filters)
        self.record_filters.toggle.toggled.connect(lambda _:self.sync_history_filters())
        self.record_message=label('','muted',True)
        self.records_body=HistoryWorkspace();self.records_body.resized.connect(self.layout_record_detail)
        self.record_parent=Group();parent_layout=Column(self.record_parent)
        self.record_parent.setFixedWidth(200)
        self.record_parent_title=label('프로젝트','section');parent_layout.addWidget(self.record_parent_title)
        self.parent_stack=Stack();parent_layout.addWidget(self.parent_stack,1)
        self.session_parent_table=table([])
        for parent_table in (self.session_parent_table,):
            parent_table.setMinimumHeight(200);self.parent_stack.addWidget(parent_table)
            parent_table.cellClicked.connect(self.activate_record_parent);parent_table.cellActivated.connect(lambda index,*_:self.activate_record_parent(index,1))
        self.parent_table=self.session_parent_table
        self.records_body.addWidget(self.record_parent)
        self.record_current=Scroll();self.record_current.put(fillViewport=True)
        self.record_current.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        current_body=Group();self.record_current.setWidget(current_body);current_layout=Column(current_body)
        current_header=Row();current_header.put(flow=True,spacing=12)
        self.record_current_title=label('요청','section');self.record_current_title.hide()
        self.history_view_controls=Row();self.history_view_controls.setSpacing(0);self.history_view_buttons={}
        for title,key in (('표','table'),('차트','chart')):
            button=Button(title);button.setCheckable(True);button.put(selectionTab=True)
            button.clicked.connect(lambda value=key:self.set_history_display(value))
            self.history_view_controls.addWidget(button);self.history_view_buttons[key]=button
        self.history_view_controls.hide();current_header.addWidget(self.history_view_controls)
        self.history_up_button=Button('요청별 보기');self.history_up_button.put(role='quiet',leadingIcon='back');self.history_up_button.clicked.connect(self.show_history_requests)
        self.history_all_calls_button=Button('전체 호출 펼치기');self.history_all_calls_button.put(role='quiet',leadingIcon='right');self.history_all_calls_button.clicked.connect(self.show_history_calls)
        current_header.addWidget(self.history_up_button);current_header.addWidget(self.history_all_calls_button)
        current_header.addStretch();current_header.addWidget(self.search);current_header.addWidget(self.sort);current_layout.addWidget(current_header)
        current_layout.addWidget(self.session_scope)
        current_layout.addWidget(self.record_message)
        self.history_display_stack=Stack();current_layout.addWidget(self.history_display_stack,1)
        self.history_tables={};self.history_table_stack=Stack();self.history_display_stack.addWidget(self.history_table_stack)
        for kind in ('sessions','requests','calls'):
            node=HistoryTable([]);node.put(rowHeight=56,rowDetails=True,readableColumns=True,freezeFirstColumn=True)
            node.expansionRequested.connect(self.toggle_history_session);node.setMinimumHeight(200)
            clear_search=Button('검색 지우기');clear_search.put(role='quiet');clear_search.clicked.connect(self.clear_history_search)
            reset_filters=Button('필터 초기화');reset_filters.put(role='quiet');reset_filters.clicked.connect(self.reset_history_filters)
            node.addWidget(clear_search);node.addWidget(reset_filters)
            node.cellClicked.connect(self.activate_record)
            node.cellActivated.connect(self.activate_record)
            self.history_tables[kind]=node;self.history_table_stack.addWidget(node)
        self.table=self.history_tables['sessions']
        self.history_chart_panel=Group();chart_layout=Column(self.history_chart_panel);chart_layout.setSpacing(12)
        self.history_metric=combo([('환산액 (USD)','cost'),('입력과 출력 토큰','tokens'),('입력 토큰 기준 캐시율 (%)','cache_ratio'),('소요시간 (초)','duration'),('전체 응답 기준 출력 속도','output_speed')])
        self.history_metric.setAccessibleName('차트 지표');self.history_metric.currentIndexChanged.connect(self.render_history_chart)
        chart_controls=Row();chart_controls.put(flow=True,spacing=8);chart_controls.addWidget(self.history_metric)
        self.history_axis=combo([('요청/호출 순서','ordinal'),('기록 시각','time')]);self.history_axis.setAccessibleName('차트 X축')
        self.history_axis.currentIndexChanged.connect(self.render_history_chart);chart_controls.addWidget(self.history_axis)
        self.history_zoom=Button('선택 구간 확대');self.history_zoom.put(leadingIcon='plus');self.history_zoom.clicked.connect(self.zoom_history_chart);chart_controls.addWidget(self.history_zoom)
        self.history_reset_zoom=Button('전체 범위');self.history_reset_zoom.put(role='quiet');self.history_reset_zoom.clicked.connect(lambda:self.history_chart.reset_range());chart_controls.addWidget(self.history_reset_zoom)
        chart_layout.addWidget(chart_controls)
        self.history_chart=SessionTimeline();self.history_chart.put(height=-1,minHeight=200,maxHeight=16777215)
        self.history_chart.inspected.connect(self.inspect_timeline_call)
        self.history_chart.selected.connect(self.open_timeline_call);chart_layout.addWidget(self.history_chart,1)
        self.history_chart_note=label('요청을 선택하면 해당 호출로 내려갑니다.','',True);self.history_chart_note.put(fontSize=14)
        chart_layout.addWidget(self.history_chart_note)
        inspector_row=Row();inspector_row.put(spacing=12)
        self.history_inspector=label('','',True);inspector_row.addWidget(self.history_inspector,1)
        self.history_detail_button=Button('호출 상세');self.history_detail_button.put(role='quiet',leadingIcon='right');self.history_detail_button.clicked.connect(lambda:self.open_timeline_call(self.history_hovered_call))
        inspector_row.addWidget(self.history_detail_button);chart_layout.addWidget(inspector_row)
        self.history_display_stack.addWidget(self.history_chart_panel)
        self.records_body.addWidget(self.record_current)
        self.history_summary=label('','',True);self.history_summary.put(fontSize=14)
        current_layout.addWidget(Details('집계 상세',self.history_summary,compact=True))
        self.range_timer=QTimer(self);self.range_timer.setSingleShot(True);self.range_timer.timeout.connect(self.render_explorer)
        for node in (*self.history_tables.values(),self.session_parent_table):
            node.rangeRequested.connect(lambda:self.range_timer.start(0) if not self.analysis_pending else None)
        self.parent_rows=[];self.parent_kind='projects'
        self.record_detail_panel=Group();panel_layout=Column(self.record_detail_panel)
        self.detail_scroll=Scroll();self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);detail=Group();detail_layout=Column(detail);detail_layout.setSpacing(24)
        dh=Row();self.record_detail_title=label('호출 상세','section');dh.addWidget(self.record_detail_title);dh.addStretch();panel_layout.addLayout(dh)
        self.close_record_button=Button('상세 닫기');self.close_record_button.put(role='quiet',leadingIcon='close');self.close_record_button.clicked.connect(self.close_record_detail);self.close_record_button.hide();dh.addWidget(self.close_record_button)
        self.detail_sections={}
        for key,title in (('identity','작업'),('conditions','모델과 요청 조건'),('usage','토큰 사용량'),('pricing','환산 근거'),('time','시간'),('evidence','관측 정보 확인')):
            body=Group();bl=Column(body);text=RecordFields();text.put(readableLabels=True);bl.addWidget(text)
            if key=='pricing':
                section=Details(title,body,compact=True);detail_layout.addWidget(section)
            else:
                bl.insertWidget(0,label(title,'section'));section=body;detail_layout.addWidget(body)
            self.detail_sections[key]=(section,text)
            if key=='usage':
                self.input_composition=TokenComposition('input');self.output_composition=TokenComposition('output')
                bl.addWidget(self.input_composition);bl.addWidget(self.output_composition)
            if key=='evidence':
                self.incident_button=Button('캐시 사건 보기');self.incident_button.clicked.connect(self.open_call_incident);bl.addWidget(self.incident_button);self.incident_button.hide()
        self.record_identifiers=RecordFields();self.record_identifiers_details=Details('식별 정보',self.record_identifiers,compact=True);detail_layout.addWidget(self.record_identifiers_details)
        self.detail_scroll.setWidget(detail);self.detail_scroll.hide();panel_layout.addWidget(self.detail_scroll,1);self.records_body.addWidget(self.record_detail_panel,0);layout.addLayout(self.records_body,1);self.pages.addWidget(page)

    def restore_preferences(self):
        try:
            saved=json.loads(self.settings.value('dashboard/state','{}'))
            if saved:self.restore_state(saved,initial=True)
        except (ValueError,TypeError,KeyError):pass

    def capture_state(self):
        return dict(design_version=4,history_navigation=self.history_navigation.capture(),metric_workspace=self.metric_panel.capture(),page=self.current_page,settings_category=self.settings_page.current_category(),
            common={key:getattr(self,key).currentData() for key in ('home','period','project','source')},
            archive=self.archive.isChecked(),dates=[self.date_start.text(),self.date_end.text()],
            page_filters=copy.deepcopy(self.page_filters),page_scopes=copy.deepcopy(self.page_scopes),
            choices={key:getattr(self,key).currentData() for key in ('sort',)},
            selected_session=self.selected_session,selected_turn=self.selected_turn,selected_call=self.selected_call,selected_call_scope=self.selected_call_scope,
            selected_event=self.selected_event,record_section=self.record_section,record_view=self.record_view,
            history_projects_expanded=self.history_projects_expanded,history_display=self.history_display,history_metric=self.history_metric.currentData(),history_axis=self.history_axis.currentData(),
            history_chart_state=self.history_chart.capture_state() if self.history_chart_active() else None,
            search=self.search.text(),call_filters=[k for k,n in self.call_filter_controls.items() if n.isChecked()],
            extras=[k for k,n in self.extra_column_controls.items() if n.isChecked()],outside=self.outside.isChecked(),
            scrolls={str(k):v.verticalPosition.value() for k,v in self.scrollers.items()},
            record_scroll=self.table.verticalScrollBar().value(),detail_scroll=self.detail_scroll.verticalPosition.value(),
            record_horizontal=self.table.horizontalScrollBar().value(),settings_scrolls=self.settings_page.capture_scrolls(),
            parent_scroll=self.parent_table.verticalScrollBar().value(),parent_horizontal=self.parent_table.horizontalScrollBar().value(),
            record_selection=self.table.row_key(self.record_rows[self.table.currentRow()]) if 0<=self.table.currentRow()<len(self.record_rows) else None,
            widths=self.table.state['widths'],temporary=copy.deepcopy(self.temporary_context))

    def restore_state(self,state,initial=False):
        if not initial and self.current_page!=state.get('page',0):self.home_panel.remember_page(self.current_page)
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
        self.page_scopes={int(k):v for k,v in state.get('page_scopes',{}).items()}
        for values in self.page_filters.values():
            for key,attr in (('model','model'),('effort','effort'),('service_tier','mode')):
                value=values.get(key);node=getattr(self,attr)
                if value and node.findData(value)<0:node.addItem(str(value),value)
        self.metric_panel.restore(state.get('metric_workspace',{}))
        choose(self.sort,state.get('choices',{}).get('sort','ts_desc'))
        self.selected_session=tuple(state['selected_session']) if state.get('selected_session') else None
        self.selected_turn=state.get('selected_turn');self.selected_call=state.get('selected_call');self.selected_event=state.get('selected_event')
        self.selected_call_scope=tuple(state['selected_call_scope']) if state.get('selected_call_scope') else self.selected_session
        self.record_section=state.get('record_section','identity');self.record_view=state.get('record_view','sessions')
        self.history_projects_expanded=state.get('history_projects_expanded')
        self.history_display='chart' if state.get('history_display')=='chart' else 'table'
        self.refresh_history_metrics()
        choose(self.history_metric,state.get('history_metric','cost'))
        choose(self.history_axis,state.get('history_axis','ordinal'))
        self._history_chart_restore=None if initial else state.get('history_chart_state')
        if self.record_view in ('projects','children'):self.record_view='sessions';self.selected_session=None
        if initial:
            self.record_view='sessions';self.selected_session=self.selected_turn=self.selected_call=self.selected_event=self.selected_call_scope=None
        self.search.setText(state.get('search',''));self.outside.setChecked(state.get('outside',False))
        for key,node in self.call_filter_controls.items():node.setChecked(key in state.get('call_filters',[]))
        for key,node in self.extra_column_controls.items():node.setChecked(key in state.get('extras',[]))
        self.temporary_context=None if initial else state.get('temporary');self.current_page=max(0,min(5,state.get('page',0)))
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
            if self.current_page in (3,4):self._restore_positions=None

    def save_preferences(self):
        if self.restoring or self.temporary_context:return
        state=self.capture_state();state['temporary']=None
        if state['page']==4:state['page']=0
        encoded=json.dumps(state,ensure_ascii=False)
        if encoded!=getattr(self,'_saved_state',None):
            self.settings.setValue('dashboard/state',encoded);self._saved_state=encoded

    def remember_navigation(self,stack):
        state=self.capture_state()
        if not stack or stack[-1]!=state:stack.append(state)
        del stack[:-self.NAVIGATION_LIMIT]

    def push_state(self):
        self.forward_stack.clear()
        self.remember_navigation(self.back_stack)
        self.back_button.setEnabled(True)

    def go_back(self):
        if self.back_stack or self.selected_call or self.selected_turn or self.selected_session:
            self.remember_navigation(self.forward_stack)
        if self.back_stack:
            state=self.back_stack.pop();self.navigation_signature=None;self.restore_state(state)
        elif self.selected_call:self.close_record_detail()
        elif self.selected_turn:self.selected_turn=None;self.record_view='requests';self.render_explorer()
        elif self.selected_session:self.selected_session=None;self.record_view='sessions';self.render_explorer()
        self.update_navigation()

    def go_forward(self):
        if not self.forward_stack:return
        self.remember_navigation(self.back_stack)
        state=self.forward_stack.pop();self.navigation_signature=None;self.restore_state(state)
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
            deep=bool(self.selected_session or self.selected_turn or self.selected_call or self.temporary_context or self.record_view=='calls')
        else:
            path=TITLES[self.current_page];deep=False
        self.navigation_row.setVisible(self.current_page==2)
        self.history_path.setVisible(self.current_page==2);self.path_label.setVisible(self.current_page!=2)
        self.home_button.setVisible(self.current_page!=2)
        if self.current_page==2:
            self.back_button.setText('');self.back_button.put(iconName='back',flat=True);self.back_button.setFixedWidth(38)
            if self.view_result and 'explorer' in self.view_result:self.render_history_path(self.view_result['explorer'])
        else:
            self.back_button.setText('');self.back_button.put(iconName='back',flat=True);self.back_button.setFixedWidth(36)
        self.path_label.setText(Verbatim(path) if self.current_page==2 else path);self.path_label.setToolTip(Verbatim(path) if self.current_page==2 else path)
        self.close_record_button.setVisible(self.current_page==2 and self.detail_scroll.isVisible())
        self.record_message.setVisible(self.current_page==2 and bool(self.record_message.text()))
        self.home_button.setEnabled(deep)
        self.back_button.setEnabled(bool(self.back_stack or deep))
        self.back_button.setVisible(self.current_page!=2 or bool(self.back_stack or deep))

    def go_home(self):
        self.push_state()
        if self.current_page==2:
            self.selected_session=self.selected_turn=self.selected_call=self.selected_event=None
            self.selected_call_scope=None;self.exact_record=None;self.temporary_context=None
            self.history_navigation.project='';self.record_view='sessions';self.record_request+=1;self.render()
        elif self.current_page==1:self.metric_panel.reset()
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
            'home':choices.get('homes') or self.snapshot['homes']}
        names={'model':'모든 모델','effort':'모든 추론 설정','service_tier':'모든 요청 모드','project':'모든 프로젝트','source':'모든 작업 종류','home':'모든 Codex 홈'}
        source_names={'user':'유저 에이전트','subagent':'하위 에이전트','guardian_review':'내부 검토','maintenance':'캐시 유지','unknown':'미확인'}
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
                node.addItem(Verbatim(title) if key=='project' and value else str(title),value)
                if key=='project' and value:node.setItemData(node.count()-1,'\n'.join(project_paths.get(value,[])),Qt.ToolTipRole)
            # An explicit selection survives zero samples, even when the source disappears.
            if current and node.findData(current)<0:node.addItem(str(caption(key,current)),current)
            choose(node,current);node.blockSignals(blocked)
        self.home.setVisible(len(self.snapshot['homes'])>1)
    def save_page_scope(self):
        if self.current_page in (0,1,2) and not (self.current_page==2 and self.temporary_context):
            self.page_scopes[self.current_page]=dict(common={key:getattr(self,key).currentData() for key in ('home','period','project','source')},
                dates=[self.date_start.text(),self.date_end.text()],archive=self.archive.isChecked())

    def change_page(self,index):
        if self.restoring:return
        if self.current_page!=index:
            self.home_panel.remember_page(self.current_page)
            self.save_page_scope();self.forward_stack.clear()
        scope=self.page_scopes.get(index) if self.current_page!=index else None
        if self.current_page in (0,2):
            self.page_filters[self.current_page]={key:getattr(self,attr).currentData() for key,attr in (('model','model'),('effort','effort'),('service_tier','mode'))}
        self.current_page=max(0,min(5,index));self.pages.setCurrentIndex(self.current_page);self.heading.setText(TITLES[self.current_page])
        self.sync_report_demand()
        self.restoring=True
        if scope:
            for key,value in scope['common'].items():
                node=getattr(self,key)
                if value and node.findData(value)<0:node.addItem(Verbatim(str(value)),value)
                choose(node,value)
            self.date_start.setText(scope['dates'][0]);self.date_end.setText(scope['dates'][1]);self.archive.setChecked(scope['archive'])
        self.nav.put(homeOnly=self.current_page==0)
        self.nav.setCurrentRow(self.navigation_pages.index(self.current_page) if self.current_page in self.navigation_pages else -1)
        self.settings_button.put(checked=self.current_page==4)
        for key,attr in (('model','model'),('effort','effort'),('service_tier','mode')):
            if self.current_page in (0,2):choose(getattr(self,attr),self.page_filters[self.current_page].get(key,''))
        self.restoring=False
        self.content.put(contentWidth=0 if index==2 else 1808)
        self.project.setVisible(index!=2)
        self.sync_history_filters()
        self.common_filters.hide();self.scope_note.hide()
        for node in (self.model,self.effort,self.mode):node.setVisible(index in (0,2))
        self.request_id+=1;self.analysis_pending=False;self.deferred_result=None;self.defer_timer.stop()
        self.price_button.setVisible(index!=4);self.clear_analysis_notice()
        if hasattr(self,'quota_pause_button'):self.refresh_quota_monitoring()
        self.update_navigation()
        self.render();self.save_preferences()

    def open_settings(self):self.show_window();self.change_page(4)

    def open_errors(self):
        self.open_settings();self.settings_page.reveal('notifications',self.error_panel)

    def perform_error_action(self,kind,page):
        if kind=='analysis':
            if self.worker is not None and not self.worker.isRunning():
                self.worker.restart_count=0;self.worker.start()
            self.client_cache.clear();self.change_page(getattr(self,'_analysis_error_page',0))
        elif kind=='collection':
            self.open_settings();self.settings_page.reveal('general')
            self.service_controller.invoke('ensure')
        elif page=='quota':
            self.show_window();self.change_page(3)
        elif page=='integration':
            self.open_settings();self.settings_page.reveal('integration',self.recovery_button)
        elif page=='about':self.open_updates()

    def observe_error(self,source,kind,facts,*,healthy=False,resolution='정상 동작 확인'):
        fault=self.error_state.update(source,kind,facts,healthy=healthy,resolution=resolution)
        self.refresh_errors()
        if fault and self.notification_master.isChecked():
            if kind in ('connection','observation') and not self.notification_options['프록시 장애'].isChecked():return
            self.show_tray_notification('app_error',tr(fault.title),
                tr(fault.explanation)+'\n'+tr(fault.guidance),QSystemTrayIcon.Warning)

    def refresh_errors(self):
        if self._error_revision!=self.error_state.revision:
            self._error_revision=self.error_state.revision
            faults=self.error_state.grouped();self.error_panel.apply(faults)
            self.error_button.setVisible(bool(faults));self.index_status.setVisible(not faults)
            if faults:
                self.error_button.setText(next(iter(faults.values())).title if len(faults)==1 else
                    formatted('오류 {count}개 발생',count=len(faults)))
                self.error_button.setToolTip('\n'.join(tr(fault.title) for fault in faults.values()))
                from .app_errors import strongest
                principal=strongest(faults.values()) or next(iter(faults.values()))
                self.error_button.put(color=principal.severity)
        self.refresh_notification_log()
        if hasattr(self,'tray'):self.refresh_tray()

    def refresh_notification_log(self):
        records=[dict(record) for record in self.confirmed_notifications.records]
        if records==getattr(self,'_notification_rows',None):return
        self._notification_rows=records
        self.notification_log.set_rows(records,lambda event,column,role:
            [date_time(event['at']),event['title'],str(event['count']),
             ' '.join((event.get('resolution') or event['detail']).splitlines())][column])
        self.notification_log.setFixedHeight(70+max(1,len(records))*self.notification_log.state['rowHeight'])

    def receive_update_error(self,facts):
        checking=bool(facts.get('checking_failed') or facts.get('check_healthy'))
        operation=bool(facts.get('operation_error') or facts.get('operation_healthy'))
        self.observe_error('update.check' if checking else 'update.operation' if operation else 'update.status','update',facts,
            healthy=bool(facts.get('check_healthy') or facts.get('operation_healthy') or facts.get('status_healthy')),
            resolution='업데이트 정상 동작 확인')
        if facts.get('status_healthy'):
            self.error_state.clear('update.operation',resolution='업데이트 취소 확인' if facts.get('phase')=='cancelled' else '업데이트 완료 확인')
            self.refresh_errors()

    def open_updates(self):
        self.open_settings();self.settings_page.reveal('about',self.update_panel)

    def open_tray_notification(self):
        if getattr(self,'_tray_notification_kind','')=='app_error':self.open_errors()
        elif getattr(self,'_tray_notification_kind','')=='update':self.open_updates()
        else:self.open_notification_details()

    def show_tray_notification(self,kind,title,message,icon):
        self._tray_notification_kind=kind
        self.tray.showMessage(title,message,icon,6000)

    def bind_update_monitor(self,monitor):
        self.update_monitor=monitor
        self.update_panel.bind_monitor(monitor)
        monitor.available.connect(self.update_discovered)
        monitor.fault_changed.connect(self.receive_update_error)
        self.auto_update.toggled.connect(monitor.set_enabled)
        self.notification_master.toggled.connect(lambda enabled:monitor.publish() if enabled else None)

    def update_discovered(self,release):
        self.update_available.setVisible(bool(release))
        if not release:return
        from .i18n import formatted
        tag=release['tag_name'];version=tag.lstrip('v')
        monitor=self.update_monitor
        from .app_update import version_parts
        try:already_notified=version_parts(self.settings.value('updates/notifiedVersion',''))>=version_parts(version)
        except (ValueError,TypeError):already_notified=False
        if monitor.enabled and self.notification_master.isChecked() and not already_notified:
            self.settings.setValue('updates/notifiedVersion',version);self.settings.sync()
            self.show_tray_notification('update','Codexon: '+tr('업데이트 가능'),
                formatted('새 버전 {version} 사용 가능. 설정에서 바로 설치할 수 있습니다.',version=version),QSystemTrayIcon.Information)

    def open_notification_details(self):
        self.open_settings();self.settings_page.reveal('notifications',self.notification_log)

    def query(self):
        page=self.current_page
        if page==5:
            panel=self.performance_panel
            return dict(page=5,start=0,end=self.snapshot['ts']+.000001,now=self.snapshot['ts'],period='all',presentation=True,
                        time_range=panel.query_range(self.snapshot['ts'],self.snapshot.get('data_revision',self.snapshot['ts'])),plot_width=max(64,self.width()-96),granularity=panel.granularity)
        if page in (0,1):return (self.home_panel if page==0 else self.metric_panel).query()
        start,end=self.bounds()
        q=dict(page=page,include_whole_history=True,start=start,end=end,home=self.home.currentData(),project=self.project.currentData(),source=self.source.currentData(),
            archived=self.archive.isChecked(),period=self.period.currentData(),now=self.snapshot['ts'],loading=bool(self.snapshot.get('index',{}).get('loading')),
            unit='response',method='mean',metric='cost',presentation=True)
        q.update(self.page_filters.get(page,{}))
        if self.temporary_context and page==2:
            q.update(start=0,end=self.snapshot['ts']+.000001,period='all',home=self.temporary_context.get('home',''),sid=self.temporary_context.get('sid',''),project='',source='',archived=True,model='',effort='',service_tier='')
        if page==2:
            self.use_history_table()
            q['project']=''
            if self.selected_session:
                from .analytics import project_identity
                session=next((s for s in self.snapshot['sessions'] if (s['home'],s['id'])==self.selected_session),None)
                if session:self.history_navigation.project=project_identity(session)
            elif not self.history_navigation.project and self.record_view=='sessions':
                labels=self.snapshot.get('filter_choices',{}).get('project_labels') or project_choices(self.snapshot['sessions'])['project_labels']
                if labels:self.history_navigation.project=min(labels,key=lambda pid:labels[pid].casefold())
            scope=self.selected_call_scope or self.selected_session
            q['explorer']=dict(project=self.history_navigation.project,expanded=sorted(self.history_navigation.expanded),search_collapsed=sorted(self.history_navigation.search_collapsed),view=self.record_view,session=self.selected_session,turn=self.selected_turn,
                event=self.selected_event,search=self.search.text(),sort=self.sort.currentData(),
                filters=[k for k,n in self.call_filter_controls.items() if n.isChecked()],
                outside=self.outside.isChecked(),context=self.temporary_context,
                timeline=self.history_chart_active(),
                call_identity=(*scope,self.selected_call) if scope and self.selected_call else None)
            q['table_windows']={name:dict(start=max(0,node.first_visible-32),size=max(128,min(512,(node.last_visible-node.first_visible+1)*3)))
                for name,node in (('records',self.table),('parents',self.parent_table))}
        return q

    def render(self,*_,automatic=False):
        if self.restoring or not hasattr(self,'settings_page'):return
        if automatic and (not self.isVisible() or self.isMinimized()):
            self._display_dirty=True;return
        custom=self.period.currentData()=='custom';self.date_start.setVisible(custom);self.date_end.setVisible(custom)
        if self.current_page in (3,4):
            if self.current_page==3:self.quota_panel.refresh_status()
            return
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
                    self.finish_analysis_loading(value,q)
                    return
                self.apply_result(value,q,automatic)
                return
            self.analysis_pending=True
            self.begin_analysis_loading(q)
            if self.worker:self.worker.request(self.request_id,q,logical)
        else:
            self.apply_result(self.engine.page_query(q),q,automatic)

    def query_identity(self,query):
        """User-selected scope, independent of a new snapshot or table viewport."""
        ignored={'now','loading','table_windows','plot_width'}
        if query['period']!='custom':ignored.update(('start','end'))
        identity={k:v for k,v in query.items() if k not in ignored}
        if query['page']==5 and self.performance_panel.follow_latest:
            identity['time_range']=('latest',self.performance_panel.window_span)
        return json.dumps(identity,sort_keys=True)

    def clear_analysis_notice(self):
        self.pending_timer.stop();self.slow_timer.stop();self.pending_label.hide();self.retry_button.hide()
        self.analysis_notice.hide();self.pages.show();self.scope_note.hide()

    def begin_analysis_loading(self,query):
        self.clear_analysis_notice()
        self.pending_retained=self.display_identity==self.query_identity(query)
        if not self.pending_retained:
            # Suppress empty charts immediately; delay only the loading text.
            self.pages.hide();self.scope_note.hide();self.analysis_notice.show();self.analysis_notice_body.hide()
        self.pending_timer.start();self.slow_timer.start()

    def show_analysis_notice(self,title,detail,retry=False):
        self.pages.hide();self.scope_note.hide();self.analysis_notice.show();self.analysis_notice_body.show()
        self.analysis_notice_title.setText(title);self.analysis_notice_detail.setText(detail)
        self.notice_retry.setVisible(retry)

    def show_analysis_pending(self):
        if not self.analysis_pending or self.current_page in (3,4):return
        if self.pending_retained:
            self.pending_label.setText('최신 기록 반영 중');self.pending_label.setToolTip('');self.pending_label.show()
        else:
            self.show_analysis_notice(formatted('{page} 불러오는 중',page=tr(TITLES[self.current_page])),
                                      '선택한 조건에 맞는 사용 기록을 조회하고 있습니다.')

    def show_analysis_slow(self):
        if not self.analysis_pending or self.current_page in (3,4):return
        if self.pending_retained:
            self.pending_label.setText('최신 기록 분석 결과 대기 중')
        else:
            self.analysis_notice_detail.setText('분석 결과를 기다리고 있습니다.\n다른 탭을 이용할 수 있습니다.')

    def finish_analysis_loading(self,result,query):
        self.analysis_pending=False;self.clear_analysis_notice()
        if self.snapshot.get('index',{}).get('loading') and not result['analysis']['response_count']:
            self.display_identity=None
            self.show_analysis_notice('사용 기록 수집 중','Codex 사용 기록을 읽고 있습니다.')
        else:self.display_identity=self.query_identity(query)

    def retry_analysis(self):
        if self.worker is not None and not self.worker.isRunning():
            self.worker.restart_count=0;self.worker.start()
        self.client_cache.clear();self.render()

    def scope_text(self,q):
        if self.temporary_context and q['page']==2:
            ctx=self.temporary_context
            if ctx.get('population') or ctx.get('records') is not None:
                return Verbatim(tr('선택한 집계에 포함된 기록')+' / '+str(ctx.get('label') or ''))
            names=[ctx.get('home',''),ctx.get('sid',''),tr('전체 기록, 자체 호출')]
            names += list(ctx.get('filters',[]))
            return Verbatim(', '.join(str(x) for x in names if x))
        names=[Verbatim(q['home']) if q.get('home') else ('모든 Codex 홈' if len(self.snapshot['homes'])>1 else (Verbatim(self.snapshot['homes'][0]) if self.snapshot['homes'] else '로컬 기록')),
            self.period.currentText(),Verbatim(q['project']) if q.get('project') else '모든 프로젝트',self.source.currentText()]
        if q['page'] in (0,2):names += [q.get(k) for k in ('model','effort','service_tier') if q.get(k)]
        return Verbatim(', '.join(tr(x) for x in names if x))

    def receive_collection_status(self,result):
        if isinstance(result.get('collection_snapshot_at'),(int,float)):
            self.collection_snapshot_at=result['collection_snapshot_at']
        if isinstance(result.get('collection_fault'),dict):
            facts=result['collection_fault'];self.collection_starting=bool(facts.get('starting'))
            self.collection_stopped=bool(facts.get('expected_stop'))
            self.observe_error('collection.service','collection',facts,
                healthy=bool(result.get('collection_healthy')) or bool(facts.get('expected_stop')),
                resolution='수집기 종료 확인' if facts.get('expected_stop') else '수집기 실행 확인')
        if 'collection_issue' not in result:return
        self.collection_issue=result['collection_issue']
        overlay=getattr(self,'overlay',None)
        if overlay:overlay.receive_collection_status(result)

    def receive(self,value):
        if value.get('data_revision')!=self.snapshot.get('data_revision'):
            self.client_cache.clear()
        if not self.async_mode:
            self.engine.ingest(value['sessions'])
            value={**value,'sessions':[s['source'] for s in self.engine.sessions.values()],
                   'internal_review_calls':sum(len(s['prepared']['history']) for s in self.engine.internal_sessions.values())}
        self.snapshot=value
        self.error_state.clear('analysis.process',resolution='분석기 실행 확인')
        if isinstance(value.get('collection_fault'),dict):
            self.observe_error('collection.source','collection',value['collection_fault'],
                healthy=bool(value.get('usage_collection_complete')),resolution='사용 기록 수집 재개 확인')
        elif value.get('usage_errors'):
            self.observe_error('collection.source','collection',dict(read_failed=True))
        if 'observation_read_failed' in value:
            self.observe_error('observation.read','observation',dict(read_failed=bool(value['observation_read_failed'])),
                healthy=not value['observation_read_failed'],resolution='관측 기록 읽기 확인')
        if value.get('ledger_status_known'):
            self.observe_error('quota.analysis','quota',value.get('ledger_fault') or {},
                healthy=not value.get('ledger_error'),resolution='사용 한도 기록 갱신 확인')
        self.performance_panel.receive_collection_time(value['ts'])
        for service in getattr(self,'quota_services',{}).values():
            service.supply_local(value.get('quota_by_home',{}).get(service.home))
        overlay=getattr(self,'overlay',None)
        if overlay:
            overlay.receive_collection_status({'collection_issue':getattr(self,'collection_issue','')})
            overlay.receive_snapshot(value)
        self.refresh_choices();index=value.get('index',{})
        self.index_status.setText('수집 중' if index.get('loading') else '수집 확인 필요' if value.get('errors') else '로컬 기록 수집 중')
        self.show_confirmed_events(self.confirmed_notifications.cache(value.get('cache_candidates',value['sessions'])))
        self.show_confirmed_events(self.confirmed_notifications.http(value['sessions']))
        self.show_confirmed_events(self.confirmed_notifications.model_candidates(value['model_candidates']) if 'model_candidates' in value else self.confirmed_notifications.models(value['sessions']))
        self.refresh_tray();self.render(automatic=True)

    def analysis_ready(self,message):
        result=message['result']
        if message['id']!=self.request_id:return
        self.error_state.clear('analysis.query',resolution='분석 결과 갱신 확인')
        self.error_state.clear('analysis.process',resolution='분석기 실행 확인')
        self.refresh_errors()
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
        self.finish_analysis_loading(result,query)
        if query['page']==5:
            self.view_result=result;self.performance_panel.apply(result['performance'])
            restored=getattr(self,'_restore_positions',None)
            if restored:
                position=restored.get('scrolls',{}).get('5',restored.get('scrolls',{}).get(5,0))
                self.scrollers[5].verticalPosition.setValue(position);self._restore_positions=None
            self.refresh_after_analysis()
            return
        self.view_result=result;self.applied_key=result['key'];self.analysis=result['analysis'];self.lookup=result['lookup']
        self.table.live_update=automatic
        self.parent_table.live_update=automatic
        a=self.analysis;count=a['response_count'];sessions=a['session_count']
        self.scope_note.setText(Verbatim(tr(f'관측 {count:,}호출, {sessions:,}세션'+(', 수집 중' if query.get('loading') else ''))))
        self.scope_note.setToolTip(self.scope_text(query))
        if not hasattr(self,'_applied_pages'):self._applied_pages={}
        display=tuple(k for k,n in self.extra_column_controls.items() if n.isChecked()) if self.current_page==2 else ()
        previous=self._applied_pages.get(self.current_page)
        detail_changed=self.current_page==2 and bool(self.selected_call or self.selected_event)!=self.detail_scroll.isVisible()
        if previous is None or previous[0] is not result or previous[1]!=display or detail_changed or getattr(self,'_restore_positions',None):
            if self.current_page==0:self.render_overview()
            elif self.current_page==1:
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
        # Comparison setup may resolve default targets while applying the result.
        if self.display_identity is not None:self.display_identity=self.query_identity(self.query())
        self.refresh_after_analysis()

    def refresh_after_analysis(self):
        if getattr(self,'_refresh_after_pending',False):
            self._refresh_after_pending=False;QTimer.singleShot(0,lambda:self.render(automatic=True))

    def analysis_failed(self,error):
        facts=dict(error) if isinstance(error,dict) else dict(query_failed=True,request_id=self.request_id)
        if facts.get('query_failed') and facts.get('request_id')!=self.request_id:return
        facts.update(current_request_id=self.request_id,retained=self.display_identity==self.query_identity(self.query()))
        if self.current_page in (0,1,2,5):self._analysis_error_page=self.current_page
        self.observe_error('analysis.query' if facts.get('query_failed') else 'analysis.process','analysis',facts)
        self.client_cache.clear();self.pending_logical=None
        self.request_id+=1;self.deferred_result=None;self.defer_timer.stop();self.clear_analysis_notice()
        self.analysis_pending=False;self.analysis_errors=[facts];self.analysis_error_history=(self.analysis_error_history+[facts])[-10:]
        if self.current_page not in (3,4):
            if not facts['retained']:
                self.pages.hide();self.scope_note.hide()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Wheel,QEvent.MouseButtonPress,QEvent.KeyPress):self.interaction_time=time.monotonic()
        if (event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease,QEvent.MouseButtonDblClick)
            and isinstance(watched,QWidget) and watched.window() is self
            and event.button() in (Qt.BackButton,Qt.ForwardButton)):
            if event.type()==QEvent.MouseButtonRelease:
                (self.go_back if event.button()==Qt.BackButton else self.go_forward)()
            event.accept();return True
        if event.type()==QEvent.KeyPress and event.key()==Qt.Key_Escape and watched is self.quick and self.current_page==2 and self.detail_scroll.isVisible():
            self.close_record_detail();return True
        return super().eventFilter(watched,event)
    def apply_deferred(self):
        pending=self.deferred_result;self.deferred_result=None
        if pending and pending[0]==self.request_id:
            self.apply_result(pending[1],pending[2],True)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'sidebar_layout'):self.layout_sidebar()
        if hasattr(self,'settings_page'):self.settings_page.navigation.setFixedWidth(144 if self.width()<1200 else 200)
        if hasattr(self,'detail_scroll'):self.layout_record_detail()
        if getattr(self,'current_page',0)==5 and hasattr(self,'performance_panel'):self.performance_panel.resize_timer.start()
    def showEvent(self,event):
        super().showEvent(event)
        self.sync_report_demand()
        self.quota_sidebar.refresh_status()
        if getattr(self,'_display_dirty',False):
            self._display_dirty=False;QTimer.singleShot(0,lambda:self.render(automatic=True))

    def hideEvent(self,event):
        super().hideEvent(event);self.sync_report_demand()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.WindowStateChange:self.sync_report_demand()

    def closeEvent(self,event):
        if self.quitting:self.home_panel.clear_thumbnails(closing=True)
        self.settings.setValue('dashboard/geometry',self.saveGeometry());self.save_preferences();super().closeEvent(event)
    def check_stale(self):
        if not self.quitting:
            observed=max((stamp for stamp in (self.snapshot.get('ts'),self.collection_snapshot_at)
                          if stamp is not None),default=None)
            self.observe_error('collection.freshness','collection',
                dict(snapshot_at=observed,starting=self.collection_starting,expected_stop=self.collection_stopped),
                healthy=self.collection_stopped or self.collection_starting or observed is not None and time.time()-observed<=30,
                resolution='수집기 종료 확인' if self.collection_stopped else '새 수집 결과 확인')
        self.refresh_tray()
        if self.isVisible() and not self.isMinimized():
            self.quota_sidebar.refresh_status()
            if self.current_page==3:self.quota_panel.refresh_status()
        if int(time.time())%10==0:shared_theme().configure(self.settings.value('ui/theme',self.settings.value('overlay/theme','codex')))

    def render_overview(self,*_):
        self.home_panel.apply(self.view_result['usage'])


    def open_aggregate_records(self):
        if not self.aggregate_records:return
        self.push_state();self.save_page_scope();self.restoring=True
        self.temporary_context={'population':self.aggregate_records,'label':'선택 집계의 호출','home':'','sid':''}
        self.selected_session=self.selected_turn=self.selected_call=self.selected_event=self.selected_call_scope=None
        self.record_view='calls';self.exact_record=None;self.search.clear();self.search_timer.stop();self.outside.setChecked(False)
        for node in self.call_filter_controls.values():node.setChecked(False)
        self.restoring=False;self.change_page(2)


    def render_comparison(self,*_):
        self.metric_panel.apply(self.view_result['usage'])


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

    def call_filter_changed(self,checked):
        if self.restoring:return
        # Filters narrow the current level without changing the route.
        self.render_explorer()

    def toggle_history_projects(self, expanded):
        self.history_projects_expanded=expanded
        self.layout_record_detail()

    def clear_history_filter(self, key):
        self.push_state();previous=self.restoring;self.restoring=True
        try:
            if key in self.call_filter_controls:self.call_filter_controls[key].setChecked(False)
            elif key=='outside':self.outside.setChecked(False)
            elif not self.temporary_context:
                if key=='archive':self.archive.setChecked(True)
                else:choose(getattr(self,key),'')
                self.page_filters[2]={name:node.currentData() for name,node in
                    (('model',self.model),('effort',self.effort),('service_tier',self.mode))}
        finally:self.restoring=previous
        self.render_explorer()

    def update_history_filter_chips(self):
        active={}
        if not self.temporary_context:
            for key,title in (('home','Codex 홈'),('source','작업 종류'),('model','모델'),('effort','추론 설정'),('mode','요청 모드')):
                node=getattr(self,key)
                if node.currentData():active[key]=tr(title)+': '+node.currentText()
            if not self.archive.isChecked():active['archive']=tr('보관 기록 제외')
        for title,key in CALL_FILTERS:
            if self.call_filter_controls[key].isChecked():active[key]=tr(title)
        if self.outside.isChecked() and self.record_view=='calls':active['outside']=tr('범위 밖 호출 표시')
        for key,chip in self.history_filter_chips.items():
            chip.setVisible(key in active)
            if key in active:
                chip.setText(Verbatim(active[key]));chip.setAccessibleName(formatted('{condition} 조건 해제',condition=active[key]))
                chip.setToolTip(formatted('{condition} 조건 해제',condition=active[key]))
        self.history_active_filters.setVisible(bool(active))
        self.record_filters.toggle.setText(formatted('조회 조건 ({count})',count=len(active)) if active else '조회 조건')

    def sync_history_filters(self):
        history=self.current_page==2
        # Hidden filters keep their rendered owner when leaving for quota or
        # settings. Reparenting recreates every sibling QML delegate repeatedly.
        if self.current_page in (0,1,2):
            for node,field in self.common_fields.items():
                destination=(self.history_period_fields if node in (self.period,self.date_start,self.date_end) else self.history_query_fields) if history and node is not self.project else self.common_filters
                move_control(field,destination)
            if not history and self.common_filters._nodes!=list(self.common_fields.values()):
                self.common_filters._nodes=list(self.common_fields.values());self.common_filters.structureChanged.emit()
        for node in (self.home,self.source,self.archive,self.model,self.effort,self.mode):
            node.setVisible(self.current_page<3 and (node not in (self.model,self.effort,self.mode) or self.current_page in (0,2)))
        self.home.setVisible(self.current_page<3 and len(self.snapshot['homes'])>1)

    def render_history_path(self,data):
        entries=[dict(item,action='scope',literal=True) for item in data.get('path',[])]
        if self.selected_turn:
            ordinal=data.get('request_ordinal')
            title=formatted('요청 {number}',number=ordinal) if ordinal else tr('요청 미연결' if self.selected_turn=='__unlinked__' else '요청')
            entries.append(dict(title=title,action='request',literal=True))
        elif self.record_view=='calls':entries.append(dict(title='전체 호출',action='request'))
        if self.selected_call:
            record=self.exact_record or data.get('detail') or {}
            title=formatted('호출 {number}',number=record['session_ordinal']) if record.get('session_ordinal') else tr('호출 상세')
            entries.append(dict(title=title,action='leaf',literal=True))
        self.history_path.set_entries(entries)

    def activate_history_path(self,index):
        item=self.history_path.entries[index]
        if item['action']=='root':self.go_home()
        elif item['action']=='scope':
            if item.get('sid') and (item.get('home'),item['sid'])==self.selected_session:self.show_history_requests()
            else:self.enter_history(item)
        elif item['action']=='request':self.close_record_detail()

    def render_record_parent(self,data):
        self.parent_kind='projects';self.parent_table=self.session_parent_table
        self.parent_stack.setCurrentIndex(0)
        self.parent_table.setHorizontalHeaderLabels(['프로젝트'])
        self.parent_table.put(widths=[200],rowHeight=46,leftColumns=[0],treeNavigation=True)
        self.parent_rows=self.parent_table.set_window(data['parents'],lambda row,column,role:Verbatim(row['title']),('projects',))
        from .history_projection import node_key
        active=node_key('project',self.history_navigation.project)
        self.parent_table.select_row(next((data['parents']['start']+i for i,r in enumerate(data['parents']['rows']) if r['key']==active),-1))
        self.record_current_title.setText({'projects':'프로젝트','sessions':'세션','children':'하위 세션','requests':'요청','calls':'호출'}.get(self.record_view,''))

    def activate_record_parent(self,index,column=1,*_):
        if not 0<=index<len(self.parent_rows):return
        row=self.parent_rows[index]
        if row.get('_placeholder'):return
        self.enter_history(row)

    def toggle_history_session(self,key):
        self.history_navigation.toggle(key,bool(self.search.text().strip()));self.render_explorer()

    def history_search_changed(self):
        if not self.restoring:self.history_navigation.search_collapsed.clear()
        self.search_timer.start()

    def enter_history(self,row):
        self.push_state()
        self.selected_call=self.selected_event=self.selected_turn=None;self.exact_record=None
        self.history_navigation.project=row['project_id']
        self.selected_session=(row['home'],row['sid']) if row.get('sid') else None
        self.record_view='requests' if self.selected_session else 'sessions'
        if not self.selected_session:
            self.search.clear();self.search_timer.stop();self.temporary_context=None;self.selected_call_scope=None
        self.use_history_table()
        choose(self.sort,'time_asc' if self.selected_session else 'time_desc')
        self.table.verticalScrollBar().setValue(0)
        self.table.horizontalScrollBar().setValue(0)
        self.render_explorer()

    def use_history_table(self):
        kind=self.record_view if self.record_view in self.history_tables else 'sessions'
        self.table=self.history_tables[kind]
        self.history_table_stack.setCurrentIndex(list(self.history_tables).index(kind))

    def history_chart_active(self):
        return self.history_display=='chart' and bool(self.selected_session) and self.record_view in ('requests','calls')

    def set_history_display(self,value):
        if value not in ('table','chart') or value==self.history_display:return
        self.history_display=value;self.render_explorer()

    def show_history_requests(self):
        if not self.selected_session:return
        for index in range(len(self.back_stack)-1,-1,-1):
            state=self.back_stack[index]
            if (state.get('page')==2 and state.get('record_view')=='requests'
                and tuple(state.get('selected_session') or ())==self.selected_session and not state.get('selected_call')):
                self.forward_stack.clear();self.back_stack=self.back_stack[:index];self.restore_state(state);return
        if self.record_view=='requests':
            self.close_record_detail();return
        self.push_state();self.selected_turn=self.selected_call=self.selected_event=None
        self.exact_record=None;self.record_view='requests';self.render_explorer()

    def show_history_calls(self):
        if not self.selected_session:return
        self.push_state();self.selected_turn=self.selected_call=self.selected_event=None
        self.exact_record=None;self.record_view='calls';self.render_explorer()

    def zoom_history_chart(self):
        chart=self.history_chart
        if not chart.current_row:return
        center=chart.current_row.get('ordinal') if self.history_axis.currentData()=='ordinal' else chart.current_row.get('ts')
        if center is None:return
        start,end=chart.range;half=(end-start)/4
        chart.set_range(center-half,center+half)

    def refresh_history_metrics(self):
        requests=self.record_view=='requests'
        items=[('요청별 환산액 (USD)' if requests else '호출별 환산액 (USD)','cost'),
               ('요청 입력과 출력 합계' if requests else '입력과 출력 토큰','tokens'),('입력 토큰 기준 캐시율 (%)','cache_ratio'),
               ('요청 소요시간 (초)' if requests else '호출 소요시간 (초)','duration')]
        if not requests:items.append(('전체 응답 기준 출력 속도','output_speed'))
        if [(self.history_metric.itemText(i),self.history_metric.itemData(i)) for i in range(self.history_metric.count())]!=items:
            current=self.history_metric.currentData();blocked=self.history_metric.blockSignals(True);self.history_metric.clear()
            for title,key in items:self.history_metric.addItem(title,key)
            choose(self.history_metric,current);self.history_metric.blockSignals(blocked);self.history_metric.stateChanged.emit()

    def render_history_chart(self,*_):
        if self.restoring or not self.view_result or 'explorer' not in self.view_result:return
        self.refresh_history_metrics()
        if not self.history_chart_active():return
        data=self.view_result['explorer'];timeline=data.get('timeline') or {};rows=timeline.get('rows',[])
        requests=self.record_view=='requests'
        self.history_chart.empty_text=tr(self.record_status or '호출 기록 없음')
        self.history_chart.set_data(rows,self.history_metric.currentData(),axis=self.history_axis.currentData(),kind='requests' if requests else 'calls')
        if self._history_chart_restore and self.history_chart_active():
            self.history_chart.restore_state(self._history_chart_restore);self._history_chart_restore=None
        self.inspect_timeline_call(self.history_chart.current_row)
        omitted=timeline.get('omitted_timestamps',0)
        note=tr('요청 선택: 호출별 보기 / 상단 세션 경로: 요청별 보기' if requests else '호출 선택: 상세 보기 / 상단 경로: 이전 단계')
        if requests:note+='\n'+tr('요청 토큰은 포함된 호출의 합계입니다. 컨텍스트 크기와 다릅니다.')
        elif self.history_metric.currentData()=='output_speed':note+='\n'+tr('출력 토큰 ÷ 전체 응답 시간. 추론과 대기 시간이 포함됩니다.')
        if omitted:note+='\n'+formatted('시각 미확인 {count}개: 시간축에서 제외',count=omitted)
        self.history_chart_note.setText(Verbatim(note))

    def inspect_timeline_call(self,row):
        self.history_hovered_call=row
        self.history_detail_button.setEnabled(bool(row));self.history_detail_button.setVisible(bool(row))
        if not row:self.history_inspector.setText('');return
        requests=self.record_view=='requests';self.history_detail_button.setText('호출별 보기' if requests else '호출 상세')
        title=(formatted('요청 {number}',number=row.get('ordinal')) if row.get('ordinal') else tr('요청 미연결')) if requests else formatted('호출 {ordinal} / {model}',ordinal=row.get('ordinal') or '—',model=row.get('model') or '—')
        metrics=tr('환산액 {cost} / 입력 {input} / 출력 {output} / 캐시 {cache} / 소요시간 {duration}').format(
            cost=usd(row.get('cost')),input=number(row.get('input')),output=number(row.get('output')),
            cache=value_text(row.get('cache_ratio'),'cache_ratio'),duration=history_duration(row.get('duration')))
        if requests:title+=' / '+formatted('선택 {selected} / 전체 {total}호출',selected=row.get('calls',0),total=row.get('total_calls',0))+' / '+tr(row.get('state') or '완료 기록 없음')
        self.history_inspector.setText(Verbatim(date_time(row.get('ts'))+' / '+title+'\n'+metrics))

    def open_timeline_call(self,row):
        if not row or not self.history_chart_active():return
        if self.record_view=='requests':
            self.push_state();self.selected_turn=row.get('turn') or '__unlinked__';self.record_view='calls'
            self.selected_call=self.selected_event=None;self.exact_record=None;self.render_explorer();return
        if not self.selected_call:self.push_state()
        self.selected_call=call_id(row);self.selected_call_scope=(row['home'],row['sid'])
        self.selected_event=None;self.record_section='identity';self.exact_record=None
        self.render_explorer()

    def render_explorer(self,*_):
        if self.restoring or self.current_page!=2:return
        self.render()

    def clear_history_search(self):
        self.push_state();self.search.clear();self.search_timer.stop();self.render_explorer()

    def history_filters_active(self):
        if any(node.isChecked() for node in self.call_filter_controls.values()):return True
        if self.temporary_context:return False
        return (self.period.currentData()!='all' or not self.archive.isChecked()
                or any(node.currentData() for node in (self.home,self.source,self.model,self.effort,self.mode)))

    def reset_history_filters(self):
        self.push_state();self.restoring=True
        for node in self.call_filter_controls.values():node.setChecked(False)
        if not self.temporary_context:
            for node in (self.home,self.source,self.model,self.effort,self.mode):choose(node,'')
            choose(self.period,'all');self.archive.setChecked(True)
            self.page_filters[2]={'model':'','effort':'','service_tier':''}
        self.restoring=False;self.search_timer.stop();self.render_explorer()

    def history_empty_state(self,data):
        if data['records']['total']:return '','',False,False
        ctx=self.temporary_context or {}
        if self.snapshot.get('index',{}).get('loading'):
            return '사용 기록 수집 중','Codex 사용 기록을 읽고 있습니다.',False,False
        if ctx.get('sid') and data['metadata'] is None:
            return '대상 기록을 찾을 수 없음','',False,False
        if not self.snapshot['sessions']:
            return '사용 기록 없음','아직 수집된 사용 기록이 없습니다.\nCodex에서 작업한 뒤 다시 확인하세요.',False,False
        search=bool(self.search.text().strip());filters=self.history_filters_active()
        if search or filters:
            title='검색 조건에 맞는 기록 없음' if search else '조건에 맞는 기록 없음'
            return title,'검색어나 필터를 변경해 다른 기록을 확인하세요.',search,filters
        return '현재 범위에 기록 없음','선택한 프로젝트 또는 세션에 표시할 기록이 없습니다.',False,False

    def apply_explorer(self):
        # A queued resize can arrive after navigation has changed the active page.
        if self.current_page!=2 or not self.view_result or 'explorer' not in self.view_result:return
        data=self.view_result['explorer'];ctx=self.temporary_context or {}
        self.selected_session=tuple(data['session']) if data['session'] else None
        self.selected_turn=data['turn'];self.record_view=data['view'];self.record_status=''
        self.history_navigation.project=data['project']
        self.use_history_table()
        session=data['metadata'];records=data['records']['rows']
        self.render_history_path(data)
        self.table.put(highlightZeroCache=self.record_view=='calls',hierarchy=self.record_view=='sessions')
        summary=data.get('summary',{})
        self.history_summary.setText(Verbatim(tr(f"캐시 {value_text(summary.get('cache_ratio'),'cache_ratio')} / 입력 {number(summary.get('input'))} / 출력 {number(summary.get('output'))}")))
        self.update_navigation()
        self.outside.setVisible(self.record_view=='calls')
        self.update_history_filter_chips()
        self.record_columns.toggle.setEnabled(self.record_view=='calls')
        self.record_columns.setVisible(self.record_view=='calls')
        self.session_scope.setVisible(True);self.sort.setVisible(True)
        current=self.record_view
        wide=self.records_body.available_width>=1800
        self.record_current_title.hide()
        self.sync_history_filters()
        residual=(session or {}).get('unclassified');self.residual_details.setVisible(bool(residual))
        if residual:self.residual_details.set_sections([('별도 누계', ', '.join(f'{name} {number(residual.get(key))}' for key,name in (('input','입력'),('cached','캐시 읽기'),('written','캐시 쓰기'),('output','출력'),('reasoning','추론'),('total','Total')))),('집계','호출, 요청 통계에 포함하지 않음')])
        if session:
            group=data['group']
            if group['descendants']:
                total_cost=tr('확인분 ' if group.get('partial') and group.get('cost') is not None else '')+usd(group['cost'])
                context=formatted('세션 전체 (하위 포함): {calls}호출 / 환산액 {cost}',calls=f"{group['calls']:,}",cost=total_cost)
                context+='\n'+formatted('세션 자체 {own} / 하위 세션 {children}',own=usd(group['own_cost']),children=usd(group['child_cost']))
                self.history_summary.setText(Verbatim(str(context)+'\n'+self.history_summary.text()))
        scope_title=tr('세션 자체 조회 결과') if session else tr('프로젝트 조회 결과')
        if self.selected_turn:
            scope_title=formatted('요청 {number} 조회 결과',number=data.get('request_ordinal') or tr('미연결'))
        cost=('확인분 ' if summary.get('partial') and summary.get('cost') is not None else '')+usd(summary.get('cost'))
        self.session_scope.setText(formatted('{scope}: {calls}호출 / 환산액 {cost}',scope=scope_title,calls=f"{summary.get('calls',0):,}",cost=tr(cost)))
        if self.temporary_context:self.history_summary.setText(Verbatim(self.history_summary.text()+'\n'+self.scope_text(self.query())))
        if self.record_view in ('projects','sessions','children'):
            headers=['프로젝트' if self.record_view=='projects' else '세션','요청 / 호출','환산액 (하위 포함)','캐시 적중률','최근 활동'];widths=[350,116,156,116,180]
            self.table.put(leftColumns=[0],rowHeight=56)
            if wide:headers+=['입력','출력'];widths+=[110,110]
            def formatter(r,c,role,view=self.record_view):
                title=r['title']
                if view=='projects':title+=f"\n작업 {r['roots']}, 하위 {r['children']}"
                return [Verbatim(title),f"{r.get('requests',0)} / {r.get('calls',0)}",('확인분 ' if r.get('partial') and r.get('cost') is not None else '')+usd(r.get('cost')),value_text(r.get('cache_ratio'),'cache_ratio'),date_time(r.get('ts')),number(r.get('input')),number(r.get('output'))][c]
        elif self.record_view=='requests':
            headers=['요청 / 시작 시각','상태','소요시간','선택 / 전체 호출','환산액'];widths=[260,110,110,130,140];self.table.put(leftColumns=[0,1,5],rowHeight=48)
            if wide:headers+=['완료 시각','입력 / 출력','캐시 적중률'];widths+=[180,155,110]
            def formatter(r,c,role):
                whole=r.get('total_responses',r.get('responses',0))
                return [f"요청 {r.get('ordinal') or '미연결'}, "+date_time(r.get('started_at')),r.get('state') or '완료 기록 없음',history_duration(r.get('duration')),f"{r.get('responses',0):,} / {whole:,}",usd(r.get('cost')),date_time(r.get('ended_at')),number(r.get('input'))+' / '+number(r.get('output')),value_text(r.get('rate'),'cache_ratio')][c]
        else:
            self.table.put(leftColumns=[0,1],rowHeight=56)
            available=set(data['available'])
            for key,node in self.extra_column_controls.items():node.setVisible(key in available and key not in {k for k,_ in CALL_COLUMNS})
            self.active_columns=CALL_COLUMNS+[c for c in EXTRA_COLUMNS if c[0] not in {key for key,_ in CALL_COLUMNS} and c[0] in available and (self.extra_column_controls[c[0]].isChecked() or wide and c[0]=='output_speed')]
            self.table.put(leftColumns=[i for i,(key,_) in enumerate(self.active_columns) if key in ('ts','model','response_model','response_service_tier','transport')])
            headers=[title for key,title in self.active_columns];widths=[155,220,100,95,95,105,95]+[150]*(len(headers)-len(CALL_COLUMNS))
            formatter=self.response_cell
        if headers!=self.table.model().headers:
            self.table.setHorizontalHeaderLabels(headers)
            self.table.put(widths=widths)
        minimums=([230,100,135,100,145] if self.record_view in ('projects','sessions','children') else
                  [235,90,120,120,100] if self.record_view=='requests' else [135,180,90,82,82,100,95])
        self.table.put(minimumColumnWidths=minimums+[130]*max(0,len(headers)-len(minimums)),columnStretchWeights=[1]+[0]*(len(headers)-1))
        numeric={'환산액','환산액 (하위 포함)','소요시간','비용 (하위 포함)','비용','평균 호출 비용','호출 수','산정 / 전체 호출','캐시 적중률','선택 / 전체 호출','입력','입력 / 출력','캐시 읽기','캐시 쓰기','출력','추론','추론 외','평균 출력 속도'}
        self.table.put(noElideColumns=[i for i,title in enumerate(headers) if title in numeric])
        self.record_rows=self.table.set_window(data['records'],formatter,(self.record_view,tuple(headers)))
        self.render_record_parent(data)
        self.update_navigation()
        self.record_status,empty_description,clear_search,reset_filters=self.history_empty_state(data)
        self.table.put(emptyText=self.record_status,emptyDescription=empty_description)
        self.table.nodes[0].setVisible(clear_search);self.table.nodes[1].setVisible(reset_filters)
        self.parent_table.put(emptyText=self.record_status or '기록 없음')
        self.render_history_chart()
        if self.selected_event:self.render_event_detail()
        elif self.selected_call:
            self.exact_record=data['detail']
            if self.exact_record:self.render_record_detail(self.exact_record)
            else:self.request_exact_record()
            if self.exact_record and not data['detail_in_scope']:self.record_status='기록 갱신으로 현재 조건에서 제외됨'
        else:self.detail_scroll.hide()
        self.record_message.setText(self.record_status);self.record_message.setVisible(bool(self.record_status));self.layout_record_detail()

    def response_cell(self,row,column,role):
        key=self.active_columns[column][0];value=row.get(key)
        if role==Qt.ToolTipRole:
            if key=='cost' and value is None:return '환산 제외: '+price_reason(row)
            if key=='service_tier' and not recorded(request_tier(row)):return '이 호출의 요청 모드가 로컬 기록에 저장되지 않았습니다.'
            if key in ('response_model','response_service_tier','transport') and not recorded(value):return '이 호출에는 해당 정보가 기록되지 않았습니다.'
            if key=='model':
                basis='실제 요청 기록' if row.get('model_source')=='wire' else '당시 로컬 요청 설정' if row.get('model_source')=='settings' else '모델 기록 없음'
                return '\n'.join(x for x in [basis,model_comparison(row),*record_issues(row)] if x)
        if key=='model':
            model=str(value or '—')+(f" ({row['model_match']})" if model_comparison(row) else '')
            return Verbatim(model+'\n'+str(row.get('effort') or '—')+', '+request_tier(row))
        if key=='ts':return date_time(value)
        if key=='cost':return usd(value) if value is not None else '미산정'
        if key=='cache_ratio':return value_text(cache_ratio(row),'cache_ratio')
        if key=='completion_latency_ms':return history_duration(row.get('duration'))
        if key=='output_speed':return value_text(value,'output_speed') if value is not None else '측정 불가'
        if key=='observation':return ((row['_incident_phase']+', ') if row.get('_incident_phase') else '')+('범위 밖: ' if row.get('_outside') else '')+('캐시 읽기 0: ' if self.matches_call_filter(row,'cache_zero') else '')+observation(row)
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
            self.update_navigation()

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
            self.change_page(4);self.settings_page.reveal('integration',self.recovery_button)
        else:
            self.record_message.setText('대상 기록을 읽는 중: '+target.sid);self.change_page(2)
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
            rows=((self.view_result or {}).get('explorer',{}).get('timeline') or {}).get('rows',[]) if self.history_chart_active() else self.record_rows
            self.record_message.setText('' if any(identity(r)==identity(self.exact_record) for r in rows) else '기록 갱신으로 현재 조건에서 제외됨')
        else:
            self.record_status='대상 기록을 찾을 수 없음';self.record_message.setText(self.record_status);self.detail_scroll.hide()
        self.layout_record_detail()

    def render_record_detail(self,row):
        from .core import token_parts
        field_values={}
        def entries(values, literal=False, key=None):
            if key:field_values[key]=([(name,value) for name,value in values if recorded(value)],literal)
            text='\n'.join(f'{tr(name) if literal else name}  {value}' for name,value in values if recorded(value))
            return Verbatim(text) if literal else text
        section={key:'' for key in self.detail_sections}
        section['identity']=entries([('작업',row.get('title')),('프로젝트',row.get('project_name')),
            ('기록 시각',date_time(row['ts']) if row.get('ts') is not None else None),
            ('세션 ID',row.get('sid')),('요청 ID',row.get('turn')),('호출 ID',call_id(row))],literal=True,key='identity')
        identifiers=[item for item in field_values['identity'][0] if item[0].endswith(' ID')]
        field_values['identity']=([item for item in field_values['identity'][0] if not item[0].endswith(' ID')],True)
        self.record_identifiers.set_content(entries(identifiers,literal=True),identifiers,literal=True)
        self.record_identifiers_details.setVisible(bool(identifiers))
        parent=next((session for session in self.snapshot.get('sessions',[]) if session['id']==row.get('parent_thread_id') and session['home']==row['home']),None)
        if parent:
            section['identity']=Verbatim(section['identity']+'\n'+tr('상위 작업')+'  '+parent['title'])
            field_values['identity'][0].append(('상위 작업',parent['title']))
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
        section['conditions']=entries(conditions,key='conditions')
        parts=token_parts(row)
        for chart,key in ((self.input_composition,'input'),(self.output_composition,'output')):
            chart.setVisible(parts[key].get('total') is not None)
            chart.set_parts(parts[key])
        lines=[entries([('입력',number(row['input']) if row.get('input') is not None else None),
            ('출력',number(row['output']) if row.get('output') is not None else None),
            ('캐시 적중률',value_text(cache_ratio(row),'cache_ratio') if cache_ratio(row) is not None else None)],key='usage')]
        if row.get('reported_total') is not None and row['reported_total']!=row.get('total'):lines.append(f"총량 관측 차이, 보고 총량 {number(row['reported_total'])}")
        section['usage']='\n'.join(lines)
        section['usage']+='\n평균 출력 속도  '+(value_text(row['output_speed'],'output_speed') if row.get('output_speed') is not None else '측정 불가')
        field_values['usage'][0].extend([('평균 출력 속도',value_text(row['output_speed'],'output_speed') if row.get('output_speed') is not None else '측정 불가'),('환산액',usd(row['cost']) if row.get('cost') is not None else '미산정')])
        section['usage']+='\n환산액  '+(usd(row['cost']) if row.get('cost') is not None else '미산정')
        if row.get('output_speed') is not None:
            section['time']=entries([('호출 소요시간',history_duration(row.get('duration')))],key='time')+'\n출력 / 요청부터 완료까지, 대기와 통신 포함'
        price_model=row.get('price_model',row.get('model'));rate=standard_rate(row.get('model'))
        multiplier=subscription_multiplier(row.get('model'),request_tier(row))
        lines=[f'API 단가 확인 {rate_verified(price_model)} / 구독 배율 확인 {MULTIPLIER_VERIFIED}',
               'Standard API 단가 × 구독 차감 배율',entries([('가격 모델',price_model)])]
        if rate:
            for label_,tokens,price in [('일반 입력',row.get('ordinary_input'),rate.input),('캐시 읽기',row.get('cached'),rate.cached),('캐시 쓰기',row.get('written'),rate.written if rate.written is not None else rate.input),('출력',row.get('output'),rate.output)]:
                if tokens is not None and price is not None:lines.append(f'{label_}  {number(tokens)} × {usd(price)} / 1,000,000')
        if row.get('cost') is not None:
            lines.extend([f'구독 배율  × {multiplier:g}', '환산액  '+usd(row['cost'])])
        else:lines=['환산 제외 / '+price_reason(row)]
        section['pricing']='\n'.join(x for x in lines if x)
        section['evidence']='\n'.join(record_issues(row))
        for key,text in section.items():
            if key in field_values:
                values,literal=field_values[key]
                note=''
                if key=='usage' and row.get('reported_total') is not None and row['reported_total']!=row.get('total'):
                    note=formatted('총량 관측 차이, 보고 총량 {total}',total=number(row['reported_total']))
                elif key=='time':note='평균 출력 속도는 출력 토큰을 요청부터 완료까지의 시간으로 나눈 값입니다. 대기와 통신 시간을 포함합니다.'
                self.detail_sections[key][1].set_content(text,values,note,literal=literal)
            else:self.detail_sections[key][1].setText(text)
            self.detail_sections[key][0].setVisible(bool(text) or key=='usage' or key=='evidence' and bool(row.get('cache_incident_id')))
        self.record_detail_title.setText(formatted('호출 {number} 상세',number=row['session_ordinal']) if row.get('session_ordinal') else '호출 상세')
        self.incident_button.setVisible(bool(row.get('cache_incident_id')))
        self.detail_scroll.show();self.layout_record_detail()
        if getattr(self,'_revealed_call',None)!=(identity(row),self.record_section):
            self._revealed_call=(identity(row),self.record_section)
            target=self.detail_sections.get(self.record_section,self.detail_sections['identity'])[0]
            if not target.isVisible():target=self.detail_sections['conditions'][0]
            if isinstance(target,Details):target.toggle.setChecked(True)
            if self.record_section=='identity':self.detail_scroll.verticalPosition.setValue(0)
            else:self.detail_scroll.ensureWidgetVisible(target)

    def render_event_detail(self):
        self.record_identifiers_details.hide();self.record_detail_title.setText('캐시 사건')
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
            fields.append(label_+': '+', '.join(map(str,keys)))
            for key in keys:
                row=next((r for r in self.analysis['responses'] if (r['home'],r['sid'])==self.selected_session and str(call_id(r))==str(key)),None)
                if row:fields.append(f"{key}, 입력 {number(row.get('input'))}, 읽기 {number(row.get('cached'))}, {value_text(cache_ratio(row),'cache_ratio')}")
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
        chart=self.history_chart_active()
        show_projects=self.history_projects_expanded if self.history_projects_expanded is not None else not self.selected_session and self.records_body.available_width>=1000
        blocked=self.history_projects_button.blockSignals(True);self.history_projects_button.setChecked(show_projects);self.history_projects_button.blockSignals(blocked)
        self.search.setFixedWidth(220 if self.records_body.available_width-(225 if show_projects else 0)<900 else 320)
        self.records_body.put(detailed=active,keepChart=chart,showProjects=show_projects,routeToken=str((self.record_view,self.selected_session,self.selected_turn)))
        self.table.setVisible(not active);self.detail_scroll.put(width=-1,minWidth=0,maxWidth=16777215,stretch=1)
        self.history_view_controls.setVisible(bool(self.selected_session) and self.record_view in ('requests','calls'))
        self.history_up_button.setVisible(bool(self.selected_session) and self.record_view=='calls')
        self.history_all_calls_button.setVisible(bool(self.selected_session) and self.record_view=='requests')
        for key,button in self.history_view_buttons.items():button.setChecked(key==self.history_display)
        self.history_display_stack.setCurrentIndex(1 if chart else 0)
        self.sort.setVisible(not chart);self.record_columns.toggle.setVisible(not chart)
        self.record_columns.setVisible(not chart and self.record_view=='calls')
        self.record_parent.setVisible(show_projects);self.record_current.setVisible(not active or chart and self.records_body.available_width-(225 if show_projects else 0)>=1000)
    def show_prices(self):
        dialog=Dialog(self);dialog.setWindowTitle(tr('API 가격표')+' / '+MODEL_VERIFIED+' '+tr('지원 모델'));dialog.resize(1080,720)
        body=Column(dialog);body.setContentsMargins(20,20,20,20);body.setSpacing(16)
        body.addWidget(label('Standard API 단가 × 구독 차감 배율','section'))
        prices=table(['모델','일반 입력','캐시 읽기','캐시 쓰기','출력','Fast 배수'])
        for i,width in enumerate((310,135,135,135,135,160)):prices.setColumnWidth(i,width)
        rows=[]
        for model in (*SUPPORTED_MODELS, 'gpt-daybreak-blue-latest'):
            rate=standard_rate(model);multiplier=subscription_multiplier(model,'Fast')
            rows.append([model,usd(rate.input),usd(rate.cached) if rate.cached is not None else '미지원',
                         usd(rate.written) if rate.written is not None else '입력 단가',usd(rate.output),
                         f'×{multiplier:g}' if multiplier is not None else '미지원'])
        fill(prices,rows);body.addWidget(prices,1)
        body.addWidget(label('Standard 1배 / 장문 할증 제외 / USD / 100만 토큰','muted',True))
        body.addWidget(label(f'API 단가 확인 {VERIFIED} / GPT-6.1 Sol 단가 확인 {rate_verified("gpt-6.1-sol")}\n구독 배율 확인 {MULTIPLIER_VERIFIED}','muted',True))
        body.addWidget(label('GPT-5.6 Sol 프로모션 확인 기한 2026-11-21','muted',True))
        body.addWidget(label('가격 별칭: '+' / '.join(f'{alias} → {model}' for alias,model in ALIASES.items()),'muted',True))
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
            self.settings_page.add_row('notifications', kind, {'HTTP 전환': '연결 문제로 통신 방식이 전환되면 알립니다.', '캐시 저하 의심': '캐시 미적중이 반복되거나 읽기량이 감소하면 알립니다. 판단 기준은 잠정값입니다.', '모델명 불일치': '완료된 응답에서 요청 모델과 응답 모델이 다르면 알립니다.', '프록시 장애': '복구가 필요한 장애와 자동 보호 정지를 알립니다. 일시적인 지연은 제외합니다.'}[kind], option,section='전체 알림',depth=1,aliases=description)
            if key=='cache_drop':
                self.cache_scope=Choice()
                self.cache_scope.addItem('전체 세션','all');self.cache_scope.addItem('현재 선택 세션','selected')
                self.cache_scope.setCurrentIndex(max(0,self.cache_scope.findData(self.settings.value('notifications/cacheScope','all'))))
                self.cache_scope.currentIndexChanged.connect(lambda *_:self.settings.setValue('notifications/cacheScope',self.cache_scope.currentData()))
                self.settings_page.add_row('notifications','캐시 알림 범위','',self.cache_scope,section='전체 알림',depth=2)
        cache_enabled=lambda *_:self.confirmed_notifications.enable_cache(self.notification_master.isChecked() and self.notification_options['캐시 저하 의심'].isChecked())
        self.notification_master.toggled.connect(cache_enabled)
        self.notification_options['캐시 저하 의심'].toggled.connect(cache_enabled)
        cache_enabled()
        http_enabled=lambda *_:self.confirmed_notifications.enable_http(self.notification_master.isChecked() and self.notification_options['HTTP 전환'].isChecked())
        self.notification_master.toggled.connect(http_enabled);self.notification_options['HTTP 전환'].toggled.connect(http_enabled);http_enabled()
        for kind,handler in (('모델명 불일치',self.confirmed_notifications.enable_model),
                             ('프록시 장애',self.confirmed_notifications.enable_proxy)):
            handler(self.notification_options[kind].isChecked())
            self.notification_options[kind].toggled.connect(handler)
        self.observer_panel.status_observed.connect(self.receive_proxy_status)
        self.notification_log=table(['시각','알림','건수','확인 근거'],readable=False)
        self.notification_log.setFixedHeight(102)
        self.notification_log.put(inline=True,rowHeight=32,columnStretchWeights=[0,1,0,2],leftColumns=[0,1,3],emptyText='이번 실행에서 발생한 알림 없음')
        for column,width in enumerate((175,130,48,110)):self.notification_log.setColumnWidth(column,width)
        self.notification_log.cellClicked.connect(self.open_notification_record);self.notification_log.cellActivated.connect(self.open_notification_record)
        from .error_panel import ErrorPanel
        self.error_panel=ErrorPanel();self.error_panel.action_requested.connect(self.perform_error_action)
        self.settings_page.add_widget('notifications',self.error_panel,section='현재 오류',title='현재 오류',aliases='errors current status')
        self.settings_page.add_widget('notifications', self.notification_log,section='최근 알림',title='최근 알림',aliases='notification history')
        def sync_notification_controls(*_):
            for option in self.notification_options.values():option.setEnabled(self.notification_master.isChecked())
            self.cache_scope.setEnabled(self.notification_master.isChecked() and self.notification_options['캐시 저하 의심'].isChecked())
        self.notification_master.toggled.connect(sync_notification_controls)
        self.notification_options['캐시 저하 의심'].toggled.connect(sync_notification_controls)
        sync_notification_controls()
        self.settings_page.add_row('about','현재 버전','',SettingsPage.version_link(VERSION),section='버전')
        from .update_panel import UpdatePanel
        self.update_panel=UpdatePanel(self.observer_panel.manager,self)
        self.auto_update=Switch()
        self.auto_update.setChecked(self.settings.value('updates/automatic',True,type=bool))
        self.auto_update.toggled.connect(lambda value:self.settings.setValue('updates/automatic',value))
        self.settings_page.add_row('about','업데이트 자동 확인',
            '앱 실행 중 6시간 간격으로 새 버전을 확인합니다.',
            self.auto_update,section='버전',aliases='automatic update',depth=1)
        self.settings_page.add_widget('about',self.update_panel,section='업데이트',title='업데이트 확인',target=self.update_panel.button,aliases='정보 문제 해결 update')
        self.recovery_button=SettingsPage.shortcut('연결 복구',self.update_panel.open_recovery)
        self.recovery_status=Text();self.recovery_status.setWordWrap(True)
        self.update_panel.recovery_status=self.recovery_status
        self.settings_page.add_row('integration','연결 복구','연결에 문제가 있으면 복구 도구를 여세요.',self.recovery_button,section='프록시',aliases='recovery repair connection information 연결 정보',depth=1)
        self.settings_page.add_widget('integration',self.recovery_status,section='프록시')
        self.observer_panel.status_observed.connect(self.update_panel.proxy_status)
        self.observer_panel.progress_requested.connect(self.open_updates)
        self.update_panel.attention_changed.connect(self.update_attention)
        self.update_panel.fault_changed.connect(self.receive_update_error)
        self.settings_page.add_row('about','제작자','',Text('jisoq'),section='제작자')
        from .i18n import language
        guide='README.ko.md' if language()=='ko' else 'README.md'
        self.guide_link=SettingsPage.shortcut('사용 안내',lambda:QDesktopServices.openUrl(QUrl('https://github.com/jisoq/Codexon/blob/main/'+guide)))
        self.repository_link=SettingsPage.shortcut('저장소',lambda:QDesktopServices.openUrl(QUrl('https://github.com/jisoq/Codexon')))
        self.settings_page.add_row('about','사용 안내','',self.guide_link,section='제작자')
        self.settings_page.add_row('about','저장소','',self.repository_link,section='제작자',aliases='GitHub')

    def update_attention(self,needed):
        self.settings_page.update_attention(needed)
        if not needed or not self.notification_master.isChecked():return
        record=self.update_panel.record
        if record.get('phase')!='needs_exit':return
        marker=str(record.get('operation_id',''))+':'+record['phase']
        if self.settings.value('updates/actionNotified','')==marker:return
        self.settings.setValue('updates/actionNotified',marker)
        self.show_tray_notification('update',tr('업데이트 확인 필요'),
            tr('설정의 앱 정보에서 다음 작업을 확인하세요.'),QSystemTrayIcon.Warning)

    def receive_proxy_status(self,result):
        from .connection_recovery import assess
        status={**result,'configured':bool(result.get('configured'))}
        assessed=assess(status)
        events=self.confirmed_notifications.proxy(status,store=False)
        self.observe_error('connection.status','connection',
            {**status,'notification_confirmed':bool(events)},
            healthy='configured' in result and assessed['code'] in ('direct','responding'),
            resolution='프록시 사용 해제' if assessed['code']=='direct' else '정상 식별 응답 확인')
        self.observe_error('connection.operation','connection',dict(operation_failed=bool(result.get('operation_failed'))),
            healthy=bool(status.get('operation_healthy')),resolution='연결 설정 적용 확인')
        health=status.get('health')
        if isinstance(health,dict) and health:
            counters=(health.get('instance'),health.get('observation_dropped',0),health.get('storage_written',0))
            previous=self._observation_counters
            same_instance=previous is not None and previous[0]==counters[0]
            previous_dropped=previous[1] if same_instance else counters[1]
            written=bool(previous and (counters[2]>previous[2] if same_instance else counters[2]>0))
            self._observation_counters=counters
            self.observe_error('observation.health','observation',dict(health=health,previous_dropped=previous_dropped),
                healthy=bool(written and counters[1]==previous_dropped and health.get('storage_failure_streak',0)==0
                    and health.get('observation_enabled') is not False),resolution='관측 기록 저장 재개 확인')
        elif status.get('configured') is False and status.get('phase') in ('off','faulted','prepared','validated'):
            self.error_state.clear('observation.health',resolution='모델 관측 종료 확인')
        incident=result.get('incident') or {}
        if incident.get('id') and result.get('phase') in ('faulted','recovery_failed'):
            fresh=incident['id']!=self.settings.value('notifications/lastProtectionId','')
            self.settings.setValue('notifications/lastProtectionId',incident['id'])
            kind='observation' if incident.get('kind')=='observation' and not incident.get('recovery_error') else 'connection'
            self.observe_error(kind+'.protection',kind,{**status,'protection_stopped':True,
                'notification_confirmed':fresh})
        elif not incident and status.get('phase') in ('off','prepared','validated','active'):
            for kind in ('connection','observation'):
                self.error_state.clear(kind+'.protection',resolution='연결 설정 변경 확인')
        self.refresh_errors()

    def show_confirmed_events(self,events):
        for event in events:
            count=f" / {event['count']}건" if event['count']>1 else ''
            if self.notification_master.isChecked():
                if event['kind']=='cache_miss':
                    overlay=getattr(self,'overlay',None)
                    scope=self.settings.value('notifications/cacheScope','all')
                    if scope=='selected' and (not overlay or not overlay.selection_confirmed() or not overlay.selected_scope(event.get('home'),event.get('sid'))):
                        continue
                    if overlay and overlay.can_present(event.get('home'),event.get('sid')):
                        continue
                self.show_tray_notification('details','Codexon: '+tr(event['title']),
                    event['detail'].split('\n')[0]+count,QSystemTrayIcon.Warning)
        self.refresh_notification_log()

    def open_notification_record(self,index,*_):
        if not 0<=index<len(self.notification_log.model().rows):return
        event=self.notification_log.model().rows[index]
        if event.get('kind')=='app_error':
            self.open_errors();self.error_panel.show_record(event)
        elif event.get('target'):self.navigate(event['target'])
        else:self.settings_page.reveal('integration',self.recovery_button)
