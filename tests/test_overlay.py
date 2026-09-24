import copy

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay_tracking import RouteLog, Selection
from cachemonitor.overlay_windows import WindowsOverlay, observe_selection


A = '11111111-1111-1111-1111-111111111111'
B = '22222222-2222-2222-2222-222222222222'


@pytest.mark.parametrize('version', ['26.915.100.0', '26.917.6896.0', '26.918.1.0', '27.0.0.0'])
def test_new_desktop_releases_require_live_route_evidence(tmp_path, version, monkeypatch):
    target = dict(hwnd=1, pid=42, version=version)
    reader = RouteLog(tmp_path)
    assert observe_selection([target], reader, 0)['selection'] is None
    path = logfile(tmp_path)
    path.write_text(route(A), encoding='utf-8')
    assert observe_selection([target], reader, 3)['selection'].thread_id == A
    # Navigation applies the same evidence rule, without a release allowlist.
    native = WindowsOverlay.__new__(WindowsOverlay)
    native.targets = lambda: [target]
    native._navigation_log = reader
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert native.confirm_selection(target).thread_id == A
    with path.open('a', encoding='utf-8') as stream:
        stream.write(route(B).rstrip('\n'))
    assert observe_selection([target], reader, 4)['selection'] is None
    assert native.confirm_selection(target) is None
    with path.open('a', encoding='utf-8') as stream:
        stream.write('\n')
    assert native.confirm_selection(target).thread_id == B


def test_route_compatibility_never_reuses_another_process_or_ambiguous_window(tmp_path, monkeypatch):
    path = logfile(tmp_path)
    path.write_text(route(A), encoding='utf-8')
    reader = RouteLog(tmp_path)
    old = dict(hwnd=1, pid=42, version='26.917.1.0')
    new = dict(hwnd=1, pid=43, version='27.0.0.0')
    assert observe_selection([old], reader, 0)['selection'].thread_id == A
    assert observe_selection([new], reader, 1)['selection'] is None
    native = WindowsOverlay.__new__(WindowsOverlay)
    native.targets = lambda: [new]
    native._navigation_log = reader
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert native.confirm_selection(old) is None
    current = logfile(tmp_path, pid=43)
    current.write_text('unrecognized future route format\n', encoding='utf-8')
    assert observe_selection([new], reader, 4)['selection'] is None
    current.write_text(route(B), encoding='utf-8')
    assert observe_selection([new], reader, 5)['selection'].thread_id == B
    other = dict(hwnd=2, pid=44, version='27.0.0.0')
    native.targets = lambda: [new, other]
    assert observe_selection([new, other], reader, 6)['selection'] is None
    assert native.confirm_selection(new) is None


def test_daily_route_log_rollover_keeps_same_process_selection_until_new_route(tmp_path):
    from datetime import datetime,timedelta

    yesterday=datetime(2026,9,22)
    today=yesterday+timedelta(days=1)
    old=tmp_path/f'{yesterday:%Y/%m/%d}'/'codex-desktop-instance-42-t0-i1-235959-0.log'
    old.parent.mkdir(parents=True)
    old.write_text(route(A),encoding='utf8')
    new=tmp_path/f'{today:%Y/%m/%d}'/'codex-desktop-instance-42-t0-i1-000001-0.log'
    new.parent.mkdir(parents=True)
    new.write_text('new day\n',encoding='utf8')
    reader=RouteLog(tmp_path)
    assert reader.poll(42,0).thread_id==A
    with new.open('a',encoding='utf8') as file:
        file.write(f'info thread_stream_view_activity_changed active=false conversationId={A} rendererWindowId=1\n')
    assert reader.poll(42,.1) is None
    with new.open('a',encoding='utf8') as file:file.write(route(B))
    assert reader.poll(42,.2).thread_id==B


def source():
    session = Session(A, 'home', title='오버레이 검증')
    for i, (model, tier, cached) in enumerate((('gpt-6-astra', 'default', 9000),
                                              ('gpt-5.6-sol', 'priority', 1000),
                                              ('gpt-6-astra', '미확인', None))):
        session.add_usage(100+i, f'r{i}', {'input_tokens': 10000, 'cached_input_tokens': cached,
                         'cache_write_input_tokens': 0, 'output_tokens': 1000}, model, 'turn', 'high', tier)
    view = session.view(105)
    view.update(source='user', archived=False, turn_states={'turn': '완료'})
    view['history'][1].update(transport='HTTP/SSE', transport_source='response_id')
    view['history'][2].update(transport='WebSocket', transport_source='log_time')
    return view


def test_summary_reuses_cache_and_updates_price_corrections_and_title():
    view = source(); engine = AnalysisEngine(); summaries = OverlaySummaries()
    engine.ingest([view]); old = summaries.collect(engine)[0]
    engine.ingest([copy.deepcopy(view)])
    assert summaries.collect(engine)[0] is old
    view['history'][2]['cached'] = 5000
    view['history'][2]['service_tier'] = 'Standard'
    view['title'] = '새 제목'
    engine.ingest([view]); new = summaries.collect(engine)[0]
    assert new['title'] == '새 제목' and not new['partial'] and new['priced'] == 3
    assert new['cost'] > old['cost']


