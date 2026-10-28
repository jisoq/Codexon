import json
import time

from cachemonitor.model_evidence import EvidenceStore, EvidenceReader
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.index import UsageIndex
from test_index import finish
from test_core import fixture_home


def test_selected_exact_evidence_does_not_replay_unrelated_history(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor import model_evidence
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    try:
        store.db.executemany('INSERT INTO model_observations '
            '(home,attempt,ts,transport,response_id,requested_model,response_model,status,conflict) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            ((home_key(home),'historical'+str(n),n,'WebSocket','old'+str(n),'old','old','completed',0)
             for n in range(reader.MAX_POLL_ROWS+1)))
        store.write(home,'selected',10000,'WebSocket','selected','gpt-6-astra','gpt-6-astra','completed',
            requested_service_tier='priority',request_observed_at=9999,
            generation_latency_ms=120,completion_latency_ms=450)
        decode=model_evidence.observation_record;decoded=[]
        def selected_only(row):
            assert row['attempt']=='selected','Unrelated historical observation was decoded'
            decoded.append(row['seq'])
            return decode(row)
        monkeypatch.setattr(model_evidence,'observation_record',selected_only)
        reader.poll(historical=False)
        rows=[{'key':'selected','input':123,'cached':100,'output':4}]
        owner=SimpleNamespace(keys={'selected'})
        actual=reader.enrich_owned(home_key(home),rows,owner,capture_proof=True)
        assert reader.offset==0 and reader.catching_up and decoded==[reader.highwater]
        assert not reader.changed_activity and not reader.error
        assert actual[0]['model']=='gpt-6-astra' and actual[0]['service_tier']=='priority'
        assert actual[0]['generation_latency_ms']==120 and actual[0]['completion_latency_ms']==450
        full=[dict(row) for row in reader.db.execute('SELECT * FROM model_observations WHERE seq IN '
            '(SELECT max(seq) FROM model_observations WHERE home=? AND response_id=? GROUP BY attempt)',
            (home_key(home),'selected'))]
        monkeypatch.setattr(reader,'evidence_rows',lambda home,rows:((row,full) for row in rows))
        assert reader.enrich(home,rows)==actual
        assert reader.offset==0
    finally:reader.close();store.close()


