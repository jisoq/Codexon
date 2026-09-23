"""View transitions use the same native anchor and one control per action."""
import pytest
from PySide6.QtCore import QSettings, QAbstractAnimation
from PySide6.QtWidgets import QApplication

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay_tracking import Selection, anchored_monitor_geometry
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
def layout_controller(tmp_path):
    app=QApplication.instance() or QApplication([])
    controller=OverlayController(QSettings(str(tmp_path/'overlay.ini'),QSettings.IniFormat),native_enabled=False)
    controller.native=Native()
    controller.appearance_reader.read=lambda dark:controller.appearance
    engine=AnalysisEngine();engine.ingest([source()])
    controller.receive_snapshot({'overlay_sessions':OverlaySummaries().collect(engine)})
    controller.receive_target({'target':{'hwnd':1},'selection':Selection(A)})
    yield controller
    controller.stop();app.processEvents()


def test_detail_extends_left_without_resizing_monitor(layout_controller):
    controller=layout_controller
    original=controller.monitor_geometry
    controller.toggle_expanded()
    expanded=controller.current_geometry
    assert controller.automatic_mode=='detail'
    assert controller.monitor_geometry==original
    assert expanded[0]+expanded[2]==original[0]+original[2]
    assert expanded[1]+expanded[3]==original[1]+original[3]
    assert expanded[2]-original[2]==240
    assert controller.native.styles[int(controller.widget.winId())]
    assert not controller.native.styles[int(controller.detail.winId())]
    controller.toggle_expanded()
    assert controller.current_geometry==original


def test_detail_near_left_anchor_uses_inline_view_without_moving(layout_controller):
    controller=layout_controller
    controller.anchor=(0,.5);controller.refresh()
    original=controller.monitor_geometry
    controller.toggle_expanded()
    assert controller.automatic_mode=='detail-inline'
    assert controller.current_geometry==original
    assert controller.widget.width()==380
    assert controller.anchor==(0,.5)


def test_reduced_and_icon_fallback_preserve_user_preference(layout_controller):
    controller=layout_controller
    controller.toggle_expanded()
    controller.native.bounds=(0,0,1300,450);controller.refresh()
    assert controller.widget.content_model.compact
    assert controller.widget.panel_height() in (398,414)
    controller.native.bounds=(0,0,300,240);controller.refresh()
    assert controller.automatic_mode=='icon' and not controller.collapsed
    assert controller.current_geometry[2:]==(32,32)
    controller.native.bounds=(0,0,1300,1000);controller.refresh()
    assert controller.automatic_mode=='detail' and controller.expanded
    controller.set_collapsed(True)
    assert controller.automatic_mode=='icon'
    controller.set_collapsed(False)
    assert controller.automatic_mode=='detail'


def test_full_monitor_fits_the_new_height_before_reducing_tokens(layout_controller):
    controller=layout_controller
    complete=source()
    for row in complete['history']:row.update(cached=5000,written=0,reasoning=0)
    engine=AnalysisEngine();engine.ingest([complete])
    controller.receive_snapshot({'overlay_sessions':OverlaySummaries().collect(engine)})
    controller.native.bounds=(0,0,1300,610);controller.refresh()
    assert controller.automatic_mode=='monitor'
    assert controller.monitor_geometry==(904,16,380,578)
    assert not controller.widget.content_model.compact
    controller.native.bounds=(0,0,1300,609);controller.refresh()
    assert controller.automatic_mode=='compact'
    assert controller.monitor_geometry[3]==398
    assert controller.monitor_geometry[1]+controller.monitor_geometry[3]==593


def test_previous_height_legacy_anchor_migration_preserves_lower_right_edge(layout_controller):
    controller=layout_controller
    legacy=(.6,.75)
    old=anchored_monitor_geometry(controller.native.bounds,96,380,580,anchor=legacy,edge_anchor=False)
    controller.anchor=legacy;controller._legacy_anchor=True;controller.refresh()
    current=controller.monitor_geometry
    assert (current[0]+current[2],current[1]+current[3])==(old[0]+old[2],old[1]+old[3])
    assert controller.settings.value('overlay/anchorMode')=='edge'
    assert not controller._legacy_anchor


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


def test_detail_switch_places_final_geometry_once_without_animation(layout_controller):
    controller=layout_controller
    controller.native.reduce_motion=lambda:False
    monitor=controller.monitor_geometry
    placements=[]
    original_place=controller.native.place
    def record(hwnd,box):
        if hwnd==int(controller.widget.winId()):placements.append(box)
        return original_place(hwnd,box)
    controller.native.place=record
    controller.toggle_expanded()
    geometry=controller.current_geometry
    assert geometry[2]==monitor[2]+240 and placements==[geometry]
    assert (geometry[0]+geometry[2],geometry[1]+geometry[3])==(monitor[0]+monitor[2],monitor[1]+monitor[3])
    assert controller.monitor_geometry==monitor
    assert controller.detail.quick.rootObject().opacity()==1
    controller.toggle_expanded()
    assert controller.automatic_mode=='monitor' and controller.current_geometry==monitor
    assert placements==[geometry,monitor]
    controller.native.reduce_motion=lambda:True
    controller.toggle_expanded()
    assert controller.current_geometry[2]==620


