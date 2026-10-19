"""Single-column drawing and matching native hit regions."""
import math
from .charts import dynamic_bounds
from .overlay_navigation import navigation_target
from .overlay_data import main_usage
from .overlay_layout import ColumnLayout, WIDTH, MARGIN

RIGHT=WIDTH-MARGIN
SECTION_WIDTH=WIDTH-2*MARGIN
BODY_LEFT=MARGIN
BODY_WIDTH=SECTION_WIDTH
ACTIONS_X=RIGHT-72
HEADER_X=MARGIN-4
HEADER_WIDTH=ACTIONS_X-HEADER_X
TITLE_WIDTH=HEADER_WIDTH-8
POPUP_X=RIGHT-260
INLINE_CLOSE_X=ACTIONS_X
INLINE_HEADER_WIDTH=TITLE_WIDTH
TITLE_Y=12
TITLE_HEIGHT=24
SESSION_Y=362
GRAPH_COUNT=3
GRAPH_ROW=78
CHARTS=(('cost','호출 비용'),('output_speed','출력 속도'),('reasoning','추론 토큰'))
REMOTE_TITLE='원격 세션'
REMOTE_HEADING='사용량 표시 미지원'
REMOTE_DESCRIPTION='이 오버레이는 로컬 세션만 지원하며, 원격 기기의 사용량은 표시하지 않습니다.'


def notice_geometry(m):
    return dict(header=TITLE_Y,status=38,context=52,body=52,body_height=0,height=72,full_height=72)


def partial_text(data):
    return '캐시 확인분' if main_usage(data).get('token_composition',{}).get('cache_hit_partial') else ''


def miss_text(data):
    from .overlay_view import amount
    from .i18n import formatted
    misses=data.get('cache_misses',{})
    if not misses.get('count'):return ''
    return formatted('캐시 미적중 총 {count}회 ({tokens})',count=misses['count'],tokens=amount(misses.get('input'),True))


def header_status(m):
    alert=m.speed_alert().get('active')
    text=m.status_text() or ('출력 속도 저하' if alert else '')
    return text,'error' if m.states() and m.states()[0][1]=='error' else 'warning' if alert else 'secondary'


def header_layout(m):return dict(title_width=TITLE_WIDTH,status_x=MARGIN,status_width=SECTION_WIDTH)


def status_link(m):
    text=header_status(m)[0]
    if not text:return []
    target=navigation_target(m.data or {},'collection') if any(word in m.note for word in ('오류','지연')) else None
    return [dict(id='collection' if target else 'tip-status',x=MARGIN,y=38,width=SECTION_WIDTH,height=18,
                 tooltip=m.status_detail(),accessible=m.status_detail(),interaction='navigate' if target else 'tooltip',
                 **({'target':target} if target else {}))]


def geometry(data, reduced=False, has_status=False, *, preferred_height=720, main_detail_height=0, available_height=None):
    return ColumnLayout.create(preferred_height=preferred_height,main_detail_height=main_detail_height,
                               has_subagents=bool((data or {}).get('descendants',0)),reduced=reduced,
                               available_height=available_height).mapping()


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


def chart_segments(rows,key):
    """Join only adjacent global calls; an unknown sample breaks the line."""
    values=[graph_value(row,key) for row in rows]
    return tuple(((i,a),(i+1,b)) for i,(a,b) in enumerate(zip(values,values[1:])) if a is not None and b is not None)


def speed(value):return '—' if value is None else f'{value:.1f} tps'


def session_call_count(data):
    own=main_usage(data)
    return f"{own.get('priced',0):,}/{own.get('calls',0):,}"


def scope_label(data):return 'Main Agent'


def scope(data):return f"{scope_label(data)} : {session_call_count(data)}호출"


def agent_label(data,row):
    if (row.get('home'),row.get('sid'))==(data.get('home'),data.get('id')):return 'Main Agent'
    return row.get('source_title') or row.get('sid') or 'Sub Agent'


