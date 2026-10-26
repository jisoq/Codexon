from contextlib import closing
from threading import Event
from types import SimpleNamespace

import pytest

from cachemonitor import prepared_store as storage
from cachemonitor.analysis_engine import BoundedCache
from cachemonitor.performance_trends import PerformanceTrends
from cachemonitor.prepared_query import PreparedQuery
from test_prepared_analysis import OWNER, calls
from cachemonitor.prepared_query import tuple_token


def publish(store,revision,rows):
    stage=store.begin(('reuse',revision))
    dependency={name:1 for name in storage.DEPENDENCIES};dependency['source_digest']=f'{revision:064x}'
    stage.owner(*OWNER,dict(home=OWNER[0],sid=OWNER[1],title='Session',_internal_review=False),
                dependency,source_ordinal=0)
    stage.calls(OWNER,rows,reconcile=True)
    return stage.publish(('reuse',revision),ready=True)


def options(**changes):
    return dict(dict(page=5, now=110, start=100, end=110, period='custom', time_range=[100,110], plot_width=64,
                     granularity='auto', trend_mode='timeline'), **changes)


def request(handler, token, query, changes=()):
    return handler(dict(kind='query', id=1, logical='performance', query=query,
                        _prepared_changes=changes), token, lambda:False)


def chart_source():
    built=[]
    def performance_input(q,width,unit):
        built.append((q,width,unit))
        return dict(prepared_view=dict(calls=len(built), valid_until=q['now']+10))
    return SimpleNamespace(revision=1, call_revision=1, page_results=BoundedCache(rows=16384),
                           performance_input=performance_input), built


@pytest.mark.parametrize('change', [dict(time_range=[101,110]),dict(plot_width=65),
    dict(granularity='day'),dict(trend_mode='hour_of_day'),dict(trend_period='7d'),
    dict(trend_selection=dict(models=[],modes=[])),
    dict(trend_selection=dict(models=None,modes=['Standard']))])
def test_chart_identity_and_original_expiry(change):
    source,built=chart_source()
    first=PerformanceTrends().query(source,options())
    other=PerformanceTrends().query(source,options(**change))
    assert len(built)==2 and other['calls']==2
    again=PerformanceTrends().query(source,options(now=119))
    assert again==first and len(built)==2
    assert PerformanceTrends().query(source,options(now=120))['calls']==3
    source.call_revision=2
    assert PerformanceTrends().query(source,options(now=120))['calls']==4


def test_normalized_chart_inputs_and_reference_single_view_expiry():
    source,built=chart_source()
    normalized=options(plot_width=1,granularity='invalid',trend_mode='invalid')
    first=PerformanceTrends().query(source,normalized)
    assert PerformanceTrends().query(source,options(granularity='day'))==first
    assert len(built)==1
    del source.page_results
    projector=PerformanceTrends()
    first=projector.query(source,options())
    assert projector.query(source,options(now=119))==first
    assert projector.query(source,options(now=120))!=first
    projector.query(source,options(now=120,trend_mode='hour_of_day'))
    assert projector.query(source,options(now=120))['calls']==5


def test_public_mode_toggles_reuse_charts_with_current_adopted_wrappers(tmp_path,monkeypatch):
    rows=[dict(row,service_tier='Standard',effort='high',output_speed=10.,
               completion_latency_ms=1000) for row in calls()]
    with storage.PreparedStore(tmp_path/'reuse.sqlite') as store, closing(PreparedQuery(store.path)) as handler:
        token=publish(store,1,rows)
        timeline=request(handler,token,options())['result']['performance']
        clock=request(handler,token,options(trend_mode='hour_of_day'))['result']['performance']
        def unexpected(*args,**kwargs):raise AssertionError('completed chart was rebuilt')
        with monkeypatch.context() as patch:
            patch.setattr(handler.engine,'performance_input',unexpected)
            assert request(handler,token,options())['result']['performance']==timeline
            assert request(handler,token,options(trend_mode='hour_of_day'))['result']['performance']==clock
            stage=store.begin(('display',2))
            dependency={name:1 for name in storage.DEPENDENCIES};dependency['source_digest']=f'{1:064x}'
            stage.owner(*OWNER,dict(home=OWNER[0],sid=OWNER[1],title='Renamed',_internal_review=False),dependency,source_ordinal=0)
            adopted=stage.publish(('display',2),ready=True)
            journal=[(tuple_token(token),tuple_token(adopted),[OWNER],[])]
            for mode,expected in (('timeline',timeline),('hour_of_day',clock)):
                result=request(handler,adopted,options(trend_mode=mode),journal)
                assert result['result']['performance']==expected
                assert result['prepared_token']==tuple_token(adopted)
                assert result['result']['revision']==adopted.generation
        corrected=publish(store,3,[dict(rows[0],cost=7),rows[2]])
        result=request(handler,corrected,options(),[(tuple_token(adopted),tuple_token(corrected),[OWNER],[])])
        assert result['result']['performance']['calls']==2
        assert not hasattr(handler.engine,'performance')
        previous=handler.engine
        latest=publish(store,4,rows)
        assert request(handler,latest,options())['result']['performance']['calls']==3
        assert handler.engine is not previous


