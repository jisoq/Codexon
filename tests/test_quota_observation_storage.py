import copy
import hashlib
import json
import sqlite3
import pytest

from cachemonitor import quota_payloads, quota_tracking_store as tracking
from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor.quota_view import history_rows


def observation(at=100,used=10,**extra):
    return dict(observed_at=at,source='live',account='a',plan_type='pro',bucket='codex',
                windows={'weekly':dict(used_percent=used,resets_at=100000,window_minutes=10080)},**extra)


def legacy_observe(ledger,home,quota):
    for name,window in quota['windows'].items():
        identifier=hashlib.sha256(json.dumps([home,quota['observed_at'],quota.get('source'),
            quota.get('account'),quota.get('plan_type'),quota.get('bucket'),name,window],sort_keys=True).encode()).hexdigest()
        ledger.db.execute('insert or ignore into window_observations values(?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifier,home,quota['observed_at'],quota.get('source','local'),quota.get('account',''),
             quota.get('plan_type') or '',quota.get('bucket') or ('codex' if quota.get('source')=='live' else ''),
             name,window.get('used_percent'),window.get('resets_at'),window.get('window_minutes'),
             json.dumps(quota.get('separate_models',[]))))
    ledger.observe(home,quota)


def test_weekly_references_preserve_conflicting_timestamp_and_first_window_payload(tmp_path):
    current=QuotaLedger(tmp_path/'current.sqlite');legacy=QuotaLedger(tmp_path/'legacy.sqlite')
    values=[observation(100,10),observation(100,13),observation(103,14),
            observation(103,14,separate_models=['gpt-6-astra'])]
    local=observation(106,15)
    for key in ('source','account','plan_type','bucket'):local.pop(key)
    values.append(local)
    try:
        for value in values:
            current.observe('h',value);legacy_observe(legacy,'h',value)
        actual=current.report('h',200);expected=legacy.report('h',200)
        assert actual==expected
        assert history_rows(actual,'weekly')==history_rows(expected,'weekly')
        assert current.db.execute('select count(*) from window_observations').fetchone()[0]==0
        assert current.db.execute('select count(*) from observations').fetchone()[0]==5
        assert current.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==4
        assert next(row for row in actual['history'] if row['at']==103)['separate']=='[]'

        # An older writer can add a full copy later; the first reference still wins.
        legacy_observe(current,'h',values[3])
        assert current.report('h',200)==actual
    finally:current.close();legacy.close()


def test_existing_weekly_rows_remain_unchanged_across_reopen_and_new_observations(tmp_path):
    path=tmp_path/'mixed.sqlite';ledger=QuotaLedger(path)
    first=observation()
    legacy_observe(ledger,'h',first)
    original=[tuple(row) for row in ledger.db.execute('select * from window_observations')]
    initial=ledger.report('h',200);ledger.close()
    ledger=QuotaLedger(path)
    try:
        assert ledger.report('h',200)==initial
        ledger.observe('h',first)
        assert ledger.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==0
        ledger.observe('h',observation(110,11))
        assert [tuple(row) for row in ledger.db.execute('select * from window_observations')]==original
        history=ledger.report('h',200)['history']
        assert len(history)==2 and history[0]==initial['history'][0]
        assert ledger.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==1
    finally:ledger.close()


def test_invalid_raw_values_and_incomplete_windows_remain_recoverable(tmp_path):
    ledger=QuotaLedger(tmp_path/'raw.sqlite');legacy=QuotaLedger(tmp_path/'legacy.sqlite')
    try:
        for index,value in enumerate((None,-1,101,float('nan'),float('inf'),'invalid')):
            quota=observation(100+index,value)
            ledger.observe('h',quota);legacy_observe(legacy,'h',quota)
        assert ledger.report('h',200)==legacy.report('h',200)
        assert ledger.db.execute('select count(*) from observations').fetchone()[0]==6
        assert ledger.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==6
        for index,window in enumerate(({},dict(used_percent=20),dict(window_minutes=10080))):
            quota=observation(120+index);quota['windows']['weekly']=window
            ledger.observe('h',quota)
        five=observation(130)
        five['windows']={'five_hour':dict(used_percent=50,resets_at=2000,window_minutes=300)}
        ledger.observe('h',five)
        history=ledger.report('h',200)['history']
        assert len(history)==10
        assert [(row['used'],row['minutes']) for row in history if 120<=row['at']<=122]==[
            (None,None),(20,None),(None,10080)]
        assert history[-1]['window']=='five_hour'
        assert ledger.db.execute('select count(*) from window_observations').fetchone()[0]==4
        ledger.close();ledger=QuotaLedger(tmp_path/'raw.sqlite')
        assert ledger.report('h',200)['history']==history
    finally:ledger.close();legacy.close()


