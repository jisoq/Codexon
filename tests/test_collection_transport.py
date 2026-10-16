"""Live collection preserves precision while checkpoints store substantive changes."""
import copy
import json
import os
import socket
import sqlite3
import threading
import time

import pytest

from cachemonitor import collection_transport
from cachemonitor.usage_collection import CollectionChanges, CollectionChannel, CollectionClient, empty_snapshot


def snapshot(channel, at=100.):
    value=empty_snapshot(channel.homes,channel.path,at)
    value.update(usage_collection_complete=True,last_usage_collection_success=at,request_activity=[],quota_by_home={})
    value['index'].update(loading=False,usage_complete=True,last_usage_success=at)
    value['sessions']=[dict(home=channel.homes[0],id='session',history=[dict(key='one',output=3)],
                            remaining=10.,collection_complete=True,status='idle')]
    return value


def wait_until(predicate,seconds=2):
    deadline=time.monotonic()+seconds
    while not predicate():
        assert time.monotonic()<deadline
        threading.Event().wait(.005)


@pytest.mark.parametrize('live',[False,True])
def test_activity_stream_is_bounded_replays_until_saved_and_recovers_old_history(tmp_path,live):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path)
    if live:writer.start_live()
    try:
        value=snapshot(writer)
        value.update(activity_stream=True,activity_source='source',activity_source_offset=1200,
            request_activity=[dict(home=writer.homes[0],attempt=str(n),seq=n,status='completed') for n in range(1200)])
        writer.publish(value,'epoch',1)
        first=reader.read(shared=True)
        assert len(first['request_activity'])==512 and first['activity_more']
        if live:
            metadata,_=writer.live.request(dict(path=str(writer.path),scope=writer.scope,protocol=3))
            assert not metadata['ready']  # A legacy reader uses the durable full snapshot.
        assert reader.read(shared=True)['request_activity']==first['request_activity']
        reader.acknowledge_activity(first);second=reader.read(shared=True)
        assert len(second['request_activity'])==512
        reader.acknowledge_activity(second);third=reader.read(shared=True)
        assert len(third['request_activity'])==176 and not third['activity_more']
        reader.acknowledge_activity(third)
        assert reader.read(shared=True)['request_activity']==[]
        updated={**value,'activity_source_offset':1201,'request_activity':[
            dict(home=writer.homes[0],attempt='0',seq=1201,status='failed')]}
        writer.publish(updated,'epoch',2)
        assert reader.read(shared=True)['request_activity']==updated['request_activity']
        assert not writer.published_activity and not reader.activity_cache and not reader.live_activity_cache
        # A later consumer can recover all attempts, including old evicted rows.
        reader.close();reader=CollectionChannel(writer.homes,writer.path)
        seen=[]
        for _ in range(3):
            page=reader.read(shared=True);seen.extend(page['request_activity']);reader.acknowledge_activity(page)
        assert len(seen)==1200 and next(row for row in seen if row['attempt']=='0')['status']=='failed'
        cursor=reader.activity_ack
        reader.close();reader=CollectionChannel(writer.homes,writer.path);reader.activity_ack=cursor
        writer.publish(updated,'replacement-owner',1)
        assert reader.read(shared=True)['request_activity']==[]
    finally:reader.close();writer.close()


