"""The monetary chart uses matched evidence and the same actual reset events."""
from copy import deepcopy

import pytest

from cachemonitor.quota_cycles import quota_statistics, quota_value_history
from cachemonitor.quota_panel import history_rows


def interval(points, costs, **extra):
    known=[cost for _,cost in costs if cost is not None]
    return dict(id=str(points[0][0]), start=points[0][0], end=points[-1][0],
                used_start=100-points[0][1], used_end=100-points[-1][1],
                delta=points[0][1]-points[-1][1], cost=sum(known) if known else None,
                endpoints=[(at,100-remaining) for at,remaining in points],
                cost_rows=[dict(ts=at,cost=cost,model='test',service_tier='Standard') for at,cost in costs],
                models=[dict(model='test',service_tier='Standard',cost=sum(known) if known else None,
                             calls=len(costs),priced=len(known),separate=False)],
                blocked=[], live=True, account='a', forward_tracking=True,
                cost_complete=True, separate_models=[], **extra)


def report_for(cycles):
    return dict(account='a',tracking={'complete':True},cycles=cycles,
                history=[dict(id=str(at),at=at,used=used,window='weekly',
                              reset=250,account=c.get('account','a'),source='live')
                         for c in cycles for at,used in c['endpoints']])


def sample_report():
    return report_for([
        interval([(100,80),(130,60)],[(115,12)]),
        interval([(160,100),(190,90),(220,80)],[(175,4),(205,6)]),
        interval([(250,100),(280,95)],[(265,3)])])


def test_reset_zeroes_cycle_cost_but_lifetime_is_weighted_and_keeps_every_cycle():
    report=sample_report();original=deepcopy(report)
    rows,periods=quota_value_history(report,history_rows(report,'weekly'))
    assert [p['value'] for p in periods]==pytest.approx([60,50,60])
    assert [r['cycle_cost'] for r in rows]==[0,12,0,4,10,0,3]
    assert [r['cycle_value'] for r in rows]==[None,60,None,40,50,None,60]
    assert [p['reset_kind'] for p in periods]==[None,'arbitrary_reset','scheduled_reset']
    total=quota_statistics(report)
    assert total['cost']==25 and total['delta']==45
    assert total['per_percent']*100==pytest.approx(25/45*100)
    assert total['per_percent']*100!=pytest.approx(sum(p['value'] for p in periods)/3)
    assert report==original


def test_gap_excludes_both_cost_and_consumption_without_invalidating_or_splitting_cycle():
    report=report_for([
        interval([(100,80),(130,60)],[(115,12)]),
        interval([(160,100),(190,90),(400,70),(430,65)],[(175,4),(300,1000),(415,3)])])
    original=deepcopy(report)
    rows,periods=quota_value_history(report,history_rows(report,'weekly'))
    assert len(periods)==2 and not periods[1]['partial']
    assert periods[1]['cost']==7 and periods[1]['delta']==15
    assert periods[1]['value']==pytest.approx(7/15*100)
    assert rows[-2]['cycle_cost']==4 and not rows[-2]['connect']
    assert rows[-1]['cycle_cost']==7
    total=quota_statistics(report)
    assert total['cost']==19 and total['delta']==35
    assert sum(p['cost'] for p in periods)==total['cost']
    assert sum(p['delta'] for p in periods)==total['delta']
    assert report==original


def test_account_switch_does_not_mix_money_or_invent_reset():
    old=interval([(100,80),(130,60)],[(115,10000)])
    old['account']='b'
    report=report_for([old,interval([(160,80),(190,75)],[(175,3)])])
    rows,periods=quota_value_history(report,history_rows(report,'weekly'))
    assert len(periods)==1 and periods[0]['value']==60
    assert all(r['account']=='a' for r in rows)
    assert all(r['reset_kind'] is None for r in rows)
    assert quota_statistics(report)['cost']==3