def test_weekly_reference_and_canonical_payload_roll_back_together(tmp_path):
    ledger=QuotaLedger(tmp_path/'rollback.sqlite')
    try:
        ledger.observe('h',observation(),commit=False)
        assert ledger.db.in_transaction
        assert ledger.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==1
        ledger.db.rollback()
        for table in ('observations','observation_context','weekly_window_refs'):
            assert ledger.db.execute('select count(*) from '+table).fetchone()[0]==0
        ledger.observe('h',observation())
        expected=ledger.report('h',200)
        ledger.close();ledger=QuotaLedger(tmp_path/'rollback.sqlite')
        assert ledger.report('h',200)==expected
        assert ledger.db.execute('''select count(*) from weekly_window_refs r
            left join observations o on o.id=r.observation_id where o.id is null''').fetchone()[0]==0
    finally:ledger.close()


def test_replayed_observation_is_noop_and_context_only_fills_missing_fields(tmp_path):
    ledger=QuotaLedger(tmp_path/'replay.sqlite')
    watcher=sqlite3.connect(ledger.path)
    quota=observation()
    def version():return watcher.execute('pragma data_version').fetchone()[0]
    try:
        ledger.observe('h',quota)
        changes=ledger.db.total_changes;before=version()
        ledger.observe('h',copy.deepcopy(quota))
        assert ledger.db.total_changes==changes and version()==before
        ledger.db.execute("update observation_context set plan='',bucket='existing'");ledger.db.commit()
        changes=ledger.db.total_changes;before=version()
        revision=ledger.db.execute('select value from report_revision where id=1').fetchone()[0]
        ledger.observe('h',quota)
        assert tuple(ledger.db.execute('select plan,bucket from observation_context').fetchone())==('pro','existing')
        # One context correction, its revision, and one bounded change-journal row.
        assert ledger.db.total_changes==changes+3 and version()!=before
        assert ledger.db.execute('select value from report_revision where id=1').fetchone()[0]==revision+1
        assert ledger.db.execute('select table_name from report_changes where revision=?',(revision+1,)).fetchone()[0]=='observation_context'
        changes=ledger.db.total_changes;before=version()
        ledger.observe('h',quota)
        assert ledger.db.total_changes==changes and version()==before
    finally:watcher.close();ledger.close()


def test_tracking_payloads_preserve_full_history_and_legacy_rows(tmp_path):
    path=tmp_path/'payloads.sqlite'
    ledger=QuotaLedger(path);reference=QuotaLedger(tmp_path/'plain.sqlite')
    values=[observation(100.+i*10,10+i,requested_at=99.+i*10,elapsed=1.,
        credits={'available':True,'balance':12.345},
        reset_credits=[{'status':'available','expires_at':2000,'count':20}],
        unknown={'nested':[None,False,'한글',{'value':-0.0}]}) for i in range(3)]
    expected=copy.deepcopy(values)
    try:
        for store in (ledger,reference):tracking.enable(store.db,'h',90)
        for i,value in enumerate(values):
            for store in (ledger,reference):
                store.observe('h',value,commit=False)
                if store is ledger and i:
                    tracking.observe(store.db,'h',value)
                else:
                    store.db.execute('INSERT INTO tracking_observations(home,requested,received,data) VALUES(?,?,?,?)',
                        ('h',value['requested_at'],value['observed_at'],json.dumps(value)))
                    store.db.commit()
        assert values==expected
        assert ledger.report('h',200)==reference.report('h',200)
        assert ledger.db.execute('SELECT count(*) FROM tracking_payload_contexts').fetchone()[0]==1
        rows=list(ledger.db.execute('SELECT * FROM tracking_observations ORDER BY seq'))
        assert isinstance(rows[0]['data'],str) and all(isinstance(row['data'],bytes) for row in rows[1:])
        ledger.close()
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            reader=quota_payloads.PayloadReader(db)
            rows=list(db.execute('SELECT * FROM tracking_observations ORDER BY seq'))
            assert [reader.decode(row) for row in rows]==expected
            reader.decode(rows[1])['reset_credits'][0]['count']=999
            assert reader.decode(rows[2])==expected[2]
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        # Reading a database without the new column/table must not initialize it.
        with sqlite3.connect(':memory:') as db:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE tracking_observations(seq INTEGER,data TEXT)')
            db.execute('INSERT INTO tracking_observations VALUES(1,?)',(json.dumps(expected[0]),))
            db.execute('PRAGMA query_only=ON')
            assert quota_payloads.PayloadReader(db).decode(db.execute('SELECT * FROM tracking_observations').fetchone())==expected[0]
    finally:ledger.close();reference.close()


