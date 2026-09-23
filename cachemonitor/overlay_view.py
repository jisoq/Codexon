"""Fixed logical-pixel monitor and identifier-based call inspection."""
import math
import re
from datetime import datetime
from PySide6.QtCore import Qt, QRectF, QPointF, QObject, Property, Slot, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QBrush
from .presentation import Node
from .quick_runtime import QuickHost
from .overlay_appearance import default_appearance
from .overlay_navigation import navigation_target
from .ui_details import recorded, observed_transport, model_comparison
from .token_colors import TOKEN_COLORS, token_palette, readable
from .i18n import tr

LABELS = dict(cached='캐시 읽기', uncached='일반 입력', written='캐시 쓰기', output='출력·추론 제외', reasoning='추론', unknown='미분류')
ORDER = tuple(LABELS)

def money(value, compact=True):
    if value is None or not math.isfinite(value): return '—'
    if value == 0: return '$0.00'
    if 0 < value < .0001: return '<$0.0001'
    if compact and abs(value) >= 10000:
        divisor, suffix = (1e6, 'M') if abs(value) >= 999500 else (1e3, 'K')
        return f'${value/divisor:.2f}{suffix}'
    if abs(value) >= 1: return f'${value:,.2f}'
    return '$'+f'{value:.4f}'.rstrip('0').rstrip('.')

def fitted_money(value,width,size,family):
    result=money(value);metrics=QFontMetrics(font(family,size,600))
    if metrics.horizontalAdvance(result)<=width or value is None or abs(value)<1000:return result
    for divisor,suffix in ((1e3,'K'),(1e6,'M')):
        if abs(value)<divisor:continue
        for places in (2,1,0):
            result=f'${value/divisor:.{places}f}{suffix}'
            if metrics.horizontalAdvance(result)<=width:return result
    return f'${value/1e6:.0e}M'

def amount(value, exact=False):
    if value is None: return '—'
    if exact: return f'{int(value):,}'
    for divisor, suffix in ((1e6, 'M'), (1e3, 'K')):
        if abs(value) >= divisor: return f'{value/divisor:.1f}{suffix}'
    return str(int(value))

def call_count(data,family):
    values=[data.get('priced'),data.get('calls')] if data.get('missing') else [data.get('calls')]
    for places,separator in ((1,' / '),(1,'/'),(0,'/')):
        parts=[]
        for value in values:
            if value is None:parts.append('—');continue
            if value>=999500:divisor,suffix=1e6,'M'
            elif value>=1000:divisor,suffix=1e3,'K'
            else:parts.append(str(value));continue
            parts.append(f'{value/divisor:.{places}f}'.removesuffix('.0')+suffix)
        result=separator.join(parts)
        if metric_width(result,family,18)<=76:return result
    return result

def metric_width(value,family,size):
    return sum(QFontMetrics(font(family,max(11,round(size*2/3)) if part in ('K','M','$','%') else size,600)).horizontalAdvance(part)
               for part in re.findall(r'[KM$%]|[^KM$%]+',value))

def percent(value):
    if value is None: return '—'
    if 0 < value < .1: return '<0.1%'
    return f'{value:.1f}%'

def timestamp(value):
    if value is None: return '—'
    try: return datetime.fromtimestamp(value).strftime('%m/%d %H:%M:%S')
    except (ValueError, OverflowError, OSError, TypeError): return '—'


def collection_error_label(error):
    """Expose observed collection states, never diagnostic paths or stacks."""
    raw=str(error);lower=raw.casefold()
    if any(marker in lower for marker in ('database disk image is malformed','file is not a database','sqlite_corrupt','sqlite_notadb')):
        return '기록 손상'
    if any(marker in lower for marker in ('permissionerror','permission denied','access is denied','winerror 5','액세스가 거부','접근이 거부')):
        return '기록 접근 실패'
    brief=raw.strip()
    if len(brief)<=40 and re.fullmatch(r'(?:기록|수집|관측)[가-힣0-9 ·()]*?(?:오류|실패|지연|누락|손상|거부|없음|중)',brief):
        return brief
    return '기록 읽기 실패'

def font(family, size, weight=400):
    result=QFont(family);result.setPixelSize(size);result.setWeight(QFont.Weight(weight))
    result.setFeature(QFont.Tag.fromString('tnum'),1);return result

def mix(a,b,fraction):
    a,b=QColor(a),QColor(b)
    return QColor(*(round(x*(1-fraction)+y*fraction) for x,y in zip(a.getRgb()[:3],b.getRgb()[:3])))

def contrast(a,b):
    def luminance(c):
        rgb=[x/255 for x in QColor(c).getRgb()[:3]]
        linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in rgb]
        return sum(v*w for v,w in zip(linear,(.2126,.7152,.0722)))
    lo,hi=sorted((luminance(a),luminance(b)));return (hi+.05)/(lo+.05)

