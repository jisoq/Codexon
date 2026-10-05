import pytest
from cachemonitor.overlay_data import cost_composition
from cachemonitor.pricing import token_cost
from cachemonitor.overlay_monitor import scope


def priced(**changes):
    row=dict(model='gpt-6-astra',service_tier='Standard',input=1000,cached=800,written=20,output=100,reasoning=40)
    row.update(changes);return {**row,**token_cost(row)}


def test_currency_population_matches_headline_and_unknown_output_is_preserved():
    rows=[priced(),priced(model='gpt-6-sol',service_tier='Fast',reasoning=None),priced(input_conflict=True)]
    c=cost_composition(rows)
    assert c['priced']==2 and c['partial']
    assert c['input_total']+c['output_total']==pytest.approx(sum(r['cost'] for r in rows if r['cost'] is not None))
    for section in ('input','output'):
        assert sum(p['tokens'] for p in c[section+'_parts'])==pytest.approx(c[section+'_total'])
        assert sum(p['share'] for p in c[section+'_parts'])==pytest.approx(1)
    assert next(p['tokens'] for p in c['output_parts'] if p['key']=='output_unknown')==rows[1]['cost_output']


def test_currency_unknown_and_observed_zero_are_distinct():
    assert cost_composition([priced(service_tier='unknown')])['input_total'] is None
    assert cost_composition([priced(input=0,cached=0,written=0,output=0,reasoning=0)])['input_total']==0


def test_scope_omits_absent_children():
    assert scope({'calls':28})=='세션 : 28호출'
    assert scope({'calls':28,'descendants':2})=='세션 (하위 2개 포함) : 28호출'


@pytest.mark.parametrize('gap',[False,True])
def test_all_recent_graphs_connect_same_agent_across_interleaved_calls(monkeypatch,gap):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import Drawing,OverlayContent
    from cachemonitor.overlay_monitor import BODY_LEFT,BODY_WIDTH
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    m=OverlayContent();data=summary(count=6)
    for i,row in enumerate(data['recent']):
        row.update(home='home',sid='parent' if i%2==0 else 'child',cost=i+1,cache_rate=80+i,
                   output_speed=10+i,reasoning=i,output=100,model='example',effort='high',mode='Standard',transport='WebSocket')
    if gap:data['recent'][2].update(cost=None,cache_rate=None,output_speed=None,reasoning=None)
    m.set_content(data);m.monitor_action('tab-history');segments=[]
    colors=m.graph_colors(data['recent'])
    original=Drawing.line
    def line(d,x,y,x2,y2,color='border',width=1,dashed=False):
        if color in colors.values() and width==1:segments.append((x,y,x2,y2,color))
        return original(d,x,y,x2,y2,color,width,dashed)
    monkeypatch.setattr(Drawing,'line',line)
    image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied);image.fill(0)
    p=QPainter(image);m.paint(p);p.end()
    step=BODY_WIDTH/6
    for lane in range(4):
        top=m.layout()['recent']+lane*52
        pairs=[(round((a-BODY_LEFT)/step-.5),round((b-BODY_LEFT)/step-.5))
               for a,y,b,y2,color in segments if top<=y<top+52]
        assert pairs==([(1,3),(3,5)] if gap else [(0,2),(1,3),(2,4),(3,5)])
        for a,y,b,y2,color in segments:
            if top<=y<top+52:
                i=round((a-BODY_LEFT)/step-.5)
                assert color==colors[('home','parent' if i%2==0 else 'child')]


def test_agent_colors_survive_rolling_calls_and_meet_theme_contrast():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from cachemonitor.overlay_appearance import default_appearance
    from cachemonitor.token_colors import contrast_ratio,ui_palette
    app=QApplication.instance() or QApplication([])
    m=OverlayContent();data=dict(home='h',id='parent')
    rows=[dict(home='h',sid=s) for s in ('parent','child-1','child-2')]
    for dark in (True,False):
        m.set_content(data,appearance=default_appearance(dark))
        before=m.graph_colors(rows)
        assert len({before[('h',row['sid'])] for row in rows})==3
        after=m.graph_colors(rows[1:]+[dict(home='h',sid='child-3')])
        assert all(after[key]==value for key,value in before.items())
        assert m.graph_colors(rows)==after
        bg=ui_palette(m.appearance)['overlay']
        assert all(contrast_ratio(color,bg)>=3 for color in after.values())


