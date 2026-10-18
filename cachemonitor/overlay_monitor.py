"""Bounded session monitor drawing and matching native hit regions."""
import math
from .charts import dynamic_bounds
from .overlay_navigation import navigation_target
from .overlay_data import main_usage

WIDTH=350
MARGIN=12
RIGHT=WIDTH-MARGIN
SECTION_WIDTH=WIDTH-2*MARGIN
BODY_LEFT=MARGIN+8
BODY_WIDTH=RIGHT-BODY_LEFT
COLUMN_GAP=16
COLUMN_WIDTH=(BODY_WIDTH-COLUMN_GAP)/2
OUTPUT_LEFT=BODY_LEFT+COLUMN_WIDTH+COLUMN_GAP
METRIC_GAP=12
METRIC_WIDTH=(BODY_WIDTH-2*METRIC_GAP)/3
METRIC_COLUMNS=tuple(BODY_LEFT+i*(METRIC_WIDTH+METRIC_GAP) for i in range(3))
ACTIONS_X=RIGHT-74
INLINE_CLOSE_X=ACTIONS_X-28
INLINE_HEADER_WIDTH=INLINE_CLOSE_X-154
HEADER_X=MARGIN-4
HEADER_WIDTH=ACTIONS_X-HEADER_X
TITLE_WIDTH=HEADER_WIDTH-8
POPUP_X=RIGHT-160
# Logical coordinates shared by painting, native masks and scroll limits.
TITLE_Y=16
TITLE_HEIGHT=24
SECTION_GAP=12
GROUP_GAP=16
SESSION_Y=TITLE_Y+TITLE_HEIGHT+SECTION_GAP
CARD_Y=SESSION_Y+20+GROUP_GAP
CARD_HEIGHT=64
GRAPH_ROW=52
GRAPH_COUNT=4
REMOTE_TITLE='원격 세션'
REMOTE_HEADING='사용량 표시 미지원'
REMOTE_DESCRIPTION='이 오버레이는 로컬 세션만 지원하며, 원격 기기의 사용량은 표시하지 않습니다.'


def notice_geometry(m):
    height=TITLE_Y+TITLE_HEIGHT+20
    return dict(header=TITLE_Y,context=SESSION_Y,body=SESSION_Y,body_height=0,
                height=height,full_height=height)


def partial_text(data):
    return '캐시 확인분' if main_usage(data).get('token_composition',{}).get('cache_hit_partial') else ''


def miss_text(data):
    from .overlay_view import amount
    from .i18n import formatted
    misses=data.get('cache_misses',{})
    if not misses.get('count'):return ''
    tokens=amount(misses.get('input'))
    if tokens[-1:] in ('K','M','B'):tokens=tokens[:-1]+' '+tokens[-1]
    return formatted('캐시 미적중 총 {count}회 ({tokens})',count=misses['count'],tokens=tokens)


def header_status(m):
    alert=m.speed_alert().get('active')
    text=m.status_text() or ('출력 속도 저하' if alert else '')
    return text,'error' if m.states() and m.states()[0][1]=='error' else 'warning' if alert else 'secondary'


def header_layout(m):
    from PySide6.QtGui import QFontMetrics
    from .overlay_view import font
    from .i18n import tr
    available=TITLE_WIDTH+m.detail_extent
    status=header_status(m)[0]
    status_width=min(available*.48,QFontMetrics(font(m.appearance.family,10)).horizontalAdvance(tr(status))+2+(20 if m.checking_records else 0)) if status else 0
    title_width=min(available-status_width-(8 if status else 0),QFontMetrics(font(m.appearance.family,14,600)).horizontalAdvance(m.display_title())+2)
    return dict(title_width=max(0,title_width),status_x=MARGIN+title_width+8,status_width=status_width)


def status_link(m):
    text=header_status(m)[0]
    if not text:return []
    g=header_layout(m);target=navigation_target(m.data or {},'collection') if any(word in m.note for word in ('오류','지연')) else None
    return [dict(id='collection' if target else 'tip-status',x=g['status_x'],y=TITLE_Y,width=g['status_width'],height=TITLE_HEIGHT,
                 tooltip=m.status_detail(),accessible=m.status_detail(),interaction='navigate' if target else 'tooltip',**({'target':target} if target else {}))]


