"""Fixed logical-pixel monitor and expandable subagent cost detail."""
import json
import math
import re
import time
from datetime import datetime
from PySide6.QtCore import Qt, QRectF, QPointF, QObject, Property, Slot, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QBrush
from .presentation import Node
from .quick_runtime import QuickHost
from .overlay_appearance import default_appearance
from .ui_details import recorded, observed_transport, model_comparison
from .token_colors import ui_palette
from .i18n import tr, Verbatim
from .charts import value_text
from .overlay_monitor import WIDTH, MARGIN, SECTION_WIDTH, TITLE_WIDTH, INLINE_CLOSE_X
from .analysis_delivery import ANALYSIS_DELAY_SECONDS
from .overlay_data import main_usage, RECENT_CALLS

LABELS = dict(cached='캐시 읽기', uncached='일반 입력', written='캐시 쓰기', reasoning='추론', output='추론 외', unknown='미분류')
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
    for divisor, suffix in ((1e9, 'B'), (1e6, 'M'), (1e3, 'K')):
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
    return {key:QColor(value) for key,value in p.items()}


class Drawing:
    def __init__(self,painter,content):
        self.p=painter;self.model=content;self.colors=palette(content.appearance);painter.setRenderHint(QPainter.Antialiasing)
    def text(self,value,x,y,w,h=16,size=11,color='ink',weight=400,right=False,elide=False,center=False,wrap=False):
        literal=isinstance(value,Verbatim);value=tr(str(value))
        p=self.p;p.setFont(font(self.model.appearance.family,size,weight));p.setPen(self.colors.get(color,color))
        if elide:value=QFontMetrics(p.font()).elidedText(str(value),Qt.ElideRight,round(w))
        alignment=Qt.AlignHCenter if center else Qt.AlignRight if right else Qt.AlignLeft
        p.drawText(QRectF(x,y,w,h),alignment|Qt.AlignVCenter|(Qt.TextWordWrap if wrap else 0),Verbatim(value) if literal else str(value))
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
            if style.get('kind')=='cost-bar':
                d.rect(x,y,w,h,'track',h/2);offset=0
                total=style.get('total',sum(part['value'] for part in style['parts']))
                for part in style['parts']:
                    length=min(w-offset,w*part['value']/total) if total else 0
                    if length:d.rect(x+offset,y,length,h,part['color'],pattern=part['color']=='unknown')
                    offset+=length
            elif style.get('kind')=='tokens':d.tokens(x,y,w,title=value)
            elif style.get('kind')=='metric':d.metric(value,x,y,w,h,size,color=color)
            elif isinstance(value,list):
                for i,line in enumerate(value):d.text(line,x,y+i*18,w,18,size,color,weight,right=style.get('right',False))
            else:d.text(value,x,y,w,h,size,color,weight,right=style.get('right',False),elide=style.get('kind')=='single-line')
            if style.get('dot'):d.dot(x-7,y+h/2,style['dot'],2.5)
            if style.get('square'):d.rect(x-9,y+h/2-2,4,4,style['square'])

