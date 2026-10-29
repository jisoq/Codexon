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


def test_request_modes_stay_separate_normalize_and_drill_down_in_every_group(tmp_path):
    from cachemonitor.category_usage import display_rows
    values=sources()
    modes=[dict(service_tier='standard'),dict(service_tier='default'),
           dict(service_tier='fast'),dict(service_tier='priority'),dict(service_tier=None),
           dict(service_tier='Fast',mode_conflict=True),
           dict(service_tier='Standard',service_tier_source='conflict'),
           dict(service_tier='Fast',display_service_tier='Standard'),dict(service_tier='Experimental')]
    for source in values:
        source['source']='user'
        template=source['history'][0]
        source['history']=[dict(template,key=f"{source['id']}-{i}",effort='high',**mode)
                           for i,mode in enumerate(modes)]
    reference=AnalysisEngine();reference.ingest(values)
    with storage.PreparedStore(tmp_path/'modes.sqlite') as store:
        token=publish(store,values,1)
        with closing(PreparedAnalysisEngine(store.path,token)) as engine:
            total=project(engine,all_query())['usage']['metrics']
            for dimension in DIMENSIONS:
                q=all_query();q['usage']['dimension']=dimension
                data=project(engine,q)['usage']
                assert data==project(reference,q)['usage']
                assert data['metrics']==total and data['summary']['value']==18
                counts={mode:sum(row['metrics']['calls']['value'] for row in data['rows']
                                 if row['filters']['service_tier']==mode)
                        for mode in ('Standard','Fast','미확인','Experimental')}
                assert counts==dict(Standard=6,Fast=4,미확인=6,Experimental=2)
                assert len({row['key'] for row in data['rows']})==len(data['rows'])
                for row in data['rows']:
                    mode=row['filters']['service_tier']
                    assert ('⚡' in row['label'])==(mode=='Fast')
                    if mode in ('Standard','Fast'):assert mode not in row['label']
                    selected=all_query();selected.update(row['filters'])
                    detail=project(engine,selected)['usage']
                    assert detail['metrics']==row['metrics']
                    assert len(detail['rows'])==1
                    assert detail['rows'][0]['distributions']==row['distributions']
                visible=display_rows(data['rows'],dict(showUnknown=False),True)
                assert sum(row['value'] for row in visible)==12
                assert all(row['filters']['service_tier']!='미확인' for row in visible)
                assert data['metrics']==total


def test_name_order_uses_numeric_versions_families_efforts_and_keeps_parent_groups(monkeypatch):
    from cachemonitor.category_usage import display_rows
    expected=[('gpt-6.10-sol','low'),('gpt-6.9-astra','ultra'),('gpt-6.1.1-luna','low'),
              *[('gpt-6.1-astra',effort) for effort in ('ultra','max','xhigh','high','medium','low','minimal','미확인')],
              ('gpt-6.1-sol','ultra'),('gpt-6.1-terra','high'),('gpt-6.1-luna','high'),('gpt-6.1.0-luna','high'),
              ('gpt-6.1-daybreak','high'),('gpt-6.1','high'),('gpt-6.1-cobalt','high'),
              ('gpt-6-sol','high'),('gpt-5.6-astra','ultra'),('gpt-daybreak-blue-latest','high'),
              ('custom-model','high'),('미확인','high')]
    calls=[dict(scalar_call(index+1),model=model,effort=effort,service_tier=mode,project=parent)
           for parent in ('Z project','A project') for index,(model,effort) in enumerate(reversed(expected))
           for mode in ('Standard','Fast')]
    engine=ProjectedEngine({('home','one'):calls})
    for dimension in ('all','project'):
        q=all_query();q['usage'].update(dimension=dimension,sort='name')
        data=project(engine,q)['usage']
        groups=['전체'] if dimension=='all' else ['A project','Z project']
        ordered=[(parent,model,effort,mode) for parent in groups for model,effort in expected
                 for mode in ('Fast','Standard')]
        def identity(row):
            return (row['group'],row['filters']['model'],row['filters']['effort'],row['filters']['service_tier'])
        assert [identity(row) for row in data['rows']]==ordered
        with monkeypatch.context() as guard:
            guard.setattr(engine,'projected_calls',lambda *a,**kw:pytest.fail('Display changes must not read records'))
            for metric in METRICS:
                selected=choose_metric(data,metric)
                shown=display_rows(selected['rows'],dict(sort='name',showUnknown=True),True)
                assert [identity(row) for row in shown]==ordered
            for order in ('asc','desc'):
                shown=display_rows(choose_metric(data,'input_per_call')['rows'],dict(sort=order,showUnknown=True),True)
                assert [row['group'] for row in shown]==sorted(row['group'] for row in shown)
                for parent in groups:
                    amounts=[row['value'] for row in shown if row['group']==parent]
                    assert amounts==sorted(amounts,reverse=order=='desc')


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


def scalar_call(value,**changes):
    return dict(model='gpt-6-astra',effort='high',cost=value,input=value,output=value,
                reasoning=value,response_status='completed',timing_valid=True,
                completion_latency_ms=2000,**changes)


class ProjectedEngine:
    def __init__(self,owners):
        from types import SimpleNamespace
        from cachemonitor.comparison_usage import LIMIT
        self.owners=owners;self.versions={key:1 for key in owners};self.proofs={};self.contexts={}
        self.reader=SimpleNamespace(limits=SimpleNamespace(page_objects_bytes=LIMIT))
        self.snapshot=self;self.revision=1;self.read_metrics={}

    def query_key(self,q):return (self.revision,repr(q))
    def boundary_times(self,_):return ()
    def read_scope(self):
        from contextlib import nullcontext
        return nullcontext()

    def call_ranges(self,scope):
        for owner in self.owners:
            yield dict(owner=owner,revision=self.versions[owner],call_owner=self.contexts.get(owner,{})),scope['start'],scope['end']

    def owner(self,*owner):return dict(dependencies={'source_digest':str(self.versions[owner])})
    def derived(self,*args):return dict(comparison_append=self.proofs.get(args[:2]))

    def projected_calls(self,owner,fields,*,after=None,ordinal_start=None,**kwargs):
        from types import SimpleNamespace
        start=max(after or 0,(ordinal_start or 1)-1);end=start+256
        rows=self.owners[owner]
        return SimpleNamespace(rows=rows[start:end],after=end,complete=end>=len(rows))

    def replace(self,owner,rows,append=False):
        from cachemonitor.comparison_usage import dependency_stamp
        prior=dependency_stamp(self.owner(*owner)['dependencies']);count=len(self.owners[owner])
        self.owners[owner]=rows;self.versions[owner]+=1;self.revision+=1
        self.proofs[owner]=(dict(format=1,prior=prior,target=dependency_stamp(self.owner(*owner)['dependencies']),
                                 ordinal=count) if append else None)


