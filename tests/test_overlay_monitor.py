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


def test_active_calls_stay_beside_trend_and_do_not_add_footer(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import OverlayContent,Drawing
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([]);m=OverlayContent();data=summary()
    m.set_content(data);height=m.base_height();drawn=[];original=Drawing.text
    def record(d,value,x,y,*args,**kwargs):
        drawn.append((str(value),x,y));return original(d,value,x,y,*args,**kwargs)
    monkeypatch.setattr(Drawing,'text',record)
    for active in (3,0):
        m.set_content(dict(data,active_requests=active));drawn.clear()
        image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied)
        image.fill(0);painter=QPainter(image);m.paint(painter);painter.end()
        labels=[r for r in drawn if r[0].startswith('진행 중:')]
        if active:
            heading=next(r for r in drawn if r[0]=='Trend')
            assert len(labels)==1 and labels[0][1]>heading[1] and labels[0][2]==heading[2]
        else:assert not labels
        assert m.base_height()==height


def test_collection_status_shares_title_and_misses_share_trend_row(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import OverlayContent,Drawing
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    model=OverlayContent();drawn=[];original=Drawing.text
    def record(d,value,x,y,width,*args,**kwargs):
        drawn.append((str(value),x,y,width));return original(d,value,x,y,width,*args,**kwargs)
    monkeypatch.setattr(Drawing,'text',record)
    data=summary(misses=1);data['active_requests']=3
    for note in ('수집 오류','수집 지연'):
        model.set_content(data,note);drawn.clear()
        canvas=QImage(model.panel_width(),model.panel_height(),QImage.Format_ARGB32_Premultiplied)
        canvas.fill(0);painter=QPainter(canvas);model.paint(painter);painter.end()
        status=[item for item in drawn if item[0].startswith(note)]
        heading=next(item for item in drawn if item[0]=='Main Agent')
        assert len(status)==1 and status[0][2]<heading[2]
        title=next(item for item in drawn if item[0]==model.display_title())
        assert status[0][2]==title[2] and status[0][1]>=title[1]+title[3]
        trend=next(item for item in drawn if item[0]=='Trend')
        misses=[item for item in drawn if item[0].startswith('캐시 미적중 총')]
        active=next(item for item in drawn if item[0].startswith('진행 중:'))
        assert len(misses)==1 and misses[0][2]==trend[2] and misses[0][1]>trend[1]
        assert active[1]+active[3]<=misses[0][1]


def test_scope_omits_absent_children():
    assert scope({'calls':28})=='Main Agent : 28호출'
    assert scope({'calls':28,'descendants':2,'main_usage':{'calls':8}})=='Main Agent : 8호출'


def test_stale_content_fades_both_panels_and_recovers_without_dimming_historical_gaps():
    from copy import deepcopy
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import OverlayContent
    from cachemonitor.overlay_appearance import default_appearance
    from cachemonitor.overlay_chrome import OverlayDetail,named_item
    from test_overlay_presentation import subagent_summary
    app=QApplication.instance() or QApplication([]);model=OverlayContent();pane=None
    data=subagent_summary();data['_collection']={'retained':True}
    def frame(note='',value=None):
        model.set_content(data if value is None else value,note,appearance=default_appearance(False))
        image=QImage(model.panel_width(),model.panel_height(),QImage.Format_ARGB32_Premultiplied)
        image.fill(0);painter=QPainter(image);model.paint(painter);painter.end()
        return image.copy(0,int(model.layout()['session']),350,model.panel_height()-int(model.layout()['session']))
    try:
        baseline=frame();model.set_layout(detail=True,compact=True)
        pane=OverlayDetail(model);pane.resize(240,model.panel_height());pane.show();app.processEvents()
        detail=named_item(pane.quick.rootObject(),'detailContent')
        baseline=frame()
        for note in ('기록 준비 중','수집 지연','수집 오류'):
            faded=frame(note);app.processEvents()
            assert faded!=baseline and detail.opacity()==.5
            assert model.data['cost']==data['cost'] and model.has_subagents()
        assert frame()==baseline and detail.opacity()==1.
        partial=deepcopy(data);partial['coverage_gap']=True
        assert frame(value=partial)==baseline and detail.opacity()==1.
        assert model.status_text()
        assert not pane.qml_errors
    finally:
        if pane:pane.close()
        app.processEvents()


def test_rendered_main_summary_uses_own_metrics_in_both_units(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries,main_usage
    from cachemonitor.overlay_view import OverlayContent,Drawing,money,amount
    from test_overlay_call_data import row,session
    app=QApplication.instance() or QApplication([])
    parent=session([row(0,cost=2,input=2000,cached=1500,output=200,
        response_status='completed',completion_latency_ms=1000)],id='parent')
    child=session([row(1,cost=7,input=9000,cached=1000,output=900,
        response_status='completed',completion_latency_ms=1000)],id='child',parent_thread_id='parent')
    engine=AnalysisEngine();engine.ingest([parent,child])
    data=next(v for v in OverlaySummaries().collect(engine) if v['id']=='parent')
    model=OverlayContent();model.set_content(data)
    labels=[];original=Drawing.text
    def capture(d,text,x,y,*args,**kwargs):
        if y<model.layout()['divider']:labels.append(str(text))
        return original(d,text,x,y,*args,**kwargs)
    monkeypatch.setattr(Drawing,'text',capture)
    for mode,field,fmt in (('tokens','token_composition',amount),('usd','cost_composition',money)):
        model.monitor_action('unit-'+mode);labels.clear()
        image=QImage(model.panel_width(),model.panel_height(),QImage.Format_ARGB32_Premultiplied)
        image.fill(0);painter=QPainter(image)
        try:model.paint(painter)
        finally:painter.end()
        assert money(main_usage(data)['cost']) in labels and money(data['cost']) not in labels
        assert '75.0%' in labels and '200.0 tps' in labels
        assert not any('호출' in label for label in labels) and model.subagent_call_label(main_usage(data)) in labels
        own=main_usage(data)[field]
        assert all(fmt(own[side+'_total']) in labels for side in ('input','output'))
        assert money(data['cost']) not in model.state['accessible']
    assert model.state['detailAgentCount']==str(data['descendants'])
    assert model.state['detailSubCost']==money(data['child_cost'])


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


def test_agent_name_dots_match_graph_colors_across_sorting_and_rolling_calls(monkeypatch):
    from copy import deepcopy
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import OverlayContent,Drawing
    from test_overlay_presentation import subagent_summary
    app=QApplication.instance() or QApplication([])
    model=OverlayContent();data=subagent_summary()
    model.set_content(data);model.set_layout(detail=True,reduced=True)
    colors=model.graph_colors(model.rows())
    def name_colors():
        return {item[0]:item[8]['dot'] for item in model.detail_items()
                if item[8].get('kind')=='single-line' and item[8].get('dot')}
    names=name_colors()
    assert names=={row['title']:colors[(row['home'],row['id'])] for row in data['subagent_finance']['agents']}
    dots=[];original=Drawing.dot
    def dot(d,x,y,color='ink',r=1.5):
        dots.append((y,color));return original(d,x,y,color,r)
    monkeypatch.setattr(Drawing,'dot',dot)
    image=QImage(model.panel_width(),model.panel_height(),QImage.Format_ARGB32_Premultiplied)
    image.fill(0);painter=QPainter(image);model.paint(painter);painter.end()
    assert dots[0][1]==colors[(data['home'],data['id'])]
    changed=deepcopy(data)
    changed['recent']=[row for row in changed['recent'] if row['sid']=='other']
    for index,row in enumerate(changed['subagent_finance']['agents']):row['latest_ts']=1000+index
    model.set_content(changed)
    assert name_colors()==names and model.graph_colors(model.rows())==colors
    geometry=model.layout()
    assert geometry['recent']==geometry['context']
    model.scroll_lower(10000)
    assert model.lower_offset>0 and model.layout()['height']==geometry['height']
    assert any(entry['id'].startswith('call-') for entry in model.monitor_links())
    model.detail_group='model'
    assert not name_colors()


def test_waiting_cards_and_old_values_are_distinct(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    m=OverlayContent()
    for data,note,heading in ((None,'기록 확인 중','기록 준비 중'),(None,'호출 기록 없음','호출 기록 대기'),(summary(count=0),'','호출 기록 대기'),(None,'수집 오류','수집 오류'),(None,'수집 지연','수집 지연'),(None,'수집 시작 중','수집 시작 중')):
        m.set_content(data,note)
        assert m.notice_card()[0]==heading
        assert len(m.monitor_links())==1 and m.monitor_links()[0]['y']==m.layout()['header']
        assert '0호출' not in m.state['accessible'] and '미확인' not in m.state['accessible']
    data=summary();data['_collection']={'last_confirmed_at':700}
    monkeypatch.setattr('cachemonitor.overlay_view.time.time',lambda:1000)
    m.set_content(data,'수집 지연');assert not m.notice_card()
    assert m.data['cost']==data['cost'] and m.freshness_text()=='5분 전 확인값 표시 중'
    assert m.freshness_text() in m.state['accessible']
    data['_collection']['last_confirmed_at']=640
    m.set_content(data,'수집 지연');assert m.freshness_text()=='6분 전 확인값 표시 중'
    m.set_content(data);assert not m.freshness_text() and m.monitor_links()


def test_record_loading_moves_only_while_visible_and_checking(tmp_path):
    import time
    from PySide6.QtCore import QObject, QEventLoop
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import SessionOverlay
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    def until(condition):
        deadline=time.monotonic()+3
        while not condition():
            assert time.monotonic()<deadline
            app.processEvents(QEventLoop.AllEvents,10)
    window=SessionOverlay()
    try:
        window.set_layout(compact=True)
        window.set_content(None,'기록 확인 중',dark=False)
        window.show()
        until(lambda: window.quick.rootObject() is not None)
        scene=window.quick.rootObject()
        spinner=scene.findChild(QObject,'overlay-record-loading')
        animation=scene.findChild(QObject,'overlay-record-loading-animation')
        until(lambda: animation.property('running') and spinner.property('rotation')>0)
        first=window.grab().toImage()
        until(lambda: window.grab().toImage()!=first)
        assert window.grab().save(str(tmp_path/'record-loading.png'))
        window.hide()
        until(lambda: not animation.property('running'))
        window.show()
        until(lambda: animation.property('running'))
        for data,note in ((None,'수집 오류'),(None,'호출 기록 없음'),(summary(),'')):
            window.set_content(data,note)
            until(lambda: not animation.property('running'))
            assert not spinner.property('visible')
        assert not window.qml_errors
    finally:
        window.close();app.processEvents()


@pytest.mark.parametrize('language',['ko','en'])
def test_fresh_collection_time_updates_only_header_evidence_without_rebuilding_details(monkeypatch,language):
    from copy import deepcopy
    from PySide6.QtWidgets import QApplication
    from cachemonitor.i18n import set_language
    from cachemonitor.overlay_view import OverlayContent,timestamp
    from test_overlay_navigation import family_summary
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr('cachemonitor.overlay_view.time.time',lambda:1000)
    set_language(language)
    try:
        content=OverlayContent();data=family_summary(count=16)
        data['_collection']={'last_confirmed_at':950}
        content.set_content(data,'수집 지연');content.set_layout(detail=True)
        before=deepcopy(content._detail_items);accessible=content.detailBody.state['accessible']
        assert timestamp(950) in content.status_detail() and timestamp(950) not in accessible
        def unexpected_rebuild():raise AssertionError('Collection time rebuilt all session evidence')
        monkeypatch.setattr(content,'detail_items',unexpected_rebuild)
        updated={**data,'_collection':{'last_confirmed_at':953}}
        content.set_content(updated,'수집 지연')
        after=content.detailBody.state['accessible']
        assert after==accessible and timestamp(953) in content.status_detail()
        assert content.freshness_text()==OverlayContent.collection_age(data,'수집 지연')
        assert before==content._detail_items
        changes=[];content.detailBody.changed.connect(lambda:changes.append(True))
        content.set_content(updated,'수집 지연');assert not changes
        content.set_layout(detail=False);changes.clear()
        content.set_content({**data,'_collection':{'last_confirmed_at':956}},'수집 지연')
        assert not changes and not content._detail_items
    finally:set_language('ko')


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
    content.set_content(data);assert not content._detail_items and len(content.rows())==20 and 'all_calls' not in data
    changes=[];content.changed.connect(lambda:changes.append(True))
    content.set_content({**data,'_collection':{'last_confirmed_at':100}})
    assert not changes
    content.monitor_action('tab-latest');assert content.monitor_tab=='history'
    content.set_layout(reduced=True);content.scroll_lower(999)
    assert content.lower_offset==110 and content.layout()['tokens']==168


def test_layout_collapses_notices_and_composition_has_tooltip_only_regions():
    from copy import deepcopy
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay_view import OverlayContent
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    m=OverlayContent();m.set_content(summary());base=m.layout()
    assert m.monitor_tab=='history' and base['miss'] is None and base['partial'] is None
    assert base['tokens']==base['cache']+64+16
    assert base['unit']==base['session']
    assert base['session']-(base['header']+24)==12
    assert base['cache']-(base['session']+20)==16
    assert base['tab']-base['divider']==16
    assert base['status'] is None
    assert base['height']-(base['call_parts']+20)==16
    assert base['call_parts']+20==base['recent']+4*52-8
    assert base['divider']==base['rows']+3*18+16
    assert [r['id'] for r in m.monitor_links() if r['id'].startswith('tab-')]==[]
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
    assert notice['height']==base['height'] and notice['miss']==notice['tab']
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
    data['latest']['output_speed']=12.0
    m.set_content(data);m.monitor_action('tab-history');captured=[];dots=[]
    original=Drawing.text;dot=Drawing.dot
    def text(d,value,*args,**kwargs):captured.append(str(value));return original(d,value,*args,**kwargs)
    def point(d,x,y,*args,**kwargs):dots.append((x,y));return dot(d,x,y,*args,**kwargs)
    monkeypatch.setattr(Drawing,'text',text);monkeypatch.setattr(Drawing,'dot',point)
    image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied);image.fill(0)
    painter=QPainter(image);m.paint(painter);painter.end()
    assert all(label in captured for label in ('비용','캐시','출력 속도','추론'))
    assert '12.0 tps' in captured and '800 토큰' in captured
    assert not any('tok/s' in t or t.startswith('최근 호출:') or '모드 ' in t for t in captured)
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
    m=OverlayContent();m.set_content(summary(count=20));m.monitor_action('tab-history')
    latest=m.selected_id;m.pin_call(latest)
    m.set_content(summary(count=21))
    assert m.graph_pinned and m.selected_id==latest and not m.follow_latest
    NavigationModel(m).inspectCall('call-0')
    assert m.selected_id==latest
    m.pin_call(latest)
    assert not m.graph_pinned and m.selected_id==m.call_id(m.rows()[-1])
    first=m.call_id(m.rows()[0]);m.pin_call(first);m.pin_call(first,toggle=False)
    assert m.graph_pinned
    m.set_content(summary(count=22))
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
