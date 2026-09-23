"""Regression cases for the shared dashboard/overlay observation contract."""
import copy
import json
import pytest
from cachemonitor.core import Session,usage_values,token_parts
from cachemonitor.analytics import analyze,session_summary,comparison_view,stats
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.pricing import token_cost,COST_COMPONENTS,mode_assumptions
from cachemonitor.cache_health import CacheHealth
from cachemonitor.cache_misses import classify
from cachemonitor.model_evidence import EvidenceStore,EvidenceReader
from cachemonitor.request_modes import request_mode_observation,request_mode
from test_core import event


def source(rows=None):
    session=Session('s','home',title='Observed',cwd='C:\\work\\project')
    for i,values in enumerate(rows or [dict(input_tokens=100,cached_input_tokens=80,cache_write_input_tokens=10,output_tokens=20,reasoning_output_tokens=5)]):
        session.add_usage(100+i,str(i),values,'gpt-6-astra','turn','high',service_tier='Standard')
    view=session.view(110)
    view.update(turn_records={'turn':{'started_at':99,'ended_at':105,'state':'완료'}},turn_states={'turn':'완료'},collection_complete=True)
    return view


def test_disjoint_token_composition_reported_total_and_missing_call_identity():
    sample=usage_values(dict(input_tokens=100,cached_input_tokens=80,cache_write_input_tokens=10,output_tokens=20,reasoning_output_tokens=5,total_tokens=999))
    assert (sample['ordinary_input'],sample['non_reasoning'],sample['total'])==(10,15,120)
    assert sample['reported_total']==999 and sample['total_discrepancy']
    parts=token_parts(sample)
    assert parts['input']==dict(ordinary=10,read=80,write=10,unclassified=0,total=100)
    assert parts['output']==dict(ordinary=15,reasoning=5,unclassified=0,total=20)
    missing=source([dict(output_tokens=2),dict(input_tokens=0,output_tokens=0)])
    analysis=analyze([missing])
    assert len(analysis['responses'])==2
    assert analysis['responses'][0]['input'] is None and analysis['responses'][0]['key']=='0'
    assert analysis['totals']['input']==0 and analysis['totals']['missing']['input']==1
    assert stats(analysis['responses'],'input')['n']==1


def test_invalid_decomposition_is_unclassified_and_unknown_write_is_not_ordinary():
    invalid=usage_values(dict(input_tokens=100,cached_input_tokens=80,cache_write_input_tokens=30,output_tokens=20,reasoning_output_tokens=25))
    parts=token_parts(invalid)
    assert parts['input']['unclassified']==100 and parts['output']['unclassified']==20
    assert invalid['ordinary_input'] is None and invalid['non_reasoning'] is None
    invalid.update(model='gpt-6-astra',service_tier='Standard')
    assert token_cost(invalid)['cost'] is None
    for model in ('gpt-5.5','gpt-5.4-mini','gpt-5.5-pro'):
        row=dict(model=model,service_tier='Standard',input=100,cached=0,written=None,output=20,reasoning=None)
        price=token_cost(row)
        assert price['cost_uncached']==0 and price['cost_unclassified']>0 and price['cost_written']==0
        assert sum(price[k] for k in COST_COMPONENTS)==pytest.approx(price['cost'])
        row['written']=10;written=token_cost(row)
        assert written['cost_written']>0 and written['cost_unclassified']==0
        assert written['cost']==pytest.approx(price['cost'])


def test_no_response_ids_same_timestamp_and_tokens_are_distinct_and_archive_identity_stable():
    session=Session('s','h')
    first=event('token_usage_record',100,thread_id='s',usage={'input_tokens':10,'output_tokens':2})
    session.consume(dict(first,event_id='rollout.jsonl:10'),101)
    session.consume(dict(first,event_id='rollout.jsonl:20'),101)
    session.consume(dict(first,event_id='rollout.jsonl:10'),101)
    assert len(session.requests)==2
    assert len({r.key for r in session.requests})==2
    empty=event('token_usage_record',100,thread_id='s',response_id='missing',usage={})
    session.consume(empty,101)
    assert session.requests[-1].input is None and len(session.requests)==3