def test_live_activity_and_corrections_stay_current_during_historical_replay(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    monkeypatch.setattr(reader,'MAX_POLL_ROWS',2)
    monkeypatch.setattr(reader,'MAX_RECORDS',2)
    rows=[{'key':'live'}];owner=SimpleNamespace(keys={'live'});key=(home_key(home),'current')
    try:
        for n in range(7):store.write(home,'old'+str(n),n,'WebSocket','old'+str(n),status='completed')
        reader.poll(historical=False)
        assert reader.offset==0 and not reader.activity_records
        store.write(home,'current',10,'WebSocket',status='pending',request_observed_at=10)
        reader.poll(historical=False)
        pending=reader.changed_activity[key]
        assert pending['status']=='pending' and not pending['response_id']
        assert not reader.enrich_owned(home_key(home),rows,owner)[0]['wire_observed']
        store.write(home,'current',11,'WebSocket','live','requested',status='created',request_observed_at=10)
        reader.poll(historical=False)
        created=reader.enrich_owned(home_key(home),rows,owner,capture_proof=True)[0]
        assert created['response_status']=='created' and not created['timing_valid']
        store.write(home,'current',12,'WebSocket','live','requested','requested','completed',
            request_observed_at=10,completed_observed_at=12,generation_latency_ms=100,completion_latency_ms=2000)
        reader.poll(historical=False)
        completed=reader.enrich_owned(home_key(home),rows,owner,capture_proof=True)[0]
        assert completed['response_status']=='completed' and completed['completion_latency_ms']==2000
        assert pending['status']=='pending' and created['response_status']=='created'
        assert reader.offset==0
        store.db.execute("UPDATE model_observations SET requested_model='corrected' WHERE seq=?",(reader.highwater,))
        store.db.commit();reader.poll(historical=False)
        corrected=reader.enrich_owned(home_key(home),rows,owner,capture_proof=True)
        assert corrected[0]['requested_model']=='corrected' and reader.changed_activity[key]['requested_model']=='corrected'
        proof=owner._evidence_proof[1]
        store.gap(11,13);reader.poll(historical=False)
        gapped=reader.enrich_owned(home_key(home),rows,owner,capture_proof=True)
        assert gapped[0]['observation_missing'] and owner._evidence_proof[1]!=proof
        deadline=time.monotonic()+10
        while reader.catching_up:
            assert time.monotonic()<deadline
            reader.poll()
            assert not reader.error
            if key in reader.changed_activity:
                assert reader.changed_activity[key]['status']=='completed'
                assert reader.changed_activity[key]['requested_model']=='corrected'
        assert reader.offset==reader.highwater
        assert reader.activity_records[key]['status']=='completed'
        assert reader.records[(key[0],'live')]['current']['requested_model']=='corrected'
        assert reader.enrich_owned(home_key(home),rows,owner,capture_proof=True)==gapped
    finally:reader.close();store.close()


def test_selected_exact_join_covers_unprocessed_live_tail(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.model_evidence import EvidenceProof,home_key
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    monkeypatch.setattr(reader,'MAX_POLL_ROWS',2)
    rows=[{'key':'selected'}];owner=SimpleNamespace(keys={'selected'});key=home_key(home)
    try:
        store.write(home,'current',10,'WebSocket','selected','old','old','completed',
            requested_service_tier='default',generation_latency_ms=10,completion_latency_ms=20)
        reader.poll(historical=False)
        initial=reader.enrich_owned(key,rows,owner,capture_proof=True)
        initial_proof=owner._evidence_proof[1]
        store.write(home,'unrelated-one',11,'WebSocket','unrelated-one',status='completed')
        store.write(home,'unrelated-two',12,'WebSocket','unrelated-two',status='completed')
        store.write(home,'current',13,'WebSocket','selected','current','current','completed',
            requested_service_tier='priority',response_service_tier='priority',
            request_observed_at=12,completed_observed_at=13,generation_latency_ms=120,completion_latency_ms=1000)
        reader.poll(historical=False)
        assert reader.offset==0 and reader.live_offset<reader.highwater
        assert 'selected' not in reader.changed_responses.get(key,set())
        actual=reader.enrich_owned(key,rows,owner,capture_proof=True)
        proof=EvidenceProof(reader.gaps)
        expected=reader.enrich_home(key,rows,proof=proof)
        assert actual==expected
        assert actual[0]['model']=='current' and actual[0]['requested_service_tier']=='priority'
        assert actual[0]['response_service_tier']=='priority'
        assert actual[0]['generation_latency_ms']==120 and actual[0]['completion_latency_ms']==1000
        assert owner._evidence_proof[1]==proof.digest()!=initial_proof
        assert initial[0]['model']=='old' and initial[0]['completion_latency_ms']==20
        assert reader.enrich_owned(key,rows,owner,capture_proof=True) is actual
        assert reader.offset==0 and reader.live_offset<reader.highwater
    finally:reader.close();store.close()


def test_selected_join_created_before_first_poll_does_not_skip_captured_history(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    monkeypatch.setattr(reader,'MAX_POLL_ROWS',2)
    rows=[{'key':'selected'}];owner=SimpleNamespace(keys={'selected'});key=home_key(home)
    try:
        initial=reader.enrich_owned(key,rows,owner,capture_proof=True)
        for number in range(3):store.write(home,str(number),number,'WebSocket',str(number),status='completed')
        store.write(home,'current',4,'WebSocket','selected','current','current','completed')
        reader.poll(historical=False)
        actual=reader.enrich_owned(key,rows,owner,capture_proof=True)
        assert not initial[0]['wire_observed'] and actual[0]['model']=='current'
        assert reader.offset==0
    finally:reader.close();store.close()


def test_selected_join_invalidates_replaced_evidence_without_acknowledging_history(tmp_path):
    from types import SimpleNamespace
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'evidence.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    rows=[{'key':'selected'}];owner=SimpleNamespace(keys={'selected'})
    try:
        store.write(home,'old',1,'WebSocket','selected','old','old','completed')
        reader.poll(historical=False)
        assert reader.enrich_owned(home_key(home),rows,owner,capture_proof=True)[0]['model']=='old'
        proof=owner._evidence_proof[1];generation=reader.generation
        store.db.execute('DELETE FROM model_observations')
        store.db.execute("DELETE FROM sqlite_sequence WHERE name='model_observations'")
        store.write(home,'replacement',2,'WebSocket','selected','new','new','completed')
        store.write(home,'next',3,'WebSocket','other',status='created')
        reader.poll(historical=False)
        assert reader.generation>generation and reader.offset==0
        assert reader.enrich_owned(home_key(home),rows,owner,capture_proof=True)[0]['model']=='new'
        assert owner._evidence_proof[1]!=proof
        assert not reader.activity_records
    finally:reader.close();store.close()


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


def test_corrections_refresh_latest_activity_without_replacing_it_with_older_rows(tmp_path):
    from cachemonitor.model_evidence import home_key, CORRECTION_FIELDS
    store=EvidenceStore(tmp_path/'corrections.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    key=(home_key(home),'attempt')
    try:
        store.write(home,'attempt',1,'WebSocket','r1',status='created')
        store.write(home,'attempt',2,'WebSocket','r1',status='completed')
        store.write(home,'outside',3,'WebSocket','outside',status='completed')
        reader.poll();generation=reader.generation;published=reader.activity_records[key]
        store.db.execute("UPDATE model_observations SET status='failed',completed_observed_at=2.5 WHERE seq=2")
        store.write(home,'next',4,'WebSocket','next',status='completed')
        reader.poll()
        assert reader.generation==generation and published['status']=='completed'
        assert reader.changed_activity[key]['status']=='failed'
        assert reader.changed_activity[key]['completed_observed_at']==2.5
        assert reader.records[(key[0],'r1')]['attempt'] is reader.activity_records[key]
        assert not set(CORRECTION_FIELDS)&reader.activity_records[key].keys()
        store.db.execute("UPDATE model_observations SET status='incomplete' WHERE seq=1");store.db.commit()
        reader.poll()
        assert key not in reader.changed_activity and reader.activity_records[key]['status']=='failed'
        # Older producers retain their table allowlist and named INSERT contract.
        assert {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}=={
            'model_observations','observation_gaps','sqlite_sequence'}
        before=store.db.total_changes
        store.write(home,'legacy',5,'WebSocket','legacy',status='completed')
        assert store.db.total_changes-before==1
    finally:reader.close();store.close()


def test_correction_window_and_structural_edits_replay_activity_without_stale_keys(tmp_path,monkeypatch):
    from cachemonitor import model_evidence
    from cachemonitor.model_evidence import home_key
    monkeypatch.setattr(model_evidence,'CORRECTION_ROWS',3)
    store=EvidenceStore(tmp_path/'bounded.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home';key=home_key(home)
    try:
        for n in range(8):store.write(home,f'a{n}',n,'WebSocket',f'r{n}',status='completed')
        reader.poll();generation=reader.generation
        for seq in range(1,5):store.db.execute("UPDATE model_observations SET requested_model='corrected' WHERE seq=?",(seq,))
        store.db.commit();reader.poll()
        assert reader.generation>generation
        assert store.db.execute('SELECT count(*) FROM model_observations WHERE correction_revision>0').fetchone()[0]<=3
        assert all(reader.activity_records[(key,f'a{n}')]['requested_model']=='corrected' for n in range(4))
        store.db.execute("UPDATE model_observations SET home='other-home',response_id='moved' WHERE seq=2")
        store.db.commit();reader.poll()
        assert (key,'a1') not in reader.activity_records and ('other-home','a1') in reader.activity_records
        store.db.execute('DELETE FROM model_observations WHERE seq=3');store.db.commit();reader.poll()
        assert (key,'a2') not in reader.activity_records
        store.db.execute("UPDATE model_observations SET attempt='moved-attempt' WHERE seq=5")
        store.db.commit();reader.poll()
        assert (key,'a4') not in reader.activity_records and (key,'moved-attempt') in reader.activity_records
        store.db.execute("UPDATE model_observations SET requested_model='latest-stamp' WHERE seq=4")
        store.db.commit();reader.poll();previous=reader.correction_revision
        store.db.execute('PRAGMA recursive_triggers=OFF')
        store.db.execute('INSERT OR REPLACE INTO model_observations '
            '(seq,home,attempt,ts,transport,response_id,requested_model,response_model,status,conflict) '
            "VALUES(4,?,'replacement',4,'WebSocket','replacement','new','new','completed',0)",(key,))
        store.db.commit();reader.poll()
        assert reader.correction_revision>previous and (key,'a3') not in reader.activity_records
        assert reader.activity_records[(key,'replacement')]['requested_model']=='new'
        # Raw row restoration must not reinstall a deleted source's seed.
        saved=store.db.execute('SELECT * FROM model_observations ORDER BY seq').fetchall()
        columns=[row[1] for row in store.db.execute('PRAGMA table_info(model_observations)')]
        changed=list(saved[0]);changed[columns.index('requested_model')]='restored'
        saved[0]=tuple(changed);epoch=reader.correction_epoch;generation=reader.generation
        store.db.execute('DELETE FROM model_observations')
        store.db.executemany('INSERT INTO model_observations VALUES('+','.join('?' for _ in columns)+')',saved)
        store.write(home,'restored-next',9,'WebSocket','restored-next',status='completed');reader.poll()
        assert reader.correction_epoch!=epoch and reader.generation>generation
        assert reader.activity_records[(key,'a0')]['requested_model']=='restored'
        store.db.execute("UPDATE model_observations SET requested_model='first-stamp' WHERE seq=1")
        store.db.execute("UPDATE model_observations SET requested_model='highest-stamp' WHERE seq=2")
        store.db.commit();reader.poll();epoch=reader.correction_epoch
        store.db.execute('UPDATE OR REPLACE model_observations SET seq=2 WHERE seq=1')
        store.db.commit();reader.poll()
        assert reader.correction_epoch!=epoch and ('other-home','a1') not in reader.activity_records
        assert reader.activity_records[(key,'a0')]['seq']==2
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
    store.write(tmp_path,'a',1,'WebSocket','r','model\nAuthorization: SECRET','{"prompt":"PRIVATE"}','arbitrary',
        requested_effort='PRIVATE prompt',response_effort='PRIVATE\noutput')
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


def test_effort_sides_keep_wire_and_context_provenance_independent(tmp_path):
    store=EvidenceStore(tmp_path/'effort.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    try:
        store.write(home,'wire',1,'HTTP/SSE','wire','requested','actual','completed',
            requested_effort='high',response_effort='low')
        store.write(home,'absent',2,'HTTP/SSE','absent','requested','actual','completed')
        reader.poll()
        wire,absent=reader.enrich(home,[dict(key='wire',effort='medium'),dict(key='absent',effort='medium')])
        assert wire['requested_effort']==wire['effort']=='high'
        assert wire['requested_effort_source']=='wire' and wire['configured_effort']=='medium'
        assert wire['response_effort']=='low' and wire['response_effort_source']=='wire'
        assert wire['response_effort_confirmed'] and wire['model_alert_confirmed']
        assert absent['requested_effort']==absent['effort']=='medium'
        assert absent['requested_effort_source']=='turn_context'
        assert absent['response_effort']=='' and absent['response_effort_source']=='unknown'
        assert not absent['response_effort_confirmed'] and absent['model_alert_confirmed']
        assert reader.enrich(home,[wire])[0]['configured_effort']=='medium'
    finally:reader.close();store.close()


def test_effort_conflicts_are_independent_and_never_revoke_model_confirmation(tmp_path):
    store=EvidenceStore(tmp_path/'conflicts.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    try:
        cases=[('request-values',dict(requested_effort='high',response_effort='low'),
                 dict(requested_effort='medium',response_effort='low')),
               ('request-flag',dict(requested_effort_conflict=True,response_effort='low'),{}),
               ('response-values',dict(requested_effort='high',response_effort='low'),
                 dict(requested_effort='high',response_effort='medium')),
               ('response-flag',dict(requested_effort='high',response_effort='low',response_effort_conflict=True),{})]
        for key,first,second in cases:
            store.write(home,key+'-a',1,'WebSocket',key,'requested','actual','completed',**first)
            if second:store.write(home,key+'-b',2,'WebSocket',key,'requested','actual','completed',**second)
        reader.poll()
        for key,_,_ in cases:
            row=reader.enrich(home,[dict(key=key,effort='medium')])[0]
            assert row['model_alert_confirmed'] and row['model_match']=='불일치'
            if key.startswith('request'):
                assert row['requested_effort_conflict'] and row['requested_effort_source']=='conflict'
                assert row['requested_effort']=='' and row['effort']=='미확인'
                assert not row['response_effort_conflict'] and row['response_effort_confirmed']
            else:
                assert row['response_effort_conflict'] and row['response_effort_source']=='conflict'
                assert row['response_effort']=='' and not row['response_effort_confirmed']
                assert not row['requested_effort_conflict'] and row['requested_effort']=='high'
    finally:reader.close();store.close()


def test_response_effort_requires_completed_join_without_observation_gaps(tmp_path):
    store=EvidenceStore(tmp_path/'unconfirmed.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    try:
        for key,requested,status,missing in [('pending','requested','created',False),
                ('unpaired','','completed',False),('gap','requested','completed',True)]:
            store.write(home,key,1,'HTTP/SSE',key,requested,'actual',status,
                response_effort='low',observation_missing=missing)
        reader.poll()
        for key in ('pending','unpaired','gap'):
            row=reader.enrich(home,[dict(key=key,effort='high')])[0]
            assert row['response_effort']=='low' and row['response_effort_source']=='wire'
            assert not row['response_effort_confirmed']
    finally:reader.close();store.close()


def test_effort_legacy_read_defaults_and_additive_upgrade(tmp_path):
    import sqlite3
    from cachemonitor.model_evidence import home_key
    path=tmp_path/'legacy.sqlite';home=tmp_path/'home'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE model_observations(seq INTEGER PRIMARY KEY AUTOINCREMENT,home TEXT NOT NULL,'
            'attempt TEXT NOT NULL,ts REAL NOT NULL,transport TEXT NOT NULL,response_id TEXT NOT NULL,'
            'requested_model TEXT NOT NULL,response_model TEXT NOT NULL,status TEXT NOT NULL,conflict INTEGER NOT NULL)')
        db.execute('INSERT INTO model_observations(home,attempt,ts,transport,response_id,requested_model,response_model,status,conflict) '
            'VALUES(?,?,?,?,?,?,?,?,?)',(home_key(home),'a',1,'HTTP/SSE','r','requested','actual','completed',0))
    reader=EvidenceReader(path)
    try:
        reader.poll();before=reader.enrich(home,[dict(key='r',effort='medium')])[0]
        assert not reader.error and before['requested_effort_source']=='turn_context'
        assert before['response_effort']=='' and not before['response_effort_confirmed']
    finally:reader.close()
    store=EvidenceStore(path);reader=EvidenceReader(path)
    try:
        reader.poll();assert reader.enrich(home,[dict(key='r',effort='medium')])[0]==before
        store.db.execute('INSERT INTO model_observations(home,attempt,ts,transport,response_id,requested_model,response_model,status,conflict) '
            'VALUES(?,?,?,?,?,?,?,?,?)',(home_key(home),'old-writer',2,'HTTP/SSE','old','requested','actual','completed',0))
        store.db.commit();reader.poll()
        old=reader.enrich(home,[dict(key='old',effort='medium')])[0]
        assert old['requested_effort']=='medium' and old['response_effort']==''
        store.write(home,'new',3,'HTTP/SSE','new','requested','actual','completed',requested_effort='high',response_effort='low')
        reader.poll();assert reader.enrich(home,[dict(key='new')])[0]['response_effort_confirmed']
    finally:reader.close();store.close()


def test_bounded_effort_witnesses_preserve_middle_conflicts_and_missing_values(tmp_path,monkeypatch):
    from cachemonitor.model_evidence import home_key
    store=EvidenceStore(tmp_path/'bounded.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    cases=('request-values','response-values','response-flag','absent-response')
    try:
        for key in cases:
            for n in range(129):
                efforts=dict(requested_effort='high',response_effort='low')
                if n==63:
                    if key=='request-values':efforts['requested_effort']='medium'
                    elif key=='response-values':efforts['response_effort']='medium'
                    elif key=='response-flag':efforts['response_effort_conflict']=True
                    else:efforts['response_effort']=''
                store.write(home,key+str(n),n,'HTTP/SSE',key,'requested','actual','completed',**efforts)
        reader.poll();rows=[dict(key=key,effort='medium') for key in cases]
        assert all(len(selected)<=48 for _,selected in reader.evidence_rows(home_key(home),rows))
        actual=reader.enrich(home,rows)
        assert actual[0]['requested_effort_conflict'] and actual[0]['response_effort_confirmed']
        assert actual[1]['response_effort_conflict'] and actual[2]['response_effort_conflict']
        assert not actual[3]['response_effort_conflict'] and not actual[3]['response_effort_confirmed']
        assert all(row['model_alert_confirmed'] for row in actual)
        full={key:[dict(row) for row in reader.db.execute('SELECT * FROM model_observations WHERE response_id=?',(key,))]
              for key in cases}
        monkeypatch.setattr(reader,'evidence_rows',lambda home,rows:((row,full[row['key']]) for row in rows))
        assert reader.enrich(home,rows)==actual
    finally:reader.close();store.close()


def test_effort_only_correction_refreshes_index_analysis_and_restart(tmp_path):
    home,_=fixture_home(tmp_path);path=tmp_path/'index.sqlite'
    index=UsageIndex([home],path);store=EvidenceStore(tmp_path/'model-evidence.sqlite');engine=AnalysisEngine()
    try:
        initial=finish(index)['sessions'];key=initial[0]['history'][0]['key']
        store.write(home,'attempt',10000,'HTTP/SSE',key,'gpt-6-astra','gpt-5.6-luna','completed',
            requested_effort='high',response_effort='low')
        first=finish(index,10011)['sessions'];assert engine.ingest(first)
        store.db.execute("UPDATE model_observations SET response_effort='medium' WHERE response_id=?",(key,))
        store.db.commit()
        second=finish(index,10012)['sessions']
        assert second[0]['usage_revision']>first[0]['usage_revision'] and engine.ingest(second)
        assert first[0]['history'][0]['response_effort']=='low'
        assert second[0]['history'][0]['response_effort']=='medium'
        assert second[0]['history'][0]['model_alert_confirmed']
        assert second[0]['history'][0]['effort']=='high'
    finally:index.close();store.close()
    restored=UsageIndex([home],path)
    try:
        row=finish(restored,10013)['sessions'][0]['history'][0]
        assert row['response_effort']=='medium' and row['response_effort_confirmed']
    finally:restored.close()


def test_tracker_keeps_effort_when_events_omit_it_and_isolates_replay_conflicts(tmp_path):
    from cachemonitor.model_proxy import Tracker
    store=EvidenceStore(tmp_path/'tracker.sqlite');reader=EvidenceReader(store.path);home=tmp_path/'home'
    tracker=Tracker(store,home,'WebSocket',dict(requests=0,responses=0,storage_errors=0))
    try:
        tracker.request(dict(model='requested',reasoning=dict(effort='high')))
        tracker.response(dict(type='response.created',response=dict(id='r',model='actual',reasoning=dict(effort='low'))))
        tracker.response(dict(type='response.completed',response=dict(id='r',model='actual')))
        reader.poll();first=reader.enrich(home,[dict(key='r')])[0]
        assert first['requested_effort']=='high' and first['response_effort']=='low' and first['response_effort_confirmed']
        tracker.request(dict(model='next',reasoning=dict(effort='medium')))
        tracker.response(dict(type='response.completed',response=dict(id='r',model='actual',reasoning=dict(effort='medium'))))
        reader.poll();second=reader.enrich(home,[dict(key='r')])[0]
        assert second['response_effort_conflict'] and not second['requested_effort_conflict']
        assert second['model_alert_confirmed'] and not second['response_effort_confirmed']
        assert sum(map(len,tracker.pending.values()))==1
        assert not tracker.completed['r']['conflict']
    finally:reader.close();store.close()
