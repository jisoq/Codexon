"""Selected financial reuse stays exact, bounded and private to its actor."""
from copy import deepcopy
from dataclasses import replace
from threading import Event

import pytest

from cachemonitor import prepared_store as storage
from cachemonitor.prepared_ingest import prepare_owner, stage_owner
from cachemonitor.prepared_overlays import FinanceCache, PreparedOverlays, hydrate_overlay
from cachemonitor.prepared_query import PreparedQuery, tuple_token


def family():
    from test_data_contract import source
    sources = {name: source() for name in ('parent', 'child', 'other')}
    for number, (name, value) in enumerate(sources.items()):
        value.update(id=name, title=name)
        value['history'][0].update(key=name, call_id=name, ts=100+number)
    sources['child']['parent_thread_id'] = 'parent'
    return sources


def publish(store, sources, revision, names=None):
    stage = store.begin(('source', revision))
    for ordinal, (name, value) in enumerate(sources.items()):
        if names is None or name in names:
            dependency = {name: 1 for name in storage.DEPENDENCIES}
            dependency['source_digest'] = f'{revision:064x}'
            stage_owner(stage, prepare_owner(deepcopy(value), owns=lambda *_: True), dependency, ordinal)
    return stage.publish(('source', revision), ready=True)


def summary(store):
    with store.read_current() as view:
        owners = {(row['metadata']['home'], row['metadata']['id']): row
                  for row in _pages(view.catalog)}
        return next(row for row in PreparedOverlays().collect(view, owners, None, (), {})
                    if row['id'] == 'parent')


def _pages(method):
    after = None
    while True:
        page = method(after=after)
        yield from page.rows
        if page.complete:
            return
        after = page.after


def detail(store, selected, cache=None):
    with storage.PreparedReader(store.path) as reader, reader.pin(store.token) as view:
        return hydrate_overlay(view, selected, finance=True, finance_cache=cache)


def without_finance(value, token):
    result = dict(value, _prepared_token=tuple_token(token))
    result.pop('subagent_finance', None)
    return result


def test_actor_reuses_unchanged_member_versions_and_never_reuses_stale_pins(tmp_path, monkeypatch):
    sources = family()
    with storage.PreparedStore(tmp_path/'finance.sqlite') as store:
        first_token = publish(store, sources, 1)
        actor = PreparedQuery(store.path)
        first = summary(store)
        message = dict(kind='overlay_detail', id='finance', summary=first, finance=True)
        initial = actor(message, first_token, lambda: False)
        assert initial['summary'] == detail(store, first)
        reads = []
        original = storage.Snapshot.financial_member
        def member(view, home, sid):
            reads.append((home, sid))
            return original(view, home, sid)
        monkeypatch.setattr(storage.Snapshot, 'financial_member', member)
        assert actor(message, first_token, lambda: False) == initial
        assert reads == []
        # Advancing an unrelated owner changes the requested pin, but neither
        # selected public version needs decoding or validation again.
        sources['other']['history'][0]['output'] += 100
        current = publish(store, sources, 2, ['other'])
        selected = summary(store)
        second = actor(dict(message, summary=selected), current, lambda: False)
        assert second['summary'] == detail(store, selected)
        assert reads == []
        assert second['prepared_token'] == tuple_token(current)
        assert actor(message, first_token, lambda: False)['kind'] == 'stale'
        actor.close()
        assert not actor.finance_cache.entries


def test_changed_member_reloads_and_returned_nested_values_cannot_change_cache(tmp_path, monkeypatch):
    sources = family(); cache = FinanceCache()
    with storage.PreparedStore(tmp_path/'finance.sqlite') as store:
        publish(store, sources, 1)
        selected = summary(store)
        original = detail(store, selected, cache)
        original['subagent_finance']['agents'][0]['token_composition']['counts']['input'] = -100
        original['subagent_finance']['agents'][0]['model_breakdown'].clear()
        assert detail(store, selected, cache) == detail(store, selected)
        reads = []; method = storage.Snapshot.financial_member
        def member(view, home, sid):
            reads.append((home, sid))
            return method(view, home, sid)
        monkeypatch.setattr(storage.Snapshot, 'financial_member', member)
        sources['child']['history'][0]['output'] += 100
        publish(store, sources, 2, ['child'])
        selected = summary(store)
        assert detail(store, selected, cache) == detail(store, selected)
        assert reads == [('home', 'child')]


@pytest.mark.parametrize('change', ['reparent', 'internal', 'remove', 'malformed'])
def test_current_selected_membership_and_packets_override_cached_members(tmp_path, change):
    sources = family(); cache = FinanceCache()
    with storage.PreparedStore(tmp_path/'finance.sqlite') as store:
        publish(store, sources, 1)
        previous = detail(store, summary(store), cache)
        with store.read_current() as view:
            child = view.owner('home', 'child')
            packet = view.derived('home', 'child', 'summary')
            rows = list(view.calls(('home', 'child')).rows)
        stage = store.begin(('source', 2))
        metadata = dict(child['metadata'])
        if change == 'reparent': metadata['parent_thread_id'] = 'other'
        if change == 'internal': metadata['_internal_review'] = True
        if change == 'malformed': packet = dict(packet, owner=('home', 'wrong'))
        stage.owner('home', 'child', metadata, child['dependencies'], source_ordinal=child['owner_ordinal'],
                    summary=packet)
        if change == 'remove':
            stage.remove_owner(('home', 'child'))
        else:
            stage.calls(('home', 'child'), rows, reconcile=True)
        token = stage.publish(('source', 2), ready=True)
        selected = without_finance(previous, token)
        expected = storage.NotReady if change == 'remove' else storage.RebuildRequired
        with pytest.raises(expected): detail(store, selected, cache)


