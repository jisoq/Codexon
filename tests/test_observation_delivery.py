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


def _paused_analysis_peer(connection, waiting, release, interrupt):
    try:
        assert connection.poll(8)
        waiting.set()
        assert release.wait(8)
        first = connection.recv()
        assert first['kind'] == 'overlay_detail' and first['id'] == 1
        next_command = connection.recv()
        if not interrupt:
            assert next_command['kind'] == 'overlay_detail' and next_command['id'] == 100
            connection.send(dict(kind='overlay_detail', id=100, summary=next_command['summary']))
            next_command = connection.recv()
        assert next_command['kind'] == 'stop'
    finally:
        connection.close()


@pytest.mark.parametrize('interrupt', [False, True])
def test_bridge_coalesces_or_stops_while_a_command_is_blocked(monkeypatch, interrupt):
    import multiprocessing
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from cachemonitor import analysis_worker

    context = multiprocessing.get_context('spawn')
    waiting, release = context.Event(), context.Event()
    def process(**kwargs):
        return context.Process(target=_paused_analysis_peer,
            args=(kwargs['args'][0], waiting, release, interrupt), daemon=True)
    monkeypatch.setattr(analysis_worker.mp, 'get_context',
                        lambda *_:SimpleNamespace(Pipe=context.Pipe, Process=process))
    bridge = analysis_worker.AnalysisBridge([])
    received = threading.Event()
    bridge.overlay_detail.connect(lambda value:received.set(), Qt.DirectConnection)
    bridge.request_overlay_detail(1, dict(title='in-flight-' * 32768))
    bridge.start()
    try:
        assert waiting.wait(8), 'peer must observe the blocked command before the next requests'
        for request_id in range(2, 101):
            bridge.request_overlay_detail(request_id, dict(title='latest'))
        if interrupt:
            bridge.requestInterruption()
        release.set()
        if not interrupt:
            assert received.wait(8), 'only the latest unsent selection must complete'
            bridge.requestInterruption()
        assert bridge.wait(5000), 'stop must complete without terminating the analysis process'
        assert bridge.process.exitcode == 0
    finally:
        release.set()
        bridge.requestInterruption()
        if not bridge.wait(1000):
            bridge.process.terminate()
            assert bridge.wait(5000)


@pytest.mark.parametrize('during_stop', [False, True])
def test_bridge_send_failure_releases_a_live_peer(during_stop):
    from PySide6.QtCore import Qt
    from cachemonitor.analysis_worker import AnalysisBridge

    bridge = AnalysisBridge([], static_snapshot=snapshot(source()))
    faults = []
    entered, release = threading.Event(), threading.Event()
    class FailingPayload:
        def __reduce__(self):
            entered.set()
            assert release.wait(8)
            raise TypeError('test command cannot be serialized')
    def failed(value):
        faults.append(value)
        bridge.requestInterruption()
    bridge.fault.connect(failed, Qt.DirectConnection)
    bridge.request_overlay_detail(1, dict(unserializable=FailingPayload()))
    bridge.start()
    try:
        assert entered.wait(8)
        if during_stop:
            bridge.requestInterruption()
        release.set()
        assert bridge.wait(8000), 'failed sender must release the child through EOF'
        if not during_stop:
            assert faults and faults[0]['process_exited']
        assert bridge.process.exitcode == 0
    finally:
        release.set()
        bridge.requestInterruption()
        if not bridge.wait(1000):
            bridge.process.terminate()
            assert bridge.wait(5000)


def test_bridge_delivers_simultaneous_large_command_and_snapshot():
    from PySide6.QtCore import Qt
    from cachemonitor.analysis_worker import AnalysisBridge

    session = source()
    session['title'] = 'publication-' * 16384
    summary = dict(title='selected-detail-' * 16384)
    bridge = AnalysisBridge([], static_snapshot=snapshot(session))
    snapshots = []
    details = []
    received = threading.Event()

    def on_snapshot(value):
        snapshots.append(value)
        if details:
            received.set()

    def on_detail(value):
        details.append(value)
        if snapshots:
            received.set()

    bridge.snapshot.connect(on_snapshot, Qt.DirectConnection)
    bridge.overlay_detail.connect(on_detail, Qt.DirectConnection)
    bridge.request_overlay_detail(1, summary)
    bridge.start()
    try:
        assert received.wait(8), 'simultaneous command and publication must both arrive'
        assert snapshots[0]['sessions'][0]['title'] == session['title']
        assert details[0]['id'] == 1 and details[0]['summary'] == summary
        assert bridge.restart_count == 0
    finally:
        bridge.requestInterruption()
        stopped = bridge.wait(3000)
        if not stopped:
            # Release only this test's child if the regression deadlocks its pipes.
            if bridge.process is not None:
                bridge.process.terminate()
            assert bridge.wait(5000), 'analysis bridge did not release its process'
        if received.is_set():
            assert stopped, 'successful delivery must also allow graceful shutdown'


