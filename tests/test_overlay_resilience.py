import time
import sys
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtTest import QTest
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_tracking import RouteLog,Selection
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.analysis_engine import AnalysisEngine
from test_overlay import logfile,route,A,B,source


def test_long_line_partial_tail_truncation_and_same_path_replacement(tmp_path):
    path=logfile(tmp_path);reader=RouteLog(tmp_path);reader.READ_LIMIT=512
    path.write_bytes(b'x'*2500+route(B).encode()+b'\n'+route(A).encode())
    offsets=[]
    for i in range(8):
        selected=reader.poll(42,i*.1);offsets.append(reader.offset)
        if selected:break
    assert selected.thread_id==A and reader.offset==path.stat().st_size
    assert offsets==sorted(set(offsets)) and len(reader.partial)<=512
    with path.open('ab') as f:f.write(route(B).encode()[:-1])
    assert reader.poll(42,1) is None
    with path.open('ab') as f:f.write(b'\n')
    assert reader.poll(42,1.1).thread_id==B
    path.write_text(route(A),encoding='utf8')
    assert reader.poll(42,1.2).thread_id==A
    replacement=path.with_suffix('.replacement');replacement.write_text(route(B),encoding='utf8');replacement.replace(path)
    assert reader.poll(42,1.3).thread_id==B


@pytest.mark.skipif(sys.platform!='win32',reason='Owned native Windows windows')
def test_auto_fallback_restores_preference_anchor_and_clickthrough(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setWindowTitle('CacheMonitor owned resize test');host.resize(900,1000);host.show()
    controller=OverlayController(QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat),native_enabled=False,appearance_path=tmp_path/'codex.toml')
    native=WindowsOverlay();controller.native=native
    native.configure(int(controller.widget.winId()))
    native.visible_target=lambda hwnd:bool(native.u.IsWindowVisible(hwnd) and not native.u.IsIconic(hwnd))
    controller.anchor=(1,.7);controller.expanded=True
    engine=AnalysisEngine();s=source();s['request_state']=dict(turn='turn',state='완료',started_at=90,ended_at=104)
    engine.ingest([s]);controller.receive_snapshot(dict(overlay_sessions=OverlaySummaries().collect(engine)))
    def refresh():
        app.processEvents();controller.receive_target(dict(target={'hwnd':int(host.winId())},selection=Selection(A)))
    def resize_frame(width,height):
        import ctypes
        from ctypes import wintypes as W
        hwnd=int(host.winId());deadline=time.monotonic()+3
        desired=(width,height)
        while time.monotonic()<deadline:
            app.processEvents();frame=native.frame(hwnd)
            raw=W.RECT();assert native.u.GetWindowRect(hwnd,ctypes.byref(raw))
            if frame:
                actual=(frame[2]-frame[0],frame[3]-frame[1])
                if all(abs(a-b)<=1 for a,b in zip(actual,desired)):
                    refresh();return
                extra=(raw.right-raw.left-actual[0],raw.bottom-raw.top-actual[1])
                assert native.u.SetWindowPos(hwnd,None,0,0,width+extra[0],height+extra[1],0x0016)
            QTest.qWait(20)
        pytest.fail(f'Frame did not settle: requested={desired}, actual={native.frame(hwnd)}')
    requested=None
    def size_for(mode):
        nonlocal requested
        dpi=native.u.GetDpiForWindow(int(host.winId()));factor=dpi/96;gap=round(16*factor)
        m=controller.widget.content_model
        full=round(m.monitor_height(False)*factor);compact=round(m.monitor_height(True)*factor)
        height={'full':full+40,'compact':(full+compact)//2,'icon':compact//2}[mode]
        requested=(round(660*controller.appearance.scale*factor)+2*gap,height+2*gap)
        resize_frame(*requested)
    try:
        refresh();size_for('full');assert controller.automatic_mode=='detail'
        from PySide6.QtQuick import QQuickItem
        root=controller.actions.quick.rootObject()
        assert controller.can_present('home',A)
        assert root.findChild(QQuickItem,'evidence') is None and root.findChild(QQuickItem,'expand') is not None
        assert controller.expanded and not hasattr(controller,'open_evidence')
        size_for('compact')
        assert controller.automatic_mode in ('detail','detail-inline') and controller.widget.content_model.compact and controller.widget.isVisible()
        assert controller.widget.grab().save(str(tmp_path/'compact.png'))
        size_for('icon')
        assert controller.automatic_mode=='icon' and controller.icon.isVisible() and not controller.collapsed
        assert not controller.can_present('home',A)
        size_for('full')
        assert controller.automatic_mode=='detail' and controller.anchor==(1,.7)
        assert controller.widget.grab().save(str(tmp_path/'detail.png'))
        assert native.u.GetWindowLongPtrW(int(controller.widget.winId()),-20)&0x20
        controller.set_collapsed(True);size_for('full')
        assert controller.automatic_mode=='icon' and controller.collapsed
        controller.set_enabled(False)
        assert not controller.timer.isActive() and not controller.widget.isVisible()
        controller.set_enabled(True);refresh()
        assert controller.collapsed and not controller.widget.qml_errors
    except BaseException:
        import os,json
        from pathlib import Path
        folder=Path(os.environ.get('CODEXON_OVERLAY_QA_OUTPUT',str(tmp_path/'failure')))
        folder.mkdir(parents=True,exist_ok=True)
        m=controller.widget.content_model
        (folder/'geometry.json').write_text(json.dumps(dict(requested=requested,frame=native.frame(int(host.winId())),dpi=native.u.GetDpiForWindow(int(host.winId())),qt_dpr=host.devicePixelRatioF(),scale=controller.appearance.scale,full=m.monitor_height(False),compact=m.monitor_height(True),mode=controller.automatic_mode)),encoding='utf-8')
        controller.widget.grab().save(str(folder/'overlay.png'));host.grab().save(str(folder/'host.png'))
        raise
    finally:controller.stop();host.close();app.processEvents()


@pytest.mark.skipif(sys.platform!='win32',reason='Windows tracking lifecycle')
def test_disabled_tracker_is_dormant_and_restart_reads_fresh_selection(tmp_path,monkeypatch):
    from cachemonitor.overlay_windows import WindowsOverlay
    app=QApplication.instance() or QApplication([])
    enumerated=[];monkeypatch.setattr(WindowsOverlay,'targets',lambda self:enumerated.append(1) or [])
    settings=QSettings(str(tmp_path/'dormant.ini'),QSettings.IniFormat);settings.setValue('overlay/enabled',False)
    controller=OverlayController(settings,native_enabled=True)
    try:
        QTest.qWait(120)
        assert not controller.tracker.isRunning() and not controller.timer.isActive() and not enumerated
        for _ in range(2):
            previous=len(enumerated);controller.set_enabled(True)
            deadline=time.monotonic()+3
            while len(enumerated)==previous and time.monotonic()<deadline:
                app.processEvents();time.sleep(.01)
            assert controller.tracker.isRunning() and len(enumerated)>previous
            controller.set_enabled(False);count=len(enumerated);QTest.qWait(100)
            assert not controller.tracker.isRunning() and len(enumerated)==count
    finally:controller.stop()
