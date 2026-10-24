"""Behavior shared by the vertical overlay's charts and fixed summaries."""
from copy import deepcopy
import json
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication

from cachemonitor.overlay_appearance import default_appearance
from cachemonitor.overlay_view import OverlayContent
from test_overlay_presentation import subagent_summary, summary


@pytest.fixture
def column_model():
    app = QApplication.instance() or QApplication([])
    model = OverlayContent()
    yield model
    model.deleteLater()
    app.processEvents()


def test_agent_color_identity_does_not_depend_on_discovery_order(column_model):
    data = subagent_summary()
    rows = [dict(home='home', sid=sid) for sid in ('parent', 'child', 'other')]
    column_model.set_content(data)
    original = column_model.graph_colors(rows)
    fresh = OverlayContent()
    try:
        fresh.set_content(data)
        reordered = fresh.graph_colors(list(reversed(rows)))
        assert {key: original[key] for key in reordered} == reordered
    finally:
        fresh.deleteLater()


def test_row_glows_restart_each_changed_owner_without_queueing():
    from cachemonitor.overlay_view import RowGlows
    main=('home','main');child=('home','child');new=('home','new')
    glows=RowGlows();initial={main:'$0.02',child:'100.0 tps'}
    glows.update(initial,now_ms=0);glows.update(dict(initial),now_ms=100)
    assert not glows.needs_frame
    changed={main:'$0.03',child:'120.0 tps'}
    glows.update(changed,now_ms=200)
    assert set(glows.active)=={main,child}
    assert glows.active[main].progress(300)>0
    for now in range(300,1001,100):
        changed={main:str(now),child:'120.0 tps',new:'new row'}
        glows.update(changed,now_ms=now)
        assert glows.active[main].progress(now)==0
        assert len(glows.active)<=3
    glows.update({main:str(1000)},now_ms=1100)
    assert set(glows.active)=={main}
    assert not glows.active[main].finished(1200)
    glows.advance(now_ms=2000)
    assert not glows.needs_frame
    glows.update({main:str(1000)},now_ms=2100)
    assert not glows.needs_frame
    glows.update({main:'new update'},now_ms=2200)
    assert glows.needs_frame
    glows.clear();glows.update({main:'different scope'},now_ms=2201)
    assert not glows.needs_frame


def test_reduced_motion_row_glow_restarts_then_expires_after_last_change():
    from cachemonitor.overlay_view import RowGlows
    owner=('home','main');glows=RowGlows()
    glows.update({owner:'before'},now_ms=0,reduced_motion=True)
    glows.update({owner:'first update'},now_ms=10,reduced_motion=True)
    assert glows.active[owner].reduced_motion
    assert glows.active[owner].progress(150)>0
    glows.update({owner:'second update'},now_ms=200,reduced_motion=True)
    assert glows.active[owner].progress(200)==0
    assert not glows.active[owner].finished(499)
    glows.advance(now_ms=500)
    assert not glows.needs_frame


