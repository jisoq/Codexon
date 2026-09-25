"""Selectable analytical charts; each mark retains its actual record population."""
from collections import defaultdict
from math import isfinite
from PySide6.QtCore import Qt, QRectF, QPointF, QRect, Signal
from PySide6.QtGui import QColor, QPen, QPolygonF, QPainter, QFont
from .presentation import Node
from .pricing import usd
from .theme import shared_theme

def dynamic_bounds(values):
    """Fit visible finite data, with padding even for a constant value."""
    values=[v for v in values if v is not None and isfinite(v)]
    if not values:return 0.,1.
    low,high=min(values),max(values)
    padding=max((high-low)*.12,abs(high)*.01,abs(low)*.01,1e-6)
    return low-padding,high+padding


class Plot(Node):
    kind = "plot"
    selected = Signal(object)

    def __init__(self):
        super().__init__()
        self.rows = []
        self.hits = []
        self.cursor = 0
        self.money = False
        self.selected_group = None
        self.setMinimumHeight(160)
        self.setFocusPolicy(Qt.StrongFocus)
        self._paint_width=600;self._paint_height=200;self._focused=False

    def set_rows(self, rows):
        self.rows = rows
        self._scatter_geometry=None
        self.cursor = min(self.cursor, max(0, len(rows)-1))
        self.update()

    def base(self):
        p = self._painter
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.fillRect(self.rect(), QColor(shared_theme().palette['surface']))
        font = QFont(self.font())
        font.setPixelSize(14)
        p.setFont(font)
        self.hits = []
        if not self.rows:
            p.setPen(QColor(shared_theme().palette['muted']))
            p.drawText(self.rect(), Qt.AlignCenter, getattr(self,'empty_text','기록 없음'))
        if self.hasFocus():
            p.setPen(QPen(QColor(shared_theme().palette['accent']), 1, Qt.DotLine))
            p.drawRect(self.rect().adjusted(1, 1, -2, -2))
        return p

    def width(self):return self._paint_width
    def height(self):return self._paint_height
    def rect(self):return QRect(0,0,self.width(),self.height())
    def font(self):return QFont(shared_theme().family)
    def hasFocus(self):return self._focused
    def activate_at(self,x,y):
        for rect,row,tip in reversed(self.hits):
            if rect.contains(QPointF(x,y)):self.selected.emit(row);return
    def tip_at(self,x,y):
        return next((tip for rect,row,tip in reversed(self.hits) if rect.contains(QPointF(x,y))), '')
    def key(self,key):
        if self.rows and key in (Qt.Key_Left,Qt.Key_Up,Qt.Key_Right,Qt.Key_Down):
            self.cursor=(self.cursor+(-1 if key in (Qt.Key_Left,Qt.Key_Up) else 1))%len(self.rows);self.update()
        elif self.rows and key in (Qt.Key_Return,Qt.Key_Space):self.selected.emit(self.rows[self.cursor])


COLORS = ('cached','uncached','written','output','reasoning','completed','unknown','warning')

def value_text(value, metric='cost'):
    if value is None: return '—'
    if metric=='output_speed': return ('<0.1' if 0<value<.1 else f'{value:,.1f}')+' tok/s'
    if metric == 'cost': return usd(value)
    if metric in ('cache_ratio','cache_rate'): return f'{value:.1f}%'
    if metric=='duration': return f'{value:,.2f}초'
    if metric in ('completion_latency_ms','elapsed_ms','request_elapsed_ms'): return f'{value / 1000:,.2f}초'
    return f'{value:,.1f}' if isinstance(value,float) and not value.is_integer() else f'{value:,.0f}'