def test_notifications_coalesce_publish_reclaim_epoch_and_stop(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionClient(writer.homes,writer.path)
    writer.start_live()
    try:
        value=snapshot(writer);writer.publish(value,'epoch',1)
        reader.poll();wake=reader.notification_connection();first=reader.waiter
        wait_until(lambda:bool(writer.live.waiters))
        # Loading/other reads cannot create a second wait or pending message.
        for _ in range(20):reader.notification_connection()
        writer.publish(value,'epoch',2)
        assert wake.poll(1)
        for sequence in range(3,22):writer.publish(value,'epoch',sequence)
        # A timer may have read the publication before its notification arrives.
        reader.poll()
        assert wake.poll(1) and reader.consume_notification() is False
        reader.notification_connection()
        writer.publish(value,'epoch',22)
        assert wake.poll(1) and reader.consume_notification()
        assert not wake.poll()
        reader.poll();assert reader.channel.live_publication==('epoch',22)
        reader.notification_connection();assert reader.waiter is first
        wait_until(lambda:bool(writer.live.waiters))
        writer.publish(value,'replacement',1)
        assert wake.poll(1) and reader.consume_notification()
        reader.poll();new=reader.notification_connection()
        assert reader.waiter is not first and not first.thread.is_alive()
        wait_until(lambda:bool(writer.live.waiters))
        latest=reader.waiter
        reader.stop_notifications()
        wait_until(lambda:not writer.live.waiters)
        assert not latest.thread.is_alive() and reader.waiter is None
        assert new.closed
    finally:reader.close();writer.close()


def test_notification_failure_releases_socket_and_allows_polling_recovery(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionClient(writer.homes,writer.path)
    writer.start_live()
    try:
        value=snapshot(writer);writer.publish(value,'epoch',1)
        reader.poll();wake=reader.notification_connection();worker=reader.waiter
        wait_until(lambda:bool(writer.live.waiters))
        writer.close()
        assert wake.poll(2) and not reader.consume_notification()
        assert reader.poll()['sessions']==value['sessions']
        assert reader.notification_connection() is None
        reader.close();assert not worker.thread.is_alive()
    finally:reader.close();writer.close()


def test_analyzer_publication_wakes_before_timer_and_freeze_reclaims_waiter(tmp_path):
    import multiprocessing as mp
    from cachemonitor.analysis_worker import process_main
    from test_overlay import source
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    writer.start_live()
    context=mp.get_context('spawn');parent,child=context.Pipe()
    process=context.Process(target=process_main,args=(child,writer.homes,writer.path),
        kwargs={'quota_path':tmp_path/'quota.sqlite'})
    try:
        value=snapshot(writer,time.time());session=source();session['home']=writer.homes[0]
        value['sessions']=[session];writer.publish(value,'epoch',1)
        process.start();child.close()
        assert parent.poll(15)
        first=parent.recv();assert first['kind']=='snapshot'
        if not first['value'].get('ledger_status_known'):
            assert parent.poll(15)
            first=parent.recv()
            assert first['kind']=='snapshot' and first['value']['ledger_status_known']
        wait_until(lambda:bool(writer.live.waiters))
        changed=copy.deepcopy(value);changed['ts']+=.25
        started=time.perf_counter();writer.publish(changed,'epoch',2)
        assert parent.poll(1)
        second=parent.recv();assert second['kind']=='snapshot'
        assert second['value']['ts']==changed['ts']
        assert time.perf_counter()-started<1
        wait_until(lambda:bool(writer.live.waiters))
        parent.send({'kind':'freeze'})
        wait_until(lambda:not writer.live.waiters)
        parent.send({'kind':'stop'});process.join(3)
        assert not process.is_alive() and process.exitcode==0
    finally:
        if process.pid and process.is_alive():
            parent.send({'kind':'stop'});process.join(3)
            if process.is_alive():process.terminate();process.join()
        parent.close();child.close();writer.close()


def test_quiet_notifications_preserve_staleness_and_ledger_retry_deadlines(monkeypatch,tmp_path):
    from types import SimpleNamespace
    from cachemonitor import analysis_worker
    clock=[100.];reads=[];opens=[];sent=[]
    monkeypatch.setattr(analysis_worker,'time',SimpleNamespace(monotonic=lambda:clock[0],time=lambda:clock[0]))
    notification=object()
    def wait(connections,timeout):clock[0]+=timeout;return []
    monkeypatch.setattr(analysis_worker,'wait',wait)
    class Collector:
        path=tmp_path/'index.sqlite'
        def __init__(self,*args):pass
        def poll(self):
            reads.append(clock[0])
            return dict(ts=100.,sessions=[],homes=[],errors=[],unassigned=[],index={'loading':False})
        def notification_connection(self):return notification
        def close(self):pass
    class Ledger:
        def __init__(self,*args):
            opens.append(clock[0])
            if len(opens)==1:raise sqlite3.OperationalError('locked')
        def enrich_modes(self,*args):pass
        def sync(self,*args):pass
        def import_observations(self,*args):pass
        def close(self):pass
    class Connection:
        def poll(self,*args):return len(reads)>=5
        def recv(self):return {'kind':'stop'}
        def send(self,value):sent.append(value)
        def close(self):pass
    monkeypatch.setattr(analysis_worker,'CollectionClient',Collector)
    monkeypatch.setattr(analysis_worker,'QuotaLedger',Ledger)
    analysis_worker.process_main(Connection(),[],tmp_path/'index.sqlite')
    assert opens==[100.,105.]
    assert 130.<reads[-1]<130.1
    assert len(sent)==5 and all(value['kind']=='snapshot' for value in sent)


def test_new_session_loading_does_not_reprocess_existing_call_history(tmp_path):
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.analysis_delivery import SnapshotPublisher, SnapshotReceiver
    from cachemonitor.core import Session
    from cachemonitor.overlay_data import OverlaySummaries
    from cachemonitor.quota_cycles import QuotaLedger
    ledger=QuotaLedger(tmp_path/'quota.sqlite')
    engine=AnalysisEngine();overlays=OverlaySummaries();publisher=SnapshotPublisher();receiver=SnapshotReceiver()
    def session(sid,count):
        source=Session(sid,'home',title=sid)
        for i in range(count):
            source.add_usage(90.+i/10,sid+str(i),dict(input_tokens=100,cached_input_tokens=80,
                cache_write_input_tokens=0,output_tokens=20,reasoning_output_tokens=5),
                'gpt-6-astra',sid+'-turn','high',service_tier='Standard')
        return dict(source.view(200.),usage_revision=1,collection_complete=True,
            turn_records={sid+'-turn':dict(started_at=89.,ended_at=110.,state='완료')})
    value=dict(ts=200.,sessions=[session('old',128)],homes=['home'],errors=[],index=dict(loading=False))
    def deliver():
        engine.ingest(value['sessions']);ledger.sync(engine,value)
        return receiver.receive(publisher.publish(value,engine,overlays.collect(engine)))
    try:
        first=deliver();before=ledger.db.total_changes
        query=dict(page=0,start=0,end=300,sid='old')
        assert engine.query(query)['analysis']['turns'][0]['complete']
        value.update(ts=201.,index=dict(loading=True))
        value['sessions']=[dict(s,collection_complete=False) for s in value['sessions']]+[session('new',1)]
        next_value=deliver()
        assert ledger.db.total_changes-before<16
        assert overlays.speed_health.metrics['scanned_rows']==1
        old_overlay=next(s for s in next_value['overlay_sessions'] if s['id']=='old')
        assert old_overlay==dict(first['overlay_sessions'][0],collection_complete=False)
        assert next(s for s in next_value['overlay_sessions'] if s['id']=='new')['calls']==1
        assert engine.sessions[('home','old')]['prepared']['collection_complete'] is False
        assert not engine.query(query)['analysis']['turns'][0]['complete']
        before=ledger.db.total_changes
        value.update(ts=202.,index=dict(loading=False))
        value['sessions']=[dict(s,collection_complete=True) for s in value['sessions']]
        recovered=deliver()
        assert ledger.db.total_changes-before<16
        assert overlays.speed_health.metrics['scanned_rows']==0
        assert all(s['collection_complete'] for s in recovered['sessions'])
        assert all(s['collection_complete'] for s in recovered['overlay_sessions'])
        assert engine.query(query)['analysis']['turns'][0]['complete']
        corrected=copy.deepcopy(value['sessions'][-1]);corrected['usage_revision']=2
        corrected['history'][0]['output']=30;value['sessions'][-1]=corrected
        deliver()
        assert ledger.db.execute('select output from calls where uid=?',('new0',)).fetchone()[0]==30
    finally:ledger.close()


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


def test_trusted_collection_tokens_validate_only_changed_rows_and_preserve_order(tmp_path,monkeypatch):
    from cachemonitor import usage_collection
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);writer.start_live()
    owner=object();home=writer.homes[0]
    first=snapshot(writer)
    first['sessions'][0]['groups']={'today':[dict(tokens=3)]}
    first['sessions'].append(dict(home=home,id='retained',history=[dict(key=str(i),output=i) for i in range(300)]))
    first['request_activity']=[dict(home=home,attempt=str(i),status='started') for i in range(100)]
    session_tokens={(home,s['id']):1 for s in first['sessions']}
    activity_tokens={(home,r['attempt']):1 for r in first['request_activity']}
    def tokens():return CollectionChanges(owner,dict(session_tokens),dict(activity_tokens))
    checked=[];fingerprint=usage_collection.publication_fingerprint
    def count(value):
        if isinstance(value,dict) and ('history' in value or 'attempt' in value):checked.append(value)
        return fingerprint(value)
    monkeypatch.setattr(usage_collection,'publication_fingerprint',count)
    try:
        writer.publish(first,'epoch',1,changes=tokens());initial=reader.read(shared=True)
        first['sessions'][0]['groups']['today'][0]['tokens']=4
        assert reader.read()['sessions'][0]['groups']['today'][0]['tokens']==3
        checked.clear()
        idle={**first,'ts':103.}
        writer.publish(idle,'epoch',2,changes=tokens())
        assert not checked and reader.read()==idle
        changed={**idle,'ts':106.,'sessions':list(idle['sessions']),
                 'request_activity':list(reversed(idle['request_activity']))}
        changed['sessions'][0]=copy.deepcopy(changed['sessions'][0])
        changed['sessions'][0]['history'][0]['output']=4
        changed['request_activity'][0]={**changed['request_activity'][0],'status':'completed'}
        session_tokens[(home,'session')]=2;activity_tokens[(home,'99')]=2
        writer.publish(changed,'epoch',3,changes=tokens());received=reader.read(shared=True)
        assert received==changed
        assert len(checked)==2
        assert received['sessions'][1]['history'] is initial['sessions'][1]['history']
        # Retire rows while a consumer misses an intermediate publication.
        removed={**changed,'ts':109.,'sessions':changed['sessions'][:1],
                 'request_activity':changed['request_activity'][:2]}
        writer.publish(removed,'epoch',4,changes=tokens())
        latest={**removed,'ts':112.,'request_activity':list(reversed(removed['request_activity']))}
        writer.publish(latest,'epoch',5,changes=tokens());assert reader.read()==latest
        assert len(writer.live.current['session_tokens'])==1
        assert len(writer.live.current['activity_tokens'])==2
        assert not any(key.startswith('change_') or key.endswith('_tokens') for key in received)
    finally:reader.close();writer.close()


@pytest.mark.parametrize('reset',['owner','epoch'])
def test_trusted_collection_token_reuse_requires_same_owner_and_epoch(tmp_path,reset):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);writer.start_live()
    owner=object();home=writer.homes[0]
    try:
        first=snapshot(writer)
        first['request_activity']=[dict(home=home,attempt='active',status='pending')]
        changes=CollectionChanges(owner,{(home,'session'):1},{(home,'active'):1},activity_revision=1)
        writer.publish(first,'epoch',1,changes=changes);assert reader.read()==first
        changed=copy.deepcopy(first);changed['ts']=103.;changed['sessions'][0]['history'][0]['output']=4
        changed['request_activity'][0]['status']='completed'
        next_owner=object() if reset=='owner' else owner
        epoch='replacement' if reset=='epoch' else 'epoch'
        writer.publish(changed,epoch,2,changes=CollectionChanges(
            next_owner,{(home,'session'):1},{(home,'active'):1},activity_revision=1))
        assert reader.read()==changed
    finally:reader.close();writer.close()


