import json

from cachemonitor.model_evidence import EvidenceStore, EvidenceReader
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.index import UsageIndex
from test_index import finish
from test_core import fixture_home


def test_old_evidence_remains_queryable_after_bounded_memory_eviction(tmp_path,monkeypatch):
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path)
    monkeypatch.setattr(reader,'MAX_RECORDS',16)
    try:
        for n in range(128):store.write(tmp_path/'home',f'a{n}',n,'WebSocket',f'r{n}','requested','actual','completed')
        reader.poll()
        assert len(reader.activity_records)<=16
        assert sum(len(records) for records in reader.records.values())<=16
        row=reader.enrich(tmp_path/'home',[{'key':'r0'}])[0]
        assert row['requested_model']=='requested' and row['response_model']=='actual'
        store.write(tmp_path/'home','a0',200,'WebSocket','r0','corrected','corrected','completed')
        reader.poll()
        assert len(reader.changed_activity)==1
        assert reader.enrich(tmp_path/'home',[{'key':'r0'}])[0]['requested_model']=='corrected'
    finally:reader.close();store.close()


def test_many_attempts_keep_all_conflict_decisions_with_bounded_join_memory(tmp_path,monkeypatch):
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    try:
        for n in range(257):
            store.write(home,f'a{n}',n,'HTTP/SSE' if n==128 else 'WebSocket','same',
                'different' if n==128 else 'requested','actual','completed',
                requested_service_tier='priority' if n==129 else 'default',observation_missing=n==127)
        reader.poll();rows=[{'key':'same'}]
        original=reader.evidence_rows
        selected=list(original(home_key(home),rows))[0][1]
        assert len(selected)<=32
        actual=reader.enrich(home,rows)
        full=[dict(row) for row in reader.db.execute('SELECT * FROM model_observations')]
        monkeypatch.setattr(reader,'evidence_rows',lambda home,rows:((row,full) for row in rows))
        assert reader.enrich(home,rows)==actual
    finally:reader.close();store.close()


def test_reader_drains_available_batches_without_waiting_between_polls(tmp_path, monkeypatch):
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path)
    monkeypatch.setattr(reader,'READ_BATCH',2)
    try:
        for n in range(7):store.write(tmp_path/'home',f'a{n}',n,'WebSocket',f'r{n}',status='completed')
        reader.poll()
        assert len(reader.records)==7 and not reader.catching_up
        monkeypatch.setattr(reader,'READ_SECONDS',0)
        for n in range(7,12):store.write(tmp_path/'home',f'a{n}',n,'WebSocket',f'r{n}',status='completed')
        reader.poll()
        assert reader.catching_up and len(reader.records)==9
        monkeypatch.setattr(reader,'READ_SECONDS',10)
        reader.poll()
        assert not reader.catching_up and len(reader.records)==12
    finally:reader.close();store.close()


def test_partial_observation_read_advances_publication_revision_and_recovers(tmp_path,monkeypatch):
    import sqlite3
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    try:
        store.write(home,'first',1,'WebSocket','first',status='completed');reader.poll()
        before=reader.revision;db=reader.db
        for n in range(2,4):store.write(home,f'a{n}',n,'WebSocket',f'r{n}',status='completed')
        monkeypatch.setattr(reader,'READ_BATCH',1)
        class Interrupted:
            def __init__(self):self.batches=0
            def execute(self,sql,*args):
                if 'seq>? AND seq<=?' in sql:
                    self.batches+=1
                    if self.batches==2:raise sqlite3.OperationalError('interrupted observation read')
                return db.execute(sql,*args)
            def close(self):db.close()
        reader.db=Interrupted();reader.poll()
        assert reader.error and reader.catching_up and reader.revision>before
        assert len(reader.activity_records)==2
        reader.db=db;reader.poll()
        assert not reader.error and not reader.catching_up
        assert len(reader.activity_records)==3
    finally:reader.close();store.close()


