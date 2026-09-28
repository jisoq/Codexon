"""Bounded session monitor drawing and matching native hit regions."""
import math
from .charts import dynamic_bounds
from .overlay_navigation import navigation_target

BODY_LEFT=28
BODY_WIDTH=336
METRIC_COLUMNS=(28,140,252)
# Logical coordinates shared by painting, native masks and scroll limits.
TITLE_Y=16
TITLE_HEIGHT=24
SECTION_GAP=12
SESSION_Y=TITLE_Y+TITLE_HEIGHT+SECTION_GAP
CARD_Y=SESSION_Y+20+SECTION_GAP
CARD_HEIGHT=64
GRAPH_ROW=52
GRAPH_COUNT=4


def partial_text(data):
    parts=[]
    if data.get('missing'):parts.append(f"환산 가능: {data.get('priced',0)}/{data.get('calls',0)} 호출")
    if data.get('token_composition',{}).get('cache_hit_partial'):parts.append('캐시 확인분')
    return ', '.join(parts)


def miss_text(data):
    from .overlay_view import amount
    from .i18n import formatted
    misses=data.get('cache_misses',{})
    if not misses.get('count'):return ''
    template='본 세션 캐시 미적중 {count}회: 해당 입력 {tokens}' if data.get('descendants') else '캐시 미적중 {count}회: 해당 입력 {tokens}'
    return formatted(template,count=misses['count'],tokens=amount(misses.get('input')))


def footer(m, include_call=True):
    data=m.data or {};latest=data.get('latest') or {}
    warnings=latest.get('warnings',[])
    note=', '.join(w if isinstance(w,str) else w.get('text','') for w in warnings)
    if latest.get('cost') is None:note=latest.get('price_issue') or '환산 불가'
    if not include_call:note=''
    active=data.get('active_requests',int(data.get('request',{}).get('state')=='진행'))
    alert=m.speed_alert().get('active')
    text=m.status_text().replace(' · ',': ') or ('출력 속도 저하: 근거 보기' if alert else '') or note or (f'진행 중: {active}개' if active else '')
    return text,'warning' if alert or note else 'secondary'


def geometry(data, reduced=False, has_footer=False):
    data=data or {}
    composition=CARD_Y+CARD_HEIGHT+SECTION_GAP
    bar=composition+22;rows=bar+14
    populations=[data.get(key,{}) for key in ('token_composition','cost_composition')]
    counts={section:max((len(c.get(section+'_parts',[])) for c in populations),default=0)
            for section in ('input','output')}
    unit=SESSION_Y
    end=max(rows+max(counts.values())*18,bar+6)
    partial=end+8 if partial_text(data) else None
    if partial is not None:end=partial+18
    miss=end+8 if miss_text(data) else None
    if miss is not None:end=miss+18
    divider=end+SECTION_GAP;tab=divider+SECTION_GAP;context=tab+20+SECTION_GAP
    content_end=context+GRAPH_ROW*GRAPH_COUNT-8
    full_height=content_end+(8+18 if has_footer else 0)+16
    height=min(432,full_height) if reduced else full_height
    return dict(header=TITLE_Y,session=SESSION_Y,cache=CARD_Y,tokens=composition,bar=bar,rows=rows,
                unit=unit,partial=partial,miss=miss,divider=divider,tab=tab,context=context,
                model=context,meta=context+24,recent=context,result=context+56,
                result_value=context+78,duration=context+112,call_tokens=context+142,
                call_bar=context+166,call_parts=context+180,content_end=content_end,
                body_end=height-(38 if has_footer else 16),status=height-34 if has_footer else None,height=height,full_height=full_height)


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
    return '—' if value is None else f'{value:.1f} tok/s'


def scope(data):
    child=f" (하위 {data['descendants']}개 포함)" if data.get('descendants') else ''
    return f"세션{child} : {data.get('calls',0):,}호출"


def toggle_segments(m,kind):
    """Share compact, right-aligned label geometry with native hit regions."""
    from PySide6.QtGui import QFontMetrics
    from .overlay_view import font
    from .i18n import tr,formatted
    options=(('tokens','개수'),('usd','$')) if kind=='unit' else (('latest','현재'),('history','최근'))
    metrics=QFontMetrics(font(m.appearance.family,11))
    widths=[max(28,metrics.horizontalAdvance(tr(label))+14) for _,label in options]
    x=364-sum(widths);segments=[]
    for (key,label),width in zip(options,widths):
        segments.append((x,width,key,label));x+=width
    return segments


