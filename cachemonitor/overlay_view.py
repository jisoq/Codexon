"""Fixed logical-pixel monitor and expandable subagent cost detail."""
import json
import math
import re
import time
from datetime import datetime
from dataclasses import dataclass
from enum import Enum
from PySide6.QtCore import Qt, QRectF, QPointF, QObject, Property, Slot, Signal, QTimer, QEasingCurve
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QBrush, QLinearGradient, QPainterPath
from .presentation import Node
from .quick_runtime import QuickHost
from .overlay_appearance import default_appearance
from .ui_details import recorded, observed_transport, model_comparison
from .token_colors import ui_palette
from .i18n import tr, Verbatim, formatted
from .charts import value_text
from .overlay_monitor import WIDTH, MARGIN, SECTION_WIDTH, TITLE_WIDTH, INLINE_CLOSE_X
from .analysis_delivery import ANALYSIS_DELAY_SECONDS
from .overlay_data import main_usage, RECENT_CALLS
from .overlay_layout import AGENT_SUMMARY_HEIGHT
from .overlay_icons import agent_mark

LABELS = dict(cached='캐시 읽기', uncached='일반 입력', written='캐시 쓰기', reasoning='추론', output='추론 외', unknown='미분류')
ORDER = tuple(LABELS)
SUBAGENT_GAP = 19
AGENT_ICON_SIZE = 12
AGENT_ICON_GAP = 4
AGENT_FRAME_INSET = 4

def money(value):
    if value is None or not math.isfinite(value): return '—'
    places=2 if abs(round(value,4))>=1 else 4
    return f'${value:,.{places}f}'

def amount(value):
    if value is None: return '—'
    for divisor, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
        if abs(value) >= divisor: return f'{value/divisor:.1f}'.removesuffix('.0')+suffix
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
    if len(brief)<=40 and re.fullmatch(r'(?:기록|수집|관측)[가-힣0-9,: ()]*?(?:오류|실패|지연|누락|손상|거부|없음|중)',brief):
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
    p=ui_palette(appearance)
    p.update(surface=p['overlay'],band=p['panel'],secondary=p['muted'],meta=p['muted'])
    neutral='#ffffff' if contrast(p['surface'],'#ffffff')>contrast(p['surface'],'#000000') else '#000000'
    p['interaction_tint']=mix(p['surface'],neutral,.60)
    return {key:QColor(value) for key,value in p.items()}


@dataclass(frozen=True)
class TextRun:
    text: str
    color: str = 'secondary'
    strike: bool = False


class HighlightStyle(Enum):
    RIBBON = ('ribbon', '잔광 리본')
    SCAN = ('scan', '라이트 스캔')
    EDGE = ('edge', '테두리 스윕')
    PULSE = ('pulse', '소프트 펄스')
    TRAIL = ('trail', '신호 트레일')

    def __init__(self, key, label):
        self.key = key
        self.label = label


@dataclass(frozen=True)
class RowGlow:
    start_ms: float
    reduced_motion: bool

    def progress(self, now_ms):
        return max(0.,min(1.,(now_ms-self.start_ms)/(300 if self.reduced_motion else 800)))

    def finished(self, now_ms):
        return self.progress(now_ms)>=1.


class RowGlows:
    def __init__(self):
        self.appearances=None
        self.active={}

    def update(self, rows, *, now_ms, reduced_motion=False):
        self.advance(now_ms=now_ms)
        self.active={owner:glow for owner,glow in self.active.items() if owner in rows}
        if self.appearances is not None:
            for owner,appearance in rows.items():
                if self.appearances.get(owner)!=appearance:
                    self.active[owner]=RowGlow(now_ms,reduced_motion)
        self.appearances=dict(rows) if rows else None

    def advance(self, *, now_ms):
        owners=frozenset(self.active)
        self.active={owner:glow for owner,glow in self.active.items() if not glow.finished(now_ms)}
        return owners

    @property
    def needs_frame(self):return bool(self.active)

    def clear(self):
        self.appearances=None;self.active.clear()


def fitted_model_runs(runs, metrics, width):
    response,request,suffix=runs
    suffix_width=min(metrics.horizontalAdvance(suffix.text),max(0,width*.35))
    available=max(0,width-suffix_width)
    response_width=min(metrics.horizontalAdvance(response.text),max(available/2,available-metrics.horizontalAdvance(request.text)))
    widths=(response_width,available-response_width,suffix_width)
    return tuple(TextRun(metrics.elidedText(run.text,Qt.ElideRight,max(0,int(space))),run.color,run.strike)
                 for run,space in zip(runs,widths))


_glow_easing=QEasingCurve(QEasingCurve.BezierSpline)
_glow_easing.addCubicBezierSegment(QPointF(.2,.5),QPointF(.4,1),QPointF(1,1))


def paint_row_glow(p,x,y,w,h,*,style,glow,now_ms,color):
    if glow.finished(now_ms):return
    progress=glow.progress(now_ms)
    def tint(alpha):
        result=QColor(color);result.setAlphaF(alpha);return result
    def wash(alpha,width):
        gradient=QLinearGradient(x,y,x+width,y)
        gradient.setColorAt(0,tint(alpha));gradient.setColorAt(1,tint(alpha*.16))
        p.setPen(Qt.NoPen);p.setBrush(QBrush(gradient));p.drawRoundedRect(QRectF(x,y,width,h),5,5)
    p.save()
    try:
        p.setRenderHint(QPainter.Antialiasing)
        clip=QPainterPath();clip.addRoundedRect(QRectF(x,y,w,h),5,5)
        p.setClipPath(clip,Qt.IntersectClip)
        if glow.reduced_motion:
            wash(.17*(1-progress),w);return
        age=_glow_easing.valueForProgress(progress);peak=.95
        def at(*frames):
            for (start,a),(end,b) in zip(frames,frames[1:]):
                if age<=end:return a+(b-a)*(age-start)/(end-start)
            return frames[-1][1]
        if style is HighlightStyle.RIBBON:
            alpha=peak*at((0,1),(.22,.72),(1,0))
            width=w*at((0,.83),(.22,1),(1,1))
            wash(.25*alpha,width)
        elif style is HighlightStyle.SCAN:
            alpha=peak*at((0,0),(.18,1),(.7,.6),(1,0))
            width=w*.75;left=x+width*(-1.1+2.6*age)
            gradient=QLinearGradient(left,y,left+width,y)
            gradient.setColorAt(0,tint(0));gradient.setColorAt(.5,tint(.31*alpha));gradient.setColorAt(1,tint(0))
            p.setPen(Qt.NoPen);p.setBrush(QBrush(gradient));p.drawRect(QRectF(left,y,width,h))
            tip=x+(w-1)*age
            gradient=QLinearGradient(tip,y+5,tip,y+h-5)
            gradient.setColorAt(0,tint(0));gradient.setColorAt(.5,tint(peak*at((0,0),(.15,1),(1,0))));gradient.setColorAt(1,tint(0))
            p.setBrush(QBrush(gradient));p.drawRect(QRectF(tip,y+5,1.6,h-10))
        elif style is HighlightStyle.EDGE:
            wash(.25*peak*at((0,0),(.35,.25),(1,0)),w)
            length=(w-10)*at((0,.04),(.5,1),(1,1))
            alpha=peak*at((0,1),(.5,1),(1,0))
            for left,top in ((x+5,y),(x+w-5-length,y+h-1.6)):
                gradient=QLinearGradient(left,top,left+length,top)
                gradient.setColorAt(0,tint(0));gradient.setColorAt(.5,tint(alpha));gradient.setColorAt(1,tint(0))
                p.setPen(Qt.NoPen);p.setBrush(QBrush(gradient));p.drawRect(QRectF(left,top,length,1.6))
        elif style is HighlightStyle.PULSE:
            p.setPen(Qt.NoPen);p.setBrush(tint(.23*math.sin(math.pi*progress)))
            p.drawRoundedRect(QRectF(x,y,w,h),5,5)
        elif style is HighlightStyle.TRAIL:
            alpha=peak*at((0,0),(.18,1),(.72,.75),(1,0))
            length=min(42,w*.2);left=x-length+(w-5+length)*age
            gradient=QLinearGradient(left,y,left+length,y)
            gradient.setColorAt(0,tint(0));gradient.setColorAt(1,tint(alpha))
            p.setPen(Qt.NoPen);p.setBrush(QBrush(gradient));p.drawRect(QRectF(left,y+h-2,length,2))
            p.setBrush(tint(peak*at((0,0),(.2,1),(1,0))))
            p.drawEllipse(QPointF(x+(w-5)*age,y+h-2),2,2)
            if progress<.65:
                ring=_glow_easing.valueForProgress(progress/.65);radius=7*(.7+ring)
                p.setPen(QPen(tint(peak*(1-ring)),1.5));p.setBrush(Qt.NoBrush)
                p.drawEllipse(QPointF(x+AGENT_ICON_SIZE/2,y+12),radius,radius)
    finally:p.restore()


