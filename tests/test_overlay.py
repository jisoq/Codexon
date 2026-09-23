import copy
import time

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.analytics import analyze
from cachemonitor.core import Session
from cachemonitor.overlay import OverlayController, SessionOverlay
from cachemonitor.overlay_data import OverlaySummaries, token_composition
from cachemonitor.overlay_tracking import RouteLog, Selection, overlay_geometry
from cachemonitor.overlay_windows import supported_codex_release


A = '11111111-1111-1111-1111-111111111111'
B = '22222222-2222-2222-2222-222222222222'


@pytest.mark.parametrize('version,expected',[
    ('26.915.100.0',True),('26.917.6896.0',True),
    ('26.916.1.0',False),('26.918.1.0',False),('27.0.0.0',False),
])
def test_desktop_overlay_supports_only_verified_route_formats(version,expected):
    assert supported_codex_release(version) is expected


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


def test_summary_matches_existing_costs_and_uses_last_call_as_one_unit():
    view = source(); engine = AnalysisEngine(); engine.ingest([view])
    summaries = OverlaySummaries(); summary = summaries.collect(engine)[0]
    expected = analyze([view], 0, 1000)
    assert summary['cost'] == expected['totals']['cost']
    assert summary['mean_cost'] == pytest.approx(summary['cost']/2)
    assert (summary['priced'], summary['missing'], summary['calls']) == (2, 1, 3)
    assert summary['partial'] and summary['assumed'] == 0
    assert (summary['model'], summary['effort'], summary['mode']) == ('gpt-6-astra', 'high', '미확인')
    assert summary['transport'] == 'WebSocket · 추정'
    assert summary['latest_cost'] is None and summary['cache_rate'] is None
    # UI filters or another session must not change a selected session's lifetime summary.
    child = copy.deepcopy(view); child['id'] = B; child['source'] = 'subagent'
    engine.ingest([view, child])
    engine.query({'page': 1, 'start': 101, 'end': 102, 'model': 'gpt-5.6-sol', 'service_tier': 'Fast'})
    assert summaries.collect(engine)[0] == summary


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


def test_confirmed_model_mismatch_shows_both_names_and_preserves_effort_mode():
    app = QApplication.instance() or QApplication([])
    view = source(); row = view['history'][-1]
    row.update(requested_model='gpt-6-astra', response_model='gpt-5.6-sol',
               model_match='불일치', model_alert_confirmed=False, response_status='completed')
    engine = AnalysisEngine(); engine.ingest([view]); summaries = OverlaySummaries()
    widget = SessionOverlay()
    try:
        data = summaries.collect(engine)[0]
        assert not data['model_mismatch']
        widget.set_content(data)
        assert '→' not in widget.accessibleName()
        row['model_alert_confirmed'] = True
        engine.ingest([view]); data = summaries.collect(engine)[0]; widget.set_content(data)
        assert '모델 불일치 (요청:gpt-6-astra, 응답:gpt-5.6-sol)' in widget.accessibleName()
        assert 'high' in widget.accessibleName() and '미확인' not in widget.accessibleName() and 'Standard 가정' not in widget.accessibleName()
        assert widget.height() == widget.panel_height() < 720
        assert widget.lines()[0] == view['title']
    finally: widget.close(); app.processEvents()


def route(tid=A, window='1', suffix=''):
    return (f'2026-09-19T01:00:00Z info IAB_LIFECYCLE received browser sidebar owner sync '
            f'windowId={window} ownerRoutePath=/local/{tid}{suffix}\n')


def logfile(tmp_path, pid=42, suffix='0'):
    path = tmp_path / '2026/09/19' / f'codex-desktop-example-{pid}-t0-i1-{suffix}.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_standalone_process_reads_physical_msix_logs_when_alias_is_missing(tmp_path):
    local = tmp_path/'Codex/Logs'
    packaged = tmp_path/'Packages/OpenAI.Codex_test/LocalCache/Local/Codex/Logs'
    path = logfile(packaged); path.write_text(route(), encoding='utf-8')
    reader = RouteLog(local, [packaged])
    assert not local.exists()
    assert reader.poll(42, 0).thread_id == A
    assert reader.path == path
    assert reader.poll(43, 1) is None