def toggle_segments(m,kind):return []


def main_model_rect(m):return MARGIN,m.layout().get('main',SESSION_Y)+22,SECTION_WIDTH,18


def _chart(d,m,rows,key,label,y,height,displayed):
    from .overlay_view import amount,money,percent
    from PySide6.QtGui import QColor,QPainterPath
    from .i18n import formatted
    value=graph_value(displayed,key)
    text=money(value) if key=='cost' else speed(value) if key=='output_speed' else '—' if value is None else formatted('{tokens} 토큰',tokens=amount(value))
    if key=='cost':text+=' / '+formatted('캐시 {rate}',rate=percent(graph_value(displayed,'cache_rate')))
    d.text(label,MARGIN,y,96,18,11,'secondary')
    d.text(text,MARGIN+96,y,SECTION_WIDTH-96,18,11,right=True)
    top=y+24;bottom=y+height-10;plot_height=max(1,bottom-top)
    values=[graph_value(row,key) for row in rows]
    _,high=dynamic_bounds([0,*values]);high=max(high,1e-12)
    colors=m.graph_colors(rows);step=SECTION_WIDTH/max(1,len(rows));data=m.data or {}
    if not rows and key=='cost':d.text('호출 기록 대기',MARGIN,top,SECTION_WIDTH,plot_height,11,'secondary',center=True)
    def position(index,value):return MARGIN+(index+.5)*step,bottom-plot_height*value/high
    def color(row):return colors[(row.get('home'),row.get('sid'))]
    def fill(points,tint):
        path=QPainterPath();path.moveTo(*points[0])
        for point in points[1:]:path.lineTo(*point)
        path.closeSubpath();q=QColor(tint);q.setAlphaF(.19);d.p.fillPath(path,q)
    if m.chart_style=='filled-line':
        for (i,a),(j,b) in chart_segments(rows,key):
            x1,y1=position(i,a);x2,y2=position(j,b);xm=(x1+x2)/2;ym=(y1+y2)/2
            c1,c2=color(rows[i]),color(rows[j])
            fill(((x1,bottom),(x1,y1),(xm,ym),(xm,bottom)),c1)
            fill(((xm,bottom),(xm,ym),(x2,y2),(x2,bottom)),c2)
            d.line(x1,y1,xm,ym,c1,1.4);d.line(xm,ym,x2,y2,c2,1.4)
    for i,(row,value) in enumerate(zip(rows,values)):
        if value is None:continue
        x,yy=position(i,value);active=m.call_id(row)==m.call_id(displayed);col=color(row)
        if active:d.line(x,top-2,x,bottom+2,'secondary',.7)
        main=(row.get('home'),row.get('sid'))==(data.get('home'),data.get('id'))
        if m.chart_style=='bar':
            width=max(1,min(12,step*.7));d.rect(x-width/2,yy,width,max(1,bottom-yy),col,1)
        radius=3 if active else 2
        if main:d.dot(x,yy,col,radius)
        else:d.rect(x-radius,yy-radius,radius*2,radius*2,col)


