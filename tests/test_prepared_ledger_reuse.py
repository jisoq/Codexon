"""Exact prepared owner reuse never substitutes for mutable quota progress."""
from contextlib import closing, contextmanager
from threading import Event

import pytest

from cachemonitor import prepared_ledger as delivery
from cachemonitor import prepared_store as storage
from cachemonitor import quota_prepared_sync as progress
from cachemonitor.quota_cycles import QuotaLedger


def _publish(store, number, changes, *, removed=(), order=None, malformed=False):
    stage = store.begin(('source', number))
    for ordinal, (sid, revision) in enumerate(changes.items()):
        owner = ('home', sid)
        dependencies = {name: 1 for name in storage.DEPENDENCIES}
        dependencies.update(source_digest=f'{revision:064x}', coverage_bounds=None if malformed else (90, 100),
                            call_count=1, speed_frontier=1)
        metadata = dict(home='home', id=sid, title=f'{sid}-{revision}', _internal_review=False,
                        custom={'values': [revision]})
        stage.owner(*owner, metadata, dependencies, source_ordinal=ordinal,
                    request_maps=('turn_records',))
        stage.calls(owner, [dict(key=sid+'-call', ts=100, session_ordinal=1, turn='turn',
            model='gpt-6-astra', service_tier='Standard', input=100, cached=80, written=0,
            output=20, reasoning=5, cost=.1*revision)], reconcile=True)
        stage.requests(owner, [('turn', 0, dict(started_at=90, ended_at=110, state='완료'))], reconcile=True)
    for sid in removed:
        owner=('home',sid)
        with store.read_current() as view:
            previous=view.owner(*owner)
        stage.owner(*owner,previous['metadata'],previous['dependencies'],
                    source_ordinal=previous['owner_ordinal'],request_maps=previous['request_maps'])
        stage.remove_owner(owner)
    if order is not None:
        stage.catalog_order((('home', sid), ordinal) for ordinal, sid in enumerate(order))
    return stage.publish(('source', number), ready=True)


def _envelope(at=200):
    return dict(homes=['home'], ts=at, errors=[], usage_errors=[],
                index=dict(loading=False, usage_complete=True, usage_complete_at=at),
                usage_collection_complete=True)


def _forbidden(*args, **kwargs):
    raise AssertionError('unchanged prepared source content must be reused')


def test_same_token_reuses_decodes_hashes_and_weights_with_independent_envelopes(tmp_path, monkeypatch):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1, 'second': 1})
        adapter = delivery.PreparedLedger(ledger, store)
        first, certificates = adapter._sources()
        first[('home', 'first')]['custom']['values'].append(999)
        first[('home', 'first')]['request_revision'] = 'caller-only'
        with monkeypatch.context() as hit:
            hit.setattr(storage.Snapshot, 'ledger_source', _forbidden)
            hit.setattr(storage.Snapshot, 'owner_revisions', _forbidden)
            hit.setattr(progress, 'source_stamp', _forbidden)
            hit.setattr(delivery, '_weight', _forbidden)
            second, current = adapter._sources()
        assert current == certificates
        assert second[('home', 'first')]['custom']['values'] == [1]
        assert 'request_revision' not in second[('home', 'first')]
        second[('home', 'second')]['custom']['values'].append(888)
        third, _ = adapter._sources()
        assert third[('home', 'second')]['custom']['values'] == [1]
        assert adapter.metrics['metadata_reads'] == 2


def test_new_generation_reads_only_changed_versions_and_handles_order_and_removal(tmp_path, monkeypatch):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1, 'second': 1, 'third': 1})
        adapter = delivery.PreparedLedger(ledger, store)
        first, certificates = adapter._sources()
        _publish(store, 2, {'second': 2}, removed=['third'], order=['second', 'first'])
        reads=[]
        original=storage.Snapshot.ledger_source
        def observed(view, home, sid):
            reads.append((home,sid))
            return original(view,home,sid)
        monkeypatch.setattr(storage.Snapshot,'ledger_source',observed)
        updated, current = adapter._sources()
        assert reads == [('home','second')]
        assert list(updated) == [('home','second'),('home','first')]
        assert current[('home','first')] == certificates[('home','first')]
        assert current[('home','second')] != certificates[('home','second')]
        assert first[('home','second')]['title'] == 'second-1'
        assert updated[('home','second')]['title'] == 'second-2'


