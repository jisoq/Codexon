from collections.abc import Mapping
from dataclasses import replace

import pytest

from cachemonitor import prepared_store as storage
from cachemonitor.analysis_engine import retained_bytes
from cachemonitor.prepared_analysis import PreparedAnalysisEngine


BUDGET = 12 * 1024


def population(count=24):
    return [(('home-' + str(number % 3), f'owner-{number:02}'),
             dict(home='home-' + str(number % 3), id=f'owner-{number:02}',
                  title=f'Owner {number} ' + 'x' * 256,
                  _internal_review=number % 7 == 0,
                  source='cli', project=f'project-{number % 2}',
                  archived=number % 5 == 0, model='gpt-6-astra',
                  service_tier='Fast' if number % 2 else 'Standard',
                  parent_thread_id='parent', mode_source='parent'))
            for number in range(count)]


def publish(store, revision, owners, *, order=None, calls=False):
    source = ('owner-test', revision)
    stage = store.begin(source)
    dependency = dict.fromkeys(storage.DEPENDENCIES, 1)
    dependency['source_digest'] = f'{revision:064x}'
    for ordinal, (key, metadata) in enumerate(owners):
        position = order.index(key) if order is not None else ordinal if revision == 1 else None
        stage.owner(*key, metadata, dependency, source_ordinal=position)
        if calls:
            stage.calls(key, [dict(key='response-' + key[1], ts=100 + ordinal,
                session_ordinal=0, home=key[0], sid=key[1], model='gpt-6-astra',
                service_tier=metadata['service_tier'], input=10, cached=0,
                output=ordinal + 1, cost=float(ordinal + 1), turn=None)], reconcile=True)
    if order is not None:
        stage.catalog_order((key, ordinal) for ordinal, key in enumerate(order))
    return stage.publish(source, ready=True)


def query(**selection):
    return dict(page=3, start=0, end=1000, now=1000, period='all',
                include_whole_history=False, **selection)


def test_owner_population_exceeding_cache_budget_keeps_exact_queries_and_catalog_order(tmp_path, monkeypatch):
    owners = population()
    order = [key for key, metadata in reversed(owners)]
    assert all(retained_bytes((key, metadata), BUDGET) < BUDGET for key, metadata in owners)
    assert sum(retained_bytes((key, metadata), BUDGET) for key, metadata in owners) > BUDGET
    monkeypatch.setattr(PreparedAnalysisEngine, 'OWNER_BYTES', BUDGET)
    limits = replace(storage.Limits(), page_rows=2)
    with storage.PreparedStore(tmp_path / 'owners.sqlite', limits) as store:
        token = publish(store, 1, owners, order=order, calls=True)
        engine = PreparedAnalysisEngine(store.path, token, limits=limits)
        try:
            assert isinstance(engine.sessions, Mapping)
            assert isinstance(engine.internal_sessions, Mapping)
            visible = [key for key, metadata in reversed(owners) if not metadata['_internal_review']]
            assert list(engine.sessions) == visible
            assert list(engine.internal_sessions) == [owners[number][0] for number in (21, 14, 7, 0)]
            result = engine.query(query())['analysis']
            assert [row['id'] for row in result['sessions']] == [key[1] for key in visible]
            expected = [number + 1 for number in reversed(range(24)) if number % 7]
            assert [row['output'] for row in result['responses']] == expected
            assert result['totals']['cost'] == sum(expected)
            selected = engine.query(query(home=owners[1][0][0], sid=owners[1][0][1]))['analysis']
            assert [row['output'] for row in selected['responses']] == [2]
            assert selected['responses'][0]['service_tier'] == 'Fast'
            assert engine.sessions[owners[1][0]]['prepared']['parent_thread_id'] == 'parent'
            assert engine.sessions[owners[1][0]]['prepared']['mode_source'] == 'parent'
            assert engine.internal_sessions[owners[0][0]]['owner_ordinal'] == 23
            assert engine.snapshot is None and not engine.reader.db.in_transaction
        finally:
            engine.close()