def test_finance_cache_does_not_cross_database_instances(tmp_path):
    sources = family(); cache = FinanceCache()
    for number in (1, 2):
        with storage.PreparedStore(tmp_path/f'finance-{number}.sqlite') as store:
            sources['child']['history'][0]['output'] += number * 100
            publish(store, sources, 1)
            selected = summary(store)
            assert detail(store, selected, cache) == detail(store, selected)
            assert cache.instance == store.token.instance
            assert all(key[1][0] == store.token.instance for key in cache.entries)


def test_selected_header_pages_and_eviction_keep_current_family_aggregation(tmp_path):
    sources = family(); sources['other']['parent_thread_id'] = 'parent'
    cache = FinanceCache()
    with storage.PreparedStore(tmp_path/'finance.sqlite') as store:
        publish(store, sources, 1)
        selected = summary(store)
        expected = detail(store, selected)
        with (storage.PreparedReader(store.path, replace(storage.Limits(), page_rows=1)) as reader,
              reader.pin(store.token) as view):
            assert hydrate_overlay(view, selected, finance=True, finance_cache=cache) == expected
        # A complete financial result is built anew from the displayed family,
        # even when every stored member version remains unchanged.
        changed = dict(selected, child_cost=123.456, members=(selected['members'][0],
            *reversed(selected['members'][1:])))
        assert detail(store, changed, cache) == detail(store, changed)
        cache.MAX_ENTRIES = 1
        actual = detail(store, selected, cache)
        assert actual == expected and len(cache.entries) <= 1
        assert cache.bytes + storage._size(actual) <= storage.Limits().page_objects_bytes


def test_cache_only_finance_checks_cancellation_and_capacity(tmp_path, monkeypatch):
    sources = family(); cache = FinanceCache()
    with storage.PreparedStore(tmp_path/'finance.sqlite') as store:
        publish(store, sources, 1)
        selected = summary(store)
        complete = detail(store, selected, cache)
        selected = without_finance(complete, store.token)
        cancelled = Event()
        original = storage.Snapshot.owner_revisions
        def revisions(view, *args, **kwargs):
            page = original(view, *args, **kwargs)
            cancelled.set()
            return page
        with storage.PreparedReader(store.path) as reader, reader.pin(store.token, cancel=cancelled) as view:
            with monkeypatch.context() as patch:
                patch.setattr(storage.Snapshot, 'owner_revisions', revisions)
                with pytest.raises(storage.ReadCancelled):
                    hydrate_overlay(view, selected, finance=True, finance_cache=cache)
        cache.MAX_BYTES = 1
        assert detail(store, selected, cache) == detail(store, selected)
        assert not cache.entries
        with storage.PreparedReader(store.path) as reader, reader.pin(store.token) as view:
            view.limits = replace(view.limits, page_objects_bytes=128)
            with pytest.raises(storage.CapacityExceeded):
                hydrate_overlay(view, selected, finance=True, finance_cache=cache)


def test_staged_finance_uses_authoritative_reads_without_persistent_cache(tmp_path, monkeypatch):
    sources = family(); cache = FinanceCache()
    with storage.PreparedStore(tmp_path/'finance.sqlite') as store:
        publish(store, sources, 1)
        previous = detail(store, summary(store), cache)
        stage = store.begin(('source', 2))
        def forbidden(*args, **kwargs):
            raise AssertionError('staged packets cannot use committed finance reuse')
        monkeypatch.setattr(cache, 'collect', forbidden)
        with store.read_current(staged=True) as view:
            selected = without_finance(previous, view.publication_token)
            actual = hydrate_overlay(view, selected, finance=True, finance_cache=cache)
            assert actual['subagent_finance'] == previous['subagent_finance']
        stage.abort()


def test_complete_reply_and_cache_metadata_share_the_object_budget(tmp_path):
    sources = family(); cache = FinanceCache()
    with storage.PreparedStore(tmp_path/'finance.sqlite') as store:
        publish(store, sources, 1)
        complete = detail(store, summary(store), cache)
        with storage.PreparedReader(store.path) as reader, reader.pin(store.token) as view:
            view.limits = replace(view.limits, page_objects_bytes=storage._size(complete))
            assert hydrate_overlay(view, complete, finance=True) == complete
            # Even an empty cache retains its object and LRU metadata. A reply
            # that fills the complete allowance cannot silently add that state.
            with pytest.raises(storage.CapacityExceeded):
                hydrate_overlay(view, complete, finance=True, finance_cache=cache)
            assert not cache.entries


def test_existing_recent_reply_reserves_space_during_financial_projection(tmp_path):
    sources = family(); cache = FinanceCache()
    with storage.PreparedStore(tmp_path/'finance.sqlite') as store:
        publish(store, sources, 1)
        complete = detail(store, summary(store), cache)
        selected = without_finance(complete, store.token)
        selected['_padding'] = 'x' * (4 * storage._size(complete['subagent_finance']))
        expected = detail(store, selected)
        # The final reply fits, including empty-cache metadata. The intermediate
        # financial projections also coexist with the already built recent view.
        limit = storage._size(expected) + FinanceCache().bytes + 512
        with storage.PreparedReader(store.path) as reader, reader.pin(store.token) as view:
            view.limits = replace(view.limits, page_objects_bytes=limit)
            assert hydrate_overlay(view, selected, finance=True) == expected
            with pytest.raises(storage.CapacityExceeded):
                hydrate_overlay(view, selected, finance=True, finance_cache=cache)