def test_unchanged_trusted_activity_revision_reuses_history_without_visiting_records(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);writer.start_live()
    owner=object();home=writer.homes[0]
    class UnchangedRecords(list):
        def __iter__(self):raise AssertionError('Unchanged activity records were visited')
    try:
        first=snapshot(writer)
        first['request_activity']=[dict(home=home,attempt=key,status='pending') for key in ('old','active')]
        changes=CollectionChanges(owner,{(home,'session'):1},
                                  {(home,key):1 for key in ('old','active')},activity_revision=(0,1))
        writer.publish(first,'epoch',1,changes=changes);assert reader.read()==first
        idle={**first,'ts':103.,'request_activity':UnchangedRecords(first['request_activity'])}
        writer.publish(idle,'epoch',2,changes=changes)
        expected={**first,'ts':103.};assert reader.read()==expected
        latest={**first,'ts':106.,'request_activity':[dict(home=home,attempt='active',status='completed')]}
        updated=CollectionChanges(owner,{(home,'session'):1},{(home,'active'):2},activity_revision=(0,2))
        writer.publish(latest,'epoch',3,changes=updated)
        assert reader.read()==latest
        # Trusted revisions remain private, while durable recovery retains the result.
        writer.live_server.close();writer.live_server=None;writer.live=None
        recovered=CollectionChannel(writer.homes,writer.path)
        try:assert recovered.read()==latest
        finally:recovered.close()
    finally:reader.close();writer.close()