@pytest.mark.parametrize('budget', [1,16*1024*1024])
def test_shared_eviction_and_oversized_charts_have_no_production_alias(tmp_path,budget,monkeypatch):
    with storage.PreparedStore(tmp_path/'eviction.sqlite') as store, closing(PreparedQuery(store.path)) as handler:
        token=publish(store,1,calls())
        request(handler,token,options())
        cache=handler.engine.page_results
        cache.clear();cache.entries=1;cache.rows=budget
        built=[];original=handler.engine.performance_input
        def build(*args):
            built.append(True)
            return original(*args)
        monkeypatch.setattr(handler.engine,'performance_input',build)
        first=request(handler,token,options())['result']['performance']
        request(handler,token,options(trend_mode='hour_of_day'))
        assert len(cache.data)<=1 and cache.weight<=budget
        assert not hasattr(handler.engine,'performance')
        if budget==1:assert not cache.data
        else:
            assert len(cache.data)==1
            assert all(value[0]['panels'] is not first['panels'] for value in cache.data.values())
        assert request(handler,token,options())['result']['performance']==first
        assert len(built)==3


def test_canceled_completed_chart_is_not_published(tmp_path,monkeypatch):
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    canceled=Event()
    with storage.PreparedStore(tmp_path/'cancel.sqlite') as store:
        with closing(PreparedAnalysisEngine(store.path,publish(store,1,calls()),cancel=canceled)) as source:
            def finish(*args):
                canceled.set()
                return dict(prepared_view=dict(calls=0,valid_until=120))
            monkeypatch.setattr(source,'performance_input',finish)
            with pytest.raises(storage.ReadCancelled):source.page_query(options())
            assert not source.page_results.data


@pytest.mark.parametrize('change', ['model','visibility','order','removed'])
def test_factual_adoption_invalidates_all_performance_entries(tmp_path,change):
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    with storage.PreparedStore(tmp_path/'adopt.sqlite') as store:
        with closing(PreparedAnalysisEngine(store.path,publish(store,1,calls()))) as source:
            source.page_query(options())
            source.page_query(options(trend_mode='hour_of_day'))
            assert sum(key[2]=='view' for key in source.page_results.data)==2
            stage=store.begin(('adopt',2))
            if change=='order':stage.catalog_order([(OWNER,0)])
            else:
                metadata=dict(home=OWNER[0],sid=OWNER[1],title='Session',_internal_review=change=='visibility')
                if change=='model':metadata['model']='different'
                dependency={name:1 for name in storage.DEPENDENCIES};dependency['source_digest']=f'{1:064x}'
                stage.owner(*OWNER,metadata,dependency,source_ordinal=0)
                if change=='removed':stage.remove_owner(OWNER)
            token=stage.publish(('adopt',2),ready=True)
            source.adopt(token,[] if change in ('order','removed') else [OWNER],
                         [OWNER] if change=='removed' else [])
            assert source.call_revision==token.generation
            assert not source.page_results.data


