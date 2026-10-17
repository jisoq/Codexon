"""Durable publication and exact selected-read contracts for prepared analysis."""
from dataclasses import replace
from threading import Event

import sqlite3

import pytest

from cachemonitor import prepared_store as storage
from cachemonitor.analysis_records import compact_record


OWNER = ('home', 'session')


def publish(store, revision, rows, *, ready=True):
    source = ('source', revision)
    stage = store.begin(source)
    dependency = {name: 1 for name in storage.DEPENDENCIES}
    dependency['source_digest'] = f'{revision:064x}'
    stage.owner(*OWNER, dict(home=OWNER[0], sid=OWNER[1], title='Session'),
                dependency, source_ordinal=0)
    stage.calls(OWNER, rows, reconcile=True)
    return stage.publish(source, ready=ready)


def calls():
    return [dict(key=f'call-{i}', ts=100+i, session_ordinal=i,
                 home=OWNER[0], sid=OWNER[1], title='Session', model='gpt-6-astra',
                 input=100, cached=20, output=10, cost=1., turn='request')
            for i in range(3)]


def test_publication_is_atomic_and_interrupted_staging_does_not_replace_saved_facts(tmp_path):
    path = tmp_path/'analysis.sqlite'
    rows = calls()
    with storage.PreparedStore(path) as store, storage.PreparedReader(path) as reader:
        first = publish(store, 1, rows)
        with reader.pin(first) as old:
            changed = [dict(rows[0], cost=2.), rows[2]]
            second = publish(store, 2, changed)
            assert old.record(*OWNER, 'call-0')['cost'] == 1.
            assert old.record(*OWNER, 'call-1') is not None
        with pytest.raises(storage.StaleGeneration):
            reader.pin(first)
        with reader.pin(second) as current:
            assert current.record(*OWNER, 'call-0')['cost'] == 2.
            assert current.record(*OWNER, 'call-1') is None
        source = ('source', 3)
        stage = store.begin(source)
        dependency = {name: 1 for name in storage.DEPENDENCIES}
        dependency['source_digest'] = '3'*64
        stage.owner(*OWNER, dict(home=OWNER[0], sid=OWNER[1], title='Interrupted'),
                    dependency, source_ordinal=0)
        stage.calls(OWNER, [dict(rows[0], cost=100.)], reconcile=True)
        # Closing the writer discards the unpublished stage. Its persisted facts
        # cannot replace the last complete publication on a normal restart.
    with storage.PreparedStore(path) as store, storage.PreparedReader(path) as reader:
        assert store.token == second
        with reader.pin(second) as current:
            assert current.record(*OWNER, 'call-0')['cost'] == 2.
            assert current.record(*OWNER, 'call-0')['title'] == 'Session'
            assert current.record(*OWNER, 'call-1') is None


def test_cleanup_waits_for_pinned_reader_without_failing_writer_or_growing_wal(tmp_path):
    from contextlib import closing
    import time
    from cachemonitor.prepared_runtime import PreparedWriter
    from cachemonitor.prepared_speed_writer import PreparedSpeedWriter

    path = tmp_path/'analysis.sqlite'
    rows = calls()
    writer = object.__new__(PreparedWriter)
    writer.stopped = Event()
    with (storage.PreparedStore(path) as store, storage.PreparedReader(path) as reader,
          closing(PreparedSpeedWriter(store, preparation='test')) as speed_writer):
        first = publish(store, 1, rows)
        store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        with reader.pin(first) as old:
            second = publish(store, 2, [dict(rows[0], cost=2.), rows[2]])
            stage = store.begin(('source', 3))
            before = store.disk_usage()['wal']
            # Scale the real admission boundary to this small fixture. Its
            # pinned reader, queued cleanup and checkpoint result are genuine.
            store.limits = replace(store.limits, wal_bytes=before+4096, write_reserve_bytes=8192)
            with pytest.raises(storage.WriteDeferred):
                store.maintain()
            assert writer._maintain(store)
            dependency = {name:1 for name in storage.DEPENDENCIES}
            dependency['source_digest'] = '3'*64
            with pytest.raises(storage.WriteDeferred):
                stage.owner(*OWNER, dict(home=OWNER[0], sid=OWNER[1], title='Uncommitted'), dependency)
            assert not writer._resume_writes(store, speed_writer)
            assert store.token == second and store.disk_usage()['wal'] == before
            assert old.record(*OWNER, 'call-0')['cost'] == 1.
        assert writer._resume_writes(store, speed_writer)
        assert store.stage is None and stage.finished
        deadline = time.monotonic()+5
        while writer._maintain(store):
            assert time.monotonic() < deadline
        assert store.disk_usage()['wal'] <= store.limits.wal_bytes
        with reader.pin(second) as current:
            assert current.record(*OWNER, 'call-0')['cost'] == 2.
            assert current.record(*OWNER, 'call-1') is None


def test_growing_transport_diagnostics_do_not_overflow_owner_metadata(tmp_path):
    from copy import deepcopy
    from cachemonitor.core import Session
    from cachemonitor.prepared_ingest import prepare_owner, stage_owner

    session = Session(OWNER[1], OWNER[0], title='Session')
    session.add_usage(100, 'call', dict(input_tokens=100, cached_input_tokens=20,
                      output_tokens=10), 'gpt-6-astra', service_tier='Standard')
    source = session.view(101)
    source['history'][0].update(transport='WebSocket', transport_source='response_id')
    diagnostics = [dict(ts=100+i, transport='WebSocket', endpoint='/responses',
                        evidence='observed', turn='request') for i in range(412)]
    source.update(transports=diagnostics[:282], unmatched_transports=diagnostics[282:])
    original = deepcopy(source)
    with pytest.raises(storage.CapacityExceeded):
        storage.pack({key:source[key] for key in storage.RAW_TRANSPORT_FIELDS})
    material = prepare_owner(source, owns=lambda *_:True)
    dependency = {name:1 for name in storage.DEPENDENCIES}
    dependency['source_digest'] = '1'*64
    with storage.PreparedStore(tmp_path/'analysis.sqlite') as store:
        stage = store.begin(('source', 1))
        stage_owner(stage, material, dependency, 0)
        stage.publish(('source', 1), ready=True)
        with store.read_current() as view:
            saved = view.owner(*OWNER)
            summary = view.derived(*OWNER, 'summary')
            assert not storage.RAW_TRANSPORT_FIELDS.intersection(saved['metadata'])
            row = view.record(*OWNER, 'call')
            assert row['transport']=='WebSocket' and row['transport_source']=='response_id'
        # Restaging metadata from an older prepared database also drops only
        # these diagnostics, retaining its calls and derived summary unchanged.
        legacy = dict(saved['metadata'], **{key:source[key] for key in storage.RAW_TRANSPORT_FIELDS})
        stage = store.begin(('source', 2))
        stage.owner(*OWNER, legacy, saved['dependencies'], source_ordinal=0)
        stage.publish(('source', 2), ready=True)
        with store.read_current() as view:
            assert not storage.RAW_TRANSPORT_FIELDS.intersection(view.owner(*OWNER)['metadata'])
            assert view.record(*OWNER, 'call')==row
            assert view.derived(*OWNER, 'summary')==summary
    assert source==original