def all_query(metric='calls'):
    q=query();q['usage'].update(dimension='all',metric=metric,showUnknown=True)
    return q


@pytest.mark.parametrize('values',[
    [],[0],[7],[3]*9,list(range(10)),[2**53+1,2**53+2],[0,0.0,-0.0,1.0,1,2**53+1,2**64-1,2**80,2**80+1],
    [0]*9+[1000],
])
def test_exact_box_populations_match_existing_interpolation(values):
    from cachemonitor.analytics import sorted_quantile
    from cachemonitor.comparison_usage import MEAN_FIELDS
    rows=[scalar_call(value) for value in values]
    engine=ProjectedEngine({('home','one'):rows})
    data=project(engine,all_query())['usage']
    assert data['summary']['value']==len(values)
    if not values:
        assert data['rows']==[]
        return
    row=data['rows'][0]
    valid_speed=[v for v in values if type(v) is int]
    populations={key:values if field in ('cost','input','output','reasoning') else [2.0]*len(valid_speed) if field=='latency'
                 else [v/2 for v in valid_speed] for key,field in MEAN_FIELDS.items()}
    for metric,population in populations.items():
        expected=dict(kind='box',**{key:sorted_quantile(sorted(population),p)
                                   for key,p in [('p10',.1),('q1',.25),('median',.5),('q3',.75),('p90',.9)]})
        assert row['distributions'][metric]==expected
        assert row['metrics'][metric]['value']==sum(population)/len(population)
        assert row['metrics'][metric]['known']==len(population)
    assert row['metrics']['calls']==dict(value=len(values),known=len(values),total=len(values),partial=False)


def test_box_statistics_mean_coverage_and_local_switches_are_independent(monkeypatch):
    rows=[scalar_call(v) for v in range(10)]
    rows+=[dict(model='gpt-6-astra',effort='high',cost=None,input=0,output=None),
           dict(model='gpt-6-astra',effort='high',cost=True,input=-1,output=float('inf')),
           dict(model='gpt-6-astra',effort='high',cost=float('nan'),input=None,output=0,
                response_status='completed',completion_latency_ms=1000,timing_valid=False)]
    engine=ProjectedEngine({('home','one'):rows})
    data=project(engine,all_query())['usage'];row=data['rows'][0]
    assert row['distributions']['cost_per_call']==dict(kind='box',p10=.9,q1=2.25,median=4.5,q3=6.75,p90=8.1)
    assert row['metrics']['cost_per_call']==dict(value=4.5,known=10,total=13,partial=True)
    assert row['metrics']['input_per_call']==dict(value=45/11,known=11,total=13,partial=True)
    assert row['metrics']['output_per_call']==dict(value=45/11,known=11,total=13,partial=True)
    assert row['metrics']['latency']==dict(value=2.0,known=10,total=13,partial=True)
    assert row['metrics']['tps']==dict(value=2.25,known=10,total=13,partial=True)
    monkeypatch.setattr(engine,'projected_calls',lambda *a,**kw:pytest.fail('Selection must not read calls'))
    for metric in METRICS:
        selected=choose_metric(data,metric)
        assert selected['rows'][0]['plot']==row['distributions'].get(metric)
        assert selected['summary']==data['metrics'][metric]
        assert project(engine,all_query(metric))['usage']['rows'][0]['plot']==row['distributions'].get(metric)
    skew=project(ProjectedEngine({('home','one'):[scalar_call(v) for v in [0]*9+[1000]]}),all_query('cost_per_call'))['usage']
    assert skew['rows'][0]['value']==100
    assert skew['markMaximum']==pytest.approx(105)
    tiny=project(ProjectedEngine({('home','one'):[scalar_call(.0001)]}),all_query('cost_per_call'))['usage']
    assert tiny['markMaximum']==pytest.approx(.000105)


def test_reasoning_uses_known_calls_across_unequal_owners_without_timing_requirements():
    owners={
        ('home','one'):[dict(scalar_call(100),reasoning=0,completion_latency_ms=1000),
                        dict(scalar_call(100),reasoning=30,completion_latency_ms=10000)],
        ('home','two'):[dict(scalar_call(100),reasoning=value,response_status='unknown',
                            timing_valid=False,completion_latency_ms=None)
                        for value in (90,None,True,-1,float('inf'),float('nan'))],
    }
    engine=ProjectedEngine(owners)
    data=project(engine,all_query('reasoning'))['usage']
    expected=dict(value=40,known=3,total=8,partial=True)
    assert data['summary']==data['rows'][0]['metrics']['reasoning']==expected
    assert data['rows'][0]['plot']==dict(kind='box',p10=6,q1=15,median=30,q3=60,p90=78)
    assert data['metrics']['tps']==dict(value=55,known=2,total=8,partial=True)
    assert project(engine,all_query('reasoning'))['usage']==data
    assert engine.read_metrics['category_projection_rows']==8


@pytest.mark.parametrize('values,mean,known,plot',[
    ([0,0],0,2,dict(kind='box',p10=0,q1=0,median=0,q3=0,p90=0)),
    ([None,True,-1,float('inf'),float('nan')],None,0,None),
])
def test_reasoning_zero_and_unknown_calls_preserve_coverage(values,mean,known,plot):
    rows=[dict(scalar_call(100),reasoning=value,timing_valid=False) for value in values]
    data=project(ProjectedEngine({('home','one'):rows}),all_query('reasoning'))['usage']
    assert data['summary']==dict(value=mean,known=known,total=len(values),partial=known!=len(values))
    assert data['rows'][0]['plot']==plot


def test_comparison_reasoning_mean_preserves_raw_reasoning_totals():
    values=sources()
    for source,reasoning in zip(values,([0,30,None,None],[90,None,None,None])):
        for row,value in zip(source['history'],reasoning):row['reasoning']=value
    engine=AnalysisEngine();engine.ingest(values)
    q=query();q['usage'].update(metric='reasoning')
    assert project(engine,q)['usage']['summary']==dict(value=40,known=3,total=8,partial=True)
    q['usage']=dict(dimension='model',metric='reasoning')
    assert engine.page_query(q)['usage']['summary']==dict(value=120,known=3,total=8,partial=True)