def test_untrusted_in_place_correction_with_same_revision_is_not_hidden_by_tokens(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);writer.start_live()
    home=writer.homes[0]
    try:
        first=snapshot(writer);first['sessions'][0]['usage_revision']=1
        writer.publish(first,'epoch',1,changes=CollectionChanges(object(),{(home,'session'):1},{}))
        original=reader.read(shared=True)
        first['sessions'][0]['history'][0]['output']=9
        writer.publish(first,'epoch',2)
        assert reader.read()==first
        assert original['sessions'][0]['history'][0]['output']==3
    finally:reader.close();writer.close()


def test_failed_trusted_checkpoint_retries_changed_row_without_advancing_tokens(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);writer.start_live()
    owner=object();home=writer.homes[0]
    try:
        first=snapshot(writer)
        writer.publish(first,'epoch',1,changes=CollectionChanges(owner,{(home,'session'):1},{}))
        assert reader.read()==first
        changed=copy.deepcopy(first);changed['ts']=103.;changed['sessions'][0]['history'][0]['output']=4
        changes=CollectionChanges(owner,{(home,'session'):2},{})
        writer.db.execute("CREATE TRIGGER interrupt_snapshot BEFORE INSERT ON snapshot BEGIN SELECT RAISE(ABORT,'interrupted'); END")
        with pytest.raises(sqlite3.IntegrityError,match='interrupted'):
            writer.publish(changed,'epoch',2,changes=changes)
        assert reader.read()==first
        writer.db.execute('DROP TRIGGER interrupt_snapshot')
        writer.publish(changed,'epoch',3,changes=changes)
        assert reader.read()==changed
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


def test_live_manifest_transmits_only_changed_sessions_and_preserves_order(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);writer.start_live()
    try:
        value=snapshot(writer)
        value['sessions'].extend(dict(home=writer.homes[0],id=str(n),history=[dict(key=str(n))]) for n in range(100))
        writer.publish(value,'epoch',1)
        first=reader.read(shared=True)
        cursor=reader.live_publication
        value={**value,'ts':101.};writer.publish(value,'epoch',2)
        message,body=reader.live_request(protocol=4,publication=cursor)
        assert message['sessions']==[] and not message['full'] and body==b''
        assert len(json.dumps(message).encode())<2048
        assert reader.read()==value
        cursor=reader.live_publication
        changed=copy.deepcopy(value)
        changed['sessions'][0]['history'][0]['output']=9
        writer.publish(changed,'epoch',3)
        changed['sessions'][1]['collection_complete']=False
        writer.publish(changed,'epoch',4)
        message,body=reader.live_request(protocol=4,publication=cursor)
        assert not message['full'] and [entry['sid'] for entry in message['sessions']]==['session','0']
        assert message['sessions'][0]['size']>0 and message['sessions'][1]['size']==0
        delivered=reader.read(shared=True)
        assert delivered==changed
        assert delivered['sessions'][2] is first['sessions'][2]
        cursor=reader.live_publication
        changed['sessions'].reverse();writer.publish(changed,'epoch',5)
        message,body=reader.live_request(protocol=4,publication=cursor)
        assert message['full'] and body==b''
        assert reader.read()==changed
        # The previous protocol remains readable during an upgrade handoff.
        message,body=reader.live_request(protocol=3,publication=reader.live_publication)
        assert len(message['sessions'])==len(changed['sessions']) and body==b''
        current_handler=writer.live_server.handler
        writer.live_server.handler=lambda request:(dict(ready=False),b'') if request.get('protocol') in (4,5) else current_handler(request)
        assert reader.read()==changed and reader.last_read_live
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
        lookups=[sql for sql in statements if sql.lstrip().startswith('select ') and 'from calls ' in sql and 'sid=' in sql]
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
def test_recovery_prefers_latest_genuine_snapshot_without_freshening(tmp_path,monkeypatch,later_epoch):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite');writer.start_live()
    client=CollectionClient(writer.homes,writer.path)
    try:
        first=snapshot(writer);writer.publish(first,'epoch',1)
        recent=copy.deepcopy(first);recent['ts']=110.;recent['last_usage_collection_success']=110.
        recent['index']['last_usage_success']=110.;writer.publish(recent,'epoch',2)
        assert client.poll(110.)==recent
        writer.live_server.close();writer.live_server=None;writer.live=None
        with monkeypatch.context() as patch:
            patch.setattr('cachemonitor.usage_collection.zlib.decompress',
                          lambda *a,**k:pytest.fail('older recovery snapshot decoded'))
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


