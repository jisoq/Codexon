"""Aligned metric rows with bounded rendering and exact time selection."""
from bisect import bisect_left, bisect_right
from math import ceil
from PySide6.QtCore import Qt, QRectF, QPointF, Signal, Slot
from PySide6.QtGui import QColor, QPen, QImage, QPainter, QPainterPath
from .charts import Plot, dynamic_bounds
from .theme import shared_theme
from .pricing import usd
from .quota_view import clock, prepare_series, observation_label
from .quota_share import share_at, share_label, share_color_key
from .token_colors import color_distance, mix
from .i18n import LocalizedPainter, tr, formatted


def quota_bounds(values):
    """Fit quota values without a negative padded lower bound."""
    low,high=dynamic_bounds(values)
    low=max(0.,low)
    return low,max(high,low+1e-6)


class GapPixels:
    """Lazy monotone coordinates: resizing never scans every historical gap."""
    def __init__(self,series,left,scale,width,end=False):
        self.series,self.left,self.scale,self.width,self.end=series,left,scale,width,end
    def __len__(self):return len(self.series['gap_indices'])
    def __getitem__(self,index):
        if index<0:index+=len(self)
        row=self.series['gap_indices'][index]
        return self.left+self.series['active_times'][row]*self.scale+(index+int(self.end))*self.width


def monitoring_palette(paused=False):
    palette=dict(shared_theme().palette)
    if paused:
        for key in ('accent','written','output'):
            palette[key]=shared_theme().readableText(mix(palette[key],palette['muted'],.68),'surface')
    return palette


