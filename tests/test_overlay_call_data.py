import copy

import pytest

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.cache_health import CacheHealth
from cachemonitor.cache_misses import classify
from cachemonitor.overlay_data import OverlayCacheHealth, OverlaySummaries, summarize_session, token_composition


def row(index, **changes):
    return dict(dict(key=f'call-{index}', ts=100+index, turn='turn', model='gpt-6-astra',
                service_tier='Standard', effort='high', input=1000, cached=900,
                written=0, output=100, reasoning=40, cost=.02), **changes)


def session(rows, **changes):
    return dict(dict(id='session', home='home', title='실제 세션', history=rows), **changes)


def test_recent_calls_are_chronological_and_share_latest_call_context():
    rows=[row(i) for i in range(30)]
    rows[-1].update(requested_model='actual-request', response_model=None,
                    service_tier='priority', effort='ultra', cached=0, cost=None,
                    transport='WebSocket', transport_source='response_id')
    result=summarize_session(session(list(reversed(rows))))
    assert [r['id'] for r in result['recent']]==[f'call-{i}' for i in range(6,30)]
    assert [r['ordinal'] for r in result['recent']]==list(range(7,31))
    latest=result['latest']
    assert latest is result['recent'][-1]
    for key in ('model','response_model','effort','mode','transport','cache_rate'):
        assert result[key]==latest[key]
    assert result['latest_cost'] is latest['cost'] is None
    assert result['cache_rate']==0 and latest['cache_miss']
    assert result['response_model'] is None
    assert latest['model']=='actual-request' and not latest['model_setting']
    assert latest['mode']=='Fast' and latest['transport_state']=='확정'
    rows.append(row(30))
    assert summarize_session(session(rows))['recent'][-2]['id']==latest['id']


def test_no_history_is_reconstructed_from_session_totals():
    result=summarize_session(session([],unclassified={'total':7000}))
    assert result['recent']==[] and result['latest']=={}
    assert result['calls']==0 and result['priced']==0
    assert result['cost'] is result['mean_cost'] is result['cache_rate'] is None
    assert result['token_composition']['total']==0
    assert result['unattached_total']==7000
    assert result['token_composition']['unknown']==0
    assert not result['coverage_gap'] and not result['partial']
    assert {'text':'호출 기록 없음','severity':'muted'} in result['statuses']


def test_unknown_and_zero_stay_distinct_and_average_uses_only_priced_calls():
    rows=[row(i) for i in range(3)]
    rows[0].update(cost=0,cached=0)
    rows[1].update(cost=None,cached=None)
    rows[2].update(cost=2,cached=0)
    result=summarize_session(session(rows))
    assert [r['cache_rate'] for r in result['recent']]==[0,None,0]
    assert [r['cost'] for r in result['recent']]==[0,None,2]
    assert result['cost']==2 and result['mean_cost']==1
    assert (result['priced'],result['missing'],result['calls'])==(2,1,3)
    assert result['partial'] and result['token_composition']['cache_hit_partial']
    assert result['cache_misses']['count']==2
    assert result['recent'][0]['cache_miss']
    assert [e['key'] for e in result['cache_misses']['events']]==['call-0','call-2']


def test_model_and_transport_conflicts_do_not_become_confirmed_mismatches():
    r=row(0)
    r.update(requested_model='model-a',response_model='model-b',model_alert_confirmed=False,
             model_evidence='관측 충돌',model_match='확인 불가',
             transport='WebSocket',transport_source='conflict',
             response_tier_confirmed=True,response_service_tier='priority',service_tier='미확인')
    result=summarize_session(session([r]))
    assert result['mode']=='미확인'
    assert not result['model_mismatch'] and result['model_state']=='관측 충돌'
    assert result['transport']=='미확인 · 관측 충돌'
    assert '모델명 불일치' not in result['warnings']
    r.update(model_evidence='저장 관측 누락',observation_missing=True)
    assert summarize_session(session([r]))['model_state']=='관측 누락'
    r['model_alert_confirmed']=True
    assert not summarize_session(session([r]))['model_mismatch']
    r=row(1)
    result=summarize_session(session([r]))
    assert result['model_setting'] and result['response_model'] is None
    assert result['latest']['transport_state']=='미확인'


def test_token_composition_order_and_comparison_have_distinct_denominators():
    result=token_composition(session([
        dict(input=1000,cached=700,written=100,output=500,reasoning=400),
        dict(input=2000,cached=1000,written=0,output=500,reasoning=100)]))
    assert [part['key'] for part in result['parts']]==['cached','uncached','written','output','reasoning']
    assert result['total']==4000 and result['cached_share']==1700/3000
    assert result['cache_hit_rate']==pytest.approx(1700/3000*100)
    assert [bar['tokens'] for bar in result['bars']]==[1200,100,500,500]
    assert result['bar_max']==1200
    assert [bar['bar_share'] for bar in result['bars']]==pytest.approx([1,100/1200,500/1200,500/1200])
    assert sum(part['share'] for part in result['parts'])==pytest.approx(1)
    assert result['bars'][0]['share']==1200/4000


def test_unknown_tokens_are_aggregated_only_for_monitor_and_preserved_for_detail():
    r=dict(input=1000,cached=600,written=None,output=200,reasoning=None)
    result=token_composition(session([r],unclassified={'total':500}))
    assert result['total']==1200 and not result['partial']
    assert [(p['key'],p['tokens']) for p in result['parts']]==[('cached',600),('unknown',600)]
    assert result['counts']['input_unknown']==400
    assert result['counts']['output_unknown']==200
    assert result['counts']['unknown']==0
    assert [b['key'] for b in result['bars']]==['uncached','written','output','reasoning','unknown']
    assert result['input_total']==1000 and result['output_total']==200
    assert sum(p['tokens'] for p in result['input_parts'])==1000
    assert sum(p['tokens'] for p in result['output_parts'])==200
    detail=summarize_session(session([dict(r,key='partial',ts=1)]))['latest']
    assert detail['input_unknown']==400 and detail['output_unknown']==200
    assert detail['non_reasoning'] is None and detail['written'] is None


