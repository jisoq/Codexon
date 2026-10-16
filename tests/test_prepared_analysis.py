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
        category_usage=True, presentation=True, usage=dict(dimension='project', metric='cost', size=8))
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
                project(engine, {**query, 'usage':{**query['usage'], 'sort':'name', 'size':1}})
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
            engine.page_query({**query, 'usage':{**query['usage'], 'sort':'name', 'size':1}})
            assert boundary_reads == [query['end']]
        finally:
            engine.close()


def test_home_totals_use_canonical_summary_and_refresh_changed_bounds(tmp_path, monkeypatch):
    from copy import deepcopy
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.core import Session
    from cachemonitor.prepared_analysis import PreparedAnalysisEngine
    from cachemonitor.prepared_ingest import PREPARATION, prepare_owner, stage_owner

    sources = []
    for sid, model in (('selected', 'gpt-6-astra'), ('other', 'gpt-6-sol')):
        session = Session(sid, 'home', title=sid, cwd='D:/project')
        for ordinal in range(4):
            session.add_usage(100+ordinal, f'{sid}-{ordinal}',
                dict(input_tokens=100+ordinal, cached_input_tokens=20 if ordinal else None,
                     output_tokens=10), model, 'request', 'medium', service_tier='Standard')
        source = deepcopy(session.view(110))
        source['usage_revision'] = 1
        sources.append(source)
    reference = AnalysisEngine()
    reference.ingest(deepcopy(sources))
    # Ownership excludes this duplicate, so raw coverage cannot prove Home coverage.
    sources[0]['history'].insert(0, dict(sources[0]['history'][0], key='duplicate', ts=1))
    dependency = {name:1 for name in storage.DEPENDENCIES}
    dependency.update(source_digest='1'*64, preparation=PREPARATION)
    owns = lambda _, row:row['key'] != 'duplicate'
    projected_rows = []
    original_projection = storage.Snapshot.projected_calls

    def projection(*args, **kwargs):
        result = original_projection(*args, **kwargs)
        projected_rows.extend(1 for _ in result.rows)
        return result

    def forbidden(*args, **kwargs):
        raise AssertionError('Home decoded full call bodies')

    query = dict(page=0, start=100, end=110, now=110, period='all',
                 category_usage=True, presentation=True)
    with storage.PreparedStore(tmp_path/'home.sqlite') as store:
        stage = store.begin(('home', 1))
        for ordinal, source in enumerate(sources):
            stage_owner(stage, prepare_owner(source, owns=owns), dependency, ordinal)
        token = stage.publish(('home', 1), ready=True)
        engine = PreparedAnalysisEngine(store.path, token)
        try:
            monkeypatch.setattr(storage.Snapshot, '_decode_call', forbidden)
            monkeypatch.setattr(storage.Snapshot, 'projected_calls', projection)

            def compare(scope):
                actual, expected = engine.page_query(scope), reference.page_query(scope)
                assert actual['analysis'] == expected['analysis']
                assert storage.pack(actual['usage']) == storage.pack(expected['usage'])

            compare(query)
            compare({**query, 'end':111, 'now':111})
            assert not projected_rows
            partial = {**query, 'start':101.1, 'end':102.2, 'period':'custom', 'model':'gpt-6-astra'}
            compare(partial)
            assert projected_rows
            projected_rows.clear()
            compare({**partial, 'end':102.8})
            assert not projected_rows

            changed = deepcopy(sources[0])
            changed['usage_revision'] = 2
            changed['history'][2]['input'] += 7
            changed['history'].append(dict(changed['history'][-1], key='new-call', ts=115))
            reference.ingest([dict(changed, history=[row for row in changed['history'] if owns(None, row)])])
            stage = store.begin(('home', 2))
            updated = {**dependency, 'source_digest':'2'*64}
            stage_owner(stage, prepare_owner(changed, owns=owns), updated, 0)
            stage.owner('home', 'other', dict(home='home', id='other'), updated, source_ordinal=1)
            stage.remove_owner(('home', 'other'))
            token = stage.publish(('home', 2), ready=True)
            engine.adopt(token, [('home', 'selected')], [('home', 'other')])
            compare({**query, 'end':120, 'now':120})
            assert not projected_rows
        finally:
            engine.close()


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


def test_competing_source_update_resumes_candidate_inventory_after_restart(tmp_path, monkeypatch):
    from copy import deepcopy
    import time
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.analysis_records import public_record
    from cachemonitor.core import Session
    from cachemonitor.prepared_ingest import PreparedCohort, read_catalog
    from cachemonitor.usage_collection import CollectionChannel, CollectionClient, empty_snapshot

    writer = CollectionChannel([tmp_path/'home'], tmp_path/'source.sqlite')
    reader = CollectionClient(writer.homes, writer.path)
    writer.start_live()
    path = tmp_path/'prepared.sqlite'
    try:
        sources = []
        for sid in ('z', 'a'):
            session = Session(sid, writer.homes[0], title=sid)
            for ordinal, key in enumerate(('shared', sid+'-own')):
                session.add_usage(100+ordinal, key,
                    dict(input_tokens=100, cached_input_tokens=20, output_tokens=10),
                    'gpt-6-astra', 'request', 'medium', service_tier='Standard')
            sources.append(deepcopy(session.view(110)))

        def publish_sources(sequence):
            now = time.time()
            snapshot = empty_snapshot(writer.homes, writer.path, now)
            snapshot.update(sessions=sources, usage_collection_complete=True,
                            last_usage_collection_success=now, request_activity=[], quota_by_home={})
            snapshot['index'].update(loading=False, usage_complete=True, last_usage_success=now)
            writer.publish(snapshot, 'candidate-proof', sequence)
            return read_catalog(reader)

        catalog = publish_sources(1)
        with storage.PreparedStore(path) as store:
            coordinator = PreparedCohort(store, reader, None)
            list(coordinator.convert(catalog, cohort_size=1))
            first, second = tuple(catalog['owners'])
            for source in sources:
                if (source['home'], source['id']) == first:
                    source['history'][1]['output'] += 7
                else:
                    source['history'].append(dict(source['history'][1], key='late-new', ts=103))
                source['usage_revision'] = 2
            catalog = publish_sources(2)
            conversion = coordinator.convert(catalog, cohort_size=1)
            assert not next(conversion)['ready']
            conversion.close()
            with storage.PreparedReader(path) as prepared:
                with pytest.raises(storage.NotReady):
                    prepared.pin(store.token)

        with storage.PreparedStore(path) as store:
            coordinator = PreparedCohort(store, reader, None)
            assert list(coordinator.convert(catalog, cohort_size=1))[-1]['ready']
            reference = AnalysisEngine()
            reference.ingest(sources)
            with storage.PreparedReader(path) as prepared, prepared.pin(store.token) as view:
                for owner, state in reference.sessions.items():
                    assert list(view.iter_calls(owner)) == [public_record(row) for row in state['prepared']['history']]
                assert view.record(*second, 'late-new') is not None

            def forbidden(*args, **kwargs):
                raise AssertionError('unchanged source reread after candidate recovery')
            monkeypatch.setattr(reader, 'selected_owner_stream', forbidden)
            assert list(coordinator.convert(catalog, cohort_size=1)) == []
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
