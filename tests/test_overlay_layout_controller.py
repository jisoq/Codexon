"""View transitions use the same native anchor and one control per action."""
import pytest

pytestmark = pytest.mark.integration
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay_tracking import Selection
from test_overlay import A, B, source


class Native:
    class API:
        def GetDpiForWindow(self, hwnd): return 96
    u=API()
    bounds=(0,0,1300,1000)
    down=False
    pointer=(0,0)
    visible=True
    def __init__(self): self.placed={};self.styles={};self.companions=set()
    def visible_target(self, hwnd): return self.visible
    def frame(self, hwnd): return self.bounds
    def configure(self, hwnd, click_through=True): self.styles[hwnd]=click_through
    def set_companions(self, handles): self.companions=set(handles)
    def place(self, hwnd, box): self.placed[hwnd]=box;return True
    def primary_down(self): return self.down
    def cursor(self): return self.pointer
    def reduce_motion(self): return True


def test_collection_error_does_not_advance_last_confirmed_time(monkeypatch):
    from types import SimpleNamespace
    from cachemonitor import overlay
    state=SimpleNamespace(snapshot_wall_time=100.,refresh=lambda:None)
    monkeypatch.setattr(overlay.time,'time',lambda:200.)
    OverlayController.receive_snapshot(state,{'errors':['collection failed']})
    assert state.snapshot_wall_time==100.
    assert state.errors==['collection failed']
    OverlayController.receive_snapshot(state,{'errors':[],'ts':150.,'last_usage_collection_success':140.})
    assert state.snapshot_wall_time==140.
    OverlayController.receive_snapshot(state,{'errors':[],'ts':150.,'last_usage_collection_success':140.})
    assert state.snapshot_wall_time==140.  # Re-reading old results is not new collection.