def test_newest_matching_process_log_wins_across_physical_and_alias_roots(tmp_path):
    import os
    local, packaged = tmp_path/'alias', tmp_path/'physical'
    stale = logfile(local); stale.write_text(route(A), encoding='utf-8')
    current = logfile(packaged); current.write_text(route(B), encoding='utf-8')
    os.utime(stale, (10, 10)); os.utime(current, (20, 20))
    reader = RouteLog(local, [packaged])
    assert reader.poll(42, 0).thread_id == B
    os.utime(stale, (30, 30))
    assert reader.poll(42, 3).thread_id == A


def test_route_tail_handles_partial_writes_secondary_views_and_navigation(tmp_path):
    path = logfile(tmp_path); path.write_text(route(), encoding='utf-8')
    reader = RouteLog(tmp_path)
    assert reader.poll(42, 0).thread_id == A
    with path.open('a', encoding='utf-8') as f:
        f.write(f'info thread_stream_view_activity_changed active=true conversationId={B} rendererWindowId=1\n')
        f.write(route(B).rstrip('\n'))
    # Incomplete route records must not be accepted or skipped.
    assert reader.poll(42, .1) is None
    with path.open('a', encoding='utf-8') as f: f.write('\n')
    assert reader.poll(42, .2).thread_id == B
    reader.consume(f'info thread_stream_view_activity_changed active=false conversationId={B} rendererWindowId=1')
    assert reader.poll(42, .3) is None
    reader.consume(route('client-new-thread:temporary'))
    assert reader.poll(42, .4).thread_id is None
    reader.consume(route(A, suffix='?hostId=remote-control%3Atest'))
    assert reader.poll(42, .5).host == 'remote-control:test'


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


@pytest.mark.parametrize('dpi', [96, 120, 144, 192])
def test_bottom_right_physical_geometry_including_negative_monitor(dpi):
    scale = dpi/96
    frame = (-3840, -200, -640, 1900)
    x, y, w, h = overlay_geometry(frame, dpi)
    assert (w, h) == (round(380*scale), round(212*scale))
    assert frame[2]-(x+w) == frame[3]-(y+h) == round(16*scale)
    tx, ty, tw, th = overlay_geometry(frame, dpi, position='top-right')
    assert (tx, tw, th) == (x, w, h)
    assert ty-frame[1] == round(16*scale)
    assert overlay_geometry((0, 0, 100, 100), dpi) is None


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


def test_glass_render_is_translucent_but_text_remains_opaque(tmp_path):
    app = QApplication.instance() or QApplication([])
    widget = SessionOverlay()
    engine = AnalysisEngine(); engine.ingest([source()])
    data = OverlaySummaries().collect(engine)[0]
    data['request']=dict(turn='t',calls=3,priced=2,cost=1.5,partial=True,state='완료')
    try:
        for dark in (True, False):
            widget.set_content(data, dark=dark)
            image = widget.grab().toImage()
            assert image.pixelColor(0, 0).alpha() == 0
            # Inspect logical background point with DPR accounted for.
            scale = image.devicePixelRatio()
            assert 210 <= image.pixelColor(round(12*scale), round(100*scale)).alpha() < 255
            text_pixels = [image.pixelColor(x, y).alpha()
                           for x in range(round(22*scale), round(200*scale))
                           for y in range(round((114)*scale), round((158)*scale))]
            assert max(text_pixels) == 255
        assert '미확인' not in widget.accessibleName()
        assert '산정 2 / 3호출' in widget.accessibleName()
    finally:
        widget.close(); app.processEvents()


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


def test_token_bars_partition_input_output_and_distinguish_denominators():
    result = token_composition({'history': [
        {'input': 1000, 'cached': 700, 'written': 100, 'output': 500, 'reasoning': 400},
        {'input': 2000, 'cached': 1000, 'written': 0, 'output': 500, 'reasoning': 100}]})
    parts = {p['key']: p['tokens'] for p in result['parts']}
    assert result['total'] == 4000 and not result['partial']
    assert parts == {'uncached': 1200, 'cached': 1700, 'written': 100, 'output': 500, 'reasoning': 500}
    assert sum(p['share'] for p in result['parts']) == pytest.approx(1)
    assert result['cache_hit_rate'] == pytest.approx(1700/3000*100)
    assert result['cached_share'] == 1700/3000
    assert result['non_cache_total'] == 2300
    assert max(p['bar_share'] for p in result['bars']) == 1
    assert result['bars'][0]['bar_share'] == 1
    assert result['bars'][0]['share'] == 1200/4000