def test_restore_icon_can_be_dragged_to_top_left_and_keeps_its_anchor(layout_controller):
    controller=layout_controller
    controller.set_collapsed(True)
    x,y,_,_=controller.current_geometry
    controller.begin_drag((100,100));controller.end_drag((100+16-x,100+16-y))
    assert controller.current_geometry==(16,16,32,32)
    anchor=controller.anchor
    controller.set_collapsed(False);controller.set_collapsed(True)
    assert controller.current_geometry==(16,16,32,32)
    assert controller.anchor==anchor
    assert controller.settings.value('overlay/anchorMode')=='edge'


def test_identical_controller_refresh_does_not_repaint_content(layout_controller):
    controller=layout_controller
    repaints=[]
    controller.widget.content_model.update=lambda:repaints.append(1)
    for _ in range(20):controller.refresh()
    assert repaints==[]


def test_conditional_height_transition_keeps_lower_anchor(layout_controller):
    controller=layout_controller
    controller.native.reduce_motion=lambda:False
    controller.reduced_motion=False
    before=controller.monitor_geometry
    complete=source()
    for row in complete['history']:row.update(cached=5000,written=0,reasoning=0)
    engine=AnalysisEngine();engine.ingest([complete])
    controller.receive_snapshot({'overlay_sessions':OverlaySummaries().collect(engine)})
    assert controller.height_animation.state()==QAbstractAnimation.Running
    controller.height_animation.setCurrentTime(60)
    during=controller.monitor_geometry
    assert during[1]+during[3]==before[1]+before[3]
    assert controller.widget.panel_height()<during[3]<before[3]
    controller.height_animation.setCurrentTime(120)
    assert controller.monitor_geometry[3]==controller.widget.panel_height()


def test_minimize_restore_transition_is_brief_and_preserves_view_anchor(layout_controller):
    controller=layout_controller
    controller.toggle_expanded()
    monitor=controller.monitor_geometry
    controller.native.reduce_motion=lambda:False
    controller.set_collapsed(True)
    assert controller.view_animation.state()==QAbstractAnimation.Running
    assert controller.view_animation.duration()<=120
    controller.view_animation.setCurrentTime(50)
    assert 0<controller.icon.windowOpacity()<1
    controller.view_animation.setCurrentTime(100)
    assert controller.icon.windowOpacity()==1 and controller.expanded
    controller.set_collapsed(False)
    assert controller.view_animation.state()==QAbstractAnimation.Running
    assert controller.automatic_mode=='detail' and controller.monitor_geometry==monitor
    controller.view_animation.setCurrentTime(100)
    assert all(window.windowOpacity()==1 for window in (controller.widget,controller.shadow,*controller.chrome))
    controller.native.reduce_motion=lambda:True
    controller.set_collapsed(True)
    assert controller.view_animation.state()==QAbstractAnimation.Stopped
    assert controller.icon.windowOpacity()==1


def test_keyboard_focus_connects_existing_header_graph_scroll_and_popup(layout_controller):
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


def test_keyboard_minimize_restore_and_move_keep_existing_control_paths(layout_controller):
    from PySide6.QtCore import Qt
    from PySide6.QtQuick import QQuickItem
    from PySide6.QtTest import QTest
    controller=layout_controller
    controller.focus_control('collapse')
    QTest.keyClick(controller.actions.quick,Qt.Key_Space);QTest.qWait(20)
    restore=controller.icon.quick.rootObject().findChild(QQuickItem,'restore')
    ring=controller.icon.quick.rootObject().findChild(QQuickItem,'moveFocusRing')
    assert controller.collapsed and restore.hasActiveFocus()
    assert restore.property('keyboardFocus') and ring.isVisible()
    assert (restore.x(),restore.y(),restore.width(),restore.height())==(4,4,32,32)
    assert (ring.x(),ring.y(),ring.width(),ring.height())==(0,0,40,40)
    icon_box=controller.native.placed[int(controller.icon.winId())]
    assert icon_box==(controller.current_geometry[0]-4,controller.current_geometry[1]-4,40,40)
    before=controller.current_geometry
    QTest.keyClick(controller.icon.quick,Qt.Key_Left,Qt.ShiftModifier);QTest.qWait(20)
    assert controller.current_geometry[0]==before[0]-10
    assert controller.settings.value('overlay/anchorMode')=='edge'
    QTest.keyClick(controller.icon.quick,Qt.Key_Return);QTest.qWait(20)
    assert not controller.collapsed
    assert controller.actions.quick.rootObject().findChild(QQuickItem,'expand').hasActiveFocus()
    assert not any(control.qml_errors for control in controller.chrome)


def test_pointer_restore_returns_target_focus_before_hiding_icon(layout_controller):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    controller=layout_controller
    restores=[];controller.native.restore_target_focus=restores.append
    controller.set_collapsed(True)
    restores.clear()
    QTest.mouseClick(controller.icon,Qt.LeftButton)
    assert not controller.collapsed and restores==[1]


def test_drag_uses_geometry_only_and_keeps_relative_windows(layout_controller):
    c=layout_controller
    before=dict(c.native.placed);original=c.monitor_geometry
    c.begin_drag((500,500))
    refresh=c.refresh
    c.refresh=lambda:pytest.fail('Unchanged layout must not refresh content during drag')
    c.move_drag((450,460))
    assert c.monitor_geometry[:2]==(original[0]-50,original[1]-40)
    for hwnd,box in before.items():
        assert c.native.placed[hwnd]==(box[0]-50,box[1]-40,*box[2:])
    c.refresh=refresh;c.end_drag((450,460))


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
        test_keyboard_focus_connects_existing_header_graph_scroll_and_popup(c)
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