def test_same_token_policies_certificates_and_freshness_still_change(tmp_path, monkeypatch):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        initial_reads=adapter.metrics['call_rows']
        token=store.token
        with monkeypatch.context() as hit:
            hit.setattr(storage.Snapshot,'ledger_source',_forbidden)
            hit.setattr(progress,'source_stamp',_forbidden)
            # Policy activation must replay stored request maps, although no
            # prepared source content or canonical calls changed.
            ledger.db.execute('INSERT INTO tracking_config VALUES(?,?)',('home',50))
            ledger.db.execute('INSERT INTO tracking_controls VALUES(?,?,?)',('home',50,1))
            ledger.db.commit()
            list(adapter.sync(_envelope(201)))
            assert adapter.metrics['call_rows'] == initial_reads
            assert tuple(ledger.db.execute('SELECT start,end FROM tracking_tasks').fetchone()) == (90,110)
            # The invalidation trigger removes the durable source certificate.
            # A prepared-cache hit cannot conceal this mutable ledger change.
            ledger.db.execute('UPDATE calls SET cost=99 WHERE home=? AND sid=?',('home','first'))
            ledger.db.commit()
            assert progress.applied(ledger.db,[('home','first')]) == {}
            list(adapter.sync(_envelope(202)))
        assert store.token == token
        assert adapter.metrics['call_rows'] == initial_reads+1
        assert ledger.db.execute('SELECT cost FROM calls').fetchone()[0] == .1
        assert tuple(ledger.db.execute('SELECT at,complete_at FROM state').fetchone()) == (202,202)


def test_source_plan_token_guards_selected_calls_and_checkpoints(tmp_path):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        previous=_publish(store, 1, {'first': 1})
        adapter=delivery.PreparedLedger(ledger,store)
        sources,_=adapter._sources()
        _publish(store,2,{'first':2})
        with pytest.raises(storage.StaleGeneration):
            adapter._owner(('home','first'),delivery.FACT_BYTES,expected=previous)
        with pytest.raises(storage.StaleGeneration):
            adapter._append(('home','first'),sources[('home','first')],None,None,
                            delivery.FACT_BYTES,expected=previous)
        assert progress.applied(ledger.db,[('home','first')]) == {}


def test_generation_change_after_planning_leaves_no_partial_application(tmp_path, monkeypatch):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store,1,{'first':1})
        adapter=delivery.PreparedLedger(ledger,store)
        original=adapter._sources
        def changed():
            result=original()
            _publish(store,2,{'first':2})
            return result
        monkeypatch.setattr(adapter,'_sources',changed)
        with pytest.raises(storage.StaleGeneration):
            list(adapter.sync(_envelope()))
        assert progress.applied(ledger.db,[('home','first')]) == {}
        assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0] == 0


def test_capacity_fallback_and_cancel_preserve_application_and_reusable_content(tmp_path, monkeypatch):
    stopped=Event()
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store,1,{'first':1})
        adapter=delivery.PreparedLedger(ledger,store,cancel=stopped.is_set)
        adapter._sources()
        cache,token=adapter._source_cache,adapter._source_token
        with monkeypatch.context() as small:
            small.setattr(delivery,'METADATA_BYTES',1)
            with pytest.raises(storage.CapacityExceeded):adapter._sources()
        assert adapter._source_cache == {} and adapter._source_token is None
        assert progress.applied(ledger.db,[('home','first')]) == {}
        adapter._sources()
        cache,token=adapter._source_cache,adapter._source_token
        stopped.set()
        with pytest.raises(storage.ReadCancelled):adapter._sources()
        assert adapter._source_cache is cache and adapter._source_token == token
        stopped.clear()
        assert adapter._sources()[0][('home','first')]['title'] == 'first-1'