def test_same_home_response_id_dedup_and_no_parent_cost_inheritance():
    first=source();second=copy.deepcopy(first);second.update(id='child',source='subagent')
    second['history'][0]['key']='child-call';second['history'][0]['call_id']='child-call'
    mirror=copy.deepcopy(first);mirror['id']='z-copy'
    engine=AnalysisEngine();engine.ingest([first,second,mirror])
    a=engine.query(dict(page=0,start=0,end=200))['analysis']
    assert len(a['responses'])==2
    parent=engine.query(dict(page=0,start=0,end=200,sid='s'))['analysis']
    assert len(parent['responses'])==1
    foreign=copy.deepcopy(first);foreign['home']='other-home';engine.ingest([first,foreign])
    assert len(engine.query(dict(page=0,start=0,end=200))['analysis']['responses'])==2


def test_complete_request_requires_boundaries_full_membership_and_collected_interval():
    view=source([dict(input_tokens=100,cached_input_tokens=80,cache_write_input_tokens=0,output_tokens=5)]*2)
    complete=analyze([view])['turns'][0]
    assert complete['complete'] and complete['duration']==6 and complete['start_input']==100
    assert complete['total_responses']==2 and complete['responses']==2
    for change in ({'turn_records':{}},{'collection_complete':False},{'coverage_gaps':[{'start':100,'end':101,'tokens':20}]}):
        assert not analyze([{**view,**change}])['turns'][0]['complete']
    assert not analyze([view],start=100)['turns'][0]['complete']  # all calls fit, request start does not
    assert not analyze([view],end=103)['turns'][0]['complete']   # all calls fit, request end does not
    mixed=copy.deepcopy(view);mixed['history'][1]['service_tier']='Fast'
    assert analyze([mixed])['turns'][0]['complete']
    partial=analyze([mixed],service_tier='Standard')['turns'][0]
    assert not partial['complete'] and partial['exclusions']==['조건 일부']
    assert partial['responses']==1 and partial['total_responses']==2


def test_price_aliases_never_merge_analysis_identity_or_effort():
    view=source([dict(input_tokens=100,cached_input_tokens=80,cache_write_input_tokens=10,output_tokens=20)]*2)
    view['history'][0].update(model='gpt-5.6',configured_model='gpt-5.6',effort='ultra')
    view['history'][1].update(model='gpt-5.6-sol',configured_model='gpt-5.6-sol',effort='max')
    a=analyze([view]);assert len(a['groups'])==2
    assert {r['price_model'] for r in a['responses']}=={'gpt-5.6-sol'}
    assert {r['model'] for r in a['responses']}=={'gpt-5.6','gpt-5.6-sol'}
    assert {r['effort'] for r in a['responses']}=={'max','ultra'}


def test_model_priority_conflict_and_completed_only_timing_and_model_match(tmp_path):
    path=tmp_path/'wire.sqlite';store=EvidenceStore(path);reader=EvidenceReader(path)
    row=source()['history'][0];row['key']='id';row['configured_model']='setting-model';row['model']='setting-model'
    try:
        store.write('home','a',101,'WebSocket','id','actual-model','response-model','created',completion_latency_ms=10)
        reader.poll();created=reader.enrich('home',[row])[0]
        assert created['model']=='actual-model' and created['model_source']=='wire' and created['model_match']=='확인 불가'
        assert analyze([{**source(),'history':[created]}])['responses'][0]['duration'] is None
        store.write('home','a',102,'WebSocket','id','actual-model','response-model','completed',completion_latency_ms=1500,generation_latency_ms=200)
        reader.poll();done=reader.enrich('home',[row])[0]
        priced=analyze([{**source(),'history':[done]}])['responses'][0]
        assert priced['duration']==1.5 and priced['generation_wait']==.2
        store.write('home','b',103,'WebSocket','id','different','response-model','completed')
        reader.poll();conflict=reader.enrich('home',[row])[0]
        assert conflict['model']=='' and conflict['model_source']=='conflict'
        assert conflict['model_match']=='관측 충돌' and not conflict['timing_valid']
    finally:reader.close();store.close()


