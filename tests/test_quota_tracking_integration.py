from types import SimpleNamespace
import pytest
from cachemonitor.quota_cycles import QuotaLedger, quota_statistics
from cachemonitor import quota_tracking_store as tracking


def activity(ledger, at, turns, calls=(), home='h', wire=()):
    rows=[dict(key=str(t),ts=t,model='gpt-6-astra',input=1000,cached=0,written=0,
               output=10,reasoning=0,service_tier='Standard') for t in calls]
    engine=SimpleNamespace(sessions={(home,'s'):{'revision':at,'prepared':{'history':rows}}})
    ledger.sync(engine,dict(homes=[home],ts=at,usage_collection_complete=True,request_activity=wire,
        index=dict(loading=False,usage_complete=True,last_usage_success=at),
        sessions=[dict(home=home,id='s',turn_records=turns)]))


def observe(ledger, at, remaining, home='h'):
    q=dict(source='live',account='a',plan_type='pro',bucket='codex',requested_at=at,
           observed_at=at+0.1,windows=dict(weekly=dict(used_percent=100-remaining,
           window_minutes=10080,resets_at=1000000)))
    ledger.observe(home,q)
    tracking.observe(ledger.db,home,q)


def test_projected_current_period_survives_restart_and_invalidates_old_edits(tmp_path):
    from cachemonitor.quota_view import prepare_quota_view
    from cachemonitor.quota_worker import QuotaComputation,chart_result
    path=tmp_path/'projected.sqlite'
    worker=QuotaComputation(path,'h');ledger=worker._ledger(writable=True)
    def publish(at):
        raw=ledger.report('h',at)
        return worker._prepare(raw,(worker.ledger_generation,ledger._report_generation))
    def rows(report):
        return list(chart_result(prepare_quota_view(report,deferred=True))['series']['rows'])
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,70),(110,69),(120,99),(125,100),(130,99),(140,98)):
            observe(ledger,at,remaining)
        activity(ledger,141,{},[107,117,137],wire=[
            dict(home='h',identity='corrected',response='117',start=126,end=129,status='completed'),
            dict(home='h',identity='latest',response='137',start=132,end=137,status='completed')])
        assert ledger.current_report('h',142) is None
        expected=publish(142)['view']['chart']['series']['rows']
        assert ledger.db.execute('select valid from quota_current_projection').fetchone()[0]==1
        worker.close();worker=QuotaComputation(path,'h');ledger=worker._ledger()
        current=ledger.current_report('h',142)
        assert current['history_pending'] and min(r['at'] for r in current['history'])==120.1
        assert rows(current)==list(expected)
        # Ordinary append and cost correction do not replay the immutable
        # observation prefix. They still use current authoritative call values.
        observe(ledger,150,97)
        ledger.db.execute("update calls set cost=cost+1 where uid='137'");ledger.db.commit()
        assert rows(ledger.current_report('h',152))==rows(ledger.report('h',152))
        ledger.db.execute('delete from tracking_observations where received=110.1');ledger.db.commit()
        assert ledger.current_report('h',153) is None
        publish(153)
        assert rows(ledger.current_report('h',154))==rows(ledger.report('h',154))
        ledger.db.execute("update quota_current_projection set payload=x'00'");ledger.db.commit()
        assert ledger.current_report('h',154) is None
        assert ledger.report('h',154)['history']
        publish(154)
        assert ledger.current_report('h',155) is not None
    finally:worker.close()


def test_closed_period_projection_keeps_exact_queries_and_detail_after_restart(tmp_path,monkeypatch):
    from cachemonitor.quota_worker import QuotaComputation,chart_result
    from cachemonitor.quota_view import prepare_quota_query,prepare_quota_view
    from cachemonitor.quota_archive import load
    from cachemonitor.quota_exclusions import preview_exclusion
    path=tmp_path/'archive.sqlite'
    clock=[170.]
    monkeypatch.setattr('cachemonitor.quota_worker.time.time',lambda:clock[0])
    worker=QuotaComputation(path,'h');ledger=worker._ledger(writable=True)
    queries=({},dict(start=104.,end=135.),dict(model='gpt-6-astra'),dict(days=7),dict(mode='excluded'))
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,70),(110,69),(120,100),(130,99),(140,98),(150,100),(160,99)):
            observe(ledger,at,remaining)
        calls=[107,127,157]
        wire=[dict(home='h',identity=str(at),response=str(at),start=at-2,end=at,status='completed') for at in calls]
        activity(ledger,161,{},calls,wire=wire)
        raw=ledger.report('h',clock[0])
        raw['_cycle_versions']={key:value[0] for key,value in ledger._cycle_cache.items()}
        full={**raw,'view':prepare_quota_view(raw,deferred=True)}
        reply=worker._prepare(raw,(worker.ledger_generation,ledger._report_generation))
        expected=[prepare_quota_query(full,q) for q in queries]
        selected=next(p['start'] for p in full['view']['periods'] if not p['current'])
        chart=chart_result(full['view'],selected)
        preview=preview_exclusion(full,120.1,140.1)
        assert load(ledger,'h',clock[0]) is not None
        worker.close();worker=QuotaComputation(path,'h');ledger=worker._ledger()
        def no_replay(*a,**kw):pytest.fail('closed observations replayed after restart')
        with monkeypatch.context() as patch:
            patch.setattr(ledger,'report',no_replay)
            result=worker.execute(dict(kind='report',id='r',key='k'),publish=lambda value:None)
        assert 'error' not in result
        prepared=worker.reports[result['report']['worker_revision']]
        assert not prepared.get('history_pending')
        for query,value in zip(queries,expected):assert prepare_quota_query(prepared,query)==value
        actual=chart_result(prepared['view'],selected)
        assert actual['digest']==chart['digest']
        assert actual['series']==chart['series']
        actual_preview=worker.execute(dict(kind='preview',id='p',key='k',revision=result['report']['worker_revision'],start=120.1,end=140.1))
        assert actual_preview.get('value')==preview
        # A new call without wire timing affects the current suffix, not every
        # completed period. A correction to an old cost invalidates the prefix.
        writer=QuotaLedger(path)
        try:
            clock[0]=180.;observe(writer,175,98)
            activity(writer,176,{},[*calls,173],wire=wire)
            assert load(ledger,'h',clock[0]) is not None
            expected_raw=writer.report('h',clock[0])
            with monkeypatch.context() as patch:
                patch.setattr(ledger,'report',no_replay)
                result=worker.execute(dict(kind='report',id='r2',key='k'),publish=lambda value:None)
            assert 'error' not in result
            actual=worker.reports[result['report']['worker_revision']]
            assert actual['view']['lifetime']['cost']==quota_statistics(expected_raw,include_mode_assumptions=True)['cost']
            expected_view={**expected_raw,'view':prepare_quota_view(expected_raw,deferred=True)}
            assert prepare_quota_query(actual,{})==prepare_quota_query(expected_view,{})
            clock[0]=210.
            observe(writer,190,100);observe(writer,200,99)
            activity(writer,201,{},[*calls,173,197],wire=wire)
            expected_raw=writer.report('h',clock[0])
            assert load(ledger,'h',clock[0]) is not None,[tuple(row) for row in ledger.db.execute('SELECT table_name,at FROM report_changes ORDER BY revision DESC LIMIT 15')]
            # A rejected new prefix must leave the matching replay checkpoint
            # usable. The next poll must not silently fall back to all history.
            with monkeypatch.context() as patch:
                patch.setattr('cachemonitor.quota_archive.MAX_BYTES',1)
                patch.setattr(ledger,'report',no_replay)
                for request_id in ('bounded-first','bounded-repeat'):
                    bounded=worker.execute(dict(kind='report',id=request_id,key='k'))
                    assert 'error' not in bounded
            with monkeypatch.context() as patch:
                patch.setattr(ledger,'report',no_replay)
                result=worker.execute(dict(kind='report',id='r3',key='k'),publish=lambda value:None)
            assert 'error' not in result
            actual=worker.reports[result['report']['worker_revision']]
            assert actual['view']['lifetime']['cost']==quota_statistics(expected_raw,include_mode_assumptions=True)['cost']
            assert load(ledger,'h',clock[0]).cutoff==190.1
            assert ledger.db.execute('SELECT count(*) FROM quota_archive_heads').fetchone()[0]==2
            # The previous view's exact detail remains available after advancing
            # the prefix, without retaining its source rows in the reader.
            assert chart_result(prepared['view'],selected)['series']==chart['series']
            writer.db.execute("UPDATE calls SET cost=cost+1 WHERE uid='107'");writer.db.commit()
            assert load(ledger,'h',clock[0]) is None
            stale=worker.execute(dict(kind='chart',id='stale',key='k',revision=result['report']['worker_revision'],
                selection=min(p['start'] for p in actual['view']['periods'])))
            assert stale.get('stale') and not stale.get('permanent')
        finally:writer.close()
    finally:worker.close()