def test_owner_lookup_and_filtered_catalog_do_not_read_unselected_owners(tmp_path, monkeypatch):
    owners = population()
    catalog_reads = []
    point_reads = []
    catalog = storage.Snapshot.catalog
    owner = storage.Snapshot.owner

    def read_catalog(self, **kwargs):
        page = catalog(self, **kwargs)
        catalog_reads.append((kwargs, [row['owner'] for row in page.rows]))
        return page

    def read_owner(self, home, sid, **kwargs):
        point_reads.append((home, sid))
        return owner(self, home, sid, **kwargs)

    monkeypatch.setattr(storage.Snapshot, 'catalog', read_catalog)
    monkeypatch.setattr(storage.Snapshot, 'owner', read_owner)
    with storage.PreparedStore(tmp_path / 'selected.sqlite') as store:
        token = publish(store, 1, owners)
        engine = PreparedAnalysisEngine(store.path, token)
        try:
            assert not catalog_reads and not point_reads
            key = owners[1][0]
            assert engine.sessions[key]['prepared']['title'] == owners[1][1]['title']
            assert point_reads == [key] and not catalog_reads
            assert engine.snapshot is None and not engine.reader.db.in_transaction
            selected = list(engine._selected(dict(home=key[0], sid=key[1])))
            assert [identity for identity, state in selected] == [key]
            assert all(args.get('home') == key[0] and args.get('sid') == key[1]
                       for args, rows in catalog_reads)
            assert [identity for args, rows in catalog_reads for identity in rows] == [key]
            assert engine.snapshot is None and not engine.reader.db.in_transaction
        finally:
            engine.close()


def test_generation_changes_validate_cached_owner_and_refresh_evicted_metadata(tmp_path, monkeypatch):
    owners = population()
    monkeypatch.setattr(PreparedAnalysisEngine, 'OWNER_BYTES', BUDGET)
    with storage.PreparedStore(tmp_path / 'generations.sqlite') as store:
        initial = publish(store, 1, owners)
        engine = PreparedAnalysisEngine(store.path, initial)
        try:
            key, metadata = owners[1]
            assert engine.sessions[key]['prepared']['title'] == metadata['title']
            updated = dict(metadata, title='Updated owner')
            current = publish(store, 2, [(key, updated)])
            with pytest.raises(storage.StaleGeneration):
                engine.sessions[key]
            assert engine.snapshot is None and not engine.reader.db.in_transaction
            engine.adopt(current, (key,))
            assert engine.sessions[key]['prepared']['title'] == 'Updated owner'
            assert list(engine.sessions) == [identity for identity, row in owners if not row['_internal_review']]
            assert engine.sessions[key]['prepared']['title'] == 'Updated owner'
            assert engine.snapshot is None and not engine.reader.db.in_transaction
        finally:
            engine.close()


def test_adoption_without_delta_evidence_refreshes_metadata_and_reorders(tmp_path):
    owners = population(8)
    with storage.PreparedStore(tmp_path / 'unreported.sqlite') as store:
        token = publish(store, 1, owners, calls=True)
        engine = PreparedAnalysisEngine(store.path, token)
        try:
            original = engine.query(query())
            key, metadata = owners[1]
            updated = dict(metadata, title='Unreported metadata update')
            publish(store, 2, [(key, updated)])
            current = publish(store, 3, [])
            engine.adopt(current, ())
            assert engine.sessions[key]['prepared']['title'] == updated['title']
            assert engine.query(query()) is not original
            order = [identity for identity, row in reversed(owners)]
            current = publish(store, 4, [], order=order)
            engine.adopt(current, ())
            visible = [identity for identity, row in reversed(owners) if not row['_internal_review']]
            assert list(engine.sessions) == visible
            assert [row['id'] for row in engine.query(query())['analysis']['sessions']] == [identity[1] for identity in visible]
            assert engine.snapshot is None and not engine.reader.db.in_transaction
        finally:
            engine.close()


def test_adoption_validates_changed_owner_before_committing_generation(tmp_path):
    owners = population(3)
    with storage.PreparedStore(tmp_path / 'failed-adoption.sqlite') as store:
        initial = publish(store, 1, owners)
        engine = PreparedAnalysisEngine(store.path, initial)
        try:
            sessions = engine.sessions
            key, metadata = owners[1]
            assert engine.sessions[key]['prepared']['title'] == metadata['title']
            malformed = {name: value for name, value in metadata.items() if name != '_internal_review'}
            current = publish(store, 2, [(key, malformed)])
            with pytest.raises(storage.NotReady, match='internal-review classification'):
                engine.adopt(current, (key,))
            assert engine.token == initial and engine.sessions is sessions
            assert engine.snapshot is None and not engine.reader.db.in_transaction
        finally:
            engine.close()