def test_certified_append_correction_deletion_and_reuse_preserve_distributions():
    owners={('home','one'):[scalar_call(i) for i in range(9)],('home','two'):[scalar_call(20)]}
    engine=ProjectedEngine(owners);project(engine,all_query())
    unchanged=engine._comparison_cache['entries'][('home','two')]
    before=deepcopy(engine._comparison_cache);reads=engine.read_metrics['category_projection_rows']
    engine.replace(('home','one'),owners[('home','one')]+[scalar_call(50)],append=True)
    actual=project(engine,all_query())['usage']
    assert engine.read_metrics['category_projection_rows']-reads==1
    assert engine._comparison_cache['entries'][('home','two')] is unchanged
    assert before['entries'][('home','one')]['groups'][('전체','gpt-6-astra','high','미확인')][1].totals.count==9
    assert actual==project(ProjectedEngine(deepcopy(engine.owners)),all_query())['usage']
    from cachemonitor.prepared_store import _size
    for entry in engine._comparison_cache['entries'].values():
        assert entry['weight']>=sum(256+_size(key)+_size(label)+value.retained_bytes()
                                     for key,(label,value) in entry['groups'].items())
    for rows in ([scalar_call(3)],[]):
        engine.replace(('home','one'),rows)
        assert project(engine,all_query())['usage']==project(ProjectedEngine(deepcopy(engine.owners)),all_query())['usage']


def test_fifty_thousand_distinct_calls_fit_existing_capacity():
    from cachemonitor.analytics import sorted_quantile
    from cachemonitor.comparison_usage import LIMIT,MEAN_FIELDS
    owners={('home',str(i)):[] for i in range(20)};populations={i:[] for i in range(8)}
    for i in range(50000):
        group=i%8;value=i+1
        row=scalar_call(value);row.update(model=f'model-{group}',cost=value/100000,completion_latency_ms=value+1000)
        owners[('home',str(i%20))].append(row);populations[group].append(row)
    engine=ProjectedEngine(owners);data=project(engine,all_query('latency'))['usage']
    assert len(data['rows'])==8 and data['summary']['known']==50000
    for row in data['rows']:
        calls=populations[int(row['filters']['model'].split('-')[1])]
        for metric,field in MEAN_FIELDS.items():
            values=[r['output']/(r['completion_latency_ms']/1000) if metric=='tps'
                    else r['completion_latency_ms']/1000 if metric=='latency' else r[field] for r in calls]
            ordered=sorted(values)
            assert row['distributions'][metric]==dict(kind='box',**{key:sorted_quantile(ordered,p)
                for key,p in [('p10',.1),('q1',.25),('median',.5),('q3',.75),('p90',.9)]})
            assert row['metrics'][metric]['known']==6250
            assert row['metrics'][metric]['value']==pytest.approx(sum(values)/6250)
    for key in ('comparison_cache_bytes','comparison_working_bytes','comparison_working_peak_bytes','comparison_result_bytes'):
        assert 0<engine.read_metrics[key]<=LIMIT
    import json
    from pathlib import Path
    path=Path(__file__).resolve().parents[1]/'artifacts/verification/comparison-boxplot/capacity.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(dict(calls=50000,owners=20,groups=8,budget=LIMIT,**engine.read_metrics),indent=2),encoding='utf-8')


def test_many_sessions_with_multiple_input_bands_keep_exact_cold_and_warm_results():
    inputs=(1000,20000,60000,120000)
    owners={}
    for owner in range(1000):
        rows=[]
        for base in inputs:
            for index in range(21):
                row=scalar_call(index+1)
                row.update(input=base+index,cost=(index+1)/8)
                rows.append(row)
        owners[('home',str(owner))]=rows
    engine=ProjectedEngine(owners)
    q=all_query('cost_per_call');q['usage']['dimension']='input_band'
    cold=project(engine,q);warm=project(engine,q)
    assert cold==warm
    assert cold['analysis']==dict(response_count=84000,session_count=1000)
    data=cold['usage']
    expected_means=dict(cost_per_call=1.375,latency=2.0,tps=5.5,input_per_call=50260,output_per_call=11,reasoning=11)
    assert data['metrics']['calls']==dict(value=84000,known=84000,total=84000,partial=False)
    for metric,value in expected_means.items():
        assert data['metrics'][metric]==dict(value=value,known=84000,total=84000,partial=False)
    assert len(data['rows'])==4
    assert {row['metrics']['input_per_call']['value'] for row in data['rows']}=={base+10 for base in inputs}
    quantiles=dict(p10=3,q1=6,median=11,q3=16,p90=19)
    for row in data['rows']:
        base=row['metrics']['input_per_call']['value']-10
        assert row['metrics']['calls']==dict(value=21000,known=21000,total=21000,partial=False)
        for metric,value in dict(expected_means,input_per_call=base+10).items():
            assert row['metrics'][metric]==dict(value=value,known=21000,total=21000,partial=False)
        expected_boxes=dict(
            cost_per_call={key:value/8 for key,value in quantiles.items()},
            latency=dict.fromkeys(quantiles,2.0),
            tps={key:value/2 for key,value in quantiles.items()},
            input_per_call={key:base+value-1 for key,value in quantiles.items()},
            output_per_call=quantiles,reasoning=quantiles)
        assert row['distributions']=={metric:dict(kind='box',**box) for metric,box in expected_boxes.items()}


@pytest.mark.parametrize('failure',['growth','sorting','large_integer','delivery'])
def test_capacity_failures_preserve_published_aggregate(failure,monkeypatch):
    from cachemonitor import comparison_usage as comparison
    from cachemonitor.prepared_store import CapacityExceeded
    engine=ProjectedEngine({('home','one'):[scalar_call(1)]})
    accepted=project(engine,all_query())['usage'];published=engine._comparison_cache
    retained=deepcopy(published)
    if failure=='growth':
        engine.replace(('home','one'),engine.owners[('home','one')]+[scalar_call(i) for i in range(1,1000)],append=True)
        engine.reader.limits.page_objects_bytes=12000
    elif failure=='large_integer':
        engine.replace(('home','one'),[scalar_call(2**1000)]*300,append=False)
        engine.reader.limits.page_objects_bytes=20000
    elif failure=='sorting':
        rows=[scalar_call(i) for i in range(1000)]
        probe=ProjectedEngine({('home','one'):rows});project(probe,all_query())
        engine.replace(('home','one'),rows)
        engine.reader.limits.page_objects_bytes=probe.read_metrics['comparison_working_peak_bytes']-1
    else:
        original=comparison._size
        monkeypatch.setattr(comparison,'_size',lambda value:comparison.LIMIT+1 if isinstance(value,dict) and value.get('comparison') is True else original(value))
    with pytest.raises(CapacityExceeded):project(engine,all_query())
    assert engine._comparison_cache is published
    assert published['end']==retained['end'] and published['signature']==retained['signature']
    for owner,entry in published['entries'].items():
        for key,(_,aggregate) in entry['groups'].items():
            prior=retained['entries'][owner]['groups'][key][1]
            assert aggregate.totals.result()==prior.totals.result()
            budget=comparison._Budget(comparison.LIMIT)
            assert aggregate.distributions(budget)==prior.distributions(budget)
    if failure!='delivery':
        engine.reader.limits.page_objects_bytes=comparison.LIMIT
        assert project(engine,all_query())['usage']==project(ProjectedEngine(deepcopy(engine.owners)),all_query())['usage']
    assert accepted['summary']['value']==1


