"""Calendar performance trends with a persistent interval analysis panel."""
import json
from datetime import datetime, timedelta
from bisect import bisect_left, bisect_right
from math import ceil, log1p, isfinite
from time import mktime
from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, QDate, Slot
from PySide6.QtGui import QColor, QPen, QPolygonF, QPainter
from .charts import AnalyticalPlot, Plot, dynamic_bounds, value_text
from .presentation import Node, Group, Column, Row, Button, Text, Choice, Input, DateInput, Scroll
from .theme import shared_theme
from .i18n import tr, Verbatim


def clear(container):
    old=container._nodes;container._nodes=[];container.structureChanged.emit()
    for node in old:node.setParent(None);node.deleteLater()


def axis_number(value):
    if abs(value)>=1000000:return f'{value/1000000:.1f}M'
    if abs(value)>=10000:return f'{value/1000:.1f}k'
    return f'{value:,.4f}'.rstrip('0').rstrip('.') if abs(value)<.01 else f'{value:,.2f}'.rstrip('0').rstrip('.')


def time_ticks(start,end,width):
    """Choose readable local-calendar ticks for the visible time range."""
    spacing=(end-start)/max(1,width/72)
    steps=(1,2,5,10,15,30,60,120,300,600,900,1800,3600,7200,10800,21600,43200,
           86400,172800,259200,432000,604800,864000,1209600)
    step=next((s for s in steps if s>=spacing),None)
    first=datetime.fromtimestamp(start);last=datetime.fromtimestamp(end);ticks=[start]
    if step is not None:
        origin=first.replace(hour=0,minute=0,second=0,microsecond=0)
        if step in (604800,1209600):origin-=timedelta(days=origin.weekday())
        current=origin+timedelta(seconds=ceil((first-origin).total_seconds()/step)*step)
        while current<last:
            if current>first:
                stamp=mktime(current.timetuple())
                if ticks[-1]<stamp<end:ticks.append(stamp)
            current+=timedelta(seconds=step)
    else:
        months=next((n for n in (1,2,3,6,12,24,60,120) if n*2629800>=spacing),120)
        while months*2629800<spacing:months*=2
        index=first.year*12+first.month-1
        index-=index%months
        current=datetime(index//12,index%12+1,1)
        while current<last:
            if current>first:ticks.append(mktime(current.timetuple()))
            index+=months;current=datetime(index//12,index%12+1,1)
        step=months*2629800
    ticks.append(end)
    return ticks,step


def time_label(stamp,previous,step):
    current=datetime.fromtimestamp(stamp)
    if step<86400:
        clock=current.strftime('%H:%M:%S' if step<60 else '%H:%M')
        if previous is not None and current.date()==previous.date():return clock
        date=current.strftime('%Y/%m/%d' if previous is None or current.year!=previous.year else
                              '%m/%d' if current.month!=previous.month else '%d')
        return clock+'\n'+date
    if previous is None or current.year!=previous.year:return current.strftime('%m/%d\n%Y')
    return current.strftime('%m/%d' if current.month!=previous.month else '%d')


def caption(text,size=13,color='muted',bold=False):
    node=Text(text);node.put(fontSize=size,color=color,bold=bold,wrap=True);return node


def button(text,callback):
    node=Button(text);node.clicked.connect(callback);return node


def metric_unit(unit):
    return {'cost':'USD','output_speed':'tok/s','duration':tr('초'),'cache_ratio':'%',
            'reasoning':tr('토큰'),'input':tr('토큰'),'count':tr('호출')}[unit]


def aggregation_text(unit):
    return tr('총 출력 토큰 ÷ 총 소요시간' if unit=='output_speed' else
              '세션 동일 비중 평균' if unit=='cache_ratio' else '구간 호출 수' if unit=='count' else '호출당 평균')


def interval_dates(start,end):
    first,last=datetime.fromtimestamp(start),datetime.fromtimestamp(end)
    return first.strftime('%Y.%m.%d %H:%M')+'\n→ '+last.strftime('%m.%d %H:%M' if first.year==last.year else '%Y.%m.%d %H:%M')


def series_pen(color,mode,width=3.2):
    pen=QPen(color,width,Qt.DashLine if mode=='Fast' else Qt.SolidLine)
    if mode=='Fast':pen.setDashPattern([4,2])
    return pen


def series_mark(p,xy,mode,size=3):
    if mode=='Fast':
        p.drawPolygon(QPolygonF([QPointF(xy.x(),xy.y()-size-1),QPointF(xy.x()+size+1,xy.y()),QPointF(xy.x(),xy.y()+size+1),QPointF(xy.x()-size-1,xy.y())]))
    else:p.drawEllipse(xy,size,size)


class SeriesLegend(Plot):
    def __init__(self,model,mode,active):
        super().__init__();self.model=model;self.mode=mode;self.active=active
        self.setFixedSize(48,26);self.setFocusPolicy(Qt.NoFocus)
        shared_theme().changed.connect(self.update)

    def paint(self,painter):
        p=self._painter;p.setRenderHint(QPainter.Antialiasing)
        color=QColor(shared_theme().model_mode_color(self.model,self.mode));color.setAlphaF(1 if self.active else .2)
        p.setPen(series_pen(color,self.mode));p.drawLine(QPointF(3,13),QPointF(45,13))
        p.setPen(QPen(color,1.5));p.setBrush(QColor(shared_theme().palette['surface']))
        series_mark(p,QPointF(24,13),self.mode)


class DistributionRange(AnalyticalPlot):
    def __init__(self):
        super().__init__();self.setFixedHeight(56);self.put(clickEnabled=False)
        self.setFocusPolicy(Qt.NoFocus);self.stats={}

    def set_stats(self,stats):
        self.stats=stats;self.set_rows([stats] if stats.get('minimum') is not None else [])

    def paint(self,painter):
        p=self.base()
        if not self.rows:return
        s=self.stats;lo,hi=dynamic_bounds([s['minimum'],s['maximum']])
        x=lambda value:12+(self.width()-24)*(value-lo)/(hi-lo)
        p.setPen(QPen(self.color('border'),2));p.drawLine(QPointF(x(s['minimum']),18),QPointF(x(s['maximum']),18))
        if s['p10'] is not None:
            shade=self.color('accent');shade.setAlphaF(.2)
            p.fillRect(QRectF(x(s['p10']),10,max(2,x(s['p90'])-x(s['p10'])),16),shade)
        p.setPen(QPen(self.color('accent'),3));p.drawLine(QPointF(x(s['median']),6),QPointF(x(s['median']),30))
        p.setPen(self.color('muted'))
        font=p.font();font.setPixelSize(11);p.setFont(font)
        p.drawText(QRectF(0,32,self.width()/2,20),Qt.AlignLeft,tr('최소')+' '+axis_number(s['minimum']))
        p.drawText(QRectF(self.width()/2,32,self.width()/2,20),Qt.AlignRight,tr('최대')+' '+axis_number(s['maximum']))


class IntervalDetails(Group):
    def __init__(self):
        super().__init__();self.setObjectName('panel')
        body=Column(self);body.setContentsMargins(18,16,18,16);body.setSpacing(14)
        self.title=caption('구간 분석',14,'ink',True);body.addWidget(self.title)
        self.identity=caption('',12);body.addWidget(self.identity)
        hero=Row();hero.setSpacing(8);body.addLayout(hero)
        self.value=caption('—',34,'ink',True);self.value.put(wrap=False);hero.addWidget(self.value)
        self.unit=caption('',13);self.unit.put(wrap=False);hero.addWidget(self.unit,1)
        self.period=caption('',12);body.addWidget(self.period)
        self.hint=caption('그래프에 마우스를 올리면\n구간 통계가 표시됩니다.',12);body.addWidget(self.hint)
        self.comparison=Group();self.comparison.put(background='secondary',radius=6);body.addWidget(self.comparison)
        compare=Column(self.comparison);compare.setContentsMargins(12,10,12,10);compare.setSpacing(5)
        compare.addWidget(caption('직전 구간 대비',11))
        self.change=caption('',24,'accent',True);compare.addWidget(self.change)
        self.previous=caption('',12);compare.addWidget(self.previous)
        self.absolute_change=caption('',12,'ink');compare.addWidget(self.absolute_change)
        self.sample_heading=caption('호출 표본',13,'ink',True);body.addWidget(self.sample_heading)
        self.samples=Row();self.samples.setSpacing(6);body.addLayout(self.samples)
        self.sample_values={};self.sample_notes={}
        for key,label in (('eligible','유효'),('missing','누락'),('sessions','세션')):
            card=Group();card.put(background='secondary',radius=4);self.samples.addWidget(card,1)
            column=Column(card);column.setContentsMargins(8,8,8,8);column.setSpacing(4)
            column.addWidget(caption(label,11));value=caption('—',20,'ink',True);column.addWidget(value)
            note=caption('',10);column.addWidget(note);self.sample_values[key]=value;self.sample_notes[key]=note
        self.distribution=Group();body.addWidget(self.distribution);stats=Column(self.distribution);stats.setSpacing(6)
        self.distribution_title=caption('개별 호출 분포',13,'ink',True);stats.addWidget(self.distribution_title)
        middle=Row();middle.setSpacing(8);stats.addLayout(middle)
        middle.addWidget(caption('중앙값',12),1);self.median=caption('',18,'ink',True);self.median.put(wrap=False);middle.addWidget(self.median)
        self.range_plot=DistributionRange();stats.addWidget(self.range_plot)
        self.range_text=caption('',12);stats.addWidget(self.range_text)
        separator=Group();separator.put(background='border');separator.setFixedHeight(1);body.addWidget(separator)
        body.addWidget(caption('계산 근거',13,'ink',True))
        self.formula=caption('',11);body.addWidget(self.formula)
        self.total_rows=[]
        for _ in range(3):
            row=Row();row.setSpacing(8);body.addLayout(row)
            label=caption('',12);value=caption('',13,'ink',True);value.setAlignment(Qt.AlignRight)
            row.addWidget(label,1);row.addWidget(value,1);self.total_rows.append((row,label,value))
        body.addStretch();self.clear()

    def clear(self):
        self.title.setText(tr('구간 분석'));self.identity.setText('');self.value.setText('—');self.unit.setText('')
        self.period.setText('');self.hint.show();self.comparison.hide();self.distribution.hide()
        for value in self.sample_values.values():value.setText('—');value.put(color='ink')
        for note in self.sample_notes.values():note.setText('')
        self.formula.setText('')
        for row,_,_ in self.total_rows:row.hide()

    def set_totals(self,rows):
        for index,(row,label,value) in enumerate(self.total_rows):
            row.setVisible(index<len(rows))
            if index<len(rows):label.setText(tr(rows[index][0]));value.setText(Verbatim(rows[index][1]))

    def set_point(self,point,spec):
        unit=spec['unit'];fmt=lambda value:tr(value_text(value,unit))
        self.hint.hide();self.title.setText(tr(spec['title']))
        self.identity.setText(Verbatim(point['model']+' / '+point['service_tier']+(' / '+point['effort'] if point.get('effort') else '')))
        self.value.setText(Verbatim(axis_number(point['value'])));self.unit.setText(Verbatim(metric_unit(unit)))
        period=interval_dates(point['start'],point['end'])
        if not point['complete']:period+='  /  '+tr(point['status'])
        self.period.setText(Verbatim(period));self.comparison.show()
        if point['baseline'] is None:
            self.change.put(fontSize=14,color='muted');self.change.setText(tr('비교 구간 없음'))
            self.previous.setText(tr('직전 완전 구간 비교 불가'));self.absolute_change.setText('')
        else:
            delta=point['delta'];relative=point['baseline'] and unit!='cache_ratio'
            change=f'{delta/point["baseline"]:+.1%}' if relative else f'{delta:+.1f}%p' if unit=='cache_ratio' else ('+' if delta>=0 else '−')+fmt(abs(delta))
            self.change.put(fontSize=26,color='accent');self.change.setText(Verbatim(change))
            self.previous.setText(Verbatim(tr('직전 구간')+'  '+fmt(point['baseline'])+' / '+tr('표본')+f" {point['baseline_count']:,}"))
            self.absolute_change.setText(Verbatim(tr('차이')+'  '+('+' if delta>=0 else '−')+fmt(abs(delta)) if relative else ''))
        eligible,total=point['coverage'];self.samples.show();self.sample_heading.show()
        self.sample_heading.setText(tr('세션 표본' if unit=='cache_ratio' else '호출 표본'))
        for key,value in (('eligible',eligible),('missing',total-eligible),('sessions',point['sessions'])):
            self.sample_values[key].setText(Verbatim(f'{value:,}'))
        self.sample_values['missing'].put(color='warning' if total>eligible else 'ink')
        self.sample_notes['eligible'].setText(Verbatim(tr('전체')+f' {total:,}'))
        self.sample_notes['missing'].setText(tr('계산 제외'));self.sample_notes['sessions'].setText(tr('구간 합계'))
        stats=point['distribution'];self.distribution.setVisible(unit!='count')
        self.distribution_title.setText(tr('세션별 적중률 분포' if unit=='cache_ratio' else '개별 호출 분포'))
        self.median.setText(Verbatim(fmt(stats['median'])));self.range_plot.set_stats(stats)
        self.range_text.setText(Verbatim(tr('중앙 80% 범위')+'\n'+fmt(stats['p10'])+' — '+fmt(stats['p90'])) if stats['p10'] is not None else tr('분포 범위 표시: 표본 10개 이상'))
        totals=point['totals'];self.formula.setText(Verbatim(aggregation_text(unit)))
        if unit=='output_speed':rows=[('총 출력 토큰',f"{totals['output']:,}"),('총 소요시간',tr(value_text(totals['seconds'],'duration')))]
        elif unit=='cache_ratio':rows=[('유효 입력 토큰',f"{totals['input']:,}"),('캐시 입력 토큰',f"{totals['cached']:,}"),('유효 호출',f"{totals['valid_calls']:,} / {point['calls']:,}")]
        elif unit=='count':rows=[('구간 호출 수',f"{point['calls']:,}")]
        else:rows=[('유효 표본 합계',fmt(totals.get('sum')))]
        self.set_totals(rows)


class PerformancePlot(AnalyticalPlot):
    def __init__(self,panel):
        super().__init__();self.panel=panel;self.data={};self.times=[];self.strip=[]
        self.setFixedHeight(390);self.bounds=(0,1);self.shared_bounds=False
        self.put(clickEnabled=False)
        self.setAccessibleName('성능 추이 / 방향키로 시각 확인')

    def set_data(self,data,bounds=None):
        self.data=data;self.metric=data['unit']
        self.set_rows(sorted((point for line in data['lines'] for point in line['points'] if point['value'] is not None),key=lambda p:p['ts']))
        # Only displayed interval values define the axis. Raw observations remain in the anomaly strip.
        self.shared_bounds=bounds is not None
        self.times=[p['ts'] for p in self.rows];self.bounds=bounds if bounds is not None else dynamic_bounds([p['value'] for p in self.rows]);self.rebuild_strip()

    def selected_style(self,p):
        return p['model'] not in ('미확인','codex-auto-review') and self.panel.is_model_selected(p['model']) and self.panel.modes.get(p['service_tier'],False)

    def ink(self,p,alpha=1):
        selected=self.selected_style(p)
        color=QColor(shared_theme().model_mode_color(p['model'],p['service_tier']) if selected else self.color('muted'))
        color.setAlphaF(alpha if selected else min(.1,alpha));return color

    def x(self,ts):
        a,z=self.panel.range;return 76+(self.width()-96)*(ts-a)/(z-a)

    def y(self,value):
        lo,hi=self.bounds;return self.height()-72-((value-lo)/(hi-lo))*(self.height()-100)

    def strip_top(self):return self.height()-62

    def xy(self,p):return QPointF(self.x(p['ts']),self.y(p['value']))

    def rebuild_strip(self):
        cells={};a,z=self.panel.range
        for point in self.data.get('points',[]):
            if not point.get('count') or not self.selected_style(point):continue
            index=min(79,max(0,int((point['ts']-a)/(z-a)*80)))
            cell=cells.setdefault((index,point['direction']),dict(index=index,direction=point['direction'],count=0))
            cell['count']+=point['count']
        self.strip=list(cells.values());self.update()

    def strip_cell(self,x,y):
        if not self.strip_top()<=y<=self.strip_top()+20:return None
        index=int((x-76)/max(1,self.width()-96)*80)
        direction=1 if y<self.strip_top()+10 else -1
        return next((c for c in self.strip if c['index']==index and c['direction']==direction),None)

    def time_labels(self,metrics):
        ticks,step=time_ticks(*self.panel.range,max(1,self.width()-96))
        labels=[]
        for stamp in ticks:
            while True:
                previous=datetime.fromtimestamp(labels[-1][0]) if labels else None
                text=time_label(stamp,previous,step)
                width=max(metrics.horizontalAdvance(line) for line in text.split('\n'))+4
                left=max(76,min(self.width()-20-width,self.x(stamp)-width/2))
                rect=QRectF(left,self.height()-36,width,36)
                if not labels or rect.left()>=labels[-1][2].right()+12:
                    labels.append((stamp,text,rect));break
                if stamp!=ticks[-1]:break
                # Keep the range endpoint, recomputing its context after removing a crowded tick.
                labels.pop()
        return labels

    def paint(self,painter):
        p=self.base()
        if not self.rows and not self.shared_bounds:
            return
        lo,hi=self.bounds
        p.setPen(self.color('muted'));p.drawText(QRectF(76,0,self.width()-96,20),Qt.AlignLeft|Qt.AlignVCenter,tr('단위')+': '+metric_unit(self.metric))
        p.setPen(QPen(self.color('grid'),1))
        for f in (0,1/3,2/3,1):
            y=self.y(lo+(hi-lo)*f);p.drawLine(QPointF(76,y),QPointF(self.width()-20,y))
            p.setPen(self.color('muted'));p.drawText(QRectF(0,y-10,66,20),Qt.AlignRight|Qt.AlignVCenter,axis_number(lo+(hi-lo)*f))
            p.setPen(QPen(self.color('grid'),1))
        for line in sorted(self.data.get('lines',[]),key=self.selected_style):
            points=line['points'];previous=None
            color=self.ink(line);mode=line['service_tier']
            p.setPen(series_pen(color,mode))
            for point in points:
                if point['value'] is None:previous=None;continue
                xy=self.xy(point)
                if previous and point['bucket']==previous[0]+1:p.drawLine(previous[1],xy)
                previous=(point['bucket'],xy)
            p.setPen(QPen(color,1.5));p.setBrush(self.color('surface'))
            for point in points:
                if point['value'] is not None:series_mark(p,self.xy(point),mode)
        strip=self.strip_top()
        p.setPen(self.color('muted'));p.drawText(QRectF(0,strip-1,66,22),Qt.AlignRight|Qt.AlignVCenter,tr('이탈'))
        p.fillRect(QRectF(76,strip,self.width()-96,20),self.color('panel'))
        maximum=max((cell['count'] for cell in self.strip),default=1)
        for cell in self.strip:
            color=self.color('warning' if cell['direction']>0 else 'accent');color.setAlphaF(.25+.65*log1p(cell['count'])/log1p(maximum))
            w=(self.width()-96)/80
            p.fillRect(QRectF(76+cell['index']*w,strip if cell['direction']>0 else strip+11,max(1,w-1),8),color)
        stamp=self.panel.inspection_time
        if stamp is not None and self.panel.range[0]<=stamp<=self.panel.range[1]:
            p.setPen(QPen(self.color('muted'),1,Qt.DashLine));p.drawLine(QPointF(self.x(stamp),15),QPointF(self.x(stamp),strip+20))
        p.setPen(self.color('muted'))
        for stamp,text,rect in self.time_labels(p.fontMetrics()):
            p.drawLine(QPointF(self.x(stamp),strip+21),QPointF(self.x(stamp),strip+25))
            p.drawText(rect,Qt.AlignHCenter|Qt.AlignTop,text)

    def nearest(self,x,y):
        if not self.rows:return None
        a,z=self.panel.range;t=a+(x-76)/max(1,self.width()-96)*(z-a)
        tolerance=(z-a)*10/max(1,self.width()-96)
        left=bisect_left(self.times,t-tolerance);right=bisect_right(self.times,t+tolerance)
        candidates=self.rows[left:right]
        if not candidates:
            i=bisect_left(self.times,t);candidates=self.rows[max(0,i-1):i+1]
        selected=[r for r in candidates if self.selected_style(r)];candidates=selected or candidates
        return min(candidates,key=lambda r:(self.x(r['ts'])-x)**2+(self.y(r['value'])-y)**2)

    def tip_at(self,x,y):
        cell=self.strip_cell(x,y)
        if cell:
            self.panel.show_anomalies(cell,self.data['key']);return ''
        if not (76<=x<=self.width()-20 and 28<=y<=self.y(self.bounds[0])):return ''
        point=self.nearest(x,y)
        if point:
            self.panel.show_interval(point,self.data['key'])
        return ''

    def key(self,key):
        if key in (Qt.Key_Plus,Qt.Key_Equal):self.panel.zoom(.5)
        elif key==Qt.Key_Minus:self.panel.zoom(2)
        elif self.rows:
            if key in (Qt.Key_Left,Qt.Key_Right):
                self.cursor=max(0,min(len(self.rows)-1,self.cursor+(-1 if key==Qt.Key_Left else 1)));self.panel.show_interval(self.rows[self.cursor],self.data['key'])


class TimeNavigator(Node):
    kind='plot'

    def __init__(self,panel):
        super().__init__();self.panel=panel;self.setFixedHeight(56)
        self.put(timeNavigation=True,fullStart=0.,fullEnd=1.,start=0.,end=1.,oldestLabel='',latestLabel='')

    @Slot(float,float)
    @Slot(float,float,bool)
    def commit(self,start,end,anchor_latest=False):
        if anchor_latest:start,end=self.panel.full[1]-(end-start),self.panel.full[1]
        self.panel.set_window(start,end)

    @Slot(int)
    def key(self,key):
        if key in (Qt.Key_Left,Qt.Key_Right):self.panel.pan(-1 if key==Qt.Key_Left else 1)
        elif key in (Qt.Key_Plus,Qt.Key_Equal):self.panel.zoom(.5)
        elif key==Qt.Key_Minus:self.panel.zoom(2)
        elif key==Qt.Key_Home:self.panel.reset()
        elif key==Qt.Key_End:self.panel.move_latest()


class PerformancePanel(Group):
    def __init__(self,dashboard):
        super().__init__();self.dashboard=dashboard;self.range=(0,1);self.full=(0,1);self.time_range=None
        self.follow_latest=True;self.window_span=None;self.period_preset='all';self._follow_revision=object();self.navigation_ready=False
        self.plots={};self.inspection_time=None;self.inspected=None;self.available=[];self.updating=False
        try:saved=json.loads(dashboard.settings.value('performance/selection','{}'))
        except (ValueError,TypeError):saved={}
        self.granularity=saved.get('granularity','day')
        if self.granularity not in ('day','week','month'):self.granularity='day'
        self.reasoning_effort=saved.get('reasoning_effort','high');self.reasoning_specs={};self.reasoning_buttons={}
        self.models=saved.get('models',{});self.modes=saved.get('modes',{'Standard':True,'Fast':True})
        if not any(self.modes.values()):self.modes={'Standard':True,'Fast':True}
        self.resize_timer=QTimer(self);self.resize_timer.setSingleShot(True);self.resize_timer.setInterval(180);self.resize_timer.timeout.connect(self.request)
        self.body=Column(self);self.body.setSpacing(8)
        toolbar=Row();toolbar.put(flow=True,spacing=8);self.body.addLayout(toolbar)
        self.model_choice=Choice();self.model_choice.setFixedWidth(230);self.model_choice.currentIndexChanged.connect(self.choose_model);toolbar.addWidget(self.model_choice)
        self.add_button=button('+ 비교 추가',self.toggle_picker);toolbar.addWidget(self.add_button)
        self.mode_buttons={}
        for title,mode in (('전체 모드','all'),('Standard','Standard'),('Fast','Fast')):
            b=button(title,lambda m=mode:self.choose_mode(m));b.setCheckable(True);b.put(selectionTab=True,flat=True);toolbar.addWidget(b);self.mode_buttons[mode]=b
        self.legend_button=button('범례',self.toggle_legend);self.legend_button.setCheckable(True);self.legend_button.setChecked(True);toolbar.addWidget(self.legend_button)
        self.picker=Group();self.picker.hide();pick=Column(self.picker);self.body.addWidget(self.picker)
        self.search=Input();self.search.setPlaceholderText('비교할 모델 검색');self.search.textChanged.connect(self.search_models);pick.addWidget(self.search)
        self.search_results=Row();self.search_results.put(flow=True,spacing=6);pick.addLayout(self.search_results)
        navigation=Row();navigation.put(flow=True,spacing=6);self.body.addLayout(navigation)
        aggregation=Row();aggregation.setSpacing(6);navigation.addLayout(aggregation)
        label=caption('집계 단위',12);label.put(wrap=False);aggregation.addWidget(label)
        self.granularity_buttons={}
        for title,unit in (('일별','day'),('주별','week'),('월별','month')):
            b=button(title,lambda u=unit:self.choose_granularity(u));b.setCheckable(True);b.put(selectionTab=True,flat=True)
            aggregation.addWidget(b);self.granularity_buttons[unit]=b
        self.granularity_buttons['week'].setToolTip('월요일 시작')
        periods=Row();periods.setSpacing(6);navigation.addLayout(periods)
        label=caption('조회 기간',12);label.put(wrap=False);periods.addWidget(label);self.period_buttons={}
        for title,preset in (('최근 7일','7d'),('최근 30일','30d'),('전체','all')):
            b=button(title,lambda p=preset:self.choose_period(p));b.setCheckable(True);b.put(selectionTab=True,flat=True)
            periods.addWidget(b);self.period_buttons[preset]=b
        self.period=button('',self.toggle_dates);self.period.setAccessibleName('조회 기간 변경');navigation.addWidget(self.period)
        self.date_editor=Group();self.date_editor.hide();self.body.addWidget(self.date_editor)
        dates=Row();dates.put(flow=True,spacing=6);Column(self.date_editor).addLayout(dates)
        dates.addWidget(caption('시작일',12));self.date_start=DateInput();dates.addWidget(self.date_start)
        dates.addWidget(caption('종료일',12));self.date_end=DateInput();dates.addWidget(self.date_end)
        self.date_apply=button('기간 적용',self.apply_dates);dates.addWidget(self.date_apply);dates.addWidget(button('닫기',self.date_editor.hide))
        self.date_error=caption('',12,'warning');dates.addWidget(self.date_error)
        self.body.addWidget(caption('선: 구간별 추이   Y축: 추세선 범위   아래 띠: 개별 이상치',12))
        content=Row();content.setSpacing(20);self.body.addLayout(content,1)
        self.legend_scroll=Scroll();self.legend_scroll.setFixedWidth(240);self.legend_scroll.put(fillViewport=True)
        self.legend_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);content.addWidget(self.legend_scroll)
        legend_panel=Group();legend_panel.setObjectName('panel');legend_body=Column(legend_panel);legend_body.setContentsMargins(12,16,12,16);legend_body.setSpacing(14)
        legend_body.addWidget(caption('범례',14,'ink',True));self.legend=Column();self.legend.setSpacing(16);legend_body.addLayout(self.legend);legend_body.addStretch()
        self.legend_scroll.setWidget(legend_panel)
        center=Column();center.setSpacing(4);content.addLayout(center,1)
        self.chart_scroll=Scroll();self.chart_scroll.put(fillViewport=True);self.chart_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        charts=Group();self.chart_body=Column(charts);self.chart_body.setSpacing(8);self.chart_scroll.setWidget(charts);center.addWidget(self.chart_scroll,1)
        self.timeline_footer=Group();footer=Column(self.timeline_footer);footer.setSpacing(4);center.addWidget(self.timeline_footer)
        separator=Group();separator.put(background='border');separator.setFixedHeight(1);footer.addWidget(separator)
        self.navigator=TimeNavigator(self);footer.addWidget(self.navigator)
        status=Row();status.put(flow=True,spacing=6);footer.addLayout(status)
        self.follow_button=button('최신 따라가기',self.toggle_follow);self.follow_button.setCheckable(True);status.addWidget(self.follow_button)
        self.latest_button=button('최신 이동',self.move_latest);status.addWidget(self.latest_button)
        self.collection_time=caption('',11);self.collection_time.put(wrap=False);status.addWidget(self.collection_time)
        self.inspector=Scroll();self.inspector.setFixedWidth(310);self.inspector.put(fillViewport=True)
        self.inspector.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);content.addWidget(self.inspector)
        self.details=IntervalDetails();self.inspector.setWidget(self.details)
        self.sync_selection()
        self.sync_navigation()

    def is_model_selected(self,model):return not self.models or bool(self.models.get(model,False))
    def request(self):self.dashboard.render()
    def persist(self):self.dashboard.settings.setValue('performance/selection',json.dumps(dict(models=self.models,modes=self.modes,granularity=self.granularity,reasoning_effort=self.reasoning_effort)))
    def toggle_legend(self):self.legend_scroll.setVisible(self.legend_button.isChecked())
    def choose_granularity(self,unit):
        if unit==self.granularity:
            self.granularity_buttons[unit].setChecked(True);return
        self.granularity=unit;self.inspect(None);self.persist();self.sync_selection();self.request()
    def sync_selection(self):
        for unit,b in self.granularity_buttons.items():b.setChecked(unit==self.granularity)
        mode='all' if all(self.modes.get(m,False) for m in ('Standard','Fast')) else 'Fast' if self.modes.get('Fast') else 'Standard'
        for key,b in self.mode_buttons.items():b.setChecked(key==mode)
        clear(self.legend);self.legend_entries=[]
        selected=[m for m in self.available if self.is_model_selected(m)]
        for model in selected:
            item=Column();item.setSpacing(4);self.legend.addLayout(item)
            heading=Row();heading.setSpacing(4);item.addLayout(heading);heading.addWidget(caption(Verbatim(model),13,'ink'),1)
            modes=Row();modes.setSpacing(8);item.addLayout(modes)
            for mode in ('Standard','Fast'):
                active=self.modes.get(mode,False)
                entry=Row();entry.setSpacing(4);modes.addLayout(entry)
                entry.addWidget(SeriesLegend(model,mode,active))
                label=caption(Verbatim(mode),12,'ink' if active else 'muted');label.put(wrap=False);entry.addWidget(label)
                self.legend_entries.append((model,mode))
            # Selected models can be removed without clearing other comparisons.
            if len(selected)>1 and len(selected)!=len(self.available):
                remove=button('×',lambda m=model:self.remove_model(m));remove.put(flat=True);remove.setFixedSize(26,28);remove.setToolTip('비교에서 제거');heading.addWidget(remove)
        for plot in self.plots.values():plot.rebuild_strip()
        if self.available:
            self.updating=True;self.model_choice.clear();self.model_choice.addItem('전체 모델','all')
            if 1<len(selected)<len(self.available):self.model_choice.addItem(tr('모델 비교')+f' ({len(selected)})','compare')
            for m in self.available:self.model_choice.addItem(Verbatim(m),m)
            key='all' if len(selected)==len(self.available) else selected[0] if len(selected)==1 else 'compare'
            self.model_choice.setCurrentIndex(self.model_choice.findData(key));self.updating=False

    def choose_model(self,*_):
        if self.updating:return
        model=self.model_choice.currentData()
        if model=='all':self.models={}
        elif model in self.available:self.models={m:m==model for m in self.available}
        else:return
        self.selection_changed()
    def solo(self,model):self.models={m:m==model for m in self.available};self.selection_changed()
    def remove_model(self,model):
        self.models={m:self.is_model_selected(m) and m!=model for m in self.available};self.selection_changed()
    def choose_mode(self,mode):self.modes={m:mode=='all' or m==mode for m in ('Standard','Fast')};self.selection_changed()
    def highlight(self,key,checked,model):
        (self.models if model else self.modes)[key]=checked;self.selection_changed()
    def selection_changed(self):
        self.persist();self.sync_selection();self.refresh_inspection(self.inspected)
    def toggle_picker(self):
        self.picker.setVisible(not self.picker.isVisible());self.search.setText('');self.search_models()
    def search_models(self,*_):
        clear(self.search_results);query=self.search.text().casefold()
        for model in self.available:
            if query in model.casefold():self.search_results.addWidget(button(Verbatim(model),lambda m=model:self.add_model(m)))
    def add_model(self,model):
        if all(self.is_model_selected(m) for m in self.available):self.models={m:m==model for m in self.available}
        else:self.models[model]=True
        self.picker.hide();self.selection_changed()
    def sync_navigation(self):
        for preset,b in self.period_buttons.items():b.setChecked(preset==self.period_preset);b.setEnabled(self.navigation_ready)
        self.navigator.setEnabled(self.navigation_ready);self.period.setEnabled(self.navigation_ready);self.follow_button.setEnabled(self.navigation_ready)
        self.follow_button.setChecked(self.follow_latest)
        start,end=self.time_range or self.range
        self.latest_button.setEnabled(self.navigation_ready and (not self.follow_latest or end<self.full[1]-.001))
        if not self.navigation_ready:self.period.setText('조회 기간');return
        fmt='%Y.%m.%d %H:%M:%S' if end-start<86400 else '%Y.%m.%d %H:%M'
        label=datetime.fromtimestamp(start).strftime(fmt)+' — '+datetime.fromtimestamp(end).strftime(fmt)
        self.period.setText(Verbatim(label));self.period.setToolTip(Verbatim(label))
        self.navigator.put(fullStart=float(self.full[0]),fullEnd=float(self.full[1]),start=float(start),end=float(end),
            oldestLabel=datetime.fromtimestamp(self.full[0]).strftime('%Y.%m.%d'),latestLabel=datetime.fromtimestamp(self.full[1]).strftime('%Y.%m.%d'))

    def query_range(self,now,revision=None):
        if self.time_range is not None and self.follow_latest and (revision is None or revision!=self._follow_revision):
            self._follow_revision=revision;end=max(now,self.full[1]);span=self.window_span or self.time_range[1]-self.time_range[0]
            self.time_range=[max(self.full[0],end-span),end]
        return list(self.time_range) if self.time_range is not None else None

    def receive_collection_time(self,stamp):
        self.collection_time.setText(Verbatim(tr('마지막 수집')+' '+datetime.fromtimestamp(stamp).strftime('%H:%M:%S')))

    def choose_period(self,preset):
        self.period_preset=preset;self.follow_latest=True;self.date_editor.hide();self._follow_revision=object()
        self.window_span={'7d':7*86400,'30d':30*86400}.get(preset)
        self.time_range=[max(self.full[0],self.full[1]-self.window_span),self.full[1]] if self.window_span else None
        self.inspect(None);self.sync_navigation();self.request()

    def set_window(self,start,end,follow=None):
        if not isfinite(start) or not isfinite(end):return
        start=max(self.full[0],min(self.full[1]-1,start));end=min(self.full[1],max(start+1,end))
        self.time_range=[start,end];self.window_span=end-start;self.period_preset=None
        self.follow_latest=(abs(end-self.full[1])<.001) if follow is None else follow;self._follow_revision=object()
        self.inspect(None);self.sync_navigation();self.request()

    def toggle_follow(self):
        if self.follow_button.isChecked():self.move_latest();return
        self.follow_latest=False
        if self.time_range is None:self.time_range=list(self.range);self.window_span=self.range[1]-self.range[0]
        self.sync_navigation()

    def move_latest(self):
        self.follow_latest=True;self._follow_revision=object()
        if self.time_range is not None:
            span=self.window_span or self.time_range[1]-self.time_range[0]
            self.time_range=[max(self.full[0],self.full[1]-span),self.full[1]]
        self.inspect(None);self.sync_navigation();self.request()

    def toggle_dates(self):
        if self.date_editor.isVisible():self.date_editor.hide();return
        start,end=self.time_range or self.range
        self.date_start.setDate(QDate.fromString(datetime.fromtimestamp(start).strftime('%Y-%m-%d'),Qt.ISODate))
        self.date_end.setDate(QDate.fromString(datetime.fromtimestamp(end-.001).strftime('%Y-%m-%d'),Qt.ISODate))
        self.date_error.setText('');self.date_editor.show()

    def apply_dates(self):
        first,last=self.date_start.date(),self.date_end.date()
        if not first.isValid() or not last.isValid() or first>last:
            self.date_error.setText('종료일: 시작일 이후 날짜');return
        minimum=QDate.fromString(datetime.fromtimestamp(self.full[0]).strftime('%Y-%m-%d'),Qt.ISODate)
        maximum=QDate.fromString(datetime.fromtimestamp(self.full[1]).strftime('%Y-%m-%d'),Qt.ISODate)
        if last<minimum or first>maximum:
            self.date_error.setText('선택 기간 기록 없음');return
        start=self.full[0] if first<=minimum else datetime.combine(first.toPython(),datetime.min.time()).timestamp()
        end=self.full[1] if last>=maximum else datetime.combine(last.addDays(1).toPython(),datetime.min.time()).timestamp()
        self.date_editor.hide();self.set_window(start,end,follow=False)

    def reset(self):self.choose_period('all')
    def zoom(self,factor):
        a,z=self.range;center=self.inspection_time if self.inspection_time is not None else (a+z)/2
        width=min(self.full[1]-self.full[0],max(1,(z-a)*factor));start=max(self.full[0],min(self.full[1]-width,center-width/2))
        self.set_window(start,start+width)
    def pan(self,direction):
        a,z=self.range;width=z-a;start=max(self.full[0],min(self.full[1]-width,a+direction*width*.2));self.set_window(start,start+width)
    def show_anomalies(self,cell,key):
        self.inspect(None);details=self.details;details.hint.hide();spec=self.specs[key]
        details.title.setText(tr(spec['title'])+' / '+tr('개별 이상치'))
        details.value.setText(Verbatim(f"{cell['count']:,}"));details.unit.setText(tr('증가' if cell['direction']>0 else '감소'))
        a,z=self.range;start=a+(z-a)*cell['index']/80;end=a+(z-a)*(cell['index']+1)/80
        details.period.setText(Verbatim(interval_dates(start,end)))
        details.identity.setText(tr('선택한 모델 및 모드 합산'))
        details.sample_heading.hide();details.samples.hide()
        details.formula.setText(tr('과거 표본 10개 이상에서 중앙값 대비 이탈 판정'))
        details.set_totals([('집계 대상',tr('세션' if spec['unit']=='cache_ratio' else '구간' if spec['unit']=='count' else '호출'))])

    def show_interval(self,point,key):
        if self.inspected==(point,key):return
        self.inspected=(point,key);self.inspect(point['ts']);self.details.set_point(point,self.specs[key])

    def refresh_inspection(self,previous=None,only=None):
        keys=[only] if only else list(self.plots)
        if previous and previous[1] in keys:keys.remove(previous[1]);keys.insert(0,previous[1])
        for key in keys:
            plot=self.plots[key];points=[p for p in plot.rows if plot.selected_style(p)]
            if points:
                matching=[p for p in points if previous and p['model']==previous[0]['model'] and p['service_tier']==previous[0]['service_tier']]
                point=min(matching,key=lambda p:abs(p['ts']-previous[0]['ts'])) if matching else points[-1]
                self.show_interval(point,key);return
        self.inspect(None)

    def inspect(self,stamp):
        if stamp is None:self.inspected=None;self.details.clear()
        if stamp==self.inspection_time:return
        self.inspection_time=stamp
        for plot in self.plots.values():plot.update()

    def reasoning_spec(self):
        return dict(self.reasoning_specs[self.reasoning_effort],key='reasoning',title='평균 추론 토큰')

    def choose_reasoning_effort(self,effort):
        if effort not in self.reasoning_specs:return
        if effort==self.reasoning_effort:
            self.reasoning_buttons[effort].setChecked(True);return
        previous=self.inspected;self.reasoning_effort=effort;self.persist()
        self.set_reasoning_data();self.refresh_inspection(previous,only='reasoning')

    def set_reasoning_data(self):
        spec=self.reasoning_spec();self.specs['reasoning']=spec
        self.plots['reasoning'].set_data(spec,self.reasoning_bounds)
        self.notes['reasoning'].setText(Verbatim(tr('유효 호출')+f" {spec['n']:,} / {spec['N']:,}  /  "+tr('공통 Y축')))
        for effort,b in self.reasoning_buttons.items():b.setChecked(effort==self.reasoning_effort)

    def apply(self,data):
        previous=self.inspected
        self.inspect(None)
        self.range=(data['start'],data['end']);self.full=data['full'];self.available=[m for m in data['models'] if m not in ('미확인','codex-auto-review')]
        self.navigation_ready=True
        self.sync_navigation()
        collected=getattr(self.dashboard,'snapshot',{}).get('ts',self.full[1])
        self.receive_collection_time(collected)
        shared_theme().register_models(self.available)
        self.reasoning_specs={p['key'].split(':',1)[1]:p for p in data['panels'] if p['key'].startswith('reasoning:')}
        if self.reasoning_specs and self.reasoning_effort not in self.reasoning_specs:
            self.reasoning_effort='high' if 'high' in self.reasoning_specs else next(iter(self.reasoning_specs))
        # Keep one comparison scale across the effort tabs for the current time range.
        # Per-call outliers stay in the strip and do not define the trend scale.
        self.reasoning_bounds=dynamic_bounds(point['value'] for spec in self.reasoning_specs.values()
            for line in spec['lines'] for point in line['points'])
        display=[]
        for spec in data['panels']:
            if spec['key'].startswith('reasoning:'):
                if not any(p['key']=='reasoning' for p in display):display.append(self.reasoning_spec())
            else:display.append(spec)
        self.specs={p['key']:p for p in display}
        keys=tuple(self.specs)
        if keys!=getattr(self,'panel_keys',None):
            clear(self.chart_body);self.plots={};self.labels={};self.notes={};self.panel_keys=keys
            self.reasoning_tabs=None;self.reasoning_buttons={};self.reasoning_tab_keys=()
            for spec in display:
                group=Group();body=Column(group);body.setSpacing(0)
                head=Row();title=caption('',14,'ink',True);note=caption('',12);head.addWidget(title,1);head.addWidget(note);body.addLayout(head)
                if spec['key']=='reasoning':
                    self.reasoning_tabs=Row();self.reasoning_tabs.put(flow=True,spacing=4);body.addLayout(self.reasoning_tabs)
                plot=PerformancePlot(self);body.addWidget(plot);self.chart_body.addWidget(group)
                self.plots[spec['key']]=plot;self.labels[spec['key']]=title;self.notes[spec['key']]=note
            self.chart_body.addStretch()
        if self.reasoning_specs and tuple(self.reasoning_specs)!=self.reasoning_tab_keys:
            clear(self.reasoning_tabs);self.reasoning_buttons={};self.reasoning_tab_keys=tuple(self.reasoning_specs)
            for effort in self.reasoning_specs:
                b=button(effort,lambda e=effort:self.choose_reasoning_effort(e));b.setCheckable(True);b.put(selectionTab=True,flat=True)
                self.reasoning_tabs.addWidget(b);self.reasoning_buttons[effort]=b
        for spec in display:
            self.labels[spec['key']].setText(tr(spec['title']))
            if spec['key']=='reasoning':
                self.set_reasoning_data();continue
            coverage_label='유효 구간' if spec['key']=='count' else '유효 세션 표본' if spec['key']=='session_cache' else '유효 호출'
            self.notes[spec['key']].setText(Verbatim(tr(coverage_label)+f" {spec['n']:,} / {spec['N']:,}"))
            self.plots[spec['key']].set_data(spec)
        self.sync_selection()
        self.refresh_inspection(previous)
