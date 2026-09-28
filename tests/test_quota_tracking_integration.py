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
