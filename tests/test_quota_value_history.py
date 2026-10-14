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


@pytest.mark.parametrize('packed',(False,True))
def test_changed_period_reuses_other_graphs_and_matches_uncached_values(monkeypatch,packed):
    from cachemonitor.quota_cycles import _PackedHistory
    from cachemonitor.quota_view import prepare_quota_view, prepare_overall, period_series
    from cachemonitor.quota_worker import chart_result
    from cachemonitor import quota_share
    shares=[];prepare_share=quota_share.prepare_model_share
    def prepared(series,intervals):
        shares.append(True)
        return prepare_share(series,intervals)
    monkeypatch.setattr(quota_share,'prepare_model_share',prepared)
    def view(report,previous=None):
        if packed:report={**report,'history':_PackedHistory(sorted(report['history'],key=lambda r:r['at']))}
        return prepare_quota_view(report,previous,deferred=True)
    def materialized(view):
        return [{**{k:v for k,v in p.items() if not k.startswith('_')},'series':period_series(p)} for p in view['periods']]
    def unchanged(before,after,index):
        start=before['periods'][index]['start']
        digest=chart_result(before,start)['digest']
        assert chart_result(after,start,digest)['series'] is None
    report=sample_report()
    before=view(report)
    assert not shares  # Unselected charts do not calculate display-only shares.
    changed=deepcopy(report)
    changed['cycles'][-1]=interval([(250,100),(280,95),(310,90)],[(265,3),(295,7)])
    changed=report_for(changed['cycles'])
    after=view(changed,before)
    full=prepare_quota_view(changed)
    assert after['overall'] is None
    unchanged(before,after,1);unchanged(before,after,2)
    assert materialized(after)==full['periods']
    assert after['lifetime']==full['lifetime']
    assert prepare_overall(after['_overall_parts'],after['_exclusions'])==full['overall']
    assert prepare_overall(after['_overall_parts'],after['_exclusions'],packed=True)==full['overall']
    digests={p['start']:chart_result(after,p['start'])['digest'] for p in after['periods']}
    assert len(after['_series_cache'])<=2
    for start,digest in digests.items():
        assert chart_result(after,start,digest)['series'] is None
        assert len(after['_series_cache'])<=2
    # A late correction invalidates that historical period, not its neighbors.
    corrected=deepcopy(changed)
    corrected['cycles'][0]=interval([(100,80),(130,60)],[(115,24)])
    correction=view(corrected,after)
    full=prepare_quota_view(corrected)
    unchanged(after,correction,0);unchanged(after,correction,1)
    assert materialized(correction)==full['periods']
    assert correction['lifetime']==full['lifetime']
    # A model-only correction changes the share even when chart costs match.
    start=correction['periods'][-1]['start']
    old_chart=chart_result(correction,start)
    changed_model=deepcopy(corrected)
    changed_model['cycles'][0]['cost_rows'][0]['model']='corrected-model'
    changed_model['cycles'][0]['models'][0]['model']='corrected-model'
    model_view=view(changed_model,correction)
    new_chart=chart_result(model_view,start,old_chart['digest'])
    assert new_chart['series'] is not None and new_chart['digest']!=old_chart['digest']
    assert materialized(model_view)==prepare_quota_view(changed_model)['periods']


