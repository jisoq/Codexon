from contextlib import closing
from copy import deepcopy
import builtins
import io
import json
from pathlib import Path
import sqlite3
from threading import Event
import time

import pytest

from cachemonitor.analysis_delivery import SnapshotReceiver
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.index import UsageIndex
from cachemonitor.model_evidence import EvidenceReader, EvidenceStore, home_key
from cachemonitor.overlay_data import summarize_session
from cachemonitor.prepared_health import hydrate_health
from cachemonitor.prepared_ingest import PreparedCohort, read_catalog, read_enriched_source
from cachemonitor.prepared_query import PreparedQuery, token_value
from cachemonitor.prepared_runtime import PreparedWriter
from cachemonitor.prepared_store import PreparedReader, PreparedStore
from cachemonitor.prepared_summary import hydrate_summary
from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor.usage_collection import CollectorService, CollectionClient, CollectionCatalogUnavailable
from test_core import TID, event, fixture_home, usage


OTHER='12345678-abcd-abcd-abcd-222222222222'
CHILD='12345678-abcd-abcd-abcd-333333333333'
NEW='12345678-abcd-abcd-abcd-444444444444'


def write_session(home, sid, *, parent=None, count=1, at=None, mode='default', written=None):
    at=time.time()-100 if at is None else at
    folder=home/'sessions';folder.mkdir(exist_ok=True)
    path=folder/(sid+'.jsonl')
    context=event('turn_context',at-1,turn_id=sid+'-turn',model='gpt-6-astra',effort='high')
    if mode is not None:context['payload']['service_tier']=mode
    records=[event('session_meta',at-2,id=sid,parent_thread_id=parent,source='cli'),context]
    for number in range(count):
        row=usage(sid+'-'+str(number),at+number,input=10000+number,cached=8000,tid=sid)
        if written is not None:row['payload']['usage']['cache_write_input_tokens']=written
        row['payload'].update(turn_id=sid+'-turn',model='gpt-6-astra')
        if mode is not None:row['payload']['service_tier']=mode
        records.append(row)
    path.write_text(''.join(json.dumps(row)+'\n' for row in records),encoding='utf-8')
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('DELETE FROM threads WHERE id=?',(sid,))
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',
                   (sid,str(path),at,None,sid,'gpt-6-astra','openai','project'))
    return path


def complete(service, *, detailed=False):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        snapshot=service.poll(detailed=detailed)
        assert not snapshot['usage_errors'],snapshot['usage_errors']
        if snapshot['usage_collection_complete'] and not snapshot['index']['loading']:
            return snapshot
    pytest.fail('Demanded source membership did not complete')


def publication(service, writer, receiver, predicate, *, deadline=None):
    deadline=time.monotonic()+10 if deadline is None else deadline
    last=None
    while time.monotonic()<deadline:
        service.poll(detailed=False)
        if not writer.output.receiver.poll(min(.05,max(0,deadline-time.monotonic()))):continue
        message=writer.output.take()
        assert message['kind']!='fatal',message
        if message['kind']!='snapshot':continue
        last=receiver.receive(message['value'])
        if last is not None and predicate(last):return last
    pytest.fail('Demanded publication did not arrive. Last publication '+repr(last))


def selected_detail(service,writer,receiver,snapshot,sid,request_id):
    deadline=time.monotonic()+10
    with closing(PreparedQuery(writer.path)) as query:
        while time.monotonic()<deadline:
            summary=next(row for row in snapshot['overlay_sessions'] if row['id']==sid)
            result=query(dict(kind='overlay_detail',id=request_id,summary=summary),
                         token_value(snapshot['prepared_token']),lambda:False)
            if result['kind']!='stale':
                assert result['kind']=='overlay_detail'
                return result,snapshot
            snapshot=publication(service,writer,receiver,
                lambda value:any(row['id']==sid for row in value['overlay_sessions']),deadline=deadline)
    pytest.fail('Selected detail did not reach the current canonical publication')


