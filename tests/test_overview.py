from datetime import datetime
import pytest
from cachemonitor.analytics import analyze,overview_view
from cachemonitor.core import Session


def ts(value):return datetime.fromisoformat(value).timestamp()


def session_view():
    session=Session('a','h',title='Overview task',cwd='C:\\projects\\app')
    times=('2026-01-30T12:00:00','2026-02-01T12:00:00','2026-02-02T12:00:00')
    for i,(date,model,turn) in enumerate(zip(times,('gpt-6-astra','unknown','gpt-6-astra'),('a','unknown','b'))):
        session.add_usage(ts(date),str(i),dict(input_tokens=100000,cached_input_tokens=80000,
            cache_write_input_tokens=10000,output_tokens=2000),model,turn,'high',service_tier='Standard')
    view=session.view(ts('2026-02-03T00:00:00'))
    view.update(turn_states={'a':'완료','unknown':'완료','b':'진행'},source='user',archived=False)
    view['turn_records']={turn:dict(started_at=ts(date)-1,ended_at=ts(date)+1,state='완료') for turn,date in zip(('a','unknown'),times)}
    return view


def test_daily_empty_unknown_partial_and_same_priced_composition():
    start,end=ts('2026-01-30T00:00:00'),ts('2026-02-03T00:00:00')
    view=overview_view(analyze([session_view()],start,end),start,end,granularity='day')
    assert (view['days'],view['active_days'],view['known'],view['missing'],view['completed'])==(4,3,2,1,2)
    assert view['total']==pytest.approx(.81)
    assert [r['total'] for r in view['timeline']]==[pytest.approx(.405),0,None,pytest.approx(.405)]
    assert sum(r['total'] for r in view['components'])==pytest.approx(view['total'])
    assert len(view['components'])==5 and view['summary']['cost']['partial']
    assert view['cache_known']==3 and view['cache_ratio']==pytest.approx(.8)
    assert view['turn_stats']['n']==1 and view['request_calls']==1
    assert len(view['attention']['unpriced'])==1
    assert all(not bucket['partial'] for bucket in view['timeline'])


def test_calendar_weeks_months_and_exclusive_boundaries():
    start,end=ts('2026-01-30T00:00:00'),ts('2026-02-03T09:00:00')
    analysis=analyze([session_view()],start,end)
    weeks=overview_view(analysis,start,end,granularity='week')['timeline']
    assert [(r['start'],r['end']) for r in weeks]==[
        ('2026-01-30T00:00:00','2026-02-02T00:00:00'),('2026-02-02T00:00:00','2026-02-03T09:00:00')]
    assert all(r['partial'] for r in weeks)
    months=overview_view(analysis,start,end,granularity='month')['timeline']
    assert months[0]['end']=='2026-02-01T00:00:00' and months[1]['start']=='2026-02-01T00:00:00'
    leap=overview_view(analyze([]),ts('2024-02-01T00:00:00'),ts('2024-03-01T00:00:00'),granularity='month')
    assert leap['days']==29 and leap['timeline'][0]['end']=='2024-03-01T00:00:00'
    assert overview_view(analyze([]),0,end)['timeline']==[]


def test_completed_request_trend_uses_completion_time_and_missing_means_gap():
    source=session_view();source['turn_records']['a']['ended_at']=ts('2026-01-31T01:00:00')
    start,end=ts('2026-01-30T00:00:00'),ts('2026-02-03T00:00:00')
    view=overview_view(analyze([source],start,end),start,end,basis='turn_mean',granularity='day')
    assert [r['value'] for r in view['timeline']]==[None,pytest.approx(.405),None,None]
    assert [r['known'] for r in view['timeline']]==[0,1,0,0]
    assert [r['N'] for r in view['timeline']]==[0,1,1,0]
    assert view['timeline'][1]['calls']==0  # requests complete independently of call timestamps
