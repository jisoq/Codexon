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