class SourceReads:
    def __init__(self, monkeypatch, paths, forbidden):
        self.paths={str(Path(path).resolve()):sid for sid,path in paths.items()}
        self.forbidden=set(forbidden)
        self.bodies=[];self.headers=[];self.cached=[];self.events=[]
        original_open=builtins.open
        original_io_open=io.open
        original_connect=sqlite3.connect
        guard=self

        class SourceFile:
            def __init__(self,stream,sid):self.stream=stream;self.sid=sid
            def __enter__(self):self.stream.__enter__();return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def __getattr__(self,name):return getattr(self.stream,name)
            def readline(self,size=-1):
                if self.stream.tell()==0 and 0<size<=2*1024*1024:
                    guard.headers.append(self.sid)
                else:
                    assert self.sid not in guard.forbidden,'Unvisited original body '+self.sid
                    guard.bodies.append(self.sid)
                return self.stream.readline(size)
            def read(self,*args):
                assert self.sid not in guard.forbidden,'Unvisited original body '+self.sid
                guard.bodies.append(self.sid);return self.stream.read(*args)
            def readinto(self,*args):
                assert self.sid not in guard.forbidden,'Unvisited original body '+self.sid
                guard.bodies.append(self.sid);return self.stream.readinto(*args)
            def fileno(self):
                assert self.sid not in guard.forbidden,'Unvisited native source body '+self.sid
                guard.bodies.append(self.sid)
                return self.stream.fileno()
            def __iter__(self):return self
            def __next__(self):
                value=self.readline()
                if not value:raise StopIteration
                return value

        def wrap(opener,file,*args,**kwargs):
            stream=opener(file,*args,**kwargs)
            if isinstance(file,(str,Path)):
                sid=guard.paths.get(str(Path(file).resolve()))
                mode=args[0] if args else kwargs.get('mode','r')
                if sid and 'r' in mode:return SourceFile(stream,sid)
            return stream

        class CacheConnection(sqlite3.Connection):
            def execute(self,sql,parameters=(),/):
                lowered=sql.lower()
                if lowered.lstrip().startswith('select') and 'payload' in lowered and 'from sessions where home=? and sid=?' in lowered:
                    key=tuple(parameters[-2:])
                    if len(key)==2 and isinstance(key[1],str):
                        assert key[1] not in guard.forbidden,'Unvisited cached body '+key[1]
                        guard.cached.append(key[1])
                return super().execute(sql,parameters)
            def blobopen(self,table,column,row,/,**kwargs):
                if table=='sessions' and column=='payload':
                    owner=super().execute('SELECT home,sid FROM sessions WHERE rowid=?',(row,)).fetchone()
                    if owner is not None:
                        assert owner[1] not in guard.forbidden,'Unvisited cached blob '+owner[1]
                        guard.cached.append(owner[1])
                return super().blobopen(table,column,row,**kwargs)

        def connect(*args,**kwargs):
            kwargs.setdefault('factory',CacheConnection)
            return original_connect(*args,**kwargs)
        monkeypatch.setattr(builtins,'open',lambda *args,**kwargs:wrap(original_open,*args,**kwargs))
        monkeypatch.setattr(io,'open',lambda *args,**kwargs:wrap(original_io_open,*args,**kwargs))
        monkeypatch.setattr(sqlite3,'connect',connect)
        from cachemonitor import index as index_module,event_store,collection_stream,collection_replay,usage_collection
        for module in (index_module,event_store):
            unpack=module.unpack_event
            def decode_event(value,unpack=unpack):
                decoded=unpack(value)
                payload=json.loads(decoded).get('payload',{})
                sid=payload.get('thread_id') or payload.get('id')
                if sid not in paths:
                    sid=next((sid for sid in paths if payload.get('turn_id','').startswith(sid)),None)
                if sid in paths:
                    assert sid not in guard.forbidden,'Unvisited cached event '+sid
                    guard.events.append(sid)
                return decoded
            monkeypatch.setattr(module,'unpack_event',decode_event)
        enter=collection_stream.SelectedOwnerStream.__enter__
        def enter_owner(stream):
            assert stream.key[1] not in guard.forbidden,'Unvisited owner stream '+stream.key[1]
            guard.cached.append(stream.key[1])
            return enter(stream)
        monkeypatch.setattr(collection_stream.SelectedOwnerStream,'__enter__',enter_owner)
        decode=usage_collection.CollectionChannel._decode_body
        def decode_owner(channel,key,payload,digest):
            assert key[1] not in guard.forbidden,'Unvisited decoded body '+key[1]
            guard.cached.append(key[1])
            return decode(channel,key,payload,digest)
        monkeypatch.setattr(usage_collection.CollectionChannel,'_decode_body',decode_owner)
        retain=collection_replay.EncodedSession.__init__
        def retain_owner(body,header,payload,fingerprint,digest):
            assert header['id'] not in guard.forbidden,'Unvisited retained payload '+header['id']
            guard.cached.append(header['id'])
            retain(body,header,payload,fingerprint,digest)
        monkeypatch.setattr(collection_replay.EncodedSession,'__init__',retain_owner)


def assert_own_matches_kernel(path, token, source):
    engine=AnalysisEngine();engine.ingest([deepcopy(source)])
    owner=source['home'],source['id']
    prepared=engine.sessions[owner]['prepared']
    expected=summarize_session(prepared,prepared['cache_health'])
    with PreparedReader(path) as reader,reader.pin(token_value(token)) as view:
        actual=hydrate_summary(view,*owner)['own']
        actual.update(hydrate_health(view,owner));actual.pop('_health_ref')
        assert actual==expected
        assert len(actual['recent'])==min(20,len(source['history']))


@pytest.mark.parametrize('warm',[False,True])
def test_selected_entry_never_reads_unvisited_original_or_cached_body(tmp_path,monkeypatch,warm):
    home,_=fixture_home(tmp_path)
    selected=write_session(home,TID,count=27,at=time.time()-100000)
    unrelated=write_session(home,OTHER,count=11,at=time.time()-200000)
    target=tmp_path/'index.sqlite';quota=tmp_path/'quota.sqlite'
    evidence=tmp_path/'model-evidence.sqlite'
    with closing(EvidenceStore(evidence)) as observations:
        observations.db.executemany('INSERT INTO model_observations '
            '(home,attempt,ts,transport,response_id,requested_model,response_model,status,conflict) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            ((home_key(home),'historical'+str(n),n,'WebSocket','old'+str(n),'old','old','completed',0)
             for n in range(EvidenceReader.MAX_POLL_ROWS+1)))
        observations.write(home,'selected',time.time(),'WebSocket',TID+'-26','gpt-6-astra','gpt-6-astra','completed',
            requested_service_tier='priority',request_observed_at=time.time()-1,
            generation_latency_ms=120,completion_latency_ms=450)
    if warm:
        with closing(CollectorService([home],target)) as service:
            complete(service,detailed=True)
            deadline=time.monotonic()+10
            while service.maintain():assert time.monotonic()<deadline
    reads=SourceReads(monkeypatch,{TID:selected,OTHER:unrelated},{OTHER})
    from cachemonitor import model_evidence
    observation_record=model_evidence.observation_record
    def selected_observation(row):
        assert not row['attempt'].startswith('historical'),'Unrelated historical evidence was decoded'
        return observation_record(row)
    monkeypatch.setattr(model_evidence,'observation_record',selected_observation)
    with closing(CollectorService([home],target)) as service,closing(CollectionClient([home],target)) as client:
        client.set_demand([(None,TID)])
        source=complete(service)['sessions'][0]
        assert source['id']==TID
        assert service.index.model_evidence.offset==0 and service.index.model_evidence.catching_up
        with PreparedStore(tmp_path/'prepared.sqlite') as store,closing(QuotaLedger(quota)) as ledger:
            catalog=read_catalog(client)
            source,_=read_enriched_source(client,(str(home),TID),catalog,ledger)
            row=next(row for row in source['history'] if row['key']==TID+'-26')
            assert row['model_source']=='wire' and row['requested_service_tier']=='priority'
            assert row['service_tier']=='Fast'
            assert row['generation_latency_ms']==120 and row['completion_latency_ms']==450
            changes=list(PreparedCohort(store,client,ledger).convert(catalog))
            assert changes[-1]['ready']
            assert_own_matches_kernel(store.path,(store.token.instance,store.token.generation),source)
        assert set(reads.bodies)<= {TID}
        assert set(reads.cached)<= {TID}
        assert set(reads.events)<= {TID}
        assert reads.bodies or reads.cached


