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


@pytest.fixture
def overlay_cache_controller(tmp_path):
    app=QApplication.instance() or QApplication([])
    controller=OverlayController(QSettings(str(tmp_path/'cache.ini'),QSettings.IniFormat),native_enabled=False)
    requested=[]
    controller.collapsed=False;controller.expanded=False
    controller.refresh=lambda:None
    controller.overlay_loader=lambda *args,**kwargs:requested.append((args,kwargs))
    yield controller,requested
    controller.stop();app.processEvents()


def overlay_summary(sid, *, descendants=0, generation=1, revision=1):
    return dict(home='home',id=sid,_prepared_token=('test',generation),_overlay_revision=('test',revision),
        _recent_refs=True,recent=[{'key':sid}],latest={'key':sid},descendants=descendants,
        cost=3.5,child_cost=1.25,_collection={'last_confirmed_at':100.})


def overlay_select(controller,summaries,sid):
    controller.target_state={'selection':Selection(sid)}
    controller.session_lookup={key:[value] for key,value in summaries.items()}
    return controller.prepared_content(summaries[sid])


def overlay_reply(request, **extra):
    args,options=request
    summary=args[1]
    detail=dict(summary,_recent_refs=False,recent=[{'key':summary['id'],'output':100}],
                latest={'key':summary['id'],'output':100},**extra)
    return dict(id=args[0],summary=detail,prepared_token=summary['_prepared_token'])


@pytest.mark.parametrize('expanded',[False,True])
def test_overlay_revisits_reuse_complete_sessions_and_reject_changed_or_removed_ones(overlay_cache_controller,expanded):
    controller,requested=overlay_cache_controller;controller.expanded=expanded
    summaries={sid:overlay_summary(sid) for sid in (A,B)}
    for sid in (A,B):
        value,pending=overlay_select(controller,summaries,sid)
        assert pending and value is None
        assert requested[-1][1]==dict(health=False,finance=False)
        controller.receive_overlay_detail(overlay_reply(requested[-1]))
    summaries[A]=dict(summaries[A],_prepared_token=('test',2))
    controller.expanded=False
    value,pending=overlay_select(controller,summaries,A)
    assert not pending and value['latest']['output']==100 and len(requested)==2
    controller.expanded=expanded
    value,pending=overlay_select(controller,summaries,A)
    assert not pending and len(requested)==2
    overlay_select(controller,summaries,B)
    summaries[A]=dict(summaries[A],_prepared_token=('test',3),_overlay_revision=('test',3))
    value,pending=overlay_select(controller,summaries,A)
    assert pending and value is None and len(requested)==3
    controller.receive_snapshot({'overlay_sessions':[summaries[B]]})
    summaries[A]=overlay_summary(A)
    value,pending=overlay_select(controller,summaries,A)
    assert pending and value is None and len(requested)==4


@pytest.mark.parametrize('expanded',[False,True])
def test_overlay_reuses_verified_detail_across_unrelated_publications(overlay_cache_controller,expanded):
    controller,requested=overlay_cache_controller;controller.expanded=expanded
    data=overlay_summary(A,descendants=1)
    finance={'cost':1.25,'agents':[{'home':'home','id':B,'cost':1.25}]}
    overlay_select(controller,{A:data},A)
    controller.receive_overlay_detail(overlay_reply(requested[-1],subagent_finance=finance))
    for generation in range(2,22):
        current=dict(data,_prepared_token=('test',generation),speed_health={'active':True},
                     _collection={'received_age':generation})
        result,pending=overlay_select(controller,{A:current},A)
        assert not pending and result['latest']['output']==100
        assert result['_prepared_token']==current['_prepared_token']
        assert result['speed_health']==current['speed_health']
        assert result['_collection']==current['_collection']
        assert result['subagent_finance']==finance
    assert len(requested)==1
    changed=dict(current,_prepared_token=('test',22),_overlay_revision=('test',22))
    for generation in range(22,43):
        retained,pending=overlay_select(controller,{A:dict(changed,_prepared_token=('test',generation))},A)
        assert pending and retained['latest']['output']==100
        assert retained['_overlay_revision']==('test',1)
        assert retained['_prepared_token']==current['_prepared_token']
        assert retained['subagent_finance']==finance
    assert len(requested)==2
    # A duplicate obsolete completion cannot replace the coherent previous display.
    controller.receive_overlay_detail(overlay_reply(requested[0],subagent_finance=finance))
    assert controller._overlay_detail['_overlay_revision']==('test',1)


