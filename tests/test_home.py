"""Home owns a stable today scope and routes to the existing analytical pages."""
import copy
import time
from datetime import datetime

import pytest

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session,day_start
from cachemonitor.home_panel import HomePanel
from cachemonitor.quota import quota_payload_display
from tests.test_ui import dashboard,wait_for_rendered


def test_home_today_projection_matches_prepared_calls_and_partial_coverage(tmp_path,monkeypatch):
    from cachemonitor import prepared_store as storage
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    from cachemonitor.prepared_ingest import prepare_owner,stage_owner
    now=datetime(2026,10,18,12).timestamp();today=day_start(now);sources=[]
    for sid,model in (('priced','gpt-6-astra'),('unknown','unknown-fixture-model')):
        session=Session(sid,'fixture',title=sid,cwd='V:/'+sid)
        for ordinal,stamp in enumerate((today-60,today+60,today+120)):
            session.add_usage(stamp,f'{sid}-{ordinal}',dict(input_tokens=100,cached_input_tokens=20,cache_write_input_tokens=10,output_tokens=10),
                              model,'request','medium',service_tier='Standard')
        source=copy.deepcopy(session.view(now));source.update(source='user',archived=True);sources.append(source)
    reference=AnalysisEngine();reference.ingest(sources);q=HomePanel.query(now)
    expected=reference.page_query(q)
    assert expected['analysis']['response_count']==4
    assert expected['analysis']['session_count']==2
    assert expected['usage']['summary']['known']==2
    assert expected['usage']['summary']['total']==4
    assert expected['usage']['summary']['partial']
    dependency={name:1 for name in storage.DEPENDENCIES};dependency['source_digest']='1'*64
    with storage.PreparedStore(tmp_path/'home.sqlite') as store:
        stage=store.begin(('home',1))
        for ordinal,source in enumerate(sources):stage_owner(stage,prepare_owner(source,owns=lambda *_:True),dependency,ordinal)
        token=stage.publish(('home',1),ready=True);engine=PreparedAnalysisEngine(store.path,token)
        try:
            def forbidden(*args,**kwargs):raise AssertionError('home must not decode call bodies')
            monkeypatch.setattr(storage.Snapshot,'_decode_call',forbidden)
            actual=engine.page_query(q)
            assert actual['analysis']==expected['analysis']
            assert actual['usage']['summary']==expected['usage']['summary']
            assert actual['valid_until']==datetime(2026,10,19).timestamp()
        finally:engine.close()


@pytest.mark.parametrize('kind',['finite','unlimited','stale','failure'])
def test_home_allowance_uses_sidebar_validity(kind):
    now=1000;quota=dict(source='live',observed_at=now,windows=dict(weekly=dict(used_percent=30,resets_at=2000)))
    payload=dict(quota=quota)
    if kind=='unlimited':quota.update(windows={},unlimited_windows=['weekly'])
    if kind=='stale':quota['observed_at']=900
    if kind=='failure':payload['quota_faults']=dict(lookup=dict(lookup_failed=True))
    display=quota_payload_display(payload,'weekly',now)
    assert display['remaining']==(70 if kind=='finite' else None)
    assert (display['state']=='제한 없음')==(kind=='unlimited')


@pytest.mark.integration
def test_home_routes_keep_native_pages_and_restore_sidebar(dashboard):
    from cachemonitor.quick_qa import click,render_plot
    w=dashboard;assert w.current_page==6 and not w.quota_sidebar.isVisible()
    w.resize(1000,680)
    for page in (3,1,5):
        click(w,render_plot(w,w.home_panel.links[page]));assert w.current_page==page
        assert w.quota_sidebar.isVisible()
        assert w.pages.nodes[page-1] is (w.scrollers[3] if page==3 else w.metric_panel if page==1 else w.performance_panel)
        w.change_page(6)
    click(w,render_plot(w,w.home_panel.connection));assert w.current_page==4 and w.quota_sidebar.isVisible()
    w.change_page(2);assert w.quota_sidebar.isVisible()
    assert not w.qml_errors


@pytest.mark.integration
def test_home_loading_masks_only_today_and_readiness_uses_collection_evidence(dashboard):
    w=dashboard;w.change_page(6);panel=w.home_panel
    w.show_analysis_message('홈 불러오는 중','')
    assert panel.today_summary._state['analysisLoading']['active']
    assert not panel.links[3]._state.get('analysisLoading')
    assert not panel.quota_cards['weekly']['value']._state.get('analysisLoading')
    evidence=dict(homes=['fixture'],sessions=[],usage_collection_complete=False)
    panel.apply_usage(dict(response_count=0,session_count=0),dict(value=0,known=0,total=0,partial=False))
    panel.apply_status(evidence,{},time.time());assert panel.readiness=='collecting'
    evidence['usage_collection_complete']=True
    panel.apply_status(evidence,{},time.time());assert panel.readiness=='empty'
    evidence['sessions']=[dict(id='empty-session',home='fixture')]
    panel.apply_status(evidence,{},time.time());assert panel.readiness=='empty'
    evidence['sessions'][0]['coverage_available']=True
    panel.apply_status(evidence,{},time.time());assert panel.readiness=='no_today'
    evidence['collection_fault']=dict(read_failed=True)
    panel.apply_status(evidence,{},time.time());assert panel.readiness=='failed'
    w.home_collection_status=dict(collection_fault=dict(runtime_failed=True))
    w.receive_collection_status(dict(collection_healthy=True))
    assert w.home_collection_status['collection_fault']=={}


