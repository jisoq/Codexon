"""Live collection preserves precision while checkpoints store substantive changes."""
import copy
import json
import socket
import sqlite3
import threading
import time

import pytest

from cachemonitor import collection_transport
from cachemonitor.usage_collection import CollectionChannel, CollectionClient, empty_snapshot


def snapshot(channel, at=100.):
    value=empty_snapshot(channel.homes,channel.path,at)
    value.update(usage_collection_complete=True,last_usage_collection_success=at,request_activity=[],quota_by_home={})
    value['index'].update(loading=False,usage_complete=True,last_usage_success=at)
    value['sessions']=[dict(home=channel.homes[0],id='session',history=[dict(key='one',output=3)],
                            remaining=10.,collection_complete=True,status='idle')]
    return value


def test_live_idle_preserves_exact_freshness_heavy_identity_and_zero_sql_writes(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path)
    writer.start_live()
    try:
        first=snapshot(writer);writer.publish(first,'epoch',1)
        result=reader.read(shared=True)
        history=result['sessions'][0]['history']
        changes=writer.db.total_changes
        consumer_changes=reader.db.total_changes
        for sequence in range(2,12):
            current=copy.deepcopy(first)
            current.update(ts=100.+sequence,last_usage_collection_success=100.+sequence)
            current['index'].update(last_usage_success=100.+sequence,bytes_read=sequence,done=sequence)
            current['sessions'][0]['remaining']=float(sequence)
            writer.publish(current,'epoch',sequence)
            received=reader.read(shared=True);reader.subscribe(current['ts'])
            assert received==current and reader.last_read_live
            assert received['sessions'][0]['history'] is history
        assert writer.db.total_changes==changes
        assert reader.db.total_changes==consumer_changes
        assert writer.db.execute('SELECT observed FROM snapshot').fetchone()[0]==first['ts']
        assert writer.live.requested_homes(time.time())==writer.homes
        # A new management channel must see current clocks without loading history.
        with_reader=CollectionChannel(writer.homes,writer.path)
        try:
            header=with_reader.read_header()
            assert header['ts']==current['ts'] and 'sessions' not in header
            assert with_reader.last_read_live
        finally:with_reader.close()
    finally:reader.close();writer.close()


@pytest.mark.parametrize('unchanged_records',[3,300])
def test_live_delta_encodes_and_decodes_only_changed_rows(tmp_path,monkeypatch,unchanged_records):
    from cachemonitor import usage_collection
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);writer.start_live()
    try:
        first=snapshot(writer)
        first['sessions'].append(dict(home=writer.homes[0],id='unchanged',
            history=[dict(key=str(i),output=i) for i in range(unchanged_records)]))
        first['request_activity']=[dict(home=writer.homes[0],attempt=key,state='started') for key in ('old','active')]
        writer.publish(first,'epoch',1)
        original=reader.read(shared=True)
        changed=copy.deepcopy(first);changed['ts']=103.
        changed['sessions'][0]['history'][0]['output']=4
        changed['request_activity'][1]['state']='completed'
        encoded=[];decoded=[]
        compress=usage_collection.zlib.compress;decompress=usage_collection.zlib.decompress
        def encode(value,*args,**kwargs):
            encoded.append(json.loads(value))
            return compress(value,*args,**kwargs)
        def decode(value,*args,**kwargs):
            result=decompress(value,*args,**kwargs);decoded.append(json.loads(result))
            return result
        monkeypatch.setattr(usage_collection.zlib,'compress',encode)
        monkeypatch.setattr(usage_collection.zlib,'decompress',decode)
        writer.publish(changed,'epoch',2)
        received=reader.read(shared=True)
        assert received==changed and reader.last_read_live
        # Growing an unrelated history must not add compressed or decoded records.
        assert len(encoded)==len(decoded)==2
        assert [row.get('id',row.get('attempt')) for row in encoded]==['session','active']
        assert [row.get('id',row.get('attempt')) for row in decoded]==['session','active']
        assert received['sessions'][1]['history'] is original['sessions'][1]['history']
        assert received['request_activity'][0] is original['request_activity'][0]
    finally:reader.close();writer.close()


