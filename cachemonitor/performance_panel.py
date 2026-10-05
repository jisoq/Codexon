"""Calendar performance trends with a persistent interval analysis panel."""
import json
from datetime import datetime, timedelta
from bisect import bisect_left, bisect_right
from math import ceil, log1p
from time import mktime
from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import QColor, QPen, QPolygonF, QPainter
from .charts import AnalyticalPlot, Plot, dynamic_bounds, value_text
from .presentation import Group, Column, Row, Button, Text, Choice, Input, Scroll
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
        self.value=caption('—',34,'ink',True);hero.addWidget(self.value)
        self.unit=caption('',13);hero.addWidget(self.unit,1)
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
        middle.addWidget(caption('중앙값',12),1);self.median=caption('',18,'ink',True);middle.addWidget(self.median)
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
        self.setFixedHeight(390);self.bounds=(0,1)
        self.put(clickEnabled=False)
        self.setAccessibleName('성능 추이 / 방향키로 시각 확인')

    def set_data(self,data):
        self.data=data;self.metric=data['unit']
        self.set_rows(sorted((point for line in data['lines'] for point in line['points'] if point['value'] is not None),key=lambda p:p['ts']))
        # Only displayed interval values define the axis. Raw observations remain in the anomaly strip.
        self.times=[p['ts'] for p in self.rows];self.bounds=dynamic_bounds([p['value'] for p in self.rows]);self.rebuild_strip()

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
        if not self.rows:
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


