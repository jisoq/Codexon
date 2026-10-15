from copy import deepcopy
from cachemonitor.quota_cycles import quota_statistics,quota_value_history
from cachemonitor.quota_view import history_rows
from test_quota_value_history import interval,report_for


def example(points,calls,pending=()):
    cycle=interval(points,[(end,cost) for uid,start,end,cost in calls])
    for row,(uid,start,end,cost) in zip(cycle['cost_rows'],calls):row.update(uid=uid,request_start=start)
    report=report_for([cycle]);report['request_windows']=[dict(uid=uid,start=start,end=end) for uid,start,end,cost in calls]+list(pending)
    return report


def test_idle_decrease_is_excluded_but_actual_history_and_cycle_are_unchanged():
    report=example([(100,100),(130,98),(160,98),(190,95),(220,95),(250,94),(280,94)],
                   [('a',110,115,2),('b',230,235,1)])
    before=deepcopy(report);summary=quota_statistics(report)
    assert summary['delta']==3 and summary['cost']==3 and summary['per_percent']==1
    assert summary['account_delta']==6 and summary['attribution']['idle_delta']==3
    rows,periods=quota_value_history(report,history_rows(report,'weekly'))
    assert len(periods)==1 and rows[-1]['remaining']==94
    assert rows[3]['cycle_value']==rows[2]['cycle_value']
    assert report==before


def test_long_request_includes_consumption_before_its_cost_record_arrives():
    report=example([(100,100),(130,99),(160,98),(190,97),(220,96),(250,95),(280,95),(310,94)],
                   [('long',115,235,8)])
    summary=quota_statistics(report)
    assert summary['delta']==5 and summary['cost']==8
    assert summary['attribution']['idle_delta']==1


def test_pending_usage_is_excluded_until_response_identity_is_matched():
    report=example([(100,100),(130,99),(160,99)],[],[dict(uid='late',start=110,end=120)])
    before=quota_statistics(report)
    assert before['per_percent'] is None and before['delta']==0
    assert before['attribution']['pending_delta']==1
    after=example([(100,100),(130,99),(160,99)],[('late',110,120,3)])
    assert quota_statistics(after)['per_percent']==3


def test_concurrent_requests_share_the_same_consumption_once():
    report=example([(100,100),(130,99),(160,98),(190,97),(220,97)],
                   [('a',110,140,2),('b',125,165,4)])
    summary=quota_statistics(report)
    assert summary['delta']==3 and summary['cost']==6 and summary['calls']==2


def test_an_unfinished_request_does_not_pollute_completed_neighbors():
    report=example([(100,100),(130,99),(160,99),(190,98),(220,97)],
                   [('a',110,120,2)],[dict(uid='running',start=175,end=None)])
    summary=quota_statistics(report)
    assert summary['delta']==1 and summary['cost']==2
    assert summary['attribution']['pending_delta']==2


def test_old_unfinished_requests_cannot_suppress_three_observed_percentage_points():
    from cachemonitor.quota_view import prepare_quota_view
    report=example([(100,5),(130,4),(160,3),(190,2)],
                   [('a',110,120,2),('b',140,150,3),('c',170,180,4)],
                   [dict(uid='stale',start=1,end=None)])
    original=deepcopy(report)
    summary=quota_statistics(report)
    assert summary['cost']==9 and summary['delta']==3
    assert summary['per_percent']==3
    rows=prepare_quota_view(report)['overall']['rows']
    assert rows[-1]['remaining']==3 and rows[-1]['cycle_cost']==9
    assert rows[-1]['cycle_value']==300
    assert report==original


def test_period_selection_cannot_count_the_tail_of_a_request_without_its_baseline():
    report=example([(100,100),(130,99),(160,98),(190,97),(220,97)],[('long',110,180,6)])
    assert quota_statistics(report,130)['per_percent'] is None


def test_gap_does_not_allow_a_request_to_bridge_unobserved_consumption():
    report=example([(100,100),(130,99),(400,90),(430,89),(460,89)],[('long',115,420,4)])
    summary=quota_statistics(report)
    assert summary['delta']==0 and summary['per_percent'] is None


def test_overview_has_one_exact_aggregate_per_cycle_and_retains_lifetime_totals():
    from cachemonitor.quota_view import prepare_quota_view
    first=example([(100,100),(130,80),(160,60),(190,40)],[('a',110,180,30)])
    second=example([(220,100),(250,80),(280,60),(310,40)],[('b',230,300,60)])
    report=report_for(first['cycles']+second['cycles'])
    report['request_windows']=first['request_windows']+second['request_windows']
    view=prepare_quota_view(report);rows=view['overall']['rows']
    assert len(view['periods'])==len(rows)==2
    assert [(row['cycle_start'],row['at']) for row in rows]==[(100,190),(220,310)]
    assert [(row['remaining'],row['cycle_cost'],row['cycle_value']) for row in rows]==[(60,30,50),(60,60,100)]
    assert view['lifetime']['delta']==120 and view['lifetime']['cost']==90
    assert not rows[0]['current'] and rows[1]['current']
    assert view['periods'][1]['series']['rows'][-1]['remaining']==40


def test_weekly_value_requires_recorded_cost_but_accepts_zero():
    from cachemonitor.quota_view import prepare_quota_view
    for cost in (6,0):
        report=example([(100,100),(130,99),(160,98),(190,97),(220,97)],[('a',110,180,cost)])
        view=prepare_quota_view(report)
        rows=view['periods'][0]['series']['rows']
        assert rows[1]['cycle_value'] is None and rows[2]['cycle_value'] is None
        assert rows[-1]['cycle_value']==cost/3*100
        assert view['overall']['rows'][0]['cycle_value']==cost/3*100


def test_new_request_during_optional_settling_sample_preserves_completed_pair():
    report=example([(100,100),(130,99),(160,98),(190,97)],
                   [('done',110,150,4)],[dict(uid='new',start=175,end=None)])
    summary=quota_statistics(report)
    assert summary['delta']==2 and summary['cost']==4
    assert summary['attribution']['pending_delta']==1
