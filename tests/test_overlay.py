import copy
import sys

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QWidget

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay_tracking import RouteLog, Selection
from cachemonitor.overlay_windows import WindowsOverlay, observe_selection


A = '11111111-1111-1111-1111-111111111111'
B = '22222222-2222-2222-2222-222222222222'


@pytest.mark.parametrize('health',[False,True])
def test_overlay_reuses_verified_detail_across_unrelated_publications(health):
    from types import SimpleNamespace
    requested=[]
    data=dict(home='home',id=A,_prepared_token=('test',1),_overlay_revision=('test',1),
              _recent_refs=True,recent=[{'key':'call'}],latest={'key':'call'},
              cache_health={'count':1},speed_health={'active':False},_collection={'received_age':0})
    detail=dict(data,recent=[{'key':'call','output':100}],latest={'key':'call','output':100},
                _recent_refs=False,_health_detail=health,
                cache_health={'count':1,'events':[{'id':'incident'}]} if health else data['cache_health'])
    controller=SimpleNamespace(collapsed=False,expanded=health,_overlay_detail=detail,
        pending_overlay=None,_overlay_blocked=None,overlay_serial=0,
        overlay_loader=lambda *args,**kwargs:requested.append((args,kwargs)),
        overlay_identity=OverlayController.overlay_identity,
        reusable_overlay_detail=OverlayController.reusable_overlay_detail,
        refreshed_overlay_detail=OverlayController.refreshed_overlay_detail)
    for generation in range(2,22):
        current=dict(data,_prepared_token=('test',generation),speed_health={'active':True},
                     _collection={'received_age':generation})
        result,pending=OverlayController.prepared_content(controller,current)
        assert not pending and result['latest']['output']==100
        assert result['_prepared_token']==current['_prepared_token']
        assert result['speed_health']==current['speed_health']
        assert result['_collection']==current['_collection']
        assert result.get('_health_detail') is health
    assert requested==[]
    changed=dict(current,_prepared_token=('test',22),_overlay_revision=('test',22))
    assert OverlayController.prepared_content(controller,changed)==(None,True)
    for generation in range(23,43):
        assert OverlayController.prepared_content(controller,dict(changed,_prepared_token=('test',generation)))==(None,True)
    assert len(requested)==1
    controller.target_state={'selection':Selection(A)}
    controller.session_lookup={A:[changed]}
    controller.refresh=lambda:None
    controller._pending_overlay_navigation=None
    request=controller.pending_overlay[0]
    # An obsolete completion cannot overwrite the changed selected owner.
    OverlayController.receive_overlay_detail(controller,dict(id=request,summary=detail,
        prepared_token=detail['_prepared_token']))
    assert controller._overlay_detail['_overlay_revision']==('test',1)


def test_overlay_accepts_inflight_detail_only_when_selected_dependencies_are_unchanged():
    from types import SimpleNamespace
    old=dict(home='home',id=A,_prepared_token=('test',1),_overlay_revision=('test',1),
             _recent_refs=False,recent=[{'key':'call','output':100}],latest={'key':'call','output':100})
    current=dict(old,_prepared_token=('test',2),_recent_refs=True,recent=[{'key':'call'}],latest={'key':'call'})
    controller=SimpleNamespace(pending_overlay=('request',OverlayController.overlay_identity(old),False),
        target_state={'selection':Selection(A)},session_lookup={A:[current]},
        _overlay_detail=None,_overlay_blocked=None,_pending_overlay_navigation=None,refresh=lambda:None,
        overlay_identity=OverlayController.overlay_identity,
        reusable_overlay_detail=OverlayController.reusable_overlay_detail,
        refreshed_overlay_detail=OverlayController.refreshed_overlay_detail)
    OverlayController.receive_overlay_detail(controller,dict(id='request',summary=old,prepared_token=('test',1)))
    assert controller._overlay_detail['_prepared_token']==('test',2)
    assert controller._overlay_detail['latest']['output']==100