def paint(d,m):
    from .overlay_view import amount,money,percent
    data=m.data or {};latest=data.get('latest') or {};c=data.get('token_composition',{});g=m.layout()
    from PySide6.QtGui import QFontMetrics
    from .i18n import tr,formatted
    d.text('세션',16,g['session'],80,20,12,weight=600)
    count_x=16+QFontMetrics(d.p.font()).horizontalAdvance(tr('세션'))+8
    child=f" (하위 {data['descendants']}개 포함)" if data.get('descendants') else ''
    d.text(f"{data.get('calls',0):,}호출"+child,count_x,g['session'],toggle_segments(m,'unit')[0][0]-8-count_x,20,11,'secondary',elide=True)
    values=(money(data.get('cost')),percent(c.get('cache_hit_rate')),speed(data.get('output_speed_summary',{}).get('value')))
    d.rect(16,CARD_Y,348,CARD_HEIGHT,'band',8)
    for x,label,value in zip(METRIC_COLUMNS,('비용','캐시 적중률','평균 출력 속도'),values):
        d.text(label,x,CARD_Y+8,100,18,11,'secondary')
        d.text(value,x,CARD_Y+30,100,26,20 if x in METRIC_COLUMNS[:2] else 18,color='cached_text' if x==140 else 'ink',weight=600,elide=True)
    if g['partial'] is not None:d.text(partial_text(data),28,g['partial'],336,18,10,'secondary',elide=True)
    if g['miss'] is not None:d.text(miss_text(data),28,g['miss'],336,18,11,'secondary',elide=True)
    currency=m.composition_unit=='usd'
    comp=data.get('cost_composition',{}) if currency else c
    segments=toggle_segments(m,'unit')
    d.rect(segments[0][0],g['unit'],364-segments[0][0],20,'band',10)
    for x,w,key,label in segments:
        if m.composition_unit==key:d.rect(x+1,g['unit']+1,w-2,18,'selection',9)
        d.text(label,x+7,g['unit'],w-7,20,11)
    fmt=money if currency else amount
    for x,section,title in ((28,'input','입력'),(204,'output','출력')):
        d.text(title,x,g['tokens'],64,18,11,weight=600)
        d.text(fmt(comp.get(section+'_total')),x+64,g['tokens'],96,18,11,right=True)
        d.rect(x,g['bar'],160,6,'track',3);offset=0
        for i,part in enumerate(comp.get(section+'_parts',[])):
            color=part['key'] if not part['key'].endswith('unknown') else 'unknown'
            length=160*part['share'];d.rect(x+offset,g['bar'],length,6,color);offset+=length
            d.rect(x,g['rows']+i*18+5,4,4,color)
            d.text('미분류' if color=='unknown' else part['label'],x+9,g['rows']+i*18,76,18,11,'secondary')
            d.text(fmt(part['tokens']) if part.get('known',True) else '—',x+85,g['rows']+i*18,75,18,11,right=True)
    d.line(16,g['divider'],364,g['divider'],'border',1)
    d.text('모델 호출',16,g['tab'],160,20,12,weight=600)
    segments=toggle_segments(m,'tab')
    d.rect(segments[0][0],g['tab'],364-segments[0][0],20,'band',10)
    for x,w,key,label in segments:
        if m.monitor_tab==key:d.rect(x+1,g['tab']+1,w-2,18,'selection',9)
        d.text(label,x+7,g['tab'],w-7,20,11)
    d.p.save();d.p.setClipRect(BODY_LEFT,g['context'],BODY_WIDTH,g['body_end']-g['context'])
    d.p.translate(0,-m.lower_offset)
    if m.monitor_tab=='history':
        rows=m.rows();step=BODY_WIDTH/max(1,len(rows))
        inspected=next((r for r in rows if m.call_id(r)==getattr(m,'inspected_call',None)),None)
        displayed=m.selected() if m.graph_pinned else inspected or latest
        if m.graph_pinned:inspected=None
        for lane,(key,label,fmt) in enumerate((('cost','비용 ($)',money),('cache_rate','캐시 (%)',percent),('output_speed','출력 속도 (tok/s)',lambda n:'—' if n is None else f'{n:.1f}'),('reasoning','추론 (토큰)',amount))):
            y=g['recent']+lane*GRAPH_ROW;low,high=dynamic_bounds(graph_value(r,key) for r in rows)
            d.text(label,28,y,168,16,10,'secondary');d.text((formatted('{number}번: ',number=inspected.get('ordinal','')) if inspected else '')+fmt(graph_value(displayed,key)),220,y,144,16,11,right=True)
            previous=None
            for i,row in enumerate(rows):
                val=graph_value(row,key);x=BODY_LEFT+(i+.5)*step
                if val is None:previous=None;continue
                yy=y+20+24*(high-val)/(high-low)
                condition=tuple(row.get(k) for k in ('model','effort','mode','transport','home','sid'))
                if previous and (key!='output_speed' or condition==previous[2]):d.line(previous[0],previous[1],x,yy,'accent',1)
                active=m.call_id(row)==m.call_id(displayed)
                if active:d.line(x,y+19,x,y+45,'secondary',.6)
                d.dot(x,yy,'warning' if row.get('cache_warning') else 'accent',3 if active else 2)
                previous=(x,yy,condition)
    else:
        from .ui_details import observed_transport
        model,suffix,mismatch=model_text(latest)
        if mismatch:
            d.text(model+suffix,28,g['model'],336,20,12,'error',weight=600,elide=True)
        else:
            from .overlay_view import font
            from PySide6.QtCore import Qt
            metrics=QFontMetrics(font(m.appearance.family,12))
            suffix_width=metrics.horizontalAdvance(tr(suffix))
            visible=metrics.elidedText(model,Qt.ElideRight,max(0,336-suffix_width))
            width=metrics.horizontalAdvance(visible)
            d.text(visible,28,g['model'],width,20,12)
            d.text(suffix,28+width,g['model'],336-width,20,12,'secondary')
        meta=f"추론 {latest.get('effort','미확인')}    모드 {latest.get('mode','미확인')}    연결 {observed_transport(latest) or '미확인'}"
        d.text(meta,28,g['meta'],336,20,11,'secondary',elide=True)
        for x,label,value in zip(METRIC_COLUMNS,('비용','캐시 적중률','출력 속도'),(money(latest.get('cost')),percent(latest.get('cache_rate')),speed(latest.get('output_speed')))):
            d.text(label,x,g['result'],108,18,11,'secondary',center=True);d.text(value,x,g['result_value'],108,26,18,color='cached_text' if x==140 else 'ink',weight=600,elide=True,center=True)
        duration=latest.get('completion_latency_ms')
        d.text('소요시간: '+(f'{duration/1000:.2f}초' if duration is not None else '—'),28,g['duration'],336,18,11,center=True)
        for x,section,label,part_key,part_label,color in (
                (28,'input','입력','cached','캐시 읽기','cached'),
                (204,'output','출력','reasoning','추론','reasoning')):
            total=latest.get(section);part=latest.get(part_key)
            valid=(type(total) is int and total>0 and type(part) is int and 0<=part<=total
                   and not latest.get(section+'_conflict'))
            d.text(label,x,g['call_tokens'],60,20,11,'secondary')
            d.text(amount(total),x+60,g['call_tokens'],100,20,14,weight=600,right=True)
            d.rect(x,g['call_bar'],160,6,'track',2)
            if valid:
                if section=='output':d.rect(x,g['call_bar'],160,6,'output',2)
                d.rect(x,g['call_bar'],160*part/total,6,color,2)
            d.text(part_label,x,g['call_parts'],82,20,10,'secondary')
            d.text(amount(part),x+82,g['call_parts'],78,20,11,right=True)
    d.p.restore()
    status,color=footer(m,include_call=m.monitor_tab=='latest')
    if status:d.text(status,28,g['status'],336,18,11,color,elide=True)



