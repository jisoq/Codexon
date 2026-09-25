"""Four matching series/axes with bounded rendering and exact time selection."""
from bisect import bisect_left, bisect_right
from math import ceil, floor, log10
from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QColor, QPen, QImage, QPainter, QPainterPath
from .charts import Plot, dynamic_bounds
from .theme import shared_theme
from .pricing import usd
from .quota_view import clock, prepare_series, observation_label
from .quota_share import share_at
from .token_colors import color_distance
from .i18n import LocalizedPainter, tr


class GapPixels:
    """Lazy monotone coordinates: resizing never scans every historical gap."""
    def __init__(self,series,left,scale,width,end=False):
        self.series,self.left,self.scale,self.width,self.end=series,left,scale,width,end
    def __len__(self):return len(self.series['gap_indices'])
    def __getitem__(self,index):
        if index<0:index+=len(self)
        row=self.series['gap_indices'][index]
        return self.left+self.series['active_times'][row]*self.scale+(index+int(self.end))*self.width


class QuotaHistory(Plot):
    gap_selected=Signal(str)
    def __init__(self):
        super().__init__()
        self.setFixedHeight(340)
        self.empty_text='수집된 잔여량이 없습니다'
        self.money=False;self.reference=None;self.series=prepare_series([])
        self.put(quotaDetail=True)
        self._cache_key=None;self.box=QRectF()
        self.strip_box=QRectF();self.inspection_x=None
        self.gap_lefts=[];self.gap_rights=[];self.gap_width=24
        self.setAccessibleName('잔여량과 API 동등 가치 · 방향키로 시각 선택')
        shared_theme().changed.connect(self.update)

    def set_series(self,series):
        if self.series is series:return
        self.series=series;self.rows=series['rows'];self.cursor=min(self.cursor,max(0,len(self.rows)-1))
        share=series.get('model_share')
        self.setFixedHeight(440 if share else 340)
        shared_theme().register_models(share['models'] if share else [])
        self._cache_key=None;self.update()

    def set_rows(self,rows):self.set_series(prepare_series(rows))

    def connects(self,a,b,key):
        return (self.series['breaks'][a]==self.series['breaks'][b] and
                (key=='remaining' or self.series['missing'][key][a]==self.series['missing'][key][b]))

    def point(self,index,key):
        value=self.value_at(index,key)
        if value is None:return None
        low,high=self.bounds(key)
        return QPointF(self.x_at(index),
                       self.box.bottom()-self.box.height()*(value-low)/(high-low))

    def bounds(self,key):
        if key=='remaining':return self.axis[:2]
        if key=='cycle_cost':return self.cost_floor,self.cost_ceiling
        if key=='completed_cost':return self.completed_floor,self.completed_ceiling
        return self.value_floor,self.ceiling

    def value_at(self,index,key):
        return self.series['completed_costs'][index] if key=='completed_cost' else self.rows[index].get(key)

    def curves(self):
        curves=[('remaining','cached',Qt.SolidLine)]+([
            ('cycle_cost','output',Qt.SolidLine),('cycle_value','written',Qt.DashDotLine),
            ('completed_cost','completed',Qt.SolidLine)] if self.money else [])
        palette=shared_theme().palette
        if any(color_distance(palette[a[1]],palette[b[1]])<.04 for i,a in enumerate(curves) for b in curves[i+1:]):
            return [(key,color,style) for (key,color,_),style in zip(curves,(Qt.SolidLine,Qt.DashLine,Qt.DashDotLine,Qt.DotLine))]
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
        return f'수집 공백 {duration} · {clock(times[right-1],True)} → {clock(times[right],True)}'

    def static_image(self):
        from .quota_panel import remaining_axis
        palette=shared_theme().palette
        ratio=self._painter.device().devicePixelRatioF()
        pic=QImage(round(self.width()*ratio),round(self.height()*ratio),QImage.Format_ARGB32_Premultiplied)
        pic.setDevicePixelRatio(ratio);pic.fill(Qt.transparent);p=LocalizedPainter(QPainter(pic))
        p.setFont(self.font());font=p.font();font.setPixelSize(13);p.setFont(font)
        p.setRenderHint(QPainter.Antialiasing)
        cumulative=self.series.get('cumulative',False)
        if cumulative:
            low,high=dynamic_bounds((self.series['low'],self.series['high']))
            self.axis=(low,high,(high-low)/4)
        else:self.axis=remaining_axis([{'remaining':self.series['low']},{'remaining':self.series['high']}])
        value_low=self.series['value_minimum']
        value_high=self.series['value_maximum']
        if self.reference is not None:
            value_low=self.reference if value_low is None else min(value_low,self.reference)
            value_high=max(value_high,self.reference)
        self.value_floor,self.ceiling=dynamic_bounds((value_low,value_high) if value_low is not None else ())
        self.cost_floor,self.cost_ceiling=dynamic_bounds((self.series['cost_minimum'],self.series['cost_maximum']) if self.series['cost_minimum'] is not None else ())
        self.completed_floor,self.completed_ceiling=dynamic_bounds((self.series['completed_minimum'],self.series['completed_maximum']) if self.series['completed_minimum'] is not None else ())
        completed_width=max(82,p.fontMetrics().horizontalAdvance(usd(self.completed_ceiling))+14)
        remaining_title='누적 소모\n%p' if cumulative else '잔여량\n%'
        remaining_labels=[f'{self.axis[1]:g}'+('%p' if cumulative else '%'),*tr(remaining_title).splitlines()]
        remaining_width=max(60,max(p.fontMetrics().horizontalAdvance(label) for label in remaining_labels)+14)
        cost_width=max(82,p.fontMetrics().horizontalAdvance(usd(self.cost_ceiling))+14)
        value_width=max(92,p.fontMetrics().horizontalAdvance(usd(self.ceiling))+14)
        right=cost_width+value_width if self.money else 18
        left=remaining_width+(completed_width if self.money else 0)
        extra=100 if self.series.get('model_share') else 0
        self.box=box=QRectF(left,58,max(1,self.width()-left-right),self.height()-98-extra)
        self.strip_box=QRectF(box.left(),box.bottom()+65,box.width(),34) if extra else QRectF()
        gap_indices=self.series['gap_indices'];count=len(gap_indices)
        # Keep all gaps equally narrow; cap their total footprint only when
        # an exceptionally dense set cannot fit at the normal 24 px width.
        self.gap_width=0 if self.series.get('active_only') else min(24,box.width()*.4/count) if count else 24
        self.time_scale=(box.width()-self.gap_width*count)/max(1,self.series['active_times'][-1])
        self.gap_lefts=GapPixels(self.series,box.left(),self.time_scale,self.gap_width)
        self.gap_rights=GapPixels(self.series,box.left(),self.time_scale,self.gap_width,True)
        visible_gaps=range(0,count,max(1,ceil(1/self.gap_width))) if self.gap_width else range(0)
        self.reset_hits=[]
        if cumulative:
            boundaries=[]
            for reset in self.series.get('resets',[]):
                index=min(bisect_left(self.series['times'],reset['at']),len(self.rows)-1)
                boundaries.append((self.x_at(index),reset))
            edges=[box.left()]+[x for x,_ in boundaries]+[box.right()]
            for i,(left,right_edge) in enumerate(zip(edges,edges[1:])):
                if i%2:
                    color=QColor(palette['secondary']);color.setAlpha(110)
                    p.fillRect(QRectF(left,box.top(),max(0,right_edge-left),box.height()),color)
            for x,reset in boundaries:
                self.reset_hits.append((QRectF(x-8,7,16,24),reset))
        low,high,step=self.axis
        ticks=[low+step*i for i in range(5)]
        for value in ticks:
            y=box.bottom()-box.height()*(value-low)/(high-low)
            p.setPen(QPen(QColor(palette['border']),1));p.drawLine(QPointF(box.left(),y),QPointF(box.right(),y))
            p.setPen(QColor(palette['cached']));p.drawText(QRectF(box.left()-remaining_width,y-9,remaining_width-7,18),Qt.AlignRight|Qt.AlignVCenter,f'{value:g}'+('%p' if cumulative else '%'))
        p.setPen(QPen(QColor(palette['cached']),1))
        p.drawLine(QPointF(box.left(),box.top()),QPointF(box.left(),box.bottom()))
        p.drawText(QRectF(box.left()-remaining_width,0,remaining_width-7,54),Qt.AlignRight|Qt.AlignVCenter|Qt.TextWordWrap,
                   remaining_title)
        if self.money:
            for x,width,axis_low,ceiling,color,title,on_left in (
                    (box.left()-remaining_width,completed_width,self.completed_floor,self.completed_ceiling,'completed','완료 구간\nUSD',True),
                    (box.right(),cost_width,self.cost_floor,self.cost_ceiling,'output','누적 API\nUSD',False),
                    (box.right()+cost_width,value_width,self.value_floor,self.ceiling,'written','주간 동등\nUSD / 100%p',False)):
                text_x=x-width if on_left else x+7
                alignment=Qt.AlignRight if on_left else Qt.AlignLeft
                p.setPen(QPen(QColor(palette[color]),1));p.drawLine(QPointF(x,box.top()),QPointF(x,box.bottom()))
                p.drawText(QRectF(text_x,0,width-7,54),alignment|Qt.AlignVCenter|Qt.TextWordWrap,title)
                for fraction in (0,.5,1):
                    y=box.bottom()-box.height()*fraction
                    p.drawText(QRectF(text_x,y-9,width-7,18),alignment|Qt.AlignVCenter,usd(axis_low+(ceiling-axis_low)*fraction))
            if self.reference is not None:
                y=box.bottom()-box.height()*(self.reference-self.value_floor)/(self.ceiling-self.value_floor)
                p.setPen(QPen(QColor(palette['accent']),1,Qt.DashLine))
                p.drawLine(QPointF(box.left(),y),QPointF(box.right(),y))
        indices=self.series['samples'][256 if box.width()<600 else 768]
        xs=[self.x_at(index) for index in indices]
        for key,color,style in self.curves():
            low,high=self.bounds(key)
            scale=box.height()/(high-low);bottom=box.bottom()
            previous=None;previous_x=None;previous_index=None
            path=QPainterPath()
            p.setPen(QPen(QColor(palette[color]),2,style))
            # One native path avoids thousands of temporary Qt line/point
            # wrappers each time a refreshed series invalidates the image.
            for index,x in zip(indices,xs):
                value=self.value_at(index,key)
                y=bottom-(value-low)*scale if value is not None else None
                if y is not None:
                    if previous is not None and self.connects(previous_index,index,key):
                        path.moveTo(previous_x,previous);path.lineTo(x,previous)
                        path.moveTo(x,previous);path.lineTo(x,y)
                previous,previous_x,previous_index=y,x,index
                # A completed amount belongs to its whole plateau, up to the
                # next boundary, even when that next plateau is still pending.
                if key=='completed_cost' and y is not None and index+1<len(self.rows):
                    if (self.value_at(index+1,key) is None and self.series['breaks'][index]==self.series['breaks'][index+1]
                            and self.series['missing'][key][index+1]==self.series['missing'][key][index]+1):
                        path.moveTo(x,y);path.lineTo(self.x_at(index+1),y)
            p.setBrush(Qt.NoBrush);p.drawPath(path)
        if count and self.gap_width:
            p.setPen(QColor(palette['muted']))
            p.drawText(QRectF(box.left(),8,box.width(),20),Qt.AlignLeft,'// 수집 공백')
            for k in visible_gaps:
                left=self.gap_lefts[k]
                strip=QRectF(left,box.top(),self.gap_width,box.height())
                p.fillRect(strip,QColor(palette['surface']))
                p.save();p.setClipRect(strip)
                p.setPen(QPen(QColor(palette['border']),1))
                for y in range(int(box.top()-self.gap_width),int(box.bottom()+self.gap_width),9):
                    p.drawLine(QPointF(left,y+self.gap_width),QPointF(left+self.gap_width,y))
                p.restore()
                if self.gap_width>=12:
                    p.setPen(QColor(palette['muted']))
                    p.drawText(QRectF(left-2,box.bottom()-10,self.gap_width+4,20),Qt.AlignCenter,'//')
        self._cache_hits=[]
        for pos,index in enumerate(indices):
            x=xs[pos];row=self.rows[index]
            left=(xs[pos-1]+x)/2 if pos else box.left()
            right=(xs[pos+1]+x)/2 if pos+1<len(indices) else box.right()
            self._cache_hits.append((QRectF(left,box.top(),max(1,right-left),box.height()),row,observation_label(row,self.money)))
            if row['reset_kind']:
                p.setPen(QPen(QColor(palette['muted']),1,Qt.DotLine));p.drawLine(QPointF(x,box.top()),QPointF(x,box.bottom()))
        for k in visible_gaps:
            left=self.gap_lefts[k]
            self._cache_hits.append((QRectF(left,box.top(),self.gap_width,box.height()),{},self.gap_label(k)))
        for rect,reset in self.reset_hits:
            x=rect.center().x()
            p.setPen(QPen(QColor(palette['muted']),1,Qt.DashLine));p.drawLine(QPointF(x,box.top()),QPointF(x,box.bottom()))
            p.setPen(QColor(palette['ink']));p.drawText(rect,Qt.AlignCenter,'◆')
            self._cache_hits.append((rect,{},reset['label']+' · '+clock(reset['at'],True)))
        p.setPen(QColor(palette['muted']))
        p.drawText(QRectF(box.left(),box.bottom()+10,box.width()/2,20),Qt.AlignLeft,clock(self.rows[0]['at']))
        p.drawText(QRectF(box.center().x(),box.bottom()+10,box.width()/2,20),Qt.AlignRight,clock(self.rows[-1]['at']))
        self.draw_model_share(p,palette)
        p.end();return pic

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
                   p.fontMetrics().elidedText(tr('모델별 점유율 · 5분'),Qt.ElideRight,int(area.width())))
        p.drawText(QRectF(0,area.top(),area.left()-8,20),Qt.AlignRight,'100%')
        p.drawText(QRectF(0,area.bottom()-20,area.left()-8,20),Qt.AlignRight,'0%')
        p.fillRect(area,QColor(palette['track']))
        p.fillRect(area,QBrush(QColor(palette['muted']),Qt.BDiagPattern))
        p.save();p.setClipRect(area);p.setRenderHint(QPainter.Antialiasing,False)
        for start,end,bucket in share['spans']:
            item=share['bins'][bucket];x=self.x_at_time(start);right=self.x_at_time(end)
            width=right-x
            if width<=0:continue
            rect=QRectF(x,area.top(),width,area.height())
            p.fillRect(rect,QColor(palette['track']))
            if item['state']=='unknown':
                p.fillRect(rect,QBrush(QColor(palette['muted']),Qt.BDiagPattern));continue
            if item['state']=='zero':continue
            y=area.bottom()
            for name in share['models']:
                height=area.height()*item['shares'].get(name,0)/100
                if height<=0:continue
                y-=height;p.fillRect(QRectF(x,y,width,height),QColor(shared_theme().model_color(name)))
                pattern=shared_theme().model_pattern(name)
                if pattern:p.fillRect(QRectF(x,y,width,height),QBrush(QColor(palette['surface']),pattern))
        p.restore()

    def set_inspection(self,detail,x,y):
        self.inspection_x=x if detail else None
        self.inspection_at=detail.get('inspection_at',detail.get('at')) if detail else None
        self.update()

    def paint(self,painter):
        p=self.base();palette=shared_theme().palette;p.fillRect(self.rect(),QColor(palette['surface']))
        if not self.rows:
            p.setPen(QColor(palette['muted']));p.drawText(self.rect(),Qt.AlignCenter,self.empty_text);return
        key=(id(self.series),self.width(),self.height(),self.money,self.reference,tuple(palette.items()),shared_theme().family,self._painter.device().devicePixelRatioF())
        if key!=self._cache_key:self._picture=self.static_image();self._cache_key=key
        p.drawImage(0,0,self._picture);self.hits=self._cache_hits
        if self.inspection_x is None:return
        x=max(self.box.left(),min(self.box.right(),self.x_at_time(self.inspection_at) if self.inspection_at is not None else self.inspection_x))
        p.setPen(QPen(QColor(palette['muted']),1,Qt.DotLine))
        p.drawLine(QPointF(x,self.box.top()),QPointF(x,self.strip_box.bottom() if not self.strip_box.isNull() else self.box.bottom()))

    def index_at(self,x,y):
        if not self.rows or not (self.box.contains(QPointF(x,y)) or self.strip_box.contains(QPointF(x,y))):return None
        if self.gap_at(x,self.box.center().y()) is not None:return None
        gaps=bisect_right(self.gap_rights,x+1e-7)
        times=self.series['times']
        at=times[0]+(x-self.box.left()-gaps*self.gap_width)/self.time_scale+self.series['gap_seconds'][gaps]
        return max(0,min(bisect_right(times,at+1e-6)-1,len(times)-1))

    def tip_at(self,x,y):
        for rect,reset in getattr(self,'reset_hits',[]):
            if rect.contains(QPointF(x,y)):return reset['label']+' · '+clock(reset['at'],True)
        gap=self.gap_at(x,y)
        if gap is not None:return self.gap_label(gap)
        index=self.index_at(x,y)
        return observation_label(self.rows[index],self.money) if index is not None else ''

    def detail_at(self,x,y):
        if self.strip_box.contains(QPointF(x,y)):y=self.box.center().y()
        for rect,reset in getattr(self,'reset_hits',[]):
            if rect.contains(QPointF(x,y)):
                return dict(title=reset['label'],at=reset['at'],reset_at=reset['at'],items=[
                    dict(label='시각',value=clock(reset['at'],True),color='ink'),
                    dict(label='주기',value=str(reset['number']),color='ink')],note='')
        gap=self.gap_at(x,y)
        if gap is not None:
            index=self.series['gap_indices'][gap]
            return dict(title=self.gap_label(gap).split(' · ')[0],at=self.rows[index-1]['at'],gap_end=self.rows[index]['at'],items=[
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
            note={'unknown':'미확인 · 모델 비용과 차트 증가분의 대응을 확인할 수 없습니다',
                  'zero':'확인된 비용 증가 없음','cost':''}[item['state']]
            details=[dict(label=name,value=f"{item['shares'].get(name,0):.1f}%",model=name,
                          color='ink') for name in share['models']] if item['state']=='cost' else []
        return {**detail,'inspection_at':at,'chart_top':self.box.top(),'chart_bottom':self.box.bottom(),
                'strip_top':self.strip_box.top(),'strip_bottom':self.strip_box.bottom(),
                'share':dict(title=(clock(item['start'],True)+' → '+clock(item['end'],True)) if item else '모델별 점유율 · 5분',
                             items=details,note=note)}

    def detail_for(self,index):
        row=self.rows[index];previous=self.rows[index-1] if index else None
        cumulative=self.series.get('cumulative',False)
        items=[dict(label='누적 소모량' if cumulative else '잔여량',value=f"{row['remaining']:g}"+('%p' if cumulative else '%'),color='cached')]
        if self.money:
            items += [dict(label='누적 API 환산액',value=usd(row.get('cycle_cost')),color='output'),
                      dict(label='주간 동등 가치',value=usd(row.get('cycle_value')),color='written')]
            amount=self.series['completed_costs'][index]
            items.append(dict(label='완료 구간별 API 환산액',value=usd(amount),color='completed'))
        if row['reset_kind']:note=(' · '.join(row.get('markers',[])) or '사용량 리셋')+' · 새 주기'
        elif not previous:note=''
        elif not row['connect']:note='' if self.series.get('active_only') else '공백 이후 첫 관측'
        elif self.money and row.get('cycle_cost') is None:note='금액 확인 중'
        elif self.money and row.get('cycle_value') is None:note='동등 가치 계산 대기'
        else:note=''
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
        gap=self.gap_at(x,y)
        if gap is not None:
            label=self.gap_label(gap);self.setAccessibleName(label);self.gap_selected.emit(label);return
        index=self.index_at(x,y)
        if index is not None:
            self.cursor=index;self.update();self.selected.emit(self.rows[index])
            self.setAccessibleName(observation_label(self.rows[index],self.money))

    def key(self,key):
        if not self.rows:return
        if key==Qt.Key_Home:self.cursor=0
        elif key==Qt.Key_End:self.cursor=len(self.rows)-1
        else:super().key(key)
        self.update();self.setAccessibleName(observation_label(self.rows[self.cursor],self.money))
        if key in (Qt.Key_Home,Qt.Key_End,Qt.Key_Left,Qt.Key_Right,Qt.Key_Up,Qt.Key_Down):self.selected.emit(self.rows[self.cursor])