def test_replaced_observation_sequence_discards_previous_evidence(tmp_path):
    from types import SimpleNamespace
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path)
    home=tmp_path/'home';rows=[{'key':'old-response'}];owner=SimpleNamespace(keys={'old-response'})
    try:
        store.write(home,'old',1,'WebSocket','old-response','old-model','old-model','completed')
        reader.poll();generation=reader.generation
        assert reader.enrich_owned(home_key(home),rows,owner)[0]['wire_observed']
        store.db.execute('DELETE FROM model_observations')
        store.db.execute("DELETE FROM sqlite_sequence WHERE name='model_observations'")
        store.db.commit()
        # A replacement may already have a higher high-water mark than the old reader.
        store.write(home,'new',2,'WebSocket','new-response','new-model','new-model','completed')
        store.write(home,'next',3,'WebSocket','next-response',status='created')
        reader.poll()
        assert reader.generation>generation and reader.offset==2
        assert not reader.enrich_owned(home_key(home),rows,owner)[0]['wire_observed']
        assert reader.enrich(home,[{'key':'new-response'}])[0]['requested_model']=='new-model'
        assert all(attempt!='old' for _,attempt in reader.activity_records)
    finally:reader.close();store.close()


def test_owned_join_replaces_changed_source_history(tmp_path):
    from types import SimpleNamespace
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    owner=SimpleNamespace(keys=set())
    try:
        for n in range(12):
            rows=[{'key':'shared'},{'key':f'current-{n}'}]
            owner.keys={row['key'] for row in rows}
            reader.enrich_owned(home_key(home),rows,owner)
            store.write(home,f'a{n}',n,'WebSocket','shared',status='completed');reader.poll()
            joined=reader.enrich_owned(home_key(home),rows,owner)
            assert [row['key'] for row in joined]==['shared',f'current-{n}']
            assert joined[0]['response_status']=='completed' and joined[1]['response_status']==''
    finally:reader.close();store.close()


def test_owned_join_reuses_sessions_and_refreshes_only_late_evidence(tmp_path):
    from types import SimpleNamespace
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'scan.sqlite');reader=EvidenceReader(store.path)
    home=tmp_path/'home';key=home_key(home)
    rows=[[{'key':f'r{n}'}] for n in range(3)]
    owners=[SimpleNamespace(keys={f'r{n}'}) for n in range(3)]
    try:
        reader.poll()
        first=[reader.enrich_owned(key,row,owner) for row,owner in zip(rows,owners)]
        queries=[];reader.db.set_trace_callback(lambda sql:queries.append(sql) if sql.startswith('SELECT m.*') else None)
        second=[reader.enrich_owned(key,row,owner) for row,owner in zip(rows,owners)]
        assert not queries and all(a is b for a,b in zip(first,second))
        store.write(home,'late',10,'WebSocket','r0','corrected','corrected','completed');reader.poll()
        third=[reader.enrich_owned(key,row,owner) for row,owner in zip(rows,owners)]
        assert len(queries)==1 and third[0][0]['requested_model']=='corrected'
        assert all(third[n] is second[n] for n in (1,2))
    finally:reader.close();store.close()


def test_cancelled_response_releases_tracking_and_replay_keeps_next_request(tmp_path):
    from cachemonitor.model_proxy import Tracker
    store=EvidenceStore(tmp_path/'cancelled.sqlite')
    tracker=Tracker(store, 'home', 'WebSocket', dict(requests=0,responses=0))
    try:
        for index in range(20):
            tracker.request({'model':'asked','stream_id':str(index)})
            tracker.response({'type':'response.cancelled','stream_id':str(index),'response':{'id':f'cancel-{index}'}})
        assert not tracker.active and not tracker.timings
        assert not tracker.pending
        rows=store.db.execute("select status,completed_observed_at from model_observations where status<>'pending'").fetchall()
        assert len(rows)==20 and all(row[0]=='cancelled' and row[1] for row in rows)
        tracker.request({'model':'next'})
        tracker.response({'type':'response.cancelled','response':{'id':'cancel-19'}})
        assert sum(map(len,tracker.pending.values()))==1
        tracker.response({'type':'response.completed','response':{'id':'next'}})
        assert not tracker.active and not tracker.timings
        assert tracker.completed['next']['requested_model']=='next'
    finally:store.close()