def action_links(m):
    data=m.data or {};result=[];g=m.layout()
    def add(key,x,y,w,h,**kw):result.append(dict(id=key,x=x,y=y,width=w,height=h,accessible=kw.pop('accessible',key),targetHint=key.startswith('call-') or key=='latest-context',**kw))
    for x,w,key,label in toggle_segments(m,'tab'):
        action='tab-'+key
        add(action,x,g['tab']-2,w,24,action=action,accessible=label+(': 최근 12호출 추이' if key=='history' else ': 가장 최근 호출 상세'))
    for x,w,key,label in toggle_segments(m,'unit'):
        action='unit-'+key
        add(action,x,g['unit']-2,w,24,action=action,accessible='개수: 토큰 수로 보기' if key=='tokens' else '달러 환산액으로 보기')
    from PySide6.QtGui import QFontMetrics
    from .overlay_view import font
    from .i18n import tr,formatted
    def heading(key,label,y,target,interaction='navigate'):
        if target:
            width=QFontMetrics(font(m.appearance.family,12,600)).horizontalAdvance(tr(label))
            add(key,28 if key=='collection' else 16,y,width,20,target=target,interaction=interaction,accessible=label)
    heading('session','세션',g['session'],navigation_target(data,'session'))

    if m.note and ('오류' in m.note or '지연' in m.note):
        heading('collection',m.note,g['status'],navigation_target(data,'collection'))
    if m.compact:add('scroll',28,g['context'],336,g['body_end']-g['context'],action='scroll',accessible='하단 스크롤 (위아래 방향키)')
    if m.monitor_tab=='history':
        rows=m.rows()
        for i,row in enumerate(rows):
            step=BODY_WIDTH/max(1,len(rows));x=BODY_LEFT+i*step
            y=max(g['context'],g['recent']-m.lower_offset)
            bottom=min(g['recent']+GRAPH_ROW*GRAPH_COUNT-8-m.lower_offset,g['body_end'])
            if bottom>y:
                add('call-'+str(i),x,y,step,bottom-y,target=navigation_target(data,'call',call=row),
                    interaction='select',selected=m.call_id(row)==m.selected_id,accessible=str(row.get('ordinal',i+1))+' 호출 선택')
    return result