def test_live_delta_converges_after_missed_publications_and_epoch_change(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);writer.start_live()
    try:
        first=snapshot(writer)
        first['sessions'].append(dict(home=writer.homes[0],id='removed',history=[]))
        first['request_activity']=[dict(home=writer.homes[0],attempt='removed',state='started')]
        writer.publish(first,'epoch',1);assert reader.read()==first
        changed=copy.deepcopy(first);changed['ts']=103.
        changed['sessions'][0]['history'][0]['output']=4
        writer.publish(changed,'epoch',2)
        latest=copy.deepcopy(changed);latest['ts']=106.;latest['sessions'].pop()
        latest['request_activity']=[dict(home=writer.homes[0],attempt='new',state='completed')]
        writer.publish(latest,'epoch',3)
        assert reader.read()==latest and reader.last_read_live
        assert len(reader.live_session_cache)==len(reader.live_activity_cache)==1
        # A replacement owner may restart its sequence. Its epoch requires full rows.
        restarted=copy.deepcopy(first);restarted['ts']=109.
        writer.publish(restarted,'replacement',1)
        assert reader.read()==restarted and reader.last_read_live
        assert len(reader.live_session_cache)==2 and len(reader.live_activity_cache)==1
    finally:reader.close();writer.close()


@pytest.mark.parametrize('live',[False,True],ids=['sqlite','live'])
def test_unrelated_session_change_does_not_rescan_subagent_quota_history(tmp_path,live):
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.core import Session
    from cachemonitor.quota_cycles import QuotaLedger
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path)
    ledger=QuotaLedger(tmp_path/'quota.sqlite');engine=AnalysisEngine()
    if live:writer.start_live()
    try:
        first=snapshot(writer);first['sessions']=[]
        for sid,parent in [('parent',None),('child','parent'),('grandchild','child'),('other',None)]:
            session=Session(sid,writer.homes[0],title=sid)
            session.add_usage(90.,sid+'-response',dict(input_tokens=100,cached_input_tokens=80,
                cache_write_input_tokens=0,output_tokens=20,reasoning_output_tokens=5),
                'gpt-6-astra',sid+'-turn','high',service_tier='Standard' if parent is None else '미확인')
            value=session.view(100.)
            value.update(usage_revision=1,parent_thread_id=parent,collection_complete=True)
            first['sessions'].append(value)
        writer.publish(first,'epoch',1)
        initial=reader.read(shared=True)
        assert reader.last_read_live==live
        ledger.enrich_modes(initial);engine.ingest(initial['sessions']);ledger.sync(engine,initial)
        revisions={key:session['revision'] for key,session in engine.sessions.items()}
        before={row['uid']:dict(row) for row in ledger.db.execute('select * from calls')}

        changed={**first,'ts':103.,'sessions':list(first['sessions'])}
        changed['sessions'][-1]=copy.deepcopy(first['sessions'][-1])
        changed['sessions'][-1]['history'][0]['output']+=1
        changed['sessions'][-1]['usage_revision']=2
        writer.publish(changed,'epoch',2)
        received=reader.read(shared=True)
        assert reader.last_read_live==live
        assert received==changed
        histories={s['id']:s['history'] for s in received['sessions']}
        ledger.enrich_modes(received);engine.ingest(received['sessions'])
        statements=[];ledger.db.set_trace_callback(statements.append)
        ledger.sync(engine,received);ledger.db.set_trace_callback(None)
        rescanned={key[1] for key,session in engine.sessions.items() if session['revision']!=revisions[key]}
        lookups=[sql for sql in statements if sql.startswith('select uid,signature,service_tier,sid,cost,price_id from calls where')]
        assert rescanned=={'other'}
        assert len(lookups)==1
        after={row['uid']:dict(row) for row in ledger.db.execute('select * from calls')}
        assert {key:value for key,value in after.items() if key!= 'other-response'}=={
            key:value for key,value in before.items() if key!='other-response'}

        # Publication revisions must detect corrections even without a changed usage_revision.
        corrected={**changed,'ts':106.,'sessions':list(changed['sessions'])}
        corrected['sessions'][0]=copy.deepcopy(changed['sessions'][0])
        corrected['sessions'][0]['history'][0]['service_tier']='Fast'
        writer.publish(corrected,'epoch',3)
        received=reader.read(shared=True)
        assert received==corrected
        assert received['sessions'][0]['history'] is not histories['parent']
        assert received['sessions'][-1]['history'] is histories['other']
    finally:ledger.close();reader.close();writer.close()