def test_token_zero_categories_do_not_appear_unknown_and_missing_totals_are_partial():
    result=token_composition(session([dict(input=100,cached=100,written=0,output=0,reasoning=0)]))
    assert [b['key'] for b in result['bars']]==['uncached','written','output','reasoning']
    assert all(b['bar_share']==0 for b in result['bars'])
    assert result['unknown']==0 and result['cache_hit_rate']==100
    partial=token_composition(session([dict(input=10,cached=0,written=0)]))
    assert partial['partial'] and partial['total']==10
    empty=token_composition(session([]))
    assert empty['parts']==[] and not empty['known'] and empty['cache_hit_rate'] is None
    gap=token_composition(session([row(0)],coverage_gaps=[{'start':0,'end':1}]))
    assert not gap['partial'] and not gap['cache_hit_partial'] # unattached gap is not a synthetic call
    absent=token_composition(session([{'ts':1}]))
    assert absent['partial'] and not absent['known']


def test_health_events_reuse_existing_rules_and_keep_miss_and_degradation_separate():
    rows=[row(i) for i in range(14)]
    for i in (5,6): rows[i]['cached']=300
    for i in (12,13): rows[i]['cached']=0
    misses={e['key'] for e in classify(rows)['events']}
    for r in rows:r['cache_miss']=r['key'] in misses
    tracker=OverlayCacheHealth()
    observed=tracker.update(rows)
    expected=CacheHealth().update(rows)
    assert observed==expected
    assert len(observed['events'])==2
    assert observed['events'][0]['state']=='캐시 저하 의심'
    assert observed['events'][0]['resolved'] and observed['events'][0]['occurrence_keys']==['call-5','call-6']
    assert observed['events'][1]['state']=='캐시 저하 의심'
    result=summarize_session(session(rows),observed)
    assert result['cache_degradation']['count']==2
    assert result['cache_degradation']['current']
    assert result['cache_misses']['count']==2
    assert result['recent'][5]['cache_degradation']
    assert not result['recent'][7]['cache_warning']
    assert result['latest']['warnings']==['캐시 읽기 0','캐시 저하 의심']
    saved=copy.deepcopy(observed)
    fixed=[dict(r,cached=900,cache_miss=False) for r in rows]
    assert tracker.update(fixed)['events']==[]
    assert observed==saved


def test_summary_cache_reuses_revision_and_session_switch_cannot_reuse_calls():
    engine=AnalysisEngine();collector=OverlaySummaries()
    original=session([row(0)])
    engine.ingest([original]);first=collector.collect(engine)[0]
    assert collector.collect(engine)[0] is first
    engine.ingest([session([],title='새 세션') | {'id':'other'}])
    other=collector.collect(engine)[0]
    assert other['id']=='other' and other['recent']==[]
    assert len(collector.health)==1


def test_coverage_diagnostics_update_without_repricing_or_reusing_stale_scope():
    engine=AnalysisEngine();collector=OverlaySummaries()
    original=session([row(0)])
    engine.ingest([original]);first=collector.collect(engine)[0]
    revision=engine.revision
    original['coverage_gaps']=[dict(start=1,end=2,tokens=100)]
    engine.ingest([original])
    assert engine.revision>revision
    changed=collector.collect(engine)[0]
    assert changed is not first and changed['coverage_gap']
    assert not changed['token_composition']['partial']
    assert not changed['token_composition']['cache_hit_partial']
    assert changed['latest_cost']==first['latest_cost']
    assert collector.collect(engine)[0] is changed


def test_unobserved_token_category_is_not_a_confirmed_zero():
    result=token_composition({'history':[dict(input=1000,cached=900,written=None,output=100,reasoning=None)]})
    assert result['category_known']['cached']
    assert not any(result['category_known'][key] for key in ('uncached','written','output','reasoning'))
    assert result['category_known']['unknown'] and result['unknown']==200
    actual_zero=token_composition({'history':[dict(input=0,output=0)]})
    assert not any(actual_zero['category_known'][key] for key in ('cached','uncached','written','output','reasoning'))
    assert actual_zero['input_total']==actual_zero['output_total']==0


def test_degradation_evidence_retains_observed_calls_outside_recent_graph():
    rows=[row(i) for i in range(40)]
    for i in (5,6):rows[i]['cached']=300
    health=OverlayCacheHealth().update(rows);original=copy.deepcopy(health)
    result=summarize_session(session(rows),health)
    event=result['cache_degradation']['events'][0]
    assert event['resolved'] and event['baseline_read']==900 and event['baseline_rate']==.9
    assert [r['key'] for r in event['baseline_calls']]==event['baseline_keys']
    assert [r['ordinal'] for r in event['baseline_calls']]==list(range(1,6))
    assert [(r['ordinal'],r['input'],r['cached'],r['cache_rate']) for r in event['comparison_calls']]==[
        (6,1000,300,30),(7,1000,300,30)]
    assert all(r['key'] not in {recent['key'] for recent in result['recent']} for r in event['comparison_calls'])
    assert health==original


def test_unclassified_aggregate_is_observed_without_creating_a_call():
    result=summarize_session(session([],unclassified={'total':7000}))
    assert not result['token_composition']['known']
    assert result['token_composition']['total']==0
    assert result['unattached_total']==7000
    assert sum(b['tokens'] for b in result['token_composition']['bars'])==0
    assert result['recent']==[] and result['calls']==0 and result['latest']=={}