def test_closed_projection_rejects_coverage_change_during_save(tmp_path,monkeypatch):
    from cachemonitor import quota_archive
    from cachemonitor.quota_view import prepare_quota_view
    path=tmp_path/'coverage-race.sqlite'
    reader=QuotaLedger(path,pack_history=True,weekly_history_only=True)
    writer=QuotaLedger(path)
    try:
        tracking.enable(writer.db,'h',100)
        for at,remaining in ((102,70),(110,69),(120,100),(130,99)):
            observe(writer,at,remaining)
        activity(writer,140,{},[107,127])
        raw=reader.report('h',140)
        raw['_cycle_versions']={key:value[0] for key,value in reader._cycle_cache.items()}
        view=prepare_quota_view(raw,deferred=True)
        original=quota_archive.encode;changed=[]
        def encode(value):
            if not changed:
                writer.db.execute("UPDATE state SET unclassified=1 WHERE home='h'")
                writer.db.commit();changed.append(True)
            return original(value)
        monkeypatch.setattr(quota_archive,'encode',encode)
        assert not quota_archive.save(reader,raw,view)
        assert changed
        assert quota_archive.load(reader,'h',140) is None
        assert writer.db.execute('SELECT count(*) FROM calls').fetchone()[0]==2
    finally:
        writer.close();reader.close()


def test_closed_projection_write_failure_preserves_previous_version_and_repairs_bad_head(tmp_path):
    import sqlite3
    from cachemonitor import quota_archive
    from cachemonitor.quota_view import prepare_quota_view
    path=tmp_path/'archive-write.sqlite'
    ledger=QuotaLedger(path,pack_history=True,weekly_history_only=True)
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,70),(110,69),(120,100),(130,99)):
            observe(ledger,at,remaining)
        activity(ledger,140,{},[107,127])
        def prepare(at):
            raw=ledger.report('h',at)
            raw['_cycle_versions']={key:value[0] for key,value in ledger._cycle_cache.items()}
            return raw,prepare_quota_view(raw,deferred=True)
        assert quota_archive.save(ledger,*prepare(140))
        previous=quota_archive.load(ledger,'h',140)
        observe(ledger,150,100);observe(ledger,160,99)
        activity(ledger,170,{},[107,127,157])
        raw,view=prepare(170)
        ledger.db.set_authorizer(lambda action,table,*args:
            sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and table=='quota_archive_entries' else sqlite3.SQLITE_OK)
        try:
            with pytest.raises(sqlite3.DatabaseError):quota_archive.save(ledger,raw,view)
        finally:ledger.db.set_authorizer(None)
        ledger.close();ledger=QuotaLedger(path,pack_history=True,weekly_history_only=True,reader=True)
        assert quota_archive.load(ledger,'h',170).epoch==previous.epoch
        assert ledger.db.execute('SELECT count(*) FROM quota_archive_heads').fetchone()[0]==1
        assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0]==3
        assert quota_archive.save(ledger,*prepare(170))
        latest=quota_archive.load(ledger,'h',170)
        ledger.db.execute('UPDATE quota_archive_heads SET payload=? WHERE epoch=?',(b'corrupted',latest.epoch));ledger.db.commit()
        assert quota_archive.load(ledger,'h',170) is None
        assert quota_archive.save(ledger,*prepare(170))
        assert quota_archive.load(ledger,'h',170) is not None
    finally:ledger.close()


def test_current_projection_does_not_require_full_history_cache_admission(tmp_path,monkeypatch):
    from cachemonitor import quota_view
    from cachemonitor.quota_worker import QuotaComputation,chart_result
    monkeypatch.setattr(quota_view,'HISTORY_GROUP_CACHE_BYTES',1)
    monkeypatch.setattr(tracking,'REPLAY_CACHE_BYTES',8192)
    worker=QuotaComputation(tmp_path/'bounded.sqlite','h');ledger=worker._ledger(writable=True)
    try:
        tracking.enable(ledger.db,'h',100)
        calls=[];wire=[]
        for index in range(20):
            at=200+index*20
            observe(ledger,at,100);observe(ledger,at+10,99)
            calls.append(at+5);wire.append(dict(home='h',identity=str(at),response=str(at+5),start=at+1,end=at+5,status='completed'))
        activity(ledger,at+11,{},calls,wire=wire)
        raw=ledger.report('h',at+12)
        expected=worker._prepare(raw,(worker.ledger_generation,ledger._report_generation))['view']['chart']['series']['rows']
        current=ledger.current_report('h',at+12)
        assert current is not None
        actual=chart_result(quota_view.prepare_quota_view(current,deferred=True))['series']['rows']
        assert list(actual)==list(expected)
        assert len(current['history'])<len(raw['history'])
    finally:worker.close()


def test_report_worker_edit_reuses_prepared_ledger_without_maintenance(tmp_path,monkeypatch):
    from cachemonitor import quota_cycles
    from cachemonitor.quota_worker import QuotaComputation
    path=tmp_path/'prepared-edit.sqlite';ledger=QuotaLedger(path)
    tracking.enable(ledger.db,'h',100)
    for at,remaining in ((102,70),(110,69),(120,99),(125,100),(130,99),(140,98)):
        observe(ledger,at,remaining)
    activity(ledger,141,{},[107,117,137]);ledger.close()
    monkeypatch.setattr(quota_cycles.time,'time',lambda:142)
    def unexpected_maintenance(self):raise AssertionError('An explicit edit must not repeat initialization')
    monkeypatch.setattr(QuotaLedger,'_initialize',unexpected_maintenance)
    worker=QuotaComputation(path,'h')
    try:
        report=worker.execute(dict(kind='report',id=1))['report']
        start=min(period['start'] for period in report['view']['periods'])
        result=worker.execute(dict(kind='clear_cycle',id=2,start=start,account='a',delete=True))
        assert result.get('saved') and not result.get('error')
        assert len(result['report']['view']['periods'])==len(report['view']['periods'])-1
        assert worker.ledger.db.execute('select count(*) from tracking_observations').fetchone()[0]==6
    finally:worker.close()