def test_waiting_cards_and_old_values_are_distinct(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    m=OverlayContent()
    for data,note,heading in ((None,'기록 확인 중','기록 확인 중'),(None,'호출 기록 없음','호출 기록 대기'),(summary(count=0),'','호출 기록 대기'),(None,'수집 오류','수집 오류'),(None,'수집 지연','수집 지연'),(None,'수집 시작 중','수집 시작 중')):
        m.set_content(data,note)
        assert m.notice_card()[0]==heading and not m.monitor_links()
        assert '0호출' not in m.state['accessible'] and '미확인' not in m.state['accessible']
    data=summary();data['_collection']={'last_confirmed_at':700}
    monkeypatch.setattr('cachemonitor.overlay_view.time.time',lambda:1000)
    m.set_content(data,'수집 지연');assert not m.notice_card()
    assert m.data['cost']==data['cost'] and m.freshness_text()=='5분 전 확인값 표시 중'
    assert m.freshness_text() in m.state['accessible']
    data['_collection']['last_confirmed_at']=640
    m.set_content(data,'수집 지연');assert m.freshness_text()=='6분 전 확인값 표시 중'
    m.set_content(data);assert not m.freshness_text() and m.monitor_links()


def test_child_call_navigation_and_scope_switch_guard():
    from types import SimpleNamespace
    from cachemonitor.overlay_navigation import navigation_target
    from cachemonitor.overlay_chrome import NavigationModel
    row=dict(home='h',sid='child',id='call')
    content=SimpleNamespace(data=dict(home='h',id='root',members=[('h','root'),('h','child')],latest=row))
    target=navigation_target(content.data,'call');assert target.sid=='child'
    nav=NavigationModel(content);received=[];nav.navigationRequested.connect(received.append)
    nav.link_state([dict(id='call',target=target)]);nav.activateLink('call');assert received==[target]
    nav.captureNavigation('call');content.data={**content.data,'id':'different'};nav.activateNavigation();assert received==[target]


def test_inactive_details_and_freshness_do_not_repaint():
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    from cachemonitor.overlay_view import OverlayContent
    from test_overlay_presentation import summary
    content=OverlayContent();data=summary(count=100)
    content.set_content(data);assert not content._detail_items and len(content.rows())==12 and 'all_calls' not in data
    changes=[];content.changed.connect(lambda:changes.append(True))
    content.set_content({**data,'_collection':{'last_confirmed_at':100}})
    assert not changes
    content.monitor_action('tab-latest');assert content.monitor_tab=='latest'
    content.set_layout(reduced=True);content.scroll_lower(999)
    assert content.lower_offset==110 and content.layout()['tokens']==168


def test_layout_collapses_notices_and_composition_has_tooltip_only_regions():
    from copy import deepcopy
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    m=OverlayContent();m.set_content(summary());base=m.layout()
    assert m.monitor_tab=='latest' and base['miss'] is None and base['partial'] is None
    assert base['tokens']==base['cache']+64+16
    assert base['unit']==base['session']
    assert base['session']-(base['header']+24)==12
    assert base['cache']-(base['session']+20)==16
    assert base['tab']-base['divider']==16
    assert base['status'] is None
    assert base['height']-(base['call_parts']+20)==16
    assert base['call_parts']+20==base['recent']+4*52-8
    assert base['divider']==base['rows']+3*18+16
    assert [r['id'] for r in m.monitor_links() if r['id'].startswith('tab-')]==['tab-latest','tab-history']
    assert not any(r.get('formula') or r['id'].startswith('composition-') for r in m.monitor_links())
    # Composition tooltips must never become navigation or mutation targets.
    for x,count in ((20,3),(187,2)):
        for i in range(count):
            px,py=x+20,base['rows']+i*18+9
            assert not any(r['x']<=px<r['x']+r['width'] and r['y']<=py<r['y']+r['height'] for r in m.monitor_links() if r.get('interaction')!='tooltip')
    for tab in ('history','latest'):
        m.monitor_action('tab-'+tab)
        for unit in ('usd','tokens'):
            m.monitor_action('unit-'+unit);assert m.layout()==base
    m.set_content(summary(misses=1));notice=m.layout()
    assert notice['height']==base['height']+52 and notice['miss'] is not None
    assert not any(r['id']=='cache-misses' for r in m.monitor_links())
    changed=deepcopy(summary());changed['cost_composition']['output_parts'].append(dict(key='output_unknown',tokens=1,share=.01,label='미분류'))
    m.set_content(changed);unknown=m.layout();assert unknown['unit']==base['unit']
    m.monitor_action('unit-usd');assert m.layout()==unknown
    m.set_content(summary());assert m.layout()==base


def test_model_labels_require_confirmed_response_and_highlight_only_mismatch():
    from cachemonitor.overlay_monitor import model_text
    row=dict(model='gpt-example',requested_model='gpt-example',response_model='gpt-example',response_status='completed',model_match='일치')
    assert model_text(row)==('gpt-example',' (일치)',False)
    row.update(response_model='gpt-other',model_match='불일치',model_alert_confirmed=True)
    assert model_text(row)==('gpt-example',' (응답: gpt-other)',True)
    assert model_text({**row,'observation_missing':True})==('gpt-example',' (확인 불가)',False)
    assert model_text(dict(model='gpt-config',model_setting=True))==('설정: gpt-config',' (확인 불가)',False)


@pytest.mark.parametrize('changes,expected', [
    ({'reasoning':0},0),({'reasoning':40},40),({'reasoning':None},None),
    ({'reasoning':101},None),({'reasoning':-1},None),({'reasoning':float('nan')},None),
    ({'output_conflict':True},None)])
def test_reasoning_series_preserves_zero_and_rejects_missing_or_conflicting_values(changes,expected):
    from cachemonitor.overlay_monitor import graph_value
    assert graph_value(dict(output=100,reasoning=40)|changes,'reasoning')==expected


def test_recent_renders_four_call_series_without_latest_identity(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import Drawing,OverlayContent
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    m=OverlayContent();data=summary(count=3)
    for i,row in enumerate(data['recent']):row['reasoning']=(0,None,800)[i]
    m.set_content(data);m.monitor_action('tab-history');captured=[];dots=[]
    original=Drawing.text;dot=Drawing.dot
    def text(d,value,*args,**kwargs):captured.append(str(value));return original(d,value,*args,**kwargs)
    def point(d,x,y,*args,**kwargs):dots.append((x,y));return dot(d,x,y,*args,**kwargs)
    monkeypatch.setattr(Drawing,'text',text);monkeypatch.setattr(Drawing,'dot',point)
    image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied);image.fill(0)
    painter=QPainter(image);m.paint(painter);painter.end()
    assert all(label in captured for label in ('비용 ($)','캐시 (%)','출력 속도 (tok/s)','추론 (토큰)'))
    assert not any('gpt-' in t or t.startswith('최근 호출:') or '모드 ' in t for t in captured)
    assert not any(r['id']=='latest-context' for r in m.monitor_links())
    graph_top=m.layout()['recent']+3*52
    assert len([p for p in dots if graph_top<=p[1]<graph_top+52])==2
    links=[r for r in m.monitor_links() if r.get('interaction')=='select']
    assert len(links)==3 and all(r['width']==106 for r in links)
    assert not any(r.get('formula') for r in links)


def test_hover_repeats_across_columns_refreshes_and_leave():
    from PySide6.QtCore import QEvent,QPointF,Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from cachemonitor.overlay_chrome import OverlayLinks
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    m=OverlayContent();m.set_content(summary());m.monitor_action('tab-history');host=OverlayLinks(m)
    try:
        host.resize(m.panel_width(),m.panel_height());host.sync()
        assert host.quick.hasMouseTracking()
        for repeat in range(3):
            for index in (0,5,11,3):
                row=next(r for r in m.monitor_links() if r['id']=='call-'+str(index))
                point=QPointF(row['x']+row['width']/2,row['y']+20)
                event=QMouseEvent(QEvent.MouseMove,point,point,Qt.NoButton,Qt.NoButton,Qt.NoModifier)
                QApplication.sendEvent(host.quick,event)
                assert m.inspected_call==m.call_id(m.rows()[index])
                m.set_content(summary(count=13+repeat));host.sync()
                assert m.inspected_call==m.call_id(m.rows()[index])
            QApplication.sendEvent(host.quick,QEvent(QEvent.Leave));assert m.inspected_call is None
    finally:host.close()


def test_graph_pin_toggle_refresh_and_window_expiry():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from cachemonitor.overlay_chrome import NavigationModel
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    m=OverlayContent();m.set_content(summary(count=12));m.monitor_action('tab-history')
    latest=m.selected_id;m.pin_call(latest)
    m.set_content(summary(count=13))
    assert m.graph_pinned and m.selected_id==latest and not m.follow_latest
    NavigationModel(m).inspectCall('call-0')
    assert m.selected_id==latest
    m.pin_call(latest)
    assert not m.graph_pinned and m.selected_id==m.call_id(m.rows()[-1])
    first=m.call_id(m.rows()[0]);m.pin_call(first);m.pin_call(first,toggle=False)
    assert m.graph_pinned
    m.set_content(summary(count=14))
    assert not m.graph_pinned and m.follow_latest
    m.pin_call(m.call_id(m.rows()[0]));m.monitor_action('tab-latest')
    assert not m.graph_pinned


def test_english_recent_call_number():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import OverlayContent,Drawing
    from cachemonitor.overlay_monitor import paint
    from cachemonitor.i18n import set_language,formatted
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    try:
        set_language('en')
        assert str(formatted('{number}번: ',number=2))=='Call 2: '
        m=OverlayContent();m.set_content(summary());m.monitor_tab='history';m.inspected_call=m.call_id(m.rows()[0])
        canvas=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied)
        painter=QPainter(canvas)
        class Capture(Drawing):
            labels=[]
            def text(self,value,*args,**kwargs):self.labels.append(str(value));super().text(value,*args,**kwargs)
        drawing=Capture(painter,m)
        try:paint(drawing,m)
        finally:painter.end()
        assert any('Call 1:' in text for text in drawing.labels)
        assert not any('번:' in text for text in drawing.labels)
    finally:set_language('ko')