def test_packed_history_resume_keeps_appends_corrections_and_cycle_edits_exact(monkeypatch):
    from cachemonitor import quota_view
    from cachemonitor.quota_cycles import _PackedHistory
    from cachemonitor.quota_tracking import ResetTracker
    visits=[];observe=ResetTracker.observe
    def counted(self,*args,**kwargs):
        visits.append(args[1])
        return observe(self,*args,**kwargs)
    monkeypatch.setattr(ResetTracker,'observe',counted)
    points=[[(start+i,100-i/10) for i in range(400)] for start in (1,501,1001)]
    source=report_for([interval(rows,[(rows[10][0],2)]) for rows in points])
    source['history']=_PackedHistory(source['history'])
    view=quota_view.prepare_quota_view(source,deferred=True)
    def checked(report,previous):
        visits.clear()
        result=quota_view.prepare_quota_view(report,previous,deferred=True)
        visited=len(visits)
        actual=[{**{k:v for k,v in p.items() if not k.startswith('_')},
                 'series':quota_view.period_series(p)} for p in result['periods']]
        fresh=quota_view.prepare_quota_view(report)
        assert actual==fresh['periods']
        assert result['lifetime']==fresh['lifetime']
        assert quota_view.prepare_overall(result['_overall_parts'],result['_exclusions'])==fresh['overall']
        return result,visited
    # The unchanged prefix crosses packed-block boundaries and two resets.
    appended={**source,'cycles':[*source['cycles'][:-1],interval(points[-1]+[(1401,60)],[(1011,2)])],
              'history':source['history'].with_suffix(1401,[dict(id='1401',at=1401,used=40,
                  window='weekly',reset=250,account='a',source='live')])}
    view,count=checked(appended,view)
    assert count<600
    # Cost changes must reload that historical period even with shared history.
    correction={**appended,'cycles':[interval(points[0],[(11,9)]),*appended['cycles'][1:]]}
    view,count=checked(correction,view)
    assert count<600
    # An old observation changes a reset boundary inside the shared prefix.
    tail=[{**row,'used':0} if row['at']==700 else row for row in correction['history'].iter_from(700)]
    correction={**correction,'history':correction['history'].with_suffix(700,tail)}
    view,_=checked(correction,view)
    # Controls and a removed cycle invalidate the saved detector state.
    controlled={**correction,'tracking':dict(complete=True,controls=[
        dict(at=650,enabled=False),dict(at=660,enabled=True)])}
    view,_=checked(controlled,view)
    deleted={**controlled,'cycle_clears':[dict(account='a',start=501,end=1001,baseline='null')]}
    view,_=checked(deleted,view)
    # A full metadata cache falls back without withholding any historical row.
    monkeypatch.setattr(quota_view,'HISTORY_GROUP_CACHE_BYTES',1)
    view,_=checked(deleted,view)
    assert view['_history_groups']['groups'] is None
    checked(appended,view)


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


def test_completed_opening_survives_latest_collection_lag():
    from cachemonitor.quota_view import prepare_quota_view
    c=interval([(100,100),(130,100),(160,99),(190,98)],[(120,2),(150,3),(180,7)])
    c['cost_complete_at']=190
    report=report_for([c])
    expected=prepare_quota_view(report)['periods'][0]['series']['completed_costs']
    c['endpoints'].append((220,2));c['end']=220
    report=report_for([c]);c['cost_complete']=False
    actual=prepare_quota_view(report)['periods'][0]['series']['completed_costs']
    assert actual[:3]==expected[:3]==[5,5,7]
    c['cost_complete_at']=None
    assert prepare_quota_view(report)['periods'][0]['series']['completed_costs'][0] is None


def test_hidden_idle_boundary_keeps_completed_cost(monkeypatch):
    from cachemonitor import quota_view
    rows=[dict(at=at,remaining=remaining,cycle_cost=cost,cycle_delta=100-remaining,cycle_value=1000,confirmed_cost=cost,confirmed_delta=100-remaining,local_observed=visible,local_range=region,connect=i>0,tracking_continuous=True,cycle_start=100,reset_kind=None,account='a',label=str(at))
          for i,(at,remaining,cost,visible,region) in enumerate([(100,76,0,True,0),(110,75,2,True,0),(120,75,10,True,0),(130,74,10,False,0),(140,74,10,True,1),(150,73,22,True,1)])]
    period=dict(start=100,end=150,current=True,reset_kind=None,cost=22,delta=3,value=22/3*100,cost_intervals=[])
    monkeypatch.setattr(quota_view,'quota_value_history',lambda *args,**kwargs:(rows,[dict(period)]))
    monkeypatch.setattr(quota_view,'history_rows',lambda *args:[])
    monkeypatch.setattr(quota_view,'quota_statistics',lambda *args,**kw:{})
    view=quota_view.prepare_quota_view({})
    assert view['periods'][0]['series']['completed_costs']==[None,8,8,12,None]
    assert view['overall']['completed_costs']==[None,8,8,12,None]
    assert not view['periods'][0]['series']['rows'][3]['connect']