def palette(appearance):
    dark=appearance.dark
    p=dict(surface=appearance.panel_surface or appearance.surface,ink=appearance.ink,
           band='#252C27' if dark else '#EDF1EE',secondary='#B6C2BA' if dark else '#53615A',
           meta='#95A59B' if dark else '#647069',border='#455048' if dark else '#CDD5D0',
           track='#343D37' if dark else '#E2E7E4',warning='#EDB563' if dark else '#9B5E17',
           error='#F08B85' if dark else '#AF443E',**token_palette(appearance))
    if p['surface'].lower()!=default_appearance(dark).surface.lower():
        p.update(band=mix(p['surface'],p['ink'],.06),secondary=mix(p['ink'],p['surface'],.24),
                 meta=mix(p['ink'],p['surface'],.34),border=mix(p['surface'],p['ink'],.23),track=mix(p['surface'],p['ink'],.12))
    target=max(('#FFFFFF','#000000'),key=lambda value:contrast(value,p['surface']))
    p['cached_text']=readable(p['cached'], (p['surface'],))
    for key in ('ink','secondary','meta','warning','error'):
        for _ in range(64):
            if contrast(p[key],p['surface'])>=5:break
            p[key]=mix(p[key],target,.08)
        if contrast(p[key],p['surface'])<4.5:p[key]=target
    return {key:QColor(value) for key,value in p.items()}

class Drawing:
    def __init__(self,painter,content):
        self.p=painter;self.model=content;self.colors=palette(content.appearance);painter.setRenderHint(QPainter.Antialiasing)
    def text(self,value,x,y,w,h=16,size=11,color='ink',weight=400,right=False,elide=False):
        value=tr(str(value))
        p=self.p;p.setFont(font(self.model.appearance.family,size,weight));p.setPen(self.colors.get(color,color))
        if elide:value=QFontMetrics(p.font()).elidedText(str(value),Qt.ElideRight,round(w))
        p.drawText(QRectF(x,y,w,h),(Qt.AlignRight if right else Qt.AlignLeft)|Qt.AlignVCenter,str(value))
    def number(self,value,x,baseline,w,size,color='ink',weight=600,right=False):
        value=tr(str(value))
        p=self.p;p.setFont(font(self.model.appearance.family,size,weight));p.setPen(self.colors[color])
        metrics=QFontMetrics(p.font());start=x+w-metrics.horizontalAdvance(value) if right else x
        p.drawText(QPointF(start,baseline),value)
    def metric(self,value,x,y,w,h=24,size=18,baseline=None,color='ink'):
        p=self.p;family=self.model.appearance.family
        metrics=QFontMetrics(font(family,size,600));start=x+w-metric_width(value,family,size)
        if baseline is None:baseline=y+(h-metrics.height())/2+metrics.ascent()
        for part in re.findall(r'[KM$%]|[^KM$%]+',value):
            s=max(11,round(size*2/3)) if part in ('K','M','$','%') else size
            self.number(part,start,baseline,w,s,color);start+=QFontMetrics(font(family,s,600)).horizontalAdvance(part)
    def rect(self,x,y,w,h,color,radius=0,alpha=None,pattern=False):
        if w<=0 or h<=0:return
        p=self.p;c=QColor(self.colors.get(color,color))
        if alpha is not None:c.setAlphaF(alpha)
        p.setPen(Qt.NoPen);p.setBrush(c)
        if radius:p.drawRoundedRect(QRectF(x,y,w,h),radius,radius)
        else:p.drawRect(QRectF(x,y,w,h))
        if pattern:
            hatch=QColor(self.colors['surface']);hatch.setAlpha(145);p.setBrush(QBrush(hatch,Qt.BDiagPattern));p.drawRect(QRectF(x,y,w,h))
    def line(self,x,y,x2,y2,color='border',width=1,dashed=False):
        pen=QPen(self.colors.get(color,color),width)
        if dashed:pen.setDashPattern([1,1])
        self.p.setPen(pen);self.p.drawLine(QPointF(x,y),QPointF(x2,y2))
    def dot(self,x,y,color='ink',r=1.5):
        self.p.setPen(Qt.NoPen);self.p.setBrush(self.colors[color]);self.p.drawEllipse(QPointF(x,y),r,r)
    def graphs(self,rows,x,y,width,capacity,detail=False):
        m=self.model;left=36;plot=width-left;step=plot/capacity
        cache_y=y+20;cache_h=64 if detail else 32
        cost_y=cache_y+cache_h+(28 if detail else 8);cost_h=24 if detail else 20;bottom=cost_y+cost_h
        self.text(f'최근 {capacity}호출',x,y,width)
        self.text('캐시',x,cache_y+cache_h-16,28,color='meta');self.text('비용',x,cost_y+cost_h-16,28,color='meta')
        peak=max((r['cost'] for r in rows if r.get('cost') is not None),default=None)
        if detail:
            self.text('100%',x,cache_y-2,32,color='meta');self.text(money(peak),x+left,cost_y-20,plot,color='meta',right=True)
        else:self.text(money(peak),x+left,y,plot,color='meta',right=True)
        self.line(x+left,cache_y+cache_h,x+width,cache_y+cache_h,'track');self.line(x+left,bottom,x+width,bottom,'track')
        selected=m.selected_id
        for index,row in enumerate(rows[-capacity:]):
            cx=x+left+(capacity-len(rows[-capacity:])+index+.5)*step;bar=min(16,step*.55)
            rate=row.get('cache_rate',row.get('rate'))
            if detail and m.call_id(row)==selected:
                self.rect(cx-step/2,cache_y,step,bottom-cache_y,'accent',alpha=.07);self.line(cx,cache_y,cx,bottom,'accent',.8)
            elif detail and m.call_id(row)==m.hover_id:
                self.rect(cx-step/2,cache_y,step,bottom-cache_y,'accent',alpha=.04)
            warning=bool(row.get('cache_warning') or row.get('cache_miss'));color='warning' if warning else 'cached'
            if rate is None:self.line(cx-bar/2,cache_y+cache_h/2,cx+bar/2,cache_y+cache_h/2,'meta',1,True)
            elif rate==0:self.line(cx-bar/2,cache_y+cache_h,cx+bar/2,cache_y+cache_h,color,1.5)
            else:
                height=cache_h*max(0,min(100,rate))/100;self.rect(cx-bar/2,cache_y+cache_h-height,bar,height,color)
            if warning:self.line(cx,cache_y+3,cx,cache_y+7,'warning',1.2);self.dot(cx,cache_y+10,'warning',.8)
            if index==len(rows[-capacity:])-1:self.dot(cx,cache_y-3,color,1.3)
            if m.call_id(row)==m.highlight_id:
                self.line(cx-bar/2-1,cache_y,cx-bar/2-1,cache_y+cache_h,color,1)
            cost=row.get('cost')
            if cost is None:self.line(cx-bar/2,cost_y+cost_h/2,cx+bar/2,cost_y+cost_h/2,'meta',1,True)
            elif peak is not None and peak>0:
                cy=bottom-cost_h*cost/peak;self.line(cx,bottom,cx,cy,'secondary',1);self.dot(cx,cy,'secondary',1.3)
    def tokens(self,x,y,width=348,title='토큰 구성'):
        c=(self.model.data or {}).get('token_composition',{})
        self.text(title,x,y,width*.5,20,12,weight=500)
        self.text(('확인 토큰 ' if c.get('partial') else '총 토큰 ')+amount(c.get('total') if c.get('known') else None),x+width*.5,y,width*.5,20,12,right=True)
        narrow=width<300;column=width if narrow else (width-16)/2;top=y+28
        groups=[('입력 구성','input_parts','input_total'),('출력 구성','output_parts','output_total')]
        for index,(label,key,total_key) in enumerate(groups):
            xx=x if narrow else x+index*(column+16);parts=c.get(key,[])
            total=c.get(total_key) if parts else None
            self.text(label,xx,top,column*.55,18,11,weight=500)
            self.text(amount(total),xx+column*.55,top,column*.45,18,11,right=True)
            self.rect(xx,top+24,column,6,'track',3);offset=0
            for part in parts:
                length=max(0,min(column-offset,column*part['share']));color=part['key'] if part['key'] in ORDER else 'unknown'
                if length:self.rect(xx+offset,top+24,length,6,color,0,pattern=color=='unknown')
                offset+=length
            for row,part in enumerate(parts):
                yy=top+38+row*24;color=part['key'] if part['key'] in ORDER else 'unknown'
                label='출력·추론 제외' if part['key']=='output' else '미분류' if color=='unknown' else part['label']
                self.dot(xx+2,yy+9,color,2)
                self.text(label,xx+9,yy,column-58,18,11,'secondary')
                self.text(amount(part['tokens']) if part.get('known',True) else '—',xx+column-48,yy,48,18,11,right=True)
            if narrow:top+=42+max(1,len(parts))*24+12