def _committed_quota_fixture(ledger):
    tracking.enable(ledger.db,'h',100)
    activity(ledger,101,{})
    observe(ledger,102,70)
    activity(ledger,109,{'one':dict(started_at=103,ended_at=108)},[107])
    observe(ledger,110,69)
    activity(ledger,111,{'one':dict(started_at=103,ended_at=108)},[107])


def _advance_quota_fixture(ledger):
    observe(ledger,120,68)
    activity(ledger,131,{'one':dict(started_at=103,ended_at=108),
                         'two':dict(started_at=123,ended_at=126)},[107,125])


def test_activity_journal_retirement_preserves_measurements_and_current_activity(tmp_path):
    path=tmp_path/'ledger.sqlite'
    ledger=QuotaLedger(path)
    _committed_quota_fixture(ledger)
    ledger.db.execute('insert into tracking_wire values(?,?,?,?,?,?)',('h','wire','response',103,108,'completed'))
    ledger.db.executescript('''
        CREATE TABLE tracking_activity_versions(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            home TEXT,identity TEXT,seen REAL,start REAL,end REAL,kind TEXT,status TEXT,response TEXT);
        CREATE INDEX tracking_activity_seen ON tracking_activity_versions(home,seen);
        INSERT INTO tracking_activity_versions(home,identity,seen,start,end,kind,status,response)
            VALUES('h','wire',104,103,NULL,'wire','pending',''),
                  ('h','wire',109,103,108,'wire','completed','response');
    ''')
    expected=ledger.report('h',112)
    summary=tracking.ActivitySummary().read(ledger.db,'h',100)
    ledger.close()
    ledger=QuotaLedger(path)
    try:
        assert ledger.report('h',112)==expected
        assert tracking.ActivitySummary().read(ledger.db,'h',100)==summary
        assert ledger.db.execute("select 1 from sqlite_master where name='tracking_activity_versions'").fetchone() is None
        _advance_quota_fixture(ledger)
        assert ledger.db.execute('select count(*) from tracking_tasks').fetchone()[0]==2
        assert tuple(ledger.db.execute('select start,end,status from tracking_wire').fetchone())==(103,108,'completed')
        assert ledger.db.execute('pragma integrity_check').fetchone()[0]=='ok'
    finally:ledger.close()


def _quota_process_waiting_for_termination(path, connection, partial):
    ledger=QuotaLedger(path)
    _committed_quota_fixture(ledger)
    expected=ledger.report('h',112)
    if partial:
        class DeferredCommit:
            def __init__(self,db):self.db=db
            def __getattr__(self,name):return getattr(self.db,name)
            def commit(self):pass
        ledger.db=DeferredCommit(ledger.db)
        _advance_quota_fixture(ledger)
        assert ledger.db.in_transaction
    connection.send(expected)
    connection.recv()  # The parent kills this process without closing the ledger.


@pytest.mark.parametrize('partial',[False,True],ids=['committed','uncommitted'])
def test_forced_termination_preserves_quota_and_rolls_back_partial_coverage(tmp_path,partial):
    import multiprocessing as mp
    context=mp.get_context('spawn')
    receiving,sending=context.Pipe()
    path=tmp_path/'killed.sqlite'
    process=context.Process(target=_quota_process_waiting_for_termination,
                            args=(path,sending,partial))
    process.start();sending.close()
    try:
        assert receiving.poll(15),'quota child did not reach its durable or staged boundary'
        expected=receiving.recv()
    finally:
        process.kill();process.join(10);receiving.close()
    assert not process.is_alive() and process.exitcode!=0
    ledger=QuotaLedger(path)
    reference=QuotaLedger(tmp_path/'reference.sqlite')
    try:
        assert ledger.db.execute('pragma integrity_check').fetchone()[0]=='ok'
        assert ledger.report('h',112)==expected
        assert tuple(ledger.db.execute('select at,complete from tracking_health').fetchone())==(111,1)
        assert tuple(ledger.db.execute('select at,complete_at from state').fetchone())==(111,111)
        assert ledger.db.execute('select count(*) from tracking_gaps').fetchone()[0]==0
        for table,count in [('observations',2),('window_observations',0),('weekly_window_refs',2),
                            ('tracking_observations',2),('tracking_tasks',1),('calls',1)]:
            assert ledger.db.execute(f'select count(*) from {table}').fetchone()[0]==count
        assert ledger.db.execute('''select count(*) from weekly_window_refs r
            left join observations o on o.id=r.observation_id where o.id is null''').fetchone()[0]==0
        _advance_quota_fixture(ledger)
        _committed_quota_fixture(reference);_advance_quota_fixture(reference)
        assert ledger.report('h',132)==reference.report('h',132)
        assert tuple(ledger.db.execute('select at,complete_at from state').fetchone())==(131,131)
        assert tuple(ledger.db.execute('select start,end from tracking_gaps').fetchone())==(111,131)
    finally:
        ledger.close();reference.close()


def test_duplicate_heartbeat_does_not_dirty_storage_and_new_health_survives_reopen(tmp_path):
    import sqlite3
    path=tmp_path/'ledger.sqlite';ledger=QuotaLedger(path)
    watcher=sqlite3.connect(path)
    tracking.enable(ledger.db,'h',100)
    engine=SimpleNamespace(sessions={})
    snapshot=dict(homes=['h'],sessions=[],ts=101,usage_collection_complete=True,
                  index=dict(loading=False,usage_complete=True))
    try:
        ledger.sync(engine,snapshot)
        version=watcher.execute('pragma data_version').fetchone()[0]
        ledger.sync(engine,snapshot)
        assert watcher.execute('pragma data_version').fetchone()[0]==version
        ledger.sync(engine,{**snapshot,'ts':103,'usage_collection_complete':False,
                           'index':dict(loading=True,usage_complete=False)})
        assert tuple(watcher.execute('select at,complete from tracking_health').fetchone())==(103,0)
        assert tuple(watcher.execute('select at,loading,complete_at from state').fetchone())==(103,1,101)
        ledger.close();ledger=QuotaLedger(path)
        ledger.sync(engine,{**snapshot,'ts':120})
        assert tuple(watcher.execute('select at,complete from tracking_health').fetchone())==(120,1)
        assert tuple(watcher.execute('select at,loading,complete_at from state').fetchone())==(120,0,120)
        assert tuple(watcher.execute('select start,end from tracking_gaps').fetchone())==(103,120)
    finally:
        watcher.close();ledger.close()


