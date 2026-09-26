"""Cross-platform contracts for the macOS desktop boundary and fallback UI."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings, QPointF, QPoint, Qt
from PySide6.QtWidgets import QApplication

from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_macos import MacOverlay, selection_state, window_targets
from cachemonitor.overlay_tracking import RouteLog, Selection


A = '11111111-1111-1111-1111-111111111111'
B = '22222222-2222-2222-2222-222222222222'


def cg_window(pid=42, handle=7, **changes):
    return dict(kCGWindowOwnerPID=pid, kCGWindowNumber=handle, kCGWindowLayer=0,
                kCGWindowIsOnscreen=True, kCGWindowBounds=dict(X=-900, Y=80, Width=800, Height=700), **changes)


def test_codex_bundle_identity_and_point_bounds_do_not_require_window_titles():
    applications = [dict(pid=42, bundle_id='com.openai.codex', name='ChatGPT'),
                    dict(pid=43, bundle_id='com.apple.Safari', name='Codex')]
    windows = [cg_window(), cg_window(43, 8), {**cg_window(42, 9), 'kCGWindowLayer': 3},
               {**cg_window(42, 10), 'kCGWindowIsOnscreen': False}]
    targets = window_targets(windows, applications)
    assert len(targets) == 1
    assert targets[0]['pid'] == 42 and targets[0]['hwnd'] == 7
    assert targets[0]['frame'] == (-900, 80, -100, 780)
    assert MacOverlay.dpi(None, 7) == 96


def test_mac_route_reader_tracks_the_same_process_and_refuses_ambiguous_windows(tmp_path):
    path = tmp_path/'2026/09/26/codex-desktop-instance-42-t0-i1-120000-0.log'
    path.parent.mkdir(parents=True)
    def route(sid):
        return f'info IAB_LIFECYCLE received browser sidebar owner sync windowId=1 ownerRoutePath=/local/{sid}\n'
    path.write_text(route(A))
    log = RouteLog(tmp_path)
    target = dict(pid=42, hwnd=7)
    assert selection_state([target], log, 0)['selection'].thread_id == A
    with path.open('a') as stream:
        stream.write(route(B))
    assert selection_state([target], log, 1)['selection'].thread_id == B
    assert selection_state([target, dict(pid=42, hwnd=8)], log, 2)['selection'] is None
    assert selection_state([dict(pid=43, hwnd=7)], log, 3)['selection'] is None


class PointBackend:
    def __init__(self):
        self.placements = {}
    def dpi(self, handle): return 96
    def frame(self, handle): return (0, 0, 1400, 1100)
    def visible_target(self, handle): return True
    def configure(self, *args, **kwargs): pass
    def set_companions(self, handles): pass
    def place(self, handle, bounds): self.placements[handle] = bounds;return True
    def primary_down(self): return False
    def cursor(self): return (0, 0)
    def reduce_motion(self): return True


@pytest.fixture
def controller(tmp_path):
    app = QApplication.instance() or QApplication([])
    value = OverlayController(QSettings(str(tmp_path/'test.ini'), QSettings.IniFormat), native_enabled=False)
    value.native = PointBackend()
    value.appearance_reader.read = lambda dark: value.appearance
    # Keep the real overlay summary and navigation data contract.
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    from test_overlay import source
    engine = AnalysisEngine();engine.ingest([source()])
    summaries = OverlaySummaries().collect(engine)
    value.receive_snapshot(dict(overlay_sessions=summaries))
    yield value
    value.stop();app.processEvents()


def test_manual_panel_requires_explicit_known_selection_and_ignores_automatic_changes(controller):
    value = controller
    assert not value.set_manual_session('unknown', A)
    assert value.manual_session is None
    assert value.set_manual_session('home', A)
    assert value.target_state['target']['manual']
    assert value.widget.isVisible()
    assert value.content()[0]['id'] == A
    automatic = dict(target={'hwnd': 17, 'pid': 44}, selection=Selection(B))
    value.receive_target(automatic)
    assert value.target_state['selection'].thread_id == A
    assert value.header.view.state['title'].startswith('고정 · ')
    value.follow_codex()
    assert value.manual_session is None
    assert value.target_state == automatic
    assert value.content()[0] is None


def test_explicit_manual_home_disambiguates_duplicate_session_ids(controller):
    value = controller
    data = value.sessions[0]
    value.receive_snapshot(dict(overlay_sessions=[data, {**data, 'home': 'other'}]))
    value.receive_target(dict(target={'hwnd': 17}, selection=Selection(A)))
    assert value.content()[0] is None
    assert value.set_manual_session('other', A)
    assert value.content()[0]['home'] == 'other'
    assert value.selected_scope('other', A)
    assert not value.selected_scope('home', A)


def test_mouse_coordinates_stay_in_points_on_retina(monkeypatch):
    import sys
    from cachemonitor.overlay_chrome import OverlayChrome
    monkeypatch.setattr(sys, 'platform', 'darwin')
    event = SimpleNamespace(globalPosition=lambda: QPointF(-320, 90))
    assert OverlayChrome.event_position(None, event) == (-320, 90)


def test_wheel_forwarding_does_not_request_access_without_permission():
    native = MacOverlay.__new__(MacOverlay)
    native.accessibility_enabled = lambda: False
    native.visible_target = lambda _: True
    assert native.forward_wheel(7, QPoint(0, 120), QPointF(1, 1), Qt.NoModifier) is False


def test_accessibility_subscriptions_follow_new_windows_and_release_on_permission_loss(monkeypatch):
    import sys
    from cachemonitor.overlay_macos import AccessibilityEvents
    added=[];removed=[];sources=[];windows=['first-window'];allowed=[True]
    accessibility=SimpleNamespace(
        AXObserverCreate=lambda pid,callback,out:(0,'observer'),
        AXUIElementCreateApplication=lambda pid:'application',
        AXUIElementCopyAttributeValue=lambda *args:(0,windows.copy()),
        AXObserverAddNotification=lambda observer,element,name,context:added.append((element,name)) or 0,
        AXObserverRemoveNotification=lambda observer,element,name:removed.append((element,name)),
        AXObserverGetRunLoopSource=lambda observer:'source')
    monkeypatch.setitem(sys.modules,'CoreFoundation',SimpleNamespace(
        CFRunLoopGetMain=lambda:'main',kCFRunLoopCommonModes='common',
        CFRunLoopAddSource=lambda *args:sources.append(('add',args)),
        CFRunLoopRemoveSource=lambda *args:sources.append(('remove',args))))
    native=SimpleNamespace(accessibility=accessibility,accessibility_enabled=lambda:allowed[0])
    def callback_during_teardown():raise RuntimeError('QObject already deleted')
    events=AccessibilityEvents(native,callback_during_teardown)
    events.sync([42]);initial=len(added)
    assert any(element=='first-window' for element,name in added)
    events.sync([42]);assert len(added)==initial
    windows[:]=['second-window'];events.sync([42])
    assert any(element=='second-window' for element,name in added)
    assert any(element=='first-window' for element,name in removed)
    assert len([kind for kind,args in sources if kind=='add'])==1
    events._changed(None,None,None,None)
    allowed[0]=False;events.sync([42]);assert events.observers=={}
    assert sources[-1][0]=='remove'


def test_optional_accessibility_failure_keeps_valid_polled_selection(tmp_path):
    from cachemonitor.overlay_macos import MacSelectionTracker
    app=QApplication.instance() or QApplication([])
    target=dict(pid=42,hwnd=7)
    native=SimpleNamespace(targets=lambda:[target],issue='')
    tracker=MacSelectionTracker(native,tmp_path)
    tracker.log=SimpleNamespace(poll=lambda *args:Selection(A))
    def unavailable(*args):raise RuntimeError('AX unavailable')
    tracker.events=SimpleNamespace(sync=unavailable,close=lambda:None)
    states=[];tracker.observed.connect(states.append)
    try:
        tracker.start()
        assert states[-1]['target']==target and states[-1]['selection'].thread_id==A
    finally:tracker.stop();app.processEvents()