class DetailGraph(Node):
    def __init__(self,content):super().__init__(content);self.content=content
    def paint(self,p):
        p.scale(self.content.appearance.scale,self.content.appearance.scale);Drawing(p,self.content).graphs(self.content.rows(),0,0,self.content.detail_width,24,True)
    def row_at(self,x):
        m=self.content;plot=m.detail_width-36;column=int((x/m.appearance.scale-36)/(plot/24));index=column-(24-len(m.rows()))
        return m.rows()[index] if x/m.appearance.scale>=36 and 0<=index<len(m.rows()) else None
    @Slot(float,float)
    def activate_at(self,x,y):
        row=self.row_at(x)
        if row:self.content.select(self.content.call_id(row))
    @Slot(float,float)
    def hover_at(self,x,y):
        row=self.row_at(x);key=self.content.call_id(row) if row else None
        if key!=self.content.hover_id:self.content.hover_id=key;self.update()
    @Slot()
    def clear_hover(self):
        if self.content.hover_id is not None:self.content.hover_id=None;self.update()
    @Slot(int)
    def key(self,key):
        m=self.content;rows=m.rows()
        if not rows:return
        index=next((i for i,r in enumerate(rows) if m.call_id(r)==m.selected_id),-1)
        if key==Qt.Key_Left:index=max(0,index-1)
        elif key==Qt.Key_Right:index=min(len(rows)-1,index+1)
        elif key==Qt.Key_Home:index=0
        elif key==Qt.Key_End:index=len(rows)-1
        else:return
        m.select(m.call_id(rows[index]))

class DetailBody(Node):
    def __init__(self,content):super().__init__(content);self.content=content;self.scroll_offset=0
    @Slot(float)
    def setScrollOffset(self,value):
        if self.scroll_offset!=value:self.scroll_offset=value;self.update()
    def paint(self,p):
        scale=self.content.appearance.scale;p.translate(0,-self.scroll_offset);p.scale(scale,scale);d=Drawing(p,self.content)
        top=self.scroll_offset/scale;bottom=(self.scroll_offset+getattr(self,'_paint_height',self.content.panel_height()))/scale
        for item in self.content._detail_items:
            value,x,y,w,h,size,color,weight=item[:8];style=item[8] if len(item)>8 else {}
            if y+h<top or y>bottom:continue
            if style.get('kind')=='tokens':d.tokens(x,y,w,title=value)
            elif style.get('kind')=='metric':d.metric(value,x,y,w,h,size,color=color)
            elif isinstance(value,list):
                for i,line in enumerate(value):d.text(line,x,y+i*18,w,18,size,color,weight,right=style.get('right',False))
            else:d.text(value,x,y,w,h,size,color,weight,right=style.get('right',False))

