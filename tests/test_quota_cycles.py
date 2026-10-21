from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.quota_cycles import QuotaLedger, estimate, split_cycles, quota_statistics
from cachemonitor.quota_panel import QuotaPanel


def _interrupt_archive_compaction(path, control, point):
    QuotaLedger.ARCHIVE_COMPACTION_ROWS=2
    ledger=QuotaLedger(path)
    def interrupt(sql):
        target='UPDATE cost_archive_progress' if point=='copy' else 'DROP TABLE cost_archive'
        if sql.startswith(target):
            control.send('uncommitted')
            control.recv()
    ledger.db.set_trace_callback(interrupt)
    try:
        while ledger._archive_compaction_pending:
            ledger._compact_cost_archive()
    finally:
        ledger.close();control.close()


@pytest.mark.integration
@pytest.mark.parametrize('point',['copy','swap'])
def test_price_history_compaction_survives_kill_and_concurrent_append(tmp_path,monkeypatch,point):
    import multiprocessing as mp
    import sqlite3
    path=tmp_path/'archive.sqlite'
    rows=[('h',f'r{i}','old',f'[ {i}, "model" ]',i+.25,'Fast',100+i+.125) for i in range(5)]
    with sqlite3.connect(path) as db:
        db.execute('create table cost_archive(home text,uid text,price_id text,signature text,cost real,service_tier text,at real,primary key(home,uid,price_id,signature))')
        db.executemany('insert into cost_archive values(?,?,?,?,?,?,?)',rows)
    context=mp.get_context('spawn');parent,pipe=context.Pipe()
    child=context.Process(target=_interrupt_archive_compaction,args=(str(path),pipe,point))
    child.start();pipe.close()
    try:
        assert parent.poll(15),f'archive copy did not reach interruption; exit={child.exitcode}'
        assert parent.recv()=='uncommitted'
        with sqlite3.connect(path) as db:
            assert db.execute('select * from cost_archive order by uid').fetchall()==rows
        child.kill();child.join(10)
        assert not child.is_alive() and child.exitcode!=0
        # An old writer can append while a new process resumes the copy.
        extra=('h','r5','old','[5]',8.75,'Slow',122.375)
        with sqlite3.connect(path) as db:
            db.execute('insert into cost_archive values(?,?,?,?,?,?,?)',extra)
        rows.append(extra)
        monkeypatch.setattr(QuotaLedger,'ARCHIVE_COMPACTION_ROWS',2)
        ledger=QuotaLedger(path)
        try:
            for _ in range(4):ledger._compact_cost_archive()
            assert not ledger._archive_compaction_pending
            assert [tuple(r) for r in ledger.db.execute('select * from cost_archive order by uid')]==rows
            assert ledger.db.execute('pragma integrity_check').fetchone()[0]=='ok'
            assert not ledger.db.execute("select name from sqlite_master where name in ('cost_archive_progress','cost_archive_packing')").fetchall()
            with ledger.db:ledger.db.execute('insert or ignore into cost_archive values(?,?,?,?,?,?,?)',extra)
            assert ledger.db.execute('select count(*) from cost_archive').fetchone()[0]==len(rows)
        finally:ledger.close()
    finally:
        if child.is_alive():child.kill()
        child.join(10);parent.close()


def test_price_history_compaction_defers_for_new_records_when_space_is_low(tmp_path,monkeypatch):
    import sqlite3
    import shutil
    path=tmp_path/'low-space.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('create table cost_archive(home text,uid text,price_id text,signature text,cost real,service_tier text,at real,primary key(home,uid,price_id,signature))')
        db.execute("insert into cost_archive values('h','r1','old','[1]',1.25,'Fast',100.125)")
    free=[0]
    monkeypatch.setattr(shutil,'disk_usage',lambda path:SimpleNamespace(free=free[0]))
    ledger=QuotaLedger(path)
    try:
        assert ledger._archive_compaction_pending
        assert not ledger.db.execute("select name from sqlite_master where name='cost_archive_packing'").fetchone()
        ledger.observe('h',quota(100,10))
        with ledger.db:ledger.db.execute("insert into cost_archive values('h','r2','old','[2]',2.5,'Slow',101.25)")
        expected=[tuple(r) for r in ledger.db.execute('select * from cost_archive order by uid')]
        free[0]=1024**3
        ledger._compact_cost_archive()
        assert not ledger._archive_compaction_pending
        assert [tuple(r) for r in ledger.db.execute('select * from cost_archive order by uid')]==expected
        assert len(ledger.report('h',200)['history'])==1
    finally:ledger.close()


