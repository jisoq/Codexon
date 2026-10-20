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
    assert [r['id'] for r in result['recent']]==[f'call-{i}' for i in range(10,30)]
    assert [r['ordinal'] for r in result['recent']]==list(range(11,31))
    latest=result['latest']
    assert latest is result['recent'][-1]
    for key in ('model','response_model','effort','mode','transport','cache_rate'):
        assert result[key]==latest[key]
    assert result['latest_cost'] is latest['cost'] is None
    assert result['cache_rate']==0 and latest['cache_miss']
    assert result['response_model'] is None
    assert latest['model']=='actual-request' and not latest['model_setting']
    models={item['model']:item for item in result['model_breakdown']}
    assert models['actual-request']['calls']==1 and models['actual-request']['cost'] is None
    assert latest['mode']=='Fast' and latest['transport_state']=='확정'
    rows.append(row(30))
    assert summarize_session(session(rows))['recent'][-2]['id']==latest['id']
    rows[-1].update(analysis_model='conflicting-model',model_conflict=True)
    conflicted=summarize_session(session(rows))
    assert conflicted['latest']['model']=='미확인'
    assert next(item for item in conflicted['model_breakdown'] if item['model']=='미확인')['calls']==1


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


def test_prepared_overlay_detail_revision_tracks_descendants_and_removed_members():
    from types import SimpleNamespace
    from cachemonitor.prepared_ingest import prepare_owner
    from cachemonitor.prepared_health import split_summary
    from cachemonitor.prepared_overlays import PreparedOverlays, hydrate_overlay
    from cachemonitor.prepared_store import Limits
    from cachemonitor.prepared_summary import summary_for_storage

    sources={name:session([row(i,key=name)],id=name,**metadata) for i,(name,metadata) in enumerate((
        ('parent',{}),('child',{'parent_thread_id':'parent'}),('other',{})))}
    owners={};packets={}
    def replace_owner(name):
        source=sources[name];key=('home',name)
        material=prepare_owner(source,owns=lambda *_:True)
        owners[key]={'metadata':{k:v for k,v in source.items() if k!='history'}}
        packets[key]=summary_for_storage(split_summary(material['summary'])[0])
    sources['parent']['history'][0]['service_tier']='priority'
    sources['parent']['request_state']=dict(turn='turn',state='완료')
    sources['child']['request_state']=dict(turn='turn',state='진행')
    for name in sources:replace_owner(name)
    view=SimpleNamespace(publication_token=SimpleNamespace(instance='test',generation=1),
        limits=Limits(), check=lambda:None, owner=lambda home,sid:owners.get((home,sid)),
        derived=lambda home,sid,kind:packets[(home,sid)])
    overlays=PreparedOverlays()
    def collect(changed,removed=()):
        compact=overlays.collect(view,owners,changed,removed,{})
        assert all('model_breakdown' not in item and 'subagent_finance' not in item for item in compact)
        return {item['id']:hydrate_overlay(view,dict(item,_recent_refs=False),finance=True) for item in compact}
    first=collect(None)
    assert first['parent']['mode']=='Standard'
    assert first['parent']['main_usage']['mode']=='Fast'
    assert first['parent']['subagent_finance']['agents'][0]['mode']=='Standard'
    assert first['parent']['main_usage']['request_state']=='완료'
    assert first['parent']['subagent_finance']['agents'][0]['request_state']=='진행'
    view.publication_token.generation=2
    sources['other']['history'][0]['output']+=1;replace_owner('other')
    second=collect({('home','other')})
    assert second['parent']['_prepared_token']!=first['parent']['_prepared_token']
    assert second['parent']['_overlay_revision']==first['parent']['_overlay_revision']
    assert second['child']['_overlay_revision']==first['child']['_overlay_revision']
    assert second['other']['_overlay_revision']!=first['other']['_overlay_revision']
    view.publication_token.generation=3
    sources['child']['history'][0]['output']+=1;replace_owner('child')
    sources['child']['request_state']['state']='완료';replace_owner('child')
    third=collect({('home','child')})
    assert [item['key'] for item in third['parent']['recent']]==[item['key'] for item in second['parent']['recent']]
    assert third['parent']['_overlay_revision']!=second['parent']['_overlay_revision']
    assert third['child']['_overlay_revision']!=second['child']['_overlay_revision']
    assert third['parent']['subagent_finance']['models']!=second['parent']['subagent_finance']['models']
    assert third['parent']['own_cost']==second['parent']['own_cost']
    assert third['parent']['subagent_finance']['agents'][0]['request_state']=='완료'
    view.publication_token.generation=4
    owners.pop(('home','child'))
    fourth=collect(set(),{('home','child')})
    assert fourth['parent']['calls']==1 and 'child' not in fourth
    assert 'subagent_finance' not in fourth['parent']
    assert fourth['parent']['_overlay_revision']!=third['parent']['_overlay_revision']
    assert set(overlays.detail_revisions)==set(owners)