@pytest.mark.extended
def test_real_default_owner_inventory_overflow_keeps_lazy_exact_reads(tmp_path, monkeypatch):
    retired_limit = 64 * 1024 * 1024
    assert PreparedAnalysisEngine.OWNER_BYTES == retired_limit
    limits = storage.Limits()

    def owner_weight(row, generation):
        state, internal = PreparedAnalysisEngine._owner_state(
            row['owner'], row['metadata'], row['call_metadata'], generation,
            row['owner_ordinal'], row['request_maps'])
        weight = retained_bytes((row['owner'], state), retired_limit)
        assert not internal and weight < retired_limit
        return weight

    owner_count = retired_weight = cohort = 0
    with storage.PreparedStore(tmp_path / 'default-owner-inventory.sqlite') as store:
        while retired_weight <= retired_limit:
            cohort += 1
            source = ('owner-inventory-64mib', cohort)
            home = f'cohort-{cohort - 1:03}'
            stage = store.begin(source)
            dependency = dict.fromkeys(storage.DEPENDENCIES, 1)
            dependency['source_digest'] = f'{cohort:064x}'
            for number in range(owner_count, owner_count + limits.owners_per_batch):
                key = (home, f'owner-{number:06}')
                metadata = dict(home=home, id=key[1], title=f'Owner {number}',
                                _internal_review=False, source='cli', project='stress')
                metadata.update((f'detail_{field:02}', f'{number:06}:{field:02}:' + 'x' * 900)
                                for field in range(48))
                stage.owner(*key, metadata, dependency, source_ordinal=number)
            token = stage.publish(source, ready=True)
            with storage.PreparedReader(store.path) as reader, reader.pin(token) as view:
                for row in view.pages(view.catalog, query=True, home=home):
                    retired_weight += owner_weight(row, token.generation)
                    owner_count += 1
        with storage.PreparedReader(store.path) as reader, reader.pin(token) as view:
            retired_weight = sum(owner_weight(row, token.generation)
                                 for row in view.pages(view.catalog, query=True))
        assert retired_weight > retired_limit

        selected = ('cohort-000', 'owner-000000')
        catalog_reads = point_reads = 0
        catalog = storage.Snapshot.catalog
        owner = storage.Snapshot.owner

        def selected_catalog(view, **kwargs):
            nonlocal catalog_reads
            assert kwargs.get('home') == selected[0] and kwargs.get('sid') == selected[1]
            page = catalog(view, **kwargs)
            assert all(row['owner'] == selected for row in page.rows)
            catalog_reads += len(page.rows)
            return page

        def selected_owner(view, home, sid, **kwargs):
            nonlocal point_reads
            assert (home, sid) == selected
            point_reads += 1
            return owner(view, home, sid, **kwargs)

        with monkeypatch.context() as reads:
            reads.setattr(storage.Snapshot, 'catalog', selected_catalog)
            reads.setattr(storage.Snapshot, 'owner', selected_owner)
            engine = PreparedAnalysisEngine(store.path, token)
            try:
                assert catalog_reads == point_reads == 0
                assert engine.snapshot is None and not engine.reader.db.in_transaction
                assert engine.sessions[selected]['prepared']['title'] == 'Owner 0'
                assert point_reads == 1 and catalog_reads == 0
                assert engine.snapshot is None and not engine.reader.db.in_transaction
                ranges = list(engine.call_ranges(query(home=selected[0], sid=selected[1])))
                assert len(ranges) == 1 and ranges[0][0]['owner'] == selected
                assert ranges[0][1:] == (0, 1000)
                assert catalog_reads == 1 and point_reads == 1
                assert engine.snapshot is None and not engine.reader.db.in_transaction
                reads.undo()
                traversed = 0
                for key, state in engine.sessions.items():
                    expected = (f'cohort-{traversed // limits.owners_per_batch:03}', f'owner-{traversed:06}')
                    assert key == expected and state['owner_ordinal'] == traversed
                    assert state['prepared']['title'] == f'Owner {traversed}'
                    assert engine._query_owners.bytes <= retired_limit
                    traversed += 1
                assert traversed == owner_count and retired_weight > retired_limit
                assert len(engine._query_owners.cache) < owner_count
                assert selected not in engine._query_owners.cache
                assert engine.snapshot is None and not engine.reader.db.in_transaction
                assert engine.sessions[selected]['prepared']['title'] == 'Owner 0'
                assert engine._query_owners.bytes <= retired_limit
                assert engine.snapshot is None and not engine.reader.db.in_transaction
                print(f'owner_inventory_experiment source={source!r} owners={owner_count} '
                      f'retired_owner_weight_bytes={retired_weight} retired_owner_limit_bytes={retired_limit} '
                      f'optional_cache_weight_bytes={engine._query_owners.bytes} rss=not_measured')
            finally:
                engine.close()