def test_tracking_payload_timestamp_references_preserve_numeric_types(tmp_path):
    ledger=QuotaLedger(tmp_path/'timestamps.sqlite')
    cases=[(100,100.25),(100.,100.25),(-0.0,100.25),(2**53+1,100.25),
           (None,100.25),(False,100.25),(float('nan'),100.25),(float('inf'),100.25)]
    try:
        for requested,received in cases:
            value=observation(received,10,requested_at=requested,reset_credits=[{'count':20}]*8)
            data,context=quota_payloads.encode(ledger.db,value,requested,received)
            assert context is not None
            ledger.db.execute('INSERT INTO tracking_observations(home,requested,received,data,payload_context) VALUES(?,?,?,?,?)',
                ('h',requested,received,data,context))
            row=ledger.db.execute('SELECT * FROM tracking_observations ORDER BY seq DESC LIMIT 1').fetchone()
            restored=quota_payloads.PayloadReader(ledger.db).decode(row)
            assert json.dumps(restored,sort_keys=True)==json.dumps(value,sort_keys=True)
    finally:ledger.close()


def test_tracking_payload_contexts_are_atomic_and_revision_tracks_reference_edits(tmp_path):
    ledger=QuotaLedger(tmp_path/'atomic-payload.sqlite')
    value=observation(100.,10,requested_at=99.,reset_credits=[{'count':20}]*8)
    try:
        tracking.enable(ledger.db,'h',90)
        ledger.observe('h',value,commit=False)
        data,context=quota_payloads.encode(ledger.db,value,99.,100.)
        assert context is not None and ledger.db.in_transaction
        ledger.db.execute('INSERT INTO tracking_observations(home,requested,received,data,payload_context) VALUES(?,?,?,?,?)',
            ('h',99.,100.,data,context))
        ledger.db.rollback()
        for table in ('observations','observation_context','weekly_window_refs','tracking_observations','tracking_payload_contexts'):
            assert ledger.db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        ledger.observe('h',value,commit=False);tracking.observe(ledger.db,'h',value)
        with pytest.raises(sqlite3.IntegrityError):ledger.db.execute('UPDATE tracking_payload_contexts SET data=?',(b'bad',))
        with pytest.raises(sqlite3.IntegrityError):ledger.db.execute('DELETE FROM tracking_payload_contexts')
        ledger.db.rollback()
        # Simulate the existing trigger predating the added reference column.
        sql=ledger.db.execute("SELECT sql FROM sqlite_master WHERE name='report_tracking_observations_update'").fetchone()[0]
        ledger.db.execute('DROP TRIGGER report_tracking_observations_update')
        ledger.db.execute(sql.replace(' OR old."payload_context" IS NOT new."payload_context"',''))
        ledger.db.commit();ledger.close();ledger=QuotaLedger(tmp_path/'atomic-payload.sqlite')
        before=ledger.db.execute('SELECT value FROM report_revision').fetchone()[0]
        other={**value,'account':'another-account'}
        other_data,other_context=quota_payloads.encode(ledger.db,other,99.,100.)
        row=ledger.db.execute('SELECT * FROM tracking_observations').fetchone()
        assert other_data==row['data'] and other_context!=row['payload_context']
        ledger.db.execute('UPDATE tracking_observations SET payload_context=?',(other_context,));ledger.db.commit()
        assert ledger.db.execute('SELECT value FROM report_revision').fetchone()[0]==before+1
        assert tracking.build(ledger.db,'h',200,context_only=True)['account']=='another-account'
    finally:ledger.close()