def test_future_boundary_lookup_skips_past_calls_from_other_homes(tmp_path):
    ledger=QuotaLedger(tmp_path/'future.sqlite')
    try:
        with ledger.db:
            ledger.db.executemany('insert into calls(home,uid,ts) values(?,?,?)',
                                 [('other',str(n),n) for n in range(200)]+[('other','future',700)])
            ledger.db.execute('insert into tracking_observations(home,requested,received,data) values(?,?,?,?)',
                              ('other',699,700,'{}'))
        steps=[]
        ledger.db.set_progress_handler(lambda:(steps.append(1) or 0),1)
        boundary=ledger.db.execute('SELECT min(ts) FROM calls WHERE home IN (?) AND ts>?',('home',500)).fetchone()[0]
        ledger.db.set_progress_handler(None,0)
        assert boundary is None and len(steps)<200
        for table,columns in ledger.FUTURE_COLUMNS:
            for column in columns:
                condition='' if table=='tracking_observations' else 'home IN (?) AND '
                params=(500,) if not condition else ('home',500)
                plan=ledger.db.execute(f'EXPLAIN QUERY PLAN SELECT min({column}) FROM {table} WHERE {condition}{column}>?',params).fetchall()
                assert any(f'{column}>?' in row[3] for row in plan)
        ledger.report('home',now=500)
        assert ledger._report_cache['until']==700
        ledger.report('home',now=701)
        assert ledger._report_cache['until']==float('inf')
    finally:ledger.close()


def quota(at, used, reset=1000, source='live', account='a', separate=()):
    return {'windows': {'weekly': {'used_percent': used, 'resets_at': reset, 'window_minutes': 10080}},
            'plan_type': 'pro', 'observed_at': at, 'source': source, 'account': account,
            'separate_models': list(separate)}


def test_clear_current_cycle_survives_reopen_and_preserves_previous_cycle(tmp_path,monkeypatch):
    from cachemonitor.quota_view import prepare_quota_view
    path=tmp_path/'clear.sqlite'
    ledger=QuotaLedger(path)
    try:
        for at,used in ((100,0),(130,10),(200,0),(230,10)):
            ledger.observe('h',quota(at,used))
        sync(ledger,[row(ts=115),row(key='r2',ts=215)],at=235)
        before=prepare_quota_view(ledger.report('h',235))
        monkeypatch.setattr('cachemonitor.quota_cycles.time.time',lambda:240)
        ledger.clear_selected_cycle('h',200,'a')
        after=prepare_quota_view(ledger.report('h',240))
        assert after['periods'][0]['cost']==0
        assert after['periods'][0]['value'] is None
        assert [r['at'] for r in after['periods'][0]['series']['rows']]==[240]
        assert after['periods'][0]['series']['rows'][0]['remaining']==90
        assert after['periods'][1]['cost']==before['periods'][1]['cost']
        with pytest.raises(ValueError):ledger.clear_selected_cycle('h',200,'a')
        monkeypatch.setattr('cachemonitor.quota_cycles.time.time',lambda:250)
        ledger.clear_selected_cycle('h',240,'a')
        assert ledger.db.execute('select count(*) from quota_cycle_clears').fetchone()[0]==1
    finally:ledger.close()
    ledger=QuotaLedger(path)
    try:
        assert prepare_quota_view(ledger.report('h',250))['periods'][0]['start']==250
        ledger.observe('h',quota(260,12));ledger.observe('h',quota(290,14))
        sync(ledger,[row(ts=115),row(key='r2',ts=215),row(key='r3',ts=275)],revision=2,at=295)
        resumed=prepare_quota_view(ledger.report('h',295))
        assert resumed['periods'][0]['delta']==2
        assert resumed['periods'][0]['cost']>0
    finally:ledger.close()


def test_delete_selected_cycle_preserves_neighbors_and_survives_reopen(tmp_path,monkeypatch):
    from cachemonitor.quota_view import prepare_quota_view
    path=tmp_path/'delete.sqlite'
    ledger=QuotaLedger(path)
    monkeypatch.setattr('cachemonitor.quota_cycles.time.time',lambda:340)
    try:
        for at,used in ((100,0),(130,10),(200,0),(230,10),(300,0),(330,10)):
            ledger.observe('h',quota(at,used))
        sync(ledger,[row(key=str(at),ts=at) for at in (115,215,315)],at=335)
        before=prepare_quota_view(ledger.report('h',340))
        # A stale reset confirmation must never turn into a deletion.
        with pytest.raises(ValueError):ledger.clear_selected_cycle('h',200,'a')
        with pytest.raises(ValueError):ledger.clear_selected_cycle('h',300,'a',delete=True)
        with pytest.raises(ValueError):ledger.clear_selected_cycle('h',200,'other',delete=True)
        ledger.clear_selected_cycle('h',200,'a',delete=True)
        with pytest.raises(ValueError):ledger.clear_selected_cycle('h',200,'a',delete=True)
    finally:ledger.close()
    ledger=QuotaLedger(path)
    try:
        after=prepare_quota_view(ledger.report('h',340))
        assert [p['start'] for p in after['periods']]==[300,100]
        for period,original in zip(after['periods'],(before['periods'][0],before['periods'][2])):
            for key in ('start','end','cost','delta','value','current'):
                assert period[key]==original[key]
        assert after['lifetime']['cost']==pytest.approx(before['lifetime']['cost']-before['periods'][1]['cost'])
        ledger.clear_selected_cycle('h',300,'a')
        assert prepare_quota_view(ledger.report('h',340))['periods'][0]['start']==340
        # A previously reset cycle can later be deleted without resurrecting it.
        ledger.observe('h',quota(370,12));ledger.observe('h',quota(400,0))
        monkeypatch.setattr('cachemonitor.quota_cycles.time.time',lambda:410)
        ledger.clear_selected_cycle('h',340,'a',delete=True)
        assert [p['start'] for p in prepare_quota_view(ledger.report('h',410))['periods']]==[400,100]
        assert ledger.db.execute('select count(*) from quota_cycle_clears').fetchone()[0]==2
    finally:ledger.close()