def test_overlay_banner_identifies_each_trigger_without_changing_stale_threshold(monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.overlay_view import OverlayContent
    monkeypatch.setattr('cachemonitor.overlay.time.monotonic',lambda:100.)
    controller=SimpleNamespace(target_state={'selection':SimpleNamespace(thread_id=A,host='local')},
        session_lookup={A:[dict(id=A,calls=2)]},collection_issue='',collection_fault={},errors=[],loading=False,
        snapshot_at=89.,snapshot_wall_time=1000.,refresh=lambda:None)
    controller.prepared_content=OverlayController.prepared_content.__get__(controller)
    labels=[]
    for issue,errors,reason in (('',[],'analysis_delay'),('', ['read failure'],'source_error'),
                               ('status failure',[],'service_error'),('수집 결과 갱신 지연',[],'service_delay')):
        controller.collection_issue=issue;controller.errors=errors
        data,note=OverlayController.content(controller)
        assert data['calls']==2 and data['_collection']['reason']==reason
        labels.append(OverlayContent.collection_age(data,note))
    assert len(set(labels))==4
    assert '11' in labels[0]
    controller.collection_issue='';controller.errors=[];controller.snapshot_at=90.
    data,note=OverlayController.content(controller)
    assert note=='' and not OverlayContent.collection_age(data,note)
    OverlayController.receive_snapshot(controller,dict(overlay_sessions=[dict(id=A,calls=2)],
        errors=[],collection_stale=True,ts=1001.))
    data,note=OverlayController.content(controller)
    assert note=='수집 지연' and data['_collection']['source_error_count']==0
    assert data['_collection']['service_fault']['snapshot_stale']
    assert controller.snapshot_wall_time==1000.
    OverlayController.receive_snapshot(controller,dict(overlay_sessions=[dict(id=A,calls=2)],
        errors=['read failure'],collection_stale=True,ts=1002.))
    data,note=OverlayController.content(controller)
    assert note=='수집 오류' and data['_collection']['reason']=='source_error'
    OverlayController.receive_snapshot(controller,dict(overlay_sessions=[dict(id=A,calls=2)],errors=[],ts=1003.))
    assert OverlayController.content(controller)[1]==''


def test_verified_session_displays_during_unrelated_restore_without_hiding_faults(monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.overlay_view import OverlayContent
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr('cachemonitor.overlay.time.monotonic',lambda:100.)
    session=dict(id=A,calls=2,collection_complete=True)
    controller=SimpleNamespace(target_state={'selection':Selection(A)},session_lookup={A:[session]},
        collection_issue='',collection_fault={},errors=[],loading=True,
        snapshot_at=100.,snapshot_wall_time=1000.)
    controller.prepared_content=OverlayController.prepared_content.__get__(controller)
    data,note=OverlayController.content(controller)
    assert not note and not data['_collection']['loading']
    content=OverlayContent();content.set_content(data,note)
    assert content.data['calls']==2
    for flag in (False,None):
        session['collection_complete']=flag
        data,note=OverlayController.content(controller)
        content.set_content(data,note)
        assert data['_collection']['loading'] and 'calls' not in content.data
    controller.loading=False
    assert OverlayController.content(controller)[1]==''
    controller.loading=True;session['collection_complete']=True
    controller.errors=['read failure']
    assert OverlayController.content(controller)[1]=='수집 오류'
    controller.errors=[];controller.collection_stale=True
    assert OverlayController.content(controller)[1]=='수집 지연'


def test_missing_session_retains_service_error_and_clears_on_recovery():
    from types import SimpleNamespace
    import time
    controller=SimpleNamespace(target_state={'selection':SimpleNamespace(thread_id=A,host='local')},
        session_lookup={},collection_issue='process access denied',errors=[],loading=False,
        snapshot_at=time.monotonic(),refresh=lambda:None)
    assert OverlayController.content(controller)==(None,'수집 오류')
    OverlayController.receive_collection_status(controller,{'phase':'active'})
    assert controller.collection_issue=='process access denied'
    OverlayController.receive_collection_status(controller,{'collection_issue':''})
    assert OverlayController.content(controller)==(None,'호출 기록 없음')
    controller.errors=['stale snapshot']
    assert OverlayController.content(controller)==(None,'수집 오류')
    controller.target_state['selection'].host='remote'
    assert OverlayController.content(controller)[1].startswith('원격 작업')


@pytest.mark.parametrize('version', ['26.915.100.0', '26.917.6896.0', '26.918.1.0', '27.0.0.0'])
@pytest.mark.parametrize('prefix,namespace', [('/local/', ''), ('/c/', ''), ('/c/local-chatgpt%3A', 'local-chatgpt:')])
def test_new_desktop_releases_require_live_route_evidence(tmp_path, version, monkeypatch, prefix, namespace):
    target = dict(hwnd=1, pid=42, version=version)
    reader = RouteLog(tmp_path)
    assert observe_selection([target], reader, 0)['selection'] is None
    path = logfile(tmp_path)
    path.write_text(route(A, prefix=prefix), encoding='utf-8')
    assert observe_selection([target], reader, 3)['selection'].thread_id == namespace+A
    # Navigation applies the same evidence rule, without a release allowlist.
    native = WindowsOverlay.__new__(WindowsOverlay)
    native.targets = lambda: [target]
    native._navigation_log = reader
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    assert native.confirm_selection(target).thread_id == namespace+A
    with path.open('a', encoding='utf-8') as stream:
        stream.write(route(B, prefix=prefix).rstrip('\n'))
    assert observe_selection([target], reader, 4)['selection'] is None
    assert native.confirm_selection(target) is None
    with path.open('a', encoding='utf-8') as stream:
        stream.write('\n')
    assert native.confirm_selection(target).thread_id == namespace+B


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


def test_summary_readiness_updates_without_repricing_and_waits_for_descendants():
    parent=source();parent.update(usage_revision=1,collection_complete=True)
    child=copy.deepcopy(parent);child.update(id=B,parent_thread_id=A,collection_complete=False)
    for row in child['history']:row['key']='child-'+row['key']
    engine=AnalysisEngine();summaries=OverlaySummaries()
    engine.ingest([parent,child]);first={s['id']:s for s in summaries.collect(engine)}
    assert not first[A]['collection_complete'] and not first[B]['collection_complete']
    cached=summaries.cache[(parent['home'],A)][1]
    child['collection_complete']=True
    engine.ingest([parent,child]);ready={s['id']:s for s in summaries.collect(engine)}
    assert ready[A]['collection_complete'] and ready[B]['collection_complete']
    assert ready[A]['cost']==first[A]['cost'] and ready[A]['calls']==first[A]['calls']
    assert summaries.cache[(parent['home'],A)][1] is cached
    assert summaries.collect(engine)[0] is ready[A]
    child['collection_complete']=False
    engine.ingest([parent,child]);pending={s['id']:s for s in summaries.collect(engine)}
    assert not pending[A]['collection_complete'] and not pending[B]['collection_complete']


def route(tid=A, window='1', suffix='', *, prefix='/local/'):
    return (f'2026-09-19T01:00:00Z info IAB_LIFECYCLE received browser sidebar owner sync '
            f'windowId={window} ownerRoutePath={prefix}{tid}{suffix}\n')


def logfile(tmp_path, pid=42, suffix='0'):
    path = tmp_path / '2026/09/19' / f'codex-desktop-example-{pid}-t0-i1-{suffix}.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.parametrize('prefix,namespace', [('/local/', ''), ('/c/', ''), ('/c/local-chatgpt%3A', 'local-chatgpt:')])
def test_route_activity_and_host_preserve_conversation_identity(tmp_path, prefix, namespace):
    path = logfile(tmp_path)
    raw = route(A, suffix='?hostId=remote%3Atest', prefix=prefix)
    path.write_text(raw, encoding='utf-8')
    reader = RouteLog(tmp_path)
    selected = reader.poll(42, 0)
    assert selected == Selection(namespace+A, 'remote:test', '1', raw.split('ownerRoutePath=')[1].strip())
    for tid, active, expected in ((B, 'false', selected), (namespace+A, 'false', None),
                                  (namespace+A, 'true', selected)):
        with path.open('a', encoding='utf-8') as stream:
            stream.write(f'info thread_stream_view_activity_changed active={active} conversationId={tid} rendererWindowId=1\n')
        assert reader.poll(42, .1) == expected


@pytest.mark.parametrize('invalid_path', ['/c/not-a-uuid', f'/c/{A}/extra', f'/c/{A}/',
                                         '/c/local-chatgpt%3Anot-a-uuid', f'/c/local-chatgpt%3A{A}%2Fextra',
                                         f'/unknown/{A}', f'/local/local-chatgpt:{A}'])
def test_unknown_route_clears_work_selection(tmp_path, invalid_path):
    path = logfile(tmp_path)
    path.write_text(route(A, prefix='/c/'), encoding='utf-8')
    reader = RouteLog(tmp_path)
    assert reader.poll(42, 0).thread_id == A
    with path.open('a', encoding='utf-8') as stream:
        stream.write(route('', prefix=invalid_path))
    assert reader.poll(42, .1).thread_id is None


@pytest.mark.skipif(sys.platform != 'win32', reason='Owned native Windows windows')
def test_work_routes_render_matching_records_and_missing_record_notice(tmp_path):
    from PySide6.QtTest import QTest

    app = QApplication.instance() or QApplication([])
    host = QWidget()
    host.resize(1100, 1000)
    host.show()
    controller = OverlayController(QSettings(str(tmp_path/'work-overlay.ini'), QSettings.IniFormat),
                                   native_enabled=False, appearance_path=tmp_path/'codex.toml')
    native = WindowsOverlay()
    controller.native = native
    # Visibility belongs to this test window, independent of desktop focus.
    native.visible_target = lambda hwnd: bool(native.u.IsWindowVisible(hwnd))
    reader = RouteLog(tmp_path)
    path = logfile(tmp_path)
    engine = AnalysisEngine()
    engine.ingest([source()])
    controller.receive_snapshot({'overlay_sessions': OverlaySummaries().collect(engine)})
    try:
        for index, (prefix, tid, expected) in enumerate((('/c/', A, A), ('/c/', B, None),
                                                       ('/c/local-chatgpt%3A', A, None), ('/local/', A, A))):
            with path.open('a', encoding='utf-8') as stream:
                stream.write(route(tid, prefix=prefix))
            controller.receive_target({'target': {'hwnd': int(host.winId())},
                                       'selection': reader.poll(42, index)})
            QTest.qWait(100)
            assert controller.widget.isVisible()
            assert (controller.widget.data or {}).get('id') == expected
            assert controller.widget.note == ('' if expected else '호출 기록 없음')
            assert not controller.widget.qml_errors
            assert controller.widget.grab().save(str(tmp_path/f'work-route-{index}.png'))
        with path.open('a', encoding='utf-8') as stream:
            stream.write(route(A, prefix='/unknown/'))
        controller.receive_target({'target': {'hwnd': int(host.winId())}, 'selection': reader.poll(42, 4)})
        assert not controller.widget.isVisible()
    finally:
        controller.stop()
        host.close()
        app.processEvents()


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
        assert 'reset_position' not in window.settings_page.controls
        assert controller.anchor == (.2,.3)
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