class PerformancePanel(Group):
    def __init__(self,dashboard):
        super().__init__();self.dashboard=dashboard;self.range=(0,1);self.full=(0,1);self.time_range=None
        self.plots={};self.inspection_time=None;self.inspected=None;self.available=[];self.updating=False
        try:saved=json.loads(dashboard.settings.value('performance/selection','{}'))
        except (ValueError,TypeError):saved={}
        self.granularity=saved.get('granularity','day')
        if self.granularity not in ('day','week','month'):self.granularity='day'
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
        self.picker=Group();self.picker.hide();pick=Column(self.picker);self.body.addWidget(self.picker)
        self.search=Input();self.search.setPlaceholderText('비교할 모델 검색');self.search.textChanged.connect(self.search_models);pick.addWidget(self.search)
        self.search_results=Row();self.search_results.put(flow=True,spacing=6);pick.addLayout(self.search_results)
        legend=Row();legend.put(flow=True,spacing=12);self.body.addLayout(legend);self.legend=legend
        navigation=Row();navigation.put(flow=True,spacing=6);self.body.addLayout(navigation)
        self.granularity_buttons={}
        for title,unit in (('일별','day'),('주별','week'),('월별','month')):
            b=button(title,lambda u=unit:self.choose_granularity(u));b.setCheckable(True);b.put(selectionTab=True,flat=True)
            navigation.addWidget(b);self.granularity_buttons[unit]=b
        self.granularity_buttons['week'].setToolTip('월요일 시작')
        self.period=caption('');navigation.addWidget(self.period)
        for title,fn in (('이전 구간',lambda:self.pan(-1)),('확대',lambda:self.zoom(.5)),('축소',lambda:self.zoom(2)),('다음 구간',lambda:self.pan(1)),('전체 기간',self.reset)):
            navigation.addWidget(button(title,fn))
        self.body.addWidget(caption('선: 구간별 추이   Y축: 추세선 범위   아래 띠: 개별 이상치',12))
        content=Row();content.setSpacing(20);self.body.addLayout(content,1)
        self.chart_scroll=Scroll();self.chart_scroll.put(fillViewport=True);self.chart_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        charts=Group();self.chart_body=Column(charts);self.chart_body.setSpacing(8);self.chart_scroll.setWidget(charts);content.addWidget(self.chart_scroll,1)
        self.inspector=Scroll();self.inspector.setFixedWidth(310);self.inspector.put(fillViewport=True)
        self.inspector.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);content.addWidget(self.inspector)
        self.details=IntervalDetails();self.inspector.setWidget(self.details)
        self.sync_selection()

    def is_model_selected(self,model):return not self.models or bool(self.models.get(model,False))
    def request(self):self.dashboard.render()
    def persist(self):self.dashboard.settings.setValue('performance/selection',json.dumps(dict(models=self.models,modes=self.modes,granularity=self.granularity)))
    def choose_granularity(self,unit):
        if unit==self.granularity:return
        self.granularity=unit;self.inspect(None);self.persist();self.sync_selection();self.request()
    def sync_selection(self):
        for unit,b in self.granularity_buttons.items():b.setChecked(unit==self.granularity)
        mode='all' if all(self.modes.get(m,False) for m in ('Standard','Fast')) else 'Fast' if self.modes.get('Fast') else 'Standard'
        for key,b in self.mode_buttons.items():b.setChecked(key==mode)
        clear(self.legend);self.legend_entries=[]
        selected=[m for m in self.available if self.is_model_selected(m)]
        for model in selected:
            item=Row();item.setSpacing(8);self.legend.addLayout(item)
            for mode in ('Standard','Fast'):
                active=self.modes.get(mode,False)
                entry=Row();entry.setSpacing(4);item.addLayout(entry)
                entry.addWidget(SeriesLegend(model,mode,active))
                entry.addWidget(caption(Verbatim(model+' / '+mode),13,'ink' if active else 'muted'))
                self.legend_entries.append((model,mode))
            # Selected models can be removed without clearing other comparisons.
            if len(selected)>1 and len(selected)!=len(self.available):
                remove=button('×',lambda m=model:self.remove_model(m));remove.put(flat=True);remove.setFixedSize(26,28);remove.setToolTip('비교에서 제거');item.addWidget(remove)
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
    def reset(self):self.time_range=None;self.inspect(None);self.request()
    def zoom(self,factor):
        a,z=self.range;center=self.inspection_time if self.inspection_time is not None else (a+z)/2;half=max(1,(z-a)*factor/2)
        center=max(a,min(z,center));self.time_range=[max(self.full[0],center-half),min(self.full[1],center+half)];self.inspect(None);self.request()
    def pan(self,direction):
        a,z=self.range;width=z-a;start=max(self.full[0],min(self.full[1]-width,a+direction*width*.7));self.time_range=[start,min(self.full[1],start+width)];self.inspect(None);self.request()
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

    def refresh_inspection(self,previous=None):
        keys=list(self.plots)
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
    def apply(self,data):
        previous=self.inspected
        self.inspect(None)
        self.range=(data['start'],data['end']);self.full=data['full'];self.available=[m for m in data['models'] if m not in ('미확인','codex-auto-review')]
        self.period.setText(Verbatim(datetime.fromtimestamp(data['start']).strftime('%Y.%m.%d')+' — '+datetime.fromtimestamp(data['end']).strftime('%Y.%m.%d')))
        shared_theme().register_models(self.available);self.specs={p['key']:p for p in data['panels']}
        keys=tuple(self.specs)
        if keys!=getattr(self,'panel_keys',None):
            clear(self.chart_body);self.plots={};self.labels={};self.notes={};self.panel_keys=keys
            for spec in data['panels']:
                group=Group();body=Column(group);body.setSpacing(0)
                head=Row();title=caption('',14,'ink',True);note=caption('',12);head.addWidget(title,1);head.addWidget(note);body.addLayout(head)
                plot=PerformancePlot(self);body.addWidget(plot);self.chart_body.addWidget(group)
                self.plots[spec['key']]=plot;self.labels[spec['key']]=title;self.notes[spec['key']]=note
            self.chart_body.addStretch()
        for spec in data['panels']:
            self.labels[spec['key']].setText(tr(spec['title']))
            coverage_label='유효 구간' if spec['key']=='count' else '유효 세션 표본' if spec['key']=='session_cache' else '유효 호출'
            self.notes[spec['key']].setText(Verbatim(tr(coverage_label)+f" {spec['n']:,} / {spec['N']:,}"))
            self.plots[spec['key']].set_data(spec)
        self.sync_selection()
        self.refresh_inspection(previous)