def test_empty_metric_populations_deliver_no_plot_or_invented_coverage():
    engine=ProjectedEngine({('home','one'):[dict(model='gpt-6-astra',effort='high',cost=None,input=True,output=-1)]})
    data=project(engine,all_query())['usage'];row=data['rows'][0]
    assert row['distributions']==dict.fromkeys(('cost_per_call','latency','tps','input_per_call','output_per_call','reasoning'))
    for metric in row['distributions']:
        selected=choose_metric(data,metric)
        assert selected['rows'][0]['plot'] is None
        assert selected['summary']==dict(value=None,known=0,total=1,partial=True)
        assert selected['markMaximum']==1


def exact_scalars(value):
    if type(value) is float:return ('float',value.hex())
    if isinstance(value,dict):return {key:exact_scalars(child) for key,child in value.items()}
    if isinstance(value,(tuple,list)):return [exact_scalars(child) for child in value]
    return (type(value).__name__,value)


@pytest.mark.parametrize('populations',[
    [[-0.0]],[[0,-0.0],[0.0,0,1]],
    [[2**53+1,2**53+2],[2**64-1,2**64,2**80+1]],
    [[0.0,2**64,2**1000],[0,2**53+1,1e308]],
])
def test_owner_fold_preserves_scalar_types_stable_ties_and_exact_sums(populations):
    from cachemonitor.analytics import output_speed,sorted_quantile
    from cachemonitor.comparison_usage import MEAN_FIELDS
    owners={('home',str(i)):[scalar_call(value) for value in values] for i,values in enumerate(populations)}
    row=project(ProjectedEngine(owners),all_query())['usage']['rows'][0]
    for metric,field in MEAN_FIELDS.items():
        parts=[]
        for rows in owners.values():
            parts.append([call[field] for call in rows] if field in ('cost','input','output','reasoning')
                         else [2.0 for call in rows if output_speed(call) is not None] if field=='latency'
                         else [output_speed(call) for call in rows if output_speed(call) is not None])
        values=[value for part in parts for value in part]
        ordered=sorted(values)
        expected=(dict(kind='box',**{key:sorted_quantile(ordered,p) for key,p in
                  [('p10',.1),('q1',.25),('median',.5),('q3',.75),('p90',.9)]}) if values else None)
        assert exact_scalars(row['distributions'][metric])==exact_scalars(expected)
        expected_mean=sum(sum(part) for part in parts)/len(values) if values else None
        assert exact_scalars(row['metrics'][metric]['value'])==exact_scalars(expected_mean)


@pytest.mark.parametrize('values',[(1,0.25,0.125,2**1000),(1,2**31,2**1000)])
def test_totals_charge_every_scalar_after_integer_growth_and_sum_transitions(values):
    import sys
    from cachemonitor.comparison_usage import Totals
    from cachemonitor.prepared_store import _size
    total=Totals()
    for value in values:
        total.add(scalar_call(value))
        actual=(sys.getsizeof(total)+sys.getsizeof(total.count)+_size(total.known)+sys.getsizeof(total.sums)
                +sum(_size(key)+sys.getsizeof(accumulator)
                     +sum(sys.getsizeof(getattr(accumulator,name)) for name in accumulator.__slots__)
                     for key,accumulator in total.sums.items()))
        assert total.retained_bytes()>=actual


def test_warm_fold_delivers_current_metadata_and_independent_mutable_values():
    engine=ProjectedEngine({('home','one'):[scalar_call(1),scalar_call(5)]})
    engine.boundary_times=lambda _: (200,)
    first=project(engine,all_query())
    first['usage']['rows'][0]['metrics']['input_per_call']['value']=999
    first['usage']['rows'][0]['distributions']['input_per_call']['median']=999
    first['usage']['rows'][0]['filters']['model']='changed'
    first['usage']['rows'][0]['components'].append('changed')
    q=all_query('input_per_call');q.update(now=120,end=120,loading=True,_prepared_token=['new'])
    engine.revision+=1;engine.versions[('home','one')]+=1
    engine.owner=lambda *args:dict(dependencies={'source_digest':'1'})
    engine.boundary_times=lambda _: (300,)
    refreshed=project(engine,q)
    assert refreshed['valid_until']==300.002
    assert refreshed['valid_until']!=first['valid_until']
    assert refreshed['revision']==engine.revision
    assert refreshed['key']==engine.query_key({key:value for key,value in q.items() if key not in ('usage','presentation')})
    expected=project(ProjectedEngine(deepcopy(engine.owners)),q)
    assert exact_scalars(refreshed['usage'])==exact_scalars(expected['usage'])


def test_owner_order_removal_and_context_changes_invalidate_the_fold():
    owners={('home','one'):[scalar_call(0),scalar_call(-0.0)],
            ('home','two'):[scalar_call(0.0),scalar_call(0),scalar_call(1)]}
    engine=ProjectedEngine(owners);project(engine,all_query())
    for selected in (dict(reversed(list(owners.items()))),{('home','one'):owners[('home','one')]}):
        engine.owners=selected
        actual=project(engine,all_query())
        expected=project(ProjectedEngine(deepcopy(selected)),all_query())
        assert exact_scalars(actual['usage'])==exact_scalars(expected['usage'])
        assert actual['analysis']==expected['analysis']
    owner=('home','one');engine.owners[owner]=[dict(row,project='project',project_name='Old') for row in owners[owner]]
    engine.contexts[owner]=dict(project='project',project_name='Old')
    q=all_query();q['usage']['dimension']='project'
    project(engine,q)
    engine.contexts[owner]['project_name']='New'
    engine.owners[owner]=[dict(row,project_name='New') for row in engine.owners[owner]]
    assert project(engine,q)['usage']['rows'][0]['group_label']=='New'