def test_durable_catalog_selects_only_requested_bodies_without_advancing_full_reader(tmp_path,monkeypatch):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path)
    try:
        value=snapshot(writer);home=writer.homes[0]
        value['sessions'].append(dict(home=home,id='unselected',history=[dict(key='two',output=7)]))
        writer.publish(value,'epoch',1)
        key=(home,'session');other=(home,'unselected')
        payload=writer.db.execute('SELECT payload FROM sessions WHERE home=? AND sid=?',other).fetchone()[0]
        with writer.db:writer.db.execute('UPDATE sessions SET payload=? WHERE home=? AND sid=?',(b'invalid',*other))
        decoded=[];decode=reader._decode_body
        def tracked(owner,*args):
            decoded.append(owner)
            return decode(owner,*args)
        monkeypatch.setattr(reader,'_decode_body',tracked)
        catalog=reader.read_catalog()
        assert not decoded and set(catalog['owners'])=={key,other}
        assert 'sessions' not in catalog['header'] and 'session_manifest' not in catalog['header']
        selected=reader.read_owners([key],catalog=catalog)
        assert selected['catalog']['publication']==('epoch',1)
        assert selected['sessions']==value['sessions'][:1] and decoded==[key]
        assert reader.identity is None and reader.cached is None and not reader.session_cache
        # A selective read must not claim to own all checkpoint bodies on the
        # next full read, and full reads continue sharing unchanged evidence.
        with writer.db:writer.db.execute('UPDATE sessions SET payload=? WHERE home=? AND sid=?',(payload,*other))
        decoded.clear();full=reader.read(shared=True)
        assert full==value and decoded==[key,other]
        decoded.clear();again=reader.read(shared=True)
        assert not decoded and again['sessions'][0]['history'] is full['sessions'][0]['history']
    finally:reader.close();writer.close()


def test_durable_owner_read_pins_catalog_and_body_to_one_transaction(tmp_path,monkeypatch):
    from cachemonitor.usage_collection import CollectionPending
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path)
    try:
        value=snapshot(writer);writer.publish(value,'epoch',1)
        key=(writer.homes[0],'session');catalog=reader.read_catalog()
        changed=copy.deepcopy(value);changed['sessions'][0]['history'][0]['output']=9
        load=reader._durable_sessions
        def publish_during_read(db,entries,previous):
            writer.publish(changed,'epoch',2)
            yield from load(db,entries,previous)
        with monkeypatch.context() as patch:
            patch.setattr(reader,'_durable_sessions',publish_during_read)
            selected=reader.read_owners([key],catalog=catalog)
        assert selected['sessions']==value['sessions']
        assert selected['catalog']['publication']==('epoch',1)
        with monkeypatch.context() as patch:
            patch.setattr(reader,'_decode_body',lambda *a,**kw:pytest.fail('stale catalog decoded a body'))
            with pytest.raises(CollectionPending):reader.read_owners([key],catalog=catalog)
        selected=reader.read_owners([key])
        assert selected['catalog']['publication']==('epoch',2) and selected['sessions']==changed['sessions']
    finally:reader.close();writer.close()


@pytest.mark.parametrize('legacy',[False,True])
def test_recovery_and_live_delivery_release_superseded_history(tmp_path,monkeypatch,legacy):
    import zlib
    from cachemonitor import usage_collection
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path)
    try:
        value=snapshot(writer)
        value['sessions'][0].update(usage_revision='epoch:1',request_revision='epoch:1')
        writer.publish(value,'epoch',1)
        if legacy:
            header=json.loads(zlib.decompress(writer.db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0]))
            entry=header['session_manifest'][0];entry.pop('body_digest')
            payload=writer.db.execute('SELECT payload FROM sessions').fetchone()[0]
            heavy=json.loads(zlib.decompress(payload))
            for field in ('usage_revision','request_revision'):heavy[field]=entry['state'].pop(field)
            with writer.db:
                writer.db.execute('UPDATE sessions SET payload=?',(zlib.compress(json.dumps(heavy).encode()),))
                writer.db.execute('UPDATE snapshot SET payload=?',(zlib.compress(json.dumps(header).encode()),))
        catalog=reader.read_catalog()
        selected=reader.read_owners([(writer.homes[0],'session')],catalog=catalog)
        assert selected['sessions']==value['sessions']
        assert reader.identity is None and not reader.session_cache
        durable=reader.read(shared=True)
        assert durable['sessions']==value['sessions']
        assert reader.session_cache and not reader.live_session_cache
        writer.start_live();writer.publish(value,'epoch',2)
        metadata,body=reader.live_request(protocol=5,checkpoint=reader.identity)
        assert metadata['protocol']==5
        if not legacy:assert metadata['sessions'][0]['size']==0 and body==b''
        with monkeypatch.context() as patch:
            patch.setattr(usage_collection.zlib,'decompress',lambda *a,**kw:pytest.fail('unchanged session decoded'))
            live=reader.read(shared=True)
        assert live==durable and reader.last_read_live
        assert live['sessions'][0]['history'] is durable['sessions'][0]['history']
        assert not reader.session_cache and reader.cached is None
        assert reader.live_session_cache
        writer.live_server.close();writer.live_server=None;writer.live=None
        changed=copy.deepcopy(value);changed['sessions'][0]['history'][0]['output']=9
        writer.publish(changed,'replacement',1)
        assert reader.read(shared=True)==changed
        assert reader.session_cache and not reader.live_session_cache and reader.live_cached is None
        writer.start_live();writer.publish(changed,'replacement',2)
        assert reader.read(shared=True)==changed and reader.last_read_live
        assert not reader.session_cache
        # Existing users of a snapshot retain its immutable evidence after a
        # transport handoff, and closing releases only the channel's ownership.
        reader.close()
        assert not reader.live_session_cache and reader.live_cached is None
        assert live['sessions'][0]['history'][0]['output']==3
    finally:reader.close();writer.close()