@pytest.fixture
def layout_controller(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    controller=OverlayController(QSettings(str(tmp_path/'overlay.ini'),QSettings.IniFormat),native_enabled=False)
    # Keep mouse-driven companion activation deterministic on the QA desktop.
    for control in controller.chrome:
        original=control.activateWindow
        def activate(control=control,original=original):
            original();app.processEvents()
            app.setActiveWindow(control)
        monkeypatch.setattr(control,'activateWindow',activate)
    controller.native=Native()
    controller.appearance_reader.read=lambda dark:controller.appearance
    engine=AnalysisEngine();engine.ingest([source()])
    controller.receive_snapshot({'overlay_sessions':OverlaySummaries().collect(engine)})
    controller.receive_target({'target':{'hwnd':1},'selection':Selection(A)})
    yield controller
    controller.stop();app.processEvents()


def publish_subagents(controller, *, nested=False):
    parent=source();sessions=[parent]
    for sid,parent_id in ((B,A),('grandchild',B)) if nested else ((B,A),):
        child=Session(sid,'home',title='하위 작업')
        child.add_usage(103,'sub-'+sid,dict(input_tokens=100,cached_input_tokens=20,
            cache_write_input_tokens=0,output_tokens=10),'gpt-6-astra','turn','high','default')
        value=child.view(105)
        value.update(source='subagent',parent_thread_id=parent_id,archived=False)
        sessions.append(value)
    engine=AnalysisEngine();engine.ingest(sessions)
    controller.receive_snapshot({'overlay_sessions':OverlaySummaries().collect(engine)})
    return parent


def test_selected_settings_button_still_changes_on_hover():
    from PySide6.QtCore import QPoint,QPointF,QRect,QSize,QEvent,Qt
    from PySide6.QtGui import QEnterEvent,QMouseEvent
    from cachemonitor.overlay_chrome import OverlayChrome,named_item
    from tests.test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([])
    chrome=OverlayChrome('actions');chrome.resize(78,30);chrome.move(100,100)
    chrome.view.put(popupOpen=True,popupKind='settings',reducedMotion=True)
    try:
        chrome.show();chrome.raise_();chrome.activateWindow()
        wait_for_rendered(lambda:chrome.quick is not None and chrome.quick.rootObject() is not None)
        button=named_item(chrome.quick.rootObject(),'settingsButton')
        center=button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint()
        top=button.mapToScene(QPointF(0,0)).toPoint()
        rect=QRect(top,QSize(round(button.width()),round(button.height())))
        def hover(point):
            local=QPointF(point);global_pos=QPointF(chrome.quick.mapToGlobal(point))
            app.sendEvent(chrome.quick,QEnterEvent(local,local,global_pos))
            app.sendEvent(chrome.quick,QMouseEvent(QEvent.MouseMove,local,global_pos,Qt.NoButton,Qt.NoButton,Qt.NoModifier))
        hover(QPoint(chrome.quick.width()-1,1))
        wait_for_rendered(lambda:not button.property('hovered'))
        before=chrome.quick.grabFramebuffer().copy(rect)
        hover(center)
        wait_for_rendered(lambda:button.property('hovered'))
        wait_for_rendered(lambda:chrome.quick.grabFramebuffer().copy(rect)!=before)
        assert button.property('selected')
    finally:chrome.close();app.processEvents()

def assert_embedded_subagents(controller):
    from cachemonitor.overlay_monitor import MARGIN,SECTION_WIDTH
    c=controller;m=c.widget.content_model;layout=m.layout()
    x,y,w,h=c.monitor_geometry
    factor=c.native.u.GetDpiForWindow(1)/96*m.appearance.scale
    detail=c.native.placed[int(c.detail.winId())]
    assert detail==(x+round(MARGIN*factor),y+round(layout['sub_y']*factor),
                    round(SECTION_WIDTH*factor),round(layout['sub_height']*factor))
    assert detail[0]>=x and detail[1]>=y
    assert detail[0]+detail[2]<=x+w and detail[1]+detail[3]<=y+h
    assert c.automatic_mode in ('monitor','compact') and c.detail.isVisible()


def compact_frame(controller):
    from cachemonitor.overlay_monitor import geometry,header_status
    m=controller.widget.content_model
    target=controller.target_state.get('target',{})
    factor=(controller.native.u.GetDpiForWindow(target.get('hwnd',1)) or 96)/96
    height=geometry(m.data,has_status=bool(header_status(m)[0]),preferred_height=0,
                    main_detail_height=m.main_detail_height(),auto_sub_height=m.automatic_sub_height())['height']
    full=round(round(height*m.appearance.scale)*factor)
    return (0,0,1300,full-1+2*round(16*factor))


def test_subagent_viewport_tracks_family_without_main_disclosure(layout_controller):
    from cachemonitor.overlay_chrome import named_item
    c=layout_controller;m=c.widget.content_model
    button=named_item(c.actions.quick.rootObject(),'settingsButton')
    assert button.isVisible() and named_item(c.actions.quick.rootObject(),'expand') is None
    assert c.automatic_mode=='monitor' and not c.detail.isVisible()
    parent=publish_subagents(c,nested=True)
    assert m.data['descendants']==2 and not m.main_expanded
    assert_embedded_subagents(c)
    engine=AnalysisEngine();engine.ingest([parent])
    c.receive_snapshot({'overlay_sessions':OverlaySummaries().collect(engine)})
    assert c.automatic_mode=='monitor' and not c.detail.isVisible()
    publish_subagents(c)
    assert_embedded_subagents(c)
    c.receive_target({'target':{'hwnd':1},'selection':Selection(B)})
    assert not c.detail.isVisible()
    c.receive_target({'target':{'hwnd':1},'selection':Selection(A)})
    assert_embedded_subagents(c)


def test_subagent_companion_uses_only_lower_viewport(layout_controller):
    from cachemonitor.overlay_chrome import named_item
    c=layout_controller;publish_subagents(c,nested=True)
    c.anchor=(.6,1);c.refresh()
    assert_embedded_subagents(c)
    root=c.detail.quick.rootObject();m=c.widget.content_model
    assert named_item(root,'closeDetail') is None
    assert named_item(root,'openDashboard') is None
    assert named_item(root,'detailCostAmount-count') is None
    assert named_item(root,'detailCostAmount-main') is None
    assert named_item(root,'detailScroll').height()==c.detail.height()
    assert {row['id'] for row in m.detail_groups()}=={B,'grandchild'}


def test_main_disclosure_changes_height_and_preserves_right_anchor(layout_controller):
    c=layout_controller;publish_subagents(c);m=c.widget.content_model
    c.native.bounds=(0,0,1300,2400)
    c.anchor=(1,1);c.refresh()
    closed=c.monitor_geometry;anchor=c.anchor
    before=m.layout();m.monitor_action('main-detail');opened=c.monitor_geometry
    assert m.main_expanded and m.main_detail_height()>0
    assert opened[0]==closed[0] and opened[2]==closed[2]
    assert opened[1]+opened[3]==closed[1]+closed[3]
    assert opened[3]-closed[3]==round(m.main_detail_height()*m.appearance.scale)
    assert m.layout()['sub_y']-before['sub_y']==m.main_detail_height()
    assert c.anchor==anchor
    assert_embedded_subagents(c)
    c.toggle_expanded()
    assert not m.main_expanded and c.monitor_geometry==closed


def test_visible_count_changes_only_subagent_allowance_and_persists_chart_style(layout_controller):
    c=layout_controller;publish_subagents(c,nested=True);m=c.widget.content_model
    c.native.bounds=(0,0,1300,2400);c.refresh()
    c.set_visible_agent_count(1)
    before=m.layout();anchor=c.anchor
    c.set_visible_agent_count(2)
    after=m.layout()
    assert after['charts']==before['charts'] and after['main']==before['main']
    assert after['sub_y']==before['sub_y'] and after['sub_height']>before['sub_height']
    assert after['height']-before['height']==after['sub_height']-before['sub_height'] and c.anchor==anchor
    import json
    assert json.loads(c.settings.value('overlay/visibleAgentCounts'))[c.widget.content_model.agent_count_key()]==2
    assert_embedded_subagents(c)
    c.set_chart_style('filled-line')
    assert m.chart_style=='filled-line' and c.settings.value('overlay/chartStyle')=='filled-line'


def test_equal_heartbeats_keep_watchdog_and_restore_hidden_overlay(layout_controller,monkeypatch):
    c=layout_controller;now=[100.]
    monkeypatch.setattr('cachemonitor.overlay.time.monotonic',lambda:now[0])
    c.observed_at=c.snapshot_at=now[0];c.next_theme=0;c.refresh()
    state=dict(c.target_state)
    with monkeypatch.context() as stable:
        def unexpected():pytest.fail('Stable heartbeat refreshed overlay content')
        stable.setattr(c,'content',unexpected)
        for elapsed in (1.,2.,3.):
            now[0]=100.+elapsed
            c.receive_target(dict(state));c._tick()
            assert c.widget.isVisible()
        hwnd=int(c.widget.winId());c.native.placed[hwnd]=(0,0,1,1)
        c._tick()
        assert c.native.placed[hwnd]==c.current_geometry
    now[0]+=3.01;c._tick()
    assert not c.widget.isVisible() and not c.icon.isVisible()
    c.receive_target(dict(state))
    assert c.widget.isVisible()
    c.native.visible=False;c._tick()
    assert not c.widget.isVisible()
    c.native.visible=True;c.receive_target(dict(state))
    assert c.widget.isVisible()


def test_compact_detail_loading_preserves_visible_summary_and_reuses_rows(layout_controller):
    from copy import deepcopy
    c=layout_controller;app=QApplication.instance();m=c.widget.content_model
    publish_subagents(c)
    complete=deepcopy(next(value for value in c.sessions if value['id']==A))
    complete.update(_prepared_token=('detail-test',1),_overlay_revision=('detail-test',1),_recent_refs=False)
    recent={key:value for key,value in complete.items() if key!='subagent_finance'}
    compact=dict(recent,_recent_refs=True,recent=[{'key':row['id']} for row in recent['recent']],
                 latest={'key':recent['latest']['id']})
    c._overlay_detail=recent
    requests=[];c.overlay_loader=lambda request,data,**options:requests.append((request,data,options))
    c.receive_snapshot({'overlay_sessions':[compact]})
    c.native.bounds=compact_frame(c)
    c.refresh();app.processEvents()
    assert m.compact and len(requests)==1
    before=(m.data['cost'],deepcopy(m.rows()))
    assert c.detail.isVisible() and c.widget.isVisible() and m.compact
    assert (m.data['cost'],m.rows())==before
    assert not requests[-1][1]['_recent_refs'] and requests[-1][2]['finance']
    assert m.detail_items()
    c.receive_overlay_detail(dict(id=requests[-1][0],summary=complete,prepared_token=complete['_prepared_token']))
    app.processEvents()
    assert m.detail_data()==complete['subagent_finance'] and (m.data['cost'],m.rows())==before
    c.refresh();c.refresh();app.processEvents()
    assert c.detail.isVisible() and len(requests)==1
    assert_embedded_subagents(c)


def test_maintenance_updates_snapshot_delay_and_displayed_age(layout_controller,monkeypatch):
    c=layout_controller;now=[100.];m=c.widget.content_model
    monkeypatch.setattr('cachemonitor.overlay.time.monotonic',lambda:now[0])
    c.observed_at=c.snapshot_at=now[0];c.refresh()
    now[0]=115.;c.receive_target(dict(c.target_state));c._tick()
    assert not m.note
    now[0]=115.01;c.receive_target(dict(c.target_state));c._tick()
    assert m.note=='갱신 중' and m.data['_collection']['reason']=='analysis_delay'
    assert m.heartbeat_state()['mode']=='pending'
    previous=m.freshness_text()
    now[0]=116.01;c.receive_target(dict(c.target_state));c._tick()
    assert m.freshness_text()!=previous
    assert m.data['_collection']['received_age']==pytest.approx(16.01)
    c.receive_snapshot({'overlay_sessions':c.sessions})
    assert not m.note


def test_maintenance_detects_collection_timeout_and_service_recovery(layout_controller,monkeypatch):
    c=layout_controller;now=[100.];m=c.widget.content_model
    monkeypatch.setattr('cachemonitor.overlay.time.time',lambda:now[0])
    c.collection_started_at=1.
    c.receive_collection_status(dict(collection_issue='',collection_fault={},collection_snapshot_at=100.))
    c.receive_target(dict(c.target_state));c._tick()
    now[0]=130.
    c.receive_target(dict(c.target_state));c._tick()
    assert not m.note
    now[0]=130.01
    c.receive_target(dict(c.target_state));c._tick()
    assert m.note=='수집 지연' and m.heartbeat_state()['mode']=='error'
    c.receive_collection_status(dict(collection_snapshot_at=now[0]))
    assert not m.note and m.heartbeat_state()['mode']!='error'


def test_maintenance_recovers_frame_dpi_and_changed_appearance(layout_controller,monkeypatch):
    from dataclasses import replace
    c=layout_controller
    c.native.bounds=(40,30,1940,2130)
    monkeypatch.setattr(c.native.u,'GetDpiForWindow',lambda hwnd:144)
    c.receive_target(dict(c.target_state))
    assert c._placement_context==(c.native.bounds,144)
    assert c.native.placed[int(c.widget.winId())]==c.current_geometry
    assert c.current_geometry[0]>=c.native.bounds[0]
    assert c.current_geometry[0]+c.current_geometry[2]<=c.native.bounds[2]
    width=c.current_geometry[2]
    appearance=replace(c.appearance,font_size=21)
    c.appearance_reader.read=lambda dark:appearance;c.next_theme=0
    c._tick()
    assert c.widget.content_model.appearance==appearance
    assert c.current_geometry[2]>width


@pytest.mark.parametrize('compact',[False,True])
def test_pending_overlay_refresh_keeps_confirmed_panel_until_replacement(layout_controller,monkeypatch,compact):
    from copy import deepcopy
    from test_ui import wait_for_rendered
    import time
    c=layout_controller;app=QApplication.instance();m=c.widget.content_model
    confirmed=deepcopy(c.sessions[0])
    confirmed.update(_prepared_token=('fixture',1),_overlay_revision=('fixture',1),_recent_refs=False)
    c._overlay_detail=confirmed
    requests=[]
    c.overlay_loader=lambda request,data,**kwargs:requests.append((request,data))
    def publication(detail):
        return dict(detail,_recent_refs=True,recent=[{'key':row['id']} for row in detail['recent']],
                    latest={'key':detail['latest']['id']})
    def publish(detail,stamp):
        c.receive_snapshot(dict(overlay_sessions=[publication(detail)],ts=stamp))
        app.processEvents()
    stamp=time.time();publish(confirmed,stamp)
    if compact:c.native.bounds=compact_frame(c)
    m.monitor_tab='history';m.pin_call(m.call_id(m.rows()[0]));c.refresh();app.processEvents()
    assert m.compact is compact
    selected=m.selected_id;geometry=c.monitor_geometry
    changed=deepcopy(confirmed)
    changed.update(_prepared_token=('fixture',2),_overlay_revision=('fixture',2),cost=confirmed['cost']+1)
    changed['recent'][-1]['output']+=10;changed['latest']=changed['recent'][-1]
    publish(changed,stamp+1)
    assert m.data and m.data['cost']==confirmed['cost']
    assert m.data['latest']['output']==confirmed['latest']['output']
    assert m.data['_collection']['last_confirmed_at']==stamp
    assert m.selected_id==selected and m.graph_pinned
    assert c.monitor_geometry==geometry
    assert m.note=='갱신 중' and m.heartbeat_state()['mode']=='pending'
    # The arrival highlight may still be fading while confirmed values stay
    # unchanged. Compare rendered content only after that expected animation.
    wait_for_rendered(lambda:m._agent_highlight.state()==m._agent_highlight.State.Stopped,timeout=5)
    before=c.widget.quick.grabFramebuffer()
    assert m.notice_card() is None and len(requests)==1
    # Rejected obsolete reads must not erase the still-valid displayed snapshot.
    c.receive_overlay_detail(dict(id=requests[-1][0],kind='stale'))
    app.processEvents()
    assert c.widget.quick.grabFramebuffer()==before and m.selected_id==selected
    assert m.data['cost']==confirmed['cost'] and m.data['latest']==confirmed['latest']
    # A stalled detail refresh must disclose the age of the retained values.
    with monkeypatch.context() as clock:
        clock.setattr('cachemonitor.overlay.time.time',lambda:stamp+20)
        c._tick()
        assert m.data['cost']==confirmed['cost'] and m.note=='갱신 중'
        assert m.heartbeat_state()['mode']=='pending'
        assert m.data['_collection']['last_confirmed_at']==stamp and m.freshness_text()
    changed['_prepared_token']=('fixture',3);publish(changed,stamp+2)
    c.receive_overlay_detail(dict(id=requests[-1][0],summary=changed,prepared_token=changed['_prepared_token']))
    app.processEvents()
    assert m.data['cost']==changed['cost'] and m.data['latest']['output']==changed['latest']['output']
    assert m.data['_collection']['last_confirmed_at']==stamp+2
    assert m.selected_id==selected and m.graph_pinned and m.notice_card() is None
    # Selecting another chat must never display this chat's retained numbers.
    other=dict(publication(changed),id='another-chat')
    c.receive_snapshot(dict(overlay_sessions=[other],ts=stamp+3))
    c.receive_target(dict(target={'hwnd':1},selection=Selection('another-chat')))
    assert m.notice_card() and not m.rows() and not m.selected_id


def test_popup_outside_click_escape_order_and_persisted_opacity(layout_controller):
    controller=layout_controller
    publish_subagents(controller)
    assert controller.opacity==94 and not controller.toolbar.isVisible()
    controller.toggle_expanded();controller.toggle_opacity()
    assert controller.popup_open and controller.toolbar.isVisible()
    assert controller.toolbar.size().width()==160 and controller.toolbar.size().height()==40
    controller.escape()
    assert not controller.popup_open and controller.widget.content_model.main_expanded
    controller.escape()
    assert not controller.widget.content_model.main_expanded and not controller.collapsed
    controller.escape()
    assert not controller.collapsed
    controller.toggle_opacity();controller.native.pointer=(0,0);controller.native.down=True
    controller.poll_popup()
    assert not controller.popup_open
    controller.set_opacity(63)
    assert controller.settings.value('overlay/opacity',type=int)==63
    controller.toggle_settings()
    assert controller.popup_open and controller.popup_kind=='settings' and controller.toolbar.isVisible()
    from cachemonitor.overlay_chrome import named_item
    from PySide6.QtCore import QPointF
    count=named_item(controller.toolbar.quick.rootObject(),'agentCount-4')
    assert count.isVisible()
    assert controller.toolbar.quick.rect().contains(count.mapToScene(QPointF(count.width(),count.height())).toPoint())
    controller.escape()
    assert not controller.popup_open and not controller.collapsed




def test_session_collapse_isolated_and_persistent(layout_controller):
    from copy import deepcopy
    c=layout_controller
    original=deepcopy(c.sessions[0]);other=deepcopy(original);other['id']='other-session'
    c.receive_snapshot({'overlay_sessions':[original,other]})
    c.set_collapsed(True)
    first_key=c._session_collapse_key()
    c.receive_target({'target':{'hwnd':1},'selection':Selection(other['id'])})
    assert not c.collapsed
    c.receive_target({'target':{'hwnd':1},'selection':Selection(original['id'])})
    assert c.collapsed
    c._collapse_scope=None;c.collapsed=False;c.refresh()
    assert c.collapsed and c.settings.value(first_key,False,type=bool)
    c.set_collapsed(False)
    c.receive_target({'target':{'hwnd':1},'selection':Selection(other['id'])})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(original['id'])})
    assert not c.collapsed
    other_home=deepcopy(original);other_home['home']='different-home'
    c.receive_snapshot({'overlay_sessions':[other_home]})
    assert not c.collapsed and c._session_collapse_key()!=first_key


