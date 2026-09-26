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
    # Test cross-window Qt keyboard routing without requiring foreground rights
    # in the hosted runner's Windows service session. Native placement is still
    # exercised by test_native_companion_keyboard_routes below.
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


def check_keyboard_focus_path(layout_controller):
    from PySide6.QtCore import Qt
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    controller=layout_controller
    controller.toggle_expanded()
    def item(control,name):return control.quick.rootObject().findChild(QQuickItem,name)
    def key(control,value,modifier=Qt.NoModifier):
        QTest.keyClick(control.quick,value,modifier);QTest.qWait(20)
    controller.focus_control('expand')
    key(controller.actions,Qt.Key_Tab)
    graph=item(controller.detail,'detailGraph')
    body=item(controller.detail,'detailScroll')
    assert graph.hasActiveFocus() and graph.property('keyboardFocus')
    key(controller.detail,Qt.Key_Tab)
    assert body.hasActiveFocus() and body.property('keyboardFocus')
    key(controller.detail,Qt.Key_End)
    assert body.property('contentY')==max(0,body.property('contentHeight')-body.height())
    assert body.hasActiveFocus()
    from cachemonitor.overlay_chrome import named_item
    for link in controller.detail.view.state['detailLinks']:
        key(controller.detail,Qt.Key_Tab)
        assert named_item(controller.detail.quick.rootObject(),'evidence-'+link['id']).hasActiveFocus()
    key(controller.detail,Qt.Key_Tab)
    assert item(controller.detail,'openDashboard').hasActiveFocus()
    key(controller.detail,Qt.Key_Tab)
    assert item(controller.actions,'opacityButton').hasActiveFocus()
    key(controller.actions,Qt.Key_Space)
    assert controller.popup_open and item(controller.toolbar,'opacity').hasActiveFocus()
    key(controller.toolbar,Qt.Key_Escape)
    assert not controller.popup_open and controller.expanded
    assert item(controller.actions,'opacityButton').hasActiveFocus()
    key(controller.actions,Qt.Key_Tab,Qt.ShiftModifier)
    assert body.hasActiveFocus()
    key(controller.detail,Qt.Key_Tab,Qt.ShiftModifier)
    assert graph.hasActiveFocus()
    key(controller.detail,Qt.Key_Tab,Qt.ShiftModifier)
    assert item(controller.actions,'expand').hasActiveFocus()
    key(controller.actions,Qt.Key_Tab,Qt.ShiftModifier)
    assert item(controller.header,'dragTitle').hasActiveFocus()
    key(controller.header,Qt.Key_Tab)
    assert item(controller.actions,'expand').hasActiveFocus()
    key(controller.actions,Qt.Key_Escape)
    assert not controller.expanded and item(controller.actions,'expand').hasActiveFocus()
    assert not any(control.qml_errors for control in controller.chrome)


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


@pytest.mark.parametrize('destination',['opacityButton','restore'])
@pytest.mark.parametrize('tab_activation',[False,True])
def test_delayed_window_activation_preserves_keyboard_destination(layout_controller,destination,tab_activation):
    from PySide6.QtCore import Qt,QEvent
    from PySide6.QtGui import QFocusEvent
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_chrome import named_item
    c=layout_controller
    if destination=='restore':c.set_collapsed(True)
    c.focus_control(destination);QTest.qWait(20)
    control=c.icon if destination=='restore' else c.actions
    item=named_item(control.quick.rootObject(),destination)
    # Native activation can arrive after the cross-window Tab request. The
    # widget's default FocusIn handler then picks its first QML tab stop.
    app=QApplication.instance()
    app.sendEvent(control.quick,QFocusEvent(QEvent.FocusOut,Qt.ActiveWindowFocusReason))
    app.sendEvent(control.quick,QFocusEvent(QEvent.FocusIn,Qt.TabFocusReason if tab_activation else Qt.ActiveWindowFocusReason))
    QTest.qWait(20)
    assert item.hasActiveFocus()
    assert item.property('keyboardFocus' if destination=='restore' else 'visualFocus')
    if destination=='restore':assert named_item(control.quick.rootObject(),'moveFocusRing').isVisible()


def test_queued_keyboard_focus_cannot_override_new_target_or_hidden_window(layout_controller):
    from PySide6.QtCore import Qt,QEvent
    from PySide6.QtGui import QFocusEvent
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_chrome import named_item
    c=layout_controller;control=c.actions;app=QApplication.instance()
    c.focus_control('opacityButton')
    app.sendEvent(control.quick,QFocusEvent(QEvent.FocusIn,Qt.TabFocusReason))
    c.focus_control('collapse');QTest.qWait(20)
    assert named_item(control.quick.rootObject(),'collapse').hasActiveFocus()
    app.sendEvent(control.quick,QFocusEvent(QEvent.FocusIn,Qt.TabFocusReason))
    control.hide();QTest.qWait(20)
    assert not control.isVisible() and not control.quick.hasFocus()


def test_native_companion_keyboard_routes(layout_controller):
    import sys
    if sys.platform!='win32':pytest.skip('Windows native companion activation')
    from PySide6.QtWidgets import QWidget
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_windows import WindowsOverlay
    c=layout_controller;host=QWidget();host.resize(1300,1000);host.show()
    native=WindowsOverlay();hwnd=int(host.winId())
    # The automation host may deny foreground activation. Keep only this owned
    # surface visible; test real companion placement and Qt keyboard routing.
    native.visible_target=lambda h:h==hwnd and bool(native.u.IsWindowVisible(h) and not native.u.IsIconic(h)
        )
    native.reduce_motion=lambda:True
    c.native=native
    try:
        host.activateWindow();QTest.qWait(40)
        c.receive_target({'target':{'hwnd':hwnd},'selection':Selection(A)})
        check_keyboard_focus_path(c)
        from PySide6.QtCore import Qt
        from cachemonitor.overlay_chrome import named_item
        c.focus_control('collapse')
        QTest.keyClick(c.actions.quick,Qt.Key_Space);QTest.qWait(20)
        restore=named_item(c.icon.quick.rootObject(),'restore')
        assert c.collapsed and restore.hasActiveFocus() and restore.property('keyboardFocus')
        assert named_item(c.icon.quick.rootObject(),'moveFocusRing').isVisible()
        QTest.keyClick(c.icon.quick,Qt.Key_Return);QTest.qWait(20)
        assert not c.collapsed and named_item(c.actions.quick.rootObject(),'expand').hasActiveFocus()
        assert not c.actions.quick.grabFramebuffer().isNull()
    finally:
        c.hide_all();host.close()