def geometry(data, reduced=False, has_status=False):
    data=data or {}
    shift=0
    composition=CARD_Y+shift+CARD_HEIGHT+GROUP_GAP
    bar=composition+22;rows=bar+14
    populations=[main_usage(data).get(key,{}) for key in ('token_composition','cost_composition')]
    counts={section:max((len(c.get(section+'_parts',[])) for c in populations),default=0)
            for section in ('input','output')}
    unit=SESSION_Y+shift
    end=max(rows+max(counts.values())*18,bar+6)
    partial=None
    divider=end+GROUP_GAP;tab=divider+GROUP_GAP;context=tab+20+GROUP_GAP
    recent=context
    content_end=recent+GRAPH_ROW*GRAPH_COUNT-8
    full_height=content_end+16
    height=min(432,full_height) if reduced else full_height
    return dict(header=TITLE_Y,session=SESSION_Y+shift,cache=CARD_Y+shift,tokens=composition,bar=bar,rows=rows,
                unit=unit,partial=partial,miss=tab if miss_text(data) else None,divider=divider,tab=tab,context=context,
                model=recent,meta=recent+24,recent=recent,result=recent+56,
                result_value=recent+78,duration=recent+112,call_tokens=recent+142,
                call_bar=recent+166,call_parts=recent+180,content_end=content_end,
                body_end=height-16,status=TITLE_Y if has_status else None,height=height,full_height=full_height)


def model_text(row):
    from .ui_details import model_comparison
    comparison=model_comparison(row)
    model=row.get('requested_model') or row.get('model') or '미확인'
    if row.get('model_setting') and not row.get('requested_model'):model='설정: '+model
    if comparison.startswith('모델 일치'):return model,' (일치)',False
    if comparison:return model,f" (응답: {row.get('response_model') or '미확인'})",True
    return model,' (확인 불가)',False


def graph_value(row,key):
    value=row.get(key)
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:return None
    if key=='reasoning':
        if type(value) is not int or row.get('output_conflict'):return None
        total=row.get('output')
        if total is not None and (type(total) is not int or value>total):return None
    if key=='cache_rate' and (row.get('input_conflict') or value>100):return None
    if key=='output_speed' and row.get('output_conflict'):return None
    return value


def speed(value):
    return '—' if value is None else f'{value:.1f} tps'


def session_call_count(data):
    data=main_usage(data)
    calls=f"{data.get('calls',0):,}"
    return f"{data.get('priced',0):,}/{calls}" if data.get('missing') else calls


def scope_label(data):
    return 'Main Agent'


def scope(data):
    return f"{scope_label(data)} : {session_call_count(data)}호출"


def agent_label(data,row):
    if (row.get('home'),row.get('sid'))==(data.get('home'),data.get('id')):return 'Main Agent'
    return row.get('source_title') or row.get('sid') or 'Sub Agent'


def toggle_segments(m,kind):
    """Share compact, right-aligned label geometry with native hit regions."""
    from PySide6.QtGui import QFontMetrics
    from .overlay_view import font
    from .i18n import tr,formatted
    if kind!='unit':return []
    options=(('usd','$'),('tokens','개수'))
    metrics=QFontMetrics(font(m.appearance.family,11))
    widths=[max(28,metrics.horizontalAdvance(tr(label))+14) for _,label in options]
    x=RIGHT-sum(widths);segments=[]
    for (key,label),width in zip(options,widths):
        segments.append((x,width,key,label));x+=width
    return segments


def main_model_rect(m):
    from PySide6.QtGui import QFontMetrics
    from .overlay_view import font
    from .i18n import tr
    label_width=QFontMetrics(font(m.appearance.family,12,600)).horizontalAdvance(tr(scope_label(m.data or {})))
    x=MARGIN+12+label_width+8
    return x,max(0,toggle_segments(m,'unit')[0][0]-8-x)


