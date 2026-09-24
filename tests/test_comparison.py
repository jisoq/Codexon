import pytest
from cachemonitor.analytics import analyze, comparison_view, stats
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
