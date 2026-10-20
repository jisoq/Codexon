"""Paired metrics, owner reuse, and exact drilldown populations."""
from contextlib import closing
from copy import deepcopy

import pytest

from cachemonitor import prepared_store as storage
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.comparison_usage import DIMENSIONS, METRICS, choose_metric, project
from cachemonitor.core import Session
from cachemonitor.prepared_analysis import PreparedAnalysisEngine
from cachemonitor.prepared_ingest import prepare_owner, stage_owner


def sources():
    result=[]
    for number in range(2):
        session=Session(str(number),'home',title=str(number),cwd=f'D:/project-{number}')
        for index in range(4):
            session.add_usage(100+index,f'{number}-{index}',
                dict(input_tokens=8000+index*10000,cached_input_tokens=index*2000,output_tokens=100),
                'gpt-6-astra','turn','high' if index%2 else 'low',service_tier='Standard')
        value=deepcopy(session.view(110))
        for row in value['history']:
            row.update(response_status='completed',timing_valid=True,completion_latency_ms=2000,
                       observation_missing=False,transport_source='response_id')
        result.append(value)
    return result


def publish(store,values,revision,selected=None):
    stage=store.begin(('comparison',revision))
    for ordinal,source in enumerate(values):
        if selected is not None and source['id'] not in selected:continue
        dependencies={name:1 for name in storage.DEPENDENCIES}
        dependencies['source_digest']=f'{revision:064x}'
        stage_owner(stage,prepare_owner(deepcopy(source),owns=lambda *_:True),dependencies,ordinal)
    return stage.publish(('comparison',revision),ready=True)


def query(**changes):
    return dict(page=1,category_usage=True,presentation=True,include_whole_history=False,
                start=0,end=110,now=110,period='all',
                usage=dict(comparison=True,dimension='project',metric='calls'),**changes)


def test_unknown_visibility_covers_each_classification_without_changing_totals():
    from cachemonitor.category_usage import display_rows
    values=sources()
    template=values[0]['history'][0]
    rows=[dict(template,key='known')]
    for field in ('model','effort','service_tier','input','rate'):
        rows.append(dict(template,key=field,**{field:None}))
    values[0]['history']=rows
    values[1].update(source='unknown',cwd='',project=None)
    engine=AnalysisEngine();engine.ingest(values)
    for dimension in DIMENSIONS:
        q=query();q['usage']['dimension']=dimension
        data=project(engine,q)['usage']
        before=deepcopy(data)
        hidden=display_rows(data['rows'],dict(showUnknown=False),True)
        shown=display_rows(data['rows'],dict(showUnknown=True),True)
        assert len(hidden)<len(shown)
        assert all(value!='미확인' for row in hidden for value in row['filters'].values())
        assert data==before


def test_pairs_metrics_and_bands_match_reference_and_drilldown(tmp_path,monkeypatch):
    values=sources();reference=AnalysisEngine();reference.ingest(values)
    with storage.PreparedStore(tmp_path/'comparison.sqlite') as store:
        token=publish(store,values,1)
        with closing(PreparedAnalysisEngine(store.path,token)) as engine:
            monkeypatch.setattr(storage.Snapshot,'_decode_call',
                lambda *a,**kw:pytest.fail('comparison must not decode full call bodies'))
            for dimension in DIMENSIONS:
                q=query();q['usage']['dimension']=dimension
                actual=project(engine,q)['usage'];expected=project(reference,q)['usage']
                assert actual==expected
                assert actual['metrics']['calls']['value']==8
                assert all(set(row['metrics'])==set(METRICS) for row in actual['rows'])
                for row in actual['rows']:
                    selected=query(**row['filters'])
                    detail=project(engine,selected)['usage']
                    assert detail['metrics']==row['metrics']
            q=query();actual=project(engine,q)['usage']
            read=engine.read_metrics['category_projection_rows']
            for metric in METRICS:
                q['usage']['metric']=metric
                assert project(engine,q)['usage']['summary']==choose_metric(actual,metric)['summary']
            assert engine.read_metrics['category_projection_rows']==read


def test_corrections_deletions_and_unchanged_owners_reuse_only_valid_results(tmp_path):
    values=sources()
    with storage.PreparedStore(tmp_path/'comparison.sqlite') as store:
        token=publish(store,values,1)
        with closing(PreparedAnalysisEngine(store.path,token)) as engine:
            assert project(engine,query())['usage']['summary']['value']==8
            read=engine.read_metrics['category_projection_rows']
            values[0]['history'][0]['output']=900
            token=publish(store,values,2,['0']);engine.adopt(token,[('home','0')])
            actual=project(engine,query())['usage']
            reference=AnalysisEngine();reference.ingest(deepcopy(values))
            assert actual==project(reference,query())['usage']
            assert engine.read_metrics['category_projection_rows']-read==4
            read=engine.read_metrics['category_projection_rows']
            values[0]['history'].pop()
            token=publish(store,values,3,['0']);engine.adopt(token,[('home','0')])
            assert project(engine,query())['usage']['summary']['value']==7
            assert engine.read_metrics['category_projection_rows']-read==3


def test_missing_values_keep_coverage_and_means_use_known_calls():
    from cachemonitor.comparison_usage import Totals
    total=Totals()
    total.add(dict(input=100,output=20,cost=2))
    total.add(dict(input=0,output=None,cost=None))
    metrics=total.result()
    assert metrics['input_per_call']['value']==50
    assert metrics['cost_per_call']==dict(value=2,known=1,total=2,partial=True)
    assert metrics['tps']==dict(value=None,known=0,total=2,partial=True)
