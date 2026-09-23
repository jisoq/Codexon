import copy
import time
import sys
import pytest
from PySide6.QtCore import QSettings,Qt
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtTest import QTest
from cachemonitor.overlay import OverlayController,SessionOverlay
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


def test_request_start_never_reuses_previous_usage_and_recent_is_bounded():
    s=source();engine=AnalysisEngine();summaries=OverlaySummaries()
    s['request_state']=dict(turn='new',state='진행',started_at=106,ended_at=None)
    engine.ingest([s]);d=summaries.collect(engine)[0]
    assert d['request']['calls']==0 and d['request']['cost'] is None and d['request']['model'] is None
    for i in range(20):s['history'].append(dict(s['history'][0],key=f'new{i}',turn='new',ts=107+i))
    engine.ingest([s]);d=summaries.collect(engine)[0]
    assert d['request']['calls']==20 and len(d['recent'])==23
    assert d['request']['cost']!=d['cost']
    s['request_state']=dict(turn='empty',state='완료',started_at=150,ended_at=151)
    engine.ingest([s]);d=summaries.collect(engine)[0]
    assert d['request']['calls']==0 and d['request']['cost'] is None


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
        app.processEvents();controller.receive_target(dict(target={'hwnd':int(host.winId())},selection=Selection(A)));QTest.qWait(35)
    try:
        refresh();assert controller.automatic_mode=='detail'
        from PySide6.QtQuick import QQuickItem
        root=controller.actions.quick.rootObject()
        assert controller.can_present('home',A)
        assert root.findChild(QQuickItem,'evidence') is None and root.findChild(QQuickItem,'expand') is not None
        assert controller.expanded and not hasattr(controller,'open_evidence')
        host.resize(700,450);refresh()
        assert controller.automatic_mode in ('detail','detail-inline') and controller.widget.content_model.compact and controller.widget.isVisible()
        assert controller.widget.grab().save(str(tmp_path/'compact.png'))
        host.resize(200,160);refresh()
        assert controller.automatic_mode=='icon' and controller.icon.isVisible() and not controller.collapsed
        assert not controller.can_present('home',A)
        host.resize(900,1000);refresh()
        assert controller.automatic_mode=='detail' and controller.anchor==(1,.7)
        assert controller.widget.grab().save(str(tmp_path/'detail.png'))
        assert native.u.GetWindowLongPtrW(int(controller.widget.winId()),-20)&0x20
        controller.set_collapsed(True);host.resize(950,1050);refresh()
        assert controller.automatic_mode=='icon' and controller.collapsed
        controller.set_enabled(False)
        assert not controller.timer.isActive() and not controller.widget.isVisible()
        controller.set_enabled(True);refresh()
        assert controller.collapsed and not controller.widget.qml_errors
    finally:controller.stop();host.close();app.processEvents()


def test_content_identical_refresh_does_not_repaint_and_quota_preserves_reset(tmp_path):
    app=QApplication.instance() or QApplication([])
    controller=OverlayController(QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat),native_enabled=False)
    try:
        engine=AnalysisEngine();engine.ingest([source()]);d=OverlaySummaries().collect(engine)[0]
        calls=[];controller.widget.content_model.update=lambda:calls.append(1)
        controller.widget.set_content(d);count=len(calls)
        for _ in range(100):controller.widget.set_content(d)
        assert len(calls)==count
        now=time.time();q=dict(observed_at=now-2,source='local',windows={'weekly':dict(used_percent=20,resets_at=now-1)})
        controller.receive_quota({'quota':q},'home')
        text=controller.quota_text(d)
        assert '—' in text[0] and '초기화 후' in text[1]
        assert controller.quota_text(dict(d,home='another'))==('','')
    finally:controller.stop()


def test_height_fallback_keeps_anchor_and_restores_at_exact_required_height(tmp_path):
    class Native:
        class API:
            def GetDpiForWindow(self,hwnd):return 96
        u=API()
        bounds=(0,0,1000,1000)
        def visible_target(self,hwnd):return True
        def frame(self,hwnd):return self.bounds
        def configure(self,*args,**kwargs):pass
        def place(self,*args):return True
    app=QApplication.instance() or QApplication([])
    controller=OverlayController(QSettings(str(tmp_path/'boundary.ini'),QSettings.IniFormat),native_enabled=False,appearance_path=tmp_path/'codex.toml')
    native=Native();controller.native=native
    try:
        engine=AnalysisEngine();engine.ingest([source()])
        controller.receive_snapshot(dict(overlay_sessions=OverlaySummaries().collect(engine)))
        controller.anchor=(1,.6);controller.expanded=True
        controller.receive_target(dict(target={'hwnd':1},selection=Selection(A)))
        assert controller.automatic_mode=='detail'
        required=controller.widget.panel_height()+32
        native.bounds=(0,0,1000,required-1);controller.refresh()
        assert controller.automatic_mode=='detail' and controller.widget.content_model.compact
        native.bounds=(0,0,1000,required);controller.refresh()
        assert controller.automatic_mode=='detail' and not controller.widget.content_model.compact
        assert controller.anchor==(1,.6)
    finally:controller.stop();app.processEvents()


@pytest.mark.skipif(sys.platform!='win32',reason='WinEvent hook message delivery')
def test_hooks_receive_owned_window_events_and_unhook():
    from cachemonitor.overlay_windows import WindowsOverlay,WindowEvents
    app=QApplication.instance() or QApplication([])
    host=QWidget();native=WindowsOverlay();events=[]
    hook=WindowEvents(native,lambda:events.append(1));hook.targets.add(int(host.winId()))
    try:
        assert len(hook.handles)==3
        host.show();host.resize(390,210);QTest.qWait(60)
        assert events
        hook.close();count=len(events)
        host.resize(410,240);QTest.qWait(50)
        assert len(events)==count and not hook.handles
    finally:hook.close();host.close()


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
