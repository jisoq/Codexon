
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.overlay_shadow import OverlayShadow


def test_shadow_clip_stays_inside_codex_frame_and_clears_after_moving_inward():
    app=QApplication.instance() or QApplication([])
    shadow=OverlayShadow()
    try:
        geometry=(10,20,shadow.width(),shadow.height())
        shadow.clip_to_frame((0,0,500,700),geometry,1.)
        clip=shadow.mask().boundingRect()
        assert clip.left()==0 and clip.top()==0
        assert clip.right()+1==490 and clip.bottom()+1==680
        shadow.clip_to_frame((0,0,2000,2000),geometry,1.)
        assert shadow.mask().isEmpty()
    finally:shadow.close();app.processEvents()


def test_rendered_quick_scene_keeps_panel_clear_and_shadow_visible():
    app=QApplication.instance() or QApplication([])
    shadow=OverlayShadow()
    try:
        shadow.show();QTest.qWait(35)
        image=shadow.quick.grabFramebuffer()
        # QQuickWidget's framebuffer is physical-sized even when the returned
        # QImage has its default DPR metadata; use the actual framebuffer ratio.
        scale=image.width()/shadow.width()
        def alpha(x,y):return image.pixelColor(round(x*scale),round(y*scale)).alpha()
        padding=shadow.padding
        assert alpha(padding+190,padding+290)==0
        assert alpha(padding+190,padding+586)>0
        assert not shadow.qml_errors
    finally:shadow.close();app.processEvents()