def test_remote_session_uses_icon_and_restores_local_detail(layout_controller):
    c=layout_controller;m=c.widget.content_model
    publish_subagents(c)
    c.toggle_expanded();local_height=m.base_height()
    c.receive_target({'target':{'hwnd':1},'selection':Selection(A,host='remote:test')})
    assert m.is_remote and m.data is None and not m.rows()
    assert c.automatic_mode=='icon' and c.icon.isVisible() and not c.widget.isVisible() and not c.detail.isVisible()
    assert m.base_height()<local_height and all(link.get('interaction')=='tooltip' for link in m.monitor_links())
    assert m.main_expanded  # Main disclosure survives the temporary remote icon.
    c.receive_snapshot({'overlay_sessions':c.sessions})
    assert m.is_remote and m.data is None
    c.receive_target({'target':{'hwnd':1},'selection':Selection(A)})
    assert not m.is_remote and m.data['id']==A
    assert m.main_expanded and m.base_height()==local_height
    assert_embedded_subagents(c)


def test_session_collapse_reloads_from_fresh_controller(layout_controller):
    from copy import deepcopy
    c=layout_controller;c.set_collapsed(True);c.settings.sync()
    restored=OverlayController(QSettings(c.settings.fileName(),QSettings.IniFormat),native_enabled=False)
    try:
        restored.native=Native();restored.appearance_reader.read=lambda dark:restored.appearance
        restored.receive_snapshot({'overlay_sessions':deepcopy(c.sessions)})
        restored.receive_target({'target':{'hwnd':1},'selection':Selection(A)})
        assert restored.collapsed and restored.automatic_mode=='icon'
    finally:restored.stop()