def test_token_bars_keep_missing_subcategories_and_unclassified_history_explicit():
    result = token_composition({'history': [
        {'input': 1000, 'cached': 600, 'written': None, 'output': 200, 'reasoning': None}],
        'unclassified': {'total': 500}})
    assert result['total'] == 1200 and not result['partial']
    assert {p['key']: p['tokens'] for p in result['parts']} == {'cached': 600, 'unknown': 600}
    assert (result['counts']['input_unknown'], result['counts']['output_unknown'], result['counts']['unknown']) == (400, 200, 0)
    partial = token_composition({'history': [{'input': 10, 'cached': 0, 'written': 0}]})
    assert partial['total'] == 10 and partial['partial']
    assert not result['cache_hit_partial'] and result['cache_hit_rate'] == 60
    empty = token_composition({'history': []})
    assert empty['total'] == 0 and not empty['partial'] and not empty['parts']
    assert empty['cache_hit_rate'] is None
    assert len(empty['bars']) == 4 and all(p['bar_share'] == 0 for p in empty['bars'])


def test_dominant_cache_is_excluded_from_bar_scale_and_zero_categories_stay_visible():
    result = token_composition({'history': [
        {'input': 999000, 'cached': 998000, 'written': 0, 'output': 1000, 'reasoning': 800}]})
    assert result['cached_share'] == 998000/999000
    assert [p['bar_share'] for p in result['bars']] == [1, 0, .2, .8]
    assert [p['share'] for p in result['bars']] == [.001, 0, .0002, .0008]
    only_cache = token_composition({'history': [
        {'input': 100, 'cached': 100, 'written': 0, 'output': 0, 'reasoning': 0}]})
    assert only_cache['cache_hit_rate'] == 100 and only_cache['non_cache_total'] == 0
    assert all(p['bar_share'] == 0 for p in only_cache['bars'])


@pytest.mark.parametrize('dark',[True,False])
def test_rendered_non_cache_bars_keep_semantic_colors_without_a_ribbon(dark):
    from dataclasses import replace
    from cachemonitor.overlay_appearance import default_appearance
    from cachemonitor.token_colors import token_palette
    from PySide6.QtTest import QTest
    app=QApplication.instance() or QApplication([]);widget=SessionOverlay()
    engine=AnalysisEngine();engine.ingest([source()]);data=OverlaySummaries().collect(engine)[0]
    data['token_composition']=token_composition({'history':[dict(input=1600,cached=400,written=300,output=900,reasoning=400)]})
    try:
        observed=[]
        for accent in ('#ff00ff','#00ff00'):
            widget.set_content(data,appearance=replace(default_appearance(dark),accent=accent));widget.show();QTest.qWait(40)
            image=widget.grab().toImage();scale=image.devicePixelRatio()*widget.appearance.scale
            colors=[]
            for x,key in ((16,'input_parts'),(190,'output_parts')):
                offset=0
                for part in data['token_composition'][key]:
                    length=166*part['share']
                    if length>2:
                        pixel=image.pixelColor(round((x+offset+length/2)*scale),round((widget.content_model.layout()['tokens']+55)*scale)).name()
                        assert pixel==token_palette(widget.appearance)[part['key']].lower()
                        colors.append(pixel)
                    offset+=length
            observed.append(colors)
        assert all(len(colors)==len(set(colors)) for colors in observed)
    finally:widget.close();app.processEvents()


def test_missing_or_invalid_cache_does_not_become_a_zero_hit_call():
    result = token_composition({'history': [
        {'input': 100, 'cached': 80, 'written': 0, 'output': 0},
        {'input': 1000, 'cached': None, 'output': 0},
        {'input': 1000, 'cached': 1100, 'output': 0}]})
    assert result['cache_hit_partial'] and result['cache_hit_rate'] == 80


@pytest.mark.parametrize('source,kind', [('response_id','WebSocket'), ('log_time','HTTP/SSE'),
                                       ('conflict','WebSocket'), ('unknown','미확인')])
def test_overlay_and_dashboard_share_transport_label(source, kind):
    from cachemonitor.core import transport_label
    from cachemonitor.overlay_data import summarize_session
    row = {'ts': 1, 'cost': 1, 'transport': kind, 'transport_source': source}
    view = {'id': A, 'home': 'home', 'history': [row]}
    assert summarize_session(view)['transport'] == transport_label(row)