def test_recovery_snapshot_cannot_rewind_durable_quota_activity_or_coverage(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    reference=QuotaLedger(tmp_path/'reference.sqlite')
    empty=QuotaLedger(tmp_path/'empty.sqlite')
    try:
        _committed_quota_fixture(ledger)
        expected=ledger.report('h',112)
        engine=SimpleNamespace(sessions={('h','s'):{'revision':200,'prepared':{'history':[]}}})
        fallback=dict(homes=['h'],ts=105,collection_recovery=True,
            usage_collection_complete=False,index=dict(loading=True,usage_complete=False),
            usage_errors=['cached diagnostic'],sessions=[dict(home='h',id='s',
                turn_records={'one':dict(started_at=103,ended_at=None)})])
        for at in (105,111):
            ledger.sync(engine,{**fallback,'ts':at})
            assert ledger.report('h',112)==expected
            assert tuple(ledger.db.execute('select at,complete from tracking_health').fetchone())==(111,1)
            assert tuple(ledger.db.execute('select at,complete_at from state').fetchone())==(111,111)
            assert ledger.db.execute('select end from tracking_tasks').fetchone()[0]==108
        _advance_quota_fixture(ledger)
        _committed_quota_fixture(reference);_advance_quota_fixture(reference)
        assert ledger.report('h',132)==reference.report('h',132)
        assert tuple(ledger.db.execute('select start,end from tracking_gaps').fetchone())==(111,131)
        # A cache can still initialize a ledger that has no newer durable evidence.
        tracking.enable(empty.db,'h',100)
        empty.sync(engine,fallback)
        assert tuple(empty.db.execute('select at,complete_at from state').fetchone())==(105,None)
        assert tuple(empty.db.execute('select start,end from tracking_tasks').fetchone())==(103,None)
    finally:
        ledger.close();reference.close();empty.close()


def test_pending_catalog_member_cannot_rewrite_committed_calls_or_tasks(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        _committed_quota_fixture(ledger)
        ledger.db.execute('INSERT INTO coverage_issues VALUES(?,?,?,?,?)',('h','s',103,108,'stored uncertainty'))
        ledger.db.execute('UPDATE state SET unclassified=1 WHERE home=?',('h',))
        ledger.db.commit()
        calls=[tuple(row) for row in ledger.db.execute('SELECT * FROM calls')]
        tasks=[tuple(row) for row in ledger.db.execute('SELECT * FROM tracking_tasks')]
        coverage=[tuple(row) for row in ledger.db.execute('SELECT * FROM coverage_issues')]
        engine=SimpleNamespace(sessions={('h','s'):{'revision':200,'prepared':{'history':[]}}})
        pending=dict(homes=['h'],ts=112,usage_collection_complete=False,
            index=dict(loading=True,usage_complete=False),sessions=[dict(home='h',id='s',
                _collection_pending=True,turn_records={'one':dict(started_at=103,ended_at=None)})])
        ledger.sync(engine,pending)
        assert [tuple(row) for row in ledger.db.execute('SELECT * FROM calls')]==calls
        assert [tuple(row) for row in ledger.db.execute('SELECT * FROM tracking_tasks')]==tasks
        assert [tuple(row) for row in ledger.db.execute('SELECT * FROM coverage_issues')]==coverage
        assert ledger.db.execute('SELECT unclassified FROM state WHERE home=?',('h',)).fetchone()[0]==1
        assert tuple(ledger.db.execute('SELECT at,complete FROM tracking_health').fetchone())==(112,0)
        complete={**pending,'sessions':[],'usage_collection_complete':True,'index':dict(loading=False,usage_complete=True)}
        ledger.sync(SimpleNamespace(sessions={}),complete)
        assert not list(ledger.db.execute('SELECT * FROM coverage_issues'))
        assert ledger.db.execute('SELECT unclassified FROM state WHERE home=?',('h',)).fetchone()[0]==0
    finally:ledger.close()


def test_forward_continuous_account_consumption_and_cost_survive_reopen(tmp_path):
    path=tmp_path/'ledger.sqlite'
    ledger=QuotaLedger(path)
    tracking.enable(ledger.db,'h',100)
    activity(ledger,101,{},wire=[dict(home='h',attempt='old',request_observed_at=90,
                                   status='created',ts=90)])
    observe(ledger,102,70)
    turns={'one':dict(started_at=103,ended_at=108)}
    activity(ledger,109,turns,[107])
    observe(ledger,110,70)
    observe(ledger,112,65)  # account usage is retained even with no active local task
    turns['two']=dict(started_at=114,ended_at=118)
    activity(ledger,119,turns,[107,117])
    observe(ledger,120,64)
    activity(ledger,121,turns,[107,117])
    report=ledger.report('h',122)
    result=quota_statistics(report)
    assert result['delta']==6
    expected=ledger.db.execute('select sum(cost) from calls').fetchone()[0]
    assert result['cost']==pytest.approx(expected)
    assert result['per_percent']==pytest.approx(expected/6)
    assert report['tracking']['phase']=='on'
    assert report['tracking']['pending_responses']==[]
    assert len(report['cycles'])==1
    ledger.close()
    ledger=QuotaLedger(path)
    assert quota_statistics(ledger.report('h',122))['per_percent']==pytest.approx(expected/6)
    ledger.close()


@pytest.mark.parametrize('packed',(False,True))
def test_changed_cycle_reuses_closed_accounting_and_matches_full_rebuild(tmp_path,packed):
    path=tmp_path/'incremental.sqlite'
    ledger=QuotaLedger(path,pack_history=packed)
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,90),(110,80),(120,100),(130,90),(140,100),(150,90)):
            observe(ledger,at,remaining)
        activity(ledger,160,{},[107,127,147])
        first=ledger.report('h',160)
        assert ledger._cycle_rebuilds==3
        prepared=getattr(ledger,'_request_inputs',None)
        if packed:assert prepared and prepared[-1]<=ledger.REQUEST_CACHE_BYTES
        observe(ledger,152,85)
        appended=ledger.report('h',161)
        assert ledger._cycle_rebuilds==1
        if packed:assert ledger._request_inputs is prepared
        assert appended==ledger._report('h',161)
        assert appended['cycles'][-1]['cost']==first['cycles'][-1]['cost']
        ledger.db.execute('UPDATE calls SET cost=cost*2 WHERE ts=107');ledger.db.commit()
        corrected=ledger.report('h',162)
        assert ledger._cycle_rebuilds==1
        if packed:assert ledger._request_inputs is not prepared
        assert corrected==ledger._report('h',162)
        assert corrected['cycles'][-1]['cost']==first['cycles'][-1]['cost']*2
        # A rolled-back revision may be reused by another transaction.
        ledger.db.execute('UPDATE calls SET cost=0 WHERE ts=127')
        ledger.report('h',162);ledger.db.rollback()
        assert ledger.report('h',162)==corrected
    finally:ledger.close()
    reopened=QuotaLedger(path)
    try:assert reopened.report('h',162)==corrected
    finally:reopened.close()