@pytest.mark.parametrize('mode', ['monitor','main-expanded','icon'])
@pytest.mark.parametrize('source', ['known','missing','remote'])
def test_drag_moves_without_content_refresh(layout_controller,monkeypatch,mode,source):
    import time
    c=layout_controller
    # Leave movement room above the expanded fixed Main section.
    c.native.bounds=(0,0,1300,1600)
    if mode=='main-expanded' and source=='known':publish_subagents(c)
    if source=='missing':c.receive_snapshot({'overlay_sessions':[]})
    if source=='remote':c.receive_target({'target':{'hwnd':1},'selection':Selection(A,host='remote')})
    if mode=='main-expanded':c.widget.content_model.monitor_action('main-detail')
    # The synthetic target has no heartbeat worker while Qt builds its scene.
    c.observed_at=time.monotonic()
    c.collapsed=mode=='icon';c.refresh()
    def unexpected(*args,**kwargs):pytest.fail('Pure drag refreshed content')
    monkeypatch.setattr(c.widget,'set_content',unexpected)
    c.observed_at=time.monotonic()
    c.begin_drag((500,500));before=c.current_geometry
    assert c.drag_valid()
    c.move_drag((480,480))
    assert c.current_geometry[:2]==(before[0]-20,before[1]-20)
    c.cancel_drag()


