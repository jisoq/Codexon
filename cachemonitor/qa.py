from cachemonitor.screens import screen_id
"""Interaction probe used only by the explicit --smoke verification path."""
import time
from PySide6.QtTest import QTest


def taskbar_probe(window, app, path):
    """Check the embedded widget while the dashboard is closed, and capture its band."""
    import ctypes
    import sys
    from .taskbar import taskbar_rect
    if sys.platform != 'win32':
        return {'excluded': 'Windows 전용 표시'}
    indicator = window.taskbar_quota
    if indicator.native is None:
        assert not indicator.isVisible()
        return {'excluded': 'Explorer 보호: smoke에서는 작업표시줄 연결 및 시계 UIA 조회 제외'}
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    foreground = user32.GetForegroundWindow()
    indicator.sync_position()
    app.processEvents()
    assert foreground == user32.GetForegroundWindow(), 'Quota refresh changed keyboard focus'
    assert user32.IsWindowVisible(int(indicator.winId()))
    bar = taskbar_rect()
    native = indicator.native
    host = native.host()
    assert native.api.GetParent(int(indicator.winId())) == host
    assert native.api.GetWindowLongW(int(indicator.winId()), -16) & 0x40000000
    assert not native.api.GetWindowLongW(int(indicator.winId()), -20) & 8
    assert native.rect(host).contains(native.rect(int(indicator.winId())))
    assert indicator.grab().save(str(path.with_name(path.stem + '-taskbar-indicator.png')))
    screen = app.primaryScreen()
    assert screen.grabWindow(0, bar.x(), bar.y(), bar.width(), bar.height()).save(
        str(path.with_name(path.stem + '-taskbar-band.png')))
    widget_rect = native.rect(int(indicator.winId()))
    ratio = screen.devicePixelRatio()
    assert screen.grabWindow(0, round(widget_rect.x() / ratio) - 6, bar.y(),
                             round(widget_rect.width() / ratio) + 12, bar.height()).save(
        str(path.with_name(path.stem + '-taskbar-widget.png')))
    monitors = []
    saved_monitor = indicator.monitor_name
    try:
        for index, target in enumerate(app.screens(), 1):
            target_host = native.host_for_screen(screen_id(target))
            if not target_host:
                continue
            indicator.set_monitor(screen_id(target))
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                app.processEvents()
                indicator.sync_position()
                if indicator.isVisible() and indicator.embedding_error is None:
                    break
                QTest.qWait(20)
            assert indicator.isVisible() and indicator.embedding_error is None
            actual = native.rect(int(indicator.winId()))
            assert native.api.GetParent(int(indicator.winId())) == target_host
            assert native.rect(target_host).contains(actual)
            entry = {'screen': screen_id(target), 'widget': actual.getRect()}
            if target != app.primaryScreen():
                clock = native.clock_rect(target_host)
                assert clock is not None
                gap = clock.left() - actual.right() - 1
                assert gap == round(8 * target.devicePixelRatio()), (clock, actual, gap)
                entry.update(clock=clock.getRect(), gap_pixels=gap)
            height = round(native.rect(target_host).height() / target.devicePixelRatio())
            assert target.grabWindow(0, 0, target.geometry().height() - height,
                                     target.geometry().width(), height).save(
                str(path.with_name(path.stem + f'-monitor-{index}.png')))
            monitors.append(entry)
    finally:
        indicator.set_monitor(saved_monitor)
        indicator.sync_position()
    return {'native_visible_with_dashboard_closed': True, 'refresh_preserves_focus': True,
            'inside_primary_taskbar': True, 'native_child_of_taskbar': True,
            'topmost_overlay': False, 'text': indicator.accessibleName(),
            'geometry': native.rect(int(indicator.winId())).getRect(), 'taskbar_geometry': bar.getRect(),
            'monitors': monitors}
