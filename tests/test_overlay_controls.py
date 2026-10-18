from cachemonitor.quick_qa import click
from PySide6.QtQuick import QQuickItem
import sys
import pytest
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_tracking import Selection


@pytest.mark.skipif(sys.platform!='win32',reason='Real Windows companion windows')
def test_native_collapse_restore_opacity_drag_and_control_window_styles(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setWindowTitle('Cache Monitor owned control test');host.resize(900,760);host.show()
    native=WindowsOverlay();settings=QSettings(str(tmp_path/'overlay.ini'),QSettings.IniFormat)
    # The popup timer must consume owned input, never a user's desktop click.
    cursor=[(0,0)];pressed=[False]
    native.cursor=lambda:cursor[0]
    native.primary_down=lambda:pressed[0]
    controller=OverlayController(settings,native_enabled=False,appearance_path=tmp_path/'missing.toml')
    controller.native=native
    native.configure(int(controller.widget.winId()))
    native.visible_target=lambda hwnd:bool(native.u.IsWindowVisible(hwnd) and not native.u.IsIconic(hwnd))
    hwnd=int(host.winId())
    def refresh():
        controller.receive_target({'target':{'hwnd':hwnd},'selection':Selection('fixture')})
        app.processEvents();QTest.qWait(40)
    try:
        refresh()
        assert controller.widget.isVisible() and controller.header.isVisible() and not controller.toolbar.isVisible()
        panel=int(controller.widget.winId());before=native.frame(panel)
        assert native.u.GetWindowLongPtrW(panel,-20)&0x20
        for child in controller.chrome:
            flags=native.u.GetWindowLongPtrW(int(child.winId()),-20)
            assert flags&0x08000000 and not flags&0x20
        controller.toggle_opacity();refresh()
        assert controller.toolbar.isVisible()
        controller.toolbar.slider.setValue(60)
        assert controller.opacity==40 and settings.value('overlay/opacity',type=int)==40
        assert controller.input_timer.isActive()
        x,y,_,_=controller.popup_geometry
        cursor[0]=(x+1,y+1);pressed[0]=True;controller.poll_popup()
        assert controller.popup_open and controller.toolbar.isVisible()
        pressed[0]=False;controller.poll_popup()
        cursor[0]=(-10000,-10000);pressed[0]=True;controller.poll_popup()
        assert not controller.popup_open and not controller.toolbar.isVisible() and not controller.input_timer.isActive()
        pressed[0]=False
        pixels=controller.widget.grab().toImage();scale=pixels.devicePixelRatio()
        assert 85<=pixels.pixelColor(round(6*scale),pixels.height()//2).alpha()<=110
        click(controller.actions,controller.actions.quick.rootObject().findChild(QQuickItem,'collapse'));refresh()
        assert controller.collapsed and controller.icon.isVisible() and not controller.widget.isVisible()
        assert not controller.header.isVisible() and not controller.toolbar.isVisible()
        QTest.mouseClick(controller.icon,Qt.LeftButton);refresh()
        assert not controller.collapsed and controller.widget.isVisible()
        cursor[0]=(0,0)
        start=controller.current_geometry
        controller.begin_drag();cursor[0]=(-120,-90);controller.move_drag();controller.end_drag();refresh()
        moved=native.frame(panel)
        assert abs(moved[0]-start[0]+120)<=1 and abs(moved[1]-start[1]+90)<=1
        # Fast queued input must use event positions even if the live pointer
        # has already reached the final point when the press is handled.
        start=controller.current_geometry
        native.cursor=lambda:(9999,9999)
        controller.begin_drag((500,500));controller.move_drag((420,440));controller.end_drag((420,440));refresh()
        moved=native.frame(panel)
        assert abs(moved[0]-start[0]+80)<=1 and abs(moved[1]-start[1]+60)<=1
        stored=controller.anchor
        restored=OverlayController(settings,native_enabled=False,appearance_path=tmp_path/'missing.toml')
        assert restored.anchor==stored and restored.opacity==40
        restored.stop()
        controller.set_enabled(False)
        assert not any(w.isVisible() for w in (controller.widget,*controller.chrome))
    finally:controller.stop();host.close();app.processEvents()


@pytest.fixture
def native_detail(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    from test_overlay_layout_controller import publish_subagents,A
    from tests.test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.resize(1280,900);host.show()
    native=WindowsOverlay()
    native.visible_target=lambda hwnd:bool(native.u.IsWindowVisible(hwnd))
    native.primary_down=lambda:False
    c=OverlayController(QSettings(str(tmp_path/'stack.ini'),QSettings.IniFormat),native_enabled=False)
    c.native=native
    def refresh():
        c.receive_target({'target':{'hwnd':int(host.winId())},'selection':Selection(A)})
        c.refresh();app.processEvents()
    try:
        refresh();publish_subagents(c);c.toggle_expanded();refresh()
        wait_for_rendered(lambda:c.detail.isVisible() and c.actions.quick is not None)
        yield c,host,refresh
    finally:c.stop();host.close();app.processEvents()


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows hit testing')
@pytest.mark.parametrize('mode',['detail','detail-inline'])
def test_detail_interaction_keeps_header_buttons_reachable(native_detail,mode):
    from ctypes import wintypes as W
    from PySide6.QtCore import QPointF,QEvent
    from PySide6.QtGui import QEnterEvent,QMouseEvent
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    from cachemonitor.overlay_monitor import WIDTH
    c,host,refresh=native_detail
    width=1280 if mode=='detail' else round(WIDTH*c.appearance.scale)+64
    host.resize(width,900);refresh()
    assert c.detail.isVisible()
    assert c.automatic_mode==mode
    button=named_item(c.actions.quick.rootObject(),'expand')
    grouping=named_item(c.detail.quick.rootObject(),'detailMode-model')
    click(c.detail,grouping);refresh()
    assert c.widget.content_model.detail_group=='model'
    point=button.mapToScene(QPointF(button.width()/2,button.height()/2))
    # A direct Qt click alone would bypass the HWND that actually receives input.
    x,y,w,h=c.actions_geometry
    scale=w/c.actions.width()
    native_point=W.POINT(x+round(point.x()*scale),y+round(point.y()*scale))
    api=c.native.u
    api.WindowFromPoint.argtypes=[W.POINT];api.WindowFromPoint.restype=W.HWND
    assert api.WindowFromPoint(native_point)==int(c.actions.winId())
    global_point=QPointF(c.actions.quick.mapToGlobal(point.toPoint()))
    app=QApplication.instance()
    app.sendEvent(c.actions.quick,QEnterEvent(point,point,global_point))
    app.sendEvent(c.actions.quick,QMouseEvent(QEvent.MouseMove,point,global_point,Qt.NoButton,Qt.NoButton,Qt.NoModifier))
    wait_for_rendered(lambda:button.property('hovered'))
    click(c.actions,button);refresh()
    assert not c.expanded and not c.detail.isVisible()
    click(c.actions,button);refresh()
    assert c.expanded and c.detail.isVisible()
    # Re-show and reposition the same expanded mode, then interact again.
    c.hide_all();refresh()
    grouping=named_item(c.detail.quick.rootObject(),'detailMode-model')
    click(c.detail,grouping);refresh()
    assert api.WindowFromPoint(native_point)==int(c.actions.winId())
    opacity=named_item(c.actions.quick.rootObject(),'opacityButton')
    click(c.actions,opacity);refresh()
    assert c.toolbar.isVisible()
    click(c.detail,grouping);refresh()
    foreground=api.GetForegroundWindow()
    refresh()
    assert api.GetForegroundWindow()==foreground
    x,y,w,h=c.popup_geometry
    assert api.WindowFromPoint(W.POINT(x+w//2,y+h//2))==int(c.toolbar.winId())
    c.close_popup()
    click(c.actions,named_item(c.actions.quick.rootObject(),'collapse'));refresh()
    assert c.collapsed and c.icon.isVisible()
    QTest.mouseClick(c.icon,Qt.LeftButton);refresh()
    assert not c.collapsed and c.expanded and c.detail.isVisible()


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows popup stacking')
@pytest.mark.parametrize('source',['actions','detail'])
def test_overlay_tooltip_stays_above_panels_after_activation(native_detail,source):
    from ctypes import wintypes as W
    from PySide6.QtCore import QObject
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail;app=QApplication.instance()
    owner=getattr(c,source)
    button=named_item(owner.quick.rootObject(),'expand' if source=='actions' else 'detailMode-model')
    tip=next(o for o in button.findChildren(QObject) if o.metaObject().indexOfProperty('popupType')>=0)
    tip.setProperty('delay',0);tip.setProperty('visible',True)
    def popup():
        return next((w for w in app.topLevelWindows() if w.isVisible() and
                     w.transientParent() is owner.quick.quickWindow()),None)
    wait_for_rendered(lambda:popup() is not None)
    window=popup();api=c.native.u
    api.GetWindow.argtypes=[W.HWND,W.UINT];api.GetWindow.restype=W.HWND
    for layer in (c.detail,c.actions):
        layer.begin_interaction();refresh()
        assert window.isVisible()
        order=[];handle=api.GetWindow(int(window.winId()),0)
        while handle:
            order.append(handle);handle=api.GetWindow(handle,2)
        assert all(order.index(int(window.winId()))<order.index(int(w.winId()))
                   for w in (c.widget,c.detail,c.actions))
    tip.setProperty('visible',False)