def test_main_disclosure_without_subagents_keeps_drag_lightweight(layout_controller,monkeypatch):
    c=layout_controller;c.native.bounds=(0,0,550,1000)
    c.widget.content_model.monitor_action('main-detail');c.refresh()
    assert c.automatic_mode=='monitor' and not c.detail.isVisible()
    def unexpected(*args,**kwargs):pytest.fail('Hidden detail preference refreshed content while dragging')
    monkeypatch.setattr(c.widget,'set_content',unexpected)
    c.begin_drag((500,500));before=c.current_geometry
    c.move_drag((480,480))
    assert c.current_geometry[:2]==(before[0]-20,before[1]-20)
    c.cancel_drag()


def test_restore_recovers_summary_after_main_detail_exceeds_short_frame(layout_controller):
    c=layout_controller;publish_subagents(c);m=c.widget.content_model
    c.native.bounds=(0,0,1300,660);c.refresh()
    assert c.widget.isVisible() and c.detail.isVisible() and not m.main_expanded
    m.monitor_action('main-detail')
    assert c.automatic_mode=='icon' and c.icon.isVisible() and not c.collapsed
    c.icon.restore.emit()
    assert not m.main_expanded and c.widget.isVisible() and c.detail.isVisible()
    assert c.automatic_mode!='icon' and not c.collapsed
    c.set_collapsed(True)
    c.native.bounds=(0,0,1300,1200);c.refresh()
    assert c.collapsed and c.icon.isVisible()