def test_historical_metadata_reuse_preserves_cutoffs_variants_and_future_catalog(tmp_path):
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    from cachemonitor.prepared_performance import _Input
    rows=[dict(calls()[0],key='missing',ts=90,model='model'),
          dict(calls()[0],key='null',ts=91,model='model',service_tier=None,effort=None),
          dict(calls()[0],key='empty',ts=92,model='model',service_tier='',effort=''),
          dict(calls()[0],key='low',ts=101,model='model',service_tier='Standard',effort='low',
               output_speed=10.,completion_latency_ms=1000),
          dict(calls()[0],key='high',ts=105,model='model',service_tier='Standard',effort='high'),
          dict(calls()[0],key='future',ts=300,model='future',service_tier='Fast',effort='future')]
    reference=SimpleNamespace(revision=1,sessions={OWNER:dict(prepared=dict(history=rows))})
    with storage.PreparedStore(tmp_path/'history.sqlite') as store:
        with closing(PreparedAnalysisEngine(store.path,publish(store,1,rows))) as source:
            for start,end in ((100,104),(100,105),(100,110),(101,110)):
                q=options(time_range=[start,end],now=400)
                assert source.page_query(q)['performance']==PerformanceTrends().query(reference,q)
                if start==101:assert source.read_metrics['trend_metadata_groups']==0
            with source.read_scope():
                metadata=_Input(source)
                scopes,count=metadata.metadata(111)
                assert count==5
                variants=scopes[('model','미확인','미확인')]
                assert len(variants)==3 and len({types for raw,types in variants})==3
            selected=dict(models=['model'],modes=['Standard'])
            for mode in ('timeline','hour_of_day'):
                q=options(time_range=[102,110],now=400,trend_selection=selected,trend_mode=mode)
                view=source.page_query(q)['performance']
                assert view==PerformanceTrends().query(reference,q)
                assert 'future' in view['models']
                assert {'reasoning:low','reasoning:high'}<=set(panel['key'] for panel in view['panels'])
                if mode=='hour_of_day':assert source.read_metrics['trend_metadata_groups']==0
            q=options(time_range=[102,111],now=400,trend_selection=selected,granularity='day')
            assert source.page_query(q)['performance']==PerformanceTrends().query(reference,q)
            assert source.read_metrics['trend_metadata_groups']==0


@pytest.mark.parametrize('explicit_first', [False,True])
@pytest.mark.parametrize('granularity', ['auto','day'])
def test_forced_empty_all_does_not_poison_explicit_no_speed_history(tmp_path,explicit_first,granularity):
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    rows=[dict(row,model='model',service_tier='Standard',effort='high') for row in calls()]
    reference=SimpleNamespace(revision=1,sessions={OWNER:dict(prepared=dict(history=rows))})
    implicit=options(time_range=None,trend_period='all',granularity=granularity,
                     trend_selection=dict(models=['model'],modes=['Standard']))
    explicit=dict(implicit,time_range=[90,110])
    queries=[explicit,implicit] if explicit_first else [implicit,explicit]
    with storage.PreparedStore(tmp_path/'empty.sqlite') as store:
        with closing(PreparedAnalysisEngine(store.path,publish(store,1,rows))) as source:
            for q in queries:
                view=source.page_query(q)['performance']
                assert view==PerformanceTrends().query(reference,q)
                assert view['calls']==(3 if q['time_range'] else 0)


def test_warm_metadata_reapplies_charge_and_catalog_admission_once(tmp_path):
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    from cachemonitor.prepared_performance import _Input
    from cachemonitor.prepared_store import _size
    rows=[dict(row,service_tier='Standard',effort='high') for row in calls()]
    selection=(None,('Standard',))
    with storage.PreparedStore(tmp_path/'admission.sqlite') as store:
        with closing(PreparedAnalysisEngine(store.path,publish(store,1,rows))) as source:
            with source.read_scope():
                cold=_Input(source,selection);cold_before=cold.retained
                cold.metadata(110,global_catalog=True)
                history=next(value[0] for key,value in source.page_results.data.items() if key[2]=='history')
                charge=history[2]
                catalog_charge=sum(_size(value)+64 for value in source._trend_catalog)
                assert cold.retained-cold_before==charge+catalog_charge
                warm=_Input(source,selection);before=warm.retained
                warm.metadata(110,global_catalog=True)
                assert warm.retained-before==charge+catalog_charge
                assert warm.retained==cold.retained
                limited=_Input(source,selection)
                limited.limit=limited.retained+charge+catalog_charge-1
                with pytest.raises(storage.CapacityExceeded):limited.metadata(110,global_catalog=True)
                catalog_limited=_Input(source,selection)
                catalog_limited.limit=catalog_limited.retained+catalog_charge-1
                with pytest.raises(storage.CapacityExceeded):catalog_limited.metadata(110,global_catalog=True)


def test_warm_history_still_admits_each_exact_sort_range(tmp_path):
    from dataclasses import replace
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    rows=[dict(row,service_tier='Standard',effort='high') for row in calls()]
    with storage.PreparedStore(tmp_path/'sort.sqlite') as store:
        with closing(PreparedAnalysisEngine(store.path,publish(store,1,rows))) as source:
            source.page_query(options(time_range=[102,110]))
            size=source.read_metrics['trend_sort_estimated_bytes']
            source.reader.limits=replace(source.reader.limits,speed_sort_bytes=size)
            with pytest.raises(storage.CapacityExceeded):source.page_query(options(time_range=[100,110]))
            assert sum(key[2]=='history' for key in source.page_results.data)==1
            assert sum(key[2]=='view' for key in source.page_results.data)==1


