"""Full dashboard: shared evidence, explicit populations and reversible navigation."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QSettings, QEvent, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon
from .presentation import batched_updates, Button, Choice, Column, Group, Navigation, Row, Scroll, Stack, Text
from .table_model import LazyTable
from .quick_runtime import Dialog, DialogButtons
from .tray import TrayWindow, tray_icon
from .analytics import analyze, project_choices, project_key
from .analysis_engine import AnalysisEngine, BoundedCache, retained_bytes
from .analysis_worker import AnalysisBridge
from .pricing import usd, ALIASES, FAST_MULTIPLIER, MULTIPLIER_VERIFIED, MODEL_VERIFIED, standard_rate, subscription_multiplier, rate_verified, SUPPORTED_MODELS, request_tier
from .observer_panel import ObserverPanel
from .notifications import ConfirmedNotifications
from .theme import shared_theme
from .version import VERSION
from .i18n import tr, Verbatim, formatted
from .preparation_progress import PreparationPending, PreparationStage, PreparationWork, preparation_loading

STYLE = ''
TITLES = {1:'지표 비교',3:'구독 모니터링',4:'설정',5:'성능 추이',6:'홈',7:'모델명 불일치 기록'}
PAGE_INDEX = {page: index for index, page in enumerate(TITLES)}


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


class Dashboard(TrayWindow):
    NAVIGATION_LIMIT=64


    def __init__(self, homes, start_worker=True, settings=None, index_path=None, static_snapshot=None, live_limits=True, manage_observer=False,model_evidence_path=None,quota_path=None,collection_autostart=True,mismatch_reader=None,installed_update=None):
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
        self.preparation=(PreparationPending(time.monotonic(),PreparationStage.STARTING)
                          if start_worker and static_snapshot is None else None)
        self.analysis=analyze([]);self.analysis_pending=False;self.analysis_errors=[]
        self.client_cache=BoundedCache(8,64*1024*1024);self.request_id=0;self.view_result=None;self.applied_key=None
        self.display_identity=None;self.pending_retained=False;self.displayed_pages={}
        self.lookup={'sessions':{},'turns':{},'session_turns':{}};self.page_frames={}
        self.started_at=time.time();self.alerted={};self.confirmed_notifications=ConfirmedNotifications()
        from .app_errors import ErrorState
        self.error_state=ErrorState(history=self.confirmed_notifications.records)
        self._error_revision=-1;self.collection_starting=False;self.collection_stopped=False;self._observation_counters=None
        self.collection_snapshot_at=None
        self.current_page=6;self.restoring=True;self.back_stack=[];self.forward_stack=[]
        self.home_quota_payload={};self.home_collection_status={}
        self.filter_values={}
        self.interaction_time=0;self.deferred_result=None;self.defer_timer=QTimer(self)
        self.defer_timer.setSingleShot(True);self.defer_timer.timeout.connect(self.apply_deferred)
        self.slow_timer=QTimer(self);self.slow_timer.setSingleShot(True);self.slow_timer.setInterval(5000)
        self.slow_timer.timeout.connect(self.show_analysis_slow)
        self.analysis_clock=QTimer(self);self.analysis_clock.setInterval(1000)
        self.analysis_clock.timeout.connect(self.update_analysis_elapsed)
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
        brand=Row();brand.setSpacing(8)
        logo=Group();logo.put(kind='brandLogo');logo.setFixedSize(24,24)
        brand.addWidget(logo);brand.addWidget(label('CODEXON','brand'));side_layout.addWidget(brand)
        self.navigation_pages=(6,3,1,5,7)
        self.nav=Navigation();self.nav.addItems(tuple(TITLES[i] for i in self.navigation_pages));self.nav.setObjectName('navigation');self.nav.setMaximumHeight(250)
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
        header=Row()
        self.heading=label(TITLES[self.current_page],'heading');header.addWidget(self.heading);header.addStretch()
        self.refresh_status=Group();self.refresh_status.put(kind='statusIcon',iconName='restart')
        self.refresh_status.setObjectName('analysis-refresh-status');self.refresh_status.setFixedSize(28,28)
        self.refresh_status.setAccessibleName('최신 기록 반영 중');self.refresh_status.hide();header.addWidget(self.refresh_status)
        self.refresh_text=label('','muted');self.refresh_text.put(noElide=False)
        self.refresh_text.setMaximumWidth(260);self.refresh_text.hide();header.addWidget(self.refresh_text)
        self.retry_button=Button('다시 시도');self.retry_button.clicked.connect(self.retry_analysis);self.retry_button.hide();header.addWidget(self.retry_button)
        self.price_button=Button('API 가격표');self.price_button.put(role='quiet');self.price_button.clicked.connect(self.show_prices);header.addWidget(self.price_button);layout.addLayout(header)
        self.pages=Stack();self.pages.put(deferPages=True);layout.addWidget(self.pages,1);self.scrollers={}
        self.analysis_loading={};self._loading_nodes=[]
        self.build_compare()
        from .quota_panel import QuotaPanel
        self.quota_panel=QuotaPanel(self.settings);quota_scroll=Scroll();quota_scroll.put(fillViewport=True)
        self.quota_panel.centralized_errors=True
        self.quota_panel.set_presentation_active(False)
        self.quota_panel.retry_requested.connect(self.retry_quota)
        self.quota_panel.fault_changed.connect(lambda facts:self.observe_error('quota.query','quota',facts,
            healthy=bool(facts.get('query_healthy')),resolution='사용 한도 분석 결과 갱신 확인'))
        quota_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);quota_scroll.setWidget(self.quota_panel);self.pages.addWidget(quota_scroll)
        self.scrollers[3]=quota_scroll;self.build_settings()
        from .performance_panel import PerformancePanel
        self.performance_panel=PerformancePanel(self)
        self.pages.addWidget(self.performance_panel);self.scrollers[5]=self.performance_panel.chart_scroll
        from .home_panel import HomePanel
        self.home_panel=HomePanel();self.home_panel.navigate.connect(self.change_page)
        self.home_panel.connection_requested.connect(self.open_home_connection)
        home_scroll=Scroll();home_scroll.put(fillViewport=True);home_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        home_scroll.setWidget(self.home_panel);self.pages.addWidget(home_scroll);self.scrollers[6]=home_scroll
        from .mismatch_page import MismatchPage
        self.mismatch_page=MismatchPage(reader=mismatch_reader);self.pages.addWidget(self.mismatch_page)
        self.health=label('','muted',True);self.health.hide()
        self.setup_tray();self.settings_page.bind_tray(self);self.tray.messageClicked.connect(self.open_tray_notification)
        self.tick=QTimer(self);self.tick.timeout.connect(self.check_stale);self.tick.start(1000)
        self.nav.currentRowChanged.connect(lambda row:self.change_page(self.navigation_pages[row]) if row>=0 else None)
        from .quota_layout import configure_monitoring_controls
        configure_monitoring_controls(self)
        self.restore_preferences();self.restoring=False;self.change_page(self.current_page)
        if start_worker:
            if homes and static_snapshot is None:self.start_quota_service(homes[0])
            self.worker=AnalysisBridge(homes,index_path,static_snapshot,model_evidence_path,quota_path,
                                       cache_floor=self.confirmed_notifications.cache_floor)
            self.worker.snapshot.connect(self.receive);self.worker.result.connect(self.analysis_ready)
            self.mismatch_page.set_worker(self.worker)
            self.worker.progress.connect(self.analysis_progressed)
            # The initial page can enter loading before a worker owns its request.
            self.pending_logical=None
            self.worker.fault.connect(self.analysis_failed);self.worker.start();self.render()
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
            homes,index_path,model_evidence_path,collection=bool(start_worker and collection_autostart and static_snapshot is None),installed_update=installed_update)
        self.service_controller=ServiceController(self.app_services,self)
        self.observer_panel.services=self.app_services if manage_observer else None
        self.service_controller.result.connect(self.observer_panel.display)
        self.service_controller.result.connect(self.receive_collection_status)
        self.service_controller.collection_observed.connect(self.receive_collection_observation)
        if manage_observer or self.app_services.collection:
            QTimer.singleShot(0,self.service_controller.start)

    def layout_sidebar(self):
        compact=self.height()<800
        self.sidebar_layout.setSpacing(4 if compact else 24)
        self.sidebar_layout.setContentsMargins(16,8 if compact else 24,16,8 if compact else 20)
        self.sidebar_footer_layout.setSpacing(4 if compact else 8)

    def fit_screen(self):
        screen=self.screen() or QApplication.primaryScreen()
        if screen is None:return
        work=screen.availableGeometry()
        self.setMinimumSize(min(1000,work.width()),min(680,work.height()))
        if self.windowState() & (Qt.WindowMinimized|Qt.WindowMaximized|Qt.WindowFullScreen):return
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
                service.updated.connect(lambda value,expected=service: self.receive_quota(value) if self.quota_service is expected else self.quota_panel.receive_activity(value))
                service.start()
        if home not in services:
            services[home]=QuotaService(home,self.index_path,live=self.live_limits,quota_path=self.quota_path,
                tracking_enabled=self.settings.value('quota/trackingEnabled',True,type=bool),reports_enabled=False,
                paused=self.quota_paused,paused_at=self.quota_paused_at)
            services[home].updated.connect(lambda value,expected=services[home]:self.receive_quota(value) if self.quota_service is expected else self.quota_panel.receive_activity(value))
            services[home].start()
        services[home].supply_local(self.snapshot.get('quota_by_home',{}).get(services[home].home))
        self.live_quota=None;self.quota_service=services[home]
        self.quota_sidebar.receive({'quota':None})
        self.home_quota_payload={};self.refresh_home_status()
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
        self.home_quota_payload=value;self.refresh_home_status()
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

    def retry_quota(self):
        if self.quota_service:
            self.quota_service.retry_report()
            self.quota_panel.receive(dict(home=self.quota_service.home,quota=self.quota_panel.quota,report_pending=True))

    def sync_report_demand(self):
        if not hasattr(self,'quota_panel'):return
        visible=self.isVisible() and not self.isMinimized()
        page=getattr(self,'current_page',3)
        if self.worker is not None and hasattr(self.worker,'cancel_queries') and (not visible or page in (3,4,7)):
            self.worker.cancel_queries()
            if self.analysis_pending:
                self.request_id+=1;self.analysis_pending=False
                self.deferred_result=None;self.defer_timer.stop()
                self.clear_analysis_loading();self._display_dirty=True
        if visible and getattr(self,'_display_dirty',False):
            self._display_dirty=False;QTimer.singleShot(0,lambda:self.render(automatic=True))
        self.quota_panel.set_presentation_active(visible and page==3)
        for service in getattr(self,'quota_services',{}).values():
            service.set_reports_enabled(visible and page==3 and service is self.quota_service)

    def scroll_page(self,index):
        area=Scroll();area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);body=Group();layout=Column(body)
        layout.setContentsMargins(0,0,8,0);layout.setSpacing(24);area.setWidget(body);self.pages.addWidget(area);self.scrollers[index]=area
        return layout

    def build_compare(self):
        from .usage_panel import UsagePanel
        self.metric_panel=UsagePanel(self);self.pages.addWidget(self.metric_panel)


    def restore_preferences(self):
        try:
            saved=json.loads(self.settings.value('dashboard/state','{}'))
            if saved:self.restore_state(saved,initial=True)
        except (ValueError,TypeError,KeyError):pass

    def capture_state(self):
        return dict(design_version=4,metric_workspace=self.metric_panel.capture(),page=self.current_page,
            settings_category=self.settings_page.current_category(),
            scrolls={str(k):v.verticalPosition.value() for k,v in self.scrollers.items()},
            settings_scrolls=self.settings_page.capture_scrolls())

    def restore_state(self,state,initial=False):
        previous=self.restoring;self.restoring=True
        self.metric_panel.restore(state.get('metric_workspace',{}))
        page=state.get('page',6)
        self.current_page=page if page in TITLES else 6
        if initial and self.current_page==4:self.current_page=6
        self.settings_page.reveal(state.get('settings_category','general'))
        self._restore_positions=state
        self.restoring=previous
        if not initial:
            self.change_page(self.current_page)
            for key,value in state.get('scrolls',{}).items():
                if int(key) in self.scrollers:self.scrollers[int(key)].verticalPosition.setValue(value)
            self.settings_page.restore_scrolls(state.get('settings_scrolls',{}))
            if self.current_page in (3,4):self._restore_positions=None

    def save_preferences(self):
        if self.restoring:return
        state=self.capture_state()
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

    def go_back(self):
        if not self.back_stack:return
        self.remember_navigation(self.forward_stack)
        self.restore_state(self.back_stack.pop())

    def go_forward(self):
        if not self.forward_stack:return
        self.remember_navigation(self.back_stack)
        self.restore_state(self.forward_stack.pop())


    def refresh_choices(self):
        choices=self.snapshot.get('filter_choices',{})
        sources=self.snapshot['sessions']
        rows=([r for s in sources for r in s.get('history',[])]
              if any(key not in choices for key in ('combinations','models','efforts','modes')) else [])
        projects=(project_choices(sources)
                  if any(key not in choices for key in ('projects','project_labels','project_paths')) else choices)
        self.filter_values={
            'model':choices['models'] if 'models' in choices else self.snapshot.get('models') or sorted({r.get('model') or '미확인' for r in rows}),
            'effort':choices['efforts'] if 'efforts' in choices else sorted({r.get('effort') or '미확인' for r in rows}),
            'service_tier':choices['modes'] if 'modes' in choices else sorted({request_tier(r) for r in rows}),
            'project':choices.get('projects',projects['projects']),
            'source':choices['sources'] if 'sources' in choices else sorted({s.get('source') or 'unknown' for s in sources}),
            'home':choices.get('homes',self.snapshot['homes'])}
    def change_page(self,index):
        if self.restoring:return
        if index not in TITLES:index=6
        if self.current_page!=index:self.forward_stack.clear()
        self.current_page=index;self.pages.setCurrentIndex(PAGE_INDEX[index]);self.heading.setText(TITLES[index])
        self.sync_report_demand()
        self.restoring=True
        self.nav.setCurrentRow(self.navigation_pages.index(self.current_page) if self.current_page in self.navigation_pages else -1)
        self.settings_button.put(checked=self.current_page==4)
        self.quota_sidebar.setVisible(index!=6)
        self.restoring=False
        self.mismatch_page.set_active(index==7)
        self.content.put(contentWidth=0 if index==7 else 1808)
        self.request_id+=1;self.analysis_pending=False;self.deferred_result=None;self.defer_timer.stop()
        displayed=self.displayed_pages.get(self.current_page)
        self.display_identity=displayed[0] if displayed else None
        if displayed:
            self.view_result=displayed[1]
            if self.current_page!=5:
                self.analysis=self.view_result['analysis'];self.lookup=self.view_result['lookup']
                self.applied_key=self.view_result['key']
        self.price_button.setVisible(index not in (4,6,7));self.clear_analysis_loading()
        if hasattr(self,'quota_pause_button'):self.refresh_quota_monitoring()
        self.render();self.sync_preparation();self.save_preferences()
        if index==5:QTimer.singleShot(0,self.update_performance_layout)

    def open_home_connection(self):
        self.push_state();self.change_page(4)
        self.settings_page.reveal('integration',self.observer_panel.toggle)

    def refresh_home_status(self):
        if hasattr(self,'home_panel'):
            evidence={**self.snapshot,**self.home_collection_status}
            evidence['collection_started_at']=self.started_at
            source_observed=(self.home_collection_status.get('collection_observation') or {}).get('source_fault') is not None
            evidence['collection_fault']={**({} if source_observed else self.snapshot.get('collection_fault') or {}),
                **{key:value for key,value in (self.home_collection_status.get('collection_fault') or {}).items() if value}}
            self.home_panel.apply_status(evidence,self.home_quota_payload,time.time(),preparing=self.preparation is not None)
            from .app_errors import collection_phase
            phase=collection_phase(evidence,time.time(),started_at=self.started_at)
            self.quota_panel.set_collection_phase(phase)
            self.refresh_collection_wait(phase)

    def update_performance_layout(self):
        panel=getattr(self,'performance_panel',None)
        quick=getattr(self,'_quick',None)
        if panel is None or not hasattr(panel,'update_layout') or quick is None:return
        pending=[quick.quickWindow().contentItem()]
        while pending:
            item=pending.pop()
            if item.property('node') is self.pages and item.width()>0:
                panel.update_layout(item.width());return
            pending.extend(item.childItems())

    def open_settings(self):self.show_window();self.change_page(4)

    def open_errors(self):
        self.open_settings();self.settings_page.reveal('notifications',self.error_panel)

    def perform_error_action(self,kind,page):
        if kind=='analysis':
            if self.worker is not None and not self.worker.isRunning():
                self.worker.restart_count=0;self.worker.start()
            self.client_cache.clear();self.change_page(getattr(self,'_analysis_error_page',1))
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
        from .update_state import ACTIVE
        checking=bool(facts.get('checking_failed') or facts.get('check_healthy'))
        operation=bool(facts.get('operation_error') or facts.get('operation_healthy'))
        current=bool(facts.get('status_current'))
        resolution=('업데이트 재시도 중' if facts.get('phase') in ACTIVE else
                    '업데이트 취소 확인' if facts.get('phase')=='cancelled' else '업데이트 상태 확인')
        self.observe_error('update.check' if checking else 'update.operation' if operation else 'update.status','update',facts,
            healthy=bool(facts.get('check_healthy') or facts.get('operation_healthy') or facts.get('status_healthy') or current),
            resolution=resolution)
        if facts.get('status_healthy') or current:
            self.error_state.clear('update.operation',resolution=resolution)
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
        if not release or self.update_panel.auto_start:return
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
        if page==6:return self.home_panel.query(time.time())
        if page==5:
            panel=self.performance_panel
            return dict(page=5,start=0,end=self.snapshot['ts']+.000001,now=self.snapshot['ts'],period='all',presentation=True,
                        time_range=panel.query_range(self.snapshot['ts'],self.snapshot.get('data_revision',self.snapshot['ts'])),
                        trend_period=panel.period_preset,trend_mode=panel.trend_mode,
                        trend_selection=dict(models=sorted(m for m,enabled in panel.models.items() if enabled) if panel.models else None,
                                             modes=sorted(m for m,enabled in panel.modes.items() if enabled)),
                        plot_width=max(64,min(1024,self.width()-96)),granularity=panel.granularity)
        if page==1:return self.metric_panel.query()
        raise ValueError('this page has no analytical query')

    def render(self,*_,automatic=False):
        if self.restoring or not hasattr(self,'settings_page'):return
        if (self.async_mode or automatic) and (not self.isVisible() or self.isMinimized()):
            self._display_dirty=True;return
        if self.current_page==7:
            self.mismatch_page.refresh(automatic=automatic)
            return
        if self.current_page in (3,4):
            if self.current_page==3:self.quota_panel.refresh_status()
            return
        q=self.query()
        if self.async_mode:
            key_query={k:v for k,v in q.items() if k!='now' and (q['period']=='custom' or k not in ('start','end'))}
            if q['page']==5 and self.performance_panel.follow_latest:
                # Heartbeats move the requested endpoint without changing calls.
                # Keep each relative window until its analytical expiry boundary.
                key_query['time_range']=('latest',self.performance_panel.window_span)
            revision=self.snapshot.get('data_revision',0)
            if q.get('category_usage'):
                key_query.pop('loading',None)
                revision=self.snapshot.get('call_revision',revision)
            if q['page']==6:key_query['start']=q['start']
            logical=(self.snapshot.get('epoch'),revision,json.dumps(key_query,sort_keys=True))
            if self.analysis_pending:
                if automatic:
                    self._refresh_after_pending=getattr(self,'pending_logical',None)!=logical
                    return
                if getattr(self,'pending_logical',None)==logical:
                    if self.pending_automatic:
                        self.pending_automatic=False;self.show_analysis_pending()
                        if self.analysis_loading:self.slow_timer.start()
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
            if cached and q['page']==5:
                # An expired result still belongs to this exact selected window
                # and generation. Keep its original bounds while refreshing.
                self.apply_result(cached[0],q,automatic)
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
        if query['page']==6:identity['start']=query['start']
        if query['page']==5 and self.performance_panel.follow_latest:
            identity['time_range']=('latest',self.performance_panel.window_span)
        return json.dumps(identity,sort_keys=True)


    def clear_analysis_loading(self):
        self.analysis_clock.stop()
        self.slow_timer.stop();self.refresh_status.hide();self.retry_button.hide()
        self.set_analysis_loading({})
        self.metric_panel.put(queryPending=False)
        self.pages.show()

    def analysis_regions(self):
        if self.current_page==6:return [(self.home_panel.today_summary,True)]
        if self.current_page==1:return [(self.metric_panel,True)]
        if self.current_page==5:
            return [(self.performance_panel.chart_scroll,True),(self.performance_panel.inspector,False),
                    (self.performance_panel.window_note,False)]
        return []

    @batched_updates
    def set_analysis_loading(self,state):
        regions=self.analysis_regions() if state else []
        nodes=[node for node,_ in regions]
        for node in self._loading_nodes:
            if node not in nodes:node.put(analysisLoading={})
        self._loading_nodes=nodes;self.analysis_loading=state
        self.update_refresh_status()
        for node,indicator in regions:
            region=dict(state,indicator=indicator)
            node.put(analysisLoading=region)

    def update_refresh_status(self):
        state=self.analysis_loading
        if self.preparation is not None and state and not (state.get('retained') and state.get('mode')=='error'):
            self.refresh_text.setWordWrap(True);self.refresh_text.put(noElide=True,maxWidth=16777215)
            visible=not self.analysis_regions() or bool(state.get('retained'))
            running=visible and bool(state.get('running'))
            self.refresh_status.setVisible(running);self.refresh_status.put(running=running)
            self.refresh_text.setVisible(visible)
            text=' / '.join(filter(None,(state.get('title'),state.get('progressText'),state.get('elapsedText'))))
            description='\n'.join(filter(None,(text,state.get('detail'),state.get('guidanceText'))))
            self.refresh_text.setText(Verbatim(text));self.refresh_text.setAccessibleName(Verbatim(description))
            self.refresh_status.setAccessibleName(Verbatim(state.get('title','')))
            self.refresh_status.setToolTip(Verbatim(description))
            return
        self.refresh_text.setWordWrap(False);self.refresh_text.put(noElide=False,maxWidth=260)
        self.refresh_text.setAccessibleName('');self.refresh_status.setAccessibleName('최신 기록 반영 중')
        self.refresh_status.setToolTip('')
        refreshing=self.current_page!=3 and bool(state.get('active') and state.get('retained'))
        failed=state.get('mode')=='error' and state.get('retained')
        self.refresh_status.setVisible(refreshing and not failed)
        self.refresh_status.put(running=refreshing and not failed)
        self.refresh_text.setVisible(bool(refreshing or failed))
        if failed:
            self.refresh_text.setText('갱신 실패, 이전 결과 표시 중')
            return
        if refreshing:
            detail=(tr('기록 처리에 시간이 걸리고 있습니다. 다른 탭을 이용할 수 있습니다.') if state.get('slow') else state.get('detail',''))
            progress=state.get('progressText','')
            elapsed=state.get('elapsedText','')
            self.refresh_status.setToolTip('\n'.join(filter(None,(tr('최신 기록 반영 중'),detail,progress,elapsed))))
            self.refresh_text.setText(Verbatim(' / '.join(filter(None,(progress or tr('최신 기록 반영 중'),elapsed)))))

    def begin_analysis_loading(self,query):
        self.clear_analysis_loading()
        self.analysis_started=time.monotonic()
        self.pending_retained=(self.display_identity==self.query_identity(query)
                               and self.pages.isPageReady(self.pages.currentIndex()))
        self.metric_panel.put(queryPending=query['page']==1)
        self.set_analysis_stage('queued')
        self.show_analysis_pending()
        if self.analysis_loading:
            self.slow_timer.start();self.analysis_clock.start();self.update_analysis_elapsed()

    def update_analysis_elapsed(self):
        if self.preparation is not None:
            self.sync_preparation();return
        if not self.analysis_loading or not self.analysis_loading.get('running'):return
        if not self.analysis_pending and not self.analysis_loading.get('collectionWait'):return
        elapsed=max(0,int(time.monotonic()-self.analysis_started))
        self.set_analysis_loading(dict(self.analysis_loading,
            elapsedText=formatted('{seconds}초 경과',seconds=elapsed),slow=bool(self.analysis_loading.get('slow') or elapsed>=5)))

    def set_analysis_stage(self,stage):
        stages={'queued':(0,'분석에 사용할 기록을 준비하고 있습니다.'),
                'collecting':(0,'저장된 Codex 사용 기록을 읽고 있습니다.'),
                'converting':(0,'확인한 기록으로 사용량을 계산하고 있습니다.'),
                'publishing':(0,'확인한 사용량을 화면에 반영하고 있습니다.'),
                'query':(1,'선택한 조건에 맞는 사용 기록을 읽고 있습니다.'),
                'aggregate':(2,'조회한 기록으로 지표를 계산하고 있습니다.'),
                'present':(3,'화면의 구성 요소를 준비하고 있습니다.')}
        self.analysis_stage=stage
        if self.preparation is not None:
            self.sync_preparation();return
        if self.analysis_loading:
            state=dict(self.analysis_loading)
            if state.get('stageName')!=stage:
                for key in ('processed','total','progressText'):state.pop(key,None)
            position,detail=stages.get(stage,stages['queued'])
            self.set_analysis_loading(dict(state,stageName=stage,stage=position,detail=tr(detail)))

    @batched_updates
    def analysis_progressed(self,message):
        if isinstance(message,(PreparationStage,PreparationWork)):
            overlay=getattr(self,'overlay',None)
            if overlay is not None and overlay.preparation is not None:
                overlay.receive_preparation(replace(overlay.preparation,progress=message))
            if self.preparation is not None:
                self.preparation=replace(self.preparation,progress=message)
                self.sync_preparation()
            return
        if message.get('id')==self.request_id and self.analysis_pending:
            self.set_analysis_stage(message.get('stage'))
            if self.preparation is not None:return
            done=message.get('processed');total=message.get('total')
            if type(done) is int and done>=0 and self.analysis_loading:
                templates={'calls':'처리한 호출 {count}건','intervals':'계산 구간 {count}',
                           'sessions':'확인한 세션 {count}개','files':'확인한 파일 {count}개'}
                count=f'{done:,} / {total:,}' if type(total) is int and total>=done else f'{done:,}'
                detail=formatted(templates.get(message.get('unit'),templates['calls']),count=count)
                calls=message.get('calls')
                if type(calls) is int and calls>=0:
                    detail=formatted(templates['calls'],count=f'{calls:,}')+' / '+detail
                self.set_analysis_loading(dict(self.analysis_loading,processed=done,
                    total=total if type(total) is int and total>=done else None,
                    progressText=detail))

    def show_analysis_message(self,title,detail,retry=False):
        self.pages.show()
        self.set_analysis_loading(dict(active=True,retained=False,running=not retry,
            mode='error' if retry else 'loading',title=tr(title),detail=tr(detail)))

    def show_analysis_pending(self):
        if self.preparation is not None:
            self.sync_preparation();return
        if not self.analysis_pending or self.current_page in (3,4):return
        if self.pending_retained:
            self.set_analysis_loading(dict(active=True,retained=True,running=True,title=tr('최신 기록 반영 중'),detail=''))
            self.set_analysis_stage(self.analysis_stage)
        else:
            self.show_analysis_message(formatted('{page} 불러오는 중',page=tr(TITLES[self.current_page])),
                                      '')
            self.set_analysis_stage(self.analysis_stage)

    def show_analysis_slow(self):
        if self.preparation is not None:return
        if not self.analysis_loading or not self.analysis_loading.get('running') or self.current_page in (3,4):return
        self.set_analysis_loading(dict(self.analysis_loading,slow=True))

    def finish_analysis_loading(self,result,query):
        waiting_since=getattr(self,'collection_wait_started',None)
        self.analysis_pending=False;self.clear_analysis_loading()
        if self.snapshot.get('index',{}).get('loading') and not result['analysis']['response_count']:
            self.display_identity=None
            self.displayed_pages.pop(self.current_page,None)
            self.show_analysis_message('사용 기록 수집 중','Codex 사용 기록을 읽고 있습니다.')
            self.collection_wait_started=waiting_since if waiting_since is not None else time.monotonic()
            self.analysis_started=self.collection_wait_started
            self.set_analysis_loading(dict(self.analysis_loading,collectionWait=True))
            self.set_analysis_stage('queued')
            self.analysis_clock.start();self.slow_timer.start();self.update_analysis_elapsed()
            from .app_errors import collection_phase
            self.refresh_collection_wait(collection_phase({**self.snapshot,**self.home_collection_status},time.time(),started_at=self.started_at))
        else:
            self.collection_wait_started=None
            self.display_identity=self.query_identity(query)
            # Reuse the results already mounted in the analytical pages.
            # This adds references, not another serialized result cache.
            self.displayed_pages[self.current_page]=(self.display_identity,result)

    def refresh_collection_wait(self,phase):
        state=self.analysis_loading
        if self.analysis_pending or not state.get('collectionWait'):return
        if phase=='ready':
            self.collection_wait_started=None
            self.clear_analysis_loading()
            return
        if phase=='preparing':
            if not state.get('running'):
                self.set_analysis_loading(dict(active=True,retained=False,running=True,collectionWait=True,
                    title=tr('사용 기록 수집 중'),detail=tr('Codex 사용 기록을 읽고 있습니다.')))
                self.analysis_clock.start();self.slow_timer.start()
            return
        self.analysis_clock.stop();self.slow_timer.stop()
        stopped=phase=='stopped'
        self.set_analysis_loading(dict(active=True,retained=False,running=False,collectionWait=True,
            mode='empty' if stopped else 'error',title=tr('사용 기록 수집 중지' if stopped else '사용 기록 수집 확인 필요'),
            detail=tr('자동 수집이 중지되었습니다. 확인된 기록은 계속 표시합니다.' if stopped else
                '기록 수집이 지연되거나 중단되었습니다. 수집 상태를 확인하거나 다시 시도하세요.')))
        self.retry_button.setVisible(not stopped)

    def retry_analysis(self):
        if self.worker is not None and not self.worker.isRunning():
            self.worker.restart_count=0;self.worker.start()
        elif self.worker is not None and any(facts.get('publication_failed') for facts in self.analysis_errors):
            self.worker.request_snapshot()
        if getattr(self,'preparation',None) is not None:self.analysis_errors.clear()
        self.client_cache.clear();self.pending_logical=None;self.render()
        if getattr(self,'preparation',None) is not None:self.sync_preparation()

    def sync_preparation(self):
        pending=self.preparation
        overlay=getattr(self,'overlay',None)
        if pending is None:
            return
        from .app_errors import collection_phase
        phase=collection_phase({**self.snapshot,**self.home_collection_status},time.time(),started_at=self.started_at)
        if overlay:overlay.receive_preparation(None if self.analysis_errors or phase in ('failed','delayed','stopped') else pending)
        if self.analysis_errors:
            self.show_analysis_message('현재 조건의 결과를 불러오지 못했으므로 잠시 후 다시 시도해주세요.','',retry=True)
            self.retry_button.show()
            return
        if phase in ('failed','delayed','stopped'):
            stopped=phase=='stopped'
            self.show_analysis_message('사용 기록 수집 중지' if stopped else '사용 기록 수집 확인 필요',
                '자동 수집이 중지되었습니다. 확인된 기록은 계속 표시합니다.' if stopped else
                '기록 수집이 지연되거나 중단되었습니다. 수집 상태를 확인하거나 다시 시도하세요.',retry=not stopped)
            if stopped:self.set_analysis_loading(dict(self.analysis_loading,running=False,mode='empty'))
            self.retry_button.setVisible(not stopped)
            return
        state=preparation_loading(pending,now=time.monotonic())
        state['retained']=bool(self.pending_retained and self.analysis_regions())
        self.set_analysis_loading(state)
        self.retry_button.hide()


    def receive_collection_observation(self,value):
        if self.quitting or getattr(self,'_closing',False):return
        self.home_collection_status['collection_observation']=value
        self.observe_error('collection.observer','collection',value.get('collection_fault') or {},
                           healthy=not value.get('collection_fault'),resolution='수집기 관측 재개 확인')
        source_fault=value.get('source_fault')
        if source_fault is not None:
            self.observe_error('collection.source','collection',source_fault,
                healthy=not source_fault,resolution='사용 기록 수집 재개 확인')
        overlay=getattr(self,'overlay',None)
        if overlay:overlay.receive_collection_observation(value)
        self.refresh_home_status()

    def receive_collection_status(self,result):
        self.home_collection_status={**self.home_collection_status,**{key:result[key] for key in
            ('collection_fault','collection_healthy','collection_snapshot_at') if key in result}}
        if result.get('collection_healthy') and 'collection_fault' not in result:self.home_collection_status['collection_fault']={}
        self.refresh_home_status()
        if isinstance(result.get('collection_snapshot_at'),(int,float)):
            self.collection_snapshot_at=result['collection_snapshot_at']
        if isinstance(result.get('collection_fault'),dict):
            facts=result['collection_fault'];self.collection_starting=bool(facts.get('starting'))
            self.collection_stopped=bool(facts.get('expected_stop'))
            self.observe_error('collection.service','collection',facts,
                healthy=bool(result.get('collection_healthy')) or bool(facts.get('expected_stop')),
                resolution='수집기 종료 확인' if facts.get('expected_stop') else '수집기 실행 확인')
        if 'collection_issue' in result:self.collection_issue=result['collection_issue']
        overlay=getattr(self,'overlay',None)
        if overlay:overlay.receive_collection_status(result)

    def receive(self,value):
        preparing=self.preparation is not None
        self.preparation=None
        if preparing:
            self.clear_analysis_loading()
            if self.analysis_pending:
                self.show_analysis_pending()
                if self.analysis_loading.get('running'):
                    self.analysis_clock.start();self.slow_timer.start();self.update_analysis_elapsed()
        if value.get('epoch')!=self.snapshot.get('epoch'):
            self.collection_wait_started=None
            self.client_cache.clear()
            self.displayed_pages.clear();self.display_identity=None
        if not self.async_mode:
            self.engine.ingest(value['sessions'])
            value={**value,'sessions':[s['source'] for s in self.engine.sessions.values()],
                   'internal_review_calls':sum(len(s['prepared']['history']) for s in self.engine.internal_sessions.values())}
        self.snapshot=value
        self.mismatch_page.receive_health(value.get('mismatch_history'))
        self.refresh_home_status()
        self.error_state.clear('analysis.process',resolution='분석기 실행 확인')
        if (self.home_collection_status.get('collection_observation') or {}).get('source_fault') is None:
            if isinstance(value.get('collection_fault'),dict):
                source_facts=dict(value['collection_fault'])
                if value.get('usage_errors'):source_facts['read_failed']=True
                self.observe_error('collection.source','collection',source_facts,
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
        self.index_status.setText('수집 확인 필요' if value.get('errors') else '기록 준비 중' if not value.get('usage_collection_complete',not index.get('loading')) else '자동 수집 켜짐')
        self.show_confirmed_events(self.confirmed_notifications.cache(value.get('cache_candidates',value['sessions'])))
        if self.worker:self.worker.set_cache_floor(self.confirmed_notifications.cache_floor)
        self.show_confirmed_events(self.confirmed_notifications.http(value['sessions']))
        self.show_confirmed_events(self.confirmed_notifications.model_candidates(value['model_candidates']) if 'model_candidates' in value else self.confirmed_notifications.models(value['sessions']))
        self.refresh_tray();self.render(automatic=True)

    def analysis_ready(self,message):
        result=message['result']
        if message['id']!=self.request_id:return
        self.error_state.clear('analysis.query',resolution='분석 결과 갱신 확인')
        self.error_state.clear('analysis.process',resolution='분석기 실행 확인')
        self.refresh_errors()
        cached=(result,message['valid_until'])
        self.client_cache.put(message['logical'],cached,
                              retained_bytes((message['logical'],cached),self.client_cache.rows))
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
        self.set_analysis_stage('present')
        if query['page']==5:
            self.view_result=result;self.performance_panel.apply(result['performance'])
            restored=getattr(self,'_restore_positions',None)
            if restored:
                position=restored.get('scrolls',{}).get('5',restored.get('scrolls',{}).get(5,0))
                self.scrollers[5].verticalPosition.setValue(position);self._restore_positions=None
            self.finish_analysis_loading(result,query)
            self.refresh_after_analysis()
            return
        if query['page']==6:
            self.view_result=result;self.applied_key=result['key'];self.analysis=result['analysis'];self.lookup=result['lookup']
            self.home_panel.apply_usage(result['analysis'],result['usage']['summary']);self.refresh_home_status()
            restored=getattr(self,'_restore_positions',None)
            if restored:
                position=restored.get('scrolls',{}).get('6',restored.get('scrolls',{}).get(6,0))
                self.scrollers[6].verticalPosition.setValue(position)
            self._restore_positions=None
            self.finish_analysis_loading(result,query);self.refresh_after_analysis();self.save_preferences()
            return
        self.view_result=result;self.applied_key=result['key'];self.analysis=result['analysis'];self.lookup=result['lookup']
        if self.current_page==1:self.render_comparison()
        state=getattr(self,'_restore_positions',None)
        if state:
            self._restore_positions=None
            for key,value in state.get('scrolls',{}).items():
                if int(key) in self.scrollers:self.scrollers[int(key)].verticalPosition.setValue(value)
            self.settings_page.restore_scrolls(state.get('settings_scrolls',{}))
        self.save_preferences()
        self.finish_analysis_loading(result,query)
        # Comparison setup may resolve default targets while applying the result.
        if self.display_identity is not None:
            self.display_identity=self.query_identity(self.query())
            self.displayed_pages[self.current_page]=(self.display_identity,result)
        self.refresh_after_analysis()

    def refresh_after_analysis(self):
        if getattr(self,'_refresh_after_pending',False):
            self._refresh_after_pending=False;QTimer.singleShot(0,lambda:self.render(automatic=True))

    def analysis_failed(self,error):
        facts=dict(error) if isinstance(error,dict) else dict(query_failed=True,request_id=self.request_id)
        if facts.get('query_failed') and facts.get('request_id')!=self.request_id:return
        facts.update(current_request_id=self.request_id,
                     retained=self.display_identity==self.query_identity(self.query())
                              and self.pages.isPageReady(self.pages.currentIndex()))
        if self.current_page in (1,5,6):self._analysis_error_page=self.current_page
        self.observe_error('analysis.query' if facts.get('query_failed') else 'analysis.process','analysis',facts)
        self.client_cache.clear();self.pending_logical=None
        self.request_id+=1;self.deferred_result=None;self.defer_timer.stop();self.clear_analysis_loading()
        self.analysis_pending=False;self.analysis_errors=[facts]
        if self.current_page not in (3,4):
            if not facts['retained']:
                self.show_analysis_message('현재 조건의 결과를 불러오지 못했으므로 잠시 후 다시 시도해주세요.','',retry=True)
            else:
                self.set_analysis_loading(dict(active=True,retained=True,running=False,mode='error',
                    title=tr('갱신 실패, 이전 결과 표시 중'),detail=''))
            self.retry_button.show()

    def eventFilter(self,watched,event):
        if event.type() in (QEvent.Wheel,QEvent.MouseButtonPress,QEvent.KeyPress):self.interaction_time=time.monotonic()
        if (event.type() in (QEvent.MouseButtonPress,QEvent.MouseButtonRelease,QEvent.MouseButtonDblClick)
            and isinstance(watched,QWidget) and watched.window() is self
            and event.button() in (Qt.BackButton,Qt.ForwardButton)):
            if event.type()==QEvent.MouseButtonRelease:
                (self.go_back if event.button()==Qt.BackButton else self.go_forward)()
            event.accept();return True
        return super().eventFilter(watched,event)
    def apply_deferred(self):
        pending=self.deferred_result;self.deferred_result=None
        if pending and pending[0]==self.request_id:
            self.apply_result(pending[1],pending[2],True)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'sidebar_layout'):self.layout_sidebar()
        if hasattr(self,'settings_page'):self.settings_page.navigation.setFixedWidth(144 if self.width()<1200 else 200)
        if hasattr(self,'pages'):QTimer.singleShot(0,self.update_performance_layout)
        if getattr(self,'current_page',3)==5 and hasattr(self,'performance_panel'):self.performance_panel.resize_timer.start()
    def showEvent(self,event):
        super().showEvent(event)
        self.sync_report_demand()
        self.quota_sidebar.refresh_status()
        self.refresh_home_status()

    def hideEvent(self,event):
        super().hideEvent(event);self.sync_report_demand()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.WindowStateChange:self.sync_report_demand()

    def closeEvent(self,event):
        self.settings.setValue('dashboard/geometry',self.saveGeometry());self.save_preferences();super().closeEvent(event)
    def check_stale(self):
        self.sync_preparation()
        if not self.quitting:
            from .app_errors import collection_phase,collection_publication_at
            evidence={**self.snapshot,**self.home_collection_status}
            observed=collection_publication_at(evidence)
            phase=collection_phase(evidence,time.time(),started_at=self.started_at)
            self.observe_error('collection.freshness','collection',
                dict(snapshot_at=observed,snapshot_stale=phase=='delayed',starting=phase=='preparing',expected_stop=self.collection_stopped),
                healthy=self.collection_stopped or phase=='preparing' or phase!='delayed' and observed is not None and time.time()-observed<=30,
                resolution='수집기 종료 확인' if self.collection_stopped else '새 수집 결과 확인')
        self.refresh_tray()
        if self.isVisible() and not self.isMinimized():
            shared_theme().refresh_motion()
            self.quota_sidebar.refresh_status()
            self.refresh_home_status()
            if self.current_page==6:
                result=self.displayed_pages.get(6)
                if result and time.time()>=result[1].get('valid_until',float('inf')) and not self.analysis_pending:self.render(automatic=True)
            if self.current_page==3:self.quota_panel.refresh_status()
        if int(time.time())%10==0:shared_theme().configure(self.settings.value('ui/theme',self.settings.value('overlay/theme','codex')))


    def render_comparison(self,*_):
        self.metric_panel.apply(self.view_result['usage'])


    def show_prices(self):
        dialog=Dialog(self);dialog.setWindowTitle(tr('API 가격표'));dialog.resize(1080,720)
        body=Column(dialog);body.setContentsMargins(20,20,20,20);body.setSpacing(16)
        heading=Row();heading.addWidget(label('Standard API 단가','section'));heading.addStretch()
        heading.addWidget(label('USD / 100만 토큰','muted'));body.addLayout(heading)
        prices=table(['모델','일반 입력','캐시 읽기','캐시 쓰기','출력','단가 확인','Fast 배수'])
        for i,width in enumerate((240,135,135,135,100,140,150)):prices.setColumnWidth(i,width)
        rows=[]
        for model in (*SUPPORTED_MODELS, 'gpt-daybreak-blue-latest'):
            rate=standard_rate(model);multiplier=subscription_multiplier(model,'Fast')
            rows.append([model,usd(rate.input),usd(rate.cached) if rate.cached is not None else '미지원',
                         usd(rate.written) if rate.written is not None else '입력 단가',usd(rate.output),
                         rate_verified(model),f'×{multiplier:g}' if multiplier is not None else '미지원'])
        fill(prices,rows);body.addWidget(prices,1)
        summary=Group();summary.put(background='surface',radius=6)
        summary_row=Row(summary);summary_row.setContentsMargins(16,12,16,12);summary_row.setSpacing(32)
        for title,value in (('구독 차감 배율',f'Standard ×1 / Fast ×{FAST_MULTIPLIER:g}'),('장문 입력','할증 제외')):
            column=Column();column.setSpacing(4)
            column.addWidget(label(title,'muted'));column.addWidget(label(value,'section'))
            summary_row.addLayout(column,1)
        body.addWidget(summary)

        def show_reference():
            reference=Dialog(dialog);reference.setWindowTitle(tr('가격 기준 및 별칭'));reference.resize(880,540)
            layout=Column(reference);layout.setContentsMargins(20,20,20,20);layout.setSpacing(16)
            layout.addWidget(label('가격 기준','section'))
            criteria=table(['항목','확인일 / 기한']);criteria.put(inline=True,rowDetails=False)
            criteria.setColumnWidth(0,300);criteria.setColumnWidth(1,500);criteria.setFixedHeight(200)
            fill(criteria,[['지원 모델 확인',MODEL_VERIFIED],['구독 배율 확인',MULTIPLIER_VERIFIED],
                           ['GPT-5.6 Sol 프로모션','최소 2026-11-21까지']],numeric_from=2)
            layout.addWidget(criteria)
            layout.addWidget(label('가격 별칭','section'))
            aliases=table(['기록된 모델명','단가 적용 모델']);aliases.put(inline=True,rowDetails=False)
            aliases.setColumnWidth(0,420);aliases.setColumnWidth(1,380)
            fill(aliases,[(alias,model) for alias,model in ALIASES.items()
                          if model not in ('gpt-5.4-mini','gpt-5.5')],numeric_from=2);layout.addWidget(aliases,1)
            close_row=Row();close_row.addStretch()
            close=Button('닫기');close.setObjectName('price-reference-close');close.put(iconName='');close.setFixedWidth(80)
            close.clicked.connect(reference.reject);close_row.addWidget(close);layout.addLayout(close_row)
            reference.open();dialog.reference_dialog=reference

        actions=Row();reference_button=Button('가격 기준 및 별칭');reference_button.setObjectName('price-reference-button')
        reference_button.clicked.connect(show_reference);actions.addWidget(reference_button);actions.addStretch()
        buttons=DialogButtons(DialogButtons.Close);buttons.rejected.connect(dialog.reject);actions.addWidget(buttons)
        body.addLayout(actions);dialog.open();self.price_dialog=dialog


    def build_settings(self):
        from .settings_page import SettingsPage
        from .controls import Switch
        self.settings_page = SettingsPage(self)
        self.pages.addWidget(self.settings_page)
        from .connection_recovery import target
        manager=target(self.observer_home,self.observer_directory,index=self.index_path)
        if self.model_evidence_path:manager.evidence=Path(self.model_evidence_path)
        self.observer_panel=ObserverPanel(self.observer_home,self.observer_directory,active=self.manage_observer,parent=self,manager=manager)
        self.settings_page.add_widget('integration', self.observer_panel,section='프록시',title=self.observer_panel.title.text(),description=self.observer_panel.description.text(),target=self.observer_panel.toggle,aliases='proxy connection')
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
        self.settings_page.add_widget('about',self.update_panel,section='업데이트',title='업데이트 확인',target=self.update_panel,aliases='정보 문제 해결 update')
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
