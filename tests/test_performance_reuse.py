"""Completed performance products share the engine's bounded factual lifetime."""
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


@pytest.mark.parametrize('budget', [1,16384])
def test_shared_eviction_and_oversized_charts_have_no_production_alias(tmp_path,budget):
    with storage.PreparedStore(tmp_path/'eviction.sqlite') as store, closing(PreparedQuery(store.path)) as handler:
        token=publish(store,1,calls())
        request(handler,token,options())
        cache=handler.engine.page_results
        cache.clear();cache.entries=1;cache.rows=budget
        first=request(handler,token,options())['result']['performance']
        request(handler,token,options(trend_mode='hour_of_day'))
        assert len(cache.data)<=1 and cache.weight<=budget
        assert not hasattr(handler.engine,'performance')
        if budget==1:assert not cache.data
        else:assert all(value[0] is not first for value in cache.data.values())


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
            assert len(source.page_results.data)==2
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
