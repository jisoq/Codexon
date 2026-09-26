from cachemonitor.screens import screen_id
from .quick_qa import click_row, wheel as quick_wheel, scroll_extent, scroll_middle
"""Interaction probe used only by the explicit --smoke verification path."""
import time
from statistics import median
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest


def taskbar_probe(window, app, path):
    """Check the embedded widget while the dashboard is closed, and capture its band."""
    import ctypes
    import sys
    from .taskbar import taskbar_rect
    if sys.platform == 'darwin':
        from .macos_status import MacStatusTray
        tray = window.tray
        if not isinstance(tray, MacStatusTray):
            return {'native_status_deferred': 'Isolated GUI smoke disables menu bar integration; verify_macos_desktop.py checks the native boundary.'}
        before = tray.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        before_pid = int(before.processIdentifier()) if before else None
        window.hide()
        window.refresh_tray()
        app.processEvents()
        after = tray.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        assert tray.item.isVisible() and not window.isVisible()
        assert (int(after.processIdentifier()) if after else None) == before_pid
        tray.rebuild_menu(tray.menu)
        assert tray.item.menu() == tray.menu
        return {'native_visible_with_dashboard_closed': True, 'refresh_preserves_focus': True,
                'single_status_item': True, 'text': str(tray.item.button().title()),
                'native_menu_items': int(tray.menu.numberOfItems())}
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


def interaction_probe(window,app,settle):
    """Exercise adjacent session/request and request/call pairs and real wheel input."""
    from .quick_qa import table_view
    def summary(values):
        if not values:return {'excluded':'관측 가능한 상호작용 없음'}
        values=sorted(values)
        return {'n':len(values),'median_ms':round(median(values)*1000,3),
                'p95_ms':round(values[min(len(values)-1,int(len(values)*.95))]*1000,3),
                'max_ms':round(max(values)*1000,3)}
    window.model.setCurrentIndex(0)
    window.selected_session=None;window.selected_turn=None;window.selected_call=None
    window.selected_event=None;window.temporary_context=None;window.record_view='sessions'
    window.search.clear();window.nav.setCurrentRow(2);window.render();settle()
    candidates=[(index,row) for index,row in enumerate(window.parent_rows)
                if window.lookup['session_turns'].get((row['home'],row['sid']))]
    if not candidates:return {'excluded':'탐색 가능한 요청 구간 없음'}
    session,row=max(candidates,key=lambda value:value[1]['calls'])
    click_row(window,window.parent_table,session);settle()
    assert window.record_view=='requests' and window.selected_session==(row['home'],row['sid'])
    turns=[row['turn'] for row in sorted(window.record_rows,key=lambda row:row.get('responses',0),reverse=True)[:12]]
    if not turns:return {'excluded':'선택 세션에 요청 기록 없음'}
    times=[];latest_first={}
    for turn in turns*2:
        index=next(i for i,row in enumerate(window.record_rows) if row['turn']==turn)
        expected=window.record_rows[index]['responses']
        started=time.perf_counter();click_row(window,window.table,index);settle();window.quick.grabFramebuffer()
        times.append(time.perf_counter()-started)
        assert window.record_view=='calls' and window.selected_turn==turn
        assert window.table.rowCount()==expected
        stamps=[row.get('ts',0) for row in window.record_rows]
        assert stamps==sorted(stamps,reverse=True)
        latest_first[turn]=len(stamps)
        window.go_back();settle();assert window.record_view=='requests'
    index=next(i for i,row in enumerate(window.record_rows) if row['turn']==turns[0])
    click_row(window,window.table,index);settle()
    count=window.table.rowCount();wheel=[];moved=0;view=window.table
    if count:
        table=table_view(window,window.table);table.forceActiveFocus()
        QTest.keyClick(window.quick,Qt.Key_Home);QTest.keyClick(window.quick,Qt.Key_Return);settle()
        assert window.selected_call and window.exact_record and window.detail_scroll.isVisible()
        window.close_record_detail();settle()
    scroll_middle(window,view);settle()
    if scroll_extent(window,view)>1:
        for i in range(30):
            before=view.verticalScrollBar().value();started=time.perf_counter()
            quick_wheel(window,view,120 if i%2 else -120);settle();window.quick.grabFramebuffer()
            wheel.append(time.perf_counter()-started);moved+=view.verticalScrollBar().value()!=before
        assert moved>=24
    for page in (0,1,2):window.nav.setCurrentRow(page);settle()
    revisits=[]
    for _ in range(4):
        for page in (0,1,2):
            started=time.perf_counter();window.nav.setCurrentRow(page);settle();window.grab()
            revisits.append(time.perf_counter()-started)
    return {'model_scope':'all','detail_rows':count,'adjacent_record_tables':True,
            'request_click_to_paint':summary(times),'latest_first':latest_first,
            'call_wheel_to_paint':summary(wheel) if wheel else {'excluded':'스크롤할 행 수 없음'},
            'cached_revisit_to_paint':summary(revisits)}