def paint(d,m):
    notice=m.notice_card()
    if notice:
        return
    from .overlay_view import amount,money,percent,font
    data=m.data or {};own=main_usage(data);latest=data.get('latest') or {};c=own.get('token_composition',{});g=m.layout()
    from PySide6.QtGui import QFontMetrics
    from .i18n import tr,formatted
    label=scope_label(data)
    label_width=QFontMetrics(font(m.appearance.family,12,600)).horizontalAdvance(tr(label))
    color=m.graph_colors(m.rows())[(data.get('home'),data.get('id'))]
    d.dot(MARGIN+3,g['session']+10,color,3)
    d.text(label,MARGIN+12,g['session'],label_width,20,12,weight=600)
    model_x,model_width=main_model_rect(m)
    identity=m.subagent_call_label(own)
    d.text(identity,model_x,g['session'],model_width,20,10,
           'error' if own.get('model_mismatch') else 'secondary',elide=True)
    values=(money(own.get('cost')),percent(c.get('cache_hit_rate')),speed(own.get('output_speed_summary',{}).get('value')))
    d.rect(MARGIN,g['cache'],SECTION_WIDTH,CARD_HEIGHT,'band',8)
    for x,label,value in zip(METRIC_COLUMNS,('비용','캐시 적중률','평균 출력 속도'),values):
        d.text(label,x,g['cache']+8,METRIC_WIDTH,18,11,'secondary')
        d.text(value,x,g['cache']+30,METRIC_WIDTH,26,20 if x in METRIC_COLUMNS[:2] else 18,color='cached_text' if x==METRIC_COLUMNS[1] else 'ink',weight=600,elide=True)
    currency=m.composition_unit=='usd'
    comp=own.get('cost_composition',{}) if currency else c
    segments=toggle_segments(m,'unit')
    d.rect(segments[0][0],g['unit'],RIGHT-segments[0][0],20,'band',10)
    for x,w,key,label in segments:
        if m.composition_unit==key:d.rect(x+1,g['unit']+1,w-2,18,'selection',9)
        d.text(label,x+7,g['unit'],w-7,20,11)
    fmt=money if currency else amount
    for x,section,title in ((BODY_LEFT,'input','입력'),(OUTPUT_LEFT,'output','출력')):
        d.text(title,x,g['tokens'],64,18,11,weight=600)
        d.text(fmt(comp.get(section+'_total')),x+64,g['tokens'],COLUMN_WIDTH-64,18,11,right=True)
        d.rect(x,g['bar'],COLUMN_WIDTH,6,'track',3);offset=0
        for i,part in enumerate(comp.get(section+'_parts',[])):
            color=part['key'] if not part['key'].endswith('unknown') else 'unknown'
            length=COLUMN_WIDTH*part['share'];d.rect(x+offset,g['bar'],length,6,color);offset+=length
            d.rect(x,g['rows']+i*18+5,4,4,color)
            d.text('미분류' if color=='unknown' else part['label'],x+9,g['rows']+i*18,80,18,11,'secondary')
            d.text(fmt(part['tokens']) if part.get('known',True) else '—',x+89,g['rows']+i*18,COLUMN_WIDTH-89,18,11,right=True)
    d.line(MARGIN,g['divider'],RIGHT,g['divider'],'border',1)
    d.text('Trend',MARGIN,g['tab'],160,20,12,weight=600)
    misses=miss_text(data)
    miss_width=min(SECTION_WIDTH*.58,QFontMetrics(font(m.appearance.family,10)).horizontalAdvance(tr(misses))) if misses else 0
    if misses:d.text(misses,RIGHT-miss_width,g['tab'],miss_width,20,10,'secondary',right=True,elide=True)
    active=data.get('active_requests',int(data.get('request',{}).get('state')=='진행'))
    if active:
        x=MARGIN+QFontMetrics(font(m.appearance.family,12,600)).horizontalAdvance('Trend')+10
        d.text(formatted('진행 중: {count}개',count=active),x,g['tab'],max(0,RIGHT-miss_width-8-x),20,10,'secondary',elide=True)
    d.p.save();d.p.setClipRect(BODY_LEFT,g['context'],BODY_WIDTH,g['body_end']-g['context'])
    d.p.translate(0,-m.lower_offset)
    rows=m.rows();agent_colors=m.graph_colors(rows)
    inspected=next((r for r in rows if m.call_id(r)==getattr(m,'inspected_call',None)),None)
    displayed=m.selected() if m.graph_pinned else inspected or latest
    from PySide6.QtGui import QColor,QPainterPath
    step=BODY_WIDTH/max(1,len(rows))
    if m.graph_pinned:inspected=None
    for lane,(key,label,fmt) in enumerate((('cost','비용',money),('cache_rate','캐시',percent),
            ('output_speed','출력 속도',speed),
            ('reasoning','추론',lambda n:'—' if n is None else formatted('{tokens} 토큰',tokens=amount(n))))):
        y=g['recent']+lane*GRAPH_ROW;low,high=dynamic_bounds(graph_value(r,key) for r in rows)
        d.text(label,BODY_LEFT,y,BODY_WIDTH-144,16,10,'secondary');d.text((formatted('{number}번: ',number=inspected.get('ordinal','')) if inspected else '')+fmt(graph_value(displayed,key)),RIGHT-144,y,144,16,11,right=True)
        fills=[];previous={};bottom=y+44
        for i,row in enumerate(rows):
            agent=(row.get('home'),row.get('sid'))
            val=graph_value(row,key);x=BODY_LEFT+(i+.5)*step
            if val is None:previous.pop(agent,None);continue
            yy=y+20+24*(high-val)/(high-low)
            condition=tuple(row.get(k) for k in ('model','effort','mode','transport'))
            prior=previous.get(agent) if agent[1] else None
            if prior and (key!='output_speed' or condition==prior[1]):path=prior[0]
            else:
                path=QPainterPath();path.moveTo(x,bottom)
                fills.append((path,agent_colors[agent]))
            path.lineTo(x,yy);previous[agent]=(path,condition)
        for path,color in fills:
            path.lineTo(path.currentPosition().x(),bottom);path.closeSubpath()
            tint=QColor(d.colors.get(color,color));tint.setAlphaF(tint.alphaF()*.14)
            d.p.fillPath(path,tint)
        previous={}
        for i,row in enumerate(rows):
            agent=(row.get('home'),row.get('sid'))
            color=agent_colors[agent]
            val=graph_value(row,key);x=BODY_LEFT+(i+.5)*step
            if val is None:previous.pop(agent,None);continue
            yy=y+20+24*(high-val)/(high-low)
            condition=tuple(row.get(k) for k in ('model','effort','mode','transport'))
            prior=previous.get(agent) if agent[1] else None
            if prior and (key!='output_speed' or condition==prior[2]):d.line(prior[0],prior[1],x,yy,color,1)
            active=m.call_id(row)==m.call_id(displayed)
            if active:d.line(x,y+19,x,y+45,'secondary',.6)
            radius=3 if active else 2
            if row.get('cache_warning'):d.dot(x,yy,'warning',radius+1.5)
            d.dot(x,yy,color,radius)
            previous[agent]=(x,yy,condition)
    d.p.restore()



