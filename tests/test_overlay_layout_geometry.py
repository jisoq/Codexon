"""Native placement stays anchored without applying UI scale to the outer gap."""
import pytest

from cachemonitor.overlay_tracking import (
    anchored_monitor_geometry, extend_monitor_left, monitor_anchor_for_position, edge_anchor_from_legacy,
)


@pytest.mark.parametrize('dpi', [96, 120, 144, 192])
@pytest.mark.parametrize('ui_scale', [1, 16/14, 1.5])
def test_side_detail_preserves_monitor_right_bottom_and_scales_once(dpi, ui_scale):
    frame=(-2000, -300, 1000, 1600)
    monitor=anchored_monitor_geometry(frame,dpi,380*ui_scale,560*ui_scale)
    detail=extend_monitor_left(frame,dpi,monitor,240*ui_scale)
    factor=dpi/96
    assert monitor[2:]==(round(380*ui_scale*factor),round(560*ui_scale*factor))
    assert frame[2]-monitor[0]-monitor[2]==round(16*factor)
    assert frame[3]-monitor[1]-monitor[3]==round(16*factor)
    assert detail[0]+detail[2]==monitor[0]+monitor[2]
    assert detail[1:2]+detail[3:]==monitor[1:2]+monitor[3:]
    assert detail[2]-monitor[2]==round(240*ui_scale*factor)


def test_conditional_rows_reduced_mode_and_icon_keep_user_anchor():
    frame=(0,0,1200,1100)
    anchor=(.45,.65)
    boxes=[anchored_monitor_geometry(frame,96,w,h,reference_width=380,
        reference_height=560,anchor=anchor) for w,h in ((380,560),(380,600),(380,380),(32,32))]
    assert len({(x+w,y+h) for x,y,w,h in boxes})==1
    for geometry in boxes:
        restored=monitor_anchor_for_position(frame,geometry,*geometry[:2],96,
                                             reference_width=380,reference_height=560)
        assert restored==pytest.approx(anchor,abs=.002)


def test_detail_does_not_move_saved_monitor_to_make_room():
    frame=(0,0,1200,1000)
    monitor=anchored_monitor_geometry(frame,96,380,560,anchor=(0,.5))
    assert monitor[0]==16
    assert extend_monitor_left(frame,96,monitor,240) is None
    assert anchored_monitor_geometry(frame,96,380,560,anchor=(0,.5))==monitor


def test_too_small_monitor_still_has_anchored_restore_icon():
    frame=(0,0,250,220)
    assert anchored_monitor_geometry(frame,96,380,380) is None
    assert anchored_monitor_geometry(frame,96,32,32,reference_width=380,reference_height=560)==(202,172,32,32)


def test_legacy_anchor_conversion_preserves_panel_position():
    frame=(100,50,1500,1200)
    legacy=(.3,.6)
    original=anchored_monitor_geometry(frame,144,380,580,anchor=legacy,edge_anchor=False)
    converted=edge_anchor_from_legacy(frame,144,380,580,legacy)
    assert anchored_monitor_geometry(frame,144,380,580,anchor=converted)==original
    shorter=anchored_monitor_geometry(frame,144,380,560,anchor=converted)
    assert (shorter[0]+shorter[2],shorter[1]+shorter[3])==(original[0]+original[2],original[1]+original[3])