@pytest.mark.parametrize('warm_mode',[None,'hour_of_day','timeline'])
@pytest.mark.parametrize('headroom',[-1,0])
def test_clock_cold_and_warm_history_share_exact_capacity(tmp_path,warm_mode,headroom):
    from dataclasses import replace
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine

    rows=[dict(row,service_tier='Standard',effort='high',
               output_speed=10.,completion_latency_ms=1000.) for row in calls()]
    query=options(trend_mode='hour_of_day',trend_selection=dict(models=None,modes=['Standard']))
    with storage.PreparedStore(tmp_path/'clock-capacity.sqlite') as store:
        token=publish(store,1,rows)
        with closing(PreparedAnalysisEngine(store.path,token)) as source:
            expected=source.page_query(query)['performance']
            limit=source.read_metrics['trend_scalar_peak_bytes']
        with closing(PreparedAnalysisEngine(store.path,token)) as source:
            if warm_mode:
                source.page_query(dict(query,trend_mode=warm_mode,time_range=[101,110]))
            source.reader.limits=replace(source.reader.limits,page_objects_bytes=limit+headroom)
            if headroom<0:
                with pytest.raises(storage.CapacityExceeded):
                    source.page_query(query)
            else:
                assert source.page_query(query)['performance']==expected
                assert source.read_metrics['trend_scalar_peak_bytes']==limit


def test_metadata_publication_requires_completion_but_survives_later_chart_failure(tmp_path,monkeypatch):
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    from cachemonitor.prepared_performance import _Input
    from cachemonitor import prepared_trends
    canceled=Event()
    rows=[dict(row,service_tier='Standard',effort='high') for row in calls()]
    with storage.PreparedStore(tmp_path/'partial.sqlite') as store:
        with closing(PreparedAnalysisEngine(store.path,publish(store,1,rows),cancel=canceled)) as source:
            original=_Input._admit
            def cancel_group(metadata,amount):
                original(metadata,amount)
                if metadata.stats['metadata_groups']:canceled.set()
            with monkeypatch.context() as patch:
                patch.setattr(_Input,'_admit',cancel_group)
                with pytest.raises(storage.ReadCancelled):source.page_query(options())
            assert not source.page_results.data
            canceled.clear()
            def fail(*args,**kwargs):raise RuntimeError('later chart failure')
            with monkeypatch.context() as patch:
                patch.setattr(prepared_trends,'rolling',fail)
                with pytest.raises(RuntimeError,match='later chart failure'):source.page_query(options())
            assert [key[2] for key in source.page_results.data]==['history']
            source.page_query(options())
            assert source.read_metrics['trend_metadata_groups']==0


@pytest.mark.integration
def test_analysis_benchmark_cli_completes_queries_and_closes_worker(tmp_path):
    import json
    from pathlib import Path
    import subprocess
    import sys

    checkout=Path(__file__).resolve().parents[1]
    database=tmp_path/'benchmark.sqlite'
    report_path=tmp_path/'benchmark.json'
    rows=[dict(row,service_tier='Standard',effort='high',
               output_speed=10.,completion_latency_ms=1000.) for row in calls()]
    with storage.PreparedStore(database) as store:
        token=publish(store,1,rows)
    result=subprocess.run([sys.executable,str(checkout/'tools/benchmark_analysis.py'),
        '--checkout',str(checkout),'--database',str(database),'--output',str(report_path),
        '--now','110'],cwd=checkout,text=True,capture_output=True,timeout=60)
    report=json.loads(report_path.read_text(encoding='utf-8'))
    assert result.returncode==0, (result.stdout,result.stderr,report['errors'])
    assert report['completed'] and report['cleanup']
    assert report['exit_code']==0 and report['closed']['kind']=='closed'
    assert report['error_count']==0 and report['errors']==[]
    expected_token=[token.instance,token.generation]
    assert report['ready']['token']==expected_token
    assert report['ready']['pid']==report['worker_pid']
    assert [run['label'] for run in report['runs']]==[
        'timeline-cold','clock-cold','timeline-revisit','clock-revisit',
        'timeline-3d','clock-3d','timeline-return','clock-historical-end','empty']
    assert [run['calls'] for run in report['runs']]==[3,3,3,3,3,3,3,0,0]
    assert all(run['prepared_token']==expected_token and run['panels']>0
               for run in report['runs'])
    assert report['runs'][0]['sha256']==report['runs'][2]['sha256']==report['runs'][6]['sha256']
    assert report['runs'][1]['sha256']==report['runs'][3]['sha256']
