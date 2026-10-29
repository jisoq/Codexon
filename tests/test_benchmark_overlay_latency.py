from pathlib import Path
import runpy
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from tools.benchmark_overlay_latency import ROOT, Scenario, analysis_process, collected_matches


@pytest.mark.parametrize('aggregate,owners,complete,loading,selected_complete,expected',[
    (False,0,False,True,True,True),
    (False,0,True,False,False,False),
    (True,2,False,True,True,False),
    (True,2,True,True,True,False),
    (True,2,True,False,True,True),
])
def test_publication_readiness_follows_requested_scope(aggregate,owners,complete,loading,selected_complete,expected):
    selected=dict(collection_complete=selected_complete,history=[dict(key='response')])
    snapshot=dict(usage_collection_complete=complete,index=dict(loading=loading),
                  demand=dict(aggregate=aggregate),sessions=[selected,dict(id='other')])
    command=dict(calls=1,owners=owners,aggregate=aggregate)
    assert collected_matches(snapshot,selected,command) is expected
    assert not collected_matches(snapshot,selected,dict(command,aggregate=not aggregate))
    assert not collected_matches(snapshot,selected,dict(command,calls=2))

@pytest.mark.parametrize('mode',['entry','watcher'])
def test_collector_startup_precedes_the_first_live_evidence_write(tmp_path,monkeypatch,mode):
    from queue import Queue
    from threading import current_thread
    import time
    from cachemonitor import usage_collection
    from cachemonitor.model_evidence import EvidenceReader,EvidenceStore
    from tools.benchmark_overlay_latency import collector_process

    home=tmp_path/'home';home.mkdir()
    evidence=tmp_path/'evidence.sqlite'
    commands=Queue();sent=[];threads=[];armed=Event()
    activity=dict(attempt='request',status='pending')
    command=dict(kind='poll',scenario='pending',sid='owner',calls=0,owners=0,
                 deadline=5,activity=activity,evidence={})

    class Connection:
        def recv(self):
            threads.append(current_thread())
            value=commands.get(timeout=5)
            if value is None:raise EOFError
            return value

        def send(self,value):
            sent.append(value)
            if value['kind']=='started':
                store=EvidenceStore(evidence)
                try:store.write(home,'request',time.time(),'WebSocket',status='pending')
                finally:store.close()
                commands.put(command)
            elif value['kind']=='armed':armed.set()
            elif value['kind']=='collected':commands.put(dict(kind='stop'))

        def close(self):commands.put(None)

    class Service:
        def __init__(self,*args):
            self.reader=EvidenceReader(evidence)
            self.epoch='startup-test'

        def poll(self,**kwargs):
            self.reader.poll(historical=False)
            return dict(usage_collection_complete=True,index=dict(loading=False,bytes_read=0),
                sessions=[dict(home=str(home),id='owner',collection_complete=True,history=[])],
                demand=dict(aggregate=False),request_activity=list(self.reader.changed_activity.values()),
                errors=[],ts=time.time())

        def stopping(self):return False

        def close(self):self.reader.close()

    def main():
        service=Service()
        try:
            assert not any(value['kind']=='started' for value in sent)
            Service.poll(service,detailed=False)
            assert any(value['kind']=='started' for value in sent)
            assert armed.wait(5)
            Service.poll(service,detailed=False)
            return 0
        finally:service.close()

    monkeypatch.setattr(usage_collection,'CollectorService',Service)
    monkeypatch.setattr(usage_collection,'main',main)
    monkeypatch.setattr(sys,'argv',[])
    collector_process(Connection(),home,tmp_path/'index.sqlite',evidence,
                      tmp_path/'collector.jsonl',False,mode)
    for thread in set(threads):
        if thread is not current_thread():
            thread.join(5)
            assert not thread.is_alive()
    assert not [value for value in sent if value['kind']=='error'],sent
    collected=[value for value in sent if value['kind']=='collected']
    assert len(collected)==1,sent
    assert len(collected[0]['activity'])==1
    assert all(collected[0]['activity'][0][key]==value for key,value in activity.items())


