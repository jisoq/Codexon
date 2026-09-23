import copy
import time
import threading
import sqlite3
import pytest

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.analysis_delivery import SnapshotPublisher, SnapshotReceiver
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.model_evidence import EvidenceStore, EvidenceReader
from cachemonitor.evidence_writer import EvidenceWriter
from cachemonitor.model_proxy import Tracker
from test_overlay import source


def snapshot(session):
    return dict(ts=time.time(),sessions=[session],homes=[session['home']],errors=[],unassigned=[])


def test_delta_heartbeat_correction_deletion_restart_and_old_results():
    engine=AnalysisEngine();summaries=OverlaySummaries();publisher=SnapshotPublisher();receiver=SnapshotReceiver()
    s=source();engine.ingest([s])
    first=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    full=receiver.receive(first)
    assert 'history' not in full['sessions'][0]
    assert full['overlay_sessions'][0]['cache_misses']['events']==[]
    assert receiver.receive(first) is None
    idle=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    assert not idle['sessions'] and not idle['overlay_sessions'] and not idle['model_candidates'] and not idle['cache_candidates']
    assert receiver.receive(idle)['sessions']==full['sessions']
    s=copy.deepcopy(s);s['history'][-1].update(requested_model='asked',response_model='reported',model_alert_confirmed=True,model_observation_ts=103)
    engine.ingest([s]);changed=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    assert changed['model_candidates'][0]['key']=='r2'
    assert receiver.receive(changed)['overlay_sessions'][0]['model_mismatch']
    engine.ingest([])
    deleted=publisher.publish(dict(ts=time.time(),sessions=[]),engine,summaries.collect(engine))
    assert not receiver.receive(deleted)['sessions']
    new=SnapshotPublisher();engine.ingest([s])
    reset=new.publish(snapshot(s),engine,summaries.collect(engine))
    assert receiver.receive(reset)['sessions']
    assert receiver.receive(changed) is None
    assert receiver.receive(first) is None
    gap=copy.deepcopy(reset);gap.update(reset=False,sequence=4)
    with pytest.raises(ValueError):receiver.receive(gap)


def test_spawned_analysis_dashboard_and_actual_tray_dispatch(tmp_path,monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    before=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    s=source();s['home']=str(tmp_path/'home')
    w=Dashboard([],settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat),static_snapshot=snapshot(s),live_limits=False)
    sent=[];monkeypatch.setattr(w.tray,'showMessage',lambda *args:sent.append(args))
    def until(condition):
        deadline=time.monotonic()+12
        while not condition() and time.monotonic()<deadline:app.processEvents();QTest.qWait(10)
        assert condition()
    try:
        until(lambda:bool(w.snapshot['sessions']))
        assert 'history' not in w.snapshot['sessions'][0]
        w.notification_master.setChecked(True);w.notification_options['모델명 불일치'].setChecked(True)
        now=time.time();store=EvidenceStore(tmp_path/'wire.sqlite');reader=EvidenceReader(store.path)
        try:
            store.write(s['home'],'attempt',now,'WebSocket','wire-response','asked','reported','completed')
            reader.poll()
            row={**s['history'][-1],'key':'wire-response','ts':now}
            changed=copy.deepcopy(s);changed['history'].extend(reader.enrich(s['home'],[row]))
        finally:reader.close();store.close()
        w.worker.replace_snapshot(snapshot(changed))
        until(lambda:len(sent)==1)
        assert '모델명 불일치' in sent[0][0]
        assert '요청: asked → 응답: reported' in sent[0][1]
        w.worker.replace_snapshot(snapshot(changed));QTest.qWait(200);app.processEvents()
        assert len(sent)==1
        w.model.setCurrentIndex(max(0,w.model.findData('gpt-6-astra')))
        filters=(w.period.currentData(),w.model.currentData())
        w.open_record(dict(home=s['home'],sid=s['id'],key='wire-response'))
        until(lambda:getattr(w,'exact_record',None) is not None)
        assert w.exact_record['key']=='wire-response'
        assert w.current_page==2 and w.detail_scroll.isVisible() and not w.qml_errors
        assert w.selected_session==(s['home'],s['id']) and w.selected_call=='wire-response'
        assert w.grab().save(str(tmp_path/'exact-record.png'))
        assert w.page_filters[0]['model']==filters[1]
        w.go_back()
        until(lambda:w.current_page==0)
        assert filters==(w.period.currentData(),w.model.currentData())
    finally:w.quit_app();app.setProperty('cachemonitorDisableShellIntegration',before)


