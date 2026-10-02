"""View transitions use the same native anchor and one control per action."""
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay_tracking import Selection
from test_overlay import A, source


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
    OverlayController.receive_snapshot(state,{'errors':[]})
    assert state.snapshot_wall_time==200.


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


def test_popup_outside_click_escape_order_and_persisted_opacity(layout_controller):
    controller=layout_controller
    assert controller.opacity==94 and not controller.toolbar.isVisible()
    controller.toggle_expanded();controller.toggle_opacity()
    assert controller.popup_open and controller.toolbar.isVisible()
    assert controller.toolbar.size().width()==160 and controller.toolbar.size().height()==40
    controller.escape()
    assert not controller.popup_open and controller.expanded
    controller.escape()
    assert not controller.expanded and not controller.collapsed
    controller.escape()
    assert not controller.collapsed
    controller.toggle_opacity();controller.native.pointer=(0,0);controller.native.down=True
    controller.poll_popup()
    assert not controller.popup_open
    controller.set_opacity(63)
    assert controller.settings.value('overlay/opacity',type=int)==63




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


def test_remote_session_replaces_metrics_and_restores_local_detail(layout_controller):
    c=layout_controller;m=c.widget.content_model
    c.toggle_expanded();local_height=m.base_height()
    c.receive_target({'target':{'hwnd':1},'selection':Selection(A,host='remote:test')})
    assert m.is_remote and m.data is None and not m.rows()
    assert c.automatic_mode=='monitor' and not c.detail.isVisible()
    assert m.base_height()<local_height and not m.monitor_links()
    assert not c.actions.view.state['canExpand']
    assert m.lines()[0]=='원격 세션' and '0호출' not in m.state['accessible']
    c.toggle_expanded()
    assert c.expanded  # The local detail preference survives the remote card.
    c.receive_snapshot({'overlay_sessions':c.sessions})
    assert m.is_remote and m.data is None
    c.receive_target({'target':{'hwnd':1},'selection':Selection(A)})
    assert not m.is_remote and m.data['id']==A
    assert c.automatic_mode=='detail' and c.detail.isVisible()
    assert c.actions.view.state['canExpand'] and m.base_height()==local_height


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


@pytest.mark.parametrize('mode', ['monitor','detail','icon'])
@pytest.mark.parametrize('source', ['known','missing','remote'])
def test_drag_moves_without_content_refresh(layout_controller,monkeypatch,mode,source):
    c=layout_controller
    if source=='missing':c.receive_snapshot({'overlay_sessions':[]})
    if source=='remote':c.receive_target({'target':{'hwnd':1},'selection':Selection(A,host='remote')})
    c.expanded=mode=='detail';c.collapsed=mode=='icon';c.refresh()
    def unexpected(*args,**kwargs):pytest.fail('Pure drag refreshed content')
    monkeypatch.setattr(c.widget,'set_content',unexpected)
    c.begin_drag((500,500));before=c.current_geometry
    c.move_drag((480,480))
    assert c.current_geometry[:2]==(before[0]-20,before[1]-20)
    c.cancel_drag()


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
    c=layout_controller;c.appearance=replace(c.appearance,font_size=font_size)
    monkeypatch.setattr(c.native.u,'GetDpiForWindow',lambda hwnd:dpi)
    c.native.bounds=(0,0,2400,2400);c.refresh()
    m=c.widget.content_model;factor=dpi/96;gap=round(16*factor)
    full=round(m.monitor_height(False)*factor);compact=round(m.monitor_height(True)*factor)
    width=round(700*c.appearance.scale*factor)+2*gap;anchor=c.anchor
    for height,reduced,icon in ((full,False,False),(full-1,True,False),(compact,True,False),(compact-1,True,True),(full,False,False)):
        c.native.bounds=(0,0,width,height+2*gap);c.refresh()
        assert (c.automatic_mode=='icon')==icon
        assert m.compact==reduced and c.anchor==anchor and not c.collapsed
    c.expanded=True;c.native.bounds=(0,0,round(350*c.appearance.scale*factor)+2*gap,full+2*gap);c.refresh()
    assert c.automatic_mode=='detail-inline'