def test_new_epoch_reuses_body_but_rejects_inconsistent_content_digest(tmp_path,monkeypatch):
    from cachemonitor import usage_collection
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite');writer.start_live()
    reader=CollectionChannel(writer.homes,writer.path)
    try:
        value=snapshot(writer);value['sessions'][0].update(usage_revision='first:1',request_revision='first:1')
        value['sessions'][0]['turn_records']={'z':{'started_at':2},'a':{'started_at':1}}
        writer.publish(value,'first',1)
        with monkeypatch.context() as patch:
            patch.setattr(reader,'live_request',lambda **kw:None)
            initial=reader.read(shared=True)
        assert not reader.last_read_live
        assert reader.read(shared=True)==initial and reader.last_read_live
        state_only=copy.deepcopy(value);state_only['sessions'][0]['usage_revision']='first:2'
        writer.publish(state_only,'first',2)
        metadata,body=reader.live_request(protocol=4,publication=reader.live_publication)
        assert metadata['sessions'][0]['size']==0 and body==b''
        assert reader.read(shared=True)==state_only
        changed=copy.deepcopy(value);changed['sessions'][0].update(usage_revision='second:1',request_revision='second:1')
        writer.publish(changed,'second',1)
        with monkeypatch.context() as patch:
            patch.setattr(usage_collection.zlib,'decompress',lambda *a,**kw:pytest.fail('unchanged session decoded'))
            received=reader.read(shared=True)
        assert received==changed and received['sessions'][0]['history'] is initial['sessions'][0]['history']
        assert list(received['sessions'][0]['turn_records'])==list(value['sessions'][0]['turn_records'])
        metadata,body=reader.live_request(protocol=4)
        metadata['sessions'][0]['body_digest']='0'*64
        with monkeypatch.context() as patch:
            patch.setattr(reader,'live_request',lambda **kw:(metadata,body))
            with pytest.raises(usage_collection.CollectionScopeError):reader.read(shared=True)
        assert reader.live_cached['sessions'][0]['history'] is initial['sessions'][0]['history']
    finally:reader.close();writer.close()


def test_restart_reuses_committed_bodies_until_catalog_finishes_and_repairs_corruption(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path)
    try:
        value=snapshot(writer);value['sessions'][0].update(usage_revision='first:1',request_revision='first:1')
        value['sessions'].append({**value['sessions'][0],'id':'second'})
        writer.publish(value,'first',17)
        saved=list(writer.db.execute('SELECT home,sid,revision,payload FROM sessions ORDER BY sid'))
        writer.close();writer=CollectionChannel(reader.homes,reader.path)
        partial={**value,'index':{**value['index'],'loading':True},
            'sessions':[dict(value['sessions'][0],usage_revision='second:1',request_revision='second:1')]}
        writer.publish(partial,'second',1)
        assert reader.read(shared=True)==partial
        assert list(writer.db.execute('SELECT home,sid,revision,payload FROM sessions ORDER BY sid'))==saved
        full={**value,'sessions':[dict(s,usage_revision='second:1',request_revision='second:1') for s in value['sessions']]}
        writer.publish(full,'second',2)
        assert reader.read(shared=True)==full
        assert list(writer.db.execute('SELECT home,sid,revision,payload FROM sessions ORDER BY sid'))==saved
        # Reused checkpoint revisions can exceed the restarted sequence. Real
        # corrections must stay visible when that sequence catches up.
        corrected=copy.deepcopy(full)
        for sequence,output in ((17,4),(18,5)):
            corrected['sessions'][0]['history'][0]['output']=output
            writer.publish(corrected,'second',sequence)
            assert reader.read(shared=True)==corrected
        # A restarted writer checks bytes rather than trusting a digest alone.
        writer.db.execute("UPDATE sessions SET payload=? WHERE sid='session'",(b'corrupted',));writer.db.commit()
        writer.close();writer=CollectionChannel(reader.homes,reader.path)
        writer.publish(full,'third',1)
        assert reader.read(shared=True)==full
        writer.publish({**full,'sessions':full['sessions'][:1]},'third',2)
        assert [row[0] for row in writer.db.execute('SELECT sid FROM sessions')]==['session']
        writer.db.execute('UPDATE snapshot SET payload=?',(b'corrupted',));writer.db.commit()
        writer.start_live()
        assert writer.live.recovery is None
        writer.publish(full,'fourth',1)
        assert reader.read(shared=True)==full
    finally:reader.close();writer.close()