def test_tracking_payload_limits_retain_plain_data_and_bound_context_reuse(tmp_path,monkeypatch):
    ledger=QuotaLedger(tmp_path/'payload-limits.sqlite')
    try:
        for value in ({'small':True},{'large':'x'*(quota_payloads.MAX_PAYLOAD_BYTES+1)}):
            data,context=quota_payloads.encode(ledger.db,value,1,2)
            assert context is None and json.loads(data)==value
        assert ledger.db.execute('SELECT count(*) FROM tracking_payload_contexts').fetchone()[0]==0
        monkeypatch.setattr(quota_payloads,'CONTEXT_CACHE_ENTRIES',2)
        monkeypatch.setattr(quota_payloads,'CONTEXT_CACHE_BYTES',2048)
        reader=quota_payloads.PayloadReader(ledger.db)
        rows=[]
        for i in range(5):
            value=observation(100.+i,10,reset_credits=[{'count':20}]*8)
            value['account']=str(i)
            data,context=quota_payloads.encode(ledger.db,value,100.+i,100.+i)
            assert context is not None
            row=dict(data=data,payload_context=context,requested=100.+i,received=100.+i)
            rows.append((row,value))
            assert reader.decode(row)==value
            assert len(reader.contexts)<=2 and reader.context_bytes<=2048
        assert reader.decode(rows[0][0])==rows[0][1]
        row=dict(rows[0][0]);row['data']=b'Q1[]'
        with pytest.raises(ValueError):reader.decode(row)
        row=dict(rows[0][0]);row['payload_context']=999
        with pytest.raises(ValueError):reader.decode(row)
    finally:ledger.close()


