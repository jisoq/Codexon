from types import SimpleNamespace

from cachemonitor.quota_cycles import QuotaLedger, quota_statistics


def observe(ledger, at, used, **extra):
    value = {'observed_at':at, 'source':'live', 'account':'account', 'plan_type':'pro',
             'bucket':'codex', 'windows':{'weekly':{'used_percent':used,
             'window_minutes':10080, 'resets_at':10000}}, **extra}
    ledger.observe('h',value)


def sync(ledger, times, *, at=1000, unknown=False, sessions=None):
    rows = [dict(key=str(t),ts=t,model='gpt-6-astra',input=1000,cached=0,written=0,
                 output=10,reasoning=0,service_tier='미확인' if unknown else 'Standard') for t in times]
    engine = SimpleNamespace(sessions={('h','s'):{'revision':at,'prepared':{'history':rows}}})
    ledger.sync(engine, {'homes':['h'],'sessions':sessions or [{'home':'h','id':'s'}],
                        'ts':at,'index':{'loading':False}})


def test_rebound_preserves_calls_but_never_counts_recovery_as_consumption(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        for at,used in [(100,24),(110,1),(120,24),(150,24),(180,24),(220,27)]:observe(ledger,at,used)
        sync(ledger,[115,140,170,200])
        summary=quota_statistics(ledger.report('h',1000))
        assert summary['valid']==1 and summary['delta']==3 and summary['calls']==4
        interval=summary['intervals'][0]
        assert interval['start']==100 and interval['rejected_observations']==1
        assert interval['assumptions'] and not interval['excluded']
        assert ledger.db.execute('select count(*) from observations').fetchone()[0]==6
    finally:ledger.close()


def test_same_timestamp_conflict_is_not_an_endpoint_but_later_data_survives(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        for at,used in [(100,20),(100,1),(120,24),(130,27)]:observe(ledger,at,used)
        sync(ledger,[110,125])
        summary=quota_statistics(ledger.report('h',1000))
        assert summary['valid']==1 and summary['delta']==3 and summary['calls']==1
        assert summary['intervals'][0]['start']==120
    finally:ledger.close()


def test_unknown_mode_is_an_explicit_price_assumption_without_repricing(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        observe(ledger,100,10);observe(ledger,200,15)
        sync(ledger,[150],unknown=True)
        before=list(ledger.db.execute('select cost,service_tier from calls'))
        summary=quota_statistics(ledger.report('h',1000))
        assert summary['valid']==0 and summary['per_percent'] is None
        assert summary['intervals'][0]['status']=='가정 필요'
        assumed=quota_statistics(ledger.report('h',1000),include_mode_assumptions=True)
        assert assumed['assumed']==1 and assumed['assumed_per_percent']>0
        assert assumed['valid']==0 and assumed['per_percent'] is None
        assert list(ledger.db.execute('select cost,service_tier from calls'))==before
    finally:ledger.close()


def test_only_counter_increase_edge_is_excluded_and_neighbors_are_preserved(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        for at,used in [(100,10),(200,13),(300,16),(400,20)]:observe(ledger,at,used)
        session={'home':'h','id':'s','unclassified':{'total':1110},'coverage_available':True,
                 'coverage_initial':1000,'coverage_gaps':[{'start':200,'end':300,'tokens':110}]}
        sync(ledger,[150,250,350],sessions=[session])
        summary=quota_statistics(ledger.report('h',1000))
        assert summary['valid']==2 and summary['delta']==7 and summary['calls']==2
        bad=[c for c in summary['intervals'] if c['excluded']]
        assert len(bad)==1 and (bad[0]['start'],bad[0]['end'])==(200,300)
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==3
    finally:ledger.close()


def test_return_to_an_earlier_reset_window_does_not_recount_its_high_watermark(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        observe(ledger,100,15)
        observe(ledger,110,0,windows={'weekly':{'used_percent':0,'window_minutes':10080,'resets_at':15000}})
        for at,used in [(120,1),(130,15),(140,18)]:observe(ledger,at,used)
        sync(ledger,[105,115,125,135])
        summary=quota_statistics(ledger.report('h',1000))
        assert summary['delta']==3
        assert all(r['delta']!=14 for r in summary['intervals'] if not r['excluded'])
    finally:ledger.close()


def test_unpriced_call_excludes_only_its_bracketing_observation_edge(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        for at,used in [(100,10),(200,13),(300,16),(400,20)]:observe(ledger,at,used)
        sync(ledger,[150,250,350])
        ledger.db.execute('update calls set cost=null where ts=250');ledger.db.commit()
        summary=quota_statistics(ledger.report('h',1000))
        assert summary['valid']==2 and summary['delta']==7 and summary['calls']==2
        bad=[c for c in summary['intervals'] if c['excluded']]
        assert len(bad)==1 and (bad[0]['start'],bad[0]['end'])==(200,300)
        assert '필수 토큰·단가 누락' in bad[0]['excluded']
    finally:ledger.close()


def test_small_adjacent_observations_coalesce_before_precision_threshold(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        for at,used in [(100,10),(200,10.5),(300,11),(400,11.5)]:observe(ledger,at,used)
        sync(ledger,[150,250,350])
        summary=quota_statistics(ledger.report('h',1000))
        assert summary['valid']==1 and summary['delta']==1.5 and summary['calls']==3
        assert summary['intervals'][0]['status']=='잠정'
        assert quota_statistics(ledger.report('h',100000))['intervals'][0]['status']=='잠정'
    finally:ledger.close()


def test_mode_assumption_never_restores_unknown_account_or_limit(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        observe(ledger,100,10,account='');observe(ledger,200,15,account='')
        sync(ledger,[150],unknown=True)
        summary=quota_statistics(ledger.report('h',1000),include_mode_assumptions=True)
        assert summary['valid']==0 and summary['assumed']==0 and summary['assumed_per_percent'] is None
        assert '계정 식별 미확인' in summary['intervals'][0]['excluded']
    finally:ledger.close()


def test_both_window_histories_are_persisted_without_cross_home_merge(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        five={'windows':{'five_hour':{'used_percent':50,'window_minutes':300,'resets_at':10000}},
              'observed_at':100,'source':'live','account':'a','plan_type':'pro','bucket':'codex'}
        ledger.observe('h',five);ledger.observe('another',five)
        assert ledger.report('h',200)['cycles']==[]
        assert len(ledger.report('h',200)['history'])==1
        assert ledger.report('h',200)['history'][0]['window']=='five_hour'
    finally:ledger.close()


def test_current_freshness_uses_older_but_valid_local_fallback_and_reset_state():
    from cachemonitor.quota import quota_display,select_current_quota
    direct={'source':'live','observed_at':100,'windows':{'weekly':{'used_percent':40,'resets_at':300}}}
    local={'source':'local','observed_at':90,'windows':{'weekly':{'used_percent':42,'resets_at':300}}}
    assert select_current_quota(direct,local,189) is direct
    assert select_current_quota(direct,local,190) is local
    assert quota_display(direct,'weekly',190)['remaining'] is None
    assert quota_display(local,'weekly',210)['remaining']==58
    assert quota_display(select_current_quota(direct,local,211),'weekly',211)['remaining'] is None
    assert quota_display(direct,'weekly',300)['state']=='초기화 후 확인 중'
    pending=select_current_quota(direct,local,110,baseline_after=105)
    assert quota_display(pending,'weekly',110)['state']=='초기화 후 확인 중'


def test_unlimited_requires_explicit_evidence_and_conflicting_window_is_unknown():
    from cachemonitor.quota import clean_limits,quota_display
    base={'plan_type':'pro','limit_id':'codex'}
    assert quota_display({**clean_limits(base),'observed_at':100},'five_hour',100)['state']=='미확인'
    assert quota_display({**clean_limits({**base,'unlimited_windows':['five_hour']}),'observed_at':100},'five_hour',100)['state']=='제한 없음'
    window={'window_minutes':10080,'used_percent':20,'resets_at':1000}
    conflict=clean_limits({**base,'primary':window,'secondary':{**window,'used_percent':30}})
    assert quota_display({**conflict,'observed_at':100},'weekly',100)['state']=='관측 충돌'


def test_mode_clear_inheritance_is_turn_scoped_home_isolated_and_persisted(tmp_path):
    import json
    import sqlite3
    home=str(tmp_path/'home');__import__('pathlib').Path(home).mkdir()
    database=sqlite3.connect(__import__('pathlib').Path(home)/'logs_2.sqlite')
    database.execute('create table logs(id integer primary key,thread_id text,target text,feedback_log_body text)')
    def body(turn,setting='None',start='None'):
        return f'Submission sub=Submission {{ id: "{turn}", op: TurnInput {{ request: TurnInputRequest {{ thread_settings: ThreadSettingsOverrides {{ service_tier: {setting} }}, start: TurnStartOptions {{ service_tier: {start} }} }} }} }}'
    turns=['019-test-turn-00000'+str(i) for i in range(6)]
    values=[body(turns[0],'Some(Some("priority"))'),body(turns[1]),body(turns[2],'Some(None)'),
            body(turns[3]),body(turns[4],'Some(Some("priority"))','Some("default")'),body(turns[5])]
    database.executemany('insert into logs values(?,?,?,?)',[(i+1,'s','codex_core::session::handlers',value) for i,value in enumerate(values)])
    database.commit();database.close()
    def snapshot():
        return {'homes':[home],'sessions':[{'home':home,'id':'s','usage_revision':1,'history':[{'turn':turn} for turn in turns]},
            {'home':home,'id':'other','usage_revision':1,'history':[{'turn':turns[2]}]},
            {'home':'another-home','id':'s','usage_revision':1,'history':[{'turn':turns[2]}]}]}
    ledger=QuotaLedger(tmp_path/'mode.sqlite')
    try:
        snap=snapshot();ledger.enrich_modes(snap)
        rows=snap['sessions'][0]['history']
        assert [r['service_tier'] for r in rows]==['Fast','Fast','Standard','Standard','Standard','Fast']
        assert rows[2]['request_mode_action']=='clear' and rows[2]['configured_service_tier'] is None
        assert rows[3]['request_mode_source']=='inherited_settings'
        assert rows[4]['request_mode_source']=='turn_override'
        assert all(session['history'][0]['service_tier']=='미확인' for session in snap['sessions'][1:])
        assert all('Submission' not in row[0] for row in ledger.db.execute('select data from mode_events'))
    finally:ledger.close()
    ledger=QuotaLedger(tmp_path/'mode.sqlite')
    try:
        snap=snapshot();ledger.enrich_modes(snap)
        assert snap['sessions'][0]['history'][3]['service_tier']=='Standard'
    finally:ledger.close()


def test_bare_none_without_history_is_unknown_and_same_turn_conflict_does_not_fall_back(tmp_path):
    import sqlite3
    home=str(tmp_path)
    db=sqlite3.connect(tmp_path/'logs_2.sqlite')
    db.execute('create table logs(id integer primary key,thread_id text,target text,feedback_log_body text)')
    turn='019-test-turn-00000999';unknown='019-test-turn-00000111'
    def body(turn,value):return f'Submission sub=Submission {{ id: "{turn}", op: TurnInput {{ thread_settings: ThreadSettingsOverrides {{ service_tier: {value} }} }} }}'
    for identifier,text in enumerate([body(unknown,'None'),body(turn,'Some(Some("priority"))'),body(turn,'Some(None)')]):
        db.execute('insert into logs values(?,?,?,?)',(identifier,'s','codex_core::session::handlers',text))
    db.commit();db.close()
    ledger=QuotaLedger(tmp_path/'modes.sqlite')
    try:
        rows=[{'turn':unknown},{'turn':turn,'service_tier':'Standard','service_tier_source':'settings'},
              {'turn':turn,'service_tier_source':'wire','requested_service_tier':'priority'},
              {'turn':turn,'service_tier_source':'wire','requested_service_tier':'priority','mode_conflict':True}]
        snap={'homes':[home],'sessions':[{'home':home,'id':'s','history':rows}]};ledger.enrich_modes(snap)
        assert [r['service_tier'] for r in rows]==['미확인','미확인','Fast','미확인']
        assert rows[1]['request_mode_source']=='settings_conflict' and rows[1]['mode_conflict']
    finally:ledger.close()


def test_collection_watermark_preserves_valid_intervals_during_rescan_and_source_failure(tmp_path):
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        for at,used in [(100,10),(200,15),(280,18)]:observe(ledger,at,used)
        sync(ledger,[150,250],at=220)
        engine=SimpleNamespace(sessions={})
        state={'homes':['h'],'sessions':[{'home':'h','id':'s'}],'ts':300,
               'index':{'loading':True,'usage_complete':False,'last_usage_success':220},
               'last_usage_collection_success':220,'usage_collection_complete':False}
        ledger.sync(engine,state)
        during=ledger.report('h',300)
        assert during['index_complete_at']==220 and during['index_loading']
        assert quota_statistics(during)['delta']==5
        ledger.sync(engine,{**state,'ts':310,'errors':['기록 읽기: fixture'],
            'index':{'loading':False,'usage_complete':False,'last_usage_success':220}})
        assert ledger.report('h',310)['index_complete_at']==220
        assert quota_statistics(ledger.report('h',310))['delta']==5
        ledger.sync(engine,{**state,'ts':330,'errors':['모델 관측 기록을 읽을 수 없습니다'],
            'usage_collection_complete':True,'last_usage_collection_success':320,
            'index':{'loading':False,'usage_complete':True,'last_usage_success':320}})
        completed=ledger.report('h',330)
        assert completed['index_complete_at']==320
        assert quota_statistics(completed)['delta']==8
    finally:ledger.close()