def route(tid=A, window='1', suffix=''):
    return (f'2026-09-19T01:00:00Z info IAB_LIFECYCLE received browser sidebar owner sync '
            f'windowId={window} ownerRoutePath=/local/{tid}{suffix}\n')


def logfile(tmp_path, pid=42, suffix='0'):
    path = tmp_path / '2026/09/19' / f'codex-desktop-example-{pid}-t0-i1-{suffix}.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_process_restart_log_rotation_and_multiple_windows_never_reuse_old_selection(tmp_path):
    path = logfile(tmp_path); path.write_text(route(), encoding='utf-8')
    reader = RouteLog(tmp_path)
    assert reader.poll(42, 0).thread_id == A
    assert reader.poll(43, .1) is None
    path2 = logfile(tmp_path, 43); path2.write_text(route(B), encoding='utf-8')
    assert reader.poll(43, 3).thread_id == B
    path3 = logfile(tmp_path, 43, '1'); path3.write_text('new file\n', encoding='utf-8')
    # Windows may give consecutive writes identical timestamps; make rotation explicit.
    import os
    newer = path2.stat().st_mtime_ns + 1_000_000_000
    os.utime(path3, ns=(newer, newer))
    assert reader.poll(43, 6) is None
    with path3.open('a', encoding='utf-8') as f: f.write(route()+route(B, '2'))
    assert reader.poll(43, 6.1) is None


def test_controller_clears_old_numbers_on_unknown_remote_or_duplicate_home(tmp_path):
    app = QApplication.instance() or QApplication([])
    controller = OverlayController(QSettings(str(tmp_path/'overlay.ini'), QSettings.IniFormat), native_enabled=False)
    try:
        engine = AnalysisEngine(); engine.ingest([source()])
        summaries = OverlaySummaries().collect(engine)
        controller.receive_snapshot({'overlay_sessions': summaries})
        controller.receive_target({'selection': Selection(A)})
        data, note = controller.content()
        assert data['id'] == A and note == ''
        controller.receive_target({'selection': Selection(B)})
        assert controller.content()[0] is None
        controller.receive_target({'selection': Selection(A, 'remote:test')})
        assert controller.content()[0] is None
        controller.receive_target({'selection': Selection(A)})
        controller.receive_snapshot({'overlay_sessions': summaries+[dict(summaries[0], home='other')]})
        assert controller.content()[0] is None
        controller.receive_snapshot({'overlay_sessions': summaries})
        controller.snapshot_at -= 11
        assert controller.content()[1] == '수집 지연'
        controller.set_enabled(False)
        assert not controller.widget.isVisible()
    finally:
        controller.stop(); app.processEvents()


def test_settings_is_the_single_persisted_overlay_control(tmp_path):
    from cachemonitor.app import Dashboard
    from cachemonitor.analysis_worker import AnalysisBridge
    from cachemonitor.overlay import install_overlay
    app = QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration', True)
    settings = QSettings(str(tmp_path/'tray.ini'), QSettings.IniFormat)
    window = Dashboard([], start_worker=False, settings=settings)
    window.worker = AnalysisBridge([], static_snapshot={})  # signals only; no collector or native tracker
    controller = install_overlay(window, native_enabled=False)
    try:
        assert [a.text() for a in window.tray.contextMenu().actions() if not a.isSeparator()]==['대시보드 열기','종료']
        action = window.overlay_action
        assert action.isChecked() and controller.enabled
        action.trigger()
        assert not controller.enabled and not action.isChecked()
        assert settings.value('overlay/enabled', True, type=bool) is False
        restored = OverlayController(settings, native_enabled=False)
        assert not restored.enabled
        restored.stop()
        action.trigger()
        assert action.isChecked() and controller.enabled
        assert not hasattr(controller, 'hotkey_registered')
        assert 'position' not in window.settings_page.controls
        controller.anchor=(.2,.3);controller.end_drag()
        restored = OverlayController(settings, native_enabled=False)
        assert restored.anchor == (.2,.3)
        restored.stop()
        window.settings_page.controls['reset_position'].activate()
        assert controller.position == 'bottom-right'
        assert controller.anchor is None
        assert 'collapsed' not in window.settings_page.controls and 'opacity' not in window.settings_page.controls
        controller.actions.view.collapsePanel()
        assert controller.collapsed
        controller.set_collapsed(False)
        controller.toolbar.slider.setValue(35)
        assert controller.opacity==65
        controller.set_opacity(80)
        assert controller.opacity==80 and settings.value('overlay/opacity',type=int)==80
        assert not any(a.text()=='오버레이 위치' for a in window.tray.contextMenu().actions())
        check = window.settings_page.controls['overlay']
        check.setChecked(False)
        assert not action.isChecked() and not controller.enabled
        action.trigger()
        assert check.isChecked() and controller.enabled
    finally:
        controller.stop(); window.quitting = True; window.tick.stop(); window.tray.hide(); window.close()
        app.setProperty('cachemonitorDisableShellIntegration', False)
        app.processEvents()
