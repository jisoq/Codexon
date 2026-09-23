"""Current reported allowance and auditable weekly local-call conversion."""
from datetime import datetime
import math
import os
from pathlib import Path
import time

from PySide6.QtCore import Qt, QTimer, Signal, QRectF, QPointF
from PySide6.QtGui import QColor, QPen, QPolygonF
from .presentation import Button, Choice, Column, Group, Row, Text, Toggle
from .table_model import Cell, Header, Table
from .pricing import usd
from .quota import quota_display
from .banked_resets import reset_credit_display
from .quota_cycles import quota_statistics, quota_value_history
from .quota_tracking import ResetTracker, RESET_NAMES, OBSERVATION_FRESHNESS
from .charts import Plot
from .lazy_table import LazyTable
from .theme import shared_theme
from .ui_details import Details


from .quota_view import clock, history_rows, prepare_quota_view, prepare_series, observation_label

def home_key(value):
    return os.path.normcase(str(Path(value).resolve())) if value else None


def remaining_axis(rows):
    values=[r['remaining'] for r in rows]
    if not values:return 0,100,25
    low,high=min(values),max(values)
    span=max(5,high-low)
    step=next((n for n in (1,2,5,10,20,25) if n>=span*1.2/4),25)
    padding=max(span*.1,(5-(high-low))/2)
    bottom=max(0,math.floor((low-padding)/step)*step)
    top=min(100,math.ceil((high+padding)/step)*step)
    return bottom,top,step


def model_cost_intervals(intervals, selected_model):
    """Keep account consumption separate from one model's known API cost."""
    if selected_model is None:
        return intervals
    result=[]
    for interval in intervals:
        models=[row for row in interval['models']
                if row.get('model')==selected_model and not row.get('separate')]
        if not models:
            continue
        known=[row['cost'] for row in models if row['cost'] is not None]
        cost=sum(known) if known else None
        whole=interval['cost']
        calls=sum(row['calls'] for row in models)
        priced=sum(row.get('priced',row['calls']) for row in models)
        total_calls=interval.get('calls',sum(row['calls'] for row in interval['models'] if not row.get('separate')))
        complete=priced==calls and interval.get('priced_calls',total_calls)>=total_calls
        result.append({**interval,
                       'model_filter':selected_model,
                       'model_cost':cost,
                       'model_share':cost/whole*100 if complete and cost is not None and whole is not None and whole>0 else None,
                       'model_calls':calls,
                       'model_priced':priced})
    return result


class QuotaBar(Plot):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(8)
        self.setFocusPolicy(Qt.NoFocus)
        self.empty_text=''
        self.remaining=None
        shared_theme().changed.connect(self.update)

    def set_value(self,value):
        self.remaining=value
        self.update()

    def paint(self,painter):
        palette=shared_theme().palette
        p=self.base()
        p.fillRect(self.rect(),QColor(palette['surface']))
        rect=QRectF(0,0,self.width(),6)
        p.fillRect(rect,QColor(palette['secondary']))
        if self.remaining is not None:
            p.fillRect(QRectF(0,0,self.width()*self.remaining/100,6),QColor(palette['accent']))


from .quota_chart import QuotaHistory


