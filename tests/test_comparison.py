import pytest
from cachemonitor.analytics import analyze, comparison_view, stats, initial_targets
from cachemonitor.core import Session
from cachemonitor.pricing import COST_COMPONENTS


def history():
    session=Session('s','h',title='Costs')
    for i,(turn,effort,model,inp,output,reasoning) in enumerate([
        ('done','high','gpt-6-astra',100000,2000,1000),
        ('done','high','gpt-6-astra',100000,4000,None),
        ('open','high','gpt-6-astra',200000,1000,500),
        ('mixed','low','gpt-6-astra',100000,1000,500),
        ('mixed','ultra','gpt-6-astra',100000,1000,500),
        ('unpriced',None,'unknown-model',100000,1000,500),
        (None,'low','gpt-6-astra',100000,1000,500),
    ]):
        usage=dict(input_tokens=inp,cached_input_tokens=80000,cache_write_input_tokens=10000,output_tokens=output)
        if reasoning is not None:usage['reasoning_output_tokens']=reasoning
        session.add_usage(100+i,str(i),usage,model,turn,effort,service_tier='Standard')
    view=session.view(110)
    view['turn_states']={'done':'완료','open':'진행','mixed':'완료','unpriced':'완료'}
    view['turn_records']={turn:dict(started_at=start,ended_at=end,state='완료')
        for turn,start,end in (('done',100,101.5),('mixed',103,104.5),('unpriced',105,105.5))}
    return view


def targets():
    return [dict(id='A',model='gpt-6-astra',effort='high',service_tier='Standard'),
            dict(id='B',model='unknown-model',effort='미확인',service_tier='Standard')]


def test_explicit_targets_keep_unpriced_token_populations_and_five_part_costs():
    analysis=analyze([history()]);view=comparison_view(analysis,targets=targets())
    high,unknown=view['groups']
    assert high['stats']['n']==3 and high['turn']['n']==1
    assert high['turn']['median']==pytest.approx(.91)
    assert sum(high['mean_budget'][k] for k in COST_COMPONENTS)==pytest.approx(high['mean_budget']['cost'])
    assert unknown['n']==0 and unknown['N']==1
    tokens=comparison_view(analysis,metric='reasoning',targets=targets())
    assert tokens['groups'][1]['value']==500 and tokens['groups'][1]['n']==1
    assert view['completed']==3 and view['turn_known']==2 and view['mixed']==1
    assert analysis['missing_turn_responses']==1
    assert not view['scatter']  # Usage timestamps are not duration measurements.


def test_complete_request_group_excludes_mixed_and_filters_apply_to_all_values():
    analysis=analyze([history()])
    view=comparison_view(analysis,targets=targets(),unit='turn')
    assert view['groups'][0]['n']==1
    breakdown=view['groups'][0]['decomposition']
    assert breakdown['request_mean']==pytest.approx(breakdown['call_mean']*breakdown['calls_per_request'])
    narrowed=comparison_view(analysis,targets=targets(),input_band=(0,10000))
    assert all(g['N']==0 and g['condition_excluded']>0 for g in narrowed['groups'])
    assert all(g['mean_budget']['cost'] is None for g in narrowed['groups'])
    assert len(narrowed['targets'])==2
    cut=analyze([history()],start=101)
    assert not next(t for t in cut['turns'] if t['turn']=='done')['complete']


def test_quantiles_small_samples_and_cache_denominators():
    view=comparison_view(analyze([history()]),targets=targets())
    high=view['groups'][0]
    assert high['bands'][3]['n']==2 and high['bands'][4]['n']==1
    assert high['bands'][3]['cache']==80 and high['bands'][4]['cache']==40
    result=stats([{'x':v} for v in (0,1,2,3,100,None)],'x')
    assert (result['n'],result['N'],result['missing'])==(5,6,1)
    assert result['mean']==pytest.approx(21.2) and result['median']==2
    assert result['p90']==pytest.approx(61.2)
    assert stats([{'x':0},{'x':2}],'x','p90')['value'] is None
    assert stats([{'x':0},{'x':None},{'x':2}],'x')['mean']==1
    assert stats([{'x':None}],'x')['sum'] is None
    assert stats([{'x':0}],'x')['mean']==0
    assert comparison_view(analyze([]))['targets']==[]


def test_actual_scatter_intersection_and_explicit_zero_baseline():
    source=history()
    for row in source['history'][:2]:
        row.update(response_status='completed',timing_valid=True,completion_latency_ms=2000)
    source['history'][2].update(response_status='failed',completion_latency_ms=1000)
    view=comparison_view(analyze([source]),targets=targets())
    first=view['groups'][0]
    assert len(first['scatter'])==2
    assert first['scatter_stats']['cost']['mean']==pytest.approx((.405+.505)/2)
    assert first['scatter_stats']['duration']['mean']==2
    assert all(r['duration']==2 for r in view['scatter'])
    source['history'][0]['reasoning']=0;source['history'][1]['reasoning']=0;source['history'][2]['reasoning']=0
    zero=comparison_view(analyze([source]),metric='reasoning',targets=targets())
    assert zero['groups'][1]['delta']==500 and zero['groups'][1]['relative'] is None


def test_initial_pair_balances_samples_and_explicit_editor_is_stable():
    rows=[]
    for effort,standard,fast in [('high',8,2),('ultra',3,3)]:
        for mode,n in [('Standard',standard),('Fast',fast)]:
            rows.extend(dict(model='m',effort=effort,service_tier=mode,ts=i) for i in range(n))
    pair=initial_targets(rows)
    assert [t['effort'] for t in pair]==['ultra','ultra']
    assert [t['service_tier'] for t in pair]==['Standard','Fast']
    assert comparison_view(analyze([history()]),targets=[])['groups']==[]


def test_cache_distribution_count_and_matrix_preserve_other_common_filters():
    zero=stats([dict(input=0,cached=0,rate=None) for _ in range(10)],'rate','p90')
    assert zero['n']==10 and zero['distribution_n']==0 and zero['value'] is None
    assert zero['points']==[] and zero['p90'] is None
    analysis=analyze([history()])
    by_input=comparison_view(analysis,targets=targets(),conditions={'cache_band':(75,100)},matrix_by='input')
    assert by_input['groups'][0]['N']==2
    assert sum(c['N'] for c in by_input['matrix'][0]['cells'])==2
    by_cache=comparison_view(analysis,targets=targets(),conditions={'input_band':(200000,272001)},matrix_by='cache')
    assert by_cache['groups'][0]['N']==1
    assert sum(c['N'] for c in by_cache['matrix'][0]['cells'])==1


def test_repricing_keeps_target_identity_baseline_and_excluded_intersection_count():
    source=history();source['history']=source['history'][:3]
    source['history'][1]['service_tier']='Fast'
    source['history'][2]['written']=None  # missing premium write input excludes the same record from both modes
    target=[dict(id='D',model='gpt-6-astra',effort='high',service_tier='Fast'),
            dict(id='G',model='gpt-6-astra',effort='high',service_tier='Standard')]
    result=comparison_view(analyze([source]),targets=target,baseline='D')['repricing']
    fast,standard=result['Fast'],result['Standard']
    assert (result['n'],result['N'],result['missing'])==(2,3,1)
    assert fast['id']=='D' and standard['id']=='G' and fast['baseline'] and not standard['baseline']
    assert (fast['n'],fast['N'],fast['missing'])==(2,3,1)
    assert (standard['n'],standard['N'],standard['missing'])==(2,3,1)
    assert fast['delta']==0 and standard['delta']==pytest.approx(-standard['value'])
    assert standard['relative']==pytest.approx(-50)