@pytest.mark.parametrize('stopping',[False,True])
def test_prepared_shutdown_cancellation_is_not_reported_as_a_failure(tmp_path,monkeypatch,stopping):
    from types import SimpleNamespace
    from cachemonitor import prepared_runtime

    messages=[]
    writer=object.__new__(prepared_runtime.PreparedWriter)
    writer.arguments=[],tmp_path/'index.sqlite',None,tmp_path/'quota.sqlite'
    writer.stopped=Event()
    if stopping:writer.stopped.set()
    writer.output=SimpleNamespace(put=messages.append)
    def cancelled(*args,**kwargs):raise storage.ReadCancelled('cancelled work')
    monkeypatch.setattr(prepared_runtime,'CollectionClient',cancelled)
    writer._run()
    assert bool(messages) is not stopping
    assert (tmp_path/'quota.analysis.diagnostics.log').exists() is not stopping


def test_prepared_failures_keep_sanitized_diagnostics_without_static_fallthrough(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from cachemonitor import analysis_worker, prepared_runtime

    quota = tmp_path/'quota.sqlite'
    messages = []
    def fail(*args, **kwargs):
        raise ValueError('private request text must never enter the diagnostics log')
    monkeypatch.setattr(prepared_runtime, 'CollectionClient', fail)
    writer = object.__new__(prepared_runtime.PreparedWriter)
    writer.arguments = [], tmp_path/'index.sqlite', tmp_path/'evidence.sqlite', quota
    writer.output = SimpleNamespace(put=messages.append)
    writer._run()
    assert messages.pop()['kind']=='fatal'

    closed = []
    monkeypatch.setattr(prepared_runtime, 'PreparedWriter',
        lambda *a:SimpleNamespace(path=tmp_path/'prepared.sqlite', close=lambda:closed.append('writer')))
    monkeypatch.setattr(prepared_runtime, 'PreparedQuery', fail)
    monkeypatch.setattr(prepared_runtime, 'QueryActor', lambda factory:factory())
    def static_engine():
        pytest.fail('prepared startup failure must not enter static compatibility processing')
    monkeypatch.setattr(analysis_worker, 'AnalysisEngine', static_engine)
    connection = SimpleNamespace(send=messages.append, close=lambda:closed.append('connection'))
    analysis_worker.process_main(connection, [], tmp_path/'index.sqlite', quota_path=quota)
    assert closed==['writer', 'connection'] and len(messages)==1 and messages[0]['kind']=='fatal'
    with monkeypatch.context() as patch:
        allocations = []
        def allocate_reader(factory):
            allocations.append(1)
            if len(allocations) == 2:raise OSError('detail actor allocation failed')
            return SimpleNamespace(close=lambda:closed.append('reader'))
        patch.setattr(prepared_runtime, 'QueryActor', allocate_reader)
        closed.clear()
        with pytest.raises(OSError, match='detail actor allocation failed'):
            prepared_runtime.process_main(connection, [], None, None, quota)
        assert closed == ['reader', 'writer']
    def spawn_fail():
        raise OSError('private process-start context')
    context = SimpleNamespace(Pipe=lambda:(connection, connection),
        Process=lambda **kwargs:SimpleNamespace(start=spawn_fail))
    monkeypatch.setattr(analysis_worker.mp, 'get_context', lambda *_:context)
    bridge = analysis_worker.AnalysisBridge([], quota_path=quota)
    bridge.run()
    assert bridge.restart_count==3
    text = quota.with_suffix('.analysis.diagnostics.log').read_text(encoding='utf8')
    assert 'private request text' not in text and 'private process-start context' not in text
    records = [json.loads(line[line.index('{'):]) for line in text.splitlines()]
    assert {record['stage'] for record in records}=={'prepared writer', 'prepared query startup', 'prepared process', 'analysis process start'}
    assert all(record['error'] in ('ValueError', 'OSError') and record['frames'] for record in records)


def test_typed_pages_preserve_missing_fields_overrides_and_exact_numeric_types(tmp_path):
    limits = replace(storage.Limits(), page_rows=2)
    rows = calls()
    rows[0].update(cost=-0., tps=3.25, display_service_tier='Fast')
    rows[1].update(title='Specific call', cost=2**80, effort=None)
    rows[2].pop('title')
    fields = ('key', 'title', 'agent_path', 'cost', 'tps', 'display_service_tier', 'effort')
    with storage.PreparedStore(tmp_path/'analysis.sqlite', limits) as store:
        token = publish(store, 1, rows)
        with storage.PreparedReader(store.path, limits) as reader, reader.pin(token) as view:
            first = view.projected_calls(OWNER, fields)
            assert not first.complete
            last = view.projected_calls(OWNER, fields, after=first.after)
            assert last.complete
            expected = [{name: row[name] for name in fields if name in row}
                        for row in map(compact_record, rows)]
            actual = list(first.rows+last.rows)
            assert actual == expected
            assert [storage.pack([row[name] for name in fields if name in row]) for row in actual] == [
                storage.pack([row[name] for name in fields if name in row]) for row in expected]
            # Only the explicit owner override requires its detail packet.
            assert view.projected_detail_rows == 1
        with storage.PreparedReader(store.path, limits) as reader, reader.pin(token) as view:
            assert [row['key'] for row in view.projected_calls(
                OWNER, ('key',), start=101, end=102).rows] == ['call-1']
            assert not getattr(view, 'projected_detail_rows', 0)


def test_first_inventory_rejects_duplicate_keys_across_durable_chunks(tmp_path):
    limits = replace(storage.Limits(), fact_write_rows=1)
    with storage.PreparedStore(tmp_path/'analysis.sqlite', limits) as store:
        initial = store.token
        with pytest.raises(sqlite3.IntegrityError):
            publish(store, 1, [calls()[0], calls()[0]])
        with pytest.raises(storage.IncompletePublication):
            store.stage.publish(('source', 1), ready=True)
        assert store.token == initial
        store.stage.abort()
    with storage.PreparedStore(tmp_path/'analysis.sqlite', limits) as store:
        token = publish(store, 2, calls())
        with storage.PreparedReader(store.path, limits) as reader, reader.pin(token) as view:
            assert len(view.calls(OWNER).rows) == len(calls())


def test_canceled_reader_releases_transaction_and_does_not_poison_next_query(tmp_path):
    with storage.PreparedStore(tmp_path/'analysis.sqlite') as store:
        token = publish(store, 1, calls())
        with storage.PreparedReader(store.path) as reader:
            canceled = Event()
            view = reader.pin(token, cancel=canceled)
            canceled.set()
            with pytest.raises(storage.ReadCancelled):
                view.projected_calls(OWNER, ('key',))
            assert not reader.db.in_transaction
            with reader.pin(token) as current:
                assert current.record(*OWNER, 'call-0')['cost'] == 1.


@pytest.mark.parametrize('values', [
    [1e16, 1., -1e16], [2**31, 0.25, 1.], [2**80, 1., -2**80],
    [True, 0.1, False, 0.2], [-0., -0.], [float('inf'), -float('inf')],
])
def test_streamed_display_totals_preserve_builtin_sum_numeric_semantics(values):
    from cachemonitor.prepared_sums import _Sum
    total = _Sum()
    for value in values:
        total.add(value)
    assert storage.pack(total.value()) == storage.pack(sum(values))


def test_history_window_reuses_scalar_index_and_adopts_corrections(tmp_path, monkeypatch):
    from copy import deepcopy
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.core import Session
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    from cachemonitor.prepared_ingest import prepare_owner, stage_owner

    sources = []
    for sid, count in (('selected', 160), ('child', 3)):
        session = Session(sid, 'home', title=sid, cwd='D:/project')
        for index in range(count):
            session.add_usage(100+index, f'{sid}-{index}',
                dict(input_tokens=100+index, cached_input_tokens=20, output_tokens=10),
                'gpt-6-astra', 'request', 'medium', service_tier='Standard')
        source = deepcopy(session.view(300))
        source.update(usage_revision=1, collection_complete=True,
            turn_records={'request':dict(started_at=90, ended_at=280, state='완료')},
            turn_states={'request':'완료'})
        if sid == 'child':source['parent_thread_id'] = 'selected'
        sources.append(source)
    reference = AnalysisEngine()
    reference.ingest(sources)
    dependency = {name:1 for name in storage.DEPENDENCIES}
    dependency['source_digest'] = '1'*64
    query = dict(page=2, start=100, end=300, now=300, period='custom', presentation=True,
        explorer=dict(session=['home', 'selected'], view='calls'),
        table_windows={'records':dict(start=0, size=64)})
    decoded = []
    original_decode = storage.Snapshot._decode_call
    def decode(view, row, metadata, **kwargs):
        if not kwargs.get('metrics'):decoded.append(1)
        return original_decode(view, row, metadata, **kwargs)
    monkeypatch.setattr(storage.Snapshot, '_decode_call', decode)

    with storage.PreparedStore(tmp_path/'history.sqlite') as store:
        stage = store.begin(('history', 1))
        for ordinal, source in enumerate(sources):
            stage_owner(stage, prepare_owner(source, owns=lambda *_:True), dependency, ordinal)
        token = stage.publish(('history', 1), ready=True)
        engine = PreparedAnalysisEngine(store.path, token)
        try:
            actual, expected = engine.page_query(query), reference.page_query(query)
            assert actual['explorer']['records'] == expected['explorer']['records']
            assert actual['explorer']['parents'] == expected['explorer']['parents']
            assert actual['analysis']['response_count'] == expected['analysis']['response_count'] == 163
            assert actual['explorer']['group']['calls'] == 163

            query = dict(query, table_windows={'records':dict(start=64, size=64)})
            decoded.clear()
            actual, expected = engine.page_query(query), reference.page_query(query)
            assert actual['explorer']['records'] == expected['explorer']['records']
            assert len(decoded) == len(actual['explorer']['records']['rows']) == 64

            corrected = deepcopy(sources[0])
            corrected['history'][70]['input'] += 400
            corrected['usage_revision'] = 2
            sources[0] = corrected
            reference.ingest(sources)
            stage = store.begin(('history', 2))
            stage_owner(stage, prepare_owner(corrected, owns=lambda *_:True),
                        {**dependency, 'source_digest':'2'*64}, 0)
            token = stage.publish(('history', 2), ready=True)
            engine.adopt(token, [('home', 'selected')])
            decoded.clear()
            actual, expected = engine.page_query(query), reference.page_query(query)
            assert actual['explorer']['records'] == expected['explorer']['records']
            assert actual['explorer']['parents'] == expected['explorer']['parents']
            assert actual['explorer']['summary'] == expected['explorer']['summary']
            assert len(decoded) > len(actual['explorer']['records']['rows'])
        finally:
            engine.close()


def test_prepared_request_summary_keeps_first_call_at_tied_latest_timestamp():
    from cachemonitor.core import Session
    from cachemonitor.overlay_data import summarize_session
    from cachemonitor.prepared_ingest import prepare_owner

    session = Session('session', 'home')
    for key, model in (('first', 'gpt-6-astra'), ('second', 'gpt-6-sol')):
        session.add_usage(100, key, dict(input_tokens=100, cached_input_tokens=20,
            output_tokens=10), model, 'turn', 'medium', service_tier='Standard')
    source = dict(session.view(110), request_state=dict(turn='turn'))
    material = prepare_owner(source, owns=lambda *_:True)
    prepared = material['state']['prepared']
    actual = material['summary']['own']['request']
    assert actual == summarize_session(prepared, prepared['cache_health'])['request']
    assert actual['model']=='gpt-6-astra' and actual['calls']==2


def test_collector_append_uses_tail_and_historical_correction_uses_full_owner(tmp_path, monkeypatch):
    from copy import deepcopy
    import json
    import time
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import summarize_session
    from cachemonitor.prepared_health import hydrate_health
    from cachemonitor.prepared_ingest import PreparedCohort, read_catalog
    import cachemonitor.prepared_append as append
    from cachemonitor.prepared_ledger import PreparedLedger
    from cachemonitor.prepared_summary import hydrate_summary
    from cachemonitor.quota_cycles import QuotaLedger
    from cachemonitor.usage_collection import CollectorService, CollectionClient
    from test_core import fixture_home, usage, event, TID

    home, path = fixture_home(tmp_path)
    base = int(time.time())-10
    events = [event('event_msg', base-2, type='task_started', turn_id='turn'),
              event('turn_context', base-1, turn_id='turn', model='gpt-6-astra', effort='high')]
    def append_usage(number, cached=19000):
        item = usage('response-'+str(number), time=base+number, input=20000, cached=cached)
        item['payload']['turn_id'] = 'turn'
        events.append(item)
        return json.dumps(item)+'\n'
    for number in range(7):append_usage(number, 19000 if number<6 else 0)
    path.write_text(''.join(json.dumps(item)+'\n' for item in events), encoding='utf-8')
    target = tmp_path/'index.sqlite'
    service = CollectorService([home], target)
    client = CollectionClient([home], target)
    owner = (str(home.resolve()), TID)
    ledger = QuotaLedger(tmp_path/'quota.sqlite')
    reference_ledger = QuotaLedger(tmp_path/'quota-reference.sqlite')
    reference = AnalysisEngine()
    for value in (ledger, reference_ledger):
        value.db.execute('INSERT INTO tracking_config VALUES(?,?)', (owner[0], base-20))
        value.db.execute('INSERT INTO tracking_controls VALUES(?,?,?)', (owner[0], base-20, 1))
        value.db.commit()
    source_reads = []
    stream = client.selected_owner_stream
    def counted_stream(key, **kwargs):
        source_reads.append(key)
        return stream(key, **kwargs)
    monkeypatch.setattr(client, 'selected_owner_stream', counted_stream)
    def collect():
        deadline = time.monotonic()+10
        while True:
            value = service.poll(time.time())
            if value['usage_collection_complete'] and not value['index']['loading']:return value['sessions'][0]
            assert time.monotonic()<deadline, value['index']
    def envelope(source):
        return dict(sessions=[source], homes=[owner[0]], ts=source['history'][-1]['ts']+1,
            index=dict(loading=False, usage_complete=True), usage_collection_complete=True, errors=[])
    def verify(store, source):
        reference.ingest([deepcopy(source)])
        prepared = reference.sessions[owner]['prepared']
        with store.read_current() as view:
            page = view.calls(owner)
            assert page.complete
            assert [compact_record(row) for row in page.rows] == prepared['history']
            summary = hydrate_summary(view, *owner)['own']
            summary.update(hydrate_health(view, owner))
            summary.pop('_health_ref')
            assert summary == summarize_session(prepared, prepared['cache_health'])
        reference_ledger.sync(reference, envelope(source))
        for table in ('calls', 'tracking_call_boundaries', 'tracking_tasks'):
            actual = sorted(tuple(row) for row in ledger.db.execute('SELECT * FROM '+table))
            expected = sorted(tuple(row) for row in reference_ledger.db.execute('SELECT * FROM '+table))
            assert actual == expected
    try:
        initial = collect()
        with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
            cohort = PreparedCohort(store, client, ledger)
            # Existing v1 checkpoints retain Home counters. Upgrade their next
            # certified tail without rebuilding the saved owner or quota data.
            totals = append._new_totals
            with monkeypatch.context() as patch:
                patch.setattr(append, 'FORMAT', ('prepared-owner-append', 1))
                patch.setattr(append, '_new_totals',
                    lambda:dict(totals(), home_values=[0]*5, home_missing=[0]*5))
                assert list(cohort.convert(read_catalog(client)))[-1]['ready']
            adapter = PreparedLedger(ledger, store)
            list(adapter.sync(envelope(initial)))
            verify(store, initial)
            initial_reads = len(source_reads)
            assert initial_reads>0
            with path.open('a', encoding='utf-8') as output:output.write(append_usage(7, 0))
            appended = collect()
            def forbidden(*args, **kwargs):
                raise AssertionError('Certified append must not retrieve the complete owner')
            prior_call_reads = adapter.metrics['call_rows']
            with monkeypatch.context() as patch:
                patch.setattr(client, 'selected_owner_stream', forbidden)
                patch.setattr(cohort, '_load', forbidden)
                patch.setattr(adapter, '_owner', forbidden)
                cohort.apply(read_catalog(client), [owner])
                list(adapter.sync(envelope(appended)))
            assert cohort.metrics['append_owners']==1 and len(source_reads)==initial_reads
            assert adapter.metrics['append_owners']==1 and adapter.metrics['call_rows']==prior_call_reads+1
            with store.read_current() as view:
                checkpoint = view.derived(*owner, 'checkpoint')
                assert checkpoint['format'] == append.FORMAT
                assert not {'home_values', 'home_missing'} & checkpoint['total'].keys()
            verify(store, appended)
            # An old edit combined with growth must decline the append proof.
            events[2]['payload']['usage']['input_tokens'] = 21000
            append_usage(8)
            path.write_text(''.join(json.dumps(item)+'\n' for item in events), encoding='utf-8')
            corrected = collect()
            cohort.apply(read_catalog(client), [owner])
            list(adapter.sync(envelope(corrected)))
            assert cohort.metrics['append_owners']==1 and len(source_reads)>initial_reads
            assert adapter.metrics['append_owners']==1
            verify(store, corrected)
    finally:
        ledger.close()
        reference_ledger.close()
        client.close()
        service.close()


def test_category_projection_preserves_filtered_totals_without_decoding_call_bodies(tmp_path, monkeypatch):
    from copy import deepcopy
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.category_usage import project
    from cachemonitor.core import Session
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    from cachemonitor.prepared_ingest import prepare_owner, stage_owner

    sources = []
    for sid, model in (('selected', 'gpt-6-astra'), ('other', 'gpt-6-sol')):
        session = Session(sid, 'home', title=sid, cwd='D:/'+sid)
        for ordinal in range(4):
            session.add_usage(100+ordinal, f'{sid}-{ordinal}',
                dict(input_tokens=100+ordinal, cached_input_tokens=20, output_tokens=10),
                model, 'request', 'medium', service_tier='Standard')
        sources.append(deepcopy(session.view(110)))
    reference = AnalysisEngine()
    reference.ingest(sources)
    dependency = {name:1 for name in storage.DEPENDENCIES}
    dependency['source_digest'] = '1'*64
    query = dict(page=1, start=100, end=110, now=110, period='custom', model='gpt-6-astra',
        category_usage=True, presentation=True, usage=dict(dimension='project', metric='cost'))
    with storage.PreparedStore(tmp_path/'category.sqlite') as store:
        stage = store.begin(('category', 1))
        for ordinal, source in enumerate(sources):
            stage_owner(stage, prepare_owner(source, owns=lambda *_:True), dependency, ordinal)
        token = stage.publish(('category', 1), ready=True)
        reference.revision = reference.call_revision = token.generation
        engine = PreparedAnalysisEngine(store.path, token)
        try:
            def forbidden(*args, **kwargs):
                raise AssertionError('category display decoded full call bodies')
            monkeypatch.setattr(storage.Snapshot, '_decode_call', forbidden)
            with engine.read_scope():
                actual = project(engine, query)
                assert engine.category_bounds({'model':'gpt-6-astra'}) == (100, 103)
            assert actual == project(reference, query)
            reads = engine.read_metrics['category_projection_rows']
            with engine.read_scope():
                project(engine, {**query, 'usage':{**query['usage'], 'sort':'name'}})
            assert engine.read_metrics['category_projection_rows'] == reads
            boundary_reads = []
            boundaries = engine.boundary_times
            def count_boundaries(at):
                boundary_reads.append(at)
                yield from boundaries(at)
            monkeypatch.setattr(engine, 'boundary_times', count_boundaries)
            query = dict(query, period='all')
            assert engine.page_query(query) == reference.page_query(query)
            assert boundary_reads == [query['end']]
            engine.page_query({**query, 'usage':{**query['usage'], 'sort':'name'}})
            assert boundary_reads == [query['end']]
        finally:
            engine.close()


def test_corrupt_current_cache_regenerates_without_changing_source_ledger(tmp_path):
    from contextlib import closing

    path = tmp_path/'analysis.sqlite'
    ledger = tmp_path/'source-ledger.sqlite'
    with sqlite3.connect(ledger) as source:
        source.execute('CREATE TABLE original(value TEXT)')
        source.execute("INSERT INTO original VALUES('retained source')")
    ledger_bytes = ledger.read_bytes()
    with storage.PreparedStore(path) as store:
        original = publish(store, 1, calls())
        page = store.db.execute("SELECT rootpage FROM sqlite_master WHERE name='calls'").fetchone()[0]
        page_size = store.db.execute('PRAGMA page_size').fetchone()[0]

    # Damage a derived-data B-tree, leaving the identifying header and meta intact.
    with path.open('r+b') as target:
        target.seek((page-1)*page_size)
        target.write(b'\x00')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as check:
        assert check.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok'
    with storage.PreparedStore(path) as store, storage.PreparedReader(path) as reader:
        assert store.token.instance != original.instance
        assert store.token.generation == 0
        assert store.db.execute('PRAGMA quick_check(1)').fetchone()[0] == 'ok'
        with pytest.raises(storage.NotReady):
            reader.pin()
        recovered = publish(store, 1, calls())
        with reader.pin(recovered) as view:
            assert view.record(*OWNER, 'call-1')['cost'] == 1.
    assert ledger.read_bytes() == ledger_bytes


@pytest.mark.parametrize('code', [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_IOERR])
def test_startup_check_recovers_only_reported_corruption(tmp_path, monkeypatch, code):
    path = tmp_path/'analysis.sqlite'
    with storage.PreparedStore(path) as store:
        original = publish(store, 1, calls())
    before = path.read_bytes()
    original_connect = sqlite3.connect

    class CheckConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'PRAGMA quick_check(1)':
                error = sqlite3.DatabaseError('injected check failure')
                error.sqlite_errorcode = code
                raise error
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return original_connect(*args, factory=CheckConnection, **kwargs)

    monkeypatch.setattr(storage.sqlite3, 'connect', connect)
    if code == sqlite3.SQLITE_CORRUPT:
        with storage.PreparedStore(path) as store:
            assert store.token.instance != original.instance
            assert store.token.generation == 0
    else:
        with pytest.raises(sqlite3.DatabaseError) as error:
            storage.PreparedStore(path)
        assert error.value.sqlite_errorcode == code
        assert path.read_bytes() == before


def test_startup_check_deadline_does_not_replace_a_healthy_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path/'analysis.sqlite'
    with storage.PreparedStore(path) as store:
        original = publish(store, 1, calls())
        store.db.execute('CREATE TABLE health_rows(value INTEGER)')
        store.db.executemany('INSERT INTO health_rows VALUES(?)', ((i,) for i in range(2000)))
    before = path.read_bytes()
    moments = iter((0., 11.))
    with monkeypatch.context() as patch:
        patch.setattr(storage, 'time', SimpleNamespace(monotonic=lambda:next(moments, 11.)))
        with pytest.raises(TimeoutError, match='integrity check exceeded'):
            storage.PreparedStore(path)
    assert path.read_bytes() == before
    with storage.PreparedStore(path) as store:
        assert store.token == original


def test_known_old_format_regenerates_atomically_and_preserves_other_stores(tmp_path, monkeypatch):
    path = tmp_path/'analysis.sqlite'
    original_connect = sqlite3.connect

    def old_cache(target, *, application_id=storage.APPLICATION_ID, version=4, payload=16):
        db = original_connect(target, isolation_level=None)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute(f'PRAGMA application_id={application_id}')
            db.execute('CREATE TABLE meta(singleton INTEGER PRIMARY KEY, format INTEGER)')
            db.execute('INSERT INTO meta VALUES(1,?)', (version,))
            db.execute('CREATE TABLE original(value BLOB)')
            db.execute('INSERT INTO original VALUES(zeroblob(?))', (payload,))
        finally:
            db.close()

    # A retained source ledger is outside the reset's ownership boundary.
    ledger = tmp_path/'source-ledger.sqlite'
    old_cache(ledger, application_id=7)
    ledger_bytes = ledger.read_bytes()
    for name, application_id, version in (
            ('foreign', 7, 4), ('future', storage.APPLICATION_ID, storage.FORMAT+1),
            ('unknown', storage.APPLICATION_ID, 0)):
        other = tmp_path/(name+'.sqlite')
        old_cache(other, application_id=application_id, version=version)
        before = other.read_bytes()
        with pytest.raises(storage.RebuildRequired):
            storage.PreparedStore(other)
        assert other.read_bytes() == before
    damaged = tmp_path/'damaged.sqlite'
    damaged.write_bytes(b'ownership cannot be established')
    with pytest.raises(sqlite3.DatabaseError):
        storage.PreparedStore(damaged)
    assert damaged.read_bytes() == b'ownership cannot be established'

    old_cache(path, payload=8*1024*1024)
    blocked = original_connect(path, isolation_level=None)
    try:
        blocked.execute('BEGIN')
        assert blocked.execute('SELECT format FROM meta').fetchone()[0] == 4
        with pytest.raises(sqlite3.OperationalError) as error:
            storage.PreparedStore(path)
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert blocked.execute('SELECT length(value) FROM original').fetchone()[0] == 8*1024*1024
    finally:
        blocked.close()

    class InterruptedConnection(sqlite3.Connection):
        def backup(self, target, *, progress, **kwargs):
            def interrupt(status, remaining, total):
                progress(status, remaining, total)
                if remaining:
                    raise InterruptedError('interrupted prepared reset')
            return super().backup(target, progress=interrupt, **kwargs)

    def connect(*args, **kwargs):
        if args[0] == ':memory:':
            kwargs['factory'] = InterruptedConnection
        return original_connect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage.sqlite3, 'connect', connect)
        with pytest.raises(InterruptedError, match='interrupted prepared reset'):
            storage.PreparedStore(path)
    check = original_connect(path)
    try:
        assert check.execute('SELECT format FROM meta').fetchone()[0] == 4
        assert check.execute('SELECT length(value) FROM original').fetchone()[0] == 8*1024*1024
    finally:
        check.close()

    with storage.PreparedStore(path) as store, storage.PreparedReader(path) as reader:
        assert store.token.generation == 0
        with pytest.raises(storage.NotReady):
            reader.pin()
        assert store.db.execute("SELECT 1 FROM sqlite_master WHERE name='original'").fetchone() is None
        assert path.stat().st_size <= 1024*1024
        assert store.disk_usage()['wal'] <= 1024*1024+8192
        token = publish(store, 1, calls())
        with reader.pin(token) as view:
            assert view.record(*OWNER, 'call-1')['cost'] == 1.
    with storage.PreparedStore(path) as store:
        assert store.token == token
    assert ledger.read_bytes() == ledger_bytes
    assert not list(tmp_path.glob('*.backup'))