@pytest.mark.parametrize('change',['loading','complete','errors','correction'])
def test_live_substantive_transitions_are_durable(tmp_path,change):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite');writer.start_live()
    try:
        first=snapshot(writer);writer.publish(first,'epoch',1)
        changed=copy.deepcopy(first);changed['ts']=103.
        if change=='loading':changed['index']['loading']=True
        elif change=='complete':changed['index']['usage_complete']=False;changed['usage_collection_complete']=False
        elif change=='errors':changed['errors']=['source read failed']
        else:changed['sessions'][0]['history'][0]['output']=4
        writer.publish(changed,'epoch',2)
        writer.live_server.close();writer.live_server=None;writer.live=None
        assert writer.read()==changed
    finally:writer.close()


def test_failed_checkpoint_does_not_advance_live_snapshot(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite');writer.start_live()
    reader=CollectionChannel(writer.homes,writer.path)
    try:
        first=snapshot(writer);writer.publish(first,'epoch',1)
        writer.db.execute("CREATE TRIGGER interrupt_snapshot BEFORE INSERT ON snapshot BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        changed=copy.deepcopy(first);changed['ts']=103.;changed['sessions'][0]['history'][0]['output']=4
        with pytest.raises(sqlite3.IntegrityError,match='interrupted'):writer.publish(changed,'epoch',2)
        assert reader.read()==first and reader.last_read_live
        writer.db.execute('DROP TRIGGER interrupt_snapshot')
        writer.live_server.close();writer.live_server=None;writer.live=None
        assert reader.read()==first
    finally:reader.close();writer.close()


@pytest.mark.parametrize('later_epoch',['epoch','new-epoch'])
def test_recovery_prefers_latest_genuine_snapshot_without_freshening(tmp_path,later_epoch):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite');writer.start_live()
    client=CollectionClient(writer.homes,writer.path)
    try:
        first=snapshot(writer);writer.publish(first,'epoch',1)
        recent=copy.deepcopy(first);recent['ts']=110.;recent['last_usage_collection_success']=110.
        recent['index']['last_usage_success']=110.;writer.publish(recent,'epoch',2)
        assert client.poll(110.)==recent
        writer.live_server.close();writer.live_server=None;writer.live=None
        recovered=client.poll(111.)
        assert recovered['ts']==110. and recovered['collection_recovery']
        fresh_client=CollectionClient(writer.homes,writer.path)
        try:
            stored=fresh_client.poll(111.)
            assert stored['ts']==100. and stored['collection_recovery']
        finally:fresh_client.close()
        # A later checkpoint wins even when the system clock moved backwards.
        latest=copy.deepcopy(recent);latest['ts']=105.;latest['sessions'][0]['history'][0]['output']=5
        writer.publish(latest,later_epoch,3)
        assert client.poll(116.)['sessions'][0]['history'][0]['output']==5
    finally:client.close();writer.close()


def test_transport_authentication_and_stalled_peer_have_bounded_wait(tmp_path,monkeypatch):
    path=tmp_path/'endpoint.json'
    calls=[]
    server=collection_transport.LiveServer(path,lambda request:(calls.append(request) or {'ok':True},b''))
    try:
        actual=dict(server.endpoint)
        bad={**actual,'token':'00'*32}
        path.write_text(json.dumps(bad),encoding='utf-8')
        with collection_transport._endpoint_lock:collection_transport._endpoints[str(path)]=bad
        assert collection_transport.request(path,{'test':True}) is None
        assert calls==[]
    finally:server.close()
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
    accepted=threading.Event();release=threading.Event()
    def stall():
        with listener.accept()[0]:accepted.set();release.wait(2)
    thread=threading.Thread(target=stall,daemon=True);thread.start()
    path.write_text(json.dumps(dict(port=listener.getsockname()[1],token='11'*32)),encoding='utf-8')
    monkeypatch.setattr(collection_transport,'TIMEOUT',.15)
    start=time.monotonic()
    try:
        assert collection_transport.request(path,{'test':True}) is None
        assert accepted.is_set() and time.monotonic()-start<1.
    finally:release.set();listener.close();thread.join(2)


def test_live_lease_expiry_and_case_insensitive_path(tmp_path,monkeypatch):
    from cachemonitor.usage_collection import LiveCollection
    channel=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    try:
        live=LiveCollection(channel.path,channel.scope)
        monkeypatch.setattr(time,'time',lambda:100.)
        response,_=live.request(dict(scope=channel.scope,path=str(channel.path).swapcase(),client='one',homes=channel.homes))
        assert not response.get('error')
        assert live.requested_homes(159.)==channel.homes
        assert live.requested_homes(161.)==[]
    finally:channel.close()