def test_scenario_ignores_matching_output_before_source_write():
    scenario=Scenario('append','owner',[dict(key='response')])
    value=dict(home='home',id='owner',calls=1,latest_key='response')
    assert not scenario.matches(value,'home')
    scenario.source_ns=1
    assert scenario.matches(value,'home')


def test_checkout_is_selected_before_product_imports(tmp_path,monkeypatch):
    script=Path(__file__).resolve().parents[1]/'tools/benchmark_overlay_latency.py'
    checkout=tmp_path/'product checkout'
    checkout.mkdir()
    monkeypatch.setattr(sys,'argv',[str(script),'--checkout',str(checkout)])
    monkeypatch.setattr(sys,'path',list(sys.path))
    module=runpy.run_path(str(script),run_name='benchmark_checkout_test')
    assert module['ROOT']==checkout.resolve()
    assert sys.path[0]==str(checkout.resolve())


def test_growth_gate_allows_selected_conversion_before_aggregate_release(tmp_path,monkeypatch):
    from cachemonitor import analysis_worker
    from cachemonitor.prepared_ingest import PreparedCohort
    entered,release,refresh=Event(),Event(),Event()
    converted=[]
    cohort=SimpleNamespace(store=SimpleNamespace(db=SimpleNamespace(
        execute=lambda sql:SimpleNamespace(fetchone=lambda:(0,)))))
    checkpoint=dict(ready=False,changed=(('home','history'),),removed=(),writes=1)

    def convert(instance,catalog):
        converted.append(catalog['authority'])
        yield dict(checkpoint,ready=catalog['authority']=='selected')

    def process(connection):
        aggregate=PreparedCohort.convert(cohort,dict(authority='full'))
        assert next(aggregate)==checkpoint and entered.is_set()
        assert list(PreparedCohort.convert(cohort,dict(authority='selected')))[0]['ready']
        refresh.set()
        assert next(aggregate)==dict(checkpoint,changed=(),removed=(),writes=0)
        assert not release.is_set()
        aggregate.close()
        resumed=PreparedCohort.convert(cohort,dict(authority='full'))
        refresh.set()
        assert next(resumed)==dict(checkpoint,changed=(),removed=(),writes=0)
        assert converted==['full','selected']
        release.set();refresh.set()
        assert list(resumed)==[checkpoint]
        assert converted==['full','selected','full']

    monkeypatch.setattr(PreparedCohort,'convert',convert)
    monkeypatch.setattr(analysis_worker,'process_main',process)
    analysis_process(None,trace_path=tmp_path/'analysis.jsonl',root=ROOT,entered=entered,release=release,refresh=refresh)


def test_unattributed_oracle_accepts_an_empty_selected_owner_without_invented_calls():
    from tools.benchmark_overlay_latency import check_output

    scenario=Scenario('unattributed','owner',source_at=10,unattributed=True,collector_ns=1,bridge_ns=2)
    empty=dict(home='home',id='owner',calls=0,latest_key=None,latest={},recent=[],_recent_refs=[])
    assert scenario.matches_snapshot(dict(ts=10,overlay_sessions=[]),'home')
    assert scenario.matches_snapshot(dict(ts=10,overlay_sessions=[empty]),'home')
    assert not scenario.matches_snapshot(dict(ts=9,overlay_sessions=[empty]),'home')
    assert check_output(None,scenario,'home') and check_output(empty,scenario,'home')
    for changes in (dict(calls=1),dict(latest_key='invented'),dict(latest=dict(turn='invented')),
                    dict(recent=[dict(key='invented')]),
                    dict(home='other'),dict(id='other')):
        invalid=dict(empty,**changes)
        assert not check_output(invalid,scenario,'home')
        if invalid['id']==scenario.sid:
            assert not scenario.matches_snapshot(dict(ts=10,overlay_sessions=[invalid]),'home')