class OverlayContent(Node):
    kind='plot';WIDTH=380;HEIGHT=578;BODY_SIZE=12
    def __init__(self):
        super().__init__();self.data=None;self.note='기록 확인 중';self.opacity=94;self.appearance=default_appearance(True);self.dark=True;self.compact=False
        self.detail_open=False;self.detail_inline=False;self.detail_width=208
        self.selected_id=None;self.selected_snapshot=None;self.hover_id=None;self.follow_latest=True;self.quota_lines=('','')
        self.highlight_id=None;self._highlight_timer=QTimer(self);self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self.clear_highlight)
        self._graph=DetailGraph(self);self._body=DetailBody(self)
        self.put(overlayVisible=False,overlayScale=1.,overlayInk=self.appearance.ink,overlayAccent=palette(self.appearance)['accent'].name(),detailBodyHeight=0);self.sync_details()
    @Property(QObject,constant=True)
    def detailGraph(self):return self._graph
    @Property(QObject,constant=True)
    def detailBody(self):return self._body
    @property
    def monitor_x(self):return 240 if self.detail_open and not self.detail_inline else 0
    def set_layout(self,reduced=False,detail=False,inline=False,**kwargs):
        values=(kwargs.get('compact',reduced),kwargs.get('detail_open',detail),kwargs.get('detail_inline',inline))
        if values==(self.compact,self.detail_open,self.detail_inline):return
        self.compact,self.detail_open,self.detail_inline=values;self.detail_width=348 if self.detail_inline else 208
        self.sync_details();self.update()
    def set_content(self,data,note='',dark=True,appearance=None):
        appearance=appearance or default_appearance(dark)
        checking=note in ('기록 확인 중','세션 기록 확인 중','기록 수집 중 · 잠정값')
        if data and checking:data={key:data[key] for key in ('id','home','title') if key in data}
        if note.startswith('원격 작업') or note=='현재 세션 식별 불가':data=None
        if (data,note,appearance)==(self.data,self.note,self.appearance):return
        old=(self.data or {}).get('id'),(self.data or {}).get('home');new=(data or {}).get('id'),(data or {}).get('home')
        old_call=self.call_id(self.rows()[-1]) if self.rows() else None
        if old!=new:self.selected_id=None;self.selected_snapshot=None;self.hover_id=None;self.follow_latest=True
        self.data=data;self.note=note;self.appearance=appearance;self.dark=appearance.dark
        if old!=new:self.clear_highlight()
        elif self.rows() and old_call!=self.call_id(self.rows()[-1]) and not self.state.get('reducedMotion'):
            self.highlight_id=self.call_id(self.rows()[-1]);self._highlight_timer.start(120)
        if not self.rows():self.selected_id=None;self.selected_snapshot=None;self.hover_id=None;self.follow_latest=True
        if self.follow_latest and self.rows():self.selected_snapshot=self.rows()[-1];self.selected_id=self.call_id(self.selected_snapshot)
        elif self.selected_id:self.selected_snapshot=next((r for r in self.rows() if self.call_id(r)==self.selected_id),self.selected_snapshot)
        self.put(overlayScale=appearance.scale,overlayInk=appearance.ink,overlayAccent=palette(appearance)['accent'].name(),collecting=False);self.sync_details();self.refresh_accessibility();self.update()
    def clear_highlight(self):
        self.highlight_id=None;self._highlight_timer.stop();self.update();self._graph.update()
    def rows(self):return (self.data or {}).get('recent',[])[-24:]
    @staticmethod
    def call_id(row):return row.get('id') or row.get('key')
    def select(self,key):
        self.selected_id=key;self.hover_id=None;self.selected_snapshot=next((r for r in (self.data or {}).get('all_calls',self.rows()) if self.call_id(r)==key),None)
        self.follow_latest=bool(self.rows() and key==self.call_id(self.rows()[-1]));self.sync_details()
    def selected(self):return next((r for r in self.rows() if self.call_id(r)==self.selected_id),self.selected_snapshot or {})
    def sync_details(self):
        items=self.detail_items();self._detail_items=items;height=max((r[2]+r[4] for r in items),default=16)
        colors=palette(self.appearance)
        self.put(detailBodyHeight=round(height*self.appearance.scale),detailWidth=self.detail_width,detailTitle='호출 상세',
                 overlayFamily=self.appearance.family,overlaySurface=colors['surface'].name(),overlayBorder=colors['border'].name(),
                 detailHasSelection=bool(self.selected()),detailGraphAccessible='최근 24호출 · 캐시 0–100% · 환산액',detailSelected=self.selected_id or '')
        self._graph.update();self._body.update()
        self._body.setAccessibleName('\n'.join(''.join(r[0]) if isinstance(r[0],list) else r[0] for r in items))
    def monitor_links(self):
        d=self.data or {};links=[];extra=self.context_extra()
        def add(key,target,x,y,w,h):
            if target:links.append(dict(id=key,x=x,y=y,width=w,height=h,target=target,accessible={'cache':'최근 호출의 토큰 사용량','cost':'최근 호출의 환산 근거','total':'세션 비용 내역','miss':'캐시 미적중 호출 목록','incident':'캐시 저하 근거','collection':'관측 상태 확인'}[key]))
        value=percent(d.get('cache_rate'));number=value[:-1] if value.endswith('%') else value
        cache_width=QFontMetrics(font(self.appearance.family,36,600)).horizontalAdvance(number)+(24 if value.endswith('%') else 0)
        add('cache',navigation_target(d,'cache'),16,132+extra,min(212,cache_width),40)
        cost=fitted_money(d.get('latest_cost'),124,24,self.appearance.family);cw=metric_width(cost,self.appearance.family,24)
        add('cost',navigation_target(d,'cost'),364-cw,144+extra,cw,30)
        total=fitted_money(d.get('cost'),136,18,self.appearance.family);tw=metric_width(total,self.appearance.family,18)
        add('total',navigation_target(d,'cost_total'),152-tw,322+extra,tw,24)
        status=self.layout()['status']
        if d.get('cache_misses',{}).get('count'):add('miss',navigation_target(d,'status_total',status='cache_zero'),16,status,250,16)
        events=d.get('cache_degradation',{}).get('events',[])
        if events:add('incident',navigation_target(d,'incident',event=events[-1]),266,status,98,16)
        if self.states() and any(word in self.status_text() for word in ('오류','지연','관측')):
            kind='collection' if any(word in self.status_text() for word in ('오류','지연')) else 'observation'
            add('collection',navigation_target(d,kind),16,status+16,348,16)
        return links

    def detail_links(self):
        result=[];d=self.data or {};row=self.selected()
        for index,item in enumerate(self._detail_items):
            value,x,y,w,h,*_=item;target=item[8].get('target')
            if isinstance(value,str) and value in ('모델 관측','연결 관측'):
                target=navigation_target(d,'observation',call=row)
            if target:result.append(dict(id='detail-'+str(index),x=x,y=y,width=w,height=h,target=target,accessible=str(value)))
        return result

    def context(self):
        d=self.data or {};row=self.rows()[-1] if self.rows() else d.get('latest') or {}
        secondary=' · '.join(str(v) for v in (row.get('effort'),row.get('mode'),observed_transport(row)) if recorded(v))
        return model_comparison(row),secondary
    def context_rows(self):
        value=self.context()[0]
        if not value:return []
        metrics=QFontMetrics(font(self.appearance.family,12));lines=[];line=''
        for char in value:
            if line and metrics.horizontalAdvance(line+char)>348:lines.append(line);line=''
            line+=char
        lines.append(line)
        return lines
    def context_extra(self):return max(0,len(self.context_rows())-1)*18
    def visible_bars(self):
        bars=(self.data or {}).get('token_composition',{}).get('bars',[]);result=[]
        for key in ORDER[1:]:
            selected=[b for b in bars if b['key']==key or key=='unknown' and b['key'] in ('input_unknown','output_unknown')];tokens=sum(b['tokens'] for b in selected)
            if key=='unknown' and not tokens:continue
            result.append(dict(key=key,label=LABELS[key],tokens=tokens,share=sum(b.get('share',0) for b in selected),known=any(b.get('known',True) for b in selected)))
        return result
    def states(self):
        d=self.data or {};result=[]
        if self.note:
            value={'기록 수집 중 · 잠정값':'기록 확인 중','세션 기록 확인 중':'기록 확인 중','원격 작업 · 이 PC에 사용량 기록 없음':'원격 작업 · 로컬 기록 없음'}.get(self.note,self.note)
            result.append((value,'error' if '오류' in value else 'secondary'))
        if d.get('own_calls',d.get('calls'))==0:
            result.append(('자체 호출 없음' if d.get('descendants') and d.get('calls') else '호출 기록 없음','secondary'))
        if d.get('coverage_gap'):result.append(('이전 기록 누락','secondary'))
        for state in d.get('statuses',[]):
            label=state.get('text') if isinstance(state,dict) else state
            if label=='호출 기록 없음' and d.get('descendants') and d.get('calls'):continue
            if label:result.append((label,'error' if '오류' in label else 'secondary'))
        if d.get('cache_health',{}).get('partial'):
            result.append(('일부 자체 캐시 관측 누락' if d.get('descendants') else '일부 캐시 관측 누락','secondary'))
        if not d.get('model_mismatch') and d.get('model_state') in ('관측 충돌','충돌'):
            result.append(('모델 관측 충돌','secondary'))
        if d.get('model_state')=='관측 누락':result.append(('모델 관측 누락','secondary'))
        # Current collection failures take precedence over historical gaps.
        return sorted(dict.fromkeys(result),key=lambda item:0 if item[1]=='error' else 1 if '지연' in item[0] else 2)
    def status_text(self):
        states=self.states();extra=len(states)-1+max(0,len((self.data or {}).get('_collection',{}).get('errors',[]))-1)
        return states[0][0]+(f' +{extra}' if extra>0 else '') if states else ''
    def session_scope(self):
        descendants=(self.data or {}).get('descendants',0)
        return f'세션 전체 · 하위 {descendants}개 포함' if descendants else '세션 전체'
    def notice(self):return (self.status_text(),self.states()[0][1]) if self.states() else ('','muted')
    def layout(self,reduced=None):
        reduced=self.compact if reduced is None else reduced;unknown=any(b['key']=='unknown' for b in self.visible_bars());second=bool(self.states())
        extra=self.context_extra()
        return dict(header=12,context=48,cache=116+extra,recent=184+extra,session=298+extra,tokens=None if reduced else 370+extra,status=(366 if reduced else 546+24*unknown)+extra,
                    height=(398 if reduced else self.HEIGHT+24*unknown)+16*second+extra,miss=bool((self.data or {}).get('cache_misses',{}).get('count')))
    def base_height(self,reduced=None):return self.layout(reduced)['height']
    def monitor_height(self,reduced=False):return round(self.base_height(reduced)*self.appearance.scale)
    def panel_width(self):return round((380+self.monitor_x)*self.appearance.scale)
    def panel_height(self):return round(self.base_height()*self.appearance.scale)
    def amount(self,value):return amount(value)
    def money(self,value,*args):return money(value)
    def cost_disclosure(self):
        d=self.data or {};values=[]
        if d.get('descendants'):
            values.append(f"자체 {money(d.get('own_cost'),False)} + 하위 {money(d.get('child_cost'),False)}")
        if d.get('assumed'):values.append(f"Standard 가정 {d['assumed']}호출")
        if d.get('coverage_gap'):values.append('이전 기록 누락')
        return ' · '.join(values)
    def cost_note_height(self):return 0
    def status_height(self):return 16*(1+bool(self.states()))
    def composition_full_height(self):return 164+24*any(b['key']=='unknown' for b in self.visible_bars())
    def composition_height(self):return 0 if self.compact else self.composition_full_height()
    def lines(self):
        d=self.data or {};a,b=self.context()
        return [d.get('title',''),a,b,percent(d.get('cache_rate')),money(d.get('cost')),money(d.get('mean_cost')),money(d.get('latest_cost')),
                f"{d.get('priced',0)} / {d.get('calls',0)}" if d.get('missing') else str(d.get('calls','—')),self.status_text()]
    def refresh_accessibility(self):
        d=self.data or {};values=[d.get('title',''),*self.context(),'현재 작업','최근 캐시 '+percent(d.get('cache_rate')),'최근 비용 API 환산 '+money(d.get('latest_cost'),False),
              self.session_scope(),'세션 캐시 적중률 '+percent(d.get('token_composition',{}).get('cache_hit_rate')),
              '세션 비용 '+money(d.get('cost'),False),
              '세션 호출당 평균 '+money(d.get('mean_cost'),False),self.cost_disclosure(),self.status_text()]
        if d.get('calls') is not None:
            values.append(f"산정 {d.get('priced',0)} / {d['calls']}호출" if d.get('missing') else f"세션 {d['calls']}호출")
        values.append('토큰 구성')
        values.extend(p['label']+' '+amount(p['tokens'],True)+'토큰 · 전체 '+percent(p['share']*100) for p in d.get('token_composition',{}).get('parts',[]))
        misses=d.get('cache_misses',{})
        if misses.get('count'):values.append(f"{'자체 ' if d.get('descendants') else ''}캐시 미적중 {misses['count']}회 · 해당 입력 {amount(misses.get('input'),True)}토큰")
        self.setAccessibleName('\n'.join(filter(None,values)))
    def detail_items(self):
        d=self.data or {};row=self.selected();w=self.detail_width;items=[];y=0
        def text(value,x=0,width=None,size=11,color='ink',weight=400,height=18,at=None,right=False,kind=None):
            nonlocal y
            yy=y if at is None else at;limit=width if width is not None else w;value=str(value)
            if at is None:
                metrics=QFontMetrics(font(self.appearance.family,size,weight));lines=[];line=''
                for char in value:
                    if line and metrics.horizontalAdvance(line+char)>limit:lines.append(line);line=''
                    line+=char
                lines.append(line)
                if len(lines)>1:value=lines;height=len(lines)*18
            items.append((value,x,yy,limit,height,size,color,weight,dict(right=right,kind=kind)))
            if at is None:y+=height
        def pair(label,value,color='ink',numeric=False):
            nonlocal y
            metrics=QFontMetrics(font(self.appearance.family,11))
            if numeric and metrics.horizontalAdvance(str(value))>w-92:
                text(label,color='secondary');text(value,right=True);y+=4;return
            text(label,width=88,color='secondary',at=y);metrics=QFontMetrics(font(self.appearance.family,11));lines=[];line=''
            for char in str(value):
                if line and metrics.horizontalAdvance(line+char)>w-92:lines.append(line);line=''
                line+=char
            lines.append(line);items.append((lines,92,y,w-92,len(lines)*18,11,color,400,dict(right=numeric)));y+=len(lines)*18+4
        def tokens(value):return amount(value,True)+(' 토큰' if value is not None else '')
        text('현재 작업',color='secondary',weight=600);y+=8
        if row:
            text(f"{timestamp(row.get('ts'))} · {row.get('ordinal','—')}호출",color='secondary');y+=8;half=(w-12)/2
            text('캐시',width=half,color='secondary',at=y);text('비용 · 환산',x=half+12,width=half,color='secondary',at=y);y+=18
            text(percent(row.get('cache_rate',row.get('rate'))),width=half,size=20,color='warning' if row.get('cache_warning') else 'cached_text',weight=600,height=28,at=y,kind='metric')
            text(fitted_money(row.get('cost'),half,20,self.appearance.family),x=half+12,width=half,size=20,weight=600,height=28,at=y,kind='metric');y+=40
            pair('정확한 비용',money(row.get('cost'),False),numeric=True)
            for label,key in [('입력','input'),('캐시 읽기','cached'),('캐시 쓰기','written'),('출력·추론 제외','non_reasoning'),('추론','reasoning'),('입력 미분류','input_unknown'),('출력 미분류','output_unknown'),('미분류','unknown')]:
                if '미분류' in label and not row.get(key):continue
                if row.get(key) is None:continue
                pair(label,tokens(row.get(key)),numeric=True)
            y+=12
            comparison=model_comparison(row)
            if comparison:text(comparison,color='error' if comparison.startswith('모델 불일치') else 'ink')
            for label,value in [('추론 강도',row.get('effort')),('요청 모드',row.get('mode')),('통신 방식',observed_transport(row))]:
                if not recorded(value):continue
                pair(label,value,'error' if '모델' in label and row.get('model_mismatch') else 'ink')
            for warning in row.get('warnings',[]):
                label=warning.get('text','') if isinstance(warning,dict) else warning
                if comparison and label=='모델명 불일치':continue
                text(label,color='error' if '불일치' in label or '오류' in label else 'warning' if label in ('캐시 미적중','캐시 저하 의심') else 'secondary')
        else:text('자체 호출 없음' if d.get('descendants') and not d.get('own_calls') else '호출 기록 없음' if d.get('calls')==0 else self.note or '기록 확인 중',color='secondary')
        y+=16;text(self.session_scope(),color='secondary',weight=600);y+=8
        pair('세션 비용',money(d.get('cost'),False),numeric=True)
        if d.get('descendants'):
            pair('자체 호출 비용',money(d.get('own_cost'),False),numeric=True)
            pair(f"하위 {d['descendants']}개 비용",money(d.get('child_cost'),False),numeric=True)
        pair('세션 호출당 평균',money(d.get('mean_cost'),False),numeric=True)
        if d.get('partial'):
            text(f"산정 {d.get('priced',0)} / {d.get('calls',0)}호출",color='secondary')
        if d.get('assumed'):text(f"Standard 가정 {d['assumed']}호출",color='secondary')
        c=d.get('token_composition',{})
        if c.get('cache_hit_partial'):text('확인분 입력 기준',color='secondary')
        collection_errors={}
        for error in d.get('_collection',{}).get('errors',[]):
            label=collection_error_label(error);collection_errors[label]=collection_errors.get(label,0)+1
        for state,color in self.states():
            if state!='수집 오류' or not collection_errors:text(state,color=color)
        if '지연' in self.note:pair('마지막 확인',timestamp(d.get('_collection',{}).get('last_confirmed_at')))
        for label,count in collection_errors.items():text(label+(f' · {count}건' if count>1 else ''),color='error')
        misses=d.get('cache_misses',{});degraded=d.get('cache_degradation',{});y+=8
        observed=d.get('calls') is not None
        pair('자체 캐시 읽기 0' if d.get('descendants') else '캐시 읽기 0',f"{misses.get('count',0):,}회" if observed else '—',numeric=True)
        pair('해당 입력',tokens(None if not observed or misses.get('input_missing') else misses.get('input',0)),numeric=True)
        for event in misses.get('events',[]):
            text(timestamp(event.get('ts')),color='secondary');items[-1][8]['target']=navigation_target(d,'status_total',status='cache_zero');pair('입력',tokens(event.get('input')),numeric=True)
        if degraded.get('count'):
            pair('자체 캐시 저하 의심' if d.get('descendants') else '세션 저하 의심',f"{degraded['count']:,}회",numeric=True)
            for event in degraded.get('events',[]):
                y+=8;text(timestamp(event.get('ts'))+' · '+str(event.get('count',1))+'호출',color='secondary');items[-1][8]['target']=navigation_target(d,'incident',event=event)
                if event.get('resolved'):pair('회복 확인',timestamp(event.get('ended_at')))
                segment=event.get('segment',())
                for label,value in zip(('비교 모델','비교 모드','캐시 정책'),segment):
                    if value is not None:pair(label,value)
                baseline_read=event.get('baseline_read')
                pair('기준 평균 읽기',f'{baseline_read:,.1f}'.removesuffix('.0')+' 토큰' if baseline_read is not None else '—',numeric=True)
                pair('기준 적중률',percent(event.get('baseline_rate')*100 if event.get('baseline_rate') is not None else None),numeric=True)
                baseline=event.get('baseline_calls',[])
                if baseline:pair('기준 호출',', '.join(str(call['ordinal']) for call in baseline)+'호출',numeric=True)
                elif event.get('baseline_keys'):pair('기준 호출 ID',', '.join(map(str,event['baseline_keys'])))
                for call in event.get('comparison_calls',[]):
                    text(f"{timestamp(call.get('ts'))} · {call['ordinal']}호출",color='secondary')
                    pair('캐시 읽기',tokens(call.get('cached')),numeric=True)
                    pair('적중률',percent(call.get('cache_rate')),numeric=True)
        y+=16
        if self.compact:
            height=(28+sum(54+24*max(1,len(c.get(k,[]))) for k in ('input_parts','output_parts'))) if w<348 else self.composition_full_height()
            text('토큰 구성',height=height,kind='tokens');y+=12
        token_label='세션 확인 토큰' if c.get('partial') else '세션 총 토큰'
        pair(token_label,tokens(c.get('total') if c.get('known',d and d.get('calls')) else None),numeric=True)
        for part in c.get('parts',[]):pair(part['label'],tokens(part['tokens'])+' · '+percent(part['share']*100),numeric=True)
        for key,label in [('input_unknown','입력 미분류'),('output_unknown','출력 미분류')]:
            if c.get('counts',{}).get(key):pair(label,tokens(c['counts'][key]),numeric=True)
        return items
    def paint(self,p):
        p.save();p.scale(self.appearance.scale,self.appearance.scale);d=Drawing(p,self);width=380+self.monitor_x;height=self.base_height()
        surface=QColor(d.colors['surface']);surface.setAlphaF(self.opacity/100);p.setPen(QPen(d.colors['border'],1));p.setBrush(surface);p.drawRoundedRect(QRectF(.5,.5,width-1,height-1),16,16)
        if self.detail_open:
            if not self.detail_inline:
                d.text('호출 상세',16,12,208,28,14,weight=600);d.line(self.monitor_x,12,self.monitor_x,height-12)
            else:d.text((self.data or {}).get('title',''),16,12,260,28,14,weight=600,elide=True)
        if self.detail_inline:p.restore();return
        p.save();p.translate(self.monitor_x,0);data=self.data or {};layout=self.layout()
        d.text(data.get('title',''),16,12,260,28,14,weight=600,elide=True)
        first,second=self.context();extra=self.context_extra()
        for index,line in enumerate(self.context_rows()):d.text(line,16,48+index*18,348,18,12,'error' if first.startswith('모델 불일치') else 'secondary')
        p.translate(0,extra);d.text(second,16,66,348,18,12,'secondary')
        d.text('현재 작업',16,94,348,18,11,'secondary',weight=600)
        d.text('최근 캐시',16,116,212,18,12,'secondary');value=percent(data.get('cache_rate'));number=value[:-1] if value.endswith('%') else value
        color='warning' if data.get('latest',{}).get('cache_warning') or data.get('cache_misses',{}).get('current') else 'cached_text'
        number_width=QFontMetrics(font(self.appearance.family,36,600)).horizontalAdvance(number);d.number(number,16,168,190,36,color)
        if value.endswith('%'):d.number('%',16+number_width+3,168,30,20,color,500)
        c=data.get('token_composition',{})
        d.text('최근 비용 · 환산',240,116,124,18,12,'secondary');d.metric(fitted_money(data.get('latest_cost'),124,24,self.appearance.family),240,144,124,30,24,baseline=168)
        d.graphs(self.rows()[-12:],16,184,348,12)
        d.text(self.session_scope(),16,274,230,18,11,'secondary',weight=600)
        d.text(('확인분 ' if c.get('cache_hit_partial') else '')+'적중 '+percent(c.get('cache_hit_rate')),248,274,116,18,11,'secondary',right=True)
        d.rect(8,298,364,56,'band',10,alpha=self.opacity/100)
        labels=['세션 비용',
                '호출당 평균','산정 / 전체 호출' if data.get('missing') else '호출 수']
        values=[fitted_money(data.get('cost'),136,18,self.appearance.family),fitted_money(data.get('mean_cost'),112,18,self.appearance.family),call_count(data,self.appearance.family)]
        for label,value,x,w in zip(labels,values,(16,164,288),(136,112,76)):
            d.text(label,x,302,w,18,11,'secondary');d.metric(value,x,322,w)
        if not self.compact:d.tokens(16,370,title='토큰 구성')
        misses=data.get('cache_misses',{});degraded=data.get('cache_degradation',{})
        miss_label='자체 캐시 미적중' if data.get('descendants') else '캐시 미적중'
        miss_text=f"{miss_label} {amount(misses.get('count',0))}회 · 해당 입력 "+amount(None if misses.get('input_missing') else misses.get('input',0)) if data.get('calls') is not None else f'{miss_label} — · 해당 입력 —'
        d.text(miss_text,16,layout['status']-extra,250,color='warning' if misses.get('current') else 'secondary')
        if degraded.get('count'):
            label='자체 저하' if data.get('descendants') else '저하 의심'
            d.text(f"{label} {amount(degraded['count'])}회",266,layout['status']-extra,98,color='secondary',right=True)
        if self.states():d.text(self.status_text(),16,layout['status']+16-extra,348,color=self.states()[0][1])
        p.restore();p.restore()