def test_retired_selected_body_waits_for_evidence_through_the_current_bound(tmp_path,monkeypatch):
    from cachemonitor import model_evidence
    from cachemonitor.pricing import token_cost
    home,_=fixture_home(tmp_path)
    at=time.time()-100000
    selected=write_session(home,TID,at=at,written=0)
    unrelated=write_session(home,OTHER,at=at-1000)
    target=tmp_path/'index.sqlite';evidence=tmp_path/'model-evidence.sqlite'
    monkeypatch.setattr(EvidenceReader,'MAX_POLL_ROWS',2)
    with closing(EvidenceStore(evidence)) as observations:
        for number in range(5):
            observations.write(home,'historical'+str(number),number,'WebSocket','old'+str(number),status='completed')
        observations.write(home,'current',at,'WebSocket',TID+'-0','gpt-6.1-terra','gpt-6.1-terra','completed',
            requested_service_tier='default',generation_latency_ms=10,completion_latency_ms=20)
        reads=SourceReads(monkeypatch,{TID:selected,OTHER:unrelated},{OTHER})
        decode=model_evidence.observation_record
        def current_observation(row):
            assert not row['attempt'].startswith('historical'),'Historical evidence was replayed'
            return decode(row)
        monkeypatch.setattr(model_evidence,'observation_record',current_observation)
        with closing(CollectorService([home],target,evidence)) as service,\
                closing(CollectionClient([home],target,evidence)) as client,\
                closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger,\
                PreparedStore(tmp_path/'prepared.sqlite') as store:
            client.set_demand([(None,TID)])
            complete(service)
            deadline=time.monotonic()+10
            while service.maintain():assert time.monotonic()<deadline
            key=str(home),TID
            assert hasattr(service.index.monitor.sessions[key],'_replay_body')
            initial,_=read_enriched_source(client,key,read_catalog(client),ledger)
            initial_row=initial['history'][0]
            assert initial_row['model']=='gpt-6.1-terra' and initial_row['completion_latency_ms']==20
            initial_cost=token_cost(initial_row)['cost']
            observations.write(home,'unrelated-one',at+1,'WebSocket','unrelated-one',status='completed')
            observations.write(home,'unrelated-two',at+2,'WebSocket','unrelated-two',status='completed')
            observations.write(home,'current',at+3,'WebSocket',TID+'-0','gpt-6.1-sol','gpt-6.1-sol','completed',
                requested_service_tier='priority',response_service_tier='priority',
                request_observed_at=at+2,completed_observed_at=at+3,
                generation_latency_ms=120,completion_latency_ms=1000)

            def current_source(catalog):
                source,_=read_enriched_source(client,key,catalog,ledger)
                row=source['history'][0]
                assert row['model']==row['requested_model']==row['response_model']=='gpt-6.1-sol'
                assert row['model_source']=='wire' and row['requested_service_tier']=='priority'
                assert row['response_service_tier']=='priority' and row['service_tier']=='Fast'
                assert row['generation_latency_ms']==120 and row['completion_latency_ms']==1000
                return source

            first=service.poll(detailed=False)
            reader=service.index.model_evidence
            assert reader.offset==0 and reader.live_offset<reader.highwater
            assert TID+'-0' not in reader.changed_responses.get(home_key(home),set())
            first_catalog=read_catalog(client)
            if first['sessions'][0]['collection_complete']:
                current_source(first_catalog)
            else:
                assert not first_catalog['membership_complete']
            complete(service)
            catalog=read_catalog(client)
            source=current_source(catalog)
            assert list(PreparedCohort(store,client,ledger).convert(catalog))[-1]['ready']
            with PreparedReader(store.path) as prepared,prepared.pin(store.token) as view:
                actual=hydrate_summary(view,*key)['own']
                assert actual['cost']==pytest.approx(token_cost(source['history'][0])['cost'])
                assert actual['cost']!=initial_cost
            assert_own_matches_kernel(store.path,(store.token.instance,store.token.generation),source)
            assert reader.offset==0
            assert set(reads.bodies)<= {TID} and set(reads.cached)<= {TID} and set(reads.events)<= {TID}