def test_worker_reuses_unchanged_statistics_and_rechecks_requests_and_exclusions(tmp_path,monkeypatch):
    from cachemonitor import quota_attribution, quota_cycles
    from cachemonitor.quota_view import prepare_quota_view, period_series
    from cachemonitor.quota_worker import QuotaComputation
    # Keep the full-report fallback's reuse contract independent of the closed
    # projection. Its restart, range queries and exact detail are checked above.
    worker=QuotaComputation(tmp_path/'statistics.sqlite','h',
        ledger_factory=lambda path:QuotaLedger(path,pack_history=True,weekly_history_only=True))
    ledger=worker._ledger(writable=True);attributed=[]
    original=quota_attribution.local_request_intervals
    def counted(cycle,*args,**kwargs):
        attributed.append(cycle['start'])
        return original(cycle,*args,**kwargs)
    monkeypatch.setattr(quota_attribution,'local_request_intervals',counted)
    def check(now):
        raw=ledger.report('h',now);attributed.clear()
        worker._prepare(raw,(worker.ledger_generation,ledger._report_generation))
        count=len(attributed)
        view=next(reversed(worker.reports.values()))['view']
        reference=prepare_quota_view(raw)
        assert view['lifetime']==reference['lifetime']
        assert view['summaries']==reference['summaries']
        assert [period_series(p) for p in view['periods']]==[p['series'] for p in reference['periods']]
        return count
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,90),(110,80),(120,100),(130,90),(140,100),(150,90)):
            observe(ledger,at,remaining)
        activity(ledger,160,{},[107,127,147])
        assert check(160)==3
        observe(ledger,152,85)
        assert check(161)==1
        ledger.db.execute('UPDATE calls SET cost=cost*2 WHERE ts=107');ledger.db.commit()
        assert check(162)==1
        ledger.db.execute('INSERT INTO tracking_wire VALUES(?,?,?,?,?,?)',('h','late','late',125,148,'completed'))
        ledger.db.commit();check(163)
        monkeypatch.setattr(quota_cycles.time,'time',lambda:164)
        excluded=ledger.exclude_interval('h',120.1,130.1)
        check(164)
        ledger.restore_exclusion('h',excluded['id']);check(165)
        monkeypatch.setattr(quota_cycles._PreparedQuotaStatistics,'MAX_BYTES',1)
        observe(ledger,154,80);check(166)
    finally:worker.close()


def test_packed_interval_boundaries_preserve_values_and_isolate_neighbor_corrections():
    from cachemonitor.quota_cycles import _PackedRows
    rows=[dict(uid=str(i),at=float(i),cost=i/100) for i in range(1025)]
    packed=_PackedRows(rows)
    for interval in (slice(2,1025),slice(510,514),slice(512,1024),slice(-5,None),slice(None,None,-1)):
        assert packed[interval]==rows[interval]
    changed=[dict(row) for row in rows]
    changed[510]['cost']=99
    corrected=_PackedRows(changed)
    assert packed.signature(512,900)==corrected.signature(512,900)
    assert packed.signature(508,512)!=corrected.signature(508,512)


@pytest.mark.parametrize('cache_budget,packed',[(64*1024*1024,False),(1024,False),(64*1024*1024,True),(1,True)])
def test_input_cache_catches_late_deletes_and_recovers_after_change_ring_overflow(tmp_path,monkeypatch,cache_budget,packed):
    monkeypatch.setattr(QuotaLedger,'INPUT_CACHE_BYTES',cache_budget)
    monkeypatch.setattr(QuotaLedger,'REQUEST_CACHE_BYTES',cache_budget)
    monkeypatch.setattr(tracking,'REPLAY_CACHE_BYTES',cache_budget)
    ledger=QuotaLedger(tmp_path/'changes.sqlite',pack_history=packed)
    try:
        tracking.enable(ledger.db,'h',100)
        observe(ledger,102,90);observe(ledger,110,80)
        activity(ledger,120,{},[107])
        ledger.report('h',120)
        ledger.db.execute('DELETE FROM calls WHERE ts=107');ledger.db.commit()
        assert ledger.report('h',121)==ledger._report('h',121)
        # More changes than the durable ring must fall back to authoritative SQL.
        for at in range(122,500):observe(ledger,at,79-(at-122)/100)
        assert ledger.db.execute('SELECT count(*) FROM report_changes').fetchone()[0]<=1024
        assert ledger.report('h',501)==ledger._report('h',501)
    finally:ledger.close()


def test_tracking_resume_preserves_reset_recovery_and_rechecks_old_edits(tmp_path,monkeypatch):
    import json
    ledger=QuotaLedger(tmp_path/'tracking-resume.sqlite',pack_history=True)
    replayed=[];original=tracking.TrackingState.observe
    def observe_state(state,value):
        replayed.append(value.sequence)
        return original(state,value)
    monkeypatch.setattr(tracking.TrackingState,'observe',observe_state)
    def check(now,expected_replay):
        replayed.clear()
        actual=ledger.report('h',now)
        assert len(replayed)==expected_replay
        assert actual==ledger._report('h',now)
        return actual
    try:
        tracking.enable(ledger.db,'h',100);activity(ledger,101,{})
        for at,remaining in ((102,70),(104,65),(110,80),(112,85)):observe(ledger,at,remaining)
        first=check(113,4)
        # Recovery can raise the same reset's baseline across report refreshes.
        observe(ledger,114,90)
        recovered=check(115,1)
        assert len(recovered['cycles'])==len(first['cycles'])
        assert recovered['cycles'][0]['start']==114.1
        observe(ledger,116,89);check(117,1)
        tracking.enable(ledger.db,'h',120,False);check(121,6)
        tracking.enable(ledger.db,'h',122,True);observe(ledger,124,88);check(125,7)
        row=ledger.db.execute('SELECT * FROM tracking_observations ORDER BY received LIMIT 1').fetchone()
        quota=tracking.quota_payloads.PayloadReader(ledger.db).decode(row);quota['windows']['weekly']['used_percent']=40
        ledger.db.execute('UPDATE tracking_observations SET data=? WHERE seq=?',(json.dumps(quota),row['seq']))
        ledger.db.execute('UPDATE observations SET used=40 WHERE at=102.1');ledger.db.commit()
        check(126,7)
        ledger.db.execute('DELETE FROM tracking_observations WHERE seq=?',(row['seq'],));ledger.db.commit()
        check(127,6)
    finally:ledger.close()


def test_cached_report_discovers_future_account_membership_in_another_home(tmp_path):
    ledger=QuotaLedger(tmp_path/'future-membership.sqlite',pack_history=True)
    try:
        for home in ('h','other'):tracking.enable(ledger.db,home,100)
        observe(ledger,110,80)
        observe(ledger,200,79,home='other')
        ledger.db.commit()
        before=ledger.report('h',now=150)
        assert before['tracking']['homes']==['h']
        after=ledger.report('h',now=201)
        assert set(after['tracking']['homes'])=={'h','other'}
        assert after==ledger._report('h',now=201)
    finally:ledger.close()