class Drawing:
    def __init__(self,painter,content):
        self.p=painter;self.model=content;self.colors=palette(content.appearance);self.now_ms=time.monotonic()*1000
        painter.setRenderHint(QPainter.Antialiasing)
    def text(self,value,x,y,w,h=16,size=11,color='ink',weight=400,right=False,elide=False,center=False,wrap=False):
        literal=isinstance(value,Verbatim);value=tr(str(value))
        while value.startswith('$') and size>1 and QFontMetrics(font(self.model.appearance.family,size,weight)).horizontalAdvance(value)>w:
            size-=1
        p=self.p;p.setFont(font(self.model.appearance.family,size,weight));p.setPen(self.colors.get(color,color))
        if elide:value=QFontMetrics(p.font()).elidedText(str(value),Qt.ElideRight,round(w))
        alignment=Qt.AlignHCenter if center else Qt.AlignRight if right else Qt.AlignLeft
        p.drawText(QRectF(x,y,w,h),alignment|Qt.AlignVCenter|(Qt.TextWordWrap if wrap else 0),Verbatim(value) if literal else str(value))
    def model_line(self,runs,x,y,w,h,size=11,weight=400):
        metrics=QFontMetrics(font(self.model.appearance.family,size,weight))
        runs=fitted_model_runs(runs,metrics,w)
        self.p.save();self.p.setClipRect(QRectF(x,y,w,h))
        for run in runs:
            run_font=font(self.model.appearance.family,size,weight);run_font.setStrikeOut(run.strike)
            self.p.setFont(run_font);self.p.setPen(self.colors.get(run.color,run.color))
            self.p.drawText(QRectF(x,y,w,h),Qt.AlignLeft|Qt.AlignVCenter,run.text)
            advance=metrics.horizontalAdvance(run.text);x+=advance;w-=advance
        self.p.restore()
    def row_glow(self,x,y,w,h,*,owner,color):
        glow=self.model.row_glows.active.get(owner)
        if glow is not None:
            paint_row_glow(self.p,x,y,w,h,style=self.model.highlight_style,glow=glow,now_ms=self.now_ms,color=color)
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
    def rect(self,x,y,w,h,color,radius=0,alpha=None,pattern=False,*,outline=False):
        if w<=0 or h<=0:return
        p=self.p;c=QColor(self.colors.get(color,color))
        if alpha is not None:c.setAlphaF(alpha)
        if outline:p.setPen(QPen(c,1));p.setBrush(Qt.NoBrush)
        else:p.setPen(Qt.NoPen);p.setBrush(c)
        if radius:p.drawRoundedRect(QRectF(x,y,w,h),radius,radius)
        else:p.drawRect(QRectF(x,y,w,h))
        if pattern:
            hatch=QColor(self.colors['surface']);hatch.setAlpha(145);p.setBrush(QBrush(hatch,Qt.BDiagPattern));p.drawRect(QRectF(x,y,w,h))
    def line(self,x,y,x2,y2,color='border',width=1,dashed=False):
        pen=QPen(QColor(self.colors.get(color,color)),width)
        if dashed:pen.setDashPattern([1,1])
        self.p.setPen(pen);self.p.drawLine(QPointF(x,y),QPointF(x2,y2))
    def dot(self,x,y,color='ink',r=1.5):
        self.p.setPen(Qt.NoPen);self.p.setBrush(QColor(self.colors.get(color,color)));self.p.drawEllipse(QPointF(x,y),r,r)
    def tokens(self,x,y,width=348,title='토큰'):
        c=(self.model.data or {}).get('token_composition',{})
        self.text(title,x,y,width*.5,20,12,weight=500)
        self.text(('Total (확인분) ' if c.get('partial') else 'Total ')+amount(c.get('total') if c.get('known') else None),x+width*.5,y,width*.5,20,12,right=True)
        narrow=width<300;column=width if narrow else (width-16)/2;top=y+28
        groups=[('입력','input_parts','input_total'),('출력','output_parts','output_total')]
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
                label='추론 외' if part['key']=='output' else '미분류' if color=='unknown' else part['label']
                self.dot(xx+2,yy+9,color,2)
                self.text(label,xx+9,yy,column-58,18,11,'secondary')
                self.text(amount(part['tokens']) if part.get('known',True) else '—',xx+column-48,yy,48,18,11,right=True)
            if narrow:top+=42+max(1,len(parts))*24+12

def paint_items(d,items,top=0,bottom=math.inf):
    for item in items:
        value,x,y,w,h,size,color,weight=item[:8];style=item[8] if len(item)>8 else {}
        if y+h<top or y>bottom:continue
        if style.get('kind')=='agent-heartbeat':continue
        if style.get('kind')=='cost-bar':
            d.rect(x,y,w,h,'track',h/2);offset=0
            total=style.get('total',sum(part['value'] for part in style['parts']))
            for part in style['parts']:
                length=min(w-offset,w*part['value']/total) if total else 0
                if length:d.rect(x+offset,y,length,h,part['color'],pattern=True)
                offset+=length
        elif style.get('kind')=='tokens':d.tokens(x,y,w,title=value)
        elif style.get('kind')=='agent-update':d.row_glow(x,y,w,h,owner=style['owner'],color=style['color'])
        elif style.get('kind')=='agent-frame':d.rect(x+.5,y+.5,w-1,h-1,'border',radius=5,alpha=.6,outline=True)
        elif style.get('kind')=='agent-separator':d.rect(x,y,w,h,'border',alpha=.45)
        elif style.get('kind')=='chevron':
            points=((x,y+8),(x+3,y+11),(x+6,y+8)) if style['expanded'] else ((x+1,y+7),(x+4,y+h/2),(x+1,y+13))
            for start,end in zip(points,points[1:]):d.line(*start,*end,color,1)
        elif style.get('kind')=='metric':d.metric(value,x,y,w,h,size,color=color)
        elif style.get('modelRuns'):d.model_line(style['modelRuns'],x,y,w,h,size,weight)
        elif isinstance(value,list):
            for i,line in enumerate(value):d.text(line,x,y+i*18,w,18,size,color,weight,right=style.get('right',False))
        else:d.text(value,x,y,w,h,size,color,weight,right=style.get('right',False),elide=style.get('kind')=='single-line')
        if mark:=style.get('agentMark'):
            mark.paint(d.p,QRectF(x-AGENT_ICON_SIZE-AGENT_ICON_GAP,y+(h-AGENT_ICON_SIZE)/2,AGENT_ICON_SIZE,AGENT_ICON_SIZE))
        if style.get('dot'):d.dot(x-7,y+h/2,style['dot'],2.5)
        if style.get('square'):
            if style.get('tokenMarker'):d.rect(x-16,y+5,12,8,style['square'],pattern=True)
            else:d.rect(x-9,y+h/2-3,6,6,style['square'])