def test_unattributed_snapshot_accepts_zero_call_references_before_detail_hydration():
    from tools.benchmark_overlay_latency import check_output

    scenario=Scenario('unattributed','owner',source_at=10,unattributed=True,collector_ns=1,bridge_ns=2)
    summary=dict(home='home',id='owner',calls=0,latest_key=None,latest={},recent=[],_recent_refs=True)
    assert scenario.matches_snapshot(dict(ts=10,overlay_sessions=[summary]),'home')
    assert not check_output(summary,scenario,'home')
    assert check_output(dict(summary,_recent_refs=False),scenario,'home')


@pytest.mark.parametrize('kind', ['comparison', 'trend'])
def test_controlled_query_overlap_cancels_real_work_and_completes_current_generation(tmp_path, kind):
    import json
    from multiprocessing.connection import wait
    import time
    from cachemonitor.analysis_query_actor import QueryActor
    from cachemonitor.prepared_query import PreparedQuery, token_value
    from cachemonitor.prepared_store import PreparedStore
    from tools.benchmark_cumulative import NOW, fixture, query_shapes, seed, stage_owner
    from tools.benchmark_overlay_worker import QueryOverlap, Trace, observe_queries

    path = tmp_path / 'prepared.sqlite'
    population = fixture(8, 2)
    token = token_value(seed(path, population)['token'])
    _, comparison, trend, _ = query_shapes(320)
    query = comparison if kind == 'comparison' else dict(trend, trend_selection=None)
    overlap = QueryOverlap(kind, 'original', entered=Event(), release=Event(),
                           cancelled=Event(), finished=Event(), deadline=5)
    trace = Trace(tmp_path / 'queries.jsonl')
    messages = []
    with observe_queries((overlap,), trace):
        actor = QueryActor(lambda: PreparedQuery(path))
        try:
            actor.submit(dict(kind='query', id='original', logical=(), query=query), token)
            assert overlap.entered.wait(5), 'the query did not reach its input boundary'
            assert not overlap.finished.is_set()
            messages.extend(actor.drain())
            assert not any(value['kind'] == 'result' for value in messages)
            owner = population[0]
            row = dict(owner.rows[-1], key='live-appended-call', ts=NOW - 1,
                       session_ordinal=len(owner.rows) + 1)
            owner.rows.append(row)
            with PreparedStore(path) as store:
                source = ('overlay-live', 2)
                stage = store.begin(source)
                stage_owner(stage, owner, [row])
                current = stage.publish(source, ready=True)
            assert not overlap.release.is_set() and not overlap.finished.is_set()
            actor.cancel('query')
            assert overlap.cancelled.is_set()
            actor.submit(dict(kind='query', id='current', logical=(), query=query), current)
            overlap.release.set()
            assert overlap.finished.wait(5), 'the cancelled query did not unwind'
            deadline = time.monotonic() + 5
            while not any(value['kind'] == 'result' for value in messages):
                remaining = deadline - time.monotonic()
                assert remaining > 0 and wait((actor.notification,), remaining), 'current query did not complete'
                messages.extend(actor.drain())
                assert not any(value['kind'] in ('error', 'fatal') for value in messages), messages
            results = [value for value in messages if value['kind'] == 'result']
            assert [value['id'] for value in results] == ['current']
            result = results[0]
            assert tuple(result['prepared_token']) == (current.instance, current.generation)
            value = result['result']['performance' if kind == 'trend' else 'usage']
            calls = value['calls'] if kind == 'trend' else value['metrics']['calls']['value']
            assert calls == 9
        finally:
            overlap.release.set()
            actor.close()
    events = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    assert [value['stage'] for value in events] == [
        'controlled_query_started', 'controlled_query_entered', 'controlled_query_cancelled',
        'controlled_query_released', 'controlled_query_finished']
    assert events[1]['boundary'] == overlap.boundary and events[1]['calls'] > 0
    assert events[-1]['cancelled']