@pytest.mark.integration
@pytest.mark.parametrize('facts,expected',[
    (dict(status_failed=True),'failed'),(dict(save_failed=True),'failed'),
    (dict(start_failed=True,starting=True),'failed'),(dict(starting=True),'collecting'),
    (dict(status_pending=True),'collecting'),(dict(expected_stop=True,runtime_failed=True),'stopped')])
def test_home_collection_status_overrides_confirmed_cached_records_and_recovers(dashboard,facts,expected):
    w=dashboard;w.snapshot.update(ts=time.time(),usage_collection_complete=True,collection_fault={})
    w.home_panel.apply_usage(dict(response_count=2,session_count=1),dict(value=1,known=2,total=2,partial=False))
    # Explicit facts remain authoritative even when the status envelope also
    # reports healthy. A later healthy response without facts clears old facts.
    w.receive_collection_status(dict(collection_healthy=True,collection_fault=facts))
    assert w.home_collection_status['collection_fault']==facts
    assert w.home_panel.readiness==expected
    w.receive_collection_status(dict(collection_healthy=True))
    assert w.home_collection_status['collection_fault']=={}
    assert w.home_panel.readiness=='ready'


@pytest.mark.integration
def test_home_preparation_with_old_snapshot_keeps_real_failures_visible(dashboard):
    w=dashboard;now=time.time()
    w.snapshot.update(ts=now-120,usage_collection_complete=False,collection_fault={},index={'loading':True})
    for pending in ('starting','status_pending','handoff_pending'):
        w.receive_collection_status(dict(collection_fault={pending:True},collection_healthy=False))
        w.check_stale()
        assert w.home_panel.readiness=='collecting'
        assert 'collection.freshness' not in w.error_state.sources
        for failure in ('read_failed','save_failed','start_failed','runtime_failed','status_failed'):
            w.receive_collection_status(dict(collection_fault={pending:True,failure:True},collection_healthy=False))
            assert w.home_panel.readiness=='failed'
    w.started_at=now-120
    w.receive_collection_status(dict(collection_fault={},collection_healthy=False))
    w.check_stale()
    assert w.error_state.sources['collection.freshness'].code=='stale'


@pytest.mark.integration
def test_home_saved_scroll_restores_rendered_viewport_and_consumes_pending_state(dashboard):
    import json
    from cachemonitor.quick_qa import flickable,render_plot
    w=dashboard;w.resize(1000,680);w.change_page(6)
    render_plot(w,w.home_panel.connection)
    scroll=w.scrollers[6];viewport=flickable(w,scroll)
    wait_for_rendered(lambda:viewport.property('contentY')>0)
    saved=w.capture_state();position=saved['scrolls']['6']
    assert position>0
    scroll.verticalPosition.setValue(0)
    wait_for_rendered(lambda:viewport.property('contentY')==0)
    w.restore_state(saved,initial=True);w.change_page(6)
    wait_for_rendered(lambda:abs(viewport.property('contentY')-position)<1)
    assert w._restore_positions is None
    assert json.loads(w.settings.value('dashboard/state'))['scrolls']['6']==pytest.approx(position)
    w.change_page(1)
    assert w._restore_positions is None


@pytest.mark.integration
def test_home_scope_and_identity_ignore_other_page_filters_but_expire_at_midnight(dashboard):
    from cachemonitor.dashboard import choose
    w=dashboard;w.change_page(2);choose(w.period,'all');choose(w.source,'user');w.archive.setChecked(False)
    w.change_page(6);q=w.query()
    assert q['period']=='today' and q['archived'] and q['home']==q['source']==q['model']==''
    tomorrow=dict(q,start=day_start(q['now'])+86400,end=q['end']+86400,now=q['now']+86400)
    assert w.query_identity(q)!=w.query_identity(tomorrow)


@pytest.mark.integration
@pytest.mark.parametrize('saved,expected',[(0,6),(99,6),(1,1),(3,3),(5,5),(6,6)])
def test_home_migrates_initial_page_without_replacing_saved_analysis_page(dashboard,saved,expected):
    w=dashboard;state=w.capture_state();state['page']=saved
    w.restore_state(state,initial=True)
    assert w.current_page==expected


@pytest.mark.integration
def test_dashboard_resizes_performance_from_rendered_content_width(dashboard):
    w=dashboard;w.change_page(5);w.resize(1000,700)
    wait_for_rendered(lambda:w.performance_panel.compact is True)
    w.resize(1500,900)
    wait_for_rendered(lambda:w.performance_panel.compact is False)