def _legacy_tracking_file(path,values):
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE tracking_observations(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            home TEXT,requested REAL,received REAL,data TEXT)''')
        db.executemany('INSERT INTO tracking_observations VALUES(?,?,?,?,?)',
            ((10+2*i,'h',value['observed_at']-1,value['observed_at'],json.dumps(value)) for i,value in enumerate(values)))


def test_tracking_migration_resumes_without_replaying_or_changing_records(tmp_path,monkeypatch):
    monkeypatch.setattr(quota_payloads,'MIGRATION_ROWS',2)
    monkeypatch.setattr(quota_payloads,'MIGRATION_PREPARE_SECONDS',1.)
    values=[observation(100.+i,10+i,requested_at=99.+i,reset_credits=[{'count':20}]*8) for i in range(7)]
    path=tmp_path/'legacy-tracking.sqlite';_legacy_tracking_file(path,values)
    ledger=QuotaLedger(path)
    try:
        tracking.enable(ledger.db,'h',90)
        first=ledger.report('h',200)
        revision=ledger.db.execute('SELECT value FROM report_revision').fetchone()[0]
        replayed=[];observe_state=tracking.TrackingState.observe
        monkeypatch.setattr(tracking.TrackingState,'observe',lambda state,value:(replayed.append(value.sequence),observe_state(state,value))[1])
        assert ledger.db.execute("SELECT count(*) FROM tracking_observations WHERE typeof(data)='blob'").fetchone()[0]==2
        assert ledger._payload_migration.step()==2
        assert ledger.report('h',200)==first and not replayed
        assert ledger.db.execute('SELECT value FROM report_revision').fetchone()[0]==revision
        ledger.close();ledger=QuotaLedger(path)
        assert ledger.db.execute("SELECT count(*) FROM tracking_observations WHERE typeof(data)='blob'").fetchone()[0]==6
        assert ledger._payload_migration.step()==1
        assert not ledger._payload_migration.pending and ledger._payload_migration.step()==0
        assert ledger.db.execute('SELECT value FROM report_revision').fetchone()[0]==revision
        reader=quota_payloads.PayloadReader(ledger.db)
        rows=list(ledger.db.execute('SELECT * FROM tracking_observations ORDER BY seq'))
        assert [row['seq'] for row in rows]==list(range(10,24,2))
        assert [reader.decode(row) for row in rows]==values
        assert ledger.db.execute('SELECT active,done FROM tracking_payload_migration').fetchone()[:]==(0,1)
        tracking.observe(ledger.db,'h',observation(210.,20,requested_at=209.))
        assert ledger.db.execute('SELECT max(seq) FROM tracking_observations').fetchone()[0]==23
        assert ledger.db.execute('SELECT value FROM report_revision').fetchone()[0]==revision+1
    finally:ledger.close()


def test_tracking_migration_skips_oversized_and_invalid_payloads_without_losing_later_rows(tmp_path):
    values=[observation(100.+i,10+i,reset_credits=[{'count':20}]*8) for i in range(3)]
    values[1]['large']='x'*(quota_payloads.MIGRATION_ROW_BYTES+32)
    path=tmp_path/'large-tracking.sqlite';_legacy_tracking_file(path,values)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO tracking_observations VALUES(30,'h',200,201,'invalid-json')")
        original=db.execute('SELECT data FROM tracking_observations WHERE seq=12').fetchone()[0]
    ledger=QuotaLedger(path)
    try:
        assert not ledger._payload_migration.pending
        rows=list(ledger.db.execute('SELECT * FROM tracking_observations ORDER BY seq'))
        reader=quota_payloads.PayloadReader(ledger.db)
        assert [reader.decode(row) for row in rows[:3]]==values
        assert isinstance(rows[0]['data'],bytes) and isinstance(rows[2]['data'],bytes)
        assert rows[1]['data']==original and rows[3]['data']=='invalid-json'
        assert ledger.db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)>quota_payloads.MIGRATION_ROW_BYTES
        assert ledger.db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:ledger.close()


def test_tracking_migration_rolls_back_repacking_and_can_resume(tmp_path,monkeypatch):
    monkeypatch.setattr(quota_payloads,'MIGRATION_ROWS',2)
    monkeypatch.setattr(quota_payloads,'MIGRATION_PREPARE_SECONDS',1.)
    values=[observation(100.+i,10+i,reset_credits=[{'count':20}]*8) for i in range(5)]
    path=tmp_path/'rollback-tracking.sqlite';_legacy_tracking_file(path,values)
    ledger=QuotaLedger(path)
    try:
        before=[tuple(row) for row in ledger.db.execute('SELECT * FROM tracking_observations ORDER BY seq')]
        progress=tuple(ledger.db.execute('SELECT * FROM tracking_payload_migration').fetchone())
        contexts=[tuple(row) for row in ledger.db.execute('SELECT * FROM tracking_payload_contexts')]
        revision=ledger.db.execute('SELECT value FROM report_revision').fetchone()[0]
        ledger.db.execute("CREATE TRIGGER fail_repack BEFORE INSERT ON tracking_observations WHEN NEW.seq=14 BEGIN SELECT RAISE(ABORT,'storage failure'); END")
        assert ledger._payload_migration.step()==0 and not ledger._payload_migration.pending
        assert [tuple(row) for row in ledger.db.execute('SELECT * FROM tracking_observations ORDER BY seq')]==before
        assert tuple(ledger.db.execute('SELECT * FROM tracking_payload_migration').fetchone())==progress
        assert [tuple(row) for row in ledger.db.execute('SELECT * FROM tracking_payload_contexts')]==contexts
        assert ledger.db.execute('SELECT value FROM report_revision').fetchone()[0]==revision
        ledger.db.execute('DROP TRIGGER fail_repack');ledger.close();ledger=QuotaLedger(path)
        assert ledger._payload_migration.step()==1 and not ledger._payload_migration.pending
        reader=quota_payloads.PayloadReader(ledger.db)
        assert [reader.decode(row) for row in ledger.db.execute('SELECT * FROM tracking_observations ORDER BY seq')]==values
    finally:ledger.close()


def test_tracking_migration_yields_to_space_pressure_locks_and_pinned_wal(tmp_path,monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(quota_payloads,'MIGRATION_ROWS',2)
    monkeypatch.setattr(quota_payloads,'MIGRATION_PREPARE_SECONDS',1.)
    values=[observation(100.+i,10+i,reset_credits=[{'count':20}]*8) for i in range(7)]
    path=tmp_path/'pressure-tracking.sqlite';_legacy_tracking_file(path,values)
    ledger=QuotaLedger(path);other=sqlite3.connect(path)
    try:
        progress=tuple(ledger.db.execute('SELECT * FROM tracking_payload_migration').fetchone())
        timeout=ledger.db.execute('PRAGMA busy_timeout').fetchone()[0]
        with monkeypatch.context() as patch:
            patch.setattr(quota_payloads.shutil,'disk_usage',lambda _:SimpleNamespace(free=1))
            assert ledger._payload_migration.step()==0 and ledger._payload_migration.pending
        other.execute('BEGIN IMMEDIATE')
        assert ledger._payload_migration.step()==0 and ledger._payload_migration.pending
        other.rollback()
        other.execute('BEGIN');other.execute('SELECT count(*) FROM tracking_observations').fetchone()
        ledger.db.execute("INSERT INTO tracking_health VALUES('other',100,1)");ledger.db.commit()
        with monkeypatch.context() as patch:
            patch.setattr(quota_payloads,'MIGRATION_WAL_BYTES',1)
            assert ledger._payload_migration.step()==0 and ledger._payload_migration.pending
        assert tuple(ledger.db.execute('SELECT * FROM tracking_payload_migration').fetchone())==progress
        assert ledger.db.execute('PRAGMA busy_timeout').fetchone()[0]==timeout
        other.rollback()
        assert ledger._payload_migration.step()==2
    finally:other.close();ledger.close()