def test_revision_page_overlap_evicts_optional_cache_before_uncached_rebuild(tmp_path, monkeypatch):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        names=[f'owner-{number}' for number in range(16)]
        _publish(store,1,dict.fromkeys(names,1))
        adapter=delivery.PreparedLedger(ledger,store)
        adapter._sources()
        # Find this fixture's observable cache-hit admission boundary without
        # coupling the test to Python object sizes or the accounting formula.
        low,high=0,delivery.METADATA_BYTES
        while high-low>1:
            middle=(low+high)//2
            monkeypatch.setattr(delivery,'METADATA_BYTES',middle)
            try:adapter._cached_sources()
            except storage.CapacityExceeded:low=middle
            else:high=middle
        monkeypatch.setattr(delivery,'METADATA_BYTES',high)
        adapter._sources()
        cache=adapter._source_cache
        # This publication changes only order. Its owner bodies all remain
        # reusable, but its decoded revision page still overlaps retained data.
        token=_publish(store,2,{},order=list(reversed(names)))
        sources,_=adapter._sources()
        assert list(sources) == [('home',sid) for sid in reversed(names)]
        assert cache and adapter._source_cache == {} and adapter._source_token == token
        assert adapter.metrics['source_cache_fallbacks'] == 1


def test_inventory_fitting_without_retained_copy_still_applies_mutable_policy(tmp_path, monkeypatch):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        names=[f'owner-{number}' for number in range(16)]
        token=_publish(store,1,dict.fromkeys(names,1))
        adapter=delivery.PreparedLedger(ledger,store)
        adapter._sources()
        # The allowance fits one complete inventory, but cannot retain the
        # cached templates alongside independently mutable envelopes and pages.
        monkeypatch.setattr(delivery,'METADATA_BYTES',2*adapter._source_cache_bytes)
        ledger.db.execute('INSERT INTO tracking_config VALUES(?,?)',('home',50))
        ledger.db.execute('INSERT INTO tracking_controls VALUES(?,?,?)',('home',50,1))
        ledger.db.commit()
        list(adapter.sync(_envelope()))
        assert adapter.metrics['source_cache_fallbacks'] >= 1
        assert adapter._source_cache == {} and adapter._cache_token is None
        assert adapter._source_token == token
        assert ledger.db.execute('SELECT count(*) FROM tracking_tasks').fetchone()[0] == len(names)
        assert len(progress.applied(ledger.db,[('home',sid) for sid in names])) == len(names)
        call_rows=adapter.metrics['call_rows']
        ledger.db.execute('INSERT INTO tracking_controls VALUES(?,?,?)',('home',201,0))
        ledger.db.commit()
        list(adapter.sync(_envelope(202)))
        ledger.db.execute('INSERT INTO tracking_controls VALUES(?,?,?)',('home',203,1))
        ledger.db.commit()
        list(adapter.sync(_envelope(204)))
        assert adapter.metrics['call_rows'] == call_rows
        assert adapter.metrics['request_rows'] == 2*len(names)
        assert tuple(ledger.db.execute('SELECT at,complete_at FROM state').fetchone()) == (204,204)


def test_staged_and_malformed_sources_cannot_replace_admitted_cache(tmp_path, monkeypatch):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store,1,{'first':1})
        adapter=delivery.PreparedLedger(ledger,store)
        adapter._sources()
        cache,token=adapter._source_cache,adapter._source_token
        stage=store.begin(('source','staged'))
        original=store.read_current
        @contextmanager
        def staged(**kwargs):
            with original(staged=True,**kwargs) as view:
                yield view
        with monkeypatch.context() as staging:
            staging.setattr(store,'read_current',staged)
            with pytest.raises(storage.NotReady):adapter._sources()
        stage.abort()
        assert adapter._source_cache is cache and adapter._source_token == token
        _publish(store,2,{'first':2},malformed=True)
        with pytest.raises(ValueError,match='coverage bounds'):adapter._sources()
        assert adapter._source_cache is cache and adapter._source_token == token


def test_different_store_instance_never_reuses_equal_batch_number(tmp_path):
    with (closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger,
          storage.PreparedStore(tmp_path/'one.sqlite') as first,
          storage.PreparedStore(tmp_path/'two.sqlite') as second):
        _publish(first,1,{'first':1})
        _publish(second,1,{'first':2})
        adapter=delivery.PreparedLedger(ledger,first)
        adapter._sources()
        adapter.store=second
        sources,_=adapter._sources()
        assert sources[('home','first')]['title'] == 'first-2'
        assert adapter.metrics['metadata_reads'] == 2