def test_writer_copies_bounds_memory_marks_gaps_and_flushes_on_owner_thread(tmp_path):
    entered=threading.Event();release=threading.Event();owners=[]
    class SlowStore(EvidenceStore):
        def __init__(self,path):
            owners.append(threading.get_ident());super().__init__(path)
        def write(self,**row):
            entered.set();assert release.wait(5)
            assert threading.get_ident()==owners[0];super().write(**row)
    writer=EvidenceWriter(tmp_path/'wire.sqlite',capacity=2,store_factory=SlowStore)
    now=time.time();record=dict(attempt='a',ts=now,transport='WebSocket',response_id='r',requested_model='asked',response_model='reported',status='created')
    try:
        assert writer.write('home',**record);assert entered.wait(5)
        record['status']='completed'
        assert writer.write('home',**record)
        record['response_model']='MUTATED'
        assert writer.write('home',**dict(record,attempt='b'))
        started=time.monotonic()
        for _ in range(1000):assert not writer.write('home',**record)
        assert time.monotonic()-started<1 and writer.queue.qsize()==2
    finally:release.set();assert writer.close()
    assert owners[0]!=threading.get_ident() and writer.dropped==1000
    db=sqlite3.connect(writer.path)
    assert db.execute("select response_model,status from model_observations where attempt='a' order by seq").fetchall()==[('reported','created'),('reported','completed')]
    db.close()
    reader=EvidenceReader(writer.path);reader.poll()
    try:
        row=reader.enrich('home',[dict(key='r',ts=now)])[0]
        assert row['observation_missing'] and not row['model_alert_confirmed'] and row['model_match']=='관측 누락'
        assert row['model_evidence']=='저장 관측 누락'
    finally:reader.close()


def test_writer_lock_failure_does_not_retry_request_and_recovers(tmp_path):
    class FailedOnce(EvidenceStore):
        def write(self,**row):
            if row['status']=='pending':raise sqlite3.OperationalError('locked')
            return super().write(**row)
    writer=EvidenceWriter(tmp_path/'locked.sqlite',store_factory=FailedOnce)
    health=dict(storage_errors=0,requests=0,responses=0)
    tracker=Tracker(writer,'home','WebSocket',health)
    tracker.request(dict(model='asked'))
    tracker.response(dict(type='response.completed',response=dict(id='r',model='reported')))
    assert writer.close()
    assert health['requests']==1 and writer.errors==1
    reader=EvidenceReader(writer.path);reader.poll()
    try:assert not reader.enrich('home',[dict(key='r')])[0]['model_alert_confirmed']
    finally:reader.close()


def test_latency_uses_request_observation_and_monotonic_endpoints(tmp_path,monkeypatch):
    ticks=[10.];wall=[100.]
    monkeypatch.setattr('cachemonitor.model_proxy.time.monotonic',lambda:ticks[0])
    monkeypatch.setattr('cachemonitor.model_proxy.time.time',lambda:wall[0])
    store=EvidenceStore(tmp_path/'timing.sqlite')
    tracker=Tracker(store,'home','WebSocket',dict(storage_errors=0,requests=0,responses=0))
    tracker.request(dict(model='m'))
    ticks[0]=12;wall[0]=90
    tracker.response(dict(type='response.created',response=dict(id='r',model='m')))
    ticks[0]=13
    tracker.response(dict(type='response.in_progress',response=dict(id='r')))
    ticks[0]=18
    tracker.response(dict(type='response.completed',response=dict(id='r',model='m')))
    reader=EvidenceReader(store.path);reader.poll()
    try:
        r=reader.enrich('home',[dict(key='r')])[0]
        assert r['generation_latency_ms']==3000 and r['completion_latency_ms']==8000
        assert r['request_observed_at']==100 and r['completed_observed_at']==90
        tracker.response(dict(type='response.completed',response=dict(id='unpaired',model='m')))
        reader.poll();assert reader.enrich('home',[dict(key='unpaired')])[0]['completion_latency_ms'] is None
    finally:reader.close();store.close()