def test_overlay_accepts_inflight_detail_only_when_selected_dependencies_are_unchanged(overlay_cache_controller):
    controller,requested=overlay_cache_controller
    old=overlay_summary(A)
    overlay_select(controller,{A:old},A)
    current=dict(old,_prepared_token=('test',2))
    overlay_select(controller,{A:current},A)
    controller.receive_overlay_detail(overlay_reply(requested[0]))
    value,pending=overlay_select(controller,{A:current},A)
    assert not pending and value['_prepared_token']==('test',2)
    assert value['latest']['output']==100 and len(requested)==1


def test_overlay_caches_completed_offscreen_reply_without_disturbing_current_request(overlay_cache_controller):
    controller,requested=overlay_cache_controller
    summaries={sid:overlay_summary(sid) for sid in (A,B)}
    overlay_select(controller,summaries,A)
    overlay_select(controller,summaries,B)
    controller.receive_overlay_detail(overlay_reply(requested[0]))
    value,pending=overlay_select(controller,summaries,B)
    assert value is None and pending and len(requested)==2
    controller.receive_overlay_detail(overlay_reply(requested[1]))
    value,pending=overlay_select(controller,summaries,A)
    assert not pending and value['id']==A and value['latest']['output']==100
    assert len(requested)==2


def test_overlay_detail_upgrade_reuses_recent_and_preserves_acquired_capabilities(overlay_cache_controller):
    controller,requested=overlay_cache_controller
    summaries={A:overlay_summary(A)}
    overlay_select(controller,summaries,A)
    assert requested[-1][1]==dict(health=False,finance=False)
    controller.receive_overlay_detail(overlay_reply(requested[-1]))
    summaries[A]=dict(summaries[A],descendants=1)
    data,pending=overlay_select(controller,summaries,A)
    assert pending and data['cost']==3.5 and data['latest']['output']==100
    assert requested[-1][1]==dict(health=False,finance=True)
    assert not requested[-1][0][1]['_recent_refs']
    finance={'cost':1.25,'agents':[{'home':'home','id':B,'cost':1.25}]}
    controller.receive_overlay_detail(overlay_reply(requested[-1],subagent_finance=finance))
    controller.expanded=False
    overlay_select(controller,summaries,A)
    controller.expanded=True
    data,pending=overlay_select(controller,summaries,A)
    assert not pending and data['subagent_finance']==finance and len(requested)==2


@pytest.mark.parametrize('retirement',['changed','removed'])
def test_overlay_rejects_offscreen_reply_after_owner_retirement(overlay_cache_controller,retirement):
    controller,requested=overlay_cache_controller
    summaries={sid:overlay_summary(sid) for sid in (A,B)}
    overlay_select(controller,summaries,A)
    overlay_select(controller,summaries,B)
    old=requested[0]
    if retirement=='changed':summaries[A]=overlay_summary(A,generation=2,revision=2)
    else:summaries.pop(A)
    controller.receive_snapshot({'overlay_sessions':list(summaries.values())})
    controller.receive_overlay_detail(overlay_reply(old))
    summaries.setdefault(A,overlay_summary(A))
    value,pending=overlay_select(controller,summaries,A)
    assert pending and value is None and len(requested)==3