@pytest.mark.parametrize('proof_filter',[True,False])
def test_partial_restart_keeps_one_body_population_until_confirmed_or_removed(tmp_path,monkeypatch,proof_filter):
    from cachemonitor import usage_collection
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite')
    reader=CollectionChannel(writer.homes,writer.path);reader.retain_pending=True
    try:
        value=snapshot(writer)
        value['sessions'][0].update(usage_revision='first:1',request_revision='first:1')
        value['sessions'].append(dict(value['sessions'][0],id='second'))
        writer.publish(value,'first',7)
        initial=reader.read(shared=True)
        histories={s['id']:s['history'] for s in initial['sessions']}
        writer.close();writer=CollectionChannel(reader.homes,reader.path);writer.start_live()
        if not proof_filter:
            handler=writer.live_server.handler
            def older_collector(request):
                metadata,body=handler(request)
                metadata.pop('checkpoint_filter',None)
                return metadata,body
            writer.live_server.handler=older_collector
        partial={**value,'index':{**value['index'],'loading':True},'sessions':value['sessions'][:1]}
        writer.publish(partial,'second',1)
        with monkeypatch.context() as patch:
            patch.setattr(usage_collection.zlib,'decompress',lambda *a,**kw:pytest.fail('unchanged body decoded'))
            received=reader.read(shared=True)
        assert len(received['sessions'])==2
        pending=next(s for s in received['sessions'] if s['id']=='second')
        assert pending['_collection_pending'] and pending['collection_complete'] is False
        assert pending['history'] is histories['second']
        assert not reader.session_cache and reader.pending_checkpoint==('first',7)
        # A correction can later return to checkpoint bytes. Its old proof
        # must be withdrawn after the first replacement, even while B is pending.
        corrected=copy.deepcopy(partial);corrected['sessions'][0]['history'][0]['output']=7
        writer.publish(corrected,'second',2)
        assert reader.read(shared=True)['sessions'][0]['history'][0]['output']==7
        assert bool(reader.pending_checkpoint)==proof_filter
        writer.publish(partial,'second',3)
        reverted=reader.read(shared=True)
        assert reverted['sessions'][0]['history'][0]['output']==3
        histories['session']=reverted['sessions'][0]['history']
        # A later partial publication can promote an old body without sending it.
        writer.publish({**partial,'sessions':value['sessions']},'second',4)
        metadata,body=reader.live_request(protocol=5,publication=reader.live_publication,checkpoint=reader.pending_checkpoint,
            checkpoint_exclusions=list(reader.checkpoint_exclusions))
        if proof_filter:assert not body and all(entry['size']==0 for entry in metadata['sessions'])
        else:assert body
        with monkeypatch.context() as patch:
            patch.setattr(usage_collection.zlib,'decompress',lambda *a,**kw:pytest.fail('promoted body decoded'))
            promoted=reader.read(shared=True)
        assert all(not s.get('_collection_pending') and s['history'] is histories[s['id']] for s in promoted['sessions'])
        # Preserve pending bodies across a newer durable fallback, too.
        writer.publish(partial,'second',5)
        reader.read(shared=True)
        writer.publish({**partial,'index':value['index']},'second',6)
        metadata,body=reader.live_request(protocol=5,publication=reader.live_publication)
        assert not metadata['full'] and not metadata['sessions']
        with pytest.raises(usage_collection.CollectionScopeError):reader._read_live(metadata,body+b'x')
        assert (writer.homes[0],'second') in reader.live_session_cache
        writer.publish(partial,'second',7)
        writer.live_server.close();writer.live_server=None;writer.live=None
        writer.publish(partial,'third',1)
        recovered=reader.read(shared=True)
        assert len(recovered['sessions'])==2 and recovered['sessions'][1]['history'] is histories['second']
        assert not reader.live_session_cache
        writer.start_live();writer.publish({**partial,'index':value['index']},'third',2)
        final=reader.read(shared=True)
        assert [s['id'] for s in final['sessions']]==['session']
        assert not reader.pending_checkpoint and len(reader.live_session_cache)==1
    finally:reader.close();writer.close()


def test_stale_client_read_preserves_source_errors_and_shared_freshness(tmp_path):
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'index.sqlite');writer.start_live()
    client=CollectionClient(writer.homes,writer.path)
    try:
        first=snapshot(writer);writer.publish(first,'epoch',1)
        stale=client.poll(131.)
        assert stale['collection_stale'] and not stale['usage_collection_complete']
        assert not stale['errors'] and not stale['usage_errors']
        assert client.channel.live_cached['usage_collection_complete']
        assert not client.channel.live_cached.get('collection_stale')
        failed={**first,'ts':140.,'errors':['source read failed'],'usage_errors':['source read failed']}
        writer.publish(failed,'epoch',2)
        stale=client.poll(171.)
        assert stale['collection_stale'] and stale['errors']==failed['errors']
        latest={**first,'ts':172.};writer.publish(latest,'epoch',3)
        recovered=client.poll(172.)
        assert not recovered.get('collection_stale') and not recovered['errors']
        assert recovered['usage_collection_complete']
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


def test_response_signature_preserves_protocol_without_copying_large_body():
    import hmac
    import tracemalloc
    token=b't'*32;nonce=b'n'*32;metadata=b'{"ready":true}';body=b'x'*(8*1024*1024)
    expected=hmac.digest(token,nonce+metadata+body,'sha256')
    tracemalloc.start()
    try:
        actual=collection_transport._signature(token,nonce,metadata,body)
        _,peak=tracemalloc.get_traced_memory()
    finally:tracemalloc.stop()
    assert actual==expected
    assert peak<1024*1024