class AnalyticalPlot(Plot):
    def __init__(self):
        super().__init__(); self.metric='cost'; self.empty_text='조건에 맞는 기록 없음'
        shared_theme().changed.connect(self.update)
    def base(self):
        p=super().base(); palette=shared_theme().palette
        p.fillRect(self.rect(),QColor(palette['surface'])); p.setPen(QColor(palette['ink']))
        if not self.rows: p.drawText(self.rect(),Qt.AlignCenter,self.empty_text)
        if self.hasFocus():
            p.setPen(QPen(QColor(palette['accent']),2)); p.drawRect(self.rect().adjusted(1,1,-2,-2))
        return p
    def color(self,key): return QColor(shared_theme().palette[key])
    def mark(self,p,x,y,index,size=5):
        p.setPen(QPen(QColor(shared_theme().color(COLORS[index%8])),2)); p.setBrush(self.color('surface'))
        if index%3==0: p.drawEllipse(QPointF(x,y),size,size)
        elif index%3==1: p.drawRect(QRectF(x-size,y-size,size*2,size*2))
        else: p.drawPolygon(QPolygonF([QPointF(x,y-size-1),QPointF(x+size+1,y+size),QPointF(x-size-1,y+size)]))
    def key(self,key):
        entries=[row for rect,row,text in self.hits] or self.rows
        if not entries:return
        if key in (Qt.Key_Left,Qt.Key_Up):self.cursor=max(0,self.cursor-1)
        elif key in (Qt.Key_Right,Qt.Key_Down):self.cursor=min(len(entries)-1,self.cursor+1)
        elif key in (Qt.Key_Home,Qt.Key_End):self.cursor=0 if key==Qt.Key_Home else len(entries)-1
        elif key in (Qt.Key_Return,Qt.Key_Enter,Qt.Key_Space):self.selected.emit(entries[min(self.cursor,len(entries)-1)])
        self.update()
    def remember(self,rect,row,text):
        self.hits.append((rect,row,text))
        if len(self.hits)-1==self.cursor:
            self.put(accessible=text)
            if self.hasFocus():
                self._painter.setPen(QPen(self.color('accent'),1,Qt.DotLine));self._painter.setBrush(Qt.NoBrush);self._painter.drawRect(rect.adjusted(1,1,-1,-1))