def test_explicit_clear_no_change_wire_omission_and_response_default_are_distinct(tmp_path):
    turn='019-test-turn-123456789'
    def body(value):return f'Submission sub=Submission {{ id: "{turn}", op: TurnInput {{ ThreadSettingsOverrides {{ service_tier: {value} }} }} }}'
    none=request_mode_observation(body('None'));clear=request_mode_observation(body('Some(None)'))
    assert none['action']=='unchanged' and none['mode'] is None and request_mode(body('None')) is None
    assert clear['action']=='clear' and clear['mode']=='Standard' and clear['settings_update']['configured_service_tier'] is None
    path=tmp_path/'wire.sqlite';store=EvidenceStore(path);reader=EvidenceReader(path)
    try:
        store.write('home','a',1,'WebSocket','id','m','m','completed',response_service_tier='default',wire_service_tier_present=False)
        reader.poll()
        unknown=reader.enrich('home',[dict(key='id',service_tier='미확인')])[0]
        assert unknown['service_tier']=='미확인' and unknown['response_service_tier']=='default'
        assert unknown['wire_service_tier_present'] is False and unknown['wire_observed']
        configured=reader.enrich('home',[dict(key='id',service_tier='Standard',configured_service_tier=None,request_mode_source='settings_override',request_mode_action='clear')])[0]
        assert configured['service_tier']=='Standard' and configured['request_mode_action']=='clear'
        assert configured['request_mode_source']=='settings_override'
    finally:reader.close();store.close()


def test_cache_incident_whole_history_keeps_baseline_occurrence_and_recovery_distinct():
    rows=[dict(key=str(i),ts=i,model='m',effort='ultra',service_tier='Standard',cache_policy=None,input=100,cached=c)
          for i,c in enumerate([90]*5+[0,0]+[90,90])]
    health=CacheHealth().update(rows);event=health['events'][0]
    assert event['baseline_keys']==['0','1','2','3','4']
    assert event['occurrence_keys']==['5','6'] and event['recovery_keys']==['7','8']
    assert event['count']==2 and event['resolved'] and health['incidents']==1
    assert classify([dict(rows[0],cached=0)])['count']==1
    for key,value in [('effort','max'),('service_tier','Fast'),('cache_policy','24h')]:
        changed=copy.deepcopy(rows)
        for r in changed[5:]:r[key]=value
        assert not CacheHealth().update(changed)['events']
    zeros=[dict(r,cached=0) for r in rows]
    assert not CacheHealth().update(zeros)['events']


def test_response_or_storage_failure_preserves_independent_historical_mode_and_request_model(tmp_path):
    store=EvidenceStore(tmp_path/'independent.sqlite');reader=EvidenceReader(store.path)
    historical=dict(key='missing',model='configured',service_tier='Standard',configured_service_tier=None,
                    request_mode_source='settings_override',request_mode_action='clear')
    try:
        store.write('home','missing',1,'WebSocket','missing','wire-model','response-model','completed',
                    response_service_tier='default',wire_service_tier_present=False,observation_missing=True)
        store.write('home','conflict',2,'WebSocket','conflict','wire-model','response-model','completed',
                    requested_service_tier='priority',conflict=True)
        reader.poll()
        missing=reader.enrich('home',[historical])[0]
        assert missing['service_tier']=='Standard' and missing['request_mode_source']=='settings_override'
        assert missing['configured_service_tier'] is None and missing['request_mode_action']=='clear'
        assert missing['wire_service_tier_present'] is False and missing['observation_missing']
        assert missing['model_match']=='관측 누락' and not missing['timing_valid']
        conflict=reader.enrich('home',[dict(historical,key='conflict')])[0]
        assert conflict['model']=='wire-model' and conflict['model_source']=='wire'
        assert conflict['service_tier']=='priority' and conflict['request_mode_source']=='wire'
        assert conflict['model_match']=='관측 충돌' and not conflict['timing_valid']
        assert not conflict['model_conflict'] and not conflict['mode_conflict']
    finally:reader.close();store.close()


def test_partial_sums_weighted_cache_and_whole_lookup_stay_shared():
    view=source([dict(input_tokens=100,cached_input_tokens=50,cache_write_input_tokens=0,output_tokens=0),
                 dict(input_tokens=900,cached_input_tokens=900,cache_write_input_tokens=0,output_tokens=0)])
    engine=AnalysisEngine();engine.ingest([view]);result=engine.query(dict(page=0,start=101,end=200))
    summary=session_summary(engine.sessions[('home','s')]['prepared']['history'])
    full=engine.query(dict(page=0,start=0,end=200))['overview']
    assert summary==full['summary'] and summary['cache']['value']==95
    assert len(result['lookup']['whole_turns'][('home','s','turn')])==2
    assert len(result['analysis']['responses'])==1
    assert engine.record('home','s','0')['session_ordinal']==1