class DetailBody(Node):
    def __init__(self,content):super().__init__(content);self.content=content;self.scroll_offset=0
    @Slot(float)
    def setScrollOffset(self,value):
        if self.scroll_offset!=value:self.scroll_offset=value;self.update()
    def paint(self,p):
        scale=self.content.appearance.scale;p.translate(0,-self.scroll_offset);p.scale(scale,scale);d=Drawing(p,self.content)
        top=self.scroll_offset/scale;bottom=(self.scroll_offset+getattr(self,'_paint_height',self.content.panel_height()))/scale
        paint_items(d,self.content._detail_items,top,bottom)

class OverlayContent(Node):
    layout_requested=Signal()
    kind='plot';WIDTH=WIDTH;HEIGHT=720;BODY_SIZE=12
    def __init__(self):
        super().__init__();self.data=None;self.note='기록 확인 중';self.opacity=94;self.appearance=default_appearance(True);self.dark=True;self.compact=False
        self.detail_open=False;self.detail_inline=False;self.detail_width=SECTION_WIDTH-8
        self.main_expanded=False;self.visible_agent_counts={};self.available_height=None;self.chart_style='bar'
        self._sub_summary_heights=[];self._sub_expanded_height=0
        self.detail_group='sub';self.expanded_groups=set()
        self.selected_id=None;self.selected_snapshot=None;self.follow_latest=True;self.graph_pinned=False;self.quota_lines=('','')
        self.monitor_tab='history';self.composition_unit='usd';self.lower_offset=0;self.settings=None
        self._freshness_label=''
        self.preparation=None
        self.highlight_id=None;self._highlight_timer=QTimer(self);self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self.clear_highlight)
        self.highlight_style=HighlightStyle.RIBBON;self.row_glows=RowGlows()
        self._row_glow_timer=QTimer(self);self._row_glow_timer.setInterval(16);self._row_glow_timer.setTimerType(Qt.PreciseTimer)
        self._row_glow_timer.timeout.connect(self.advance_row_glows)
        self._body=DetailBody(self)
        self.put(overlayVisible=False,overlayScale=1.,overlayInk=self.appearance.ink,overlayAccent=palette(self.appearance)['accent'].name(),detailBodyHeight=0);self.sync_details()
    @Property(QObject,constant=True)
    def detailBody(self):return self._body
    @property
    def monitor_x(self):return 0
    @property
    def detail_extent(self):return 0
    @property
    def is_remote(self):return self.note.startswith('원격 작업')
    @property
    def background_refresh(self):
        return bool(self.data and 'calls' in self.data and self.note=='갱신 중'
                    and self.data.get('_collection',{}).get('reason') in ('analysis_delay','detail_pending'))
    @property
    def checking_records(self):return not self.background_refresh and self.note in ('기록 준비 중','기록 확인 중','세션 기록 확인 중','기록 수집 중 (잠정값)','갱신 중')
    def notice_card(self):
        from .overlay_monitor import REMOTE_HEADING,REMOTE_DESCRIPTION
        if self.is_remote:return REMOTE_HEADING,REMOTE_DESCRIPTION
        if self.preparation:return self.preparation['title'],self.preparation['detail']
        if not self.data and self.note=='분석 오류':
            return self.note,'분석 결과를 표시하지 못했습니다. 문제 해결 화면에서 오류를 확인해 주세요.'
        if not self.data and self.note=='갱신 중':
            return self.note,'새 분석 결과를 기다리고 있습니다.'
        if self.note=='기록 준비 중' and (not self.data or 'calls' not in self.data):
            return '기록 준비 중','세션 기록에서 호출별 사용량을 읽고 있습니다.'
        if not self.data and self.note in ('수집 오류','수집 지연','수집 시작 중','사용 기록 수집 중지'):
            return self.note, {'수집 오류':'사용량 수집을 확인하지 못했습니다. 문제 해결 화면에서 오류를 확인해 주세요.',
                               '수집 지연':'새 수집 결과가 도착하지 않았습니다. 마지막 수집 상태를 확인해 주세요.',
                               '수집 시작 중':'수집기 실행을 요청했습니다. 새 수집 결과를 기다리고 있습니다.',
                               '사용 기록 수집 중지':'자동 수집이 중지되었습니다. 확인된 기록은 계속 표시합니다.'}[self.note]
        if self.note in ('기록 확인 중','세션 기록 확인 중','기록 수집 중 (잠정값)') and not (self.data or {}).get('_collection',{}).get('retained'):
            return '기록 준비 중','세션 기록에서 호출별 사용량을 읽고 있습니다.'
        return None
    def freshness_text(self):return self._freshness_label
    @staticmethod
    def collection_age(data,note):
        if not data or note not in ('수집 지연','수집 오류','갱신 중'):return ''
        from .i18n import formatted
        collection=data.get('_collection',{});reason=collection.get('reason')
        if reason=='analysis_delay':
            return formatted('분석 결과 수신 지연: {seconds}초 (기준 {limit}초)',
                             seconds=int(collection.get('received_age',0)),limit=ANALYSIS_DELAY_SECONDS)
        if reason=='source_error':
            return formatted('수집 결과에 오류 {count}건 / 이전 값 표시',count=collection.get('source_error_count',0))
        if reason=='service_delay':
            facts=collection.get('service_fault',{})
            if facts.get('snapshot_missing'):return tr('수집 상태 점검: 수집 결과 없음')
            if facts.get('owner_unknown'):return tr('수집 상태 점검: 수집기 식별 대기')
            if facts.get('handoff_pending'):return tr('수집 상태 점검: 새 수집기 시작 대기')
            if facts.get('snapshot_stale'):return tr('수집 상태 점검: 30초 넘게 갱신되지 않음')
            return tr('수집 상태 점검: 갱신 지연')
        if reason=='service_error':
            return formatted('수집 상태 확인 오류: {issue}',issue=tr(collection.get('service_issue','')))
        at=collection.get('last_confirmed_at')
        if not at:return tr('이전 확인값 표시 중: 확인 시각 없음')
        seconds=max(0,int(time.time()-at))
        if seconds<60:return tr('1분 이내 확인값 표시 중')
        if seconds<3600:return formatted('{minutes}분 전 확인값 표시 중',minutes=seconds//60)
        return formatted('{time} 확인값 표시 중',time=timestamp(at))
    def display_title(self):
        from .overlay_monitor import REMOTE_TITLE
        return tr(REMOTE_TITLE) if self.is_remote else (self.data or {}).get('title') or (tr('세션 사용량') if self.notice_card() else '')
    def set_layout(self,reduced=False,detail=False,inline=False,**kwargs):
        values=(kwargs.get('compact',reduced),kwargs.get('detail_open',detail),kwargs.get('detail_inline',inline))
        if values==(self.compact,self.detail_open,self.detail_inline):return
        self.compact,self.detail_open,self.detail_inline=values;self.lower_offset=0;self.detail_width=SECTION_WIDTH-8
        self.sync_details();self.update()
    def set_content(self,data,note='',dark=True,appearance=None,*,preparation=None):
        # An incomplete refresh is not evidence that confirmed agents disappeared.
        old=self.data or {};incoming=data or {};collection=incoming.get('_collection',{})
        same_scope=(incoming.get('home'),incoming.get('id'))==(old.get('home'),old.get('id'))
        if (same_scope and old.get('descendants',0)>0 and (collection.get('loading') or collection.get('retained'))
                and len(incoming.get('subagent_finance',{}).get('agents',()))<len(old.get('subagent_finance',{}).get('agents',()))):
            data=dict(old,_collection=dict(collection,retained=True))
        appearance=appearance or default_appearance(dark)
        checking=note in ('기록 확인 중','세션 기록 확인 중','기록 수집 중 (잠정값)')
        checking=checking or note=='기록 준비 중' and bool((data or {}).get('_collection',{}).get('loading'))
        if data and checking and not data.get('_collection',{}).get('retained'):data={key:data[key] for key in ('id','home','title') if key in data}
        if note.startswith('원격 작업') or note=='현재 세션 식별 불가':data=None
        waiting=note in ('기록 준비 중','기록 확인 중','세션 기록 확인 중','기록 수집 중 (잠정값)','갱신 중','호출 기록 없음','')
        if (preparation is not None and waiting and not (data or {}).get('calls')
                and not (data or {}).get('_collection',{}).get('retained')):
            from .preparation_progress import preparation_loading
            loading=preparation_loading(preparation,now=time.monotonic())
        else:loading=None
        def visible(value):
            return {k:v for k,v in (value or {}).items() if k!='_collection'} | {'_errors':(value or {}).get('_collection',{}).get('errors',[])}
        freshness=self.collection_age(data,note)
        if (visible(data),note,appearance,freshness,loading)==(visible(self.data),self.note,self.appearance,self._freshness_label,self.preparation):
            self.data=data
            return
        old=(self.data or {}).get('id'),(self.data or {}).get('home');new=(data or {}).get('id'),(data or {}).get('home')
        old_call=self.call_id(self.rows()[-1]) if self.rows() else None
        if old!=new:
            self.selected_id=None;self.selected_snapshot=None;self.follow_latest=True;self.graph_pinned=False
            self.expanded_groups.clear()
            self.put(mainFocused=False,detailFocusKey='')
        self.data=data;self.note=note;self.appearance=appearance;self.dark=appearance.dark;self.preparation=loading
        if self.expanded_groups:
            finance=self.detail_data()
            valid={self.detail_group_key(row,mode) for mode,field in (('model','models'),('sub','agents'))
                   for row in finance.get(field,())}
            self.expanded_groups.intersection_update(valid)
        self._freshness_label=freshness
        self.lower_offset=min(self.lower_offset,max(0,self.base_height(False)-self.base_height()))
        if old!=new:self.inspected_call=None;self.lower_offset=0;self.clear_highlight()
        elif self.rows() and old_call!=self.call_id(self.rows()[-1]) and not self.state.get('reducedMotion'):
            self.highlight_id=self.call_id(self.rows()[-1]);self._highlight_timer.start(120)
        if old!=new:self.clear_row_glows()
        self.update_row_glows()
        if not self.rows() and (self.follow_latest or self.graph_pinned):
            self.selected_id=None;self.selected_snapshot=None;self.follow_latest=True;self.graph_pinned=False
        if self.graph_pinned and not any(self.call_id(r)==self.selected_id for r in self.rows()):
            self.release_pin()
        if self.follow_latest and self.rows():self.selected_snapshot=self.rows()[-1];self.selected_id=self.call_id(self.selected_snapshot)
        elif self.selected_id:self.selected_snapshot=next((r for r in self.rows() if self.call_id(r)==self.selected_id),self.selected_snapshot)
        self.put(overlayScale=appearance.scale,overlayInk=appearance.ink,overlayAccent=palette(appearance)['accent'].name(),collecting=False);self.sync_details();self.refresh_accessibility();self.update()
    def clear_highlight(self):
        self.highlight_id=None;self._highlight_timer.stop();self.update()
    def row_appearances(self):
        if self.notice_card() or not (self.data or {}).get('id'):return {}
        data=self.data;groups=[((data.get('home'),data['id']),main_usage(data),'Main Agent')]
        groups.extend(((row.get('home'),row['id']),row,row['label']) for row in self.detail_groups())
        appearances={}
        for owner,group,label in groups:
            items=self.agent_summary_items(group,label,agent_mark(owner[1],self.appearance),False,SECTION_WIDTH,main=owner==groups[0][0])
            appearances[owner]=tuple((tr(str(item[0])),tuple((run.text,run.strike) for run in item[8].get('modelRuns',())),item[8].get('heartbeat',{}).get('mode'))
                                     for item in items if item[8].get('kind')!='chevron')
        return appearances
    def update_row_glows(self):
        if not self.state.get('overlayVisible'):self.row_glows.clear()
        self.row_glows.update(self.row_appearances(),now_ms=time.monotonic()*1000,reduced_motion=bool(self.state.get('reducedMotion')))
        if self.row_glows.needs_frame and self.state.get('overlayVisible'):
            if not self._row_glow_timer.isActive():self._row_glow_timer.start()
        else:self._row_glow_timer.stop()
    def set_reduced_motion(self,enabled):
        if enabled==bool(self.state.get('reducedMotion')):return
        self.put(reducedMotion=enabled)
        if enabled:
            now=time.monotonic()*1000;self.row_glows.advance(now_ms=now)
            for owner,glow in self.row_glows.active.items():
                if not glow.reduced_motion:self.row_glows.active[owner]=RowGlow(now,True)
            self.clear_highlight()
            if not self.row_glows.needs_frame:self._row_glow_timer.stop()
        self._body.update();self.update()
    def advance_row_glows(self):
        if not self.state.get('overlayVisible'):
            self.clear_row_glows();return
        now=time.monotonic()*1000;owners=self.row_glows.advance(now_ms=now)
        if not self.row_glows.needs_frame:self._row_glow_timer.stop()
        if owners:self._body.update();self.update()
    def clear_row_glows(self):
        self._row_glow_timer.stop();self.row_glows.clear()
        if hasattr(self,'_body'):self._body.update()
        self.update()
    def rows(self):return (self.data or {}).get('recent',[])[-RECENT_CALLS:]
    def graph_colors(self,rows):
        data=self.data or {};scope=(data.get('home'),data.get('id'))
        agents={scope,*((r.get('home'),r.get('sid')) for r in rows),
                *((r.get('home'),r.get('id')) for r in self.detail_data().get('agents',()))}
        return {agent:agent_mark(agent[1],self.appearance).color for agent in agents}
    @staticmethod
    def call_id(row):return (row.get('home'),row.get('sid'),row.get('id') or row.get('key'))
    def select(self,key):
        previous=self.selected_snapshot if self.selected_id==key else None
        self.selected_id=key;self.selected_snapshot=next((r for r in self.rows() if self.call_id(r)==key or str(r.get('id'))==str(key)),previous)
        if self.selected_snapshot:self.selected_id=self.call_id(self.selected_snapshot)
        self.follow_latest=bool(self.rows() and self.selected_id==self.call_id(self.rows()[-1]))
    def pin_call(self,key,toggle=True):
        self.put(mainFocused=False)
        if toggle and self.graph_pinned and self.selected_id==key:self.release_pin()
        else:
            self.select(key);self.graph_pinned=True;self.follow_latest=False
        self.sync_details();self.update()
    def release_pin(self):
        self.graph_pinned=False;self.follow_latest=True
        rows=self.rows();self.selected_snapshot=rows[-1] if rows else None
        self.selected_id=self.call_id(rows[-1]) if rows else None
        self.sync_details()
    def selected(self):return next((r for r in self.rows() if self.call_id(r)==self.selected_id),self.selected_snapshot or {})
    def displayed_call(self):
        if self.graph_pinned or not self.follow_latest:return self.selected()
        rows=self.rows()
        return next((row for row in rows if self.call_id(row)==getattr(self,'inspected_call',None)),rows[-1] if rows else {})
    def latest_call(self):
        rows=self.rows()
        return rows[-1] if rows else {}
    def speed_alert(self):
        # Collection trouble is not evidence that the current session is slow.
        data=self.data or {}
        return data.get('speed_health', {}) if not self.note and not data.get('_collection',{}).get('retained') else {}
    def has_subagents(self):return bool((self.data or {}).get('descendants',0)>0 and not self.notice_card())
    def set_detail_group(self,mode):
        if mode not in ('model','sub') or mode==self.detail_group:return
        self.detail_group=mode;self.sync_details()
    def toggle_detail_group(self,key):
        if key not in {row['key'] for row in self.detail_groups()}:return
        if key in self.expanded_groups:self.expanded_groups.remove(key)
        else:self.expanded_groups.add(key)
        self.sync_details();self.update();self.layout_requested.emit()
    def detail_data(self):
        return (self.data or {}).get('subagent_finance',{}) if self.has_subagents() else {}
    @staticmethod
    def detail_group_key(row,mode):
        identity=['model',row['model']] if mode=='model' else ['sub',row.get('home'),row['id']]
        return json.dumps(identity,ensure_ascii=False,separators=(',',':'))
    def detail_groups(self):
        data=self.detail_data();rows=[]
        for row in data.get('agents',()):
            key=self.detail_group_key(row,'sub')
            label=row.get('title') or row['id']
            rows.append(dict(row,key=key,label=label))
        def order(row):
            cost=-(row.get('cost') or 0)
            stamp=row.get('latest_ts')
            stamp=stamp if type(stamp) in (int,float) and math.isfinite(stamp) else float('-inf')
            return (-stamp,cost,row['key'])
        return sorted(rows,key=order)
    def model_runs(self,group):
        request=group.get('requested_model');response=group.get('response_model')
        confirmed=group.get('model_alert_confirmed') and recorded(request) and recorded(response)
        if not confirmed and not recorded(group.get('model')) and not recorded(group.get('effort')):return (TextRun(tr('미확인')),)
        model=group.get('model') or tr('미확인');effort=group.get('effort') or tr('미확인')
        mode=group.get('mode') or group.get('service_tier')
        suffix=', '+tr(effort)
        if mode and str(mode).casefold() not in ('standard','미확인','unknown'):suffix+=', '+str(mode).lower()
        if confirmed:
            color=agent_mark(group.get('id') or (self.data or {}).get('id'),self.appearance).color
            return (TextRun(tr(response),color),TextRun(' '+tr(request),strike=True),TextRun(suffix))
        return (TextRun(tr(model)+suffix),)
    def subagent_call_label(self,group):return ''.join(run.text for run in self.model_runs(group))
    def agent_model_tooltip(self,group):
        label=self.subagent_call_label(group)
        if len(self.model_runs(group))>1:
            request=group['requested_model']
            label+=tr(f"\n모델 불일치 (요청:{request}, 응답:{group['response_model']})")
        return label
    def main_model_tooltip(self):return self.agent_model_tooltip(main_usage(self.data or {}))
    def sync_details(self):
        items=self.detail_items() if self.has_subagents() else [];self._detail_items=items;height=max((r[2]+r[4] for r in items),default=16)
        actions=[]
        for item in items:
            style=item[8]
            text=''.join(item[0]) if isinstance(item[0],list) else item[0]
            if style.get('action'):
                actions.append(dict(id=style['action'],x=item[1],y=item[2],width=item[3],height=item[4],
                                    label=style.get('actionLabel',text),expanded=style.get('expanded',False),tooltip=style.get('tooltip','')))
        colors=palette(self.appearance)
        data=self.data or {}
        from .overlay_monitor import SESSION_Y,TITLE_Y,header_layout
        header=header_layout(self)
        layout=self.layout()
        heartbeat=self.heartbeat_state()
        def indicators(rows,dx=0,dy=0):
            return [dict(x=item[1]+dx,y=item[2]+dy,**item[8]['heartbeat'])
                    for item in rows if item[8].get('kind')=='agent-heartbeat']
        main_items=self.agent_summary_items(main_usage(data),'Main Agent',agent_mark(data.get('id'),self.appearance),self.main_expanded,SECTION_WIDTH,main=True)
        self.put(detailBodyHeight=round(height*self.appearance.scale),detailWidth=self.detail_width,detailTitle='Sub Agents',
                 mainHeartbeats=indicators(main_items,MARGIN,layout.get('main',0)) if not self.notice_card() else [],
                 detailHeartbeats=indicators(items),
                 checkingRecords=self.checking_records,loadingX=header['status_x'],loadingY=layout.get('status',38)+2,
                 overlayMargin=MARGIN,overlayFamily=self.appearance.family,overlaySurface=colors['surface'].name(),overlayBorder=colors['border'].name(),
                 overlayWarning=colors['warning'].name(),overlayBand=colors['band'].name(),overlaySelection=colors['selection'].name(),overlaySecondary=colors['secondary'].name(),
                 overlayInteractionTint=colors['interaction_tint'].name(),
                 detailHasSession=bool((self.data or {}).get('id')),detailSessionKey=str(((self.data or {}).get('home'),(self.data or {}).get('id'))),
                 preparationStatus=self.preparation['title'] if self.preparation else '',
                 detailActions=actions,heartbeatActive=heartbeat['mode']=='active',heartbeatCount=heartbeat['count'],totalRowVisible=not bool(self.preparation),
                 heartbeatMode=heartbeat['mode'],heartbeatColor=heartbeat['color'],heartbeatLabel=heartbeat['label'],heartbeatMessage=heartbeat['message'],
                 heartbeatX=heartbeat['x'],heartbeatY=heartbeat['y'])
        self._body.update()
        self._body.setAccessibleName(Verbatim('\n'.join(item[8].get('actionLabel') or (''.join(item[0]) if isinstance(item[0],list) else item[0]) for item in items)))
    def monitor_links(self):
        from .overlay_monitor import links
        return links(self)

    def monitor_action(self,action):
        if action=='main-detail':
            self.main_expanded=not self.main_expanded
            self.sync_details();self.refresh_accessibility();self.update();self.layout_requested.emit();return
        if action.startswith('tab-'):
            if self.graph_pinned:self.release_pin()
            self.monitor_tab='history';self.lower_offset=0
        elif action.startswith('unit-'):self.composition_unit=action[5:]
        if self.settings is not None:
            self.settings.setValue('overlay/compositionUnit',self.composition_unit)
        self.inspected_call=None;self.refresh_accessibility();self.update()

    def scroll_lower(self,delta):
        return

    def detail_links(self):
        return [dict(x=item[1],y=item[2],width=item[3],height=item[4],**item[8]['link'])
                for item in self._detail_items if item[8].get('kind')=='model-info']

    def context(self):
        d=self.data or {};row=self.rows()[-1] if self.rows() else d.get('latest') or {}
        secondary=', '.join(str(v) for v in (row.get('effort'),row.get('mode'),observed_transport(row)) if recorded(v))
        return model_comparison(row),secondary
    def context_rows(self):
        value=self.context()[0]
        if not value:return []
        metrics=QFontMetrics(font(self.appearance.family,12));lines=[];line=''
        for char in value:
            if line and metrics.horizontalAdvance(line+char)>SECTION_WIDTH:lines.append(line);line=''
            line+=char
        lines.append(line)
        return lines
    def context_extra(self):return max(0,len(self.context_rows())-1)*18
    def visible_bars(self):
        bars=main_usage(self.data or {}).get('token_composition',{}).get('bars',[]);result=[]
        for key in ORDER[1:]:
            selected=[b for b in bars if b['key']==key or key=='unknown' and b['key'] in ('input_unknown','output_unknown')];tokens=sum(b['tokens'] for b in selected)
            if key=='unknown' and not tokens:continue
            result.append(dict(key=key,label=LABELS[key],tokens=tokens,share=sum(b.get('share',0) for b in selected),known=any(b.get('known',True) for b in selected)))
        return result
    def states(self):
        d=self.data or {};result=[]
        from .overlay_monitor import partial_text
        if self.note:
            value={'기록 수집 중 (잠정값)':'기록 준비 중','세션 기록 확인 중':'기록 준비 중','기록 확인 중':'기록 준비 중','원격 작업: 이 PC에 사용량 기록 없음':'원격 작업: 로컬 기록 없음'}.get(self.note,self.note)
            result.append((value,'error' if '오류' in value else 'secondary'))
        if d.get('own_calls',d.get('calls'))==0:
            result.append(('자체 호출 없음' if d.get('descendants') and d.get('calls') else '호출 기록 없음','secondary'))
        if d.get('coverage_gap'):result.append(('이전 기록 누락','secondary'))
        if partial_text(d):result.append((partial_text(d),'secondary'))
        for state in d.get('statuses',[]):
            label=state.get('text') if isinstance(state,dict) else state
            if label=='호출 기록 없음' and d.get('descendants') and d.get('calls'):continue
            if label:result.append((label,'error' if '오류' in label else 'secondary'))
        if d.get('cache_health',{}).get('partial'):
            result.append(('일부 자체 캐시 관측 누락' if d.get('descendants') else '일부 캐시 관측 누락','secondary'))
        if not d.get('model_mismatch') and d.get('model_state') in ('관측 충돌','충돌'):
            result.append(('모델 관측 충돌','secondary'))
        if d.get('model_state')=='관측 누락':result.append(('모델 관측 누락','secondary'))
        warning=self.speed_alert().get('status')
        if warning=='pending':result.append(('출력 속도 경고 분석 중','secondary'))
        elif warning=='unavailable':result.append(('출력 속도 경고 미확인','secondary'))
        # Current collection failures take precedence over historical gaps.
        return sorted(dict.fromkeys(result),key=lambda item:0 if item[1]=='error' else 1 if '지연' in item[0] else 2)
    def status_text(self):
        states=self.states();extra=len(states)-1+max(0,len((self.data or {}).get('_collection',{}).get('errors',[]))-1)
        label=states[0][0] if states else ''
        label={'원격 작업: 로컬 기록 없음':'원격 세션','현재 세션 식별 불가':'세션 확인 불가','수집 시작 중':'기록 준비 중'}.get(label,label)
        return label+(f' +{extra}' if extra>0 else '') if label else ''
    def status_detail(self):
        from .i18n import formatted
        values=[tr(value) for value,_ in self.states()]
        if self.notice_card():values.extend(tr(value) for value in self.notice_card())
        if self.speed_alert().get('active'):values.append(tr('출력 속도 저하: 근거 보기'))
        if self.freshness_text():values.append(self.freshness_text())
        errors={}
        collection=(self.data or {}).get('_collection',{})
        for error in collection.get('errors',()):
            label=collection_error_label(error);errors[label]=errors.get(label,0)+1
        values.extend(formatted('{label}, {count}건',label=tr(label),count=count) if count>1 else tr(label) for label,count in errors.items())
        if self.note in ('수집 지연','수집 오류','갱신 중') and collection.get('last_confirmed_at'):
            values.append(tr('마지막 확인')+': '+timestamp(collection['last_confirmed_at']))
        return '\n'.join(dict.fromkeys(filter(None,values)))
    def heartbeat_state(self):
        data=self.data or {}
        count=data.get('active_requests',int(data.get('request',{}).get('state')=='진행'))
        failures=[value for value,level in self.states() if level=='error' or '지연' in value]
        failed=bool(failures or data.get('_collection',{}).get('errors'))
        pending=not data or bool(self.notice_card()) or self.note=='갱신 중'
        mode='error' if failed else 'pending' if pending else 'active' if count else 'idle'
        if mode=='error':
            label=failures[0] if failures else '수집 오류'
        elif mode=='pending':label=self.status_text() or '기록 준비 중'
        else:label='진행 중' if count else '대기'
        label=tr(label)+(' '+str(count) if mode=='active' else '')
        label_width=QFontMetrics(font(self.appearance.family,11)).horizontalAdvance(label)
        colors={'active':('#398466','#71b696'),'idle':('#b28924','#c7a457'),'error':('#dc2626','#f87171'),
                'pending':('#737373','#a3a3a3')}
        return dict(mode=mode,count=count,label=label,color=colors[mode][int(self.dark)],
                    x=WIDTH-MARGIN-label_width-(7 if label else 0)-6,y=self.layout().get('total',44)+5,
                    message=self.status_detail())
    def session_scope(self):
        from .overlay_monitor import scope
        return scope(self.data or {})
    def notice(self):return (self.status_text(),self.states()[0][1]) if self.states() else ('','muted')
    def agent_count_key(self):
        data=self.data or {}
        return json.dumps([data.get('home',''),data['id']],ensure_ascii=False) if data.get('id') else None
    def visible_agent_count(self):return self.visible_agent_counts.get(self.agent_count_key(),3)
    def automatic_sub_height(self):
        heights=self._sub_summary_heights[:self.visible_agent_count()]
        return sum(heights)+SUBAGENT_GAP*max(0,len(heights)-1)+self._sub_expanded_height
    def layout(self,reduced=None,*,constrain=True):
        if self.notice_card():
            from .overlay_monitor import notice_geometry
            return notice_geometry(self)
        reduced=self.compact if reduced is None else reduced
        from .overlay_monitor import geometry,header_status
        return geometry(self.data,reduced,has_status=bool(header_status(self)[0]),preferred_height=None,
                        main_detail_height=self.main_detail_height(),available_height=self.available_height if constrain else None,
                        auto_sub_height=self.automatic_sub_height() if self.has_subagents() else 0)
    def base_height(self,reduced=None):return self.layout(reduced)['height']
    def monitor_height(self,reduced=False):return round(self.base_height(reduced)*self.appearance.scale)
    def panel_width(self):return round((WIDTH+self.detail_extent)*self.appearance.scale)
    def panel_height(self):return round(self.base_height()*self.appearance.scale)
    def amount(self,value):return amount(value)
    def money(self,value,*args):return money(value)
    def cost_disclosure(self):
        d=main_usage(self.data or {});values=[]
        if d.get('maintenance_calls'):
            values.append(f"캐시 유지 {d['maintenance_calls']}회 포함, {money(d.get('maintenance_cost'))}, 미확인 {d.get('maintenance_missing',0)}회")
        if d.get('assumed'):values.append(f"Standard 가정 {d['assumed']}호출")
        if d.get('coverage_gap'):values.append('이전 기록 누락')
        return ', '.join(values)
    def cost_note_height(self):return 0
    def status_height(self):return 16*(1+bool(self.states()))
    def composition_full_height(self):
        c=main_usage(self.data or {}).get('token_composition',{})
        rows=max(1,len(c.get('input_parts',[])),len(c.get('output_parts',[])))
        return 84+24*(rows-1)
    def composition_height(self):return 0 if self.compact else self.composition_full_height()
    def lines(self):
        if self.preparation:
            return [self.display_title(),self.preparation['title'],self.preparation['detail'],
                    self.preparation.get('progressText',''),self.preparation['elapsedText']]
        if self.notice_card():return [self.display_title(),*[tr(text) for text in self.notice_card()]]
        d=self.data or {};own=main_usage(d);a,b=self.context()
        return [d.get('title',''),a,b,percent(d.get('cache_rate')),money(own.get('cost')),money(own.get('mean_cost')),money(d.get('latest_cost')),
                f"{own.get('priced',0)} / {own.get('calls',0)}" if own.get('missing') else str(own.get('calls','—')),self.status_text(),value_text(d.get('latest',{}).get('output_speed'),'output_speed').replace('tok/s','tps')]
    def refresh_accessibility(self):
        if self.notice_card():
            self.setAccessibleName(Verbatim('\n'.join(self.lines())))
            return
        d=self.data or {};own=main_usage(d)
        values=[d.get('title',''),self.session_scope(),self.main_model_tooltip(),
                '최근 20호출: 호출 비용과 캐시 적중률, 출력 속도, 추론 토큰',
                '세션 비용 '+money(own.get('cost')),
                '세션 캐시 적중률 '+percent(own.get('token_composition',{}).get('cache_hit_rate')),
                '평균 출력 속도 '+value_text(own.get('output_speed_summary',{}).get('value'),'output_speed').replace('tok/s','tps')]
        if self.main_expanded:
            values.extend(''.join(item[0]) if isinstance(item[0],list) else item[0] for item in self.main_items())
        misses=d.get('cache_misses',{})
        if misses.get('count'):values.append(f"{'자체 ' if d.get('descendants') else ''}캐시 미적중 {misses['count']}회, 해당 입력 {amount(misses.get('input'))}")
        values.append(self.freshness_text())
        self.setAccessibleName(Verbatim(values[0]+'\n'+tr('\n'.join(filter(None,values[1:])))))
    def total_cost(self):
        data=self.data or {}
        return 0. if data.get('calls')==0 else data.get('cost')
    def group_details(self,group,*,own=False):
        from .overlay_monitor import miss_text
        from .i18n import formatted
        w=self.detail_width-2*AGENT_FRAME_INSET;items=[];y=0
        def lines(value,width,size=11):
            value=tr(str(value));metrics=QFontMetrics(font(self.appearance.family,size));result=[];line=''
            for char in value:
                if line and metrics.horizontalAdvance(line+char)>width:result.append(line);line=''
                line+=char
            result.append(line)
            return result
        def add(value,x,top,width,*,right=False,color='ink',weight=400,size=11,marker=None):
            wrapped=lines(value,width,size);height=max(18,len(wrapped)*18)
            items.append((wrapped if len(wrapped)>1 else wrapped[0],x+AGENT_FRAME_INSET,top,width,height,size,color,weight,
                          dict(right=right,square=marker,tokenMarker=bool(marker))))
            return height
        def row(label,value,*,marker=None,total=False):
            nonlocal y
            label_width=90;inset=16 if marker else 0
            left=add(label,inset,y,label_width-inset,color='ink' if total else 'secondary',weight=600 if total else 400,size=11,marker=marker)
            right=add(value,label_width,y,w-label_width,right=True,weight=600 if total else 400)
            y+=max(left,right)+6
        def component(part,population,key):
            if part is not None:return part.get('tokens') if part.get('known',True) else None
            confirmed=population.get('category_known',{}).get(key)
            return population.get('counts',{}).get(key,0) if confirmed is True else None
        def pair(count,price):return f'{amount(count)} / {money(price)}'
        add(f"{tr('토큰 구성')} ({group.get('priced',0):,}/{group.get('calls',0):,})",0,y,w,weight=600)
        y+=26
        cost=group.get('cost_composition',{});composition=group.get('token_composition',{})
        partial=bool(cost.get('partial') or composition.get('partial') or group.get('coverage_gap') or group.get('priced',0)<group.get('calls',0))
        for side,title,keys in (
                ('input','입력',(('cached','캐시 읽기'),('written','캐시 쓰기'),('uncached','일반 입력'),('input_unknown','입력 미분류'))),
                ('output','출력',(('reasoning','추론'),('output','추론 외'),('output_unknown','출력 미분류')))):
            quantities={p['key']:p for p in composition.get(side+'_parts',())}
            costs={p['key']:p for p in cost.get(side+'_parts',())}
            count=composition.get(side+'_total') if quantities else None
            price=cost.get(side+'_total') if cost.get('known') else None
            row(tr(title)+(' ('+tr('확인분')+')' if partial else ''),pair(count,price),total=True)
            parts=[]
            for key,label in keys:
                quantity=quantities.get(key);part=costs.get(key)
                if key.endswith('unknown') and quantity is None and part is None:continue
                count=component(quantity,composition,key);price=component(part,cost,key)
                marker=key if key in ORDER else 'unknown'
                row(label,pair(count,price),marker=marker)
                if price is not None and price>0:parts.append(dict(value=price,color=marker))
            if parts:
                items.append(('',AGENT_FRAME_INSET,y,w,8,11,'ink',400,dict(kind='cost-bar',parts=parts,total=sum(p['value'] for p in parts))));y+=16
            y+=6
        if partial:
            add('확인분',0,y,w,color='secondary',size=11);y+=22
        if own and miss_text(self.data or {}):
            y+=add(miss_text(self.data or {}),0,y,w,color='secondary',size=11)+6
        if group.get('assumed'):
            y+=add(formatted('Standard 가정 {count}호출',count=group['assumed']),0,y,w,color='secondary',size=11)+6
        if group.get('coverage_gap'):add('이전 기록 누락',0,y,w,color='secondary',size=11)
        return items
    def main_items(self):return self.group_details(main_usage(self.data or {}),own=True)
    def main_detail_height(self):
        return max((item[2]+item[4] for item in self.main_items()),default=0)+8 if self.main_expanded else 0
    def agent_cost_column(self,width):
        width=min(width,self.detail_width)-2*AGENT_FRAME_INSET
        groups=[main_usage(self.data or {}),*(self.data or {}).get('subagent_finance',{}).get('agents',())]
        costs=[0 if group.get('calls')==0 else group.get('cost') for group in groups]
        costs.append((self.data or {}).get('child_cost'))
        metrics=QFontMetrics(font(self.appearance.family,12,600))
        available=width*.44
        price_width=min(available,max((metrics.horizontalAdvance(money(cost))+6 for cost in costs),default=0))
        return AGENT_FRAME_INSET+width-6-price_width,price_width
    def agent_summary_items(self,group,label,mark,expanded,width,*,main=False,cost_column=None):
        """Keep both agent scopes on the same summary and metric columns."""
        from .overlay_monitor import speed
        cost=0 if group.get('calls')==0 else group.get('cost')
        price_x,price_width=cost_column or self.agent_cost_column(width)
        width-=2*AGENT_FRAME_INSET;price_x-=AGENT_FRAME_INSET
        price=money(cost)
        chevron_x=price_x+price_width-2
        name_x=AGENT_ICON_SIZE+AGENT_ICON_GAP
        name_width=max(0,price_x-name_x-14)
        label_metrics=QFontMetrics(font(self.appearance.family,12,600))
        heartbeat_x=name_x+min(name_width,label_metrics.horizontalAdvance(tr(label)))+3
        request=group.get('request_state',group.get('request',{}).get('state'))
        mode='active' if request=='진행' else 'idle' if request else 'unknown'
        heartbeat_color=('#116b32','#38b96b')[int(self.dark)]
        runs=self.model_runs(group);model_style=dict(kind='single-line')
        model_width=width
        if len(runs)>1:
            model_style['modelRuns']=runs;model_width=max(0,width-22)
        items=[(label,name_x,0,name_width,20,12,'ink',600,dict(kind='single-line',agentMark=mark)),
               ('',heartbeat_x,1,4,4,11,'secondary',400,dict(kind='agent-heartbeat',heartbeat=dict(
                   id='main' if main else group.get('key',group.get('id','')),mode=mode,color=heartbeat_color,
                   accessible=tr(label)+' '+tr('진행 중' if mode=='active' else '대기' if mode=='idle' else '미확인')))),
               (price,price_x,0,price_width,20,12,'ink',600,dict(kind='single-line')),
               ('',chevron_x,0,8,20,13,'secondary',400,dict(kind='chevron',expanded=expanded)),
               (''.join(run.text for run in runs),0,24,model_width,18,11,'secondary',400,model_style)]
        if len(runs)>1:
            tip=formatted('호출 모델과 응답 모델이 달라 표시되었습니다.\n호출 모델: {request}, 응답 모델: {response}',
                          request=group['requested_model'],response=group['response_model'])
            model_metrics=QFontMetrics(font(self.appearance.family,11))
            info_x=sum(model_metrics.horizontalAdvance(run.text) for run in fitted_model_runs(runs,model_metrics,model_width))+4
            items.append(('',info_x,24,18,18,11,'secondary',400,dict(kind='model-info',link=dict(
                id='main-model-info' if main else 'model-info-'+group['key'],icon='info',interaction='tooltip',
                color=palette(self.appearance)['secondary'].name(),tooltip=tip,accessible=tip))))
        half=width/2
        metrics=(('캐시',0,width*.14,False),
                 (percent(group.get('token_composition',{}).get('cache_hit_rate')),width*.14,half-6,True),
                 ('평균 출력',half+6,width*.75,False),
                 (speed(group.get('output_speed_summary',{}).get('value')),width*.75,width,True))
        items.extend((tr(text),left,47,right-left,18,12 if align else 11,'ink' if align else 'secondary',400,dict(kind='single-line',right=align))
                     for text,left,right,align in metrics)
        return [(item[0],item[1]+AGENT_FRAME_INSET,*item[2:]) for item in items]
    @staticmethod
    def agent_summary_height(items):return max(AGENT_SUMMARY_HEIGHT,max((item[2]+item[4] for item in items),default=0))
    def detail_items(self):
        self._sub_summary_heights=[];self._sub_expanded_height=0
        d=self.detail_data();w=self.detail_width;items=[];y=0
        if not d:
            if self.has_subagents():items.append((tr('모델별 기록 확인 중'),0,0,w,20,11,'secondary',400,{}))
            return items
        cost_column=self.agent_cost_column(w)
        for index,group in enumerate(self.detail_groups()):
            key=group['key'];expanded=key in self.expanded_groups;start=y
            if index:items.append(('',0,y-8,w,1,11,'ink',400,dict(kind='agent-separator')))
            section_index=len(items)
            agent=(group.get('home'),group.get('id'));mark=agent_mark(agent[1],self.appearance)
            summary=self.agent_summary_items(group,group['label'],mark,expanded,w,cost_column=cost_column)
            summary_height=self.agent_summary_height(summary)
            items.append(('',0,y,w,summary_height,11,'ink',400,dict(kind='agent-update',owner=agent,color=mark.color)))
            self._sub_summary_heights.append(summary_height)
            for item in summary:
                items.append((*item[:2],item[2]+y,*item[3:]))
            meta=self.agent_model_tooltip(group)
            y+=summary_height
            exact=money(group.get('cost'))
            tip=group['label']+'\n'+meta+'\n'+exact
            if not group.get('model_alert_confirmed') and group.get('response_model'):tip+='\n'+tr('응답 모델')+': '+group['response_model']
            items.append(('',0,start,w,y-start,11,'ink',400,dict(action='group-'+key,actionLabel=group['label']+' '+meta+' '+exact,expanded=expanded,tooltip=tip)))
            if expanded:
                details=self.group_details(group)
                for item in details:items.append((*item[:2],item[2]+y,*item[3:]))
                detail_height=max((item[2]+item[4] for item in details),default=0)+8
                self._sub_expanded_height+=detail_height;y+=detail_height
                items.insert(section_index,('',0,start,w,y-start,11,'border',400,dict(kind='agent-frame')))
            y+=SUBAGENT_GAP
        return items
    def headline_metrics(self):
        """One row shared by painting, hit targets and layout verification."""
        data=self.data or {};cache=percent(data.get('cache_rate'))
        speed=data.get('latest',{}).get('output_speed')
        items=[dict(id='cache',label='캐시',x=16,width=100,
                    number=cache.removesuffix('%'),unit='%' if cache.endswith('%') else '',size=30,unit_size=16,
                    color='warning' if data.get('latest',{}).get('cache_warning') or data.get('cache_misses',{}).get('current') else 'cached_text'),
               dict(id='speed',label='평균 출력 속도',x=126,width=128,
                    number='—' if speed is None else '<0.1' if 0<speed<.1 else f'{speed:,.1f}',
                    unit='tps' if speed is not None else '',size=24,unit_size=10,color='ink'),
               dict(id='cost',label='비용',x=264,width=100,
                    number=money(data.get('latest_cost')),unit='',size=24,unit_size=10,color='ink')]
        for item in items:
            def width():
                return QFontMetrics(font(self.appearance.family,item['size'],600)).horizontalAdvance(item['number'])+(4+QFontMetrics(font(self.appearance.family,item['unit_size'],500)).horizontalAdvance(item['unit']) if item['unit'] else 0)
            while width()>item['width'] and item['size']>14:item['size']-=1
            if width()>item['width'] and item['id']=='speed':item['number']=f'{speed:.1e}'
            item['text_width']=width()
            item['label_size']=12
            item['label_width']=104 if item['id']=='speed' and self.speed_alert().get('active') else item['width']
            while QFontMetrics(font(self.appearance.family,item['label_size'])).horizontalAdvance(tr(item['label']))>item['label_width'] and item['label_size']>10:item['label_size']-=1
        return items

    def paint(self,p):
        p.save();p.scale(self.appearance.scale,self.appearance.scale);d=Drawing(p,self);width=WIDTH+self.detail_extent;height=self.base_height()
        surface=QColor(d.colors['surface']);surface.setAlphaF(self.opacity/100);p.setPen(QPen(d.colors['border'],1));p.setBrush(surface);p.drawRoundedRect(QRectF(.5,.5,width-1,height-1),16,16)
        p.save();p.translate(self.monitor_x,0);data=self.data or {};layout=self.layout()
        from .overlay_monitor import paint,header_layout
        header=header_layout(self)
        d.text(Verbatim(self.display_title()),MARGIN,layout['header'],header['title_width'],24,14,weight=600,elide=True)
        paint(d,self)
        p.restore();p.restore()

class SessionOverlay(QuickHost):
    WIDTH,HEIGHT=OverlayContent.WIDTH,OverlayContent.HEIGHT
    def __init__(self):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint|Qt.WindowDoesNotAcceptFocus|Qt.WindowTransparentForInput)
        self.setWindowTitle('Cache Monitor - 세션 오버레이');self.setAttribute(Qt.WA_TranslucentBackground);self.setAttribute(Qt.WA_ShowWithoutActivating);self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.content_model=OverlayContent();self.set_scene(self.content_model,'OverlayScene.qml',transparent=True,deferred=True,shared=True);self.resize(self.panel_width(),self.panel_height())
        self.content_model.changed.connect(lambda:self.setAccessibleName(self.content_model.state.get('accessible','')))
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
        self.content_model.set_content(*args,**kwargs);self.resize(self.panel_width(),self.panel_height());self.setAccessibleName(self.content_model.state['accessible'])
    def closeEvent(self,event):
        self.content_model.clear_row_glows();self.release_scene();super().closeEvent(event)
    def showEvent(self,event):super().showEvent(event);self.content_model.put(overlayVisible=True)
    def hideEvent(self,event):
        self.content_model.put(overlayVisible=False);self.content_model.clear_row_glows();super().hideEvent(event)