class UsageTrend(AnalyticalPlot):
    def __init__(self):
        super().__init__(); self.line=False; self.samples=True; self.setFixedHeight(300)
    def paint(self,painter):
        p=self.base()
        if not self.rows:return
        left,right,top=86,self.width()-16,24
        bottom=self.height()-(104 if self.samples else 46)
        low,peak=dynamic_bounds(r.get('value') for r in self.rows)
        for f in (0,.5,1):
            y=bottom-(bottom-top)*f; p.setPen(QPen(self.color('border'),1));p.drawLine(QPointF(left,y),QPointF(right,y))
            p.setPen(self.color('muted'));p.drawText(QRectF(0,y-10,left-10,20),Qt.AlignRight|Qt.AlignVCenter,value_text(low+(peak-low)*f,self.metric))
        step=(right-left)/len(self.rows); previous=None
        maxn=max([r.get('n',0) for r in self.rows]+[1])
        for i,row in enumerate(self.rows):
            x=left+step*(i+.5); value=row.get('value'); n=row.get('n',0)
            if value is not None:
                y=bottom-(bottom-top)*(value-low)/(peak-low)
                if self.line:
                    p.setPen(QPen(self.color('accent'),2))
                    if previous is not None:p.drawLine(previous,QPointF(x,y))
                    p.setBrush(self.color('surface'));p.drawEllipse(QPointF(x,y),3,3);previous=QPointF(x,y)
                elif value==0:
                    p.setPen(QPen(self.color('accent'),2));p.drawEllipse(QPointF(x,bottom),2,2)
                else:p.fillRect(QRectF(x-min(40,step*.65)/2,y,min(40,step*.65),max(1,bottom-y)),self.color('accent'))
            else:
                previous=None;p.setPen(QPen(self.color('unknown'),1));p.setBrush(Qt.NoBrush);p.drawEllipse(QPointF(x,bottom),3,3)
            if row.get('partial'):
                p.setPen(self.color('warning'))
                p.setBrush(Qt.NoBrush);p.drawEllipse(QRectF(x-4,7,8,8));p.setBrush(self.color('warning'));p.drawPie(QRectF(x-4,7,8,8),90*16,180*16)
            if i%max(1,len(self.rows)//10)==0:
                p.setPen(self.color('muted'));p.drawText(QRectF(x-43,bottom+6,86,34),Qt.AlignHCenter|Qt.AlignTop,row.get('label',''))
            if self.samples:
                sample_bottom=self.height()-10
                p.fillRect(QRectF(x-min(32,step*.6)/2,sample_bottom-48*n/maxn,min(32,step*.6),48*n/maxn),self.color('muted'))
            self.remember(QRectF(x-step/2,0,step,self.height()),row,f"{row.get('label','')} · {value_text(value,self.metric)} · 유효 {n:,} / 대상 {row.get('N',row.get('calls',n)):,}")
        if self.samples:
            p.setPen(self.color('muted'));p.drawText(QRectF(0,self.height()-60,left-10,50),Qt.AlignRight|Qt.AlignVCenter,'표본 수\n'+f'{maxn:,}')

class SourceBars(AnalyticalPlot):
    def __init__(self):super().__init__();self.setFixedHeight(300)
    def paint(self,painter):
        p=self.base()
        if not self.rows:return
        left=min(190,self.width()*.33);right=self.width()-170;span=max(20,right-left)
        low,peak=dynamic_bounds(r.get('total') for r in self.rows);step=(self.height()-8)/len(self.rows)
        p.setPen(self.color('muted'));p.drawText(QRectF(left,0,span,18),Qt.AlignCenter,usd(low)+' – '+usd(peak))
        step=(self.height()-28)/len(self.rows)
        for i,row in enumerate(self.rows):
            y=20+i*step;value=row.get('total');p.setPen(self.color('ink'))
            name=p.fontMetrics().elidedText(row.get('label','미확인'),Qt.ElideRight,int(left-12))
            p.drawText(QRectF(0,y,left-12,step),Qt.AlignLeft|Qt.AlignVCenter,name)
            if value is not None:p.fillRect(QRectF(left,y+step/2-5,span*(value-low)/(peak-low),10),QColor(shared_theme().color(row.get('color',COLORS[i%8]))))
            p.setPen(self.color('ink'));share=row.get('share')
            p.drawText(QRectF(right+8,y,162,step/2),Qt.AlignRight|Qt.AlignVCenter,
                       f"{usd(value)} · {share*100:.1f}%" if share is not None else usd(value))
            p.setPen(self.color('muted'));font=p.font();small=font;small.setPixelSize(12);p.setFont(small)
            sample=f"{row.get('known',row.get('n',0)):,} / {row.get('calls',row.get('N',0)):,}호출"
            p.drawText(QRectF(right+8,y+step/2,162,step/2),Qt.AlignRight|Qt.AlignVCenter,sample);font.setPixelSize(14);p.setFont(font)
            self.remember(QRectF(0,y,self.width(),step),row,f"{row.get('label','')} · {usd(value)} · 산정 {row.get('known',0):,} / 대상 {row.get('calls',0):,}")

def comparison_axis(rows, metric, view='distribution'):
    """Shared linear bounds cover every visible mark, excluding hidden outliers."""
    values=[r['value'] for r in rows if r.get('value') is not None]
    if view=='distribution':
        for row in rows:
            n=row.get('distribution_n',row.get('n',0))
            values.extend(row.get('points',[]) if n<10 else
                          [row.get(k) for k in ('p10','q1','median','q3','p90')])
    return dynamic_bounds(values)


class ComparisonChart(AnalyticalPlot):
    def __init__(self):super().__init__();self.view='distribution';self.setFixedHeight(280)
    def paint(self,painter):
        p=self.base()
        if not self.rows:return
        if self.view=='scatter':return self.paint_scatter(p)
        left,right=210,max(310,self.width()-210);span=right-left
        low,peak=comparison_axis(self.rows,self.metric,self.view)
        step=max(48,(self.height()-32)/len(self.rows))
        for i,row in enumerate(self.rows):
            y=step*(i+.5);p.setPen(self.color('ink'));p.drawText(QRectF(0,y-22,left-12,44),Qt.AlignLeft|Qt.AlignVCenter|Qt.TextWordWrap,row.get('id',chr(65+i)))
            color_index=row.get('target_index',i)
            p.setPen(QPen(self.color('border'),1));p.drawLine(QPointF(left,y),QPointF(right,y))
            x=lambda value:left+span*(value-low)/(peak-low)
            distribution_n=row.get('distribution_n',row.get('n',0))
            if self.view=='distribution' and distribution_n:
                if distribution_n<10:
                    for j,v in enumerate(row.get('points',[])):self.mark(p,x(v),y+(j%3-1)*6,color_index,3)
                else:
                    p.setPen(QPen(self.color('muted'),1));p.drawLine(QPointF(x(row['p10']),y),QPointF(x(row['p90']),y))
                    box=QRectF(x(row['q1']),y-8,max(2,x(row['q3'])-x(row['q1'])),16)
                    color=QColor(shared_theme().color(COLORS[color_index%8]));fill=QColor(color);fill.setAlpha(65)
                    p.fillRect(box,fill);p.setPen(QPen(color,1.5));p.setBrush(Qt.NoBrush);p.drawRect(box)
                    p.setPen(QPen(self.color('ink'),2));p.drawLine(QPointF(x(row['median']),y-10),QPointF(x(row['median']),y+10))
            if row.get('value') is not None:self.mark(p,x(row['value']),y,color_index)
            p.setPen(self.color('ink'))
            text=value_text(row.get('value'),self.metric)
            if row.get('suppressed'):text='— · 표본 10개 미만'
            elif row.get('value') is None and self.metric=='duration':text='시간 관측 없음'
            p.drawText(QRectF(right+12,y-27 if self.view=='difference' else y-22,195,22),Qt.AlignVCenter,text)
            p.setPen(self.color('muted'))
            if self.view=='difference':
                delta=row.get('delta');relative=row.get('relative')
                diff=f'{delta:+.1f}%p' if self.metric=='cache_ratio' and delta is not None else value_text(delta,self.metric)
                relative_text=f'{relative:+.1f}%' if relative is not None else '—'
                p.drawText(QRectF(right+12,y-5,195,22),Qt.AlignVCenter,f'기준 대비 {diff} · {relative_text}')
            p.drawText(QRectF(right+12,y+17 if self.view=='difference' else y,195,24),Qt.AlignVCenter,f"유효 {row.get('n',0):,} / 대상 {row.get('N',0):,}")
            self.remember(QRectF(0,y-step/2,self.width(),step),row,f"{row.get('id')} · {text} · 유효 {row.get('n',0):,} / 대상 {row.get('N',0):,}")
        p.setPen(self.color('muted'));p.drawText(QRectF(left,self.height()-24,80,24),Qt.AlignLeft,value_text(low,self.metric))
        p.drawText(QRectF(right-140,self.height()-24,140,24),Qt.AlignRight,value_text(peak,self.metric))
    def paint_scatter(self,p):
        cache=getattr(self,'_scatter_geometry',None)
        size=(self.width(),self.height())
        samples=cache[1] if cache and cache[0]==size else [r for r in self.rows if r.get('x') is not None and r.get('y') is not None]
        if not samples:
            p.setPen(self.color('muted'));p.drawText(self.rect(),Qt.AlignCenter,'환산액·시간 교집합 표본 없음');return
        left,top,w,h=88,28,max(20,self.width()-112),self.height()-80
        xmin,xmax=dynamic_bounds(r['x'] for r in samples);ymin,ymax=dynamic_bounds(r['y'] for r in samples)
        for f in (0,.5,1):
            p.setPen(QPen(self.color('border'),1));p.drawLine(QPointF(left,top+h*(1-f)),QPointF(left+w,top+h*(1-f)))
            p.setPen(self.color('muted'));p.drawText(QRectF(0,top+h*(1-f)-10,left-10,22),Qt.AlignRight,usd(ymin+(ymax-ymin)*f))
            p.drawText(QRectF(left+w*f-45,top+h+8,90,22),Qt.AlignCenter,value_text(xmin+(xmax-xmin)*f,'completion_latency_ms'))
        if cache and cache[0]==size:points,cells=cache[2:]
        else:
            points=[];cells=defaultdict(list)
            for row in samples:
                x=left+w*(row['x']-xmin)/(xmax-xmin);y=top+h*(1-(row['y']-ymin)/(ymax-ymin))
                if len(samples)>2000:cells[(int((x-left)//6),int((y-top)//6))].append(row)
                else:points.append((x,y,row))
            self._scatter_geometry=(size,samples,points,cells)
        for x,y,row in points:
                self.mark(p,x,y,row.get('target_index',0),3)
                self.remember(QRectF(x-5,y-5,10,10),row,f"{row.get('id','')} · {value_text(row['x'],'completion_latency_ms')} · {usd(row['y'])} · 표본 1")
        for (gx,gy),rows in cells.items():
            rect=QRectF(left+gx*6,top+gy*6,6,6);shade=self.color('accent');shade.setAlpha(min(255,80+len(rows)*12));p.fillRect(rect,shade)
            self.remember(rect,{'records':rows,'kind':'scatter_cell'},f'{len(rows):,}개 실제 표본')
        p.setPen(self.color('muted'));p.drawText(QRectF(left,0,w,22),Qt.AlignLeft,'비용')
        p.drawText(QRectF(left,self.height()-24,w,22),Qt.AlignRight,'소요시간')

class TokenComposition(AnalyticalPlot):
    """Two independent denominators; exact values remain readable below each bar."""
    def __init__(self,side):
        super().__init__();self.side=side;self.total=None;self.setFixedHeight(172 if side=='input' else 146)
    def set_parts(self,parts):
        self.total=parts.get('total')
        labels={'ordinary':'일반 입력' if self.side=='input' else '추론 외','read':'캐시 읽기',
                'write':'캐시 쓰기','reasoning':'추론','unclassified':'입력 미분류' if self.side=='input' else '출력 미분류'}
        colors={'ordinary':'uncached' if self.side=='input' else 'output','read':'cached','write':'written','reasoning':'reasoning','unclassified':'unknown'}
        order=('read','write','ordinary','unclassified') if self.side=='input' else ('reasoning','ordinary','unclassified')
        self.set_rows([dict(key=k,label=labels[k],total=parts[k],color=colors[k]) for k in order if k in parts])
    def paint(self,painter):
        p=self.base();p.setPen(self.color('ink'))
        title='입력' if self.side=='input' else '출력'
        p.drawText(QRectF(0,0,self.width(),24),Qt.AlignLeft,title+' · '+('미확인' if self.total is None else f'{self.total:,} 토큰'))
        p.fillRect(QRectF(0,30,self.width(),14),self.color('secondary'));x=0
        for i,row in enumerate(self.rows):
            amount=row['total'];width=self.width()*amount/self.total if self.total else 0
            if width:p.fillRect(QRectF(x,30,width,14),self.color(row['color']));x+=width
            y=52+i*26;p.fillRect(QRectF(0,y+8,8,8),self.color(row['color']));p.setPen(self.color('ink'))
            p.drawText(QRectF(16,y,self.width()*.43,24),Qt.AlignLeft|Qt.AlignVCenter,row['label'])
            percentage=f'{100*amount/self.total:.1f}%' if self.total else '해당 없음' if self.total==0 else '미확인'
            p.drawText(QRectF(self.width()*.4,y,self.width()*.6,24),Qt.AlignRight|Qt.AlignVCenter,f'{amount:,} · {percentage}')