def test_compaction_without_replacement_headroom_keeps_exact_logical_populations():
    from cachemonitor.analytics import sorted_quantile
    from cachemonitor.comparison_usage import GroupAggregate,LIMIT,MEAN_FIELDS,_Budget
    aggregate=GroupAggregate();budget=_Budget(LIMIT);budget.reserve(aggregate.retained_bytes())
    for i in range(9):aggregate.add(scalar_call(i),budget)
    before=budget.used;budget.maximum=before
    aggregate.compact(budget)
    assert budget.used==before
    actual=aggregate.distributions(_Budget(LIMIT))
    for metric,field in MEAN_FIELDS.items():
        values=[2.0]*9 if field=='latency' else [i/2 for i in range(9)] if field=='tps' else list(range(9))
        assert exact_scalars(actual[metric])==exact_scalars(dict(kind='box',**{
            key:sorted_quantile(values,p) for key,p in [('p10',.1),('q1',.25),('median',.5),('q3',.75),('p90',.9)]}))


def test_optional_fold_is_dropped_when_reader_limit_shrinks_and_can_be_rebuilt():
    from cachemonitor.comparison_usage import LIMIT
    from cachemonitor.prepared_store import _size
    engine=ProjectedEngine({('home',str(i)):[scalar_call(i)] for i in range(20)})
    original=project(engine,all_query())
    fold=engine._comparison_cache['fold']
    owner_bytes=engine.read_metrics['comparison_cache_bytes']-64-_size('fold')-_size(fold)
    engine.reader.limits.page_objects_bytes=owner_bytes
    refreshed=project(engine,all_query())
    assert exact_scalars(refreshed)==exact_scalars(original)
    assert engine._comparison_cache.get('fold') is None
    assert engine.read_metrics['comparison_cache_bytes']<=owner_bytes
    engine.reader.limits.page_objects_bytes=owner_bytes-1
    fallback=project(engine,all_query())
    assert exact_scalars(fallback)==exact_scalars(original)
    assert engine.read_metrics['comparison_cache_bytes']<owner_bytes
    assert engine.read_metrics['comparison_working_peak_bytes']<owner_bytes
    engine.reader.limits.page_objects_bytes=LIMIT
    assert exact_scalars(project(engine,all_query()))==exact_scalars(original)
    assert engine._comparison_cache.get('fold') is not None


def test_repeated_certified_appends_fit_the_original_geometric_clone_capacity():
    from cachemonitor import comparison_usage as comparison
    engine=ProjectedEngine({('home',str(i)):[scalar_call(j) for j in range(9)] for i in range(12)})
    project(engine,all_query())
    engine.reader.limits.page_objects_bytes=max(engine.read_metrics[key] for key in
        ('comparison_cache_bytes','comparison_working_peak_bytes','comparison_result_bytes'))
    owner=('home','11')
    for value in range(9,15):
        before=deepcopy(engine._comparison_cache);reads=engine.read_metrics['category_projection_rows']
        engine.replace(owner,engine.owners[owner]+[scalar_call(value)],append=True)
        actual=project(engine,all_query())
        expected=project(ProjectedEngine(deepcopy(engine.owners)),all_query())
        assert exact_scalars(actual['usage'])==exact_scalars(expected['usage'])
        assert engine.read_metrics['category_projection_rows']-reads==1
        old=before['entries'][owner]['groups'][('전체','gpt-6-astra','high','미확인')][1]
        assert old.totals.count==value
        for entry in engine._comparison_cache['entries'].values():
            assert entry['weight']>=sum(256+comparison._size(key)+comparison._size(label)+aggregate.retained_bytes()
                for key,(label,aggregate) in entry['groups'].items())


def test_tag_promotion_failure_does_not_publish_partial_append(monkeypatch):
    from cachemonitor import comparison_usage as comparison
    from cachemonitor.prepared_store import CapacityExceeded
    engine=ProjectedEngine({('home','one'):[scalar_call(i) for i in range(9)]})
    accepted=project(engine,all_query());published=engine._comparison_cache
    previous=deepcopy(published);grow=comparison._Observations._grow
    engine.replace(('home','one'),engine.owners[('home','one')]+[scalar_call(-0.0)],append=True)
    def fail_tags(self,name,required,budget):
        if name=='order':raise CapacityExceeded('tag promotion rejected')
        return grow(self,name,required,budget)
    with monkeypatch.context() as patch:
        patch.setattr(comparison._Observations,'_grow',fail_tags)
        with pytest.raises(CapacityExceeded):project(engine,all_query())
    assert engine._comparison_cache is published
    owner=('home','one');key=('전체','gpt-6-astra','high','미확인')
    aggregate=published['entries'][owner]['groups'][key][1]
    prior=previous['entries'][owner]['groups'][key][1]
    assert exact_scalars(aggregate.distributions(comparison._Budget(comparison.LIMIT)))==exact_scalars(
        prior.distributions(comparison._Budget(comparison.LIMIT)))
    assert accepted['usage']['summary']['value']==9
    assert exact_scalars(project(engine,all_query())['usage'])==exact_scalars(
        project(ProjectedEngine(deepcopy(engine.owners)),all_query())['usage'])



def capacity_owners(calls,owners,groups,*,model_chars=24,project_chars=32):
    rows={('home',str(index)):[] for index in range(owners)}
    identities=tuple(rows)
    models=[f'model-{index:04d}'.ljust(model_chars,'m') for index in range(groups)]
    projects=[f'project-{index:04d}'.ljust(project_chars,'p') for index in range(groups)]
    for ordinal in range(calls):
        owner=identities[ordinal%owners];group=ordinal%groups;value=ordinal+1
        row=scalar_call(value)
        row.update(home=owner[0],sid=owner[1],ts=value,key=f'call-{ordinal}',
                   model=models[group],project=projects[group],project_name=projects[group],
                   source='user',service_tier='default',cost=value/100000,
                   output=value%100+1,reasoning=value%10,cached=0)
        rows[owner].append(row)
    return rows