def test_no_target_then_new_selected_session_and_first_completed_call(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    old=write_session(home,TID,at=time.time()-100000)
    reads=SourceReads(monkeypatch,{TID:old},{TID})
    target=tmp_path/'index.sqlite'
    with closing(CollectorService([home],target)) as service,closing(CollectionClient([home],target)) as client:
        client.set_demand()
        idle=complete(service)
        assert idle['sessions']==[] and idle['catalog_complete']
        assert reads.bodies==[] and reads.cached==[]
        new=write_session(home,NEW,count=0)
        client.set_demand([(None,NEW)])
        entered=complete(service)
        current=next(source for source in entered['sessions'] if source['id']==NEW)
        current,_=read_enriched_source(client,(str(home),NEW),read_catalog(client),None)
        assert current['history']==[]
        now=time.time()
        records=[event('event_msg',now,type='task_started',turn_id=NEW+'-turn'),
                 usage('first-call',now+.001,tid=NEW),
                 event('event_msg',now+.002,type='task_complete',turn_id=NEW+'-turn')]
        records[1]['payload'].update(turn_id=NEW+'-turn',model='gpt-6-astra',service_tier='default')
        with new.open('a',encoding='utf-8') as stream:
            stream.write(''.join(json.dumps(row)+'\n' for row in records))
        completed=complete(service)
        current=next(source for source in completed['sessions'] if source['id']==NEW)
        current,_=read_enriched_source(client,(str(home),NEW),read_catalog(client),None)
        assert [row['key'] for row in current['history']]==['first-call']
        assert current['turn_records'][NEW+'-turn']['state']=='완료'
        assert set(reads.bodies+reads.cached)<={NEW}


def test_writer_first_selection_after_idle_wakes_on_source_completion(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    write_session(home,TID)
    target=tmp_path/'index.sqlite';receiver=SnapshotReceiver()
    change_selection=Event();wake_consumed=Event();resume_writer=Event();pending=Event()
    consume_notification=CollectionClient.consume_notification
    poll_selection=CollectionClient.poll_selection
    def consume(client,*args,**kwargs):
        notice=consume_notification(client,*args,**kwargs)
        if notice is True and change_selection.is_set():
            change_selection.clear()
            wake_consumed.set()
            assert resume_writer.wait(10)
        return notice
    def selection(client,*args,**kwargs):
        try:return poll_selection(client,*args,**kwargs)
        except CollectionCatalogUnavailable as error:
            if error.reason=='selected_pending' and [None,TID] in client.active_demand['selected']:
                pending.set()
            raise
    monkeypatch.setattr(CollectionClient,'consume_notification',consume)
    monkeypatch.setattr(CollectionClient,'poll_selection',selection)
    with closing(CollectorService([home],target)) as service,\
            closing(PreparedWriter([home],target,None,tmp_path/'quota.sqlite')) as writer:
        idle=publication(service,writer,receiver,lambda value:value.get('catalog_complete')
            and not value['sessions'])
        assert not idle['overlay_sessions']
        change_selection.set()
        service.poll(detailed=False)
        try:
            for barrier in (wake_consumed,pending):
                if barrier is pending:
                    writer.set_selected([(None,TID)])
                    resume_writer.set()
                deadline=time.monotonic()+10
                while not barrier.is_set() and time.monotonic()<deadline:
                    if not writer.output.receiver.poll(min(.05,max(0,deadline-time.monotonic()))):continue
                    message=writer.output.take()
                    assert message['kind']!='fatal',message
                    if message['kind']=='snapshot':receiver.receive(message['value'])
                assert barrier.is_set()
            completed=complete(service)
            assert next(row for row in completed['sessions'] if row['id']==TID)['collection_complete']
            current=publication(service,writer,receiver,lambda value:
                any(row['id']==TID and row['calls']==1 for row in value['overlay_sessions']),
                deadline=time.monotonic()+1)
            detail,_=selected_detail(service,writer,receiver,current,TID,'idle-selection')
            assert detail['summary']['calls']==1
        finally:resume_writer.set()


def test_writer_selection_survives_collector_starting_later(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    selected=write_session(home,TID)
    other=write_session(home,OTHER)
    reads=SourceReads(monkeypatch,{TID:selected,OTHER:other},{OTHER})
    attempted=Event()
    set_demand=CollectionClient.set_demand
    def initial_demand(client,selected=(),**options):
        set_demand(client,selected,**options)
        if selected:attempted.set()
    monkeypatch.setattr(CollectionClient,'set_demand',initial_demand)
    target=tmp_path/'index.sqlite';receiver=SnapshotReceiver()
    with closing(PreparedWriter([home],target,None,tmp_path/'quota.sqlite')) as writer:
        writer.set_selected([(None,TID)])
        assert attempted.wait(10)
        with closing(CollectorService([home],target)) as service:
            current=publication(service,writer,receiver,lambda value:
                any(row['id']==TID for row in value['overlay_sessions']))
            detail,_=selected_detail(service,writer,receiver,current,TID,'late-collector')
            assert detail['summary']['calls']==1
            assert OTHER not in reads.bodies+reads.cached+reads.events


def test_writer_selection_survives_collector_restart_without_reselection(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    selected=write_session(home,TID)
    other=write_session(home,OTHER)
    reads=SourceReads(monkeypatch,{TID:selected,OTHER:other},{OTHER})
    target=tmp_path/'index.sqlite';receiver=SnapshotReceiver()
    with closing(CollectionClient([home],target)):pass
    with closing(PreparedWriter([home],target,None,tmp_path/'quota.sqlite')) as writer:
        writer.set_selected([(None,TID)])
        with closing(CollectorService([home],target)) as service:
            publication(service,writer,receiver,lambda value:
                any(row['id']==TID for row in value['overlay_sessions']))
        write_session(home,TID,count=2)
        with closing(CollectorService([home],target)) as service:
            current=publication(service,writer,receiver,lambda value:
                any(row['id']==TID and row['calls']==2 for row in value['overlay_sessions']))
            detail,_=selected_detail(service,writer,receiver,current,TID,'restarted-collector')
            assert detail['summary']['calls']==2
            assert OTHER not in reads.bodies+reads.cached+reads.events


@pytest.mark.parametrize('ambiguous',[False,True])
def test_writer_resolves_unavailable_target_without_loading_bodies_and_finds_new_session_later(tmp_path,monkeypatch,ambiguous):
    home,_=fixture_home(tmp_path)
    paths={TID:write_session(home,TID),OTHER:write_session(home,OTHER)}
    homes=[home]
    wanted=TID if ambiguous else NEW
    if ambiguous:
        second=tmp_path/'second';second.mkdir()
        other_home,_=fixture_home(second)
        paths['duplicate-home']=write_session(other_home,TID)
        homes.append(other_home)
    reads=SourceReads(monkeypatch,paths,set(paths))
    target=tmp_path/'index.sqlite';receiver=SnapshotReceiver()
    with closing(CollectorService(homes,target)) as service,\
            closing(PreparedWriter(homes,target,None,tmp_path/'quota.sqlite')) as writer:
        writer.set_selected([(None,wanted)])
        resolved=publication(service,writer,receiver,lambda value:value.get('catalog_complete')
            and [None,wanted] in value.get('demand',{}).get('selected',()))
        assert not resolved['sessions'] and not resolved['overlay_sessions']
        assert resolved['selected_resolutions']==[[wanted,2 if ambiguous else 0]]
        assert 'session_catalog' not in resolved
        assert not reads.bodies and not reads.cached and not reads.events
        if not ambiguous:
            created=write_session(home,NEW)
            reads.paths[str(created.resolve())]=NEW
            current=publication(service,writer,receiver,lambda value:
                any(row['id']==NEW for row in value['overlay_sessions']))
            detail,_=selected_detail(service,writer,receiver,current,NEW,'newly-available')
            assert detail['summary']['calls']==1
            assert {TID,OTHER}.isdisjoint(reads.bodies+reads.cached+reads.events)


@pytest.mark.parametrize('aggregate',[False,True])
def test_selected_family_larger_than_publication_batch_is_always_complete(tmp_path,monkeypatch,aggregate):
    home,_=fixture_home(tmp_path)
    children={f'00000000-0000-0000-0000-{number:012d}' for number in range(17)}
    paths={TID:write_session(home,TID,at=time.time()-2000)}
    paths.update({sid:write_session(home,sid,parent=TID,at=time.time()-1000-number)
                  for number,sid in enumerate(sorted(children))})
    unrelated={OTHER,*[f'90000000-0000-0000-0000-{number:012d}' for number in range(19)]}
    paths.update({sid:write_session(home,sid) for sid in unrelated})
    reads=SourceReads(monkeypatch,paths,set() if aggregate else unrelated)
    target=tmp_path/'index.sqlite';receiver=SnapshotReceiver()
    def family_ready(value):
        roots=[row for row in value['overlay_sessions'] if row['id']==TID]
        if not roots:return False
        root=roots[0]
        assert {TID,*children}<={row['id'] for row in value['sessions']}
        assert root['descendants']==17 and root['calls']==18
        return True
    with closing(CollectorService([home],target)) as service,\
            closing(PreparedWriter([home],target,None,tmp_path/'quota.sqlite')) as writer:
        writer.set_selected([(None,TID)])
        if aggregate:writer.set_aggregate(23)
        current=publication(service,writer,receiver,family_ready)
        detail,_=selected_detail(service,writer,receiver,current,TID,'whole-family')
        assert detail['summary']['descendants']==17 and detail['summary']['calls']==18
        assert {row['sid'] for row in detail['summary']['recent']}=={TID,*children}
        if aggregate:
            first_unrelated=next((number for number,sid in enumerate(reads.bodies) if sid in unrelated),None)
            if first_unrelated is not None:assert children<=set(reads.bodies[:first_unrelated])
        else:assert unrelated.isdisjoint(reads.bodies+reads.cached+reads.events)


def test_selected_family_and_actual_mode_ancestor_are_the_only_admitted_histories(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    parent=write_session(home,TID,mode='priority')
    selected=write_session(home,CHILD,parent=TID,mode=None)
    descendant=write_session(home,NEW,parent=CHILD,mode='default')
    unrelated=write_session(home,OTHER)
    reads=SourceReads(monkeypatch,{TID:parent,CHILD:selected,NEW:descendant,OTHER:unrelated},{TID,OTHER})
    target=tmp_path/'index.sqlite'
    with closing(CollectorService([home],target)) as service,closing(CollectionClient([home],target)) as client:
        client.set_demand([(None,CHILD)])
        first=complete(service)
        assert {source['id'] for source in first['sessions']}=={CHILD,NEW}
        with PreparedStore(tmp_path/'prepared.sqlite') as store,closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger:
            with pytest.raises(CollectionCatalogUnavailable,match='mode_source_pending'):
                list(PreparedCohort(store,client,ledger).convert(read_catalog(client)))
            reads.forbidden.remove(TID)
            enriched=complete(service)
            list(PreparedCohort(store,client,ledger).convert(read_catalog(client)))
            with store.read_current() as view:
                assert view.owner(str(home),TID) is None
                assert view.owner(str(home),OTHER) is None
                row=view.record(str(home),CHILD,CHILD+'-0')
                assert row['service_tier']=='Fast'
            assert {source['id'] for source in enriched['sessions']}=={TID,CHILD,NEW}
            assert OTHER not in reads.bodies and OTHER not in reads.cached


def test_selected_writer_completes_with_empty_mode_ancestor_outside_family(tmp_path):
    home,_=fixture_home(tmp_path)
    write_session(home,TID,count=0,mode=None)
    write_session(home,CHILD,parent=TID,mode=None)
    target=tmp_path/'index.sqlite';receiver=SnapshotReceiver()
    with closing(CollectorService([home],target)) as service,\
            closing(PreparedWriter([home],target,None,tmp_path/'quota.sqlite')) as writer:
        writer.set_selected([(None,CHILD)])
        current=publication(service,writer,receiver,lambda value:
            any(row['id']==CHILD and row['calls']==1 for row in value['overlay_sessions']))
        assert {row['id'] for row in current['sessions']}=={CHILD}
        assert {row['id'] for row in current['overlay_sessions']}=={CHILD}
        assert (str(home),TID) in service.channel.live.requested_demand(time.time())['evidence']
        parent=next(row for row in service.last['sessions'] if row['id']==TID)
        assert parent['collection_complete']
        detail,_=selected_detail(service,writer,receiver,current,CHILD,'empty-mode-ancestor')
        assert detail['summary']['calls']==1
        assert detail['summary']['latest']['mode']=='미확인'


def test_current_session_updates_while_aggregate_source_is_unavailable(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    write_session(home,TID,count=1)
    other=write_session(home,OTHER,count=3)
    target=tmp_path/'index.sqlite';quota=tmp_path/'quota.sqlite'
    receiver=SnapshotReceiver()
    release=Event();rebuilding=Event()
    source_open=io.open
    def held_open(path,*args,**kwargs):
        if str(path)==str(other) and not release.is_set():
            rebuilding.set()
            raise PermissionError('aggregate source is held unavailable')
        return source_open(path,*args,**kwargs)
    with closing(CollectorService([home],target)) as service,closing(PreparedWriter([home],target,None,quota)) as writer:
        writer.set_selected([(None,TID)])
        first=publication(service,writer,receiver,lambda value:any(row['id']==TID for row in value['sessions']))
        monkeypatch.setattr(io,'open',held_open)
        writer.set_aggregate(7)
        publication(service,writer,receiver,lambda value:rebuilding.is_set())
        write_session(home,TID,count=2)
        updated=publication(service,writer,receiver,lambda value:
            any(row['id']==TID and row['calls']==2 for row in value['overlay_sessions']))
        detail,_=selected_detail(service,writer,receiver,updated,TID,'updated')
        assert len(detail['summary']['recent'])==2
        assert not release.is_set()
        assert updated.get('aggregate_request') is None


def test_writer_keeps_selected_detail_live_until_explicit_aggregate_completes_and_preserves_ledger(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    selected=write_session(home,TID,count=25)
    other=write_session(home,OTHER,count=3)
    reads=SourceReads(monkeypatch,{TID:selected,OTHER:other},{OTHER})
    target=tmp_path/'index.sqlite';quota=tmp_path/'quota.sqlite'
    receiver=SnapshotReceiver()
    with closing(CollectorService([home],target)) as service,closing(PreparedWriter([home],target,None,quota)) as writer:
        writer.set_selected([(None,TID)])
        first=publication(service,writer,receiver,lambda value:any(row['id']==TID for row in value['sessions']))
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            with sqlite3.connect(quota) as db:
                selected_calls=db.execute('SELECT count(*) FROM calls').fetchone()[0]
                initial_cursor=db.execute('SELECT source,revision FROM collection_activity_cursor WHERE id=1').fetchone()
            if selected_calls==25:break
            publication(service,writer,receiver,lambda value:any(row['id']==TID for row in value['sessions']))
        assert selected_calls==25 and initial_cursor is None
        detail,first=selected_detail(service,writer,receiver,first,TID,'selected')
        assert len(detail['summary']['recent'])==20
        reads.forbidden.clear()
        release=Event();rebuilding=Event()
        source_open=io.open
        def held_open(path,*args,**kwargs):
            if str(path)==str(other) and not release.is_set():
                rebuilding.set()
                raise PermissionError('aggregate source is held unavailable')
            return source_open(path,*args,**kwargs)
        monkeypatch.setattr(io,'open',held_open)
        writer.set_aggregate(7)
        first=publication(service,writer,receiver,lambda value:rebuilding.is_set())
        assert rebuilding.is_set() and not service.last['usage_collection_complete']
        retained,first=selected_detail(service,writer,receiver,first,TID,'retained')
        assert retained['kind']=='overlay_detail' and not release.is_set()
        assert first.get('aggregate_request') is None
        assert not service.last['usage_collection_complete']
        release.set()
        aggregate=publication(service,writer,receiver,lambda value:value.get('aggregate_request')==7)
        assert {row['id'] for row in aggregate['sessions']}=={TID}
        deadline=time.monotonic()+10
        with closing(PreparedQuery(writer.path)) as query:
            while time.monotonic()<deadline:
                now=time.time()
                result=query(dict(kind='query',id='all-calls',logical=(),query=dict(
                    page=1,start=0,end=now,now=now,period='custom',category_usage=True,
                    usage=dict(dimension='model',metric='calls'))),
                    token_value(aggregate['prepared_token']),lambda:False)
                if result['kind']!='stale':break
                aggregate=publication(service,writer,receiver,
                    lambda value:value.get('aggregate_request')==7,deadline=deadline)
            else:pytest.fail('Complete aggregate query did not reach the displayed publication')
        assert result['kind']=='result'
        assert result['result']['usage']['summary']['value']==28
        assert result['result']['analysis']['response_count']==28
        assert result['result']['analysis']['session_count']==2
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            with sqlite3.connect(quota) as db:
                saved=db.execute('SELECT count(*) FROM calls').fetchone()[0]
                cursor=db.execute('SELECT source,revision FROM collection_activity_cursor WHERE id=1').fetchone()
            if saved==28:break
            publication(service,writer,receiver,lambda value:value.get('aggregate_request')==7)
        assert saved==28
        with sqlite3.connect(quota) as db:
            assert dict(db.execute('SELECT sid,count(*) FROM calls GROUP BY sid'))=={TID:25,OTHER:3}
        writer.set_aggregate(None)
        writer.set_selected([(None,OTHER)])
        selected_again=publication(service,writer,receiver,lambda value:
            value.get('aggregate_request') is None and {row['id'] for row in value['sessions']}=={OTHER})
        assert selected_again['sessions'][0]['collection_complete']
        with sqlite3.connect(quota) as db:
            assert db.execute('SELECT count(*) FROM calls').fetchone()[0]==28
            assert db.execute('SELECT source,revision FROM collection_activity_cursor WHERE id=1').fetchone()==cursor
        assert OTHER in reads.bodies or OTHER in reads.cached


def test_live_observations_and_selected_tasks_reach_ledger_without_global_replay(tmp_path,monkeypatch):
    from cachemonitor.quota_tracking_store import enable
    home,_=fixture_home(tmp_path)
    selected=write_session(home,TID,count=0)
    unrelated=write_session(home,OTHER)
    target=tmp_path/'index.sqlite';quota=tmp_path/'quota.sqlite';evidence=tmp_path/'model-evidence.sqlite'
    with closing(EvidenceStore(evidence)) as observations:
        observations.db.executemany('INSERT INTO model_observations '
            '(home,attempt,ts,transport,response_id,requested_model,response_model,status,conflict) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            ((home_key(home),'historical'+str(n),n,'WebSocket','old'+str(n),'old','old','completed',0)
             for n in range(600)))
        observations.db.commit()
    with closing(CollectorService([home],target,evidence)) as warm:complete(warm,detailed=True)
    reads=SourceReads(monkeypatch,{TID:selected,OTHER:unrelated},{OTHER})
    from cachemonitor.usage_collection import CollectionChannel
    activity_page=CollectionChannel.activity_page
    def current_activity(channel,result,epoch,*,live=False):
        value=activity_page(channel,result,epoch,live=live)
        if live:assert all(row['attempt']=='current' for row in value.get('request_activity',[]))
        return value
    monkeypatch.setattr(CollectionChannel,'activity_page',current_activity)
    now=time.time()
    with closing(QuotaLedger(quota)) as ledger:enable(ledger.db,str(home),at=now-10)
    receiver=SnapshotReceiver()
    def tracked(status):
        def accept(value):
            with sqlite3.connect(quota) as db:
                row=db.execute('SELECT response,end,status FROM tracking_wire WHERE home=? AND identity=?',
                    (str(home),'current')).fetchone()
                assert db.execute('SELECT count(*) FROM collection_activity_cursor').fetchone()[0]==0
            return row is not None and row[2]==status
        return accept
    with closing(EvidenceStore(evidence)) as observations,closing(CollectorService([home],target,evidence)) as service,\
            closing(PreparedWriter([home],target,evidence,quota)) as writer:
        writer.set_selected([])
        publication(service,writer,receiver,lambda value:not value['sessions'])
        observations.write(home,'current',now,'WebSocket',status='pending',request_observed_at=now)
        pending=publication(service,writer,receiver,tracked('pending'))
        assert pending['sessions']==[]
        with sqlite3.connect(quota) as db:
            assert db.execute('SELECT count(*) FROM calls').fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM tracking_tasks').fetchone()[0]==0
        with selected.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(event('event_msg',now,type='task_started',turn_id=TID+'-turn'))+'\n')
        writer.set_selected([(None,TID)])
        publication(service,writer,receiver,lambda value:any(row['id']==TID for row in value['sessions']))
        observations.write(home,'current',now+.1,'WebSocket',TID+'-0','gpt-6-astra',status='created',request_observed_at=now)
        publication(service,writer,receiver,tracked('created'))
        with sqlite3.connect(quota) as db:
            assert db.execute('SELECT count(*) FROM calls').fetchone()[0]==0
            task=db.execute('SELECT start,end FROM tracking_tasks WHERE identity=?',(TID+':'+TID+'-turn',)).fetchone()
            assert task is not None and task[1] is None
        records=[usage(TID+'-0',now+.2,tid=TID),
                 event('event_msg',now+.3,type='task_complete',turn_id=TID+'-turn')]
        records[0]['payload'].update(turn_id=TID+'-turn',model='gpt-6-astra',service_tier='default')
        with selected.open('a',encoding='utf-8') as stream:
            stream.write(''.join(json.dumps(record)+'\n' for record in records))
        observations.write(home,'current',now+.3,'WebSocket',TID+'-0','gpt-6-astra','gpt-6-astra','completed',
            request_observed_at=now,completed_observed_at=now+.3,generation_latency_ms=100,completion_latency_ms=300)
        finished=publication(service,writer,receiver,tracked('completed'))
        detail,_=selected_detail(service,writer,receiver,finished,TID,'completed')
        assert detail['summary']['recent'][0]['model_source']=='wire'
        assert detail['summary']['recent'][0]['completion_latency_ms']==300
        with sqlite3.connect(quota) as db:
            assert db.execute('SELECT count(*) FROM calls').fetchone()[0]==1
            assert db.execute('SELECT end FROM tracking_tasks WHERE identity=?',(TID+':'+TID+'-turn',)).fetchone()[0] is not None
            assert db.execute('SELECT count(*) FROM collection_activity_cursor').fetchone()[0]==0
        assert service.index.model_evidence.offset==0
        assert OTHER not in reads.bodies and OTHER not in reads.events and OTHER not in reads.cached


def test_switch_cancels_queued_source_and_prepared_membership(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    abandoned=write_session(home,TID,count=30)
    selected=write_session(home,OTHER)
    reads=SourceReads(monkeypatch,{TID:abandoned,OTHER:selected},{TID})
    target=tmp_path/'index.sqlite'
    with closing(CollectorService([home],target)) as service,closing(CollectionClient([home],target)) as client:
        client.set_demand([(None,TID)])
        client.set_demand([(None,OTHER)])
        value=complete(service)
        assert {source['id'] for source in value['sessions']}=={OTHER}
        with PreparedStore(tmp_path/'prepared.sqlite') as store:
            list(PreparedCohort(store,client,None).convert(read_catalog(client)))
            with store.read_current() as view:
                assert view.owner(str(home),TID) is None
                assert view.record(str(home),OTHER,OTHER+'-0')['input']==10000
        assert TID not in reads.bodies and TID not in reads.cached


def test_dispatch_waits_for_complete_aggregate_and_serves_selected_detail_during_collection(tmp_path,monkeypatch):
    from multiprocessing import Pipe
    from threading import Event,Thread
    from types import SimpleNamespace
    from cachemonitor import prepared_runtime
    stopped=Event();output=prepared_runtime.Mailbox(stopped)
    demands=[];queries=[];failures=[]
    writer=SimpleNamespace(path=tmp_path/'prepared.sqlite',output=output,close=lambda:None,
        set_selected=lambda selected:None,set_aggregate=demands.append)
    monkeypatch.setattr(prepared_runtime,'PreparedWriter',lambda *args:writer)
    class Handler:
        def __init__(self,path):pass
        def __call__(self,message,token,cancel):
            if message['kind']=='query':queries.append(message['id'])
            return dict(kind='result' if message['kind']=='query' else message['kind'],id=message['id'])
        def close(self):pass
    monkeypatch.setattr(prepared_runtime,'PreparedQuery',Handler)
    parent,child=Pipe()
    def dispatch():
        try:prepared_runtime.process_main(child,[],None,None,tmp_path/'quota.sqlite')
        except BaseException as error:failures.append(error)
    thread=Thread(target=dispatch,daemon=True);thread.start()
    def receive(kind):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            assert parent.poll(max(0,deadline-time.monotonic())),kind
            value=parent.recv()
            if value['kind']==kind:return value
        pytest.fail(kind+' did not arrive')
    def snapshot(request_id,generation):
        output.put(dict(kind='snapshot',value=dict(aggregate_request=request_id),
            token=('instance',generation),transition=None))
        receive('snapshot')
    try:
        snapshot(None,1)
        parent.send(dict(kind='query',id=42,query={},logical=()))
        parent.send(dict(kind='overlay_detail',id='during-collection',summary={'_prepared_token':('instance',1)}))
        assert receive('overlay_detail')['id']=='during-collection'
        assert demands==[42] and queries==[]
        snapshot(None,2)
        parent.send(dict(kind='overlay_detail',id='partial-membership',summary={'_prepared_token':('instance',2)}))
        assert receive('overlay_detail')['id']=='partial-membership'
        assert queries==[]
        snapshot(42,3)
        assert receive('result')['id']==42 and queries==[42]
        parent.send(dict(kind='query',id=43,query={},logical=()))
        parent.send(dict(kind='aggregate_close'))
        snapshot(43,4)
        parent.send(dict(kind='overlay_detail',id='after-cancel',summary={'_prepared_token':('instance',4)}))
        assert receive('overlay_detail')['id']=='after-cancel'
        assert queries==[42] and demands[-1] is None
    finally:
        parent.send(dict(kind='stop'));thread.join(5)
        parent.close();child.close();output.close()
    assert not thread.is_alive() and not failures


def test_new_demand_wakes_existing_source_wait_without_a_file_write(tmp_path):
    from cachemonitor.collection_wake import SourceChanges
    revision=[0]
    changes=SourceChanges(evidence=tmp_path/'evidence.sqlite',demand=lambda:revision[0])
    try:
        revision[0]+=1
        hint=changes.wait(time.monotonic()+1)
        assert hint.uncertain and not hint.roots and not hint.metadata
    finally:changes.close()


def test_unreadable_unrelated_aggregate_source_does_not_block_selected_publication(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    write_session(home,TID)
    unrelated=write_session(home,OTHER)
    original_open=io.open
    def source_open(path,*args,**kwargs):
        if Path(path)==unrelated:raise PermissionError('unrelated source is unavailable')
        return original_open(path,*args,**kwargs)
    monkeypatch.setattr(io,'open',source_open)
    target=tmp_path/'index.sqlite'
    with closing(CollectorService([home],target)) as service,closing(CollectionClient([home],target)) as client:
        client.set_demand([(None,TID)],aggregate=True)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            value=service.poll(detailed=False)
            if value['usage_errors']:break
        assert value['usage_errors'] and not value['usage_collection_complete']
        selected=next(source for source in value['sessions'] if source['id']==TID)
        assert selected['collection_complete'] and len(selected['history'])==1
        catalog=read_catalog(client)
        assert catalog['membership_complete'] and not catalog['aggregate_complete']
        with PreparedStore(tmp_path/'prepared.sqlite') as store:
            list(PreparedCohort(store,client,None).convert(catalog))
            with store.read_current() as view:
                assert view.record(str(home),TID,TID+'-0')['input']==10000