def test_drag_coalesces_positions_and_applies_latest_snapshot_on_release(layout_controller,monkeypatch):
    from copy import deepcopy
    c=layout_controller;app=QApplication.instance();before=c.current_geometry
    original=c.widget.set_content;updates=[]
    def record(*args,**kwargs):updates.append(args[0]);return original(*args,**kwargs)
    monkeypatch.setattr(c.widget,'set_content',record)
    c.begin_drag((500,500))
    for i in range(1,21):
        data=deepcopy(c.sessions[0]);data['title']=f'update {i}'
        c.receive_snapshot({'overlay_sessions':[data]})
        c.queue_drag((500-i,500-i))
    assert not updates and c.current_geometry==before
    app.processEvents()
    assert c.current_geometry[:2]==(before[0]-20,before[1]-20)
    c.queue_drag((450,450));c.end_drag((460,460))
    final=c.current_geometry
    assert final[:2]==(before[0]-40,before[1]-40)
    assert len(updates)==1 and updates[0]['title']=='update 20'
    app.processEvents()
    assert c.current_geometry==final and not c.drag_timer.isActive()


@pytest.mark.parametrize('reason',['hidden','stale','selection','capture'])
def test_drag_cancellation_discards_pending_position(layout_controller,reason):
    import time
    from PySide6.QtCore import QEvent,QPoint
    c=layout_controller;c.begin_drag((500,500));c.queue_drag((400,400));before=c.anchor
    if reason=='hidden':c.native.visible=False;c.refresh()
    elif reason=='stale':c.observed_at=time.monotonic()-4;c.refresh()
    elif reason=='selection':c.receive_target({'target':{'hwnd':1},'selection':Selection('another')})
    else:
        c.header.press=QPoint(0,0)
        QApplication.sendEvent(c.header,QEvent(QEvent.UngrabMouse))
    QApplication.processEvents()
    assert c.drag_context is None and not c.drag_timer.isActive() and c.anchor==before