def test_overlay_late_base_reply_cannot_downgrade_cached_finance(overlay_cache_controller):
    controller,requested=overlay_cache_controller
    summaries={A:overlay_summary(A)}
    overlay_select(controller,summaries,A)
    summaries[A]=dict(summaries[A],descendants=1)
    overlay_select(controller,summaries,A)
    finance={'cost':1.25,'agents':[{'home':'home','id':B,'cost':1.25}]}
    controller.receive_overlay_detail(overlay_reply(requested[1],subagent_finance=finance))
    controller.receive_overlay_detail(overlay_reply(requested[0]))
    value,pending=overlay_select(controller,summaries,A)
    assert not pending and value['subagent_finance']==finance and len(requested)==2


def test_overlay_cached_owner_eviction_requires_a_fresh_read(overlay_cache_controller):
    controller,requested=overlay_cache_controller
    summaries={str(index):overlay_summary(str(index)) for index in range(9)}
    for sid in summaries:
        overlay_select(controller,summaries,sid)
        controller.receive_overlay_detail(overlay_reply(requested[-1]))
    value,pending=overlay_select(controller,summaries,'0')
    assert pending and value is None and len(requested)==10


def test_overlay_finance_extension_preserves_health_and_does_not_repeat_rows(overlay_cache_controller):
    from cachemonitor.overlay_navigation import NavigationTarget
    controller,requested=overlay_cache_controller
    summaries={A:overlay_summary(A),B:overlay_summary(B)}
    navigation=NavigationTarget('home',A,view='incident',event_id='incident')
    controller._pending_overlay_navigation=navigation
    resumed=[];controller.navigate_from_dashboard=resumed.append
    overlay_select(controller,summaries,A)
    health={'events':[{'id':'incident'}]}
    controller.receive_overlay_detail(overlay_reply(requested[-1],_health_detail=True,cache_degradation=health))
    assert resumed==[navigation]
    summaries[A]=dict(summaries[A],descendants=1)
    overlay_select(controller,summaries,A)
    payload=requested[-1][0][1]
    assert payload['_health_detail'] and payload['cache_degradation']==health
    assert not payload['_recent_refs'] and requested[-1][1]['health'] is False
    finance={'cost':1.25,'agents':[{'home':'home','id':B,'cost':1.25}]}
    controller.receive_overlay_detail(overlay_reply(requested[-1],subagent_finance=finance))
    overlay_select(controller,summaries,B)
    controller.receive_overlay_detail(overlay_reply(requested[-1]))
    data,pending=overlay_select(controller,summaries,A)
    assert not pending and data['subagent_finance']==finance and data['cache_degradation']==health
    assert data['_health_detail'] and len(requested)==3


@pytest.mark.parametrize('fault',['query_failed','fatal'])
def test_overlay_failed_read_recovers_after_fresh_snapshot(overlay_cache_controller,fault):
    controller,requested=overlay_cache_controller
    summary=overlay_summary(A)
    overlay_select(controller,{A:summary},A)
    controller.receive_overlay_fault({fault:True,'request_id':requested[0][0][0]})
    overlay_select(controller,{A:summary},A)
    assert len(requested)==1
    # A restarted worker can publish the same unchanged prepared generation.
    current=summary if fault=='fatal' else dict(summary,_prepared_token=('test',2))
    controller.receive_snapshot({'overlay_sessions':[current]})
    overlay_select(controller,{A:current},A)
    assert len(requested)==2
    controller.receive_overlay_detail(overlay_reply(requested[-1]))
    data,pending=overlay_select(controller,{A:current},A)
    assert not pending and data['latest']['output']==100