def test_main_and_child_visible_updates_paint_immediately_and_share_one_idle_timer(column_model,monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import money
    from test_overlay_monitor import render
    clock=[0.]
    monkeypatch.setattr('cachemonitor.overlay_view.time',SimpleNamespace(monotonic=lambda:clock[0]))
    m=column_model;m.put(overlayVisible=True);data=subagent_summary();m.set_content(data)
    main=('home',data['id']);child=('home',data['subagent_finance']['agents'][0]['id'])
    assert not m._row_glow_timer.isActive()
    layout=m.layout();height=m.state['detailBodyHeight']
    def detail_image():
        image=QImage(round(m.detail_width),height,QImage.Format_ARGB32_Premultiplied);image.fill(0)
        painter=QPainter(image)
        try:m.detailBody.paint(painter)
        finally:painter.end()
        return image
    clock[0]=1.;changed=deepcopy(data)
    changed['main_usage']['cost']+=.5
    changed['subagent_finance']['agents'][0]['output_speed_summary']['value']=200.
    m.set_content(changed)
    assert money(changed['main_usage']['cost']) in [item[0] for item in m.agent_summary_items(changed['main_usage'],'Main Agent',None,False,m.detail_width,main=True)]
    assert any(item[0]=='200.0 tps' for item in m._detail_items)
    assert m._row_glow_timer.isActive() and set(m.row_glows.active)=={main,child}
    clock[0]=1.1
    changed=deepcopy(changed);changed['main_usage']['cost']+=.5
    changed['subagent_finance']['agents'][0]['output_speed_summary']['value']=300.
    m.set_content(changed)
    assert any(item[0]=='300.0 tps' for item in m._detail_items)
    assert all(glow.progress(1100)==0 for glow in m.row_glows.active.values())
    assert m.layout()==layout and m.state['detailBodyHeight']==height
    clock[0]=1.2;active_main=render(m);active_child=detail_image()
    clock[0]=2.1;m.advance_row_glows()
    assert not m.row_glows.needs_frame and not m._row_glow_timer.isActive()
    assert render(m)!=active_main and detail_image()!=active_child
    changed=deepcopy(changed);changed['main_usage']['cost']+=.5
    m.set_content(changed);assert m._row_glow_timer.isActive()
    m.set_content(None)
    assert not m._row_glow_timer.isActive() and not m.row_glows.needs_frame


def test_row_refresh_ignores_metadata_palette_geometry_and_chart_selection(column_model):
    m=column_model;m.put(overlayVisible=True);data=subagent_summary();m.set_content(data)
    refreshed=deepcopy(data);refreshed['title']='Window title update'
    refreshed['latest_ts']=9000
    for agent in refreshed['subagent_finance']['agents']:
        agent['latest_ts']=9000
        agent['token_composition']['cache_hit_rate']+=.000001
    m.set_content(refreshed,appearance=replace(m.appearance,accent='#5288cc'))
    m.select(m.call_id(m.rows()[0]));m.set_layout(reduced=True)
    assert not m.row_glows.needs_frame and not m._row_glow_timer.isActive()
    changed=deepcopy(refreshed);changed['main_usage']['cost']+=.5;m.set_content(changed)
    assert m.row_glows.needs_frame
    new_scope=deepcopy(changed);new_scope['id']='different-main'
    m.set_content(new_scope)
    assert not m.row_glows.needs_frame and not m._row_glow_timer.isActive()
    m.clear_row_glows()


def test_confirmed_mismatch_theme_change_keeps_rows_quiet(column_model):
    from cachemonitor.overlay_icons import agent_mark
    m=column_model;m.put(overlayVisible=True);data=subagent_summary()
    for group in [data['main_usage'],*data['subagent_finance']['agents']]:
        group.update(requested_model='gpt-6-astra',response_model='gpt-6-luna',model_alert_confirmed=True)
    m.set_content(data,appearance=default_appearance(True))
    runs=m.model_runs(data['subagent_finance']['agents'][0])
    assert len(runs)==3 and runs[1].strike
    colors={agent_mark(owner[1],m.appearance).color for owner in m.row_appearances()}
    m.set_content(deepcopy(data),appearance=default_appearance(False))
    assert colors!={agent_mark(owner[1],m.appearance).color for owner in m.row_appearances()}
    assert not m.row_glows.needs_frame and not m._row_glow_timer.isActive()
    changed=deepcopy(data);changed['main_usage']['response_model']='gpt-6-sol'
    m.set_content(changed,appearance=m.appearance)
    assert set(m.row_glows.active)=={('home',data['id'])}


def test_reduced_motion_converts_only_live_moving_effects_and_never_resumes_travel(column_model,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.overlay_view import RowGlow
    clock=[0.]
    monkeypatch.setattr('cachemonitor.overlay_view.time',SimpleNamespace(monotonic=lambda:clock[0]))
    m=column_model;m.put(overlayVisible=True);data=subagent_summary();m.set_content(data)
    changed=deepcopy(data);changed['main_usage']['cost']+=.5
    clock[0]=1.;m.set_content(changed);owner=('home',data['id'])
    m.row_glows.active[('home','expired')]=RowGlow(0,False)
    clock[0]=1.51;m.set_reduced_motion(True);m.set_content(deepcopy(changed))
    assert set(m.row_glows.active)=={owner}
    fade=m.row_glows.active[owner]
    assert fade.reduced_motion and fade.progress(1510)==0
    clock[0]=1.6;m.set_reduced_motion(True)
    assert m.row_glows.active[owner] is fade
    m.set_reduced_motion(False)
    assert m.row_glows.active[owner] is fade
    assert not fade.finished(1809)
    clock[0]=1.811;m.advance_row_glows()
    assert not m.row_glows.needs_frame and not m._row_glow_timer.isActive()
    changed=deepcopy(changed);changed['main_usage']['cost']+=.5;m.set_content(changed)
    assert not m.row_glows.active[owner].reduced_motion
    m.put(overlayVisible=False);m.advance_row_glows()
    assert not m.row_glows.needs_frame and not m._row_glow_timer.isActive()


def test_controller_delivers_reduced_motion_before_equal_content_and_during_drag(column_controller,monkeypatch):
    from test_overlay_layout_controller import Native
    from cachemonitor.overlay_tracking import Selection
    c=column_controller;flag=[False];c.native=Native()
    monkeypatch.setattr(c.native,'reduce_motion',lambda:flag[0])
    c.appearance_reader.read=lambda dark:c.appearance
    data=subagent_summary();c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
    m=c.widget.content_model;owner=('home',data['id'])
    changed=deepcopy(m.data);changed['main_usage']['cost']+=.5;m.set_content(changed)
    assert not m.row_glows.active[owner].reduced_motion
    monkeypatch.setattr(c,'content',lambda:(m.data,m.note))
    flag[0]=True;c.next_theme=0;c.refresh()
    assert m.row_glows.active[owner].reduced_motion
    flag[0]=False;c.next_theme=0;c.refresh()
    changed=deepcopy(m.data);changed['main_usage']['cost']+=.5;m.set_content(changed)
    assert not m.row_glows.active[owner].reduced_motion
    c.begin_drag((100,100));assert c.drag_context
    before=m.data
    flag[0]=True;c.next_theme=0;c._tick()
    assert m.data is before and m.row_glows.active[owner].reduced_motion
    stationary=m.row_glows.active[owner]
    c.refresh()
    assert m.data is before and m.row_glows.active[owner] is stationary


def test_native_row_treatments_use_own_mark_colors_and_preserve_clip(column_model,monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor,QImage,QPainter,QPen
    from cachemonitor.overlay_view import Drawing,HighlightStyle,RowGlow
    from cachemonitor.overlay_icons import agent_mark
    from test_overlay_monitor import render
    clock=[1.12]
    monkeypatch.setattr('cachemonitor.overlay_view.time',SimpleNamespace(monotonic=lambda:clock[0]))
    from cachemonitor.core import Session
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    sessions=[]
    for sid,parent in (('parent',None),('child','parent'),('grandchild','child')):
        session=Session(sid,'home',title=sid)
        session.add_usage(1789884000,sid,dict(input_tokens=1000,cached_input_tokens=800,
            cache_write_input_tokens=0,output_tokens=100,reasoning_output_tokens=40),'gpt-6-astra','turn','high','Standard')
        source=session.view(1789884100)
        if parent:source.update(source='subagent',parent_thread_id=parent)
        sessions.append(source)
    engine=AnalysisEngine();engine.ingest(sessions)
    data=next(row for row in OverlaySummaries().collect(engine) if row['id']=='parent')
    m=column_model;m.put(overlayVisible=True);m.set_content(data)
    owners=[('home',data['id']),*(('home',group['id']) for group in m.detail_groups())]
    assert set(owners)=={('home','parent'),('home','child'),('home','grandchild')}
    m.row_glows.active={owner:RowGlow(1000,False) for owner in owners}
    supplied={};original=Drawing.row_glow
    def record(d,x,y,w,h,*,owner,color):
        supplied[owner]=color;return original(d,x,y,w,h,owner=owner,color=color)
    monkeypatch.setattr(Drawing,'row_glow',record)
    render(m)
    detail=QImage(round(m.detail_width),m.state['detailBodyHeight'],QImage.Format_ARGB32_Premultiplied);detail.fill(0)
    painter=QPainter(detail)
    try:m.detailBody.paint(painter)
    finally:painter.end()
    assert supplied=={owner:agent_mark(owner[1],m.appearance).color for owner in owners}
    owner=owners[0];color=supplied[owner];rect=QRectF(12,12,220,66)
    images=[]
    for style in HighlightStyle:
        m.highlight_style=style
        image=QImage(250,90,QImage.Format_ARGB32_Premultiplied);image.fill(0)
        painter=QPainter(image);painter.setPen(QPen(QColor('red'),3));painter.setClipRect(rect)
        before=painter.pen(),painter.clipBoundingRect(),painter.opacity(),painter.transform()
        try:
            Drawing(painter,m).row_glow(*rect.getRect(),owner=owner,color=color)
            assert (painter.pen(),painter.clipBoundingRect(),painter.opacity(),painter.transform())==before
        finally:painter.end()
        painted=[(x,y,image.pixelColor(x,y)) for y in range(image.height()) for x in range(image.width()) if image.pixelColor(x,y).alpha()]
        assert painted and all(rect.contains(x+.5,y+.5) for x,y,_ in painted)
        solid=[pixel for _,_,pixel in painted if pixel.alpha()>10]
        assert solid,style
        expected=QColor(color).getRgb()[:3]
        averaged=[sum(pixel.getRgb()[channel]*pixel.alpha() for pixel in solid)/sum(pixel.alpha() for pixel in solid) for channel in range(3)]
        assert all(abs(actual-wanted)<12 for actual,wanted in zip(averaged,expected))
        images.append(image)
    assert all(left!=right for index,left in enumerate(images) for right in images[index+1:])
    m.set_reduced_motion(True);stationary=[]
    for now in (1.12,1.22):
        clock[0]=now
        image=QImage(250,90,QImage.Format_ARGB32_Premultiplied);image.fill(0)
        painter=QPainter(image)
        try:Drawing(painter,m).row_glow(*rect.getRect(),owner=owner,color=color)
        finally:painter.end()
        stationary.append(image)
    def footprint(image):
        return {(x,y) for y in range(image.height()) for x in range(image.width()) if image.pixelColor(x,y).alpha()>0}
    assert footprint(stationary[0])==footprint(stationary[1])
    assert sum(stationary[1].pixelColor(x,y).alpha() for x,y in footprint(stationary[1]))<sum(stationary[0].pixelColor(x,y).alpha() for x,y in footprint(stationary[0]))


def test_agent_color_survives_rolling_calls_and_subagent_sorting(column_model):
    data = subagent_summary()
    column_model.set_content(data)
    colors = column_model.graph_colors(column_model.rows())
    updated = deepcopy(data)
    updated['recent'] = [row for row in updated['recent'] if row['sid'] == 'other']
    updated['subagent_finance']['agents'].reverse()
    column_model.set_content(updated)
    after = column_model.graph_colors(column_model.rows())
    assert all(after[key] == color for key, color in colors.items())


def test_agent_colors_return_after_switching_away_from_incremental_discovery(column_model):
    first=dict(home='h',id='owner',calls=2,cost=0,recent=[dict(home='h',sid='z',id='z-call')])
    column_model.set_content(first)
    z_color=column_model.graph_colors(column_model.rows())[('h','z')]
    expanded=dict(first,recent=[dict(home='h',sid='a',id='a-call'),*first['recent']])
    column_model.set_content(expanded)
    colors=column_model.graph_colors(column_model.rows())
    assert colors[('h','z')]==z_color and colors[('h','a')]!=z_color
    column_model.set_content(dict(home='h',id='another',calls=0,cost=0,recent=[]))
    column_model.set_content(expanded)
    assert column_model.graph_colors(column_model.rows())==colors


def test_token_details_keep_explicit_unknown_and_confirmed_zero_distinct(column_model):
    group=dict(calls=4,priced=3,cost=.5,
        token_composition=dict(known=True,category_known={'written':True},input_total=10,output_total=20,
            input_parts=[dict(key='cached',tokens=10,known=True)],
            output_parts=[dict(key='reasoning',tokens=0,known=False),dict(key='output',tokens=0,known=True)]),
        cost_composition=dict(known=True,input_total=.1,output_total=.4,
            input_parts=[dict(key='cached',tokens=.1,known=True)],
            output_parts=[dict(key='reasoning',tokens=0,known=False),dict(key='output',tokens=0,known=True)]))
    items=column_model.group_details(group)
    def value(label):
        item=next(item for item in items if item[0]==label)
        result=next(row[0] for row in items if row[2]==item[2] and row[8].get('right'))
        return ''.join(result) if isinstance(result,list) else result
    assert value('추론')=='— / —'
    assert value('추론 외')=='0 / $0.0000'
    assert value('캐시 쓰기')=='0 / —'
    assert value('입력 (확인분)')=='10 / $0.1000'
    assert value('출력 (확인분)')=='20 / $0.4000'
    assert any(item[0]=='토큰 구성 (3/4)' for item in items)


def test_codex_accent_does_not_reassign_icon_colors(column_model):
    data = subagent_summary()
    first = replace(default_appearance(True), accent='#287cf6')
    second = replace(first, accent='#e04c9a')
    column_model.set_content(data, appearance=first)
    before = column_model.graph_colors(column_model.rows())
    column_model.set_content(data, appearance=second)
    after = column_model.graph_colors(column_model.rows())
    assert before == after


def test_recent_call_population_keeps_missing_slots_and_original_order(column_model):
    data = summary(count=25)
    data['recent'][-3].update(cost=None, output_speed=None, reasoning=None)
    expected = data['recent'][-20:]
    column_model.set_content(data)
    assert column_model.rows() == expected
    assert len(column_model.rows()) == 20
    assert column_model.rows()[-3]['cost'] is None


def test_main_disclosure_keeps_selected_call_and_cumulative_population(column_model):
    data = subagent_summary()
    column_model.set_content(data)
    column_model.select(column_model.call_id(column_model.rows()[0]))
    selected = column_model.selected_id
    rows = deepcopy(column_model.rows())
    own = deepcopy(data['main_usage'])
    initial = column_model.main_expanded
    column_model.monitor_action('main-detail')
    assert column_model.main_expanded is not initial
    assert column_model.selected_id == selected
    assert column_model.rows() == rows
    assert column_model.data['main_usage'] == own
    column_model.monitor_action('main-detail')
    assert column_model.main_expanded is initial


def test_column_places_three_charts_then_main_before_sub_viewport():
    from cachemonitor.overlay_layout import ColumnLayout
    layout = ColumnLayout.create(has_subagents=True)
    assert layout.width == 300
    assert len(layout.charts) == 3
    assert all(start + layout.chart_height <= end
               for start, end in zip(layout.charts, layout.charts[1:]))
    assert layout.charts[-1] + layout.chart_height <= layout.main
    assert layout.main + layout.main_height <= layout.sub_heading
    assert layout.sub_heading < layout.sub_y
    assert layout.sub_height >= 80
    assert layout.sub_y + layout.sub_height <= layout.height


def test_main_expansion_adds_space_without_reducing_sub_viewport():
    from cachemonitor.overlay_layout import ColumnLayout
    closed = ColumnLayout.create(has_subagents=True)
    opened = ColumnLayout.create(has_subagents=True, main_detail_height=120)
    assert opened.charts == closed.charts
    assert opened.main == closed.main
    assert opened.main_detail_height == 120
    assert opened.sub_y - closed.sub_y == 120
    assert opened.height - closed.height == 120
    assert opened.sub_height == closed.sub_height


def test_length_preference_only_extends_subagent_viewport():
    from cachemonitor.overlay_layout import ColumnLayout
    short = ColumnLayout.create(preferred_height=720, has_subagents=True)
    tall = ColumnLayout.create(preferred_height=960, has_subagents=True)
    assert (tall.charts, tall.main, tall.main_height, tall.sub_y) == (
        short.charts, short.main, short.main_height, short.sub_y)
    assert tall.height - short.height == tall.sub_height - short.sub_height
    assert tall.sub_height > short.sub_height
    absent = ColumnLayout.create(preferred_height=960)
    assert absent.sub_height == 0
    assert absent.height < tall.height


@pytest.mark.parametrize('key', ['cost', 'output_speed', 'reasoning'])
def test_filled_line_keeps_missing_call_slot_and_does_not_bridge_it(key):
    from cachemonitor.overlay_monitor import chart_segments
    rows = [{key: value, 'output': 100} for value in (1, 2, None, 4, 5)]
    assert chart_segments(rows, key) == (
        ((0, 1), (1, 2)), ((3, 4), (4, 5)))


@pytest.mark.parametrize('field', ['model', 'effort', 'mode', 'transport'])
def test_speed_line_keeps_agent_continuity_when_request_condition_changes(field):
    from cachemonitor.overlay_monitor import chart_segments
    first = dict(output_speed=10, model='model', effort='high',
                 mode='Standard', transport='WebSocket')
    changed = dict(first, output_speed=20)
    changed[field] = 'changed'
    rows = [first, changed, dict(changed, output_speed=30)]
    assert chart_segments(rows, 'output_speed') == (
        ((0, 10), (1, 20)), ((1, 20), (2, 30)))


@pytest.mark.parametrize('key', ['cost', 'output_speed', 'reasoning'])
def test_filled_line_distinguishes_observed_zero_from_unknown_values(key):
    from cachemonitor.overlay_monitor import chart_segments
    observed = [{key: 0, 'output': 0}, {key: 0, 'output': 0}]
    assert chart_segments(observed, key) == (((0, 0), (1, 0)),)
    unknown = [{key: None}, {key: float('nan')}, {key: float('inf')}]
    assert chart_segments(unknown, key) == ()


def test_zero_call_summary_preserves_zero_total_and_has_no_chart_segments(column_model):
    from cachemonitor.overlay_monitor import chart_segments
    data = summary(count=0)
    column_model.set_content(data)
    assert column_model.data['calls'] == 0
    assert column_model.total_cost() == 0
    assert column_model.rows() == []
    assert all(chart_segments(column_model.rows(), key) == ()
               for key in ('cost', 'output_speed', 'reasoning'))


def test_unpriced_calls_keep_unknown_total_distinct_from_zero(column_model):
    data = summary(count=2)
    data['cost'] = None
    column_model.set_content(data)
    assert column_model.total_cost() is None


@pytest.fixture
def column_controller(tmp_path):
    from PySide6.QtCore import QSettings
    from cachemonitor.overlay import OverlayController
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / 'column.ini'), QSettings.IniFormat)
    controller = OverlayController(settings, native_enabled=False)
    yield controller
    controller.stop()
    app.processEvents()


def test_chart_style_change_preserves_selected_call_and_finance(column_controller):
    controller = column_controller
    model = controller.widget.content_model
    data = subagent_summary()
    model.set_content(data)
    model.select(model.call_id(model.rows()[0]))
    selected = model.selected_id
    own = deepcopy(model.data['main_usage'])
    rows = deepcopy(model.rows())
    for style in ('filled-line', 'bar'):
        controller.set_chart_style(style)
        assert controller.chart_style == model.chart_style == style
        assert model.selected_id == selected
        assert model.rows() == rows
        assert model.data['main_usage'] == own


def test_visible_agent_count_and_chart_style_preferences_survive_restart(column_controller,monkeypatch):
    from cachemonitor.overlay import OverlayController
    controller=column_controller
    monkeypatch.setattr(controller,'refresh',lambda:None)
    controller.widget.set_content(subagent_summary())
    controller.set_visible_agent_count(4)
    controller.set_chart_style('filled-line')
    controller.settings.setValue('overlay/manualHeights',json.dumps({controller.widget.content_model.agent_count_key():1200}))
    controller.settings.sync()
    restored=OverlayController(controller.settings,native_enabled=False)
    try:
        restored.widget.set_content(subagent_summary())
        assert restored.widget.content_model.visible_agent_count()==4
        assert restored.widget.content_model.base_height()==controller.widget.content_model.base_height()
        assert restored.chart_style=='filled-line'
    finally:restored.stop()


@pytest.mark.parametrize('invalid',[0,5,True,1.5,'2'])
def test_agent_count_rejects_invalid_choices(column_controller,monkeypatch,invalid):
    c=column_controller
    monkeypatch.setattr(c,'refresh',lambda:None)
    c.widget.set_content(subagent_summary())
    c.set_visible_agent_count(2)
    c.set_visible_agent_count(invalid)
    assert c.widget.content_model.visible_agent_count()==2
    assert json.loads(c.settings.value('overlay/visibleAgentCounts'))[c.widget.content_model.agent_count_key()]==2


@pytest.mark.parametrize('key', ['cost', 'output_speed', 'reasoning'])
def test_filled_line_connects_only_same_agent_and_preserves_interleaved_slots(key):
    from cachemonitor.overlay_monitor import chart_segments
    rows=[dict(home='home',sid=agent,output=100,**{key:value})
          for agent,value in [('a',1),('b',2),('a',3),('b',4),('a',None),('b',5),('a',6),('a',7)]]
    assert chart_segments(rows,key)==(((0,1),(2,3)),((1,2),(3,4)),((3,4),(5,5)),((6,6),(7,7)))


def test_optional_status_moves_content_without_reserving_an_empty_row():
    from cachemonitor.overlay_layout import ColumnLayout
    absent=ColumnLayout.create(has_subagents=True)
    present=ColumnLayout.create(has_subagents=True,has_status=True)
    shift=present.total-absent.total
    assert shift>0
    assert absent.header<absent.total<present.total
    assert present.status<present.total<present.charts[0]
    assert all(after-before==pytest.approx(shift) for before,after in zip(absent.charts,present.charts))
    assert present.main-absent.main==pytest.approx(shift)
    assert present.sub_y-absent.sub_y==pytest.approx(shift)


def test_notice_keeps_status_available_on_heartbeat_without_usage(column_model):
    column_model.set_content(None,'기록 확인 중')
    assert column_model.notice_card() and column_model.state['totalRowVisible']
    assert column_model.heartbeat_state()['mode']=='pending'
    assert column_model.state['heartbeatLabel'] in column_model.status_detail()
    assert not column_model.monitor_links()
    column_model.set_content(summary())
    assert column_model.notice_card() is None and column_model.state['totalRowVisible']
    assert column_model.heartbeat_state()['mode']=='idle'


def test_synthetic_preview_keeps_active_collection_fresh_after_long_idle(column_controller,monkeypatch):
    import time
    from types import SimpleNamespace
    from tools.preview_overlay import Preview,synthetic_snapshot
    controller=column_controller
    snapshot=synthetic_snapshot('multiagent')
    before=deepcopy(snapshot['overlay_sessions'])
    preview=SimpleNamespace(args=SimpleNamespace(snapshot=None),snapshot=snapshot,
                            controller=controller,selected='preview-main',hwnd=0)
    started=time.time()
    for elapsed in (31,3600):
        monkeypatch.setattr(time,'time',lambda:started+elapsed)
        Preview.poll(preview)
        data,note=controller.content()
        assert not note and not data['_collection']['delayed']
        assert data['active_requests']==3
        assert snapshot['overlay_sessions']==before


def test_agent_heartbeats_follow_own_requests_not_latest_call_or_aggregate(column_model):
    from tools.preview_overlay import synthetic_snapshot
    data=next(row for row in synthetic_snapshot('multiagent')['overlay_sessions'] if row['id']=='preview-main')
    column_model.set_content(data)
    assert column_model.state['mainHeartbeats'][0]['mode']=='active'
    groups={group['key']:group['id'] for group in column_model.detail_groups()}
    indicators=column_model.state['detailHeartbeats']
    assert {groups[item['id']] for item in indicators if item['mode']=='active'}=={'preview-agent-2','preview-agent-5'}
    assert sum(item['mode']=='idle' for item in indicators)==4
    changed=deepcopy(data)
    changed['main_usage']['request_state']='완료'
    for agent in changed['subagent_finance']['agents']:
        agent['request_state']='진행' if agent['id']=='preview-agent-1' else '완료'
    changed['active_requests']=1
    column_model.set_content(changed)
    assert column_model.state['mainHeartbeats'][0]['mode']=='idle'
    assert {groups[item['id']] for item in column_model.state['detailHeartbeats'] if item['mode']=='active'}=={'preview-agent-1'}
    assert column_model.rows()==data['recent']
    changed=deepcopy(changed)
    changed['main_usage'].pop('request_state')
    changed['main_usage'].pop('request',None)
    for agent in changed['subagent_finance']['agents']:
        agent.pop('request_state')
        agent.pop('request',None)
    column_model.set_content(changed)
    assert column_model.state['mainHeartbeats'][0]['mode']=='unknown'
    assert all(item['mode']=='unknown' for item in column_model.state['detailHeartbeats'])


def test_main_and_one_to_four_subagents_share_bottom_padding(column_model):
    from tools.preview_overlay import synthetic_snapshot
    from cachemonitor.overlay_data import main_usage
    gaps=[]
    for count in range(5):
        scenario=f'sub-{count}' if count else 'idle'
        data=next(row for row in synthetic_snapshot(scenario)['overlay_sessions'] if row['id']=='preview-main')
        column_model.set_content(data)
        column_model.visible_agent_counts[column_model.agent_count_key()]=4
        layout=column_model.layout()
        if count:
            items=column_model.detail_items();start=layout['sub_y']
        else:
            from cachemonitor.overlay_icons import agent_mark
            items=column_model.agent_summary_items(main_usage(data),'Main Agent',agent_mark(data['id'],column_model.appearance),False,276,main=True)
            start=layout['main']
        text_bottom=start+max(item[2]+item[4] for item in items if item[0])
        gaps.append(layout['height']-text_bottom)
    assert max(gaps)-min(gaps)<1


def test_automatic_height_fits_up_to_three_summaries(column_model):
    from tools.preview_overlay import synthetic_snapshot
    heights=[]
    bottom_gaps=[]
    for count in (1,2,3,4,8):
        data=next(row for row in synthetic_snapshot(f'sub-{count}')['overlay_sessions'] if row['id']=='preview-main')
        column_model.set_content(data)
        layout=column_model.layout()
        summaries=[item for item in column_model.detail_items() if item[8].get('action','').startswith('group-')]
        assert len(summaries)==count
        assert all(item[2]+item[4]<=layout['sub_height'] for item in summaries[:3])
        last_visible=summaries[min(count,3)-1]
        bottom_gaps.append(layout['sub_height']-last_visible[2]-last_visible[4])
        if count>3:assert summaries[3][2]+summaries[3][4]>layout['sub_height']
        heights.append(layout['height'])
    assert heights[0]<heights[1]<heights[2]==heights[3]==heights[4]
    assert max(bottom_gaps)-min(bottom_gaps)<1


@pytest.mark.parametrize('count',[1,8])
def test_subagent_disclosures_add_their_content_height_and_restore_on_close(column_model,count):
    from tools.preview_overlay import synthetic_snapshot
    m=column_model
    data=next(row for row in synthetic_snapshot(f'sub-{count}')['overlay_sessions'] if row['id']=='preview-main')
    m.set_content(data)
    closed=m.layout();body=m.state['detailBodyHeight']
    keys=[row['key'] for row in m.detail_groups()][-2:]
    for key in keys:
        m.toggle_detail_group(key)
        opened=m.layout()
        assert opened['height']>closed['height']
        assert opened['height']-closed['height']>=m.state['detailBodyHeight']-body
        assert opened['sub_height']-closed['sub_height']==pytest.approx(opened['height']-closed['height'])
        assert (opened['charts'],opened['main'],opened['sub_y'])==(closed['charts'],closed['main'],closed['sub_y'])
        if count<=m.visible_agent_count():assert opened['sub_height']>=m.state['detailBodyHeight']
    m.available_height=closed['height']+20
    assert m.base_height()==m.available_height
    assert m.state['detailBodyHeight']>m.layout()['sub_height']
    m.available_height=None
    assert m.layout()==opened
    for key in reversed(keys):m.toggle_detail_group(key)
    assert m.layout()==closed and m.state['detailBodyHeight']==body
    m.toggle_detail_group(keys[0])
    m.set_content(dict(data,id='other-conversation'))
    assert not m.expanded_groups and m.layout()==closed
    m.toggle_detail_group(m.detail_groups()[0]['key'])
    m.set_content(dict(m.data,descendants=0,subagent_finance={}))
    assert not m.expanded_groups and m.base_height()<closed['height']
    m.set_content(data)
    assert m.layout()==closed


def test_visible_count_is_scoped_and_survives_growth_retention_and_screen_constraints(column_controller,monkeypatch):
    from tools.preview_overlay import synthetic_snapshot
    c=column_controller;m=c.widget.content_model
    monkeypatch.setattr(c,'refresh',lambda:None)
    def data(count):return next(row for row in synthetic_snapshot(f'sub-{count}')['overlay_sessions'] if row['id']=='preview-main')
    m.set_content(data(4));automatic=m.base_height()
    for limit in (1,2,3,4):
        c.set_visible_agent_count(limit)
        layout=m.layout();summaries=[item for item in m.detail_items() if item[8].get('action','').startswith('group-')]
        assert all(item[2]+item[4]<=layout['sub_height'] for item in summaries[:limit])
        if limit<4:assert summaries[limit][2]+summaries[limit][4]>layout['sub_height']
    chosen=m.base_height()
    m.set_content(data(8))
    assert m.base_height()==chosen and m.visible_agent_count()==4
    m.available_height=620
    assert m.base_height()<chosen and m.visible_agent_count()==4
    m.available_height=None
    assert m.base_height()==chosen
    incomplete=dict(m.data,descendants=0,subagent_finance={},_collection=dict(loading=True))
    m.set_content(incomplete)
    assert len(m.detail_groups())==8 and m.base_height()==chosen
    m.set_content(data(1))
    assert m.visible_agent_count()==4 and m.base_height()<automatic
    other=dict(data(4),id='other-conversation')
    m.set_content(other)
    assert m.visible_agent_count()==3 and m.base_height()==automatic
    m.set_content(data(4))
    assert m.base_height()==chosen and m.visible_agent_count()==4
    solo=next(row for row in synthetic_snapshot('idle')['overlay_sessions'] if row['id']=='preview-main')
    m.set_content(solo)
    assert not m.has_subagents() and m.base_height()<automatic
    c.set_visible_agent_count(1)
    assert m.visible_agent_count()==4
    m.set_content(data(4))
    assert m.base_height()==chosen
    c.set_visible_agent_count(3)
    assert m.base_height()==automatic


def test_highlight_sample_matches_real_row_painter_for_every_style(column_model,monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_chrome import HighlightPreview
    from cachemonitor.overlay_view import Drawing,HighlightStyle
    clock=[1.]
    source=SimpleNamespace(monotonic=lambda:clock[0])
    monkeypatch.setattr('cachemonitor.overlay_view.time',source)
    monkeypatch.setattr('cachemonitor.overlay_chrome.time',source)
    m=column_model;preview=HighlightPreview(None);owner=('home','sample')
    preview._paint_width=220;preview._paint_height=32
    try:
        preview.set_visible(True)
        for reduced in (False,True):
            for style in HighlightStyle:
                m.highlight_style=style
                preview.configure(style,m.appearance,reduced);preview.play()
                for _ in range(3):
                    m.row_glows.active={owner:preview.glow}
                    clock[0]=(preview.glow.start_ms+120)/1000
                    images=[]
                    for sample in (False,True):
                        image=QImage(220,32,QImage.Format_ARGB32_Premultiplied);image.fill(0)
                        painter=QPainter(image)
                        try:
                            if sample:preview.paint(painter)
                            else:Drawing(painter,m).row_glow(0,0,220,32,owner=owner,color=preview.color)
                        finally:painter.end()
                        images.append(image)
                    assert images[0]==images[1]
                    assert any(images[0].pixelColor(x,y).alpha() for y in range(32) for x in range(220))
                    clock[0]=(preview.glow.start_ms+(300 if reduced else 800))/1000
                    preview.advance()
                assert preview.glow is None
    finally:preview.clear();preview.deleteLater()


@pytest.mark.parametrize('dark',[False,True])
@pytest.mark.parametrize('reduced',[False,True])
def test_highlight_sample_plays_exactly_three_distinct_icon_colors(column_model,monkeypatch,dark,reduced):
    from types import SimpleNamespace
    from cachemonitor.overlay_chrome import HighlightPreview
    from cachemonitor.overlay_icons import agent_palette
    from cachemonitor.overlay_view import HighlightStyle
    clock=[1.]
    monkeypatch.setattr('cachemonitor.overlay_chrome.time',SimpleNamespace(monotonic=lambda:clock[0]))
    preview=HighlightPreview(None);appearance=default_appearance(dark)
    colors=agent_palette(appearance)[:3]
    assert len(colors)==len(set(colors))==3
    try:
        preview.configure(HighlightStyle.SCAN,appearance,reduced)
        preview.play();assert preview.glow is None and not preview.timer.isActive()
        preview.set_visible(True)
        for color in colors:
            assert preview.color==color and preview.glow.reduced_motion is reduced
            assert preview.timer.isActive()
            end=preview.glow.start_ms+(300 if reduced else 800)
            clock[0]=(end-1)/1000;preview.advance()
            assert preview.color==color
            clock[0]=end/1000;preview.advance()
        assert preview.glow is None and not preview.timer.isActive()
        clock[0]+=10;preview.advance()
        assert preview.glow is None and not preview.timer.isActive()
    finally:preview.clear();preview.deleteLater()


def test_highlight_choice_replaces_running_sequence(column_controller,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.overlay_icons import agent_palette
    from cachemonitor.overlay_view import HighlightStyle
    clock=[1.]
    monkeypatch.setattr('cachemonitor.overlay_chrome.time',SimpleNamespace(monotonic=lambda:clock[0]))
    c=column_controller;preview=c.toolbar.view.highlightPreview
    preview.set_visible(True)
    colors=agent_palette(preview.appearance)[:3]
    clock[0]=1.8;preview.advance();assert preview.color==colors[1]
    clock[0]=1.9;c.choose_highlight_style(HighlightStyle.EDGE.key)
    assert c.highlight_style is HighlightStyle.EDGE
    assert preview.style is HighlightStyle.EDGE and preview.color==colors[0]
    assert preview.glow.start_ms==1900
    for color in colors:
        assert preview.color==color
        clock[0]=(preview.glow.start_ms+800)/1000;preview.advance()
    assert preview.glow is None and not preview.timer.isActive()
    preview.set_visible(False);preview.advance()
    assert preview.glow is None and not preview.timer.isActive()


def test_highlight_sample_reduced_motion_never_resumes_current_travel(column_model,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.overlay_chrome import HighlightPreview
    from cachemonitor.overlay_view import HighlightStyle
    clock=[1.]
    monkeypatch.setattr('cachemonitor.overlay_chrome.time',SimpleNamespace(monotonic=lambda:clock[0]))
    preview=HighlightPreview(None)
    try:
        preview.configure(HighlightStyle.SCAN,column_model.appearance,False)
        preview.set_visible(True);first=preview.glow
        assert first is not None and preview.timer.isActive()
        clock[0]=1.2;preview.configure(HighlightStyle.EDGE,column_model.appearance,False)
        assert preview.glow is first
        clock[0]=1.3;preview.configure(HighlightStyle.EDGE,column_model.appearance,True)
        stationary=preview.glow
        assert stationary.reduced_motion and stationary.start_ms==1300
        preview.configure(HighlightStyle.EDGE,column_model.appearance,False)
        assert preview.glow is stationary
        preview.set_visible(False);preview.advance()
        assert preview.glow is None and not preview.timer.isActive()
        preview.set_visible(True);assert preview.glow is not None
        preview.set_visible(False)
    finally:preview.deleteLater()