def test_owner_cache_overflow_falls_back_with_exact_cold_warm_append_and_correction_results():
    from cachemonitor import comparison_usage as comparison
    owners=capacity_owners(1008,63,4)
    values=(0,-0.0,0.0,2**53+1,2**64-1,2**64,2**80+1,1e100)
    for rows in owners.values():
        for row in rows:
            value=values[((row['ts']-1)//4)%len(values)]
            row.update(input=value,output=value,cost=value,reasoning=value)
            if row['ts']%7==0:row['reasoning']=None
    q=all_query();q['usage']['dimension']='project'
    reference=ProjectedEngine(deepcopy(owners));project(reference,q)
    global_budget=comparison._Budget(comparison.LIMIT);groups={}
    for rows in owners.values():
        for row in rows:comparison.add_row(groups,row,'project',global_budget)
    owner_bytes=max(512+comparison._size(owner)+comparison._size(entry['context'])
                    +sum(256+comparison._size(key)+comparison._size(label)+value.clone_bytes()
                         for key,(label,value) in entry['groups'].items())
                    for owner,entry in reference._comparison_cache['entries'].items())
    allowance=(global_budget.peak+2*owner_bytes
               +reference.read_metrics['comparison_working_peak_bytes']
               +reference.read_metrics['comparison_result_bytes'])
    assert owner_bytes<allowance<reference.read_metrics['comparison_cache_bytes']
    del groups,reference
    engine=ProjectedEngine(deepcopy(owners));engine.reader.limits.page_objects_bytes=allowance
    projected=engine.projected_calls;reads=[0]
    def read_page(*args,**kwargs):
        page=projected(*args,**kwargs);reads[0]+=len(page.rows)
        return page
    engine.projected_calls=read_page
    owner=('home','0')
    for phase in ('cold','warm','append','correction','removal'):
        if phase=='append':
            row=dict(engine.owners[owner][-1],key='appended',ts=1009,cost=2**64+3,reasoning=-0.0)
            engine.replace(owner,engine.owners[owner]+[row],append=True)
        elif phase=='correction':
            rows=deepcopy(engine.owners[owner]);rows[0].update(cost=0.1,input=2**80+3,reasoning=7)
            engine.replace(owner,rows)
        elif phase=='removal':
            engine.owners.pop(('home','1'));engine.revision+=1
        expected=project(ProjectedEngine(deepcopy(engine.owners)),q)
        before=engine.read_metrics.get('category_projection_rows',0);read_before=reads[0]
        actual=project(engine,q)
        assert engine.read_metrics['category_projection_rows']-before==reads[0]-read_before
        assert reads[0]-read_before>actual['analysis']['response_count']
        assert exact_scalars(actual['usage'])==exact_scalars(expected['usage']),phase
        assert actual['analysis']==expected['analysis'],phase
        assert engine.read_metrics['comparison_cache_bytes']<=allowance
        assert engine.read_metrics['comparison_working_peak_bytes']<=allowance
        assert engine.read_metrics['comparison_result_bytes']<=allowance


def test_required_projected_page_failure_does_not_retry_or_replace_cache(monkeypatch):
    from cachemonitor.prepared_store import CapacityExceeded
    engine=ProjectedEngine({('home','one'):[scalar_call(1)]})
    accepted=project(engine,all_query());published=engine._comparison_cache
    engine.replace(('home','one'),[scalar_call(2)])
    reads=[]
    def unavailable(*args,**kwargs):
        reads.append(args)
        raise CapacityExceeded('required projection unavailable')
    monkeypatch.setattr(engine,'projected_calls',unavailable)
    with pytest.raises(CapacityExceeded,match='required projection unavailable'):
        project(engine,all_query())
    assert len(reads)==1 and engine._comparison_cache is published
    assert accepted['usage']['summary']['value']==1


@pytest.mark.extended
def test_real_32_mib_owner_group_cache_overflow_keeps_complete_exact_results():
    from cachemonitor import comparison_usage as comparison
    owners=capacity_owners(8192,21,512,model_chars=256,project_chars=512)
    engine=ProjectedEngine(owners);q=all_query();q['usage']['dimension']='project'
    actual=project(engine,q)
    assert comparison.LIMIT==32*1024*1024
    assert actual['analysis']==dict(response_count=8192,session_count=21)
    assert actual['usage']['summary']==dict(value=8192,known=8192,total=8192,partial=False)
    rows={row['group']:row for row in actual['usage']['rows']}
    assert len(rows)==512 and sum(row['metrics']['calls']['value'] for row in rows.values())==8192
    total=comparison.Totals()
    for owner_rows in owners.values():
        budget=comparison._Budget(comparison.LIMIT);groups={}
        for row in owner_rows:comparison.add_row(groups,row,'project',budget)
        for _,aggregate in groups.values():total.merge(aggregate.totals)
    assert exact_scalars(actual['usage']['metrics'])==exact_scalars(total.result())
    for group,row in rows.items():
        population={owner:[call for call in calls if call['project']==group]
                    for owner,calls in owners.items()}
        reference=project(ProjectedEngine(population),q)['usage']['rows'][0]
        assert exact_scalars(row['metrics'])==exact_scalars(reference['metrics'])
        assert exact_scalars(row['distributions'])==exact_scalars(reference['distributions'])
        assert row['key']==reference['key'] and row['filters']==reference['filters']
        assert row['group_label']==reference['group_label']
    for name in ('comparison_cache_bytes','comparison_working_peak_bytes','comparison_result_bytes'):
        assert engine.read_metrics[name]<=comparison.LIMIT



def test_certified_append_reuses_untouched_groups_and_exact_folded_rows():
    owner=('home','one')
    owners={owner:[dict(scalar_call(value),model=model) for model,values in
                    [('hot',(1.,2**53+1)),('stable',(-0.0,0,2**80+1))] for value in values],
            ('home','two'):[dict(scalar_call(0.0),model='stable'),dict(scalar_call(5),model='other')]}
    engine=ProjectedEngine(owners);q=all_query()
    project(engine,q)
    previous=engine._comparison_cache;retained=deepcopy(previous)
    groups=previous['entries'][owner]['groups'];folded={row.key:row for row in previous['fold'].rows}
    engine.replace(owner,owners[owner]+[dict(scalar_call(0.1),model='hot',reasoning=None)],append=True)
    actual=project(engine,q)
    expected=project(ProjectedEngine(deepcopy(engine.owners)),q)
    assert exact_scalars(actual['usage'])==exact_scalars(expected['usage'])
    assert actual['analysis']==expected['analysis']
    current=engine._comparison_cache
    for key,(_,aggregate) in groups.items():
        assert exact_scalars(aggregate.totals.result())==exact_scalars(retained['entries'][owner]['groups'][key][1].totals.result())
        assert (current['entries'][owner]['groups'][key][1] is aggregate)==(key.model=='stable')
    for row in current['fold'].rows:
        assert (row is folded[row.key])==(row.key.model!='hot')
    for rows in (list(reversed(list(engine.owners.items()))),list(engine.owners.items())[1:]):
        engine.owners=dict(rows)
        assert exact_scalars(project(engine,q)['usage'])==exact_scalars(
            project(ProjectedEngine(deepcopy(engine.owners)),q)['usage'])


@pytest.mark.parametrize('consumer',['engine','handler'])
@pytest.mark.parametrize('fallback',[False,True])
def test_live_publication_advances_while_comparison_fold_waits(tmp_path,monkeypatch,consumer,fallback):
    from dataclasses import replace
    from threading import Event,Thread
    from cachemonitor import comparison_usage as comparison
    from cachemonitor.prepared_query import PreparedQuery

    values=[]
    template=sources()[0]
    for number in range(32):
        source=deepcopy(template);source['id']=str(number)
        source['history']=[dict(source['history'][0],sid=str(number),key=f'call-{number}')]
        values.append(source)
    values[0]['history'].append(dict(values[0]['history'][0],key='future',ts=120))
    q=query();q['usage'].update(dimension='all',showUnknown=True)
    reference=AnalysisEngine();reference.ingest(values)
    expected=project(reference,q)
    if fallback:monkeypatch.setattr(comparison,'LIMIT',32*1024)
    entered,release=Event(),Event();results=[];failures=[]
    finish=comparison._finish_fold
    def wait_for_publication(*args,**kwargs):
        entered.set()
        assert release.wait(10),'writer did not finish while comparison calculation waited'
        return finish(*args,**kwargs)
    monkeypatch.setattr(comparison,'_finish_fold',wait_for_publication)
    with storage.PreparedStore(tmp_path/'detached-comparison.sqlite') as store:
        first=publish(store,values,1)
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        def calculate():
            try:
                if consumer=='handler':
                    with closing(PreparedQuery(store.path)) as handler:
                        response=handler(dict(kind='query',id=1,logical='comparison',query=q),first,lambda:False)
                        assert response['kind']=='result'
                        results.append((response['result'],response['valid_until'],handler.engine.read_metrics.copy()))
                else:
                    with closing(PreparedAnalysisEngine(store.path,first)) as engine:
                        result=engine.page_query(q)
                        results.append((result,result['valid_until'],engine.read_metrics.copy()))
            except BaseException as error:failures.append(error)
        thread=Thread(target=calculate,daemon=True);thread.start()
        try:
            assert entered.wait(10),'comparison did not reach captured calculation'
            changed=deepcopy(values);changed[0]['history'][0]['input']=20000
            second=publish(store,changed,2)
            before=store.disk_usage()['wal']
            store.limits=replace(store.limits,wal_bytes=before+1024*1024,write_reserve_bytes=1024*1024+4096)
            changed[0]['history'][0]['input']=30000;changed[0]['history'][-1]['ts']=130
            third=publish(store,changed,3)
            assert first.generation<second.generation<third.generation
            assert thread.is_alive()
            with storage.PreparedReader(store.path) as reader,reader.pin(third) as current:
                assert current.record('home','0','call-0')['input']==30000
        finally:
            release.set();thread.join(10)
        assert not thread.is_alive() and not failures,failures
        result,valid_until,metrics=results[0]
        assert exact_scalars(result['usage'])==exact_scalars(expected['usage'])
        assert result['analysis']==expected['analysis']
        assert valid_until==expected['valid_until']==120.002
        if consumer=='handler':assert result['prepared_token']==(first.instance,first.generation)
        assert (metrics['comparison_cache_bytes']==0) is fallback


@pytest.mark.parametrize('stage',['fold','delivery'])
def test_detached_comparison_uses_request_cancellation_and_can_retry(tmp_path,monkeypatch,stage):
    from threading import Event
    from cachemonitor import comparison_usage as comparison
    from cachemonitor.prepared_query import PreparedQuery

    cancelled=Event();method='_finish_fold' if stage=='fold' else '_deliver'
    original=getattr(comparison,method)
    def cancel_request(*args,**kwargs):
        cancelled.set()
        return original(*args,**kwargs)
    with storage.PreparedStore(tmp_path/'cancel-comparison.sqlite') as store:
        token=publish(store,sources(),1)
        with closing(PreparedQuery(store.path)) as handler:
            message=dict(kind='query',id=1,logical='comparison',query=query())
            with monkeypatch.context() as patch:
                patch.setattr(comparison,method,cancel_request)
                with pytest.raises(storage.ReadCancelled):handler(message,token,cancelled.is_set)
            assert handler.engine.snapshot is None
            assert not hasattr(handler.engine,'_comparison_cache')
            cancelled.clear()
            result=handler(message,token,cancelled.is_set)
            assert result['kind']=='result' and result['result']['analysis']['response_count']==8


def test_prepared_handler_keeps_ordinary_page_expiry_in_its_read(tmp_path):
    from cachemonitor.prepared_query import PreparedQuery

    values=sources();values[0]['history'].append(dict(values[0]['history'][0],key='future',ts=120))
    q=query();q.update(category_usage=False,presentation=False)
    with storage.PreparedStore(tmp_path/'ordinary-page.sqlite') as store:
        token=publish(store,values,1)
        with closing(PreparedQuery(store.path)) as handler:
            result=handler(dict(kind='query',id=1,logical='ordinary',query=q),token,lambda:False)
            assert result['kind']=='result'
            assert len(result['result']['analysis']['responses'])==8
            assert result['valid_until']==result['result']['valid_until']==120.002
            assert handler.engine.snapshot is None


def reference_fold(entries,budget):
    from struct import calcsize
    import sys
    from cachemonitor import comparison_usage as comparison
    groups={};total=comparison.Totals();sessions=0
    budget.reserve(sys.getsizeof(groups)+total.retained_bytes())
    for entry in entries.values():
        if entry['groups']:sessions+=1
        for key,(label,value) in entry['groups'].items():
            if key not in groups:
                totals=comparison.Totals()
                budget.reserve(256+comparison._size(key)+comparison._size(label)+totals.retained_bytes())
                groups[key]=(label,totals,[])
            _,totals,parts=groups[key]
            before=totals.retained_bytes()+total.retained_bytes()
            totals.merge(value.totals);total.merge(value.totals)
            budget.reserve(totals.retained_bytes()+total.retained_bytes()-before+4*calcsize('P'))
            parts.append(value)
    return comparison._finish_fold(iter(groups.items()),len(groups),total,sessions,budget)


def test_fold_group_accumulator_has_the_original_tuple_storage_bound():
    import sys
    from cachemonitor import comparison_usage as comparison
    group=comparison._FoldGroup('label',True)
    assert not hasattr(group,'__dict__')
    assert sys.getsizeof(group)<=sys.getsizeof((group.label,group.totals,group.parts))
    assert sys.getsizeof(group.parts)==sys.getsizeof([])


def test_partial_fold_fits_the_exact_baseline_working_peak(monkeypatch):
    from cachemonitor import comparison_usage as comparison
    owners={('home',str(owner)):[dict(scalar_call(owner+1),model='hot'),
        dict(scalar_call(-0.0 if owner%2 else 2**80+owner),model='stable')]
        for owner in range(40)}
    engine=ProjectedEngine(owners);q=all_query()
    project(engine,q);previous=engine._comparison_cache
    owner=next(iter(owners))
    engine.replace(owner,owners[owner]+[dict(scalar_call(2**53+1),model='hot')],append=True)
    project(engine,q)
    entries=engine._comparison_cache['entries']
    baseline=comparison._Budget(comparison.LIMIT)
    expected=reference_fold(entries,baseline)
    budget=comparison._Budget(baseline.peak)
    finishes=[]
    finish=comparison._finish_fold
    def once(*args,**kwargs):
        finishes.append(True)
        return finish(*args,**kwargs)
    monkeypatch.setattr(comparison,'_finish_fold',once)
    actual=comparison._fold(entries,budget,previous)
    assert exact_scalars(actual)==exact_scalars(expected)
    assert budget.peak<=baseline.peak and len(finishes)==1
    old={row.key:row for row in previous['fold'].rows}
    assert all((row is old[row.key])==(row.key.model=='stable') for row in actual.rows)


@pytest.mark.parametrize('change',['append','correction','remove_contribution','label','owner_order','owner_removal'])
def test_partial_row_reuse_requires_unchanged_contributors_labels_and_owner_order(change):
    from cachemonitor import comparison_usage as comparison
    first=('home','one');second=('home','two')
    owners={first:[dict(scalar_call(1),model='hot',project='p',project_name='Old'),
                  dict(scalar_call(-0.0),model='stable',project='p',project_name='Old')],
            second:[dict(scalar_call(2**80+1),model='hot',project='p',project_name='Old'),
                    dict(scalar_call(0),model='stable',project='p',project_name='Old'),
                    dict(scalar_call(2),model='other',project='p',project_name='Old')]}
    engine=ProjectedEngine(owners);q=all_query()
    if change=='label':q['usage']['dimension']='project'
    project(engine,q);previous=engine._comparison_cache
    old_rows={row.key:row for row in previous['fold'].rows}
    if change=='append':
        engine.replace(first,owners[first]+[dict(scalar_call(0.1),model='hot')],append=True)
    elif change=='correction':
        engine.replace(first,[dict(scalar_call(3),model='hot'),owners[first][1]])
    elif change=='remove_contribution':
        engine.replace(first,[owners[first][0]])
    elif change=='label':
        engine.contexts[first]=dict(project='p',project_name='New')
        engine.replace(first,[dict(row,project_name='New') for row in owners[first]])
    elif change=='owner_order':
        engine.owners=dict(reversed(list(owners.items())))
    else:
        del engine.owners[first]
    actual=project(engine,q)
    expected=project(ProjectedEngine(deepcopy(engine.owners)),q)
    assert exact_scalars(actual['usage'])==exact_scalars(expected['usage'])
    current=engine._comparison_cache['fold']
    for row in current.rows:
        reusable=(change in ('append','correction','remove_contribution','label') and row.key.model=='other'
                  or change=='append' and row.key.model=='stable')
        assert (row is old_rows.get(row.key))==reusable
    if change=='label':assert actual['usage']['rows'][0]['group_label']=='New'
    assert previous['fold'].rows==tuple(old_rows.values())


def test_fold_capacity_failure_after_completed_row_propagates_once_and_keeps_cache(monkeypatch):
    from cachemonitor import comparison_usage as comparison
    from cachemonitor.prepared_store import CapacityExceeded
    owner=('home','one')
    engine=ProjectedEngine({owner:[dict(scalar_call(1),model='first'),dict(scalar_call(2),model='second')]})
    q=all_query();project(engine,q);published=engine._comparison_cache
    engine.replace(owner,[dict(scalar_call(3),model='first'),dict(scalar_call(4),model='second')])
    finishes=[];descriptions=[];rows=[]
    finish=comparison._finish_fold;describe=comparison._describe
    def once(groups,count,total,sessions,budget,**kwargs):
        finishes.append(True)
        def current():
            for index,group in enumerate(groups):
                if index==1:
                    rows.append('completed')
                    budget.maximum=budget.used
                yield group
        return finish(current(),count,total,sessions,budget,**kwargs)
    def observed(*args,**kwargs):
        descriptions.append(True)
        return describe(*args,**kwargs)
    monkeypatch.setattr(comparison,'_finish_fold',once)
    monkeypatch.setattr(comparison,'_describe',observed)
    with pytest.raises(CapacityExceeded,match='comparison aggregates'):
        project(engine,q)
    assert finishes==[True] and rows==['completed']
    assert len(descriptions)==len(comparison.MEAN_FIELDS)
    assert engine._comparison_cache is published


def test_fold_reuse_checks_do_linear_group_lookups():
    from cachemonitor import comparison_usage as comparison
    class Groups(dict):
        probes=0
        def get(self,*args):
            type(self).probes+=1
            return super().get(*args)
        def __contains__(self,key):
            type(self).probes+=1
            return super().__contains__(key)
    owners={('home',str(owner)):[dict(scalar_call(owner+1),model='m'+str(owner)+'-'+str(group))
        for group in range(4)] for owner in range(40)}
    engine=ProjectedEngine(owners);project(engine,all_query())
    previous=engine._comparison_cache
    entries={owner:dict(entry,groups=Groups(entry['groups'])) for owner,entry in previous['entries'].items()}
    aggregate_count=sum(len(entry['groups']) for entry in entries.values())
    fold=comparison._fold(entries,comparison._Budget(comparison.LIMIT),previous)
    assert fold.rows==previous['fold'].rows
    assert all(row is old for row,old in zip(fold.rows,previous['fold'].rows))
    assert Groups.probes<=2*aggregate_count