@pytest.mark.parametrize('reorder,capacity_fallback', [(False, False), (True, False), (True, True)])
def test_competing_source_update_resumes_candidate_inventory_after_restart(tmp_path, monkeypatch, reorder, capacity_fallback):
    from contextlib import closing
    from copy import deepcopy
    import time
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.analysis_delivery import SnapshotReceiver
    from cachemonitor.analysis_records import public_record
    from cachemonitor.core import Session
    from cachemonitor.prepared_ingest import PreparedCohort, read_catalog, preparation_signature
    from cachemonitor.prepared_delivery import PreparedPublisher
    from cachemonitor.prepared_speed_writer import PreparedSpeedWriter, SPEED_CHECKPOINT_NAMESPACE
    from cachemonitor import speed_health
    from cachemonitor.usage_collection import CollectionChannel, CollectionClient, empty_snapshot

    writer = CollectionChannel([tmp_path/'home'], tmp_path/'source.sqlite')
    reader = CollectionClient(writer.homes, writer.path)
    writer.start_live()
    path = tmp_path/'prepared.sqlite'
    if capacity_fallback:monkeypatch.setattr(speed_health, 'MAX_METADATA_BYTES', 1)
    try:
        sources = []
        for sid in (('z', 'a', 'idle') if reorder else ('z', 'a')):
            session = Session(sid, writer.homes[0], title=sid)
            for ordinal, key in enumerate(('shared', sid+'-own')):
                session.add_usage(100+ordinal, key,
                    dict(input_tokens=100, cached_input_tokens=20, output_tokens=10),
                    'gpt-6-astra', 'request', 'medium', service_tier='Standard')
            sources.append(deepcopy(session.view(110)))
            if capacity_fallback:
                for row in sources[-1]['history']:
                    row.update(output=2000, reasoning=0, transport='WebSocket', transport_source='response_id',
                               response_status='completed', timing_valid=True, completion_latency_ms=1000)

        def publish_sources(sequence):
            now = time.time()
            snapshot = empty_snapshot(writer.homes, writer.path, now)
            snapshot.update(sessions=sources, usage_collection_complete=True,
                            last_usage_collection_success=now, request_activity=[], quota_by_home={})
            snapshot['index'].update(loading=False, usage_complete=True, last_usage_success=now)
            writer.publish(snapshot, 'candidate-proof', sequence)
            return read_catalog(reader)

        catalog = publish_sources(1)
        with storage.PreparedStore(path) as store, closing(PreparedSpeedWriter(
                store, preparation=preparation_signature())) as speed:
            coordinator = PreparedCohort(store, reader, None, speed_writer=speed)
            list(coordinator.convert(catalog, cohort_size=1))
            first, second = tuple(catalog['owners'])[:2]
            if reorder:sources.reverse()
            for source in sources:
                if (source['home'], source['id']) == first:
                    source['history'][1]['output'] += 7
                    source['usage_revision'] = 2
                elif (source['home'], source['id']) == second:
                    source['history'].append(dict(source['history'][1], key='late-new', ts=103))
                    source['usage_revision'] = 2
            catalog = publish_sources(2)
            conversion = coordinator.convert(catalog, cohort_size=1)
            assert not next(conversion)['ready']
            conversion.close()
            with storage.PreparedReader(path) as prepared:
                with pytest.raises(storage.NotReady):
                    prepared.pin(store.token)

        with storage.PreparedStore(path) as store, closing(PreparedSpeedWriter(
                store, preparation=preparation_signature())) as speed:
            coordinator = PreparedCohort(store, reader, None, speed_writer=speed)
            assert list(coordinator.convert(catalog, cohort_size=1))[-1]['ready']
            reference = AnalysisEngine()
            reference.ingest(sources)
            with storage.PreparedReader(path) as prepared, prepared.pin(store.token) as view:
                summaries = {owner:view.derived(*owner, 'summary') for owner in reference.sessions}
                for owner, state in reference.sessions.items():
                    assert list(view.iter_calls(owner)) == [public_record(row) for row in state['prepared']['history']]
                assert view.record(*second, 'late-new') is not None

            def forbidden(*args, **kwargs):
                raise AssertionError('unchanged source reread after candidate recovery')
            source_stream = reader.selected_owner_stream
            monkeypatch.setattr(reader, 'selected_owner_stream', forbidden)
            assert list(coordinator.convert(catalog, cohort_size=1)) == []
            speeds = deepcopy(coordinator.current_speeds())
            publisher = PreparedPublisher(0)
            receiver = SnapshotReceiver()
            publication = dict(homes=writer.homes)
            first_publication = receiver.receive(publisher.publish(publication, store, speeds))
            with store.read_current() as view:
                checkpoint = view.checkpoint_state(SPEED_CHECKPOINT_NAMESPACE)
                current_checkpoint = checkpoint is not None and tuple(checkpoint['token']) == (view.token.instance, view.token.generation)
                assert current_checkpoint != capacity_fallback
            speed_reads = []
            speed_rows = storage.Snapshot.iter_speed_rows
            def observed_speed_rows(*args, **kwargs):
                speed_reads.append(True)
                yield from speed_rows(*args, **kwargs)
            monkeypatch.setattr(storage.Snapshot, 'iter_speed_rows', observed_speed_rows if capacity_fallback else forbidden)
            sources.reverse()
            catalog = publish_sources(3)
            updates = []
            for update in coordinator.convert(catalog, cohort_size=1):
                updates.append(update)
                if not update['ready']:assert not speed_reads
            assert updates[-1]['ready']
            assert all(not update['ready'] for update in updates[:-1])
            assert coordinator.current_speeds() == speeds
            with storage.PreparedReader(path) as prepared, prepared.pin(store.token) as view:
                for owner, state in reference.sessions.items():
                    assert view.owner(*owner)['owner_ordinal'] == catalog['owners'][owner]['source_ordinal']
                    assert list(view.iter_calls(owner)) == [public_record(row) for row in state['prepared']['history']]
                    assert view.derived(*owner, 'summary') == summaries[owner]
            with monkeypatch.context() as order_only:
                def no_derived_reads(*args, **kwargs):
                    raise AssertionError('traversal order must not reread unchanged prepared content')
                order_only.setattr(storage.Snapshot, 'derived', no_derived_reads)
                publisher.adopt(store, {owner for update in updates for owner in update['changed']})
                reordered_publication = receiver.receive(publisher.publish(publication, store, speeds))
            assert reordered_publication['cache_candidates'] == []
            assert reordered_publication['sessions'] == first_publication['sessions']
            assert reordered_publication['filter_choices'] == first_publication['filter_choices']
            def without_token(values):
                return {(value['home'], value['id']):{key:item for key,item in value.items()
                        if key!='_prepared_token'} for value in values}
            assert without_token(reordered_publication['overlay_sessions']) == without_token(first_publication['overlay_sessions'])
            if reorder and not capacity_fallback:
                monkeypatch.setattr(reader, 'selected_owner_stream', source_stream)
                monkeypatch.setattr(storage.Snapshot, 'iter_speed_rows', speed_rows)
                newcomer = Session('newcomer', writer.homes[0], title='newcomer')
                newcomer.add_usage(120, 'new-first', dict(input_tokens=100, cached_input_tokens=20, output_tokens=10),
                                   'gpt-6-astra', 'request', 'medium', service_tier='Standard')
                sources.insert(0,deepcopy(newcomer.view(120)))
                captured = publish_sources(4); later=[]; reorder_batch=coordinator._reorder
                def source_grows_during_reorder(*args, **kwargs):
                    if not later:
                        sources[0]['history'].append(dict(sources[0]['history'][0],key='new-later',ts=121))
                        sources[0]['usage_revision']=2
                        later.append(publish_sources(5))
                    return reorder_batch(*args, **kwargs)
                monkeypatch.setattr(coordinator,'_reorder',source_grows_during_reorder)
                assert list(coordinator.convert(captured,cohort_size=1))[-1]['ready']
                with store.read_current() as view:
                    assert [r['key'] for r in view.iter_calls((writer.homes[0],'newcomer'))] == ['new-first']
                assert list(coordinator.convert(later[0],cohort_size=1))[-1]['ready']
                with store.read_current() as view:
                    assert [r['key'] for r in view.iter_calls((writer.homes[0],'newcomer'))] == ['new-first','new-later']
    finally:
        reader.close()
        writer.close()


