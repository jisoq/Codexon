import copy
from cachemonitor.cache_misses import classify
from cachemonitor.core import Session
from cachemonitor.analysis_engine import AnalysisEngine


def session_view(now=100):
    session=Session('miss-session','fixture',title='Cache miss review')
    for i,cached in enumerate((0,8000,0,0,None,0)):
        usage=dict(input_tokens=10000+i*100,output_tokens=100,cache_write_input_tokens=0,reasoning_output_tokens=50)
        if cached is not None:usage['cached_input_tokens']=cached
        session.add_usage(now+i,str(i),usage,'gpt-6-astra','turn-a' if i<3 else 'turn-b','high',service_tier='Standard')
    session.add_usage(now+5,'5',usage,'gpt-6-astra','turn-b','high',service_tier='Standard')
    return session.view(now+6)


def query(start=0,end=200):
    return dict(page=2,start=start,end=end,model='',source='',archived=True,unit='response',method='mean',metric='cost')


def test_positive_input_zero_read_includes_first_and_is_not_error():
    source=session_view();summary=source['cache_misses']
    assert summary['count']==4 and summary['input']==41000 and summary['current']
    assert [r['key'] for r in summary['events']]==['0','2','3','5']
    assert not source['warning']
    rows=copy.deepcopy(source['history']);rows[-1].update(cached=1,input=1)
    assert classify(rows)['count']==3 and not classify(rows)['current']
    rows[-1].update(cached=0,input=0)
    assert classify(rows)['count']==3  # Zero input has no input cache-rate observation.
    rows[-1].update(cached=0,input=None)
    assert classify(rows)['count']==3


def test_fact_and_incident_classification_precedes_filters_and_backfill():
    engine=AnalysisEngine();source=session_view();engine.ingest([source])
    result=engine.query(query(start=102))
    assert result['analysis']['sessions'][0]['cache_misses']['count']==4
    assert [r['key'] for r in result['analysis']['responses'] if r['cache_miss']]==['2','3','5']
    assert engine.query(query())['analysis']['responses'][0]['cache_miss']
    earlier=copy.deepcopy(source['history'][0]);earlier.update(key='earlier',ts=99,cached=5000)
    source['history'].insert(0,earlier);engine.ingest([source])
    assert engine.query(query())['analysis']['sessions'][0]['cache_misses']['count']==4
    assert engine.record('fixture','miss-session','0')['cache_miss']
