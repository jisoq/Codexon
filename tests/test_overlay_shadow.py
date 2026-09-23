from dataclasses import replace

from PySide6.QtCore import Qt, QRectF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.overlay_appearance import default_appearance
from cachemonitor.overlay_shadow import OverlayShadow, shadow_image, shadow_padding


def test_cached_shadow_is_outside_the_panel_and_offset_downward():
    app=QApplication.instance() or QApplication([])
    for dark in (False,True):
        image=shadow_image(380,580,dark,1.)
        padding=shadow_padding(dark)
        assert image.pixelColor(padding+190,padding+290).alpha()==0
        assert image.pixelColor(padding+20,padding+20).alpha()==0
        assert image.pixelColor(padding+360,padding+560).alpha()==0
        above=image.pixelColor(padding+190,padding-8).alpha()
        below=image.pixelColor(padding+190,padding+588).alpha()
        assert 0<=above<below<=round(255*(.26 if dark else .10))
        assert image.pixelColor(0,0).alpha()==0
        assert shadow_image(380,580,dark,1.).cacheKey()==image.cacheKey()


def test_shadow_uses_ui_scale_once_and_preserves_cache_on_idle_and_opacity_change():
    app=QApplication.instance() or QApplication([])
    shadow=OverlayShadow()
    try:
        original=shadow.view.image.cacheKey()
        shadow.set_panel_size(380,580)
        shadow.apply_appearance(shadow.appearance,20)
        assert shadow.view.image.cacheKey()==original
        shadow.apply_appearance(replace(shadow.appearance,accent='#123456'),94)
        assert shadow.view.image.cacheKey()==original
        shadow.apply_appearance(replace(default_appearance(True),font_size=21),94)
        assert shadow.panel_rect==QRectF(79*1.5,79*1.5,380*1.5,580*1.5)
        assert shadow.view.image.cacheKey()!=original
        geometry=shadow.physical_geometry((1000,2000,855,1305),1.5)
        padding=round(79*1.5*1.5)
        assert geometry==(1000-padding,2000-padding,855+2*padding,1305+2*padding)
        assert not shadow.isVisible() and not shadow.qml_errors
    finally:shadow.close();app.processEvents()


def test_expanded_shadow_is_one_continuous_surface_and_never_receives_input():
    app=QApplication.instance() or QApplication([])
    shadow=OverlayShadow()
    try:
        shadow.set_panel_size(620,620)
        padding=shadow.padding
        image=shadow.view.image
        assert image.pixelColor(padding+240,padding+310).alpha()==0
        assert image.pixelColor(padding+240,padding+628).alpha()>0
        assert shadow.windowFlags() & Qt.WindowTransparentForInput
        assert shadow.windowFlags() & Qt.WindowDoesNotAcceptFocus
        assert shadow.focusPolicy()==Qt.NoFocus
        assert shadow.testAttribute(Qt.WA_TransparentForMouseEvents)
    finally:shadow.close();app.processEvents()


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