def links(m):
    from .tooltips import TEXT
    from .ui_details import model_comparison
    data=m.data or {};row=data.get('latest') or {};result=action_links(m)
    def add(key,x,y,w,h,tip):
        if tip:result.append(dict(id='tip-'+key,x=x,y=y,width=w,height=h,tooltip=tip,accessible=tip,interaction='tooltip'))
    g=m.layout()
    for x,key in zip(METRIC_COLUMNS,('cost','cache_total','speed_total')):add(key,x,CARD_Y,100,CARD_HEIGHT,TEXT[key])
    for item in result:
        if item.get('action') in ('unit-tokens','unit-usd','tab-latest','tab-history'):
            item['tooltip']={'unit-tokens':'토큰 개수로 보기','unit-usd':'토큰 USD로 보기','tab-latest':'마지막 확인 호출','tab-history':'최근 12회 호출'}[item['action']]
    comp=data.get('cost_composition' if m.composition_unit=='usd' else 'token_composition',{})
    for x,section,title in ((28,'input','입력'),(204,'output','출력')):
        add(section,x,g['tokens'],160,20,TEXT[title])
        for i,part in enumerate(comp.get(section+'_parts',[])):
            add(section+str(i),x,g['rows']+i*18,160,18,TEXT.get(part['label'],''))
    if g.get('miss') is not None:add('miss',28,g['miss'],336,18,TEXT['miss'])
    model_y=None
    if m.monitor_tab=='latest':
        offset=m.lower_offset
        for x,key in zip(METRIC_COLUMNS,('cost','cache','speed')):
            y=g['result']-offset
            if y>=g['context'] and y+48<=g['body_end']:add('latest-'+key,x,y,108,48,TEXT[key])
        model_y=g['model']-offset
        y=g['duration']-offset
        if y>=g['context'] and y+18<=g['body_end']:add('duration',28,y,336,18,TEXT['duration'])
    if model_y is not None:
        comparison=model_comparison(row)
        tip='라우팅 의심' if comparison.startswith('모델 불일치') else '' if comparison else '비교 기록 부족 또는 응답 확인 대기'
        if row.get('model_state')=='관측 충돌':tip=''
        add('model',16,model_y,348,20,tip)
    return result