def test_live_server_close_releases_stalled_handshake_and_worker(tmp_path):
    server=collection_transport.LiveServer(tmp_path/'endpoint.json',lambda request:({},b''))
    try:
        with collection_transport._connect(server.endpoint,time.monotonic()+3) as client:
            client.settimeout(3)
            assert len(collection_transport._receive(client,32,time.monotonic()+3))==32
            with server.connection_lock:workers=tuple(server.handlers)
            assert workers
            server.close()
            assert client.recv(1)==b''
            assert all(not worker.is_alive() for worker in workers)
            assert not server.connections and not server.handlers
    finally:server.close()


def test_live_server_close_keeps_blocked_callbacks_owned_until_completion(tmp_path,monkeypatch):
    import hmac
    import struct
    monkeypatch.setattr(collection_transport,'TIMEOUT',.2)
    entered=threading.Event();release=threading.Event();lock=threading.Lock();count=[0]
    def callback(request):
        with lock:
            count[0]+=1
            if count[0]==2:entered.set()
        assert release.wait(3)
        return {},b''
    server=collection_transport.LiveServer(tmp_path/'endpoint.json',callback)
    clients=[];workers=()
    try:
        for _ in range(2):
            client=collection_transport._connect(server.endpoint,time.monotonic()+3)
            client.settimeout(3)
            clients.append(client)
            nonce=collection_transport._receive(client,32,time.monotonic()+3)
            encoded=b'{}'
            collection_transport._send(client,
                hmac.digest(server.token,nonce+encoded,'sha256')+struct.pack('!Q',len(encoded))+encoded,
                time.monotonic()+3)
        assert entered.wait(3)
        with server.connection_lock:workers=tuple(server.handlers)
        start=time.monotonic();server.close()
        assert time.monotonic()-start<1.
        assert len(server.handlers)==len(workers)==2
        assert all(client.recv(1)==b'' for client in clients)
    finally:
        release.set()
        for worker in workers:worker.join(3)
        for client in clients:client.close()
        server.close()
    assert not server.connections and not server.handlers


@pytest.mark.skipif(os.name!='nt',reason='Windows collection uses private named pipes')
def test_pipe_requests_and_watch_cancellation_do_not_use_tcp_or_retain_handles(tmp_path,monkeypatch):
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    def handles():
        count=wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),ctypes.byref(count))
        return count.value
    def no_tcp(*args,**kwargs):raise AssertionError('A collection pipe must not allocate a TCP socket')
    monkeypatch.setattr(socket,'socket',no_tcp)
    path=tmp_path/'pipe.json';entered=threading.Event();release=threading.Event()
    body=b'x'*(2*1024*1024+17)
    def callback(value):
        if value.get('watch'):
            entered.set()
            assert release.wait(2)
        return {'ok':True},body if value.get('large') else b''
    server=collection_transport.LiveServer(path,callback)
    try:
        assert 'pipe' in server.endpoint and 'port' not in server.endpoint
        assert collection_transport.request(path,{'large':True})==({'ok':True},body)
        wait_until(lambda:not server.handlers)
        baseline=handles()
        for _ in range(12):
            # A peer can leave before sending its authentication or request.
            collection_transport._connect(server.endpoint,time.monotonic()+1).close()
            for _ in range(3):assert collection_transport.request(path,{})==({'ok':True},b'')
            entered.clear();release.clear()
            cancel=collection_transport.RequestCancellation();result=[]
            worker=threading.Thread(target=lambda:result.append(
                collection_transport.request(path,{'watch':1},timeout=12,cancel=cancel)))
            worker.start()
            try:
                assert entered.wait(2)
                cancel.close();worker.join(1)
                assert not worker.is_alive() and result==[None]
            finally:
                release.set();worker.join(2)
            wait_until(lambda:not server.handlers)
        assert handles()<=baseline+2
    finally:release.set();server.close()
    assert not server.thread.is_alive() and not server.handlers and not server.connections


@pytest.mark.skipif(os.name!='nt',reason='Windows collection uses private named pipes')
def test_pipe_stalled_response_obeys_deadline_and_legacy_tcp_endpoint_is_readable(tmp_path):
    path=tmp_path/'endpoint.json';release=threading.Event();entered=threading.Event()
    def callback(value):
        entered.set();release.wait(2)
        return {'ok':True},b''
    server=collection_transport.LiveServer(path,callback)
    started=time.monotonic()
    try:
        assert collection_transport.request(path,{},timeout=.15) is None
        assert entered.is_set() and time.monotonic()-started<1
    finally:release.set();server.close()
    legacy=collection_transport.LiveServer(path,lambda value:({'legacy':True},b'ok'),transport='tcp')
    try:assert collection_transport.request(path,{})==({'legacy':True},b'ok')
    finally:legacy.close()


@pytest.mark.skipif(os.name!='nt',reason='Windows collection uses private named pipes')
def test_pipe_capacity_rejects_extra_clients_and_recovers_after_disconnect(tmp_path):
    path=tmp_path/'endpoint.json'
    server=collection_transport.LiveServer(path,lambda value:({'ok':True},b''))
    clients=[]
    try:
        for _ in range(16):
            client=collection_transport._connect(server.endpoint,time.monotonic()+1)
            clients.append(client)
            assert len(collection_transport._receive(client,32,time.monotonic()+1))==32
        assert collection_transport.request(path,{},timeout=.1) is None
        clients.pop().close()
        wait_until(lambda:len(server.connections)==15)
        assert collection_transport.request(path,{})==({'ok':True},b'')
    finally:
        for client in clients:client.close()
        server.close()
    assert not server.thread.is_alive() and not server.handlers