def row(key='r1', ts=150, model='gpt-6-astra', **values):
    return {'service_tier':'Standard','key': key, 'ts': ts, 'model': model, 'input': 100000, 'cached': 20000,
            'written': 10000, 'output': 2000, 'reasoning': 500, **values}


def sync(ledger, rows, revision=1, at=300, loading=False):
    engine = SimpleNamespace(sessions={('h', 's'): {'revision': revision, 'prepared': {'history': rows}}})
    ledger.sync(engine, {'homes': ['h'], 'sessions': [{'home': 'h'}], 'ts': at, 'index': {'loading': loading}})


@pytest.mark.parametrize('packed',[False,True])
def test_history_import_reuses_complete_observations_and_repairs_partial_storage(tmp_path,packed,monkeypatch):
    import json
    import sqlite3
    from cachemonitor.event_store import EventStore,pack_event
    source=tmp_path/'index.sqlite';target=tmp_path/'quota.sqlite'
    records=[quota(100+n,n/100,source='history') for n in range(385)]
    records[0]['windows']['five_hour']={'used_percent':2,'window_minutes':300}
    records.append({**records[0],'separate_models':[{'model':'different'}]})
    records.append({'windows':{'weekly':{'used_percent':5}},'observed_at':700,'source':'history'})
    position=0
    def source_rows(values):
        nonlocal position
        rows=[('h',value['observed_at'],json.dumps({'payload':{'rate_limits':value}})) for value in values]
        if not packed:return rows
        result=[('retained',position+i,home,'session',at,pack_event(data)) for i,(home,at,data) in enumerate(rows)]
        position+=len(rows)
        return result
    insert='insert into events values('+','.join('?' for _ in range(6 if packed else 3))+')'
    with sqlite3.connect(source) as db:
        if packed:EventStore(db,source)
        else:db.execute('create table events(home text,ts real,data text)')
        db.executemany(insert,source_rows(records))
    ledger=QuotaLedger(target);reference=QuotaLedger(tmp_path/'reference.sqlite')
    tables=('observations','observation_context','window_observations','weekly_window_refs')
    def contents(store):
        return {table:sorted(tuple(row) for row in store.db.execute('select * from '+table)) for table in tables}
    try:
        cache_limit=ledger.db.execute('PRAGMA cache_size').fetchone()[0]
        ledger.import_observations(source,'h')
        assert ledger.db.execute('PRAGMA cache_size').fetchone()[0]==cache_limit
        for value in records:reference.observe('h',{**value,'observed_at':float(value['observed_at'])})
        assert contents(ledger)==contents(reference)
        ledger.close();ledger=QuotaLedger(target)
        queries=[];before=ledger.db.total_changes;ledger.db.set_trace_callback(queries.append)
        ledger.import_observations(source,'h');ledger.db.set_trace_callback(None)
        assert ledger.db.total_changes==before and len(queries)<len(records)//4
        # Legacy weekly display rows retain their first payload instead of gaining refs.
        legacy=reference.db.execute('''select r.window_id,o.home,o.at,o.source,o.account,c.plan,c.bucket,
            'weekly',o.used,o.reset,o.minutes,o.separate from weekly_window_refs r
            join observations o on o.id=r.observation_id join observation_context c on c.id=o.id
            where o.at=100''').fetchone()
        for store in (ledger,reference):
            store.db.execute('insert into window_observations values(?,?,?,?,?,?,?,?,?,?,?,?)',tuple(legacy))
            store.db.execute('delete from weekly_window_refs where window_id=?',(legacy[0],));store.db.commit()
        # Incomplete older stores still recover context and display references.
        ledger.db.execute("update observation_context set plan='',bucket=''")
        ledger.db.execute('delete from weekly_window_refs');ledger.db.commit()
        ledger.import_observations(source,'h')
        assert contents(ledger)==contents(reference)
        late=[quota(90,3,source='history'),quota(102,99,source='history')]
        with sqlite3.connect(source) as db:db.executemany(insert,source_rows(late))
        ledger.import_observations(source,'h')
        for value in late:reference.observe('h',{**value,'observed_at':float(value['observed_at'])})
        assert contents(ledger)==contents(reference)
        def failed_import(batch):raise sqlite3.OperationalError('interrupted import')
        monkeypatch.setattr(ledger,'_import_observation_parts',failed_import)
        with pytest.raises(sqlite3.OperationalError,match='interrupted import'):
            ledger.import_observations(source,'h')
        assert ledger.db.execute('PRAGMA cache_size').fetchone()[0]==cache_limit
    finally:ledger.close();reference.close()