def test_default_index_joins_shared_evidence_but_explicit_qa_index_is_isolated(tmp_path,monkeypatch):
    from cachemonitor.model_evidence import default_path
    home,_=fixture_home(tmp_path)
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    index=UsageIndex([home])
    try:assert index.model_evidence.path==default_path()
    finally:index.close()
    index=UsageIndex([home],tmp_path/'qa/index.sqlite')
    try:assert index.model_evidence.path==tmp_path/'qa/model-evidence.sqlite'
    finally:index.close()
    index=UsageIndex([home],tmp_path/'qa/index.sqlite',model_evidence_path=tmp_path/'explicit-wire.sqlite')
    try:assert index.model_evidence.path==tmp_path/'explicit-wire.sqlite'
    finally:index.close()


def test_late_evidence_refreshes_index_and_analysis_without_changing_usage(tmp_path):
    home,_=fixture_home(tmp_path)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    store=EvidenceStore(tmp_path/'model-evidence.sqlite')
    engine=AnalysisEngine()
    try:
        first=finish(index)['sessions']
        assert first[0]['history'][0]['model_match']=='확인 불가'
        queries=[]
        index.model_evidence.db.set_trace_callback(lambda sql:queries.append(sql) if sql.startswith('SELECT m.*') else None)
        assert finish(index)['sessions'][0]['history'] is first[0]['history']
        store.write(home,'unrelated',10000,'WebSocket','other-response','other','other','completed')
        assert finish(index)['sessions'][0]['history'] is first[0]['history']
        assert not queries
        engine.ingest(first)
        before=first[0]['history'][0]
        store.write(home,'attempt',10000,'WebSocket',before['key'],'gpt-6-astra','gpt-5.6-luna','completed')
        second=finish(index,10011)['sessions']
        after=second[0]['history'][0]
        assert second[0]['usage_revision']>first[0]['usage_revision']
        assert engine.ingest(second)
        assert after['model_match']=='불일치'
        assert after['transport']=='WebSocket'
        assert after['transport_source']=='response_id'
        assert before['transport_source']=='log_time'
        for field in ('input','output','cached','key','effort'):
            assert after[field]==before[field]
        assert after['model']=='gpt-6-astra' and after['model_source']=='wire'
        assert not engine.ingest(finish(index,10012)['sessions'])
        # Unknown source homes and legacy IDs never receive somebody else's evidence.
        assert index.model_evidence.enrich(tmp_path/'other',[before])[0]['model_match']=='확인 불가'
    finally:
        index.close();store.close()
    restored=UsageIndex([home],tmp_path/'index.sqlite')
    try: assert finish(restored)['sessions'][0]['history'][0]['model_match']=='불일치'
    finally:restored.close()


def test_owned_evidence_join_refreshes_after_skipped_batches_and_gaps(tmp_path):
    from types import SimpleNamespace
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'owned.sqlite');reader=EvidenceReader(store.path)
    home=tmp_path/'home';key=home_key(home);rows=[{'key':'r'}];owner=SimpleNamespace(keys={'r'})
    try:
        reader.poll();first=reader.enrich_owned(key,rows,owner)
        store.write(home,'late',10,'WebSocket','r','corrected','corrected','completed');reader.poll()
        reader.poll()  # The owner missed the batch carrying its changed response.
        second=reader.enrich_owned(key,rows,owner)
        assert first[0]['requested_model']=='' and second[0]['requested_model']=='corrected'
        store.gap(9,11);reader.poll()
        third=reader.enrich_owned(key,rows,owner)
        assert third[0]['observation_missing'] and not second[0]['observation_missing']
    finally:reader.close();store.close()