def action_links(m):
    if m.notice_card():return []
    data=m.data or {};result=[];g=m.layout()
    def add(key,x,y,w,h,**kw):result.append(dict(id=key,x=x,y=y,width=w,height=h,accessible=kw.pop('accessible',key),targetHint=key.startswith('call-') or key=='latest-context',**kw))
    for x,w,key,label in toggle_segments(m,'unit'):
        action='unit-'+key
        add(action,x,g['unit']-2,w,24,action=action,accessible='개수: 토큰 수로 보기' if key=='tokens' else '달러 환산액으로 보기')
    from PySide6.QtGui import QFontMetrics
    from .overlay_view import font
    from .i18n import tr,formatted
    def heading(key,label,y,target,interaction='navigate'):
        if target:
            width=QFontMetrics(font(m.appearance.family,12,600)).horizontalAdvance(tr(label))
            add(key,BODY_LEFT if key=='collection' else MARGIN+12,y,width,20,target=target,interaction=interaction,accessible=label)
    heading('session',scope_label(data),g['session'],navigation_target(data,'session'))

    if m.compact:add('scroll',BODY_LEFT,g['context'],BODY_WIDTH,g['body_end']-g['context'],action='scroll',accessible='하단 스크롤 (위아래 방향키)')
    rows=m.rows()
    for i,row in enumerate(rows):
        step=BODY_WIDTH/max(1,len(rows));x=BODY_LEFT+i*step
        y=max(g['context'],g['recent']-m.lower_offset)
        bottom=min(g['recent']+GRAPH_ROW*GRAPH_COUNT-8-m.lower_offset,g['body_end'])
        if bottom>y:
            from .i18n import Verbatim
            add('call-'+str(i),x,y,step,bottom-y,target=navigation_target(data,'call',call=row),
                interaction='select',selected=m.call_id(row)==m.selected_id,
                accessible=Verbatim(agent_label(data,row)+' / '+str(row.get('ordinal',i+1))+' 호출 선택'))
    return result




def links(m):
    if m.notice_card() or m.detail_inline:return status_link(m)
    from .tooltips import TEXT
    from .ui_details import model_comparison
    data=m.data or {};row=data.get('latest') or {};result=action_links(m)
    def add(key,x,y,w,h,tip):
        if tip:result.append(dict(id='tip-'+key,x=x,y=y,width=w,height=h,tooltip=tip,accessible=tip,interaction='tooltip'))
    g=m.layout()
    for x,key in zip(METRIC_COLUMNS,('cost','cache_total','speed_total')):add(key,x,g['cache'],METRIC_WIDTH,CARD_HEIGHT,TEXT[key])
    for item in result:
        if item.get('action') in ('unit-tokens','unit-usd'):
            item['tooltip']={'unit-tokens':'토큰 개수로 보기','unit-usd':'토큰 USD로 보기'}[item['action']]
    comp=main_usage(data).get('cost_composition' if m.composition_unit=='usd' else 'token_composition',{})
    for x,section,title in ((BODY_LEFT,'input','입력'),(OUTPUT_LEFT,'output','출력')):
        add(section,x,g['tokens'],COLUMN_WIDTH,20,TEXT[title])
        for i,part in enumerate(comp.get(section+'_parts',[])):
            add(section+str(i),x,g['rows']+i*18,COLUMN_WIDTH,18,TEXT.get(part['label'],''))
    if g.get('miss') is not None:add('miss',RIGHT-SECTION_WIDTH*.58,g['miss'],SECTION_WIDTH*.58,20,TEXT['miss'])
    result.extend(status_link(m))
    label=m.main_model_tooltip()
    x,width=main_model_rect(m)
    add('model',x,g['session'],width,20,label)
    return result