@pytest.mark.parametrize('dpi',[96,144])
def test_drag_layout_change_keeps_deferred_content(layout_controller,monkeypatch,dpi):
    c=layout_controller;c.begin_drag((500,500))
    def unexpected(*args,**kwargs):pytest.fail('Layout applied deferred content')
    monkeypatch.setattr(c.widget,'set_content',unexpected)
    c.native.bounds=(0,0,1200,900)
    monkeypatch.setattr(c.native.u,'GetDpiForWindow',lambda hwnd:dpi)
    c.refresh()
    c.move_drag((480,480))
    assert c._placement_context==(c.native.bounds,dpi)
    assert c.current_geometry[0]+c.current_geometry[2]<=1184
    c.cancel_drag()








@pytest.mark.parametrize('dpi',[96,120,144,192])
@pytest.mark.parametrize('font_size',[14,21])
def test_fallback_boundaries_preserve_anchor(layout_controller,monkeypatch,dpi,font_size):
    from dataclasses import replace
    from cachemonitor.overlay_layout import WIDTH,MIN_SUB_HEIGHT
    from cachemonitor.overlay_monitor import geometry
    c=layout_controller;publish_subagents(c);c.appearance=replace(c.appearance,font_size=font_size)
    monkeypatch.setattr(c.native.u,'GetDpiForWindow',lambda hwnd:dpi)
    c.native.bounds=(0,0,2400,2400);c.refresh()
    m=c.widget.content_model;factor=dpi/96;gap=round(16*factor);automatic=m.base_height()
    from cachemonitor.overlay_monitor import header_status
    has_status=bool(header_status(m)[0])
    auto_height=m.automatic_sub_height()
    full=round(round(geometry(m.data,has_status=has_status,preferred_height=0,auto_sub_height=auto_height)['height']*c.appearance.scale)*factor)
    compact=round(round(geometry(m.data,True,has_status=has_status,preferred_height=0,auto_sub_height=auto_height)['height']*c.appearance.scale)*factor)
    width=round(WIDTH*c.appearance.scale*factor)+2*gap;anchor=c.anchor
    for height,reduced,icon in ((full,False,False),(full-1,True,False),(compact,True,False),(compact-1,True,True),(full,False,False)):
        c.native.bounds=(0,0,width,height+2*gap);c.refresh()
        assert (c.automatic_mode=='icon')==icon
        assert m.compact==reduced and c.anchor==anchor and not c.collapsed
        if not icon:
            assert m.layout()['sub_height']>=min(MIN_SUB_HEIGHT,auto_height)
            assert_embedded_subagents(c)
    c.native.bounds=(0,0,width,round(720*c.appearance.scale*factor)+2*gap);c.refresh()
    assert c.automatic_mode=='monitor' and m.base_height()==automatic
    c.native.bounds=(0,0,width-1,2400);c.refresh()
    assert c.automatic_mode=='icon' and c.anchor==anchor