def test_packed_history_rechecks_both_times_of_legacy_window_references(tmp_path,monkeypatch):
    monkeypatch.setattr('cachemonitor.quota_cycles.hash',lambda value:0,raising=False)
    ledger=QuotaLedger(tmp_path/'linked-history.sqlite',pack_history=True)
    try:
        tracking.enable(ledger.db,'h',100)
        observe(ledger,102,90);observe(ledger,110,80);activity(ledger,111,{})
        reference=ledger.db.execute('''SELECT r.window_id,r.observation_id FROM weekly_window_refs r
            JOIN observations o ON o.id=r.observation_id ORDER BY o.at DESC LIMIT 1''').fetchone()
        ledger.db.execute('INSERT INTO window_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (reference['window_id'],'h',101,'history','a','pro','codex','weekly',20,1000000,10080,'[]'))
        ledger.db.commit()
        assert 101 not in [r['at'] for r in ledger.report('h',120)['history']]
        ledger.db.execute('DELETE FROM weekly_window_refs WHERE window_id=?',(reference['window_id'],));ledger.db.commit()
        deleted=ledger.report('h',121)
        assert 101 in [r['at'] for r in deleted['history']]
        assert deleted==ledger._report('h',121)
        ledger.db.execute('INSERT INTO weekly_window_refs VALUES(?,?)',tuple(reference));ledger.db.commit()
        restored=ledger.report('h',122)
        assert 101 not in [r['at'] for r in restored['history']]
        assert restored==ledger._report('h',122)
        ledger.db.execute('UPDATE observations SET at=125 WHERE id=?',(reference['observation_id'],));ledger.db.commit()
        future=ledger.report('h',123)
        assert 101 in [r['at'] for r in future['history']]
        assert future==ledger._report('h',123)
        assert ledger.report('h',126)==ledger._report('h',126)
    finally:ledger.close()


def test_streamed_request_join_preserves_completion_order_and_wire_precedence(tmp_path):
    ledger=QuotaLedger(tmp_path/'request-joins.sqlite')
    try:
        for home,uid,at,cost in [('a','r1',100,1),('b','r1',100,2),('a','r2',200,3),
                                 ('a','r3',300,4),('a','r4',400,None),('b','r4',400,5),
                                 ('a','r5',500,6),('a','r6',500,7)]:
            ledger.db.execute('''INSERT INTO calls(home,uid,sid,ts,model,input,cached,written,output,reasoning,
                cost,price_id,signature,service_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (home,uid,'session',at,'test',1,0,0,1,0,cost,'price','signature','Standard'))
        ledger.db.executemany('INSERT INTO tracking_wire VALUES(?,?,?,?,?,?)',[
            ('a','01','r2',190,240,'completed'),('a','02','r2',40,50,'completed'),
            ('a','03','r3',None,None,'created'),('a','04','r3',195,210,'completed'),
            ('a','05','r2',None,None,'failed'),('a','06','r1',90,210,'completed'),
            ('a','07','r6',None,None,'created'),('a','08','',400,401,'completed'),
            ('b','01','r4',390,420,'completed'),('b','02','r1',95,205,'completed')])
        ledger.db.executemany('INSERT INTO tracking_call_boundaries VALUES(?,?,?)',
                             [('a','r2',175),('a','r5',450),('a','r6',480)])
        ledger.db.commit()
        ledger._input_cache={};ledger._input_changes=[]
        for scope in (['a'],['a','b']):
            expected=[('r2','a',50,3,190,190),('r1','a',205 if len(scope)>1 else 210,1,90,90),
                      ('r3','a',210,4,195,195),
                      ('r4','b',420,5,390,390) if len(scope)>1 else ('r4','a',400,None,None,None),
                      ('r5','a',500,6,450,None),('r6','a',500,7,None,None)]
            windows=[dict(uid='r3',start=195,end=210),dict(uid='r6',start=None,end=None),
                     dict(uid='r2',start=40,end=50),dict(uid='r1',start=90,end=210)]
            if len(scope)>1:windows.append(dict(uid='r4',start=390,end=420))
            for packed in (False,True):
                ledger._pack_history=packed
                calls,times,actual_windows=ledger._report_calls(scope,0,True)
                assert [tuple(row[k] for k in ('uid','home','ts','cost','request_start','local_request_start'))
                        for row in calls]==expected
                assert list(times)==[row[2] for row in expected]
                assert list(actual_windows)==windows
            # A cost correction changes accounting but not request lifetimes.
            ledger.db.execute("UPDATE calls SET cost=11 WHERE home='a' AND uid='r1'");ledger.db.commit()
            ledger._input_changes=[('calls',100)]
            calls,_,same_windows=ledger._report_calls(scope,0,True)
            assert next(row['cost'] for row in calls if row['uid']=='r1')==11
            assert same_windows is actual_windows and list(same_windows)==windows
            ledger.db.execute("UPDATE tracking_wire SET start=60,end=70 WHERE home='a' AND identity='02'");ledger.db.commit()
            ledger._input_changes=[('tracking_wire',40)]
            calls,_,changed_windows=ledger._report_calls(scope,0,True)
            assert next(row['ts'] for row in calls if row['uid']=='r2')==70
            assert next(row for row in changed_windows if row['uid']=='r2')==dict(uid='r2',start=60,end=70)
            ledger.db.execute("UPDATE calls SET cost=1 WHERE home='a' AND uid='r1'")
            ledger.db.execute("UPDATE tracking_wire SET start=40,end=50 WHERE home='a' AND identity='02'");ledger.db.commit()
            ledger._input_changes=[('calls',100),('tracking_wire',40)]
            # Filtering occurs before completion correction, including a late
            # completion that moves a retained call before the requested cutoff.
            calls,_,_=ledger._report_calls(scope,150,True)
            assert [row['uid'] for row in calls]==['r2','r3','r4','r5','r6']
    finally:ledger.close()


def test_streamed_history_preserves_legacy_duplicates_nulls_and_reference_scope(tmp_path):
    ledger=QuotaLedger(tmp_path/'history-sources.sqlite')
    try:
        observations=[('o100','h',100,10,1000),('o101','h',101,None,None),
            ('o102a','h',102,15,1000),('o102b','h',102,15,1000),
            ('o102ref','h',102,30,1000),('o103','h',103,20,1000),
            ('o103unreferenced','h',103,20,1000),('other','other',105,25,1000)]
        ledger.db.executemany('INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)',
            [(name,home,at,'history','a',used,reset,10080,'[]') for name,home,at,used,reset in observations])
        ledger.db.execute('INSERT INTO observation_context VALUES(?,?,?)',('o102ref','pro','codex'))
        ledger.db.executemany('INSERT INTO weekly_window_refs VALUES(?,?)',
            [('o102a','o102ref'),('ref103','o103'),('ref103b','o103'),('other-ref','other')])
        ledger.db.executemany('INSERT INTO window_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',[
            ('w100','h',100,'history','a','pro','codex','weekly',10,1000,10080,'[]'),
            ('w101','h',101,'history','a','','','weekly',None,None,10080,'[]'),
            ('ref103','h',104,'history','a','pro','codex','five_hour',7,500,300,'[]')])
        ledger.db.commit()
        tables={row[0] for row in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for start,end,count in ((90,110,7),(104,110,1),(100,103,7),(102,102,3)):
            ledger._pack_history=False
            expected=ledger._report_history('h',start,end,tables)
            ledger._pack_history=True
            actual=list(ledger._report_history('h',start,end,tables))
            assert actual==expected and len(actual)==count
            assert ledger.db.execute('PRAGMA cache_size').fetchone()[0]==-ledger.CACHE_KIB
            if start==102:assert [row['used'] for row in actual if row['id']=='o102a']==[30,15]
    finally:ledger.close()


def test_same_account_homes_count_quota_and_replayed_calls_once(tmp_path):
    ledger=QuotaLedger(tmp_path/'multi.sqlite')
    for home in ('h','other'):
        tracking.enable(ledger.db,home,100)
        activity(ledger,101,{},home=home)
        observe(ledger,102,70,home=home)
    turns={'one':dict(started_at=103,ended_at=108)}
    for home in ('h','other'):
        activity(ledger,109,turns,[107],home=home)
        observe(ledger,110,69,home=home)
        activity(ledger,111,turns,[107],home=home)
    for home in ('h','other'):
        stats=quota_statistics(ledger.report(home,112))
        assert stats['calls']==1
        assert stats['delta']==1
    ledger.close()


def test_late_cost_revises_original_interval_without_importing_old_calls(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    tracking.enable(ledger.db,'h',100)
    activity(ledger,101,{})
    observe(ledger,102,70)
    turns={'one':dict(started_at=103,ended_at=108)}
    activity(ledger,109,turns,[50])
    observe(ledger,110,69)
    activity(ledger,111,turns,[50,107])
    stats=quota_statistics(ledger.report('h',112))
    assert stats['calls']==1
    assert stats['delta']==1
    assert stats['cost']==ledger.db.execute('select cost from calls where ts=107').fetchone()[0]
    ledger.close()


def test_query_failure_keeps_account_consumption_visible_and_recovery_catches_up(tmp_path):
    ledger=QuotaLedger(tmp_path/'gap.sqlite')
    tracking.enable(ledger.db,'h',100)
    activity(ledger,101,{})
    observe(ledger,102,70)
    turns={'one':dict(started_at=103,ended_at=None)}
    activity(ledger,104,turns)
    observe(ledger,105,69)
    tracking.failed_observation(ledger.db,'h',106)
    turns['one']['ended_at']=108
    activity(ledger,109,turns,[107])
    observe(ledger,120,50)
    activity(ledger,121,turns,[107])
    report=ledger.report('h',122)
    assert quota_statistics(report)['account_delta']==20
    assert quota_statistics(report)['delta']==19  # Only the completion timestamp is known for this fixture.
    assert quota_statistics(report)['calls']==1
    assert not report['tracking']['lookup_failed']
    assert not any(c['blocked'] for c in report['cycles'])
    ledger.close()


def test_monitoring_off_preserves_results_and_on_requires_new_baseline(tmp_path):
    ledger=QuotaLedger(tmp_path/'toggle.sqlite')
    tracking.enable(ledger.db,'h',100)
    activity(ledger,101,{})
    observe(ledger,102,70)
    turns={'one':dict(started_at=103,ended_at=108)}
    activity(ledger,109,turns,[107]);observe(ledger,110,69)
    activity(ledger,111,turns,[107])
    before=quota_statistics(ledger.report('h',112))['cost']
    tracking.enable(ledger.db,'h',112,enabled=False)
    count=ledger.db.execute('select count(*) from tracking_observations').fetchone()[0]
    observe(ledger,115,50)
    assert ledger.db.execute('select count(*) from tracking_observations').fetchone()[0]==count
    report=ledger.report('h',116)
    assert report['tracking']['phase']=='off'
    assert quota_statistics(report)['cost']==before
    tracking.enable(ledger.db,'h',120,enabled=True)
    turns['off_work']=dict(started_at=114,ended_at=118)
    activity(ledger,121,turns,[107,117]);observe(ledger,122,50)
    turns['three']=dict(started_at=123,ended_at=128)
    activity(ledger,129,turns,[107,117,127]);observe(ledger,130,49)
    activity(ledger,131,turns,[107,117,127])
    stats=quota_statistics(ledger.report('h',132))
    assert stats['delta']==2 and stats['calls']==2
    assert stats['cost']==pytest.approx(before*2)
    ledger.close()


def test_usage_record_after_end_lookup_uses_observed_request_completion(tmp_path):
    ledger=QuotaLedger(tmp_path/'late.sqlite')
    tracking.enable(ledger.db,'h',100)
    activity(ledger,101,{})
    observe(ledger,102,70)
    turns={'one':dict(started_at=103,ended_at=108)}
    wire=[dict(home='h',attempt='attempt',response_id='111',ts=103,
               request_observed_at=103,completed_observed_at=108,status='completed')]
    activity(ledger,109,turns,wire=wire);observe(ledger,110,69)
    activity(ledger,111,turns,wire=wire)
    assert quota_statistics(ledger.report('h',111))['per_percent'] is None
    assert quota_statistics(ledger.report('h',111))['observed_calls']==0
    assert quota_statistics(ledger.report('h',111))['account_delta']==1
    assert quota_statistics(ledger.report('h',111))['attribution']['pending_delta']==1
    activity(ledger,112,turns,[111],wire=wire)
    stats=quota_statistics(ledger.report('h',113))
    assert stats['calls']==1 and stats['delta']==1
    assert ledger.db.execute('select ts from calls').fetchone()[0]==111
    assert stats['intervals'][0]['cost_rows'][0]['ts']==108
    ledger.close()


def test_new_policy_preserves_history_but_never_recalculates_old_observations(tmp_path):
    path=tmp_path/'policy.sqlite'
    ledger=QuotaLedger(path)
    ledger.db.execute('insert into tracking_config values(?,?)',('h',10))
    ledger.db.execute('insert into tracking_controls values(?,?,?)',('h',10,1))
    ledger.db.commit()
    activity(ledger,20,{'orphan':dict(started_at=11,ended_at=None)},[19])
    observe(ledger,21,90);observe(ledger,31,80)
    count=ledger.db.execute('select count(*) from tracking_observations').fetchone()[0]
    assert quota_statistics(ledger.report('h',99))['observed_delta']==0
    tracking.enable(ledger.db,'h',100)
    assert quota_statistics(ledger.report('h',101))['observed_delta']==0
    assert ledger.db.execute('select count(*) from tracking_observations').fetchone()[0]==count
    assert ledger.db.execute('select started from tracking_policies where version=?',('legacy',)).fetchone()[0]==10
    observe(ledger,102,70);observe(ledger,110,69.75)
    activity(ledger,111,{'orphan':dict(started_at=11,ended_at=None)},[19,107])
    assert quota_statistics(ledger.report('h',112))['observed_delta']==pytest.approx(.25)
    ledger.close()
    ledger=QuotaLedger(path)
    tracking.enable(ledger.db,'h',200)
    assert ledger.report('h',201)['tracking']['started']==100
    assert quota_statistics(ledger.report('h',201))['observed_calls']==1
    ledger.close()


def test_activation_in_middle_of_request_counts_consumption_but_not_entire_old_call(tmp_path):
    ledger=QuotaLedger(tmp_path/'active.sqlite')
    tracking.enable(ledger.db,'h',100)
    turns={'active':dict(started_at=90,ended_at=None)}
    activity(ledger,101,turns)
    observe(ledger,102,70)
    rows=[dict(key='old',ts=108,turn='active',model='gpt-6-astra',input=1000,cached=0,written=0,
               output=10,reasoning=0,service_tier='Standard'),
          dict(key='new',ts=115,turn='active',model='gpt-6-astra',input=1000,cached=0,written=0,
               output=10,reasoning=0,service_tier='Standard')]
    engine=SimpleNamespace(sessions={('h','s'):{'revision':1,'prepared':{'history':rows,'turn_records':turns}}})
    ledger.sync(engine,dict(homes=['h'],ts=116,usage_collection_complete=True,
        index=dict(loading=False,usage_complete=True,last_usage_success=116),
        sessions=[dict(home='h',id='s',turn_records=turns)]))
    observe(ledger,117,69)
    stats=quota_statistics(ledger.report('h',118))
    assert stats['observed_delta']==1
    assert stats['calls']==1 and stats['boundary_excluded_calls']==1
    assert stats['cost']==ledger.db.execute("select cost from calls where uid='new'").fetchone()[0]
    assert stats['per_percent']==stats['cost']
    ledger.close()


def test_reset_does_not_bridge_and_zero_observation_stays_visible_without_calls(tmp_path):
    ledger=QuotaLedger(tmp_path/'reset.sqlite')
    tracking.enable(ledger.db,'h',100)
    observe(ledger,102,0);observe(ledger,110,0)
    stats=quota_statistics(ledger.report('h',111))
    assert stats['observed_delta']==0 and stats['per_percent'] is None
    q=dict(source='live',account='a',plan_type='pro',bucket='codex',requested_at=120,
           observed_at=120.1,windows=dict(weekly=dict(used_percent=0,window_minutes=10080,resets_at=2000000)))
    ledger.observe('h',q);tracking.observe(ledger.db,'h',q)
    q={**q,'requested_at':130,'observed_at':130.1,'windows':{'weekly':{**q['windows']['weekly'],'used_percent':.1}}}
    ledger.observe('h',q);tracking.observe(ledger.db,'h',q)
    stats=quota_statistics(ledger.report('h',131))
    assert stats['account_delta']==pytest.approx(.1)
    assert stats['observed_delta']==0
    assert stats['per_percent'] is None
    assert stats['total']==0 and len(ledger.report('h',131)['cycles'])==2
    ledger.close()


def test_account_observations_are_not_delayed_by_incomplete_usage_index(tmp_path):
    ledger=QuotaLedger(tmp_path/'index.sqlite')
    tracking.enable(ledger.db,'h',100)
    observe(ledger,102,70)
    ledger.sync(SimpleNamespace(sessions={}),dict(homes=['h'],sessions=[],ts=105,
                usage_collection_complete=False,index=dict(loading=True,usage_complete=False)))
    observe(ledger,110,69)
    stats=quota_statistics(ledger.report('h',111))
    assert stats['account_delta']==1 and stats['cost'] is None
    assert stats['observed_delta']==0
    assert stats['per_percent'] is None and not stats['pending']
    tracking.failed_observation(ledger.db,'h',112)
    assert ledger.report('h',113)['tracking']['lookup_failed']
    assert quota_statistics(ledger.report('h',113))['account_delta']==1
    ledger.close()


def test_distinct_parent_and_subagent_costs_count_once_per_real_response(tmp_path):
    ledger=QuotaLedger(tmp_path/'agents.sqlite')
    tracking.enable(ledger.db,'h',100)
    observe(ledger,102,70)
    def row(key,at):
        return dict(key=key,ts=at,model='gpt-6-astra',input=1000,cached=0,written=0,
                    output=10,reasoning=0,service_tier='Fast')
    parent=[row('parent-response',104),row('child-a-response',106)]
    child_a=[row('child-a-response',106)]
    child_b=[row('child-b-response',108)]
    engine=SimpleNamespace(sessions={('h',sid):{'revision':1,'prepared':{'history':rows}}
        for sid,rows in [('parent',parent),('child-a',child_a),('child-b',child_b)]})
    snapshot=dict(homes=['h'],ts=109,usage_collection_complete=True,
                  index=dict(loading=False,usage_complete=True,last_usage_success=109),
                  sessions=[dict(home='h',id=sid,source='subagent' if sid!='parent' else 'app',turn_records={})
                            for sid in ('parent','child-a','child-b')])
    ledger.sync(engine,snapshot)
    observe(ledger,110,69)
    stats=quota_statistics(ledger.report('h',111))
    assert stats['calls']==3 and stats['observed_priced_calls']==3
    assert stats['cost']==pytest.approx(ledger.db.execute('select sum(cost) from calls').fetchone()[0])
    assert ledger.db.execute('select count(*) from calls').fetchone()[0]==3
    assert {m['service_tier'] for m in stats['intervals'][0]['models']}=={'Fast'}
    ledger.close()


def test_account_change_does_not_add_previous_zero_delta_cost_to_new_account(tmp_path):
    ledger=QuotaLedger(tmp_path/'accounts.sqlite')
    tracking.enable(ledger.db,'h',100)
    observe(ledger,102,70)
    activity(ledger,109,{},[107]);observe(ledger,110,70)
    for at,used in ((120,10),(130,11)):
        q=dict(source='live',account='other',plan_type='pro',bucket='codex',requested_at=at,
            observed_at=at+.1,windows=dict(weekly=dict(used_percent=used,window_minutes=10080,resets_at=1000000)))
        ledger.observe('h',q);tracking.observe(ledger.db,'h',q)
    activity(ledger,131,{},[107,127])
    stats=quota_statistics(ledger.report('h',132))
    assert stats['observed_delta']==stats['delta']==1
    assert stats['observed_calls']==stats['calls']==1
    assert stats['per_percent']==ledger.db.execute('select cost from calls where ts=127').fetchone()[0]
    ledger.close()


def test_all_unpriced_calls_never_display_zero_cost_or_zero_ratio(tmp_path):
    ledger=QuotaLedger(tmp_path/'unpriced.sqlite')
    tracking.enable(ledger.db,'h',100)
    observe(ledger,102,70)
    rows=[dict(key='missing-mode',ts=107,model='gpt-6-astra',input=1000,cached=0,written=0,
               output=10,reasoning=0,service_tier='미확인')]
    engine=SimpleNamespace(sessions={('h','s'):{'revision':1,'prepared':{'history':rows}}})
    ledger.sync(engine,dict(homes=['h'],ts=111,usage_collection_complete=True,
        index=dict(loading=False,usage_complete=True,last_usage_success=111),sessions=[dict(home='h',id='s',turn_records={})]))
    observe(ledger,110,69)
    stats=quota_statistics(ledger.report('h',112))
    assert stats['account_delta']==1 and stats['observed_delta']==0
    assert stats['attribution']['pending_delta']==1 and stats['observed_priced_calls']==0
    assert stats['cost'] is None and stats['observed_cost'] is None and stats['per_percent'] is None
    observe(ledger,120,68)
    clipped=quota_statistics(ledger.report('h',121),105,121)
    assert clipped['per_percent'] is None and clipped['delta']==0
    assert ledger.db.execute('select cost from calls').fetchone()[0] is None
    ledger.close()


def test_partial_priced_calls_wait_for_matching_usage_without_losing_known_cost(tmp_path):
    ledger=QuotaLedger(tmp_path/'partial.sqlite')
    tracking.enable(ledger.db,'h',100)
    observe(ledger,102,70)
    rows=[dict(key=str(i),ts=at,model='gpt-6-astra',input=1000,cached=0,written=0,
               output=10,reasoning=0,service_tier=tier) for i,(at,tier) in enumerate([(107,'Standard'),(108,'미확인')])]
    engine=SimpleNamespace(sessions={('h','s'):{'revision':1,'prepared':{'history':rows}}})
    ledger.sync(engine,dict(homes=['h'],ts=111,usage_collection_complete=True,
        index=dict(loading=False,usage_complete=True,last_usage_success=111),sessions=[dict(home='h',id='s',turn_records={})]))
    observe(ledger,110,69)
    stats=quota_statistics(ledger.report('h',112))
    assert stats['observed_priced_calls']==1 and stats['observed_calls']==1
    assert stats['account_delta']==1 and stats['observed_delta']==1
    assert stats['cost']==ledger.db.execute('select sum(cost) from calls').fetchone()[0]
    assert stats['per_percent']==stats['cost'] and stats['attribution']['pending_delta']==0
    assert ledger.db.execute('select count(cost) from calls').fetchone()[0]==1
    ledger.close()