def test_delta_heartbeat_correction_deletion_restart_and_old_results():
    engine=AnalysisEngine();summaries=OverlaySummaries();publisher=SnapshotPublisher();receiver=SnapshotReceiver()
    s=source();engine.ingest([s])
    first=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    full=receiver.receive(first)
    assert not {'history','turn_records','unmatched_transports'}&full['sessions'][0].keys()
    assert full['overlay_sessions'][0]['cache_misses']['events']==[]
    assert receiver.receive(first) is None
    idle=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    assert not idle['sessions'] and not idle['overlay_sessions'] and not idle['model_candidates'] and not idle['cache_candidates']
    assert receiver.receive(idle)['sessions']==full['sessions']
    s=dict(s,collection_complete=False);engine.ingest([s])
    readiness=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    assert 'filter_choices' not in readiness and 'models' not in readiness
    current=receiver.receive(readiness)
    assert current['sessions'][0]['collection_complete'] is False
    assert current['filter_choices']==full['filter_choices']
    assert current['data_revision']!=full['data_revision']
    assert current['call_revision']==full['call_revision']
    s=copy.deepcopy(s);s['history'][-1].update(requested_model='asked',response_model='reported',model_alert_confirmed=True,model_observation_ts=103)
    engine.ingest([s]);changed=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    assert changed['call_revision']>current['call_revision']
    assert changed['model_candidates'][0]['key']=='r2'
    assert receiver.receive(changed)['overlay_sessions'][0]['model_mismatch']
    s=copy.deepcopy(s);s['history'][-1]['model_alert_confirmed']=False
    engine.ingest([s]);resolved=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    assert resolved['model_candidates'][0]['key']=='r2'
    assert resolved['model_candidates'][0]['model_alert_confirmed'] is False
    receiver.receive(resolved)
    stable=publisher.publish(snapshot(s),engine,summaries.collect(engine))
    assert not stable['model_candidates']
    receiver.receive(stable)
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


def test_prepared_envelope_advances_unchanged_detail_pins_only_after_acceptance():
    receiver=SnapshotReceiver()
    summary=dict(home='home',id='unchanged',_overlay_revision=('store',1),recent=[{'key':'call'}])
    first=dict(protocol=1,epoch='epoch',sequence=1,reset=True,prepared_token=('store',1),
               sessions=[],overlay_sessions=[summary],removed=[])
    original=receiver.receive(first)
    update=dict(first,sequence=2,reset=False,prepared_token=('store',2),overlay_sessions=[])
    current=receiver.receive(update)
    assert current['overlay_sessions'][0]['_prepared_token']==('store',2)
    assert current['overlay_sessions'][0]['_overlay_revision']==('store',1)
    assert original['overlay_sessions'][0]['_prepared_token']==('store',1)
    assert '_prepared_token' not in summary
    assert receiver.receive(first) is None
    with pytest.raises(ValueError):receiver.receive(dict(update,sequence=4,prepared_token=('store',4)))
    assert receiver.sequence==2
    final=receiver.receive(dict(update,sequence=3,prepared_token=('store',3)))
    assert final['overlay_sessions'][0]['_prepared_token']==('store',3)
    assert current['overlay_sessions'][0]['_prepared_token']==('store',2)


@pytest.mark.parametrize('revision',('recovery:1','live:1'))
def test_replacement_snapshot_releases_previous_raw_history(revision):
    import weakref
    class Observed(dict):pass
    engine=AnalysisEngine();summaries=OverlaySummaries();publisher=SnapshotPublisher()
    original=source();original['usage_revision']='recovery:1'
    original['history']=[Observed(row) for row in original['history']]
    retired=weakref.ref(original['history'][0])
    engine.ingest([original]);publisher.publish(snapshot(original),engine,summaries.collect(engine))
    replacement=copy.deepcopy(original);replacement['usage_revision']=revision
    del original
    engine.ingest([replacement])
    # Publication only needs change identities; deduplication only needs the
    # current source. Neither owns a retired raw population during next-view work.
    assert retired() is None
    value=publisher.publish(snapshot(replacement),engine,summaries.collect(engine))
    assert not value['model_candidates']
    assert value['overlay_sessions']==[]


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