class OverlayContent(Node):
    kind='plot';WIDTH=WIDTH;HEIGHT=542;BODY_SIZE=12
    def __init__(self):
        super().__init__();self.data=None;self.note='기록 확인 중';self.opacity=94;self.appearance=default_appearance(True);self.dark=True;self.compact=False
        self.detail_open=False;self.detail_inline=False;self.detail_width=208
        self.detail_group='sub';self.expanded_groups=set()
        self.selected_id=None;self.selected_snapshot=None;self.follow_latest=True;self.graph_pinned=False;self.quota_lines=('','')
        self.monitor_tab='history';self.composition_unit='usd';self.lower_offset=0;self.settings=None
        self._freshness_label=''
        self._agent_scope=None;self._agent_slots={}
        self.highlight_id=None;self._highlight_timer=QTimer(self);self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self.clear_highlight)
        self._body=DetailBody(self)
        self.put(overlayVisible=False,overlayScale=1.,overlayInk=self.appearance.ink,overlayAccent=palette(self.appearance)['accent'].name(),detailBodyHeight=0);self.sync_details()
    @Property(QObject,constant=True)
    def detailBody(self):return self._body
    @property
    def monitor_x(self):return 0
    @property
    def detail_extent(self):return 240 if self.detail_open and not self.detail_inline else 0
    @property
    def is_remote(self):return self.note.startswith('원격 작업')
    @property
    def checking_records(self):return self.note in ('기록 준비 중','기록 확인 중','세션 기록 확인 중','기록 수집 중 (잠정값)')
    def content_opacity(self):
        return .5 if self.checking_records or self.note in ('수집 오류','수집 지연','수집 시작 중','분석 지연') else 1.
    def notice_card(self):
        from .overlay_monitor import REMOTE_HEADING,REMOTE_DESCRIPTION
        if self.is_remote:return REMOTE_HEADING,REMOTE_DESCRIPTION
        if self.note=='기록 준비 중' and (not self.data or 'calls' not in self.data):
            return '기록 준비 중','세션 기록에서 호출별 사용량을 읽고 있습니다.'
        if not self.data and self.note in ('수집 오류','수집 지연','수집 시작 중'):
            return self.note, {'수집 오류':'사용량 수집을 확인하지 못했습니다. 문제 해결 화면에서 오류를 확인해 주세요.',
                               '수집 지연':'새 수집 결과가 도착하지 않았습니다. 마지막 수집 상태를 확인해 주세요.',
                               '수집 시작 중':'수집기 실행을 요청했습니다. 새 수집 결과를 기다리고 있습니다.'}[self.note]
        if self.note in ('기록 확인 중','세션 기록 확인 중','기록 수집 중 (잠정값)') and not (self.data or {}).get('_collection',{}).get('retained'):
            return '기록 준비 중','세션 기록에서 호출별 사용량을 읽고 있습니다.'
        if self.note=='호출 기록 없음' or not self.note and (self.data or {}).get('calls')==0:
            return '호출 기록 대기','이 세션의 첫 호출 기록을 기다리고 있습니다.'
        return None
    def freshness_text(self):return self._freshness_label
    @staticmethod
    def collection_age(data,note):
        if not data or note not in ('수집 지연','수집 오류'):return ''
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
        self.compact,self.detail_open,self.detail_inline=values;self.lower_offset=0;self.detail_width=SECTION_WIDTH if self.detail_inline else 208
        self.sync_details();self.update()
    def set_content(self,data,note='',dark=True,appearance=None):
        appearance=appearance or default_appearance(dark)
        checking=note in ('기록 확인 중','세션 기록 확인 중','기록 수집 중 (잠정값)')
        checking=checking or note=='기록 준비 중' and bool((data or {}).get('_collection',{}).get('loading'))
        if data and checking and not data.get('_collection',{}).get('retained'):data={key:data[key] for key in ('id','home','title') if key in data}
        if note.startswith('원격 작업') or note=='현재 세션 식별 불가':data=None
        def visible(value):
            return {k:v for k,v in (value or {}).items() if k!='_collection'} | {'_errors':(value or {}).get('_collection',{}).get('errors',[])}
        freshness=self.collection_age(data,note)
        if (visible(data),note,appearance,freshness)==(visible(self.data),self.note,self.appearance,self._freshness_label):
            self.data=data
            return
        old=(self.data or {}).get('id'),(self.data or {}).get('home');new=(data or {}).get('id'),(data or {}).get('home')
        old_call=self.call_id(self.rows()[-1]) if self.rows() else None
        if old!=new:
            self.selected_id=None;self.selected_snapshot=None;self.follow_latest=True;self.graph_pinned=False
            self.expanded_groups.clear()
        self.data=data;self.note=note;self.appearance=appearance;self.dark=appearance.dark
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
        if not self.rows():self.selected_id=None;self.selected_snapshot=None;self.follow_latest=True;self.graph_pinned=False
        if self.graph_pinned and not any(self.call_id(r)==self.selected_id for r in self.rows()):
            self.release_pin()
        if self.follow_latest and self.rows():self.selected_snapshot=self.rows()[-1];self.selected_id=self.call_id(self.selected_snapshot)
        elif self.selected_id:self.selected_snapshot=next((r for r in self.rows() if self.call_id(r)==self.selected_id),self.selected_snapshot)
        self.put(overlayScale=appearance.scale,overlayInk=appearance.ink,overlayAccent=palette(appearance)['accent'].name(),collecting=False);self.sync_details();self.refresh_accessibility();self.update()
    def clear_highlight(self):
        self.highlight_id=None;self._highlight_timer.stop();self.update()
    def rows(self):return (self.data or {}).get('recent',[])[-RECENT_CALLS:]
    def graph_colors(self,rows):
        from .token_colors import categorical
        data=self.data or {};scope=(data.get('home'),data.get('id'))
        if scope!=self._agent_scope:
            self._agent_scope=scope;self._agent_slots={scope:0}
        for row in rows:
            agent=(row.get('home'),row.get('sid'))
            if agent not in self._agent_slots:self._agent_slots[agent]=len(self._agent_slots)
        for row in self.detail_data().get('agents',()):
            agent=(row.get('home'),row.get('id'))
            if agent not in self._agent_slots:self._agent_slots[agent]=len(self._agent_slots)
        colors=ui_palette(self.appearance)
        series=categorical(self.appearance.accent,(colors['surface'],colors['overlay']),len(self._agent_slots))
        return {agent:series[index] for agent,index in self._agent_slots.items()}
    @staticmethod
    def call_id(row):return (row.get('home'),row.get('sid'),row.get('id') or row.get('key'))
    def select(self,key):
        previous=self.selected_snapshot if self.selected_id==key else None
        self.selected_id=key;self.selected_snapshot=next((r for r in self.rows() if self.call_id(r)==key or str(r.get('id'))==str(key)),previous)
        if self.selected_snapshot:self.selected_id=self.call_id(self.selected_snapshot)
        self.follow_latest=bool(self.rows() and self.selected_id==self.call_id(self.rows()[-1]))
    def pin_call(self,key,toggle=True):
        if toggle and self.graph_pinned and self.selected_id==key:self.release_pin()
        else:
            self.select(key);self.graph_pinned=True;self.follow_latest=False
        self.update()
    def release_pin(self):
        self.graph_pinned=False;self.follow_latest=True
        rows=self.rows();self.selected_snapshot=rows[-1] if rows else None
        self.selected_id=self.call_id(rows[-1]) if rows else None
    def selected(self):return next((r for r in self.rows() if self.call_id(r)==self.selected_id),self.selected_snapshot or {})
    def speed_alert(self):
        # Collection trouble is not evidence that the current session is slow.
        return (self.data or {}).get('speed_health', {}) if not self.note else {}
    def has_subagents(self):return bool((self.data or {}).get('descendants',0)>0 and not self.notice_card())
    def set_detail_group(self,mode):
        if mode not in ('model','sub') or mode==self.detail_group:return
        self.detail_group=mode;self.sync_details()
    def toggle_detail_group(self,key):
        if key not in {row['key'] for row in self.detail_groups()}:return
        if key in self.expanded_groups:self.expanded_groups.remove(key)
        else:self.expanded_groups.add(key)
        self.sync_details()
    def detail_data(self):
        return (self.data or {}).get('subagent_finance',{}) if self.has_subagents() else {}
    @staticmethod
    def detail_group_key(row,mode):
        identity=['model',row['model']] if mode=='model' else ['sub',row.get('home'),row['id']]
        return json.dumps(identity,ensure_ascii=False,separators=(',',':'))
    def detail_groups(self):
        data=self.detail_data();rows=[]
        for row in data.get('models' if self.detail_group=='model' else 'agents',()):
            key=self.detail_group_key(row,self.detail_group)
            label=row['model'] if self.detail_group=='model' else row.get('title') or row['id']
            rows.append(dict(row,key=key,label=label))
        def order(row):
            cost=-(row.get('cost') or 0)
            if self.detail_group=='model':return (cost,row['key'])
            stamp=row.get('latest_ts')
            stamp=stamp if type(stamp) in (int,float) and math.isfinite(stamp) else float('-inf')
            return (-stamp,cost,row['key'])
        return sorted(rows,key=order)
    def subagent_call_label(self,group):
        if not recorded(group.get('model')) and not recorded(group.get('effort')):return tr('미확인')
        model=group.get('model') or tr('미확인');effort=group.get('effort') or tr('미확인')
        state=group.get('model_state')
        match=('불일치' if state=='모델명 불일치' and group.get('model_mismatch') and group.get('response_model') and group['response_model']!=model else
               '일치' if state=='일치' and group.get('response_model')==model else '미확인')
        return f'{tr(model)}, {tr(effort)} ({tr(match)})'
    def main_model_tooltip(self):
        own=main_usage(self.data or {});label=self.subagent_call_label(own)
        if own.get('model_mismatch') and own.get('response_model'):
            label+=f"\n모델 불일치 (요청:{own.get('model')}, 응답:{own['response_model']})"
        return label
    def sync_details(self):
        items=self.detail_items() if self.detail_open else [];self._detail_items=items;height=max((r[2]+r[4] for r in items),default=16)
        actions=[]
        for item in items:
            style=item[8]
            text=''.join(item[0]) if isinstance(item[0],list) else item[0]
            if style.get('action'):
                actions.append(dict(id=style['action'],x=item[1],y=item[2],width=item[3],height=item[4],
                                    label=style.get('actionLabel',text),expanded=style.get('expanded',False),tooltip=style.get('tooltip','')))
        colors=palette(self.appearance)
        data=self.data or {};sub=self.detail_data();family=self.appearance.family
        widths=[max(28,QFontMetrics(font(family,11)).horizontalAdvance(tr(label))+14) for label in ('Agents','모델')]
        right=(MARGIN if self.detail_inline else 16)+self.detail_width;left=right-sum(widths)
        segments=[]
        for (mode,label),width in zip((('sub','Agents'),('model','모델')),widths):
            segments.append(dict(id=mode,label=tr(label),x=left,width=width,
                                 tooltip='최근 업데이트순' if mode=='sub' else '환산액순'));left+=width
        sub_partial=bool(sub.get('missing') or sub.get('coverage_gap'))
        sub_cost=sub.get('cost') if sub else data.get('child_cost')
        def exact(value):return tr('미산정') if value is None else '$0.00' if value==0 else '$'+f'{value:,.10f}'.rstrip('0').rstrip('.')
        def header_amount(value):
            width=(self.detail_width-12)/2;size=22;display=fitted_money(value,width,size,family)
            while size>10 and QFontMetrics(font(family,size,600)).horizontalAdvance(display)>width:size-=1
            return display,size
        sub_amount,sub_size=header_amount(sub_cost)
        count=str(data.get('descendants',0));count_size=22
        while count_size>10 and QFontMetrics(font(family,count_size,600)).horizontalAdvance(count)>(self.detail_width-12)/2:count_size-=1
        from .overlay_monitor import SESSION_Y,TITLE_Y,header_layout
        header=header_layout(self)
        self.put(detailBodyHeight=round(height*self.appearance.scale),detailWidth=self.detail_width,detailTitle='Sub Agents',detailHeadingY=self.layout().get('session',SESSION_Y),
                 checkingRecords=self.checking_records,loadingX=header['status_x'],loadingY=TITLE_Y+5,contentOpacity=self.content_opacity(),
                 overlayFamily=self.appearance.family,overlaySurface=colors['surface'].name(),overlayBorder=colors['border'].name(),
                 overlayWarning=colors['warning'].name(),overlayBand=colors['band'].name(),overlaySelection=colors['selection'].name(),overlaySecondary=colors['secondary'].name(),
                 detailHasSession=bool((self.data or {}).get('id')),detailSessionKey=str(((self.data or {}).get('home'),(self.data or {}).get('id'))),
                 detailGroup=self.detail_group,detailGroupSegments=segments,detailActions=actions,
                 detailAgentCount=count,detailAgentCountSize=count_size,detailSubCost=sub_amount,detailSubCostSize=sub_size,
                 detailSubExact=exact(sub_cost),detailSubPartial=sub_partial)
        if self.detail_open:self._body.update()
        self._body.setAccessibleName('\n'.join(''.join(r[0]) if isinstance(r[0],list) else r[0] for r in items))
    def monitor_links(self):
        from .overlay_monitor import links
        return links(self)

    def monitor_action(self,action):
        if action.startswith('tab-'):
            if self.graph_pinned:self.release_pin()
            self.monitor_tab='history';self.lower_offset=0
        elif action.startswith('unit-'):self.composition_unit=action[5:]
        if self.settings is not None:
            self.settings.setValue('overlay/compositionUnit',self.composition_unit)
        self.inspected_call=None;self.refresh_accessibility();self.update()

    def scroll_lower(self,delta):
        if self.compact:
            self.lower_offset=max(0,min(self.base_height(False)-self.base_height(),self.lower_offset+delta));self.update()

    def detail_links(self):
        return []

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
        # Current collection failures take precedence over historical gaps.
        return sorted(dict.fromkeys(result),key=lambda item:0 if item[1]=='error' else 1 if '지연' in item[0] else 2)
    def status_text(self):
        states=self.states();extra=len(states)-1+max(0,len((self.data or {}).get('_collection',{}).get('errors',[]))-1)
        label=states[0][0] if states else ''
        label={'원격 작업: 로컬 기록 없음':'원격 세션','현재 세션 식별 불가':'세션 확인 불가','수집 시작 중':'기록 준비 중'}.get(label,label)
        if label=='수집 지연' and (self.data or {}).get('_collection',{}).get('reason')=='analysis_delay':label='분석 지연'
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
        if self.note in ('수집 지연','수집 오류') and collection.get('last_confirmed_at'):
            values.append(tr('마지막 확인')+': '+timestamp(collection['last_confirmed_at']))
        return '\n'.join(dict.fromkeys(filter(None,values)))
    def session_scope(self):
        from .overlay_monitor import scope
        return scope(self.data or {})
    def notice(self):return (self.status_text(),self.states()[0][1]) if self.states() else ('','muted')
    def layout(self,reduced=None):
        if self.notice_card():
            from .overlay_monitor import notice_geometry
            return notice_geometry(self)
        reduced=self.compact if reduced is None else reduced
        from .overlay_monitor import geometry,header_status
        return geometry(self.data,reduced,has_status=bool(header_status(self)[0]))
    def base_height(self,reduced=None):return self.layout(reduced)['height']
    def monitor_height(self,reduced=False):return round(self.base_height(reduced)*self.appearance.scale)
    def panel_width(self):return round((WIDTH+self.detail_extent)*self.appearance.scale)
    def panel_height(self):return round(self.base_height()*self.appearance.scale)
    def amount(self,value):return amount(value)
    def money(self,value,*args):return money(value)
    def cost_disclosure(self):
        d=main_usage(self.data or {});values=[]
        if d.get('maintenance_calls'):
            values.append(f"캐시 유지 {d['maintenance_calls']}회 포함, {money(d.get('maintenance_cost'),False)}, 미확인 {d.get('maintenance_missing',0)}회")
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
                '최근 20호출: 비용, 캐시, 출력 속도, 추론 토큰',
                '세션 비용 '+money(own.get('cost'),False),
                '세션 캐시 적중률 '+percent(own.get('token_composition',{}).get('cache_hit_rate')),
                '평균 출력 속도 '+value_text(own.get('output_speed_summary',{}).get('value'),'output_speed').replace('tok/s','tps')]
        currency=self.composition_unit=='usd'
        comp=own.get('cost_composition' if currency else 'token_composition',{})
        fmt=money if currency else lambda n:amount(n,True)+'토큰'
        for section,title in (('input','입력'),('output','출력')):
            values.append(title+' '+fmt(comp.get(section+'_total')))
            values.extend(p['label']+' '+(fmt(p['tokens']) if p.get('known',True) else '미확인') for p in comp.get(section+'_parts',[]))
        misses=d.get('cache_misses',{})
        if misses.get('count'):values.append(f"{'자체 ' if d.get('descendants') else ''}캐시 미적중 {misses['count']}회, 해당 입력 {amount(misses.get('input'),True)}토큰")
        values.append(self.freshness_text())
        self.setAccessibleName(Verbatim(values[0]+'\n'+tr('\n'.join(filter(None,values[1:])))))
    def detail_items(self):
        d=self.detail_data();w=self.detail_width;items=[];y=0
        def text(value,x=0,width=None,size=11,color='ink',weight=400,height=18,at=None,right=False,kind=None):
            nonlocal y
            yy=y if at is None else at;limit=width if width is not None else w;value=tr(str(value))
            if kind not in ('metric','single-line'):
                metrics=QFontMetrics(font(self.appearance.family,size,weight));lines=[];line=''
                for char in value:
                    if line and metrics.horizontalAdvance(line+char)>limit:lines.append(line);line=''
                    line+=char
                lines.append(line)
                if len(lines)>1:value=lines;height=len(lines)*18
            items.append((value,x,yy,limit,height,size,color,weight,dict(right=right,kind=kind)))
            if at is None:y+=height
            return height
        def exact_money(value):
            if value is None:return tr('미산정')
            return '$0.00' if value==0 else '$'+f'{value:,.10f}'.rstrip('0').rstrip('.')
        def categories(group):
            nonlocal y
            cost=group.get('cost_composition',{});composition=group.get('token_composition',{})
            costs={part['key']:part for side in ('input','output') for part in cost.get(side+'_parts',())}
            quantities={part['key']:part for side in ('input','output') for part in composition.get(side+'_parts',())}
            for key,label in (('cached','캐시 읽기'),('written','캐시 쓰기'),('uncached','일반 입력'),
                              ('reasoning','추론'),('output','추론 외'),
                              ('input_unknown','입력 미분류'),('output_unknown','출력 미분류')):
                quantity=quantities.get(key);part=costs.get(key)
                count=quantity['tokens'] if quantity and quantity.get('known',True) else None
                price=part['tokens'] if part and part.get('known',True) else 0. if cost.get('known') else None
                if count==0 and price==0:continue
                if key.endswith('unknown') and not quantity and not part:continue
                count_text=' ('+(amount(count) if count is not None else tr('미확인'))+')'
                metrics=QFontMetrics(font(self.appearance.family,11))
                value_width=w*.62
                price_text=fitted_money(price,value_width-metrics.horizontalAdvance(count_text),11,self.appearance.family)
                text(label,x=10,width=w-value_width-16,color='secondary',size=10,at=y,kind='single-line')
                items[-1][8]['square']=key if key in ORDER else 'unknown'
                text(price_text+count_text,x=w-value_width,width=value_width,right=True,size=11,at=y,kind='single-line')
                y+=25
        if not d:
            if self.has_subagents():text('모델별 기록 확인 중',color='secondary')
            return items
        groups=self.detail_groups()
        agent_colors=self.graph_colors(self.rows()) if self.detail_group=='sub' else {}
        if not groups:text('호출 기록 없음' if d.get('calls')==0 else '모델별 기록 확인 중',color='secondary')
        for group in groups:
            key=group['key'];expanded=key in self.expanded_groups;start=y
            partial=group.get('priced',0)<group.get('calls',0) or group.get('coverage_gap') or (self.detail_group=='model' and d.get('coverage_gap'))
            value=group.get('cost');total=d.get('cost')
            share=percent(value/total*100) if value is not None and total else ('0.0%' if value==0 and total==0 else '—')
            display=fitted_money(value,w*.48,12,self.appearance.family)
            price_width=min(w*.48,QFontMetrics(font(self.appearance.family,12,600)).horizontalAdvance(display)+2)
            name_x=12 if self.detail_group=='sub' else 0
            name_height=text(group['label'],x=name_x,width=w-price_width-8-name_x,weight=600,size=12,at=y,kind='single-line')
            if self.detail_group=='sub':items[-1][8]['dot']=agent_colors[(group.get('home'),group.get('id'))]
            text(display,x=w-price_width,width=price_width,size=12,weight=600,at=y,kind='metric');y+=name_height
            if display!=money(value,False):text(exact_money(value),color='secondary',size=10)
            meta=self.subagent_call_label(group) if self.detail_group=='sub' else ''
            if partial:meta+=(' / ' if meta else '')+tr('확인분')
            share_text=share+(' ⌄' if expanded else ' ›')
            share_width=QFontMetrics(font(self.appearance.family,11)).horizontalAdvance(share_text)+2
            meta_height=text(meta,width=w-share_width-8,color='error' if group.get('model_mismatch') else 'secondary',size=10,at=y)
            text(share_text,x=w-share_width,width=share_width,color='secondary',right=True,at=y);y+=max(22,meta_height)
            cost=group.get('cost_composition',{})
            parts=[dict(value=max(0,part['tokens']),color=part['key'] if part['key'] in ORDER else 'unknown')
                   for side in ('input','output') for part in cost.get(side+'_parts',()) if part.get('tokens')]
            text('',height=5,kind='cost-bar');items[-1][8].update(parts=parts,total=total or 0);y+=10
            exact=exact_money(value)+(' '+tr('확인분') if partial else '')
            tip=group['label']+'\n'+(meta+'\n' if meta else '')+exact
            if self.detail_group=='sub' and group.get('response_model'):tip+='\n'+tr('응답 모델')+': '+group['response_model']
            items.append(('',0,start,w,y-start,11,'ink',400,dict(action='group-'+key,actionLabel=group['label']+' '+meta+' '+exact,expanded=expanded,tooltip=tip)))
            if expanded:
                from .overlay_monitor import speed
                speed_summary=group.get('output_speed_summary') or {}
                speed_text=speed(speed_summary.get('value'))
                if speed_summary.get('missing') and speed_summary.get('n'):speed_text+=' '+tr('확인분')
                value_width=QFontMetrics(font(self.appearance.family,11)).horizontalAdvance(speed_text)+2
                value_width=min(w*.62,value_width)
                text('평균 출력 속도',width=w-value_width-8,color='secondary',size=10,at=y,kind='single-line')
                text(speed_text,x=w-value_width,width=value_width,right=True,size=11,at=y,kind='single-line')
                y+=26
                categories(group)
                if group.get('priced',0)<group.get('calls',0):
                    text(f"산정 {group.get('priced',0):,} / {group.get('calls',0):,}호출",color='secondary',size=10)
            y+=10
        evidence=[]
        if d.get('missing'):evidence.append(f"산정 {d.get('priced',0):,} / {d.get('calls',0):,}호출")
        if d.get('assumed'):evidence.append(f"Standard 가정 {d['assumed']}호출")
        if d.get('coverage_gap'):evidence.append('이전 기록 누락')
        for line in evidence:text(line,color='secondary',size=10)
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
                    number=fitted_money(data.get('latest_cost'),100,24,self.appearance.family),unit='',size=24,unit_size=10,color='ink')]
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
        if self.detail_open:
            if not self.detail_inline:
                d.line(WIDTH,self.layout().get('session',52),WIDTH,height-12)
            else:pass
        p.save();p.translate(self.monitor_x,0);data=self.data or {};layout=self.layout()
        from .overlay_monitor import paint,header_layout,header_status
        header=header_layout(self);status,color=header_status(self)
        d.text(Verbatim(self.display_title()),MARGIN,layout['header'],header['title_width'],24,14,weight=600,elide=True)
        if status:
            inset=20 if self.checking_records else 0
            d.text(status,header['status_x']+inset,layout['header'],max(0,header['status_width']-inset),24,10,color,elide=True)
        p.setOpacity(self.content_opacity())
        if not self.detail_inline:paint(d,self)
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
        self.release_scene();super().closeEvent(event)
    def showEvent(self,event):super().showEvent(event);self.content_model.put(overlayVisible=True)
    def hideEvent(self,event):self.content_model.put(overlayVisible=False);super().hideEvent(event)
