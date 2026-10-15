from .tooltips import TEXT as TIPS
"""Auditable weekly cycle cost tracking from local calls."""
from datetime import datetime
import math
import os
from pathlib import Path
import time

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from .presentation import Button, Choice, Column, Group, Row, Text, Toggle
from .table_model import Cell, Header, Table
from .pricing import usd
from .workload import call_count
from .quota_cycles import quota_statistics, quota_value_history
from .quota_tracking import RESET_NAMES
from .lazy_table import LazyTable
from .quota_share import share_label, share_color_key
from .quota_exclusion_controls import QuotaExclusions
from .ui_details import Details
from .i18n import formatted


from .quota_view import clock, history_rows, prepare_quota_view, prepare_series, observation_label, model_cost_intervals

def home_key(value):
    return os.path.normcase(str(Path(value).resolve())) if value else None


def remaining_axis(rows):
    from .quota_chart import quota_bounds
    low,high=quota_bounds(r.get('remaining') for r in rows)
    return low,high,(high-low)/4


from .quota_chart import QuotaHistory


class QuotaPanel(Group):
    home_selected=Signal(str)
    exclusion_requested=Signal(object)
    exclusion_restore_requested=Signal(str)
    cycle_clear_requested=Signal(object)
    fault_changed=Signal(dict)

    def __init__(self,settings):
        super().__init__()
        self.settings=settings
        self.report={'cycles':[]}
        self.statistics=quota_statistics(self.report)
        self.quota=None
        self.issue=''
        self.monitoring_paused=False
        self.monitoring_paused_at=None
        self.selected_id=None
        self.period_start=None
        self.period_end=None
        self.page_index=0
        self.page_size=25
        self.expected_home=None
        self._query_worker=None
        self._query_cache={}
        self._query_pending=None
        self._prepared_query=None
        self._chart_pending=None
        self._chart_value=None
        self._summary_opened=None;self._summary_restore=None
        layout=Column(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(20)
        self.home_choice=Choice();self.home_choice.setAccessibleName('한도 관측 대상 Codex 홈')
        self.home_choice.hide()
        self.home_choice.currentIndexChanged.connect(self.choose_home)
        layout.addWidget(self.home_choice)
        self.status=Text();self.status.put(color='error',fontSize=13,wrap=True)
        self.status.hide();layout.addWidget(self.status)
        scope=Row();scope.put(flow=True);scope.setSpacing(12)
        scope.addWidget(Text('상세 내역 조회 기간'))
        self.period=Choice()
        for label,days in (('전체 기간',None),('최근 7일',7),('최근 30일',30),('최근 90일',90)):
            self.period.addItem(label,days)
        self.period.setAccessibleName('잔여량과 환산 내역의 조회 기간');scope.addWidget(self.period)
        scope.addWidget(Text('주간 환산 모델'))
        self.model_choice=Choice();self.model_choice.addItem('전체 모델',None)
        self.model_choice.setAccessibleName('주간 구독 가치 환산 모델 필터')
        scope.addWidget(self.model_choice)
        chart=Group();chart.put(background='surface',radius=6)
        chart_layout=Column(chart);chart_layout.setContentsMargins(16,16,16,14);chart_layout.setSpacing(10)
        self.history_waiting=Group()
        waiting_layout=Column(self.history_waiting);waiting_layout.setSpacing(8)
        waiting_title=Text('사용량 기록 수집 대기');waiting_title.put(fontSize=18,bold=True)
        waiting_note=Text('소모량과 구독 가치 환산액을 계산할 사용 기록을 기다리고 있습니다.')
        waiting_note.put(fontSize=14,color='muted',wrap=True)
        waiting_layout.addWidget(waiting_title);waiting_layout.addWidget(waiting_note)
        chart_layout.addWidget(self.history_waiting)
        headline=self.summary_heading=Text('전체 기록과 모델 기준 100% 환산 추정액');headline.put(fontSize=15,bold=True)
        chart_layout.addWidget(headline)
        self.result=Text();self.result.setToolTip(TIPS["quota_total"]);self.result.put(fontSize=22,bold=True,noElide=True)
        chart_layout.addWidget(self.result)
        self.lifetime_basis=Text();self.lifetime_basis.put(fontSize=12,color='muted',wrap=True)
        chart_layout.addWidget(self.lifetime_basis)
        controls=self.history_controls=Row();controls.setSpacing(12)
        self.chart_title=chart_title=Text('선택 주기의 사용량 추이');chart_title.put(fontSize=18,bold=True)
        controls.addWidget(chart_title,1)
        chart_layout.addLayout(controls)
        self.cycle_choice=Choice();self.cycle_choice.setAccessibleName('사용량 리셋 주기 선택')
        self.exclusions=QuotaExclusions()
        self.exclusions.requested.connect(self.exclusion_requested.emit)
        self.exclusions.restore_requested.connect(self.exclusion_restore_requested.emit)
        cycle_controls=Row();cycle_controls.put(flow=True);cycle_controls.setSpacing(8)
        cycle_controls.addWidget(self.cycle_choice,1)
        for button in (self.exclusions.toggle,self.exclusions.history_toggle):
            button.put(fontSize=12,height=30,flat=True)
            cycle_controls.addWidget(button)
        chart_layout.addLayout(cycle_controls)
        self.cycle_value=Text();self.cycle_value.setToolTip(TIPS["quota_cycle"]);self.cycle_value.put(fontSize=22,bold=True,wrap=True)
        chart_layout.addWidget(self.cycle_value)
        self.cycle_basis=Text();self.cycle_basis.put(fontSize=12,color='muted',wrap=True)
        chart_layout.addWidget(self.cycle_basis)
        self.history=QuotaHistory()
        self.history.selected.connect(self.select_observation)
        self.history.cycle_activated.connect(self.open_summary_cycle)
        self.history.range_selected.connect(self.exclusions.select_range)
        self.exclusions.selection_changed.connect(self.history.set_exclusion_selection)
        chart_layout.addWidget(self.exclusions)
        self.history_legend=Row();self.history_legend.put(flow=True);self.history_legend.setSpacing(16)
        self.legend_toggles={}
        for key,name,color in (('cycle_value','주간 예상 가치','accent'),
                               ('remaining','남은 사용량','written'),
                               ('cycle_cost','누적 측정 환산액','output')):
            legend=Button(name);legend.setCheckable(True);legend.setChecked(True)
            legend.toggled.connect(lambda visible,k=key:self.history.set_series_visible(k,visible))
            self.legend_toggles[key]=legend
            if key=="cycle_value":legend.setToolTip(TIPS["quota_cycle"])
            legend.put(fontSize=12,flat=True,legendColor=color,legendDashed=key=='reference',activateOnReturn=True)
            self.history_legend.addWidget(legend)
            if key=='remaining':self.remaining_legend=legend
        chart_layout.addLayout(self.history_legend)
        chart_layout.addWidget(self.history)
        self.history_empty=Text();self.history_empty.put(fontSize=14,color='muted',wrap=True)
        chart_layout.addWidget(self.history_empty)
        self.model_legend=Row();self.model_legend.put(flow=True);self.model_legend.setSpacing(16)
        chart_layout.addLayout(self.model_legend)
        chart_layout.addWidget(self.exclusions.history)
        chart_layout.addWidget(self.exclusions.message)
        self.cycle_choice.currentIndexChanged.connect(self.render_history)
        layout.addWidget(chart)
        self.conversion=Group();self.conversion.put(background='surface',radius=6)
        conversion_layout=Column(self.conversion);conversion_layout.setContentsMargins(20,18,20,18);conversion_layout.setSpacing(16)
        title=Text('관측 내역');title.put(fontSize=18,bold=True);conversion_layout.addWidget(title)
        conversion_layout.addLayout(scope)
        metrics=Row();metrics.put(collapseBelow=620,columns=2);metrics.setSpacing(20)
        self.basis=Text();self.cost_value=Text();self.basis.setToolTip(TIPS["quota_used"]);self.cost_value.setToolTip(TIPS["cost"])
        for name,value in (('소모량',self.basis),('구독 가치 환산액',self.cost_value)):
            metric=Column();label=Text(name);label.put(fontSize=13,color='muted');metric.addWidget(label)
            if value is self.basis:self.basis_label=label
            else:self.cost_label=label
            value.put(fontSize=26,bold=True,noElide=True);metric.addWidget(value);metrics.addLayout(metric,1)
        conversion_layout.addLayout(metrics)
        self.conversion_empty=Text();self.conversion_empty.put(fontSize=13,color='muted',wrap=True)
        conversion_layout.addWidget(self.conversion_empty)
        detail_body=Group();detail_layout=Column(detail_body);detail_layout.setSpacing(12)
        self.details=Details('구간별 내역',detail_body)
        conversion_layout.addWidget(self.details)
        layout.addWidget(self.conversion)
        self.interval_filter=Choice()
        for label,value in (('전체 구간','all'),('환산 구간','used'),('비용 제외 사유가 있는 구간','excluded')):
            self.interval_filter.addItem(label,value)
        self.interval_filter.setAccessibleName('환산 구간 상태');detail_layout.addWidget(self.interval_filter)
        self.intervals=LazyTable(['기간','소모량','구독 가치 환산액','주간할당량 가치','산정 / 전체 호출'])
        self.intervals.put(inline=True)
        self.intervals.verticalHeader().setDefaultSectionSize(40)
        self.intervals.horizontalHeader().setSectionResizeMode(0,Header.Stretch)
        for col,width in enumerate((245,95,120,125,140)):self.intervals.setColumnWidth(col,width)
        detail_layout.addWidget(self.intervals)
        self.interval_empty=Text();detail_layout.addWidget(self.interval_empty)
        paging=self.paging=Row()
        self.previous_page=Button('이전 구간');self.next_page=Button('다음 구간')
        for button,icon,title in ((self.previous_page,'left','이전 구간'),(self.next_page,'right','다음 구간')):
            button.setText('');button.put(iconName=icon,flat=True);button.setFixedSize(36,36);button.setAccessibleName(title);button.setToolTip(title+" 보기")
        self.page_status=Text()
        paging.addWidget(self.previous_page);paging.addWidget(self.page_status,1);paging.addWidget(self.next_page)
        detail_layout.addLayout(paging)
        self.previous_page.clicked.connect(lambda:self.change_page(-1))
        self.next_page.clicked.connect(lambda:self.change_page(1))
        self.interval_detail=Text();self.interval_detail.setWordWrap(True);self.interval_detail.put(fontSize=13)
        detail_layout.addWidget(self.interval_detail)
        self.mode_assumption=Toggle('미기록 모드에 Standard 단가 적용값 보기');detail_layout.addWidget(self.mode_assumption)
        self.assumption_value=Text();self.assumption_value.setWordWrap(True);self.assumption_value.hide();detail_layout.addWidget(self.assumption_value)
        self.table=Table();self.table.put(inline=True)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(['모델 / 요청 모드','호출 수','산정 호출 수','구독 가치 환산액','한도'])
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.horizontalHeader().setSectionResizeMode(0,Header.Stretch)
        for col,width in enumerate((320,90,110,130,130)):self.table.setColumnWidth(col,width)
        detail_layout.addWidget(self.table)
        self.interval_filter.currentIndexChanged.connect(self.change_interval_filter)
        self.intervals.itemSelectionChanged.connect(self.select_interval)
        self.period.currentIndexChanged.connect(self.change_period)
        self.model_choice.currentIndexChanged.connect(self.change_model)
        self.mode_assumption.toggled.connect(self.render)
        layout.addStretch()
        self.render()
        from .quota_layout import configure_quota_layout
        configure_quota_layout(self)

    def set_monitoring_paused(self,paused,at=None):
        self.monitoring_paused=bool(paused);self.monitoring_paused_at=at
        self.history.set_monitoring_paused(paused)
        from .quota_layout import refresh_monitoring_style
        refresh_monitoring_style(self)
        if hasattr(self,'update_summary'):self.update_summary()

    def set_homes(self,homes):
        selected=self.home_choice.currentData() or self.report.get('home')
        self.home_choice.blockSignals(True);self.home_choice.clear()
        for home in homes:self.home_choice.addItem(str(home),str(home))
        self.home_choice.setCurrentIndex(max(0,self.home_choice.findData(selected)))
        self.home_choice.blockSignals(False);self.home_choice.setVisible(len(homes)>1)
        home=self.home_choice.currentData()
        self.expected_home=home_key(home)
        if home and not self.report.get('home'):
            self.report={**self.report,'home':home};self.refresh_status()

    def choose_home(self,*_):
        home=self.home_choice.currentData()
        if not home:return
        self.expected_home=home_key(home)
        self.quota=None;self.report={'home':home,'cycles':[]};self.selected_id=None
        self.issue='';self.page_index=0
        self.render();self.home_selected.emit(home)

    def receive(self,value):
        incoming_home=value.get('home') or (value.get('report') or {}).get('home')
        if self.expected_home and (home_key(incoming_home)!=self.expected_home):
            return False
        if incoming_home:self.expected_home=home_key(incoming_home)
        previous_report,previous_issue,previous_quota=self.report,self.issue,self.quota
        self.quota=value.get('quota');self.issue=value.get('issue','')
        if 'report' in value:self.report=value['report'] or {'cycles':[]}
        if value.get('exclusion_result'):
            result=value['exclusion_result']
            if result.get('operation')=='clear_cycle':
                self._cycle_clear_pending=False
                if result.get('ok'):
                    message='선택한 주기를 삭제했습니다.' if result.get('deleted') else '선택한 주기를 초기화했습니다.'
                else:
                    message=result.get('error','선택한 주기를 처리하지 못했습니다. 다시 시도하세요.')
                    self.issue=message
                if hasattr(self,'cycle_clear_message'):
                    self.cycle_clear_message.setText(message);self.cycle_clear_message.show()
            else:self.exclusions.receive_result(result)
            if value['exclusion_result'].get('ok') and getattr(self,'dialog',None) and self.dialog.host:self.dialog.reject()
        if getattr(self,'defer_render',False):
            if (self.report is not previous_report or self.issue!=previous_issue
                    or self.quota is not previous_quota or value.get('exclusion_result')):
                self._render_dirty=True
            return True
        if (self.report is previous_report and self.issue==previous_issue
                and not value.get('exclusion_result') and not getattr(self,'_render_dirty',False)):
            if hasattr(self,'update_summary'):self.update_summary()
            return True
        self.render(automatic=True)
        return True

    def refresh_status(self):
        if getattr(self,'_render_dirty',False):
            self._render_dirty=False;self.render(automatic=True)

    def change_period(self,*_):
        days=self.period.currentData()
        self.set_history_period(time.time()-days*86400 if days else None,None)

    def change_model(self,*_):
        self.interval_filter.setCurrentIndex(0)
        self.page_index=0
        self.render()

    def set_history_period(self,start=None,end=None):
        self.period_start,self.period_end=start,end
        self.render()

    def open_summary_cycle(self,start):
        report=self.report
        def select():
            if self.report is not report:return
            index=self.cycle_choice.findData(start)
            if index<1:return
            self._summary_opened=start
            self.cycle_choice.setCurrentIndex(index)
        # Finish the chart's pointer event before replacing its selected data.
        QTimer.singleShot(0,select)

    def return_to_summary(self):
        self._summary_restore=self._summary_opened
        self.cycle_choice.setCurrentIndex(0)

    def render_history(self,*_):
        from bisect import bisect_left
        if getattr(self,'_value_report',None) is not self.report:
            had_periods=bool(getattr(self,'_view',{}).get('periods'))
            self._view=self.report.get('view') or prepare_quota_view(self.report)
            self._value_report=self.report
            selected=self.cycle_choice.currentData() if had_periods else None
            choices=[('전체','all')]+[(p['label'],p['start']) for p in self._view['periods']]
            if [(self.cycle_choice.itemText(i),self.cycle_choice.itemData(i))
                    for i in range(self.cycle_choice.count())]!=choices:
                self.cycle_choice.blockSignals(True);self.cycle_choice.clear()
                for label,start in choices:self.cycle_choice.addItem(label,start)
                index=self.cycle_choice.findData(selected)
                self.cycle_choice.setCurrentIndex(index if index>=0 else min(1,self.cycle_choice.count()-1))
                self.cycle_choice.blockSignals(False)
            periods={period['start']:period for period in self._view['periods']}
            self.cycle_choice._items=[dict(item,ink='accent' if periods.get(item['value'],{}).get('current') else 'ink') for item in self.cycle_choice._items]
            self.cycle_choice.put(items=list(self.cycle_choice._items))
        index=self.cycle_choice.currentIndex()
        all_cycles=index==0
        lifetime=self._view['lifetime']
        period=(dict(start='all',series=self._view.get('overall'),cost=lifetime['cost'],delta=lifetime['delta'],
                     priced_calls=lifetime['observed_priced_calls'],calls=lifetime['observed_calls'],
                     value=lifetime['per_percent']*100 if lifetime['per_percent'] is not None else None)
                if all_cycles else self._view['periods'][index-1] if index>0 else None)
        series=period.get('series') if period else prepare_series([])
        if 'chart' in self._view:
            self._bind_worker()
            selection=period['start'] if period else 'all'
            key=(id(self._query_worker),self.report['worker_revision'],selection)
            offered=self._view['chart']
            if offered['selection']==selection and offered.get('series') is not None:
                self._chart_value=(key,offered['series'])
            elif (offered['selection']==selection and self._chart_value
                  and self._chart_value[0][0]==key[0] and self._chart_value[0][2]==selection
                  and self._chart_value[1].get('_content_digest')==offered['digest']):
                self._chart_value=(key,self._chart_value[1])
            if self._chart_value and self._chart_value[0]==key:
                series=self._chart_value[1];self._chart_pending=None
                if self._query_worker is not None and hasattr(self._query_worker,'select_chart'):
                    self._query_worker.select_chart(selection,series.get('_content_digest'))
            else:
                if self._query_worker is not None and self._chart_pending!=key:
                    self._chart_pending=key
                    if hasattr(self._query_worker,'select_chart'):self._query_worker.select_chart(selection)
                    self._query_worker.request('chart',key=key,revision=self.report['worker_revision'],selection=selection)
                self.history.hide();self.history_legend.hide();self.history_empty.setText('계산 중...');self.history_empty.show()
                self.model_legend.hide()
                if hasattr(self,'summary_context'):self.summary_context.hide()
                if hasattr(self,'manage_button'):self.manage_button.hide()
                self.exclusions.set_context(self.report,None,True)
                if hasattr(self,'sync_dialog'):self.sync_dialog()
                return
            period={**period,'series':series} if period else period
        old=self.history.rows[self.history.cursor]['at'] if self.history.rows else None
        same=getattr(self,'_selected_cycle',None)==(period['start'] if period else None)
        follow=not self.history.rows or self.history.cursor==len(self.history.rows)-1
        self._selected_cycle=(period['start'] if period else None)
        self.history.money=True
        self.history.reference=None if all_cycles else period['value'] if period else None
        self.history.empty_text='표시할 사용 기록이 없습니다'
        self.chart_title.setText('주기별 사용량 추이' if all_cycles else '선택 주기의 사용량 추이')
        self.remaining_legend.setText('주기 소모량 (%p)' if all_cycles else '남은 사용량 (%)')
        self.history.set_series(series)
        self.history.legendChanged.emit(self.history.legend_values())
        models=series.get('model_share',{}).get('models',[])
        if getattr(self,'_legend_models',None)!=models:
            self._legend_models=list(models)
            old=self.model_legend._nodes;self.model_legend._nodes=[]
            self.model_legend.structureChanged.emit()
            for node in old:node.deleteLater()
            for name in models:
                label=Text(share_label(name));label.put(fontSize=12,color='ink',swatch=share_color_key(name),wrap=True)
                self.model_legend.addWidget(label)
            for name in ('━ 비용 증가 없음','▧ 미확인'):
                label=Text(name);label.put(fontSize=12,color='muted');self.model_legend.addWidget(label)
        self.model_legend.setVisible(bool(self.history.rows) and not all_cycles)
        rows=self.history.rows
        any_history=(self._view['has_history'] if 'has_history' in self._view else
                     bool(self._view['overall']['rows'] or any(item['series']['rows'] for item in self._view['periods'])))
        self.history_controls.setVisible(any_history)
        self.history_legend.setVisible(bool(rows))
        self.history.setVisible(bool(rows))
        self.history_empty.setText(self.history.empty_text)
        self.history_empty.setVisible(any_history and not rows)
        self.history.cursor=(len(rows)-1 if rows and (follow or not same) else
                             min(bisect_left(series['times'],old),len(rows)-1) if rows and old is not None else 0)
        if all_cycles and rows and self._summary_restore is not None:
            self.history.cursor=next((i for i,row in enumerate(rows) if row['cycle_start']==self._summary_restore),len(rows)-1)
            self._summary_restore=None
            self.history.repaintRequested.emit()
        if hasattr(self,'summary_context'):
            self.summary_context.setVisible(bool(rows))
            self.summary_note.setVisible(all_cycles)
            self.summary_back.setVisible(not all_cycles and not self.report.get('history_pending'))
        self.exclusions.set_context(self.report,period,True,cursor=self.history.cursor)
        if hasattr(self,'sync_dialog'):self.sync_dialog()
        if rows:self.select_observation(rows[self.history.cursor])
        if period:
            self.cycle_value.setText(f"누적 소모량 {period['delta']:g}%p" if all_cycles else '선택 주기 100% 환산 추정액 '+usd(period['value']))
            self.cycle_basis.setText(f"주간 100%p 기준 / 누적 환산액 {usd(period['cost'])} / 소모 {period['delta']:g}%p"
                                    ', '+('산정 ' if period['priced_calls']<period['calls'] else '')+call_count(period['priced_calls'],period['calls'])+'호출')
        else:
            self.cycle_value.setText('선택 주기 —');self.cycle_basis.setText('관측된 사용량 리셋 주기가 없습니다')
        self.cycle_choice.setVisible(any_history and bool(self._view['periods']))
        if hasattr(self,'manage_button'):
            self.manage_button.setVisible(any_history or bool(self.report.get('exclusions')) and not self.report.get('index_loading'))
        if hasattr(self,'clear_cycle_button'):
            self.clear_cycle_button.setText('선택 주기 삭제' if period and not all_cycles and not period['current'] else '선택 주기 초기화')
            self.clear_cycle_button.setEnabled(bool(period) and not all_cycles and bool(self.report.get('account')) and not getattr(self,'_cycle_clear_pending',False))
        for node in (self.cycle_value,self.cycle_basis):node.setVisible(bool(lifetime['total'] or self.report.get('exclusions')))
        if hasattr(self,'summary'):
            from .quota_layout import refresh_monitoring_style
            refresh_monitoring_style(self)

    def select_observation(self,row):
        self.history.setAccessibleName(observation_label(row,self.history.money))

    def _query_parameters(self):
        model=self.model_choice.currentData()
        return dict(days=self.period.currentData(),start=self.period_start,end=self.period_end,
                    model=model,mode=self.interval_filter.currentData() if model is None else 'all',
                    page=self.page_index,page_size=self.page_size)

    def _query_key(self):
        return (id(self.report.get('_worker')),self.report.get('worker_revision'),
                *self._query_parameters().values())

    def _cache_query(self, key, value):
        self._query_cache[key]=value
        while len(self._query_cache)>8:self._query_cache.pop(next(iter(self._query_cache)))

    def _bind_worker(self):
        worker=self.report.get('_worker')
        if worker is not self._query_worker:
            if self._query_worker is not None:
                try:self._query_worker.result.disconnect(self._receive_query)
                except (RuntimeError,TypeError):pass
            self._query_worker=worker;self._query_pending=None;self._query_cache={}
            self._chart_value=None;self._chart_pending=None
            if worker is not None:worker.result.connect(self._receive_query)

    def _prepared_statistics(self):
        """Request display-ready totals without doing aggregation on the GUI."""
        self._bind_worker()
        worker=self._query_worker;key=self._query_key()
        query=self._query_parameters()
        if (query['days'] is None and query['start'] is None and query['end'] is None
                and query['model'] is None and query['mode']=='all' and query['page']==0):
            default=self._view.get('default_query')
            if default is not None:self._cache_query(key,default)
        if key in self._query_cache:
            self._query_pending=None
            self._prepared_query=(key,self._query_cache[key])
            if getattr(self,'_query_failed',False):
                self._query_failed=False;self.fault_changed.emit(dict(query_healthy=True))
            return self._prepared_query[1]
        if worker is not None and self._query_pending!=key:
            self._query_pending=key
            worker.request('query',key=key,revision=self.report['worker_revision'],query=query)
        return None

    @Slot(object)
    def _receive_query(self, message):
        key=message.get('key')
        if message.get('kind')=='chart':
            current=(id(self._query_worker),self.report.get('worker_revision'),self.cycle_choice.currentData())
            if key!=self._chart_pending or key!=current:return
            self._chart_pending=None
            if message.get('error'):
                self.fault_changed.emit(dict(calculation_failed=True));return
            value=message['value']
            if value['selection']!=key[2] or value.get('series') is None:return
            self.fault_changed.emit(dict(query_healthy=True))
            self._chart_value=(key,value['series'])
            if getattr(self,'defer_render',False):self._render_dirty=True
            else:self.render_history()
            return
        if message.get('kind')!='query' or key!=self._query_pending:return
        self._query_pending=None
        if key!=self._query_key():return
        if message.get('error'):
            self._query_failed=True
            self.fault_changed.emit(dict(calculation_failed=True))
            if not getattr(self,'centralized_errors',False):
                self.status.setText(message.get('issue') or message['error']);self.status.show()
            return
        self._query_failed=False;self.fault_changed.emit(dict(query_healthy=True))
        self._cache_query(key,message['value'])
        if getattr(self,'defer_render',False):self._render_dirty=True
        else:self.render(automatic=True)

    def render(self,*_,automatic=False):
        self.refresh_status()
        if 'view' not in self.report:
            self.report={**self.report,'view':prepare_quota_view(self.report)}
        self.status.setText(self.issue or ('한도 기록을 읽지 못했습니다. 마지막 저장값을 확인해 주세요.' if self.report.get('error') else ''))
        self.status.setVisible(bool(self.status.text()) and not getattr(self,'centralized_errors',False))
        self.lifetime_statistics=(self.report.get('view') or {}).get('lifetime') or quota_statistics(self.report)
        lifetime=self.lifetime_statistics
        has_usage=bool(lifetime['total'] or self.report.get('exclusions') or self.report.get('cycle_clears'))
        self.history_waiting.setVisible(not has_usage)
        for node in (self.summary_heading,self.result,self.lifetime_basis,self.conversion):node.setVisible(has_usage)
        selected_model=self.model_choice.currentData()
        available=self.report['view'].get('available_models')
        if available is None:
            available=sorted({model['model'] for row in lifetime['intervals'] for model in row['models']
                              if model.get('model') and not model.get('separate')})
        if getattr(self,'_available_models',None)!=available:
            self._available_models=available
            self.model_choice.blockSignals(True)
            self.model_choice.clear();self.model_choice.addItem('전체 모델',None)
            for model in available:self.model_choice.addItem(model,model)
            self.model_choice.setCurrentIndex(max(0,self.model_choice.findData(selected_model)))
            self.model_choice.blockSignals(False)
        self.lifetime_value=lifetime['per_percent']*100 if lifetime['per_percent'] is not None else None
        self.result.setText(usd(self.lifetime_value))
        self.lifetime_basis.setText(
            f"주간 100%p 기준 / 누적 환산액 {usd(lifetime['cost'])} / 소모 {lifetime['delta']:g}%p"
            ', '+('산정 ' if lifetime['observed_priced_calls']<lifetime['observed_calls'] else '')+call_count(lifetime['observed_priced_calls'],lifetime['observed_calls'])+'호출'+
            '\n관측 기반 추정, 수집 공백 제외' if lifetime['total'] else '주간 할당량 가치를 계산할 사용량 기록을 기다리고 있습니다.')
        self.render_history()
        if hasattr(self,'update_summary'):self.update_summary()
        pending=self.report.get('history_pending',False)
        for name in ('cycle_choice','period','model_choice','manage_button'):
            node=getattr(self,name,None)
            if node is not None:node.setEnabled(not pending)
        if pending:
            self.status.setText('전체 이력을 읽지 못했습니다. 현재 주기는 계속 확인할 수 있습니다.' if self.report.get('history_failed') else
                                '현재 주기를 표시했습니다. 전체 이력을 계산하고 있습니다.')
            self.status.put(color='error' if self.report.get('history_failed') else 'muted');self.status.show()
            self.conversion.hide()
            if hasattr(self,'clear_cycle_button'):self.clear_cycle_button.setEnabled(False)
            return
        self.status.put(color='error')
        if self.model_choice.currentData() is not None:self.interval_filter.hide()
        days=self.period.currentData()
        prepared=self._view.get('summaries',{})
        query=self._prepared_statistics() if 'worker_revision' in self.report else None
        if 'worker_revision' in self.report and query is None:return
        summary=self.statistics=(query['statistics'] if query is not None else
                                 prepared[days] if days in prepared and self.period_end is None and
                                 (days is not None or self.period_start is None) else
                                 quota_statistics(self.report,self.period_start,self.period_end,self.mode_assumption.isChecked()))
        value=summary['per_percent']
        selected_model=self.model_choice.currentData()
        model_intervals=query['rows'] if query is not None else model_cost_intervals(summary['intervals'],selected_model)
        interval_count=query['model_totals']['count'] if query is not None else len(model_intervals)
        if selected_model is None:
            self.basis_label.setText('소모량');self.basis.setToolTip(TIPS['quota_used'])
            self.cost_label.setText('구독 가치 환산액')
            self.basis.setText(f"{summary['observed_delta']:g}%p" if summary['total'] else '—')
            self.cost_value.setText(usd(summary['observed_cost']) if summary['total'] else '—')
            self.conversion_empty.setText('환산할 로컬 사용 기록이 없습니다' if not summary['total'] else
                                           '주간 할당량 가치를 계산할 소모량이 아직 부족합니다.' if value is None else '')
        else:
            self.basis_label.setText('선택 모델 호출 수');self.basis.setToolTip('')
            calls=query['model_totals']['calls'] if query is not None else sum(row['model_calls'] for row in model_intervals)
            priced=query['model_totals']['priced'] if query is not None else sum(row['model_priced'] for row in model_intervals)
            self.cost_label.setText('확인된 구독 가치 환산액' if priced<calls else '구독 가치 환산액')
            self.basis.setText(f"{calls:,}회")
            known=([query['model_totals']['cost']] if query['model_totals']['cost'] is not None else []) if query is not None else [row['model_cost'] for row in model_intervals if row['model_cost'] is not None]
            self.cost_value.setText(usd(sum(known)) if known else '—')
            self.conversion_empty.setText('선택 기간에 이 모델의 관측된 호출이 없습니다' if not interval_count else
                                           '선택 모델의 구독 가치 환산액을 확인할 수 없습니다' if not known else
                                           f'{priced:,}/{calls:,}호출의 비용 확인' if priced<calls else '')
        self.conversion_empty.setVisible(bool(self.conversion_empty.text()))
        self.details.setVisible(bool(interval_count))
        self.details.toggle.setText(f"구간별 내역: {interval_count:,}개")
        has_unknown=query['has_unknown'] if query is not None else any(row.get('unknown_mode_calls') for row in summary['intervals'])
        self.mode_assumption.setVisible(has_unknown and selected_model is None)
        assumed=summary['assumed_per_percent']
        self.assumption_value.setVisible(has_unknown and selected_model is None and self.mode_assumption.isChecked())
        self.assumption_value.setText(f"Standard 단가 가정 {usd(assumed*100)} / 주간 100%p" if assumed is not None else 'Standard 단가를 적용할 비용 자료가 없습니다')
        self.render_intervals(automatic=automatic)

    def change_interval_filter(self,*_):
        self.page_index=0
        self.render_intervals()

    def render_intervals(self,*_,automatic=False):
        selected_model=self.model_choice.currentData()
        query=None
        if 'worker_revision' in self.report:
            if self._prepared_query is None or self._prepared_query[0]!=self._query_key():
                self.render(automatic=automatic);return
            query=self._prepared_query[1]
        mode=self.interval_filter.currentData()
        rows=query['rows'] if query is not None else model_cost_intervals(self.statistics['intervals'],selected_model)
        if query is None:
            if mode=='used':rows=[r for r in rows if not r['excluded']]
            elif mode=='excluded':rows=[r for r in rows if r['excluded'] or r.get('assumptions')]
        self.filtered_intervals=rows
        partial=query['partial'] if query is not None else any(r['model_priced']<r['model_calls'] if selected_model else r.get('priced_calls',r['calls'])<r['calls'] for r in rows)
        self.intervals.setHorizontalHeaderLabels(
            ['기간','계정 소모량','선택 모델 환산액','구간 구독 가치 환산액 비중','산정 / 모델 호출' if partial else '호출 수'] if selected_model else
            ['기간','소모량','구독 가치 환산액','주간할당량 가치','산정 / 전체 호출' if partial else '호출 수'])
        self.intervals.put(headerTips={'4':TIPS['coverage']} if partial else {})
        self.intervals.setVisible(bool(rows))
        self.interval_empty.setVisible(not rows)
        self.interval_empty.setText('선택 조건에 맞는 구간 0개' if self.statistics['total'] else '')
        self.interval_empty.setVisible(bool(self.statistics['total']) and not rows)
        self.interval_filter.setVisible(bool(self.statistics['total']) and selected_model is None)
        total_rows=query['total_rows'] if query is not None else len(rows)
        pages=query['pages'] if query is not None else max(1,math.ceil(len(rows)/self.page_size))
        self.paging.setVisible(pages>1)
        self.page_index=min(self.page_index,pages-1)
        self.page_status.setText(f'{self.page_index+1} / {pages}페이지, {total_rows:,}구간')
        self.previous_page.setEnabled(self.page_index>0);self.next_page.setEnabled(self.page_index+1<pages)
        if query is None:rows=rows[self.page_index*self.page_size:(self.page_index+1)*self.page_size]
        self.intervals.live_update=automatic
        self.intervals.set_rows(rows,self.format_interval)
        self.intervals.setFixedHeight(max(100,80+len(rows)*40))
        if not any(r['id']==self.selected_id for r in rows):self.selected_id=rows[0]['id'] if rows else None
        self.intervals.blockSignals(True)
        selected=next((i for i,r in enumerate(rows) if r['id']==self.selected_id),-1)
        if selected>=0:self.intervals.select_row(selected)
        self.intervals.blockSignals(False)
        self.render_models()

    def change_page(self,delta):
        self.page_index=max(0,self.page_index+delta)
        self.render_intervals()

    @staticmethod
    def format_interval(interval,col,role):
        if role==Qt.ToolTipRole:return ''
        if role==Qt.TextAlignmentRole:return (Qt.AlignLeft if col==0 else Qt.AlignRight)|Qt.AlignVCenter
        if interval.get('model_filter') is not None:
            values=[f"{clock(interval['start'])} → {clock(interval['end'])}",f"{interval['delta']:g}%p",
                    usd(interval['model_cost']),
                    f"{interval['model_share']:.1f}%" if interval['model_share'] is not None else '—',
                    call_count(interval['model_priced'],interval['model_calls'])]
        else:
            values=[f"{clock(interval['start'])} → {clock(interval['end'])}",f"{interval['delta']:g}%p",
                    usd(interval['cost']),usd(interval['per_percent']*100 if interval['per_percent'] is not None else None),
                    call_count(interval.get('priced_calls',interval['calls']),interval['calls'])]
        return values[col]

    def select_interval(self):
        row=self.intervals.currentRow()
        if 0<=row<self.intervals.rowCount():
            self.selected_id=self.intervals.model().rows[row]['id'];self.render_models()

    def render_models(self):
        interval=next((r for r in self.statistics['intervals'] if r['id']==self.selected_id),None)
        if interval is None:
            self.interval_detail.hide()
            self.table.setRowCount(0);self.table.hide();return
        self.interval_detail.show()
        reasons=', '.join(interval.get('assumptions',[]) if interval.get('forward_tracking') else interval['excluded'])
        excluded=sum(r['calls'] for r in interval['models'] if r['separate'])
        detail=f"{clock(interval['start'],True)} → {clock(interval['end'],True)}"
        if interval.get('reason') in RESET_NAMES:detail+=', '+RESET_NAMES[interval['reason']]+'로 종료'
        if excluded:detail+=f", 별도 한도 {excluded}호출 제외"
        if reasons:detail+='\n'+reasons
        self.interval_detail.setText(detail)
        selected_model=self.model_choice.currentData()
        models=[row for row in interval['models'] if row['model']==selected_model and not row['separate']] if selected_model else interval['models']
        self.table.setVisible(bool(models));self.table.setRowCount(len(models))
        for i,model in enumerate(models):
            values=[model['model']+(', '+model['service_tier'] if model['service_tier']!='미확인' else ''),f"{model['calls']:,}",f"{model['priced']:,}",usd(model['cost']),'별도 한도 (제외)' if model['separate'] else '일반 주간']
            for j,value in enumerate(values):
                cell=Cell(value);cell.setTextAlignment((Qt.AlignLeft if j in (0,4) else Qt.AlignRight)|Qt.AlignVCenter);self.table.setItem(i,j,cell)
        self.table.setFixedHeight(80+len(models)*40)