class QuotaPanel(Group):
    home_selected=Signal(str)

    def __init__(self,settings):
        super().__init__()
        self.settings=settings
        self.report={'cycles':[]}
        self.statistics=quota_statistics(self.report)
        self.quota=None
        self.issue=''
        self.selected_id=None
        self.period_start=None
        self.period_end=None
        self.page_index=0
        self.page_size=25
        self.expected_home=None
        layout=Column(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(20)
        self.home_choice=Choice();self.home_choice.setAccessibleName('한도 관측 대상 Codex 홈')
        self.home_choice.hide()
        self.home_choice.currentIndexChanged.connect(self.choose_home)
        layout.addWidget(self.home_choice)
        heading=Row();heading.setSpacing(12);heading.setMinimumHeight(24)
        title=Text('현재 잔여량');title.put(fontSize=18,bold=True)
        heading.addWidget(title,1)
        self.sample_summary=Text();self.sample_summary.put(fontSize=12,color='#5C6C80')
        heading.addWidget(self.sample_summary)
        layout.addLayout(heading)
        self.status=Text()
        self.status.put(color='#B42332',fontSize=13,wrap=False);self.status.setFixedHeight(24)
        heading.addWidget(self.status,1)
        current=Row();current.put(collapseBelow=720);current.setSpacing(16)
        limits=Group();limits_layout=Row(limits);limits_layout.put(collapseBelow=420);limits_layout.setSpacing(16)
        current.addWidget(limits,2)
        self.current={}
        for mode,name in (('weekly','주간'),('five_hour','5시간')):
            card=Group();card.put(background='#FFFFFF',radius=6)
            column=Column(card);column.setContentsMargins(20,16,20,16);column.setSpacing(10)
            label=Text(name);label.put(fontSize=14,color='#5C6C80')
            value=Text('—');value.put(fontSize=36,bold=True,noElide=True)
            bar=QuotaBar()
            evidence=Text();evidence.put(fontSize=13,color='#5C6C80',wrap=True)
            for node in (label,value,bar,evidence):column.addWidget(node)
            limits_layout.addWidget(card,1)
            self.current[mode]={'card':card,'value':value,'bar':bar,'evidence':evidence}
        self.reset_card=Group();self.reset_card.put(background='#FFFFFF',radius=6)
        reset_layout=Column(self.reset_card);reset_layout.setContentsMargins(20,16,20,16);reset_layout.setSpacing(10)
        reset_title=Text('Banked reset · 사용 초기화권');reset_title.put(fontSize=14,color='#5C6C80',wrap=True)
        self.reset_count=Text('—');self.reset_count.put(fontSize=36,bold=True,noElide=True)
        self.reset_note=Text();self.reset_note.put(fontSize=13,color='#5C6C80',wrap=True)
        self.reset_details=Details('상세 정보',compact=True)
        for node in (reset_title,self.reset_count,self.reset_note,self.reset_details):reset_layout.addWidget(node)
        current.addWidget(self.reset_card,1)
        layout.addLayout(current)
        self.current_empty=Text('한도를 조회하고 있습니다')
        self.current_empty.put(color='#5C6C80',fontSize=14)
        layout.addWidget(self.current_empty)
        scope=Row();scope.put(flow=True);scope.setSpacing(12)
        scope.addWidget(Text('상세 내역 조회 기간'))
        self.period=Choice()
        for label,days in (('전체 기간',None),('최근 7일',7),('최근 30일',30),('최근 90일',90)):
            self.period.addItem(label,days)
        self.period.setAccessibleName('잔여량과 환산 내역의 조회 기간');scope.addWidget(self.period)
        scope.addWidget(Text('주간 환산 모델'))
        self.model_choice=Choice();self.model_choice.addItem('전체 모델',None)
        self.model_choice.setAccessibleName('주간 한도 API 환산액의 모델 필터')
        scope.addWidget(self.model_choice)
        chart=Group();chart.put(background='#FFFFFF',radius=6)
        chart_layout=Column(chart);chart_layout.setContentsMargins(16,16,16,14);chart_layout.setSpacing(10)
        headline=Text('주간 사용량 API 동등 가치 · 전체 누적');headline.put(fontSize=15,bold=True)
        chart_layout.addWidget(headline)
        self.result=Text();self.result.put(fontSize=34,bold=True,noElide=True)
        chart_layout.addWidget(self.result)
        self.lifetime_basis=Text();self.lifetime_basis.put(fontSize=12,color='#5C6C80',wrap=True)
        chart_layout.addWidget(self.lifetime_basis)
        controls=Row();controls.setSpacing(12)
        self.chart_title=chart_title=Text('선택 주기의 사용량 추이');chart_title.put(fontSize=18,bold=True)
        controls.addWidget(chart_title,1)
        self.window=Choice();self.window.addItem('주간','weekly');self.window.addItem('5시간','five_hour')
        self.window.setAccessibleName('한도 이력 종류');controls.addWidget(self.window)
        chart_layout.addLayout(controls)
        self.cycle_choice=Choice();self.cycle_choice.setAccessibleName('사용량 리셋 주기 선택')
        chart_layout.addWidget(self.cycle_choice)
        self.cycle_value=Text();self.cycle_value.put(fontSize=22,bold=True,wrap=True)
        chart_layout.addWidget(self.cycle_value)
        self.cycle_basis=Text();self.cycle_basis.put(fontSize=12,color='#5C6C80',wrap=True)
        chart_layout.addWidget(self.cycle_basis)
        self.history=QuotaHistory();chart_layout.addWidget(self.history)
        self.history.selected.connect(self.select_observation)
        self.history_legend=Row();self.history_legend.put(flow=True);self.history_legend.setSpacing(16)
        for name,color in (('━ 잔여량 · %','#0F766E'),('━ 누적 API 환산액 · USD','#6551A4'),
                           ('┄ 주간 동등 가치 · USD','#8A641A'),('╌ 전체 누적 기준 · USD','#285FBC')):
            legend=Text(name);legend.put(fontSize=12,color=color);self.history_legend.addWidget(legend)
            if name.startswith('━ 잔여량'):self.remaining_legend=legend
        self.reset_legend=Text('◆ 사용량 리셋');self.reset_legend.put(fontSize=12,color='#5C6C80')
        self.history_legend.addWidget(self.reset_legend)
        chart_layout.addLayout(self.history_legend)
        self.cycle_choice.currentIndexChanged.connect(self.render_history)
        layout.addWidget(chart)
        self.conversion=Group();self.conversion.put(background='#FFFFFF',radius=6)
        conversion_layout=Column(self.conversion);conversion_layout.setContentsMargins(20,18,20,18);conversion_layout.setSpacing(16)
        title=Text('관측 내역');title.put(fontSize=18,bold=True);conversion_layout.addWidget(title)
        conversion_layout.addLayout(scope)
        metrics=Row();metrics.put(collapseBelow=620,columns=2);metrics.setSpacing(20)
        self.basis=Text();self.cost_value=Text()
        for name,value in (('소모량',self.basis),('API 환산액',self.cost_value)):
            metric=Column();label=Text(name);label.put(fontSize=13,color='#5C6C80');metric.addWidget(label)
            if value is self.basis:self.basis_label=label
            else:self.cost_label=label
            value.put(fontSize=26,bold=True,noElide=True);metric.addWidget(value);metrics.addLayout(metric,1)
        conversion_layout.addLayout(metrics)
        self.conversion_empty=Text();self.conversion_empty.put(fontSize=13,color='#5C6C80',wrap=True)
        conversion_layout.addWidget(self.conversion_empty)
        detail_body=Group();detail_layout=Column(detail_body);detail_layout.setSpacing(12)
        self.details=Details('구간별 내역',detail_body)
        conversion_layout.addWidget(self.details)
        layout.addWidget(self.conversion)
        self.interval_filter=Choice()
        for label,value in (('전체 구간','all'),('환산 구간','used'),('비용 제외 사유가 있는 구간','excluded')):
            self.interval_filter.addItem(label,value)
        self.interval_filter.setAccessibleName('환산 구간 상태');detail_layout.addWidget(self.interval_filter)
        self.intervals=LazyTable(['기간','소모량','API 환산액','주간할당량 가치','산정 / 전체 호출'])
        self.intervals.put(inline=True)
        self.intervals.verticalHeader().setDefaultSectionSize(40)
        self.intervals.horizontalHeader().setSectionResizeMode(0,Header.Stretch)
        for col,width in enumerate((245,95,120,125,140)):self.intervals.setColumnWidth(col,width)
        detail_layout.addWidget(self.intervals)
        self.interval_empty=Text();detail_layout.addWidget(self.interval_empty)
        paging=self.paging=Row()
        self.previous_page=Button('이전 구간');self.next_page=Button('다음 구간')
        for button,icon,title in ((self.previous_page,'left','이전 구간'),(self.next_page,'right','다음 구간')):
            button.setText('');button.put(iconName=icon,flat=True);button.setFixedSize(36,36);button.setAccessibleName(title);button.setToolTip(title)
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
        self.table.setHorizontalHeaderLabels(['모델 · 요청 모드','호출 수','산정 호출 수','API 환산액','한도'])
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.horizontalHeader().setSectionResizeMode(0,Header.Stretch)
        for col,width in enumerate((320,90,110,130,130)):self.table.setColumnWidth(col,width)
        detail_layout.addWidget(self.table)
        self.interval_filter.currentIndexChanged.connect(self.change_interval_filter)
        self.intervals.itemSelectionChanged.connect(self.select_interval)
        self.window.currentIndexChanged.connect(self.render_history)
        self.period.currentIndexChanged.connect(self.change_period)
        self.model_choice.currentIndexChanged.connect(self.change_model)
        self.mode_assumption.toggled.connect(self.render)
        layout.addStretch()
        self.render()

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
        self.quota=value.get('quota');self.issue=value.get('issue','')
        self.report=value.get('report') or {'cycles':[]}
        if getattr(self,'defer_render',False):self._render_dirty=True
        else:self.render(automatic=True)
        return True

    def refresh_status(self):
        if getattr(self,'_render_dirty',False):
            self._render_dirty=False;self.render(automatic=True);return
        home=self.report.get('home')
        self.status.setText(self.issue)
        self.status.setVisible(bool(self.issue))
        now=time.time()
        quota=self.quota
        resets=reset_credit_display(quota,now)
        self.reset_count.setText(resets['count']);self.reset_note.setText(resets['note'])
        self.reset_details.set_sections(resets['sections']);self.reset_details.setVisible(bool(resets['sections']))
        available=set((quota or {}).get('windows',{})) | set((quota or {}).get('unlimited_windows',[])) | set((quota or {}).get('window_conflicts',[]))
        self.current_empty.setVisible(not available and not self.issue)
        tracking=self.report.get('tracking')
        observed=(quota or {}).get('observed_at')
        state='수집 중' if tracking and tracking.get('enabled') else '수집 중지' if tracking else ''
        updated=('방금 갱신' if now-observed<60 else f'{int(max(0,now-observed)//60)}분 전 갱신') if observed else ''
        self.sample_summary.setText(' · '.join(s for s in (state,updated) if s))
        for mode,card in self.current.items():
            # Missing windows are not a numeric limit, an error, or unlimited.
            card['card'].setVisible(mode in available)
            display=quota_display(quota,mode,now)
            remaining=display['remaining']
            card['value'].setText(f'{remaining:.1f}%' if remaining is not None else
                                  display['state'] if display['state'] in ('제한 없음','관측 충돌','초기화 후 확인 중') else '—')
            card['bar'].set_value(remaining)
            window=(quota or {}).get('windows',{}).get(mode,{})
            evidence=[]
            reset=window.get('resets_at')
            if reset:
                seconds=max(0,reset-now)
                days=int(seconds//86400);hours=int(seconds%86400//3600);minutes=int(seconds%3600//60)
                duration=(f'{days}일 {hours}시간' if days else f'{hours}시간 {minutes}분' if hours else f'{minutes}분' if minutes else '1분 미만')
                evidence.append(f'정기 초기화까지 {duration} · {clock(reset)}' if reset>now else '다음 정기 초기화 시각 갱신 중')
            if remaining is None and mode in (quota or {}).get('windows',{}) and display['state']=='미확인':
                evidence.append('최근 한도 조회가 갱신되지 않았습니다')
            card['evidence'].setText('\n'.join(evidence))

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

    def render_history(self,*_):
        from bisect import bisect_left
        weekly=self.window.currentData()=='weekly'
        if getattr(self,'_value_report',None) is not self.report:
            had_periods=bool(getattr(self,'_view',{}).get('periods'))
            self._view=self.report.get('view') or prepare_quota_view(self.report)
            self._value_report=self.report
            selected=self.cycle_choice.currentData() if had_periods else None
            self.cycle_choice.blockSignals(True);self.cycle_choice.clear()
            self.cycle_choice.addItem('전체','all')
            for period in self._view['periods']:
                self.cycle_choice.addItem(period['label'],period['start'])
            index=self.cycle_choice.findData(selected)
            self.cycle_choice.setCurrentIndex(index if index>=0 else self.cycle_choice.count()-1)
            self.cycle_choice.blockSignals(False)
        index=self.cycle_choice.currentIndex()
        all_cycles=index==0
        lifetime=self._view['lifetime']
        period=(dict(start='all',series=self._view['overall'],cost=lifetime['cost'],delta=lifetime['delta'],
                     priced_calls=lifetime['observed_priced_calls'],calls=lifetime['observed_calls'],
                     value=lifetime['per_percent']*100 if lifetime['per_percent'] is not None else None)
                if all_cycles else self._view['periods'][index-1] if index>0 else None)
        series=(period['series'] if period else prepare_series([])) if weekly else self._view['five_hour']
        old=self.history.rows[self.history.cursor]['at'] if self.history.rows else None
        same=getattr(self,'_selected_cycle',None)==(weekly,period['start'] if period else None)
        follow=not self.history.rows or self.history.cursor==len(self.history.rows)-1
        self._selected_cycle=(weekly,period['start'] if period else None)
        self.history.money=weekly;self.history.reference=self.lifetime_value
        self.history.empty_text='표시할 사용 기록이 없습니다' if weekly else '수집된 잔여량이 없습니다'
        self.chart_title.setText('전체 사용량 추이' if weekly and all_cycles else '선택 주기의 사용량 추이' if weekly else '5시간 잔여량 추이')
        self.remaining_legend.setText('━ 누적 소모량 · %p' if all_cycles else '━ 잔여량 · %')
        self.reset_legend.setVisible(weekly and all_cycles)
        self.history.set_series(series)
        rows=self.history.rows
        self.history.cursor=(len(rows)-1 if rows and (follow or not same) else
                             min(bisect_left(series['times'],old),len(rows)-1) if rows and old is not None else 0)
        if rows:self.select_observation(rows[self.history.cursor])
        if period:
            self.cycle_value.setText(f"누적 소모량 {period['delta']:g}%p" if all_cycles else '선택 주기 '+usd(period['value']))
            self.cycle_basis.setText(f"주간 100%p 기준 · 누적 API {usd(period['cost'])} / 소모 {period['delta']:g}%p"
                                    f" · 산정 {period['priced_calls']:,}/{period['calls']:,}호출")
        else:
            self.cycle_value.setText('선택 주기 —');self.cycle_basis.setText('관측된 사용량 리셋 주기가 없습니다')
        for node in (self.cycle_choice,self.cycle_value,self.cycle_basis,self.history_legend):node.setVisible(weekly)

    def select_observation(self,row):
        self.history.setAccessibleName(observation_label(row,self.history.money))

    def render(self,*_,automatic=False):
        self.refresh_status()
        self.lifetime_statistics=(self.report.get('view') or {}).get('lifetime') or quota_statistics(self.report)
        lifetime=self.lifetime_statistics
        selected_model=self.model_choice.currentData()
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
        self.result.setToolTip(f"전체 누적 {usd(lifetime['cost'])} ÷ {lifetime['delta']:g}%p × 100 · 주간 100%p의 관측 기반 추정")
        self.lifetime_basis.setText(
            f"주간 100%p 기준 · 누적 API {usd(lifetime['cost'])} / 소모 {lifetime['delta']:g}%p"
            f" · 산정 {lifetime['observed_priced_calls']:,}/{lifetime['observed_calls']:,}호출"
            '\n관측 기반 추정 · 수집 공백 제외' if lifetime['total'] else '계산에 필요한 관측을 기다리고 있습니다')
        self.render_history()
        days=self.period.currentData()
        prepared=self._view.get('summaries',{})
        summary=self.statistics=(prepared[days] if days in prepared and self.period_end is None and
                                  (days is not None or self.period_start is None) else
                                  quota_statistics(self.report,self.period_start,self.period_end,self.mode_assumption.isChecked()))
        value=summary['per_percent']
        selected_model=self.model_choice.currentData()
        model_intervals=model_cost_intervals(summary['intervals'],selected_model)
        if selected_model is None:
            self.basis_label.setText('소모량')
            self.cost_label.setText('API 환산액')
            self.basis.setText(f"{summary['observed_delta']:g}%p" if summary['total'] else '—')
            self.cost_value.setText(usd(summary['observed_cost']) if summary['total'] else '—')
            self.conversion_empty.setText('환산할 로컬 사용 기록이 없습니다' if not summary['total'] else
                                           '소모량이 누적되면 주간할당량 가치를 표시합니다' if value is None else '')
        else:
            self.basis_label.setText('선택 모델 호출 수')
            calls=sum(row['model_calls'] for row in model_intervals)
            priced=sum(row['model_priced'] for row in model_intervals)
            self.cost_label.setText('확인된 API 환산액' if priced<calls else 'API 환산액')
            self.basis.setText(f"{calls:,}회")
            known=[row['model_cost'] for row in model_intervals if row['model_cost'] is not None]
            self.cost_value.setText(usd(sum(known)) if known else '—')
            self.conversion_empty.setText('선택 기간에 이 모델의 관측된 호출이 없습니다' if not model_intervals else
                                           '선택 모델의 API 비용을 확인할 수 없습니다' if not known else
                                           f'{priced:,}/{calls:,}호출의 비용 확인' if priced<calls else '')
        self.conversion_empty.setVisible(bool(self.conversion_empty.text()))
        self.details.setVisible(bool(model_intervals))
        self.details.toggle.setText(f"구간별 내역 · {len(model_intervals):,}개")
        has_unknown=any(row.get('unknown_mode_calls') for row in summary['intervals'])
        self.mode_assumption.setVisible(has_unknown and selected_model is None)
        assumed=summary['assumed_per_percent']
        self.assumption_value.setVisible(has_unknown and selected_model is None and self.mode_assumption.isChecked())
        self.assumption_value.setText(f"Standard 단가 가정 {usd(assumed*100)} / 주간 100%p" if assumed is not None else 'Standard 단가를 적용할 비용 자료가 없습니다')
        if self.report.get('error'):
            self.status.setText(self.issue or '한도 기록을 읽지 못했습니다. 마지막 저장값을 확인해 주세요.')
            self.status.show()
        self.render_intervals(automatic=automatic)

    def change_interval_filter(self,*_):
        self.page_index=0
        self.render_intervals()

    def render_intervals(self,*_,automatic=False):
        selected_model=self.model_choice.currentData()
        self.intervals.setHorizontalHeaderLabels(
            ['기간','계정 소모량','선택 모델 API','구간 API 비용 비중','산정 / 모델 호출'] if selected_model else
            ['기간','소모량','API 환산액','주간할당량 가치','산정 / 전체 호출'])
        mode=self.interval_filter.currentData();rows=model_cost_intervals(self.statistics['intervals'],selected_model)
        if mode=='used':rows=[r for r in rows if not r['excluded']]
        elif mode=='excluded':rows=[r for r in rows if r['excluded'] or r.get('assumptions')]
        self.filtered_intervals=rows
        self.intervals.setVisible(bool(rows))
        self.interval_empty.setVisible(not rows)
        self.interval_empty.setText('선택 조건에 맞는 구간 0개' if self.statistics['total'] else '')
        self.interval_empty.setVisible(bool(self.statistics['total']) and not rows)
        self.interval_filter.setVisible(bool(self.statistics['total']) and selected_model is None)
        pages=max(1,math.ceil(len(rows)/self.page_size))
        self.paging.setVisible(pages>1)
        self.page_index=min(self.page_index,pages-1)
        self.page_status.setText(f'{self.page_index+1} / {pages}페이지 · {len(rows):,}구간')
        self.previous_page.setEnabled(self.page_index>0);self.next_page.setEnabled(self.page_index+1<pages)
        rows=rows[self.page_index*self.page_size:(self.page_index+1)*self.page_size]
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
                    f"{interval['model_priced']:,}/{interval['model_calls']:,}"]
        else:
            values=[f"{clock(interval['start'])} → {clock(interval['end'])}",f"{interval['delta']:g}%p",
                    usd(interval['cost']),usd(interval['per_percent']*100 if interval['per_percent'] is not None else None),
                    f"{interval.get('priced_calls',interval['calls']):,}/{interval['calls']:,}"]
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
        reasons=' · '.join(interval.get('assumptions',[]) if interval.get('forward_tracking') else interval['excluded'])
        excluded=sum(r['calls'] for r in interval['models'] if r['separate'])
        detail=f"{clock(interval['start'],True)} → {clock(interval['end'],True)}"
        if interval.get('reason') in RESET_NAMES:detail+=' · '+RESET_NAMES[interval['reason']]+'로 종료'
        if excluded:detail+=f" · 별도 한도 {excluded}호출 제외"
        if reasons:detail+='\n'+reasons
        self.interval_detail.setText(detail)
        selected_model=self.model_choice.currentData()
        models=[row for row in interval['models'] if row['model']==selected_model and not row['separate']] if selected_model else interval['models']
        self.table.setVisible(bool(models));self.table.setRowCount(len(models))
        for i,model in enumerate(models):
            values=[model['model']+(' · '+model['service_tier'] if model['service_tier']!='미확인' else ''),f"{model['calls']:,}",f"{model['priced']:,}",usd(model['cost']),'별도 한도 · 제외' if model['separate'] else '일반 주간']
            for j,value in enumerate(values):
                cell=Cell(value);cell.setTextAlignment((Qt.AlignLeft if j in (0,4) else Qt.AlignRight)|Qt.AlignVCenter);self.table.setItem(i,j,cell)
        self.table.setFixedHeight(80+len(models)*40)