@pytest.mark.parametrize('change',['append','past_correction','owner_replace','sequence_move'])
def test_observation_recovery_resumes_without_skipping_source_corrections(tmp_path,monkeypatch,change):
    import json
    import sqlite3
    from cachemonitor.event_store import EventStore,pack_event
    from cachemonitor.quota_history_import import ObservationImport
    source=tmp_path/'source.sqlite';target=tmp_path/'quota.sqlite'
    def payload(at,used):
        return json.dumps({'payload':{'rate_limits':quota(at,used,source='history')}})
    with sqlite3.connect(source) as db:
        store=EventStore(db,source);store.enable_replay_tokens()
        for i in range(8):
            home='other' if i==1 else 'h'
            db.execute('insert into events values(?,?,?,?,?,?)',
                       ('retained',i,home,'a',100+i,pack_event(payload(100+i,i))))
    monkeypatch.setattr(ObservationImport,'OBSERVATIONS',2)
    ledger=QuotaLedger(target)
    try:
        job=ObservationImport(ledger,source,['h'])
        assert not job.step()
        assert not job.source.in_transaction
        # A complete independent import starts from precisely the already
        # committed observation history, including facts corrected later.
        with sqlite3.connect(tmp_path/'reference.sqlite') as reference_db:
            ledger.db.backup(reference_db)
        job.close();ledger.close();ledger=QuotaLedger(target)
        with sqlite3.connect(source) as db:
            if change=='append':
                db.execute('insert into events values(?,?,?,?,?,?)',
                    ('retained',8,'h','a',99,pack_event(payload(99,95))))
            elif change=='past_correction':
                data=payload(100,98)
                db.execute('update events set data=? where home=? and ts=?',
                           (pack_event(data),'h',100))
            elif change=='owner_replace':
                # Replacing an owner loses its revision counter. The formerly
                # excluded row before the cursor now belongs to this home.
                db.execute("update or replace event_sessions set home='h' where home='other'")
            else:
                db.execute('update event_records set seq=100,data=? where seq=1',
                           (pack_event(payload(100,97)),))
        reference=QuotaLedger(tmp_path/'reference.sqlite')
        try:
            reference.import_observations(source,'h')
            job=ObservationImport(ledger,source,['h'])
            for _ in range(12):
                if job.step():break
            assert job.finished
            assert not job.source.in_transaction
            tables=('observations','observation_context','window_observations','weekly_window_refs')
            for table in tables:
                actual=sorted(tuple(r) for r in ledger.db.execute('select * from '+table))
                expected=sorted(tuple(r) for r in reference.db.execute('select * from '+table))
                assert actual==expected,(change,table)
            job.close()
            ledger.close();ledger=QuotaLedger(target)
            unchanged=ObservationImport(ledger,source,['h'])
            before=ledger.db.total_changes
            assert unchanged.step() and unchanged.metrics['source_rows']==0
            assert ledger.db.total_changes==before
            unchanged.close()
        finally:reference.close()
    finally:ledger.close()


@pytest.mark.parametrize('legacy',[False,True])
def test_observation_recovery_preserves_home_isolation_and_legacy_snapshot(tmp_path,monkeypatch,legacy):
    import json
    import sqlite3
    from cachemonitor.event_store import EventStore,pack_event
    from cachemonitor.quota_history_import import ObservationImport
    source=tmp_path/'source.sqlite'
    def payload(at):return json.dumps({'payload':{'rate_limits':quota(at,5)}})
    with sqlite3.connect(source) as db:
        db.execute('PRAGMA journal_mode=WAL')
        if legacy:
            db.execute('create table events(home text,ts real,data text)')
            db.executemany('insert into events values(?,?,?)',[('h',100+i,payload(100+i)) for i in range(129)])
            db.execute('insert into events values(?,?,?)',('other',99,'{"rate_limits":invalid'))
        else:
            store=EventStore(db,source);store.enable_replay_tokens()
            db.execute('insert into events values(?,?,?,?,?,?)',('source',0,'other','s',99,pack_event('{"rate_limits":invalid')))
            db.execute('insert into events values(?,?,?,?,?,?)',('source',1,'h','s',100,pack_event(payload(100))))
    ledger=QuotaLedger(tmp_path/'quota.sqlite')
    try:
        original=ledger._import_observation_parts;appends=[]
        def concurrent_append(batch):
            original(batch)
            if legacy and not appends:
                with sqlite3.connect(source) as db:db.execute('insert into events values(?,?,?)',('h',999,payload(999)))
                appends.append(True)
        monkeypatch.setattr(ledger,'_import_observation_parts',concurrent_append)
        job=ObservationImport(ledger,source,['h'])
        assert job.step()
        assert ledger.db.execute('select count(*) from observations').fetchone()[0]==(129 if legacy else 1)
        if legacy:assert appends and job.metrics['compatibility_import']
        job.close()
    finally:ledger.close()