class QuotaHistory(Plot):
    gap_selected=Signal(str)
    range_selected=Signal(float,float)
    repaintRequested=Signal()
    legendChanged=Signal(object)
    def __init__(self):
        super().__init__()
        self.setFixedHeight(440)
        self.empty_text='수집된 잔여량이 없습니다'
        self.hidden_series=set()
        self.monitoring_paused=False
        self.money=False;self.reference=None;self.series=prepare_series([])
        self.put(quotaDetail=True)
        self._cache_key=None;self.box=QRectF()
        self.strip_box=QRectF();self.inspection_x=None;self.inspection_at=None
        self.row_boxes={};self.row_headers={};self.exclusion_selection=None;self._selection_anchor=None
        self.gap_lefts=[];self.gap_rights=[];self.gap_width=24
        self.setAccessibleName('잔여량과 구독 가치 / 방향키로 시각 선택')
        shared_theme().changed.connect(self.update)

    def set_series_visible(self,key,visible):
        if visible:self.hidden_series.discard(key)
        else:self.hidden_series.add(key)
        self._cache_key=None
        self.update()

    def set_monitoring_paused(self,paused):
        if self.monitoring_paused==bool(paused):return
        self.monitoring_paused=bool(paused)
        self._cache_key=None;self.update()

    def visible(self,key):
        if key=='reference':return self.visible('cycle_value') and key not in self.hidden_series
        return key not in self.hidden_series and (key=='remaining' or self.money)

    def set_series(self,series):
        if self.series is series:return
        previous_digest=self.series.get('_content_digest');digest=series.get('_content_digest')
        if (self._cache_key is not None and len(self.rows)==len(series['rows'])
                and self.rows[-1:]==series['rows'][-1:] and
                (previous_digest==digest if previous_digest is not None and digest is not None else self.series==series)):
            # Adopt the new input without retaining another copy of its history.
            # Worker digests avoid comparing the full history on the GUI thread.
            # Raw callers retain the existing exact-content comparison.
            self.series=series;self.rows=series['rows']
            self.legendChanged.emit(self.legend_values())
            self._cache_key=(self._cache_key[0],id(series),*self._cache_key[2:])
            self.gap_lefts.series=series;self.gap_rights.series=series
            return
        self.series=series;self.rows=series['rows'];self.cursor=min(self.cursor,max(0,len(self.rows)-1))
        share=series.get('model_share')
        self.setFixedHeight((630 if self.money else 300)+(100 if share else 0))
        shared_theme().register_models([name for name,mode in share['models']] if share else [])
        self._cache_key=None;self._selection_anchor=None;self.exclusion_selection=None
        self.legendChanged.emit(self.legend_values());self.update()

    def set_rows(self,rows):self.set_series(prepare_series(rows))

    def connects(self,a,b,key):
        missing=self.series['missing'].get(key)
        return (self.series['breaks'][a]==self.series['breaks'][b] and
                (key=='remaining' and not self.series.get('cumulative') or
                 ((missing is None or missing[a]==missing[b]) and not self.excluded_between(a,b))))

    def excluded_between(self,a,b):
        start,end=self.rows[a]['at'],self.rows[b]['at']
        return any(item['start']<end and item['end']>start for item in self.series.get('exclusions',[]))

    def point(self,index,key):
        value=self.value_at(index,key)
        if value is None:return None
        low,high=self.bounds(key)
        box=self.row_boxes.get(key,self.box)
        return QPointF(self.x_at(index),box.bottom()-box.height()*(value-low)/(high-low))

    def bounds(self,key):
        if key=='remaining':return self.axis[:2]
        if key=='cycle_cost':return self.cost_floor,self.cost_ceiling
        if key=='completed_cost':return self.completed_floor,self.completed_ceiling
        return self.value_floor,self.ceiling

    def value_at(self,index,key):
        return self.series['completed_costs'][index] if key=='completed_cost' else self.rows[index].get(key)

    def metric_definitions(self):
        return [('cycle_value','accent','주간 예상 가치','USD / 100%p'),
                ('remaining','written','누적 사용량' if self.series.get('cumulative') else '남은 사용량',
                 '%p' if self.series.get('cumulative') else '%'),
                ('cycle_cost','output','누적 측정 환산액','USD')]

    def displayed_metrics(self):
        return [item for item in self.metric_definitions() if self.visible(item[0]) or
                (item[0]=='cycle_value' and self.reference is not None and self.visible('reference'))]

    def curves(self):
        curves=[(key,color,Qt.SolidLine) for key,color,_,_ in self.metric_definitions() if self.visible(key)]
        palette=shared_theme().palette
        if any(color_distance(palette[a[1]],palette[b[1]])<.04 for i,a in enumerate(curves) for b in curves[i+1:]):
            return [(key,color,style) for (key,color,_),style in zip(curves,(Qt.SolidLine,Qt.DashLine,Qt.DashDotLine))]
        return curves

    def x_at(self,index):
        return min(self.box.right(),self.box.left()+self.series['active_times'][index]*self.time_scale+
                   bisect_right(self.series['gap_indices'],index)*self.gap_width)

    def gap_at(self,x,y):
        if not self.box.contains(QPointF(x,y)):return None
        i=bisect_left(self.gap_rights,x)
        return i if i<len(self.gap_rights) and self.gap_lefts[i]+1e-7<x<self.gap_rights[i]-1e-7 else None

    def gap_label(self,index):
        right=self.series['gap_indices'][index];times=self.series['times']
        seconds=max(0,round(times[right]-times[right-1]))
        hours,rest=divmod(seconds,3600);minutes,seconds=divmod(rest,60)
        duration=(f'{hours}시간 {minutes}분' if hours else f'{minutes}분 {seconds}초' if minutes else f'{seconds}초')
        return f'수집 공백 {duration}, {clock(times[right-1],True)} → {clock(times[right],True)}'

    def static_image(self):
        from .quota_panel import remaining_axis
        palette=monitoring_palette(self.monitoring_paused)
        ratio=self._painter.device().devicePixelRatioF()
        pic=QImage(round(self.width()*ratio),round(self.height()*ratio),QImage.Format_ARGB32_Premultiplied)
        pic.setDevicePixelRatio(ratio);pic.fill(Qt.transparent);p=LocalizedPainter(QPainter(pic))
        p.setFont(self.font());font=p.font();font.setPixelSize(13);p.setFont(font)
        p.setRenderHint(QPainter.Antialiasing)
        cumulative=self.series.get('cumulative',False)
        if cumulative:
            low,high=quota_bounds((self.series['low'],self.series['high']))
            self.axis=(low,high,(high-low)/4)
        else:self.axis=remaining_axis([{'remaining':self.series['low']},{'remaining':self.series['high']}])
        value_low=self.series['value_minimum'] if self.visible('cycle_value') else None
        value_high=self.series['value_maximum'] if self.visible('cycle_value') else float('-inf')
        if self.reference is not None and self.visible('reference'):
            value_low=self.reference if value_low is None else min(value_low,self.reference)
            value_high=max(value_high,self.reference)
        self.value_floor,self.ceiling=quota_bounds((value_low,value_high) if value_low is not None else ())
        self.cost_floor,self.cost_ceiling=quota_bounds((self.series['cost_minimum'],self.series['cost_maximum']) if self.series['cost_minimum'] is not None else ())
        self.completed_floor,self.completed_ceiling=quota_bounds((self.series['completed_minimum'],self.series['completed_maximum']) if self.series['completed_minimum'] is not None else ())
        metrics=self.displayed_metrics()
        labels=[]
        for key,_,_,_ in metrics:
            low,high=self.bounds(key)
            labels.extend(self.metric_text(key,value) for value in (low,high))
        left=max(66,max((p.fontMetrics().horizontalAdvance(label) for label in labels),default=50)+14)
        extra=100 if self.series.get('model_share') else 0
        content_bottom=self.height()-38-extra
        lane_height=(content_bottom-30)/max(1,len(metrics))
        self.row_boxes={};self.row_headers={}
        for row,(key,color,title,unit) in enumerate(metrics):
            top=30+row*lane_height
            self.row_headers[key]=QRectF(8,top,self.width()-24,26)
            header_space=10
            self.row_boxes[key]=QRectF(left,top+header_space,max(1,self.width()-left-16),max(1,lane_height-header_space-13))
        boxes=list(self.row_boxes.values())
        self.box=box=QRectF(left,boxes[0].top() if boxes else 66,max(1,self.width()-left-16),
                           boxes[-1].bottom()-boxes[0].top() if boxes else max(1,content_bottom-66))
        self.strip_box=QRectF(box.left(),box.bottom()+65,box.width(),34) if extra else QRectF()
        gap_indices=self.series['gap_indices'];count=len(gap_indices)
        self.gap_width=0 if self.series.get('active_only') else min(24,box.width()*.4/count) if count else 24
        self.time_scale=(box.width()-self.gap_width*count)/max(1,self.series['active_times'][-1])
        self.gap_lefts=GapPixels(self.series,box.left(),self.time_scale,self.gap_width)
        self.gap_rights=GapPixels(self.series,box.left(),self.time_scale,self.gap_width,True)
        visible_gaps=range(0,count,max(1,ceil(1/self.gap_width))) if self.gap_width else range(0)
        self.reset_hits=[]
        if cumulative:
            for reset in self.series.get('resets',[]):
                index=min(bisect_left(self.series['times'],reset['at']),len(self.rows)-1)
                self.reset_hits.append((QRectF(self.x_at(index)-8,4,16,22),reset))
        for key,color,title,unit in metrics:
            area=self.row_boxes[key];header=self.row_headers[key]
            low,high=self.bounds(key)
            for fraction in (0,.5,1):
                y=area.bottom()-area.height()*fraction
                grid=QColor(palette['border']);grid.setAlpha(115)
                p.setPen(QPen(grid,1));p.drawLine(QPointF(area.left(),y),QPointF(area.right(),y))
                p.setPen(QColor(palette['muted']))
                p.drawText(QRectF(0,y-9,area.left()-9,18),Qt.AlignRight|Qt.AlignVCenter,
                           self.metric_text(key,low+(high-low)*fraction))
            for item in self.series.get('exclusions',[]):
                x=self.x_at_time(item['start']);right=self.x_at_time(item['end'])
                shade=QColor(palette['muted']);shade.setAlpha(16)
                excluded=QRectF(x,area.top(),max(0,right-x),area.height())
                p.fillRect(excluded,shade)
                p.save();p.setClipRect(excluded)
                hatch=QColor(palette['muted']);hatch.setAlpha(28);p.setPen(QPen(hatch,1))
                for offset in range(int(x-area.height()),int(right),12):
                    p.drawLine(QPointF(offset,area.bottom()),QPointF(offset+area.height(),area.top()))
                p.restore()
            if key=='cycle_value' and self.reference is not None and self.visible('reference'):
                y=area.bottom()-area.height()*(self.reference-low)/(high-low)
                p.setPen(QPen(QColor(palette['accent']),1,Qt.DashLine))
                p.drawLine(QPointF(area.left(),y),QPointF(area.right(),y))
        if not metrics:
            p.setPen(QColor(palette['muted']));p.drawText(box,Qt.AlignCenter,'표시할 선을 범례에서 선택하세요')
        indices=self.series['samples'][256 if box.width()<600 else 768]
        active=self.series['active_times'];gaps=self.series['gap_indices']
        left,right=box.left(),box.right();time_scale=self.time_scale;gap_width=self.gap_width
        xs=[min(right,left+active[i]*time_scale+bisect_right(gaps,i)*gap_width) for i in indices]
        rows=self.rows;breaks=self.series['breaks'];completed=self.series['completed_costs']
        for key,color,style in self.curves():
            area=self.row_boxes[key];low,high=self.bounds(key)
            scale=area.height()/(high-low);bottom=area.bottom()
            previous=None;previous_x=None;previous_index=None
            path=QPainterPath();move=path.moveTo;line=path.lineTo
            missing=self.series['missing'].get(key)
            p.setPen(QPen(QColor(palette[color]),2,style))
            for index,x in zip(indices,xs):
                value=completed[index] if key=='completed_cost' else rows[index].get(key)
                y=bottom-(value-low)*scale if value is not None else None
                if y is not None and previous is not None and self.connects(previous_index,index,key):
                    # Completed intervals remain horizontal, with no invented
                    # slope or vertical transition between independent amounts.
                    if previous_x!=x:
                        start,end=previous_x,x
                        if key=='completed_cost':
                            if not previous_index or rows[previous_index].get('remaining')!=rows[previous_index-1].get('remaining'):start+=1.5
                            if rows[index].get('remaining')!=rows[previous_index].get('remaining'):end-=1.5
                        if end>start:move(start,previous);line(end,previous)
                    if key!='completed_cost' and previous!=y:move(x,previous);line(x,y)
                previous,previous_x,previous_index=y,x,index
                if key=='completed_cost' and y is not None and index+1<len(rows):
                    if (completed[index+1] is None and breaks[index]==breaks[index+1]
                            and missing[index+1]==missing[index]+1 and not self.excluded_between(index,index+1)):
                        start=x+1.5 if not index or rows[index].get('remaining')!=rows[index-1].get('remaining') else x
                        end=self.x_at(index+1)-1.5
                        if end>start:move(start,y);line(end,y)
            p.setBrush(Qt.NoBrush);p.drawPath(path)
        if count and self.gap_width:
            p.setPen(QColor(palette['muted']))
            p.drawText(QRectF(box.left(),4,box.width(),22),Qt.AlignLeft,'// 수집 공백')
            for area in boxes:
                for k in visible_gaps:
                    left=self.gap_lefts[k];strip=QRectF(left,area.top(),self.gap_width,area.height())
                    p.fillRect(strip,QColor(palette['surface']));p.save();p.setClipRect(strip)
                    p.setPen(QPen(QColor(palette['border']),1))
                    for y in range(int(area.top()-self.gap_width),int(area.bottom()+self.gap_width),9):
                        p.drawLine(QPointF(left,y+self.gap_width),QPointF(left+self.gap_width,y))
                    p.restore()
        self._cache_hits=[(QRectF(box),{},'')]
        for pos,index in enumerate(indices):
            if rows[index]['reset_kind']:
                x=xs[pos];p.setPen(QPen(QColor(palette['muted']),1,Qt.DotLine))
                for area in boxes:p.drawLine(QPointF(x,area.top()),QPointF(x,area.bottom()))
        for rect,reset in self.reset_hits:
            x=rect.center().x();p.setPen(QPen(QColor(palette['muted']),1,Qt.DashLine))
            for area in boxes:p.drawLine(QPointF(x,area.top()),QPointF(x,area.bottom()))
            p.setPen(QColor(palette['ink']));p.drawText(rect,Qt.AlignCenter,'◆')
            self._cache_hits.append((rect,{},reset['label']+', '+clock(reset['at'],True)))
        p.setPen(QColor(palette['muted']))
        p.drawText(QRectF(box.left(),box.bottom()+10,box.width()/2,20),Qt.AlignLeft,clock(rows[0]['at']))
        p.drawText(QRectF(box.center().x(),box.bottom()+10,box.width()/2,20),Qt.AlignRight,clock(rows[-1]['at']))
        self.draw_model_share(p,palette)
        p.end();return pic

    def metric_text(self,key,value):
        if value is None:return '—'
        if key=='remaining':return f'{value:g}'+('%p' if self.series.get('cumulative') else '%')
        return usd(value)

    def x_at_time(self,at):
        times=self.series['times'];index=bisect_left(times,at)
        if index==0:return self.x_at(0)
        if index>=len(times):return self.x_at(len(times)-1)
        lo,hi=times[index-1:index+1]
        return self.x_at(index-1)+(self.x_at(index)-self.x_at(index-1))*(at-lo)/(hi-lo)

    def time_at_x(self,x):
        gaps=bisect_right(self.gap_rights,x+1e-7)
        return self.series['times'][0]+(x-self.box.left()-gaps*self.gap_width)/max(self.time_scale,1e-12)+self.series['gap_seconds'][gaps]

    def draw_model_share(self,p,palette):
        from PySide6.QtGui import QBrush
        share=self.series.get('model_share')
        if not share:return
        area=self.strip_box
        p.setPen(QColor(palette['muted']))
        p.drawText(QRectF(area.left(),area.top()-25,area.width(),22),Qt.AlignLeft,
                   p.fontMetrics().elidedText(tr('모델 및 모드별 점유율 / 5분'),Qt.ElideRight,int(area.width())))
        p.drawText(QRectF(0,area.top(),area.left()-8,20),Qt.AlignRight,'100%')
        p.drawText(QRectF(0,area.bottom()-20,area.left()-8,20),Qt.AlignRight,'0%')
        p.fillRect(area,QColor(palette['track']))
        p.fillRect(area,QBrush(QColor(palette['muted']),Qt.BDiagPattern))
        p.save();p.setClipRect(area);p.setRenderHint(QPainter.Antialiasing,False)
        # Resolve each model's style once per image, not once per time span.
        theme=shared_theme()
        styles=[]
        for name in share['models']:
            color=QColor(theme.color(share_color_key(name)));pattern=theme.model_pattern(name[0])
            styles.append((name,color,QBrush(QColor(palette['surface']),pattern) if pattern else None))
        last_end=None;last_right=None
        for start,end,bucket in share['spans']:
            item=share['bins'][bucket]
            x=last_right if start==last_end else self.x_at_time(start)
            right=self.x_at_time(end);last_end,last_right=end,right
            width=right-x
            if width<=0:continue
            rect=QRectF(x,area.top(),width,area.height())
            p.fillRect(rect,QColor(palette['track']))
            if item['state']=='unknown':
                p.fillRect(rect,QBrush(QColor(palette['muted']),Qt.BDiagPattern));continue
            if item['state']=='zero':continue
            y=area.bottom()
            for name,color,pattern in styles:
                height=area.height()*item['shares'].get(name,0)/100
                if height<=0:continue
                y-=height;p.fillRect(QRectF(x,y,width,height),color)
                if pattern:p.fillRect(QRectF(x,y,width,height),pattern)
        p.restore()

    def set_inspection(self,detail,x,y):
        self.inspection_x=x if detail else None
        self.inspection_at=detail.get('inspection_at',detail.get('at')) if detail else None
        self.legendChanged.emit(self.legend_values())
        self.repaintRequested.emit()

    def set_exclusion_selection(self,start=None,end=None,enabled=False):
        self.exclusion_selection=(float(start),float(end)) if enabled and start is not None and end is not None else None
        if not enabled:self._selection_anchor=None
        self.put(exclusionSelecting=bool(enabled))
        self.repaintRequested.emit()

    def nearest_index(self,x):
        """Snap to an actual record, including either side of a compressed gap."""
        x=max(self.box.left(),min(self.box.right(),x))
        gap=self.gap_at(x,self.box.center().y())
        if gap is not None:
            right=self.series['gap_indices'][gap]
            return right-1 if x-self.x_at(right-1)<=self.x_at(right)-x else right
        at=self.time_at_x(x);times=self.series['times'];right=bisect_left(times,at)
        if right==0:return 0
        if right>=len(times):return len(times)-1
        return right-1 if at-times[right-1]<=times[right]-at else right

    @Slot(float,float)
    def selectionPress(self,x,y):
        if not self.state.get('exclusionSelecting') or not self.rows or not self.box.contains(QPointF(x,y)):return
        self._selection_anchor=self.nearest_index(x)
        self.selectionMove(x,y)

    @Slot(float,float)
    def selectionMove(self,x,y):
        if self._selection_anchor is None:return
        a,b=sorted((self._selection_anchor,self.nearest_index(x)))
        start,end=self.rows[a]['at'],self.rows[b]['at']
        if self.exclusion_selection==(start,end):return
        self.exclusion_selection=(start,end)
        self.repaintRequested.emit()

    @Slot(float,float)
    def selectionRelease(self,x,y):
        if self._selection_anchor is None:return
        self.selectionMove(x,y);self._selection_anchor=None
        # Recalculate only after the gesture settles, never for every pixel.
        self.range_selected.emit(*self.exclusion_selection)

    @Slot()
    def selectionCancel(self):
        self._selection_anchor=None

    def paint(self,painter):
        p=self.base();palette=shared_theme().palette;p.fillRect(self.rect(),QColor(palette['surface']))
        if not self.rows:
            p.setPen(QColor(palette['muted']));p.drawText(self.rect(),Qt.AlignCenter,self.empty_text);return
        key=(frozenset(self.hidden_series),id(self.series),self.width(),self.height(),self.money,self.reference,tuple(palette.items()),shared_theme().family,self._painter.device().devicePixelRatioF())
        if key!=self._cache_key:self._picture=self.static_image();self._cache_key=key
        p.drawImage(0,0,self._picture);self.hits=self._cache_hits

    def index_at(self,x,y):
        if not self.rows or not (self.box.contains(QPointF(x,y)) or self.strip_box.contains(QPointF(x,y))):return None
        if self.gap_at(x,self.box.center().y()) is not None:return None
        gaps=bisect_right(self.gap_rights,x+1e-7)
        times=self.series['times']
        at=times[0]+(x-self.box.left()-gaps*self.gap_width)/self.time_scale+self.series['gap_seconds'][gaps]
        return max(0,min(bisect_right(times,at+1e-6)-1,len(times)-1))

    def tip_at(self,x,y):
        if not self.geometry_ready():return ''
        for rect,reset in getattr(self,'reset_hits',[]):
            if rect.contains(QPointF(x,y)):return reset['label']+', '+clock(reset['at'],True)
        gap=self.gap_at(x,y)
        if gap is not None:return self.gap_label(gap)
        index=self.index_at(x,y)
        return ', '.join([self.detail_for(index)['title']]+[item['label']+' '+item['value'] for item in self.detail_for(index)['items']]) if index is not None and self.hidden_series else observation_label(self.rows[index],self.money) if index is not None else ''

    def detail_at(self,x,y):
        if not self.geometry_ready():return {}
        if self.strip_box.contains(QPointF(x,y)):y=self.box.center().y()
        for rect,reset in getattr(self,'reset_hits',[]):
            if rect.contains(QPointF(x,y)):
                return dict(title=reset['label'],at=reset['at'],reset_at=reset['at'],items=[
                    dict(label='시각',value=clock(reset['at'],True),color='ink'),
                    dict(label='주기',value=str(reset['number']),color='ink')],note='')
        gap=self.gap_at(x,y)
        if gap is not None:
            index=self.series['gap_indices'][gap]
            return dict(title=self.gap_label(gap).split(', ')[0],at=self.rows[index-1]['at'],gap_end=self.rows[index]['at'],items=[
                dict(label='마지막 관측',value=clock(self.rows[index-1]['at'],True),color='ink'),
                dict(label='관측 재개',value=clock(self.rows[index]['at'],True),color='ink')],
                note='')
        index=self.index_at(x,y)
        if index is None:return {}
        detail=self.detail_for(index)
        return self.with_share(detail,self.time_at_x(x))

    def with_share(self,detail,at):
        share=self.series.get('model_share')
        if not share:return detail
        item=share_at(self.series,at)
        details=[];note='미확인'
        if item:
            note={'unknown':'미확인, 모델 비용과 차트 증가분의 대응을 확인할 수 없습니다',
                  'zero':'확인된 비용 증가 없음','cost':''}[item['state']]
            details=[dict(label=share_label(name),value=f"{item['shares'].get(name,0):.1f}%",model=name[0],
                          service_tier=name[1],swatch=share_color_key(name),color='ink') for name in share['models']] if item['state']=='cost' else []
        return {**detail,'inspection_at':at,'chart_top':self.box.top(),'chart_bottom':self.box.bottom(),
                'strip_top':self.strip_box.top(),'strip_bottom':self.strip_box.bottom(),
                'share':dict(title=(clock(item['start'],True)+' → '+clock(item['end'],True)) if item else '모델 및 모드별 점유율 / 5분',
                             items=details,note=note)}

    def detail_for(self,index):
        row=self.rows[index];previous=self.rows[index-1] if index else None
        cumulative=self.series.get('cumulative',False)
        items=[dict(metric=key,label=title,value=self.metric_text(key,self.value_at(index,key)),color=color)
               for key,color,title,_ in self.metric_definitions() if self.visible(key)]
        if row.get('user_excluded'):
            note='집계에서 제외한 구간입니다'
            for item in items:
                if item['metric']!='remaining' or cumulative:item['value']=tr('집계 제외')
        elif row['reset_kind']:note=(', '.join(row.get('markers',[])) or '사용량 리셋')+', 새 주기'
        elif not previous:note=''
        elif not row['connect']:note='' if self.series.get('active_only') else '공백 이후 첫 관측'
        elif self.visible('cycle_cost') and row.get('cycle_cost') is None:note='금액 확인 중'
        elif self.visible('cycle_value') and row.get('cycle_value') is None:note='동등 가치 계산 대기'
        else:note=''
        if self.visible('cycle_value') and self.reference is not None:
            items.insert(1,dict(metric='reference',label='누적 예상 가치' if cumulative else '현 주기 평균',value=usd(self.reference),color='muted',subordinate=True))
        return self.with_share(dict(title=clock(row['at'],True),at=row['at'],items=items,note=note),row['at'])

    def refresh_detail(self,detail):
        if not detail or not self.rows:return {}
        if 'reset_at' in detail:
            return detail if any(r['at']==detail['reset_at'] for r in self.series.get('resets',[])) else {}
        at=detail.get('gap_end',detail.get('at'))
        index=bisect_left(self.series['times'],at)
        if index>=len(self.rows) or self.rows[index]['at']!=at:return {}
        if 'gap_end' not in detail:return self.with_share(self.detail_for(index),detail.get('inspection_at',at))
        gap=bisect_left(self.series['gap_indices'],index)
        if gap>=len(self.gap_lefts) or self.series['gap_indices'][gap]!=index:return {}
        return self.detail_at((self.gap_lefts[gap]+self.gap_rights[gap])/2,self.box.center().y())

    def activate_at(self,x,y):
        if not self.geometry_ready():return None
        gap=self.gap_at(x,y)
        if gap is not None:
            label=self.gap_label(gap);self.setAccessibleName(label);self.gap_selected.emit(label);return
        index=self.index_at(x,y)
        if index is not None:
            self.cursor=index;self.repaintRequested.emit();self.selected.emit(self.rows[index])
            self.setAccessibleName(observation_label(self.rows[index],self.money))

    def key(self,key):
        if not self.rows:return
        if key==Qt.Key_Home:self.cursor=0
        elif key==Qt.Key_End:self.cursor=len(self.rows)-1
        elif key in (Qt.Key_Left,Qt.Key_Up,Qt.Key_Right,Qt.Key_Down):
            self.cursor=(self.cursor+(-1 if key in (Qt.Key_Left,Qt.Key_Up) else 1))%len(self.rows)
        elif key in (Qt.Key_Return,Qt.Key_Space):self.selected.emit(self.rows[self.cursor])
        self.repaintRequested.emit();self.setAccessibleName(observation_label(self.rows[self.cursor],self.money))
        if key in (Qt.Key_Home,Qt.Key_End,Qt.Key_Left,Qt.Key_Right,Qt.Key_Up,Qt.Key_Down):self.selected.emit(self.rows[self.cursor])

    def paint_overlay(self,painter):
        if not self.rows or self._cache_key is None:return
        p=self._painter;palette=shared_theme().palette
        p.setRenderHint(QPainter.Antialiasing);p.setRenderHint(QPainter.TextAntialiasing)
        font=self.font();font.setPixelSize(14);p.setFont(font)
        if self.exclusion_selection is not None:
            start,end=self.exclusion_selection;x=self.x_at_time(start);right=self.x_at_time(end)
            shade=QColor(palette['accent']);shade.setAlpha(28)
            for area in self.row_boxes.values():
                p.fillRect(QRectF(x,area.top(),max(0,right-x),area.height()),shade)
        inspected=self.inspection_x is not None
        index=max(0,min(bisect_right(self.series['times'],self.inspection_at)-1,len(self.rows)-1)) if inspected and self.inspection_at is not None else len(self.rows)-1
        font=p.font();font.setPixelSize(13);p.setFont(font)
        p.setPen(QColor(palette['muted']))
        stamp=(formatted('마지막 관측 {time}',time=clock(self.rows[index]['at']))
               if self.monitoring_paused and not inspected else clock(self.rows[index]['at'],True))
        p.drawText(QRectF(self.box.center().x(),4,self.box.width()/2,22),Qt.AlignRight,stamp)
        if inspected:
            x=max(self.box.left(),min(self.box.right(),self.x_at_time(self.inspection_at) if self.inspection_at is not None else self.inspection_x))
            p.setPen(QPen(QColor(palette['muted']),1,Qt.DotLine))
            for area in self.row_boxes.values():p.drawLine(QPointF(x,area.top()),QPointF(x,area.bottom()))
            if not self.strip_box.isNull():p.drawLine(QPointF(x,self.strip_box.top()),QPointF(x,self.strip_box.bottom()))
        for key,color,_,_ in self.displayed_metrics():
            excluded=self.rows[index].get('user_excluded') and (key!='remaining' or self.series.get('cumulative'))
            value='집계 제외' if excluded else self.metric_text(key,self.value_at(index,key))
            if inspected and not excluded:
                point=self.point(index,key)
                if point is not None:
                    point.setX(x);p.setPen(QPen(QColor(palette['surface']),1.5));p.setBrush(QColor(palette[color]));p.drawEllipse(point,3.5,3.5)
        p.setBrush(Qt.NoBrush)

    def legend_values(self):
        if not self.rows:return {key:'—' for key,_,_,_ in self.metric_definitions()}
        cumulative=self.series.get('cumulative',False)
        inspected=cumulative and self.inspection_x is not None and self.inspection_at is not None
        index=max(0,min(bisect_right(self.series['times'],self.inspection_at)-1,len(self.rows)-1)) if inspected else len(self.rows)-1
        return {key:tr('집계 제외') if self.rows[index].get('user_excluded') and (key!='remaining' or cumulative) and (key!='cycle_value' or cumulative)
                else self.metric_text(key,self.reference if key=='cycle_value' and not cumulative else self.value_at(index,key)) for key,_,_,_ in self.metric_definitions()}

    def geometry_ready(self):
        return self._cache_key is not None and self._cache_key[1]==id(self.series)

    @Slot(float,float)
    def beginRangeSelection(self,x,y):
        if not self.geometry_ready():return
        self.put(exclusionSelecting=True)
        self.selectionPress(x,y)

    @Slot()
    def cancelRangeSelection(self):
        self.selectionCancel();self.set_exclusion_selection(None,None,False)
