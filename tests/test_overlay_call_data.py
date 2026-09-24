import copy


from cachemonitor.analysis_engine import AnalysisEngine
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