def test_one_response_does_not_invalidate_unrelated_home_session(tmp_path):
    store=EvidenceStore(tmp_path/'wire.sqlite');reader=EvidenceReader(store.path)
    a=[dict(key='a')];b=[dict(key='b')]
    try:
        reader.poll();before=reader.enrich('home',b,'other')
        reader.enrich('home',a,'first')
        store.write('home','a',100,'WebSocket','a','a','b','completed');reader.poll()
        assert reader.enrich('home',b,'other') is before
        assert reader.enrich('home',a,'first')[0]['model_alert_confirmed']
    finally:reader.close();store.close()


def test_only_explicit_wire_cache_policy_is_joined(tmp_path):
    from cachemonitor.proxy_observation import message_metadata
    store=EvidenceStore(tmp_path/'policy.sqlite');reader=EvidenceReader(store.path)
    tracker=Tracker(store,'home','WebSocket',dict(storage_errors=0,requests=0,responses=0))
    try:
        tracker.request(message_metadata('{"model":"m","prompt_cache_retention":"24h","input":"PRIVATE"}'))
        tracker.response(dict(type='response.completed',response=dict(id='r',model='m')))
        reader.poll()
        assert reader.enrich('home',[dict(key='r')])[0]['cache_policy']=='24h'
        assert reader.enrich('other-home',[dict(key='r')])[0]['cache_policy'] is None
        tracker.request(dict(model='m'));tracker.response(dict(type='response.completed',response=dict(id='unknown',model='m')))
        reader.poll();assert reader.enrich('home',[dict(key='unknown')])[0]['cache_policy'] is None
    finally:reader.close();store.close()


@pytest.mark.parametrize('transport',['WebSocket','HTTP/SSE'])
def test_relay_forwards_while_sqlite_writer_is_blocked(tmp_path,transport):
    import asyncio
    import json
    from aiohttp import web,ClientSession
    from cachemonitor.model_proxy import create_app
    from test_model_proxy import server,run_proxy_test
    entered=threading.Event();release=threading.Event()
    class SlowStore(EvidenceStore):
        def write(self,**record):
            entered.set();assert release.wait(8);super().write(**record)
    writer=EvidenceWriter(tmp_path/'slow.sqlite',capacity=2,store_factory=SlowStore)
    request={'type':'response.create','model':'asked','input':'PRIVATE_REQUEST'}
    response={'type':'response.completed','response':{'id':'relay','model':'reported','output':'PRIVATE_OUTPUT'}}
    received=[]
    async def run():
        async def upstream(req):
            if transport=='WebSocket':
                ws=web.WebSocketResponse();await ws.prepare(req)
                received.append(await ws.receive_json());await ws.send_json(response);await ws.close();return ws
            received.append(await req.json())
            return web.Response(body=('data: '+json.dumps(response)+'\n\n').encode())
        app=web.Application();app.router.add_route('*','/responses',upstream)
        async with server(app) as upstream_url,server(create_app(writer,'home',upstream_url)) as proxy,ClientSession() as client:
            if transport=='WebSocket':
                async with client.ws_connect(proxy+'/responses') as ws:
                    await ws.send_json(request)
                    assert await asyncio.wait_for(ws.receive_json(),3)==response
            else:
                async with client.post(proxy+'/responses',json=request) as res:
                    body=await asyncio.wait_for(res.read(),3)
                    assert json.loads(body.decode().removeprefix('data: '))==response
            assert not release.is_set() and received==[request]
    try:run_proxy_test(run())
    finally:release.set();assert writer.close()
    db=sqlite3.connect(writer.path)
    data=str(db.execute('select * from model_observations').fetchall());db.close()
    assert 'PRIVATE_REQUEST' not in data and 'PRIVATE_OUTPUT' not in data