def test_overlay_banner_allows_idle_delivery_grace_and_identifies_failures(monkeypatch,overlay_cache_controller):
    from cachemonitor.overlay_view import OverlayContent
    monkeypatch.setattr('cachemonitor.overlay.time.monotonic',lambda:100.)
    controller,_=overlay_cache_controller
    controller.target_state={'selection':Selection(A)}
    controller.session_lookup={A:[dict(id=A,calls=2,request={'state':'완료'})]}
    controller.loading=False;controller.snapshot_at=84.;controller.snapshot_wall_time=1000.
    labels=[]
    for issue,errors,reason in (('',[],'analysis_delay'),('', ['read failure'],'source_error'),
                               ('status failure',[],'service_error'),('수집 결과 갱신 지연',[],'service_delay')):
        controller.collection_issue=issue;controller.errors=errors
        data,note=OverlayController.content(controller)
        assert data['calls']==2 and data['_collection']['reason']==reason
        labels.append(OverlayContent.collection_age(data,note))
    assert len(set(labels))==4
    assert '16' in labels[0] and '15' in labels[0]
    controller.collection_issue='';controller.errors=[]
    for age in (10.01,15.):
        controller.snapshot_at=100.-age
        data,note=OverlayController.content(controller)
        assert note=='' and not OverlayContent.collection_age(data,note)
        assert not data['_collection']['delayed']
    controller.snapshot_at=84.99
    data,note=OverlayController.content(controller)
    assert note=='수집 지연' and data['_collection']['reason']=='analysis_delay'
    assert data['_collection']['delayed']
    OverlayController.receive_snapshot(controller,dict(overlay_sessions=[dict(id=A,calls=2)],errors=[],ts=1000.))
    data,note=OverlayController.content(controller)
    assert note=='' and not data['_collection']['delayed']
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


def test_verified_session_displays_during_unrelated_restore_without_hiding_faults(monkeypatch,overlay_cache_controller):
    from cachemonitor.overlay_view import OverlayContent
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr('cachemonitor.overlay.time.monotonic',lambda:100.)
    session=dict(id=A,calls=2,collection_complete=True)
    controller,_=overlay_cache_controller
    controller.target_state={'selection':Selection(A)};controller.session_lookup={A:[session]}
    controller.loading=True;controller.snapshot_at=100.;controller.snapshot_wall_time=1000.
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


@pytest.mark.parametrize('pending',['starting','status_pending','handoff_pending'])
def test_preparing_collector_is_not_an_error_and_does_not_mask_failure(pending):
    from types import SimpleNamespace
    import time
    controller=SimpleNamespace(target_state={'selection':SimpleNamespace(thread_id=A,host='local')},
        session_lookup={},collection_issue='수집기 실행 정보 확인 대기',collection_fault={pending:True},
        errors=[],loading=True,snapshot_at=time.monotonic(),refresh=lambda:None)
    assert OverlayController.content(controller)==(None,'기록 준비 중')
    for failure in ('start_failed','runtime_failed','read_failed','save_failed','status_failed'):
        controller.collection_fault={pending:True,failure:True}
        assert OverlayController.content(controller)==(None,'수집 오류')
    controller.collection_fault={pending:True};controller.errors=['source read failed']
    assert OverlayController.content(controller)==(None,'수집 오류')


def test_preparation_keeps_confirmed_detail_while_replacement_is_loading(overlay_cache_controller):
    from cachemonitor.overlay_view import OverlayContent
    controller,requested=overlay_cache_controller
    old=overlay_summary(A);old.update(calls=2,collection_complete=True)
    overlay_select(controller,{A:old},A)
    controller.receive_overlay_detail(overlay_reply(requested[-1],calls=2,collection_complete=True))
    current=overlay_summary(A,generation=2,revision=2)
    current.update(calls=3,collection_complete=False)
    controller.session_lookup={A:[current]};controller.loading=True
    controller.target_state={'selection':Selection(A)}
    controller.collection_fault={'status_pending':True};controller.collection_issue='실행 정보 확인 대기'
    data,note=controller.content()
    model=OverlayContent();model.set_content(data,note)
    assert model.data['calls']==2 and model.data['cost']==old['cost']
    assert not model.notice_card() and model.checking_records


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
        controller.snapshot_at -= 16
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
