"""Single-column drawing and matching native hit regions."""
import math
from .charts import dynamic_bounds
from .overlay_navigation import navigation_target
from .overlay_data import main_usage
from .overlay_layout import ColumnLayout, WIDTH, MARGIN, HEADER_Y

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
TITLE_Y=HEADER_Y
TITLE_HEIGHT=24
SESSION_Y=362
GRAPH_COUNT=3
GRAPH_ROW=78
CHARTS=(('cost','비용'),('output_speed','출력 속도'),('reasoning','추론 토큰'))
REMOTE_TITLE='원격 세션'
REMOTE_HEADING='사용량 표시 미지원'
REMOTE_DESCRIPTION='이 오버레이는 로컬 세션만 지원하며, 원격 기기의 사용량은 표시하지 않습니다.'


def notice_geometry(m):
    return dict(header=TITLE_Y,total=44,status=38,context=52,body=52,body_height=0,height=72,full_height=72)


def partial_text(data):
    return '캐시 확인분' if main_usage(data).get('token_composition',{}).get('cache_hit_partial') else ''


def miss_text(data):
    from .overlay_view import amount
    from .i18n import formatted
    misses=data.get('cache_misses',{})
    if not misses.get('count'):return ''
    return formatted('캐시 미적중 총 {count}회 ({tokens})',count=misses['count'],tokens=amount(misses.get('input')))


def header_status(m):
    # Status messages replace the heartbeat, without reserving a header row.
    return '', 'secondary'


def header_layout(m):return dict(title_width=TITLE_WIDTH,status_x=MARGIN,status_width=SECTION_WIDTH)


def geometry(data, reduced=False, has_status=False, *, preferred_height=720, main_detail_height=0, available_height=None, auto_sub_height=80):
    return ColumnLayout.create(preferred_height=preferred_height,main_detail_height=main_detail_height,auto_sub_height=auto_sub_height,
                               has_subagents=bool((data or {}).get('descendants',0)),has_status=has_status,reduced=reduced,
                               available_height=available_height).mapping()


def model_text(row):
    from .ui_details import model_comparison
    comparison=model_comparison(row)
    model=row.get('requested_model') or row.get('model') or '미확인'
    if row.get('model_setting') and not row.get('requested_model'):model='설정: '+model
    if comparison and not comparison.startswith('모델 일치') and row.get('response_model'):
        from .i18n import formatted
        return model,' '+formatted('(불일치: {model})',model=row['response_model']),True
    return model,'',False


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
    """Join each agent's observations at their original global call slots."""
    previous={};segments=[]
    for index,row in enumerate(rows):
        agent=(row.get('home'),row.get('sid'));value=graph_value(row,key)
        prior=previous.get(agent)
        if prior is not None and value is not None:segments.append((prior,(index,value)))
        previous[agent]=(index,value) if value is not None else None
    return tuple(segments)


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
    text=money(value) if key=='cost' else speed(value) if key=='output_speed' else amount(value)
    if key=='cost':text+=' / '+formatted('캐시 {rate}',rate=percent(graph_value(displayed,'cache_rate')))
    d.text(label,MARGIN,y,96,18,11,'secondary')
    d.text(text,MARGIN+96,y,SECTION_WIDTH-96,18,11,right=True)
    top=y+24;bottom=y+height-10;plot_height=max(1,bottom-top)
    values=[graph_value(row,key) for row in rows]
    _,high=dynamic_bounds([0,*values]);high=max(high,1e-12)
    colors=m.graph_colors(rows);step=SECTION_WIDTH/max(1,len(rows));data=m.data or {}
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
        if m.chart_style=='bar':
            width=max(1,min(12,step*.7)*.7);d.rect(x-width/2,yy,width,max(1,bottom-yy),col)
        else:
            radius=3 if active else 2
            d.dot(x,yy,col,radius)


def paint(d,m):
    if m.notice_card():return
    from .overlay_view import paint_items,money
    from .overlay_icons import agent_mark
    data=m.data or {};own=main_usage(data);g=m.layout();rows=m.rows()
    d.text('전체 비용',MARGIN,g['total'],56,28,11,'secondary')
    total_width=m.heartbeat_state()['x']-MARGIN-56-6
    d.text(money(m.total_cost()),MARGIN+56,g['total'],total_width,28,22,weight=600)
    displayed=m.displayed_call()
    for (key,label),y in zip(CHARTS,g['charts']):_chart(d,m,rows,key,label,y,g['chart_height'],displayed)
    displayed=m.latest_call()
    y=g['main'];mark=agent_mark(data.get('id'),m.appearance)
    if m.agent_highlight_opacity>0 and displayed and (displayed.get('home'),displayed.get('sid'))==(data.get('home'),data.get('id')):
        d.rect(MARGIN,y,SECTION_WIDTH,g['main_height'],mark.color,5,alpha=m.agent_highlight_opacity)
    d.line(MARGIN,y-6,RIGHT,y-6)
    paint_items(d,[(item[0],item[1]+MARGIN,item[2]+y,*item[3:])
                   for item in m.agent_summary_items(own,'Main Agent',mark,m.main_expanded,SECTION_WIDTH,main=True)])
    if m.main_expanded:
        d.p.save();d.p.translate(MARGIN,g['main_detail']);paint_items(d,m.main_items());d.p.restore()
    if m.has_subagents():
        d.line(MARGIN,g['sub_heading']-6,RIGHT,g['sub_heading']-6)
        d.text('Sub Agents',MARGIN,g['sub_heading'],80,20,12,weight=600)
        d.text(f"{data.get('descendants',0):,}",MARGIN+84,g['sub_heading'],40,20,11,'secondary')
        price_x,price_width=m.agent_cost_column(SECTION_WIDTH)
        d.text(money(data.get('child_cost')),MARGIN+price_x,g['sub_heading'],price_width,20,11,'secondary')


def action_links(m):
    if m.notice_card():return []
    data=m.data or {};g=m.layout();result=[]
    result.append(dict(id='main-detail',x=MARGIN,y=g['main'],width=SECTION_WIDTH,height=g['main_height'],
                       action='main-detail',interaction='detail',accessible='Main Agent 내역 접기' if m.main_expanded else 'Main Agent 내역 펼치기',selected=m.main_expanded,
                       tooltip=m.main_model_tooltip()))
    return result


def links(m):
    if m.notice_card():return []
    result=action_links(m);g=m.layout();rows=m.rows();step=SECTION_WIDTH/max(1,len(rows))
    for chart,y in enumerate(g['charts']):
        for index,row in enumerate(rows):
            result.append(dict(id=f'inspect-{chart}-{index}',x=MARGIN+index*step,y=y+20,width=step,
                height=g['chart_height']-20,interaction='inspect',hoverCall=m.call_id(row),
                accessible=agent_label(m.data or {},row)))
    return result