def paint(d,m):
    if m.notice_card():return
    from .overlay_view import money,percent,paint_items,fitted_money
    from .i18n import formatted
    data=m.data or {};own=main_usage(data);g=m.layout();rows=m.rows()
    d.text('총 호출 비용',MARGIN,g['total'],SECTION_WIDTH-28,16,11,'secondary')
    d.text(fitted_money(m.total_cost(),SECTION_WIDTH-92,26,m.appearance.family),MARGIN,g['total']+18,SECTION_WIDTH-92,30,26,weight=600)
    displayed=m.displayed_call()
    for (key,label),y in zip(CHARTS,g['charts']):_chart(d,m,rows,key,label,y,g['chart_height'],displayed)
    if displayed:
        y=g['charts'][-1]+g['chart_height']
        col=m.graph_colors(rows)[(displayed.get('home'),displayed.get('sid'))]
        main=(displayed.get('home'),displayed.get('sid'))==(data.get('home'),data.get('id'))
        if main:d.dot(MARGIN+3,y+9,col,3)
        else:d.rect(MARGIN,y+6,6,6,col)
        d.text(agent_label(data,displayed),MARGIN+13,y,SECTION_WIDTH-52,18,10,'secondary',elide=True)
        d.text('최신',RIGHT-32,y,32,18,10,'secondary',right=True)
    y=g['main'];color=m.graph_colors(rows)[(data.get('home'),data.get('id'))]
    if m.state.get('mainFocused') or (displayed.get('home'),displayed.get('sid'))==(data.get('home'),data.get('id')):
        d.rect(MARGIN-3,y-2,SECTION_WIDTH+6,g['main_height'],'selection',5)
    d.line(MARGIN,y-6,RIGHT,y-6)
    d.dot(MARGIN+3,y+10,color,3)
    d.text('Main Agent',MARGIN+13,y,104,20,12,weight=600)
    d.text(money(own.get('cost') if own.get('calls') else 0),MARGIN+112,y,SECTION_WIDTH-132,20,13,weight=600,right=True)
    d.text('⌄' if m.main_expanded else '›',RIGHT-16,y,16,20,13,'secondary',right=True)
    d.text(m.subagent_call_label(own),MARGIN,y+23,SECTION_WIDTH,18,10,'error' if own.get('model_mismatch') else 'secondary',elide=True)
    d.text(formatted('평균 출력 {speed}',speed=speed(own.get('output_speed_summary',{}).get('value'))),MARGIN,y+47,SECTION_WIDTH*.62,18,10,'secondary')
    d.text(formatted('캐시 {rate}',rate=percent(own.get('token_composition',{}).get('cache_hit_rate'))),MARGIN+SECTION_WIDTH*.62,y+47,SECTION_WIDTH*.38,18,10,'secondary',right=True)
    if m.main_expanded:
        d.p.save();d.p.translate(MARGIN,g['main_detail']);paint_items(d,m.main_items());d.p.restore()
    if m.has_subagents():
        d.line(MARGIN,g['sub_heading']-6,RIGHT,g['sub_heading']-6)
        d.text('Sub Agents',MARGIN,g['sub_heading'],SECTION_WIDTH,20,12,weight=600)


def action_links(m):
    if m.notice_card():return []
    data=m.data or {};g=m.layout();result=[]
    result.append(dict(id='main-detail',x=MARGIN,y=g['main'],width=SECTION_WIDTH,height=g['main_height'],
                       action='main-detail',interaction='detail',accessible='Main Agent 내역 접기' if m.main_expanded else 'Main Agent 내역 펼치기',selected=m.main_expanded,
                       tooltip=m.main_model_tooltip()))
    rows=m.rows();step=SECTION_WIDTH/max(1,len(rows))
    for i,row in enumerate(rows):
        result.append(dict(id='call-'+str(i),x=MARGIN+i*step,y=g['charts'][0],width=step,
                           height=g['charts'][-1]+g['chart_height']-g['charts'][0],
                           target=navigation_target(data,'call',call=row),interaction='select',
                           selected=m.call_id(row)==m.selected_id,targetHint=True,
                           accessible=agent_label(data,row)+' / '+str(row.get('ordinal',i+1))+' 호출 선택'))
    if rows:
        y=g['charts'][-1]+g['chart_height'];displayed=m.displayed_call()
        result.append(dict(id='focus-agent',x=MARGIN,y=y,width=SECTION_WIDTH-40,height=18,action='focus-agent',
                           accessible=agent_label(data,displayed),tooltip=m.subagent_call_label(displayed)))
        result.append(dict(id='latest',x=RIGHT-32,y=y,width=32,height=18,action='latest',accessible='최신 호출로 돌아가기'))
    return result


def links(m):
    if m.notice_card():return status_link(m)
    result=action_links(m);result.extend(status_link(m))
    return result