def test_transport_wire_evidence_priority_conflict_scope_and_missing_model(tmp_path):
    store=EvidenceStore(tmp_path/'wire.sqlite');reader=EvidenceReader(tmp_path/'wire.sqlite')
    home=tmp_path/'home'
    rows=[{'key':'r','transport':'WebSocket','transport_source':'log_time',
           'transport_endpoint':'/old-path','transport_turn':'old-turn'}]
    try:
        # A response with no model fields still directly proves its wire protocol.
        store.write(home,'a',1,'HTTP/SSE','r',status='created')
        reader.poll();row=reader.enrich(home,rows)[0]
        assert row['transport']=='HTTP/SSE' and row['transport_source']=='response_id'
        assert row['model_match']=='확인 불가'
        assert row['transport_endpoint']==row['transport_turn']==''
        assert reader.enrich(tmp_path/'other',rows)[0]['transport_source']=='log_time'
        assert reader.enrich(home,[{'key':'other'}])[0].get('transport_source') is None
        store.write(home,'b',2,'HTTP/SSE','r',status='completed')
        reader.poll();assert reader.enrich(home,rows)[0]['transport_source']=='response_id'
        store.write(home,'c',3,'WebSocket','r',status='completed')
        reader.poll();row=reader.enrich(home,rows)[0]
        assert row['transport']=='미확인' and row['transport_source']=='conflict'
        # Missing wire data does not promote a time-based guess to direct evidence.
        store.write(home,'d',4,'','empty',status='completed')
        reader.poll()
        assert reader.enrich(home,[{**rows[0],'key':'empty'}])[0]['transport_source']=='log_time'
    finally:reader.close();store.close()


def test_evidence_does_not_persist_untrusted_strings(tmp_path):
    store=EvidenceStore(tmp_path/'e.sqlite')
    store.write(tmp_path,'a',1,'WebSocket','r','model\nAuthorization: SECRET','{"prompt":"PRIVATE"}','arbitrary')
    text=json.dumps(store.db.execute('select * from model_observations').fetchall())
    store.close()
    assert 'SECRET' not in text and 'PRIVATE' not in text


def test_observation_indexes_share_records_without_mutating_previous_values(tmp_path):
    from cachemonitor.model_evidence import home_key
    path=tmp_path/'wire.sqlite';store=EvidenceStore(path);reader=EvidenceReader(path)
    home=tmp_path/'home';key=home_key(home);history=[{'key':'response'}]
    try:
        store.write(home,'attempt',1,'HTTP/SSE','response',status='created')
        reader.poll()
        first=reader.activity_records[(key,'attempt')]
        assert reader.records[(key,'response')]['attempt'] is first
        joined=reader.enrich(home,history)
        joined[0]['response_status']='local change'
        assert first['status']=='created'
        store.write(home,'attempt',2,'HTTP/SSE','response',status='completed')
        reader.poll()
        second=reader.activity_records[(key,'attempt')]
        assert reader.records[(key,'response')]['attempt'] is second
        assert second is not first and first['status']=='created'
        assert second['status']=='completed'
        assert reader.enrich(home,history)[0]['response_status']=='completed'
    finally:reader.close();store.close()


def test_request_tier_is_joined_by_response_id_and_never_replaced_by_response_tier(tmp_path):
    path=tmp_path/'tier.sqlite';store=EvidenceStore(path);reader=EvidenceReader(path)
    home=tmp_path/'home'
    try:
        store.write(home,'a',1,'HTTP/SSE','r','m','m','completed',
                    requested_service_tier='priority',response_service_tier='default')
        reader.poll()
        row=reader.enrich(home,[{'key':'r','service_tier':'미확인'}])[0]
        assert row['service_tier']=='priority' and row['requested_service_tier']=='priority'
        assert row['response_service_tier']=='default' and row['service_tier_source']=='wire'
        assert reader.enrich(tmp_path/'other',[{'key':'r','service_tier':'미확인'}])[0]['service_tier']=='미확인'
        store.write(home,'b',2,'HTTP/SSE','only-response','m','m','completed',response_service_tier='priority')
        reader.poll()
        row=reader.enrich(home,[{'key':'only-response','service_tier':'미확인'}])[0]
        assert row['service_tier']=='미확인'
        store.write(home,'c',3,'HTTP/SSE','r','m','m','completed',requested_service_tier='default')
        reader.poll();row=reader.enrich(home,[{'key':'r'}])[0]
        assert row['service_tier']=='미확인' and row['mode_evidence']=='요청 등급 관측 충돌'
    finally:reader.close();store.close()
