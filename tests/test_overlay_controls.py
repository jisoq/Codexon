from cachemonitor.quick_qa import click
from PySide6.QtQuick import QQuickItem
import sys
import time
import pytest
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_tracking import Selection,overlay_geometry,anchor_for_position


@pytest.mark.skipif(sys.platform!='win32',reason='Owned native overlay preview')
def test_icon_detail_toggle_native_layout_and_preference_restore(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setWindowTitle('CacheMonitor owned design preview');host.resize(1000,1000);host.show()
    settings=QSettings(str(tmp_path/'display.ini'),QSettings.IniFormat)
    settings.setValue('overlay/theme','light')
    c=OverlayController(settings,native_enabled=False);native=WindowsOverlay();c.native=native
    native.configure(int(c.widget.winId()))
    native.visible_target=lambda hwnd:bool(native.u.IsWindowVisible(hwnd) and not native.u.IsIconic(hwnd))
    data=summary();data['request']['started_at']=time.time()-20
    c.receive_snapshot(dict(overlay_sessions=[data],index={'loading':False}))
    def capture(name):
        screen=c.widget.screen();origin=screen.geometry().topLeft();ratio=screen.devicePixelRatio()
        left,top,right,bottom=native.frame(int(c.widget.winId()))
        shot=screen.grabWindow(0,round((left-origin.x())/ratio),round((top-origin.y())/ratio),round((right-left)/ratio),round((bottom-top)/ratio))
        assert shot.save(str(tmp_path/name))
    def refresh():
        c.receive_target(dict(target={'hwnd':int(host.winId())},selection=Selection('clean')))
        app.processEvents();QTest.qWait(70)
    try:
        refresh();assert not c.expanded and c.automatic_mode=='monitor'
        button=c.actions.quick.rootObject().findChild(QQuickItem,'expand')
        assert button.property('enabled') and not button.property('text')
        for dark in (False,True):
            settings.setValue('overlay/theme','dark' if dark else 'light');c.next_theme=0;refresh()
            assert not c.expanded
            capture(f'monitor-{dark}.png')
            click(c.actions,button);refresh()
            assert c.expanded and c.automatic_mode=='detail' and settings.value('overlay/expanded',type=bool)
            QTest.qWait(200)
            capture(f'detail-{dark}.png')
            assert all(not control.qml_errors for control in c.chrome) and not c.widget.qml_errors
            click(c.actions,button);refresh();assert not c.expanded
        c.toggle_expanded();refresh()
        host.resize(500,500);refresh()
        assert c.expanded and c.automatic_mode=='detail-inline' and button.property('enabled')
        host.resize(1000,1000);refresh();assert c.automatic_mode=='detail'
        restored=OverlayController(settings,native_enabled=False)
        assert restored.expanded;restored.stop()
        panel=native.frame(int(c.widget.winId()));header=native.frame(int(c.header.winId()));actions=native.frame(int(c.actions.winId()))
        assert panel[0]<=header[0]<header[2]<=actions[0]<actions[2]<=panel[2]
        assert native.u.GetWindowLongPtrW(int(c.widget.winId()),-20)&0x20
        assert not native.u.GetWindowLongPtrW(int(c.actions.winId()),-20)&0x20
    finally:c.stop();host.close();app.processEvents()


@pytest.mark.parametrize('dpi',[96,120,144,192])
def test_drag_anchors_roundtrip_and_clamp_inside_negative_monitor(dpi):
    frame=(-3000,-800,-100,1600)
    old=overlay_geometry(frame,dpi,anchor=(.5,.5))
    anchor=anchor_for_position(frame,old,old[0]+123,old[1]-87,dpi)
    new=overlay_geometry(frame,dpi,anchor=anchor)
    assert abs(new[0]-old[0]-123)<=1 and abs(new[1]-old[1]+87)<=1
    assert anchor_for_position(frame,old,-100000,100000,dpi)==(0,1)


@pytest.mark.skipif(sys.platform!='win32',reason='Real Windows companion windows')
def test_native_collapse_restore_opacity_drag_and_control_window_styles(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setWindowTitle('Cache Monitor owned control test');host.resize(900,760);host.show()
    native=WindowsOverlay();settings=QSettings(str(tmp_path/'overlay.ini'),QSettings.IniFormat)
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
        pixels=controller.widget.grab().toImage();scale=pixels.devicePixelRatio()
        assert 85<=pixels.pixelColor(round(12*scale),round(100*scale)).alpha()<=110
        click(controller.actions,controller.actions.quick.rootObject().findChild(QQuickItem,'collapse'));refresh()
        assert controller.collapsed and controller.icon.isVisible() and not controller.widget.isVisible()
        assert not controller.header.isVisible() and not controller.toolbar.isVisible()
        QTest.mouseClick(controller.icon,Qt.LeftButton);refresh()
        assert not controller.collapsed and controller.widget.isVisible()
        cursor=[(0,0)];native.cursor=lambda:cursor[0]
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
