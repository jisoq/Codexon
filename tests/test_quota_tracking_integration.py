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


def test_changed_cycle_reuses_closed_accounting_and_matches_full_rebuild(tmp_path):
    path=tmp_path/'incremental.sqlite'
    ledger=QuotaLedger(path)
    try:
        tracking.enable(ledger.db,'h',100)
        for at,remaining in ((102,90),(110,80),(120,100),(130,90),(140,100),(150,90)):
            observe(ledger,at,remaining)
        activity(ledger,160,{},[107,127,147])
        first=ledger.report('h',160)
        assert ledger._cycle_rebuilds==3
        observe(ledger,152,85)
        appended=ledger.report('h',161)
        assert ledger._cycle_rebuilds==1
        assert appended==ledger._report('h',161)
        assert appended['cycles'][-1]['cost']==first['cycles'][-1]['cost']
        ledger.db.execute('UPDATE calls SET cost=cost*2 WHERE ts=107');ledger.db.commit()
        corrected=ledger.report('h',162)
        assert ledger._cycle_rebuilds==1
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


def test_input_cache_catches_late_deletes_and_recovers_after_change_ring_overflow(tmp_path):
    ledger=QuotaLedger(tmp_path/'changes.sqlite')
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


def test_cached_report_discovers_future_account_membership_in_another_home(tmp_path):
    ledger=QuotaLedger(tmp_path/'future-membership.sqlite')
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