def test_observation_recovery_detects_reappearing_owner_before_completed_cursor(tmp_path):
    import json
    import sqlite3
    from cachemonitor.event_store import EventStore,pack_event
    from cachemonitor.quota_history_import import ObservationImport
    source=tmp_path/'source.sqlite'
    with sqlite3.connect(source) as db:
        store=EventStore(db,source);store.enable_replay_tokens()
        db.execute('insert into events values(?,?,?,?,?,?)',('source',1,'h','s',100,
            pack_event(json.dumps({'payload':{'rate_limits':quota(100,5)}}))))
        owner=db.execute('select id from event_sessions').fetchone()[0]
        db.execute('delete from event_sessions where id=?',(owner,))
    ledger=QuotaLedger(tmp_path/'quota.sqlite')
    try:
        hidden=ObservationImport(ledger,source,['h']);assert hidden.step();hidden.close()
        assert ledger.db.execute('select count(*) from observations').fetchone()[0]==0
        with sqlite3.connect(source) as db:
            db.execute('insert into event_sessions(id,home,tid) values(?,?,?)',(owner,'h','s'))
        visible=ObservationImport(ledger,source,['h']);assert visible.step();visible.close()
        assert ledger.db.execute('select count(*) from observations').fetchone()[0]==1
    finally:ledger.close()


def test_observation_recovery_rolls_back_rows_and_progress_together(tmp_path,monkeypatch):
    import json
    import sqlite3
    from cachemonitor.event_store import EventStore,pack_event
    from cachemonitor.quota_history_import import ObservationImport
    source=tmp_path/'source.sqlite';target=tmp_path/'quota.sqlite'
    with sqlite3.connect(source) as db:
        store=EventStore(db,source);store.enable_replay_tokens()
        for i in range(4):
            db.execute('insert into events values(?,?,?,?,?,?)',('source',i,'h','s',100+i,
                pack_event(json.dumps({'payload':{'rate_limits':quota(100+i,i)}}))))
    monkeypatch.setattr(ObservationImport,'OBSERVATIONS',2)
    ledger=QuotaLedger(target)
    try:
        job=ObservationImport(ledger,source,['h']);assert not job.step()
        cursor=ledger.db.execute('select cursor from observation_import_progress').fetchone()[0]
        original=ledger._import_observation_parts
        def interrupted(batch):
            original(batch)
            raise sqlite3.OperationalError('interrupted before progress commit')
        monkeypatch.setattr(ledger,'_import_observation_parts',interrupted)
        with pytest.raises(sqlite3.OperationalError,match='before progress commit'):job.step()
        assert ledger.db.execute('select count(*) from observations').fetchone()[0]==2
        assert ledger.db.execute('select cursor from observation_import_progress').fetchone()[0]==cursor
        assert not job.source.in_transaction
        job.close();ledger.close();ledger=QuotaLedger(target)
        resumed=ObservationImport(ledger,source,['h'])
        assert resumed.step()
        assert ledger.db.execute('select count(*) from observations').fetchone()[0]==4
        resumed.close()
    finally:ledger.close()