def test_performance_full_range_streams_exact_charts_within_the_object_budget(tmp_path, monkeypatch):
    from collections import OrderedDict
    from datetime import datetime
    from types import SimpleNamespace
    from cachemonitor.performance_trends import PerformanceTrends
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine

    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    base = datetime(2026, 1, 1).timestamp()
    owners = OrderedDict()
    for number, sid in enumerate(('first', 'second')):
        owner = ('home', sid)
        owners[owner] = [dict(home=owner[0], sid=sid, key=f'{sid}-{index}', turn='request',
            session_ordinal=index, ts=base+(index//2)*86400, model='model', service_tier='Standard',
            effort='high', duration=float(index%7), cost=.01*(index%9), input=100+900*number,
            cached=20, output=10, reasoning=5, completion_latency_ms=1000, output_speed=10.)
            for index in range(60)]
    reference = SimpleNamespace(revision=1, sessions=OrderedDict(
        (owner, dict(prepared=dict(history=rows))) for owner, rows in owners.items()))
    # A whole-range dictionary list exceeds this test limit. A scalar window,
    # one calendar bucket and their exact quantile scratch fit comfortably.
    limits = replace(storage.Limits(), page_objects_bytes=64*1024, read_seconds=60, read_vm_steps=20_000_000)
    with storage.PreparedStore(tmp_path/'performance.sqlite') as store:
        stage = store.begin(('performance', 1))
        dependency = {name:1 for name in storage.DEPENDENCIES}
        dependency['source_digest'] = '1'*64
        for ordinal, (owner, rows) in reversed(list(enumerate(owners.items()))):
            stage.owner(*owner, dict(home=owner[0], id=owner[1], _internal_review=False),
                        dependency, source_ordinal=ordinal)
            stage.calls(owner, rows, reconcile=True)
        token = stage.publish(('performance', 1), ready=True)
        engine = PreparedAnalysisEngine(store.path, token, limits=limits)
        def forbidden(*args, **kwargs):
            raise AssertionError('Performance must not decode full call packets')
        monkeypatch.setattr(storage.Snapshot, '_decode_call', forbidden)
        try:
            for granularity in ('auto', 'day', 'week', 'month'):
                query = dict(page=5, now=base+31*86400, plot_width=64,
                             granularity=granularity, time_range=None)
                actual = engine.page_query(query)['performance']
                assert actual == PerformanceTrends().query(reference, query)
                assert actual['calls'] == 120
                assert engine.read_metrics['trend_selected_rows'] == 120
                assert engine.read_metrics['trend_scalar_peak_bytes'] <= limits.page_objects_bytes
        finally:
            engine.close()


def test_selected_performance_windows_preserve_catalog_and_exact_speed_population(tmp_path, monkeypatch):
    from collections import OrderedDict
    from datetime import datetime
    from types import SimpleNamespace
    from cachemonitor.performance_trends import PerformanceTrends
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine

    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    now=datetime(2026,10,17,12).timestamp();day=86400
    observations=[(-60,'model','Standard',None,1000),(-45,'other','Standard',10.,1000),
                  (-20,'model','Standard',0.,1000),(-15,'model','Fast',10.,1000),
                  (-6,'model','Standard',None,1000),(-5,'model','Standard',10.,1000),
                  (-3,'model','Standard',10.,0),(-2,'model','Standard',10.,1000),
                  (-1,'model','Standard',None,1000),(2,'future','Standard',10.,1000),
                  (-60,'no-speed','Fast',None,1000),(-.5/day,'no-speed','Fast',None,1000)]
    rows=[dict(home=OWNER[0],sid=OWNER[1],key=f'window-{index}',turn='request',session_ordinal=index,
               ts=now+offset*day,model=model,service_tier=mode,effort='high',duration=1.,cost=.1,
               input=100,cached=20,reasoning=5,output=0 if speed==0 else 10,
               completion_latency_ms=latency,output_speed=speed)
          for index,(offset,model,mode,speed,latency) in enumerate(observations)]
    reference=SimpleNamespace(revision=1,sessions=OrderedDict([(OWNER,dict(prepared=dict(history=rows)))]))
    standard=dict(models=['model'],modes=['Standard'])
    cases=[('7d',standard,None,(now-7*day,now),5,2),
           ('30d',standard,None,(now-30*day,now),6,3),
           ('all',standard,None,(now-20*day,now-2*day),5,3),
           ('all',dict(models=['model'],modes=['Fast']),None,(now-15*day,now-15*day+1),1,1),
           ('all',dict(models=[],modes=['Standard']),None,(now-1,now),0,0),
           ('all',dict(models=None,modes=[]),None,(now-1,now),0,0),
           ('all',dict(models=['no-speed'],modes=['Fast']),None,(now-1,now),0,0),
           ('7d',dict(models=['no-speed'],modes=['Fast']),None,(now-7*day,now),1,0),
           (None,standard,(now-7*day,now-4*day),(now-7*day,now-4*day),2,1),
           ('7d',dict(models=None,modes=['Standard']),None,(now-7*day,now),5,2)]
    with storage.PreparedStore(tmp_path/'selected-performance.sqlite') as store:
        stage=store.begin(('performance',1));dependency={name:1 for name in storage.DEPENDENCIES}
        dependency['source_digest']='1'*64
        stage.owner(*OWNER,dict(home=OWNER[0],id=OWNER[1],_internal_review=False),dependency,source_ordinal=0)
        stage.calls(OWNER,rows,reconcile=True);token=stage.publish(('performance',1),ready=True)
        engine=PreparedAnalysisEngine(store.path,token)
        reducer=PerformanceTrends()
        def forbidden(*args,**kwargs):
            raise AssertionError('Selected trends must only read scalar call columns')
        monkeypatch.setattr(storage.Snapshot,'_decode_call',forbidden)
        try:
            for period,selection,window,bounds,count,eligible in cases:
                query=dict(page=5,now=now,plot_width=64,granularity='auto',trend_period=period,
                           trend_selection=selection,time_range=window)
                actual=engine.page_query(query)['performance']
                assert actual==reducer.query(reference,query)
                assert (actual['start'],actual['end'])==bounds
                assert actual['models']==['future','model','no-speed','other']
                assert actual['calls']==count
                assert engine.read_metrics['trend_selected_rows']==count
                speed=next(panel for panel in actual['panels'] if panel['key']=='output_speed')
                assert (speed['n'],speed['N'])==(eligible,count)
            calendar=dict(query,granularity='day',trend_period='all',trend_selection=standard,time_range=None)
            assert engine.page_query(calendar)['performance']==reducer.query(reference,calendar)
        finally:
            engine.close()


def test_performance_many_owners_complete_within_the_read_work_budget(tmp_path):
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    from cachemonitor.prepared_performance import _Input

    limits = replace(storage.Limits(), read_seconds=60, read_vm_steps=60_000)
    with storage.PreparedStore(tmp_path/'many-owners.sqlite') as store:
        stage = store.begin(('performance', 1))
        dependency = {name:1 for name in storage.DEPENDENCIES}
        dependency['source_digest'] = '1'*64
        for owner in reversed(range(64)):
            identity = ('home', str(owner))
            stage.owner(*identity, dict(home='home', id=str(owner), _internal_review=False),
                        dependency, source_ordinal=owner)
            stage.calls(identity, [dict(key=str(number), ts=100+number, session_ordinal=number,
                model='model', input=100, cached=20) for number in range(4)], reconcile=True)
        token = stage.publish(('performance', 1), ready=True)
        engine = PreparedAnalysisEngine(store.path, token, limits=limits)
        try:
            with engine.read_scope():
                rows = list(_Input(engine).ordered_rows(100, 104, keys=True))
            assert [(row['ts'], row['sid']) for row in rows] == [
                (100+number, str(owner)) for number in range(4) for owner in range(64)]
        finally:
            engine.close()


def test_query_progress_coalesces_and_discards_replaced_requests():
    from cachemonitor.analysis_query_actor import QueryActor

    started, release, completed = Event(), Event(), Event()
    class Handler:
        def __call__(self, message, token, cancel):
            message['_progress']('query')
            message['_progress']('aggregate')
            if message['id'] == 1:
                started.set()
                assert release.wait(5)
            else:
                completed.set()
            return dict(kind='result', id=message['id'])

        def close(self):
            pass

    actor = QueryActor(Handler)
    try:
        actor.submit(dict(kind='query', id=1), None)
        assert started.wait(5)
        assert actor.drain() == [dict(kind='progress', id=1, stage='aggregate')]
        actor.submit(dict(kind='query', id=2), None)
        release.set()
        assert completed.wait(5)
        assert actor.notification.poll(5)
        messages = actor.drain()
        assert all(message['id'] == 2 for message in messages)
        if not any(message['kind'] == 'result' for message in messages):
            assert actor.notification.poll(5)
            messages += actor.drain()
        assert [message for message in messages if message['kind'] == 'result'] == [dict(kind='result', id=2)]
    finally:
        release.set()
        actor.close()


def test_overlay_detail_completes_without_restarting_pending_page(tmp_path, monkeypatch):
    from multiprocessing import Pipe
    from threading import Thread
    from types import SimpleNamespace
    import time
    from cachemonitor import prepared_runtime

    started, release, cancelled, stopped = Event(), Event(), Event(), Event()
    output = prepared_runtime.Mailbox(stopped)
    closed = []
    page_starts = []
    failures = []
    writer = SimpleNamespace(path=tmp_path/'prepared.sqlite', output=output,
                             close=lambda:closed.append('writer'))
    monkeypatch.setattr(prepared_runtime, 'PreparedWriter', lambda *args:writer)
    class Handler:
        def __init__(self, path):pass
        def __call__(self, message, token, cancel):
            if message['kind'] == 'query':
                page_starts.append(message['id']);started.set()
                deadline = time.monotonic()+5
                while not release.is_set():
                    if cancel():cancelled.set();break
                    assert time.monotonic() < deadline
                    release.wait(.01)
                return dict(kind='result', id=message['id'])
            return dict(kind=message['kind'], id=message['id'])
        def close(self):closed.append('reader')
    monkeypatch.setattr(prepared_runtime, 'PreparedQuery', Handler)
    parent, child = Pipe()
    def dispatch():
        try:prepared_runtime.process_main(child, [], None, None, tmp_path/'quota.sqlite')
        except BaseException as error:failures.append(error)
    thread = Thread(target=dispatch, daemon=True)
    thread.start()
    def receive(kind):
        deadline = time.monotonic()+5
        while True:
            remaining = deadline-time.monotonic()
            assert remaining > 0 and parent.poll(remaining), f'{kind} did not complete'
            message = parent.recv()
            if message['kind'] == kind:return message
    try:
        output.put(dict(kind='snapshot', value={}, token=('instance', 1), transition=None))
        receive('snapshot')
        parent.send(dict(kind='query', id='page', query={}, logical=()))
        assert started.wait(5)
        parent.send(dict(kind='overlay_detail', id='overlay', summary={'_prepared_token':('instance', 1)}))
        assert receive('overlay_detail')['id'] == 'overlay'
        assert not cancelled.is_set() and page_starts == ['page']
        release.set()
        assert receive('result')['id'] == 'page'
    finally:
        release.set()
        parent.send(dict(kind='stop'))
        thread.join(5)
        parent.close();child.close();output.close()
    assert not thread.is_alive() and not failures
    assert closed.count('reader') == 2 and closed.count('writer') == 1
