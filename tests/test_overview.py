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


def test_category_projection_preserves_denominators_history_and_bounded_pages(monkeypatch):
    from cachemonitor.analysis_engine import AnalysisEngine
    engine=AnalysisEngine();source=session_view();engine.ingest([source])
    now=ts('2026-02-03T00:00:00')
    q=dict(page=1,start=0,end=now,now=now,period='all',category_usage=True,presentation=True,
           include_whole_history=False,usage=dict(dimension='model',metric='calls',size=2))
    view=engine.page_query(q)['usage']
    assert view['summary']['value']==3 and sum(r['value'] for r in view['rows'])==3
    assert all(sum(c['value'] for c in r['components'])==r['value'] for r in view['rows'])
    original=engine.query
    monkeypatch.setattr(engine,'query',lambda q:pytest.fail('Pagination/search must reuse the aggregate projection'))
    filtered=engine.page_query({**q,'usage':{**q['usage'],'search':'unknown','page':100}})['usage']
    assert filtered['summary']['value']==3 and filtered['count']==1 and filtered['page']==0
    monkeypatch.setattr(engine,'query',original)
    selected={**q,'start':ts('2026-02-02T00:00:00'),'model':'gpt-6-astra',
              'usage':dict(dimension='source',metric='calls',selected=['model','gpt-6-astra'])}
    detail=engine.page_query(selected)['usage']
    assert detail['summary']['value']==1
    assert detail['detail']['first']==ts('2026-01-30T12:00:00')
    assert detail['detail']['sessions']==1
    for label in ('유저','User agent'):
        found=engine.page_query({**selected,'usage':{**selected['usage'],'search':label}})['usage']
        assert found['count']==1 and found['summary']==detail['summary']


def test_category_partial_tokens_and_equal_weight_call_tps():
    from cachemonitor.category_usage import summary
    rows=[dict(output=2000,reasoning=500,completion_latency_ms=10000,response_status='completed'),
          dict(output=3000,reasoning=None,completion_latency_ms=30000,response_status='completed'),
          dict(output=None,completion_latency_ms=2000,response_status='completed')]
    assert summary(rows,'tps')==dict(value=150,known=2,total=3,partial=True)
    assert summary(rows,'output')['value']==5000 and summary(rows,'output')['partial']
    assert summary(rows,'reasoning')['value']==500
    assert summary([dict(output=0,completion_latency_ms=100,response_status='completed')],'tps')['value']==0
    assert summary([rows[2]],'tps')['value'] is None