@pytest.mark.parametrize('observations',[128,2048])
def test_report_reuses_history_across_heartbeat_and_invalidates_late_observation(tmp_path,monkeypatch,observations):
    ledger=QuotaLedger(tmp_path/'quota.sqlite',pack_history=observations==2048)
    try:
        now=observations+200
        sync(ledger,[row()],at=now)
        for n in range(observations):ledger.observe('h',quota(100+n,10+n/100,reset=10000))
        original=ledger._report;builds=[]
        def build(*args,**kwargs):builds.append(1);return original(*args,**kwargs)
        monkeypatch.setattr(ledger,'_report',build)
        first=ledger.report('h',now)
        steps=[0]
        ledger.db.set_progress_handler(lambda:(steps.__setitem__(0,steps[0]+100),0)[1],100)
        same=ledger.report('h',now)
        ledger.db.set_progress_handler(None,0)
        assert same==first and len(builds)==1 and steps[0]<1000
        sync(ledger,[row()],at=now+1)
        fresh=ledger.report('h',now+1)
        assert fresh['index_at']==now+1 and len(builds)==1
        ledger.observe('h',quota(now-1,32,reset=10000))
        changed=ledger.report('h',now+1)
        assert len(builds)==2 and changed!=fresh
        if observations==2048:
            ledger.observe('h',quota(100+observations//2,25,reset=10000))
            changed=ledger.report('h',now+1)
            assert changed==original('h',now+1)
        # Cached reads cannot survive rollback with a reused revision number.
        ledger.db.execute('update observations set used=50 where at=100')
        ledger.report('h',now+1);ledger.db.rollback()
        assert ledger.report('h',now+1)==changed
    finally:ledger.close()


def test_cached_report_preserves_coverage_progress_and_future_call_boundaries(tmp_path):
    ledger=QuotaLedger(tmp_path/'quota.sqlite')
    calls=[row(f'r{n}',ts=at) for n,at in enumerate((120,220,320,550))]
    try:
        sync(ledger,calls,at=150)
        for at,used in ((100,10),(200,20),(300,30)):ledger.observe('h',quota(at,used))
        for at,now in ((150,500),(250,501),(350,502),(351,551)):
            sync(ledger,calls,at=at)
            assert ledger.report('h',now)==ledger._report('h',now)
    finally:ledger.close()


def test_activity_cursor_commits_with_usage_and_rolls_back_on_save_failure(tmp_path):
    import sqlite3
    path=tmp_path/'quota.sqlite';ledger=QuotaLedger(path)
    def save(revision,output):
        calls=[row(f'r{i}',ts=150+i,output=output) for i in range(300)]
        engine=SimpleNamespace(sessions={('h','s'):{'revision':revision,'prepared':{'history':calls}}})
        ledger.sync(engine,dict(homes=['h'],sessions=[{'home':'h'}],ts=300,index={'loading':False},
                               activity_cursor=('source',revision)))
    try:
        save(1,2000);raw=ledger.db
        class FailedCommit:
            def __getattr__(self,name):return getattr(raw,name)
            def commit(self):raise sqlite3.OperationalError('injected save failure')
        ledger.db=FailedCommit()
        with pytest.raises(sqlite3.OperationalError):save(2,4000)
        ledger.db=raw;ledger.close();ledger=QuotaLedger(path)
        assert ledger.activity_cursor()==('source',1)
        assert tuple(ledger.db.execute('SELECT count(*),min(output),max(output) FROM calls').fetchone())==(300,2000,2000)
    finally:ledger.close()


def test_session_sync_work_does_not_scale_with_unrelated_call_history(tmp_path):
    path=tmp_path/'session-sync.sqlite'
    ledger=QuotaLedger(path)
    try:
        sync(ledger,[row()])
        def unrelated(start, end):
            ledger.db.executemany('insert into calls select home,?, ?,ts,model,input,cached,written,'
                'output,reasoning,cost,price_id,signature,service_tier from calls where home=? and uid=?',
                [(f'other-{i}',f'session-{i}','h','r1') for i in range(start,end)])
            ledger.db.commit()
        def measured_sync(revision):
            steps=[0]
            def progress():steps[0]+=100;return 0
            ledger.db.set_progress_handler(progress,100)
            try:sync(ledger,[row(output=2000+revision)],revision=revision)
            finally:ledger.db.set_progress_handler(None,0)
            assert ledger.db.execute('select output from calls where home=? and uid=?',('h','r1')).fetchone()[0]==2000+revision
            return steps[0]
        unrelated(0,32)
        small=measured_sync(2)
        unrelated(32,4096)
        ledger.close();ledger=QuotaLedger(path)
        large=measured_sync(3)
        # Count SQLite VM work, independent of device speed and cache warmth.
        assert large<=small*2+1000
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==4097
        sync(ledger,[],revision=4)
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==4096
    finally:ledger.close()


def test_restart_and_append_do_not_rewrite_unchanged_call_boundaries(tmp_path):
    path=tmp_path/'boundaries.sqlite';ledger=QuotaLedger(path)
    calls=[row(f'r{i}',ts=150+i,request_ts=100+i) for i in range(300)]
    calls[0]['request_ts']=None
    try:sync(ledger,calls)
    finally:ledger.close()
    ledger=QuotaLedger(path)
    try:
        ledger.db.executescript('''create temp table boundary_writes(uid text);
            create temp trigger changed_boundary after update on tracking_call_boundaries
            begin insert into boundary_writes values(new.uid); end;
            create temp trigger inserted_boundary after insert on tracking_call_boundaries
            begin insert into boundary_writes values(new.uid); end;''')
        sync(ledger,calls)
        assert ledger.db.execute('select count(*) from boundary_writes').fetchone()[0]==0
        calls.append(row('new',ts=220,request_ts=210))
        calls[0]={**calls[0],'request_ts':90}
        calls[1]={**calls[1],'request_ts':None}
        sync(ledger,calls,revision=2)
        assert {r[0] for r in ledger.db.execute('select uid from boundary_writes')}=={'new','r0','r1'}
        assert ledger.db.execute("select started from tracking_call_boundaries where uid='r0'").fetchone()[0]==90
        assert ledger.db.execute("select started from tracking_call_boundaries where uid='r1'").fetchone()[0] is None
    finally:ledger.close()


def test_slow_cycle_diagnostics_are_quiet_normally_and_rate_limited(monkeypatch,tmp_path):
    import json
    from cachemonitor import quota_diagnostics as diagnostics
    clock=[100.];records=[]
    monkeypatch.setattr(diagnostics.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(diagnostics,'_logger',lambda path:SimpleNamespace(warning=records.append))
    last=float('-inf');path=tmp_path/'analysis.sqlite'
    assert diagnostics.record_slow_cycle(path,{'analysis':1.},2.,last)==last
    last=diagnostics.record_slow_cycle(path,{'analysis':11.},12.,last)
    assert json.loads(records[0])['stage_seconds']=={'analysis':11.}
    clock[0]+=59
    assert diagnostics.record_slow_cycle(path,{'analysis':15.},40.,last)==last
    assert len(records)==1
    clock[0]+=1
    last=diagnostics.record_slow_cycle(path,{'analysis':1.},31.,last)
    assert len(records)==2 and json.loads(records[1])['source_age_seconds']==31.
    def full_disk(path):raise OSError('full')
    monkeypatch.setattr(diagnostics,'_logger',full_disk);clock[0]+=60
    assert diagnostics.record_slow_cycle(path,{'analysis':11.},12.,last)==clock[0]


def test_standard_report_keeps_unknown_mode_separate_and_unpriced(tmp_path):
    ledger=QuotaLedger(tmp_path/'merged.sqlite')
    try:
        ledger.observe('h',quota(100,20))
        ledger.observe('h',quota(200,30))
        sync(ledger,[row(),row('unknown',160,service_tier='미확인'),row('fast',170,service_tier='Fast')])
        cycle=ledger.report('h',300)['cycles'][0]
        standard=next(r for r in cycle['models'] if r['service_tier']=='Standard')
        assert len(cycle['models'])==3
        assert standard['calls']==1 and standard['unknown_mode_calls']==0
        unknown=next(r for r in cycle['models'] if r['service_tier']=='미확인')
        assert unknown['calls']==1 and unknown['cost'] is None
        assert ledger.db.execute("select service_tier from calls where uid='unknown'").fetchone()[0]=='미확인'
    finally:ledger.close()


def test_internal_review_stays_in_ledger_but_not_work_usage_or_models(tmp_path):
    ledger=QuotaLedger(tmp_path/'internal-review.sqlite')
    try:
        ledger.observe('h',quota(100,20))
        ledger.observe('h',quota(200,30))
        sync(ledger,[row(),row('review',160,model='codex-auto-review')])
        cycle=ledger.report('h',300)['cycles'][0]
        assert {r['model'] for r in cycle['models']}=={'gpt-6-astra'}
        assert sum(r['calls'] for r in cycle['models'])==cycle['priced_calls']==1
        assert ledger.db.execute("select count(*) from calls where model='codex-auto-review'").fetchone()[0]==1
    finally:ledger.close()


def test_same_window_formula_cache_and_reasoning_not_double_counted(tmp_path):
    ledger = QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h', quota(100, 20, separate=['gpt-5.6-luna']))
        ledger.observe('h', quota(200, 30, 1001, separate=['gpt-5.6-luna']))
        sync(ledger, [row(), row('before', 50), row('after', 250), row('reserve', 160, 'gpt-5.6-luna')])
        cycle = ledger.report('h', 300)['cycles'][0]
        assert cycle['cost'] == pytest.approx(.7 + .02 + .125 + .1)
        assert cycle['delta'] == 10 and cycle['start'] == 100
        assert estimate(cycle['cost'],cycle['delta'],5,cycle['blocked'])['usd'] == pytest.approx(.4725)
        assert len(cycle['models']) == 2 and sum(m['calls'] for m in cycle['cycle_models']) == 3
        assert cycle['partial']
        # Same response ID cannot be counted twice on refresh or restart.
        sync(ledger, [row(), row()], revision=2)
        assert ledger.db.execute('select count(*) from calls').fetchone()[0] == 1
    finally: ledger.close()
    restored = QuotaLedger(tmp_path/'cycles.sqlite')
    try: assert len(restored.report('h',300)['cycles']) == 1
    finally: restored.close()


def test_reset_jitter_natural_manual_account_and_regression_boundaries(tmp_path):
    ledger = QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        for q in (quota(100,20),quota(200,30,1001),quota(1002,0,605800),
                  quota(1100,5,605801),quota(1200,0,700000),quota(1300,10,700001),
                  quota(1400,9,700000),quota(1500,9,700000,account='b')):
            ledger.observe('h',q)
        groups=split_cycles([dict(r) for r in ledger.db.execute('select * from observations')])
        assert len(groups)==5
        assert groups[1]['reason']=='예정 리셋 확인'
        assert groups[1]['boundary']==1001
        assert groups[2]['reason']=='한도 시각 변경: 관측 구간 분리'
        assert all('수동 리셋' not in g['reason'] for g in groups)
        assert groups[3]['discontinuous']
        assert groups[4]['reason']=='계정 변경'
    finally: ledger.close()


def test_missing_price_and_incomplete_index_suppress_conversion(tmp_path):
    ledger=QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h',quota(100,10)); ledger.observe('h',quota(200,20))
        sync(ledger,[row(written=None)],at=150,loading=True)
        c=ledger.report('h',300)['cycles'][0]
        assert c['missing']==1
        assert c['models'][0]['written'] is None
        assert '해당 구간의 토큰 수집 완료 대기' in c['blocked']
        assert estimate(c['cost'],c['delta'],5,c['blocked'])['usd'] is None
        # A later authoritative modern record removes the old legacy row.
        sync(ledger,[row(key='legacy:x')],revision=2)
        sync(ledger,[row(key='modern')],revision=3)
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==1
    finally: ledger.close()


def test_mode_change_preserves_price_snapshot_and_ambiguous_gap_is_excluded(tmp_path):
    ledger=QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h',quota(100,20));ledger.observe('h',quota(200,30))
        sync(ledger,[row(),row('gap',250)])
        ledger.db.execute("update calls set price_id='old-rates',cost=42 where uid='r1'")
        ledger.db.commit()
        sync(ledger,[row(service_tier='Fast'),row('gap',250)],revision=2)
        stored=ledger.db.execute("select cost,price_id,service_tier from calls where uid='r1'").fetchone()
        assert stored['cost']==pytest.approx(2.3625) and stored['service_tier']=='Fast'
        assert ledger.db.execute("select cost from cost_archive where price_id='old-rates'").fetchone()[0]==42
        ledger.observe('h',quota(300,0,2000))
        older=ledger.report('h',350)['cycles'][1]
        assert older['uncertain_gap'] and older['cycle_end']==200
        assert sum(r['calls'] for r in older['cycle_models'])==1
    finally: ledger.close()


def test_observation_gap_excludes_unmatched_cost_and_consumption_but_preserves_raw_records(tmp_path):
    ledger=QuotaLedger(tmp_path/'gap.sqlite')
    try:
        for q in (quota(100,10,10000),quota(200,15,10000),quota(3801,23,10000),quota(3900,28,10000)):
            ledger.observe('h',q)
        sync(ledger,[row('before',150),row('gap-call',3800),row('after',3850)],at=4000)
        report=ledger.report('h',4000)
        assert len(report['cycles'])==1
        summary=quota_statistics(report)
        assert summary['valid']==2 and summary['delta']==10
        assert summary['calls']==2 and summary['cost']==pytest.approx(1.89)
        assert len(report['cycles'])==1 and report['cycles'][0]['delta']==18
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==3
    finally:ledger.close()


def test_quota_home_switch_clears_values_and_rejects_late_old_home_results(tmp_path):
    import time
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'scope.ini'),QSettings.IniFormat))
    now=time.time();a=str(tmp_path/'a');b=str(tmp_path/'b')
    panel.set_homes([a,b])
    value={'quota':quota(now,30,now+1000),'report':{'home':a,'cycles':[],'at':now,'index_loading':False}}
    assert panel.receive(value)
    assert panel.quota is value['quota']
    switched=[];panel.home_selected.connect(switched.append)
    panel.home_choice.setCurrentIndex(1)
    assert switched==[b] and panel.quota is None
    assert panel.initial_loading.isVisible() and not panel.history.rows
    assert panel.result.text().endswith('—') and not panel.intervals.isVisible()
    assert not panel.receive(value)
    assert panel.quota is None and panel.report['home']==b
    panel.receive({'report':{'home':b,'cycles':[],'manual_reset_pending':True,'index_loading':True}})
    assert panel.history_waiting.isVisible() and not panel.history_controls.isVisible()
    assert not panel.conversion.isVisible()
    panel.receive({'report':{'home':b,'cycles':[],'error':True,'index_loading':False},'issue':'원장 오류'})
    assert panel.status.text()=='원장 오류' and panel.status.isVisible()
    assert '확인 불가' not in panel.result.text()
    assert not panel.intervals.isVisible() and not panel.paging.isVisible() and not panel.history.rows
    def walk(node):
        yield node
        for child in node.nodes:yield from walk(child)
    assert not any(node.text()=='사용 한도' for node in walk(panel))
    panel.deleteLater()