class SessionOverlay(QuickHost):
    WIDTH,HEIGHT=OverlayContent.WIDTH,OverlayContent.HEIGHT
    def __init__(self):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput)
        self.setWindowTitle('Cache Monitor · 세션 오버레이');self.setAttribute(Qt.WA_TranslucentBackground);self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.content_model=OverlayContent();self.set_scene(self.content_model,'OverlayScene.qml',transparent=True);self.resize(self.panel_width(),self.panel_height())
    @property
    def opacity(self):return self.content_model.opacity
    @opacity.setter
    def opacity(self,value):self.content_model.opacity=value;self.content_model.update()
    @property
    def data(self):return self.content_model.data
    @property
    def note(self):return self.content_model.note
    @property
    def appearance(self):return self.content_model.appearance
    def lines(self):return self.content_model.lines()
    def cost_disclosure(self):return self.content_model.cost_disclosure()
    def cost_note_height(self):return self.content_model.cost_note_height()
    def status_height(self):return self.content_model.status_height()
    def composition_height(self):return self.content_model.composition_height()
    def base_height(self):return self.content_model.base_height()
    def panel_width(self):return self.content_model.panel_width()
    def panel_height(self):return self.content_model.panel_height()
    def set_layout(self,*args,**kwargs):self.content_model.set_layout(*args,**kwargs);self.resize(self.panel_width(),self.panel_height())
    def set_content(self,*args,**kwargs):
        self.content_model.set_content(*args,**kwargs);self.resize(self.panel_width(),self.panel_height());self.setAccessibleName(self.content_model.state['accessible']);self.setToolTip(self.content_model.context()[0])
    def closeEvent(self,event):self.release_scene();super().closeEvent(event)
    def showEvent(self,event):super().showEvent(event);self.content_model.put(overlayVisible=True)
    def hideEvent(self,event):self.content_model.put(overlayVisible=False);super().hideEvent(event)
