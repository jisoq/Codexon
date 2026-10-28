"""Exact prepared owner reuse never substitutes for mutable quota progress."""
from contextlib import closing, contextmanager
from threading import Event

import pytest

from cachemonitor import prepared_ledger as delivery
from cachemonitor import prepared_store as storage
from cachemonitor import quota_prepared_sync as progress
from cachemonitor.quota_cycles import QuotaLedger


def _publish(store, number, changes, *, removed=(), order=None, malformed=False, source_metadata=None):
    stage = store.begin(('source', number))
    for ordinal, (sid, revision) in enumerate(changes.items()):
        owner = ('home', sid)
        dependencies = {name: 1 for name in storage.DEPENDENCIES}
        dependencies.update(source_digest=f'{revision:064x}', coverage_bounds=None if malformed else (90, 100),
                            call_count=1, speed_frontier=1)
        metadata = dict(home='home', id=sid, title=f'{sid}-{revision}', _internal_review=False,
                        custom={'values': [revision]})
        metadata.update((source_metadata or {}).get(sid, {}))
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



def test_certificate_population_does_not_refuse_authoritative_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, 'MAX_BYTES', 1024, raising=False)
    owners = [('home', 'first'), ('home', 'second')]
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1, 'second': 2})
        adapter = delivery.PreparedLedger(ledger, store)
        _, expected = adapter._sources()
        updates = list(adapter.sync(_envelope()))
        assert {owner for update in updates for owner in update['owners']} == set(owners)
        assert progress.applied(ledger.db, owners) == expected
        assert [tuple(row) for row in ledger.db.execute('SELECT sid,cost FROM calls ORDER BY sid')] == [
            ('first', .1), ('second', .2)]
        assert [tuple(row) for row in ledger.db.execute(
            'SELECT uid,started FROM tracking_call_boundaries ORDER BY uid')] == [
            ('first-call', 90), ('second-call', 90)]
        assert list(adapter.sync(_envelope(201)))[0]['owners'] == ()
        assert progress.applied(ledger.db, owners) == expected


def test_legacy_certificate_budget_is_removed_without_losing_valid_progress(tmp_path):
    owners = [('home', 'first'), ('home', 'second')]
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        committed = progress.applied(ledger.db, owners)
        ledger.db.execute('CREATE TABLE IF NOT EXISTS prepared_analysis_budget(id INTEGER PRIMARY KEY,bytes INTEGER NOT NULL)')
        ledger.db.execute('INSERT OR REPLACE INTO prepared_analysis_budget VALUES(1,548)')
        for name in ('prepared_applied_admit', 'prepared_applied_insert', 'prepared_applied_delete'):
            ledger.db.execute(f'DROP TRIGGER IF EXISTS {name}')
        ledger.db.execute("CREATE TRIGGER prepared_applied_admit BEFORE INSERT ON prepared_analysis_applied BEGIN "
            "SELECT CASE WHEN (SELECT bytes FROM prepared_analysis_budget WHERE id=1)"
            "+(512+4*length(new.home)+4*length(new.sid))>1024 "
            "THEN RAISE(ABORT,'prepared ledger progress exceeds its metadata budget') END; END")
        ledger.db.execute('CREATE TRIGGER prepared_applied_insert AFTER INSERT ON prepared_analysis_applied BEGIN '
            'UPDATE prepared_analysis_budget SET bytes=bytes+(512+4*length(new.home)+4*length(new.sid)) WHERE id=1; END')
        ledger.db.execute('CREATE TRIGGER prepared_applied_delete AFTER DELETE ON prepared_analysis_applied BEGIN '
            'UPDATE prepared_analysis_budget SET bytes=bytes-(512+4*length(old.home)+4*length(old.sid)) WHERE id=1; END')
        ledger.db.commit()
        progress.initialize(ledger.db)
        progress.initialize(ledger.db)
        assert progress.applied(ledger.db, owners) == committed
        assert ledger.db.execute("SELECT name FROM sqlite_master WHERE name IN "
            "('prepared_analysis_budget','prepared_applied_admit','prepared_applied_insert','prepared_applied_delete')").fetchall() == []
        _publish(store, 2, {'second': 2})
        list(adapter.sync(_envelope(201)))
        _, expected = adapter._sources()
        assert progress.applied(ledger.db, owners) == expected
        assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0] == 2
        assert ledger.db.execute('SELECT count(*) FROM tracking_call_boundaries').fetchone()[0] == 2


def test_certificate_failure_rolls_back_candidate_facts_and_preserves_previous_owner(tmp_path, monkeypatch):
    import sqlite3

    owners = [('home', 'first'), ('home', 'second')]
    path = tmp_path/'quota.sqlite'
    with closing(QuotaLedger(path)) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        committed = progress.applied(ledger.db, owners)
        _publish(store, 2, {'second': 2})
        original = progress.save
        def fail_after_certificate(db, certificates):
            original(db, certificates)
            assert db.execute("SELECT count(*) FROM calls WHERE sid='second'").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM tracking_call_boundaries WHERE uid='second-call'").fetchone()[0] == 1
            assert progress.applied(db, [('home', 'second')]) == certificates
            raise RuntimeError('injected interruption before commit')
        with monkeypatch.context() as interrupted:
            interrupted.setattr(progress, 'save', fail_after_certificate)
            with pytest.raises(RuntimeError, match='injected interruption'):
                list(adapter.sync(_envelope(201)))
        assert ledger.db.in_transaction
        with closing(sqlite3.connect(path)) as reader:
            assert reader.execute('SELECT sid FROM calls').fetchall() == [('first',)]
            assert reader.execute('SELECT uid FROM tracking_call_boundaries').fetchall() == [('first-call',)]
            assert dict(((home, sid), stamp) for home, sid, stamp in reader.execute(
                'SELECT home,sid,signature FROM prepared_analysis_applied')) == committed
        ledger.db.rollback()
        assert progress.applied(ledger.db, owners) == committed
        assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0] == 1
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope(202)))
        _, expected = adapter._sources()
        assert progress.applied(ledger.db, owners) == expected
        assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0] == 2


@pytest.mark.parametrize('certificate', [None, '0'*64])
def test_sync_requires_positive_postcommit_certificate_confirmation(tmp_path, monkeypatch, certificate):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1})
        adapter = delivery.PreparedLedger(ledger, store)
        original = progress.save
        def lose_certificate(db, certificates):
            if certificate is not None:
                original(db, {owner: certificate for owner in certificates})
        monkeypatch.setattr(progress, 'save', lose_certificate)
        with pytest.raises(ValueError, match='unconfirmed or older recovery owners'):
            next(adapter.sync(_envelope()))
        assert not ledger.db.in_transaction
        assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0] == 1
        assert ledger.db.execute('SELECT count(*) FROM tracking_call_boundaries').fetchone()[0] == 1
        assert adapter.metrics['applied_owners'] == 0
        assert adapter.task_revisions == {}


@pytest.mark.parametrize('mutation', [
    "UPDATE calls SET cost=99 WHERE sid='first'",
    "DELETE FROM calls WHERE sid='first'",
    "INSERT OR REPLACE INTO calls SELECT home,uid,'second',ts,model,input,cached,written,output,reasoning,cost,price_id,signature,service_tier FROM calls WHERE sid='first'",
    "UPDATE tracking_call_boundaries SET started=80 WHERE uid='first-call'",
    "DELETE FROM tracking_call_boundaries WHERE uid='first-call'",
    "INSERT OR REPLACE INTO tracking_call_boundaries VALUES('home','first-call',80)",
])
def test_certificate_invalidation_survives_budget_migration(tmp_path, mutation):
    owner = ('home', 'first')
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        progress.initialize(ledger.db)
        assert owner in progress.applied(ledger.db, [owner])
        ledger.db.execute(mutation)
        ledger.db.commit()
        assert progress.applied(ledger.db, [owner]) == {}


def test_append_still_requires_exact_committed_certificate(tmp_path):
    owner = ('home', 'first')
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        committed = progress.applied(ledger.db, [owner])
        with pytest.raises(progress.AppendPredecessorChanged):
            progress.require_append(ledger.db, {owner: dict(
                format=progress.APPEND_FORMAT, count=1, prior_source='0'*64, policy=(None, None))})
        ledger.db.rollback()
        assert progress.applied(ledger.db, [owner]) == committed
        assert ledger.db.execute('SELECT cost FROM calls').fetchone()[0] == .1



@pytest.mark.parametrize('owner_count', [4, 64])
def test_source_population_fits_when_each_owner_and_application_cohort_fit(tmp_path, monkeypatch, owner_count):
    allowance = 64*1024
    changes = {f'owner-{number:03d}': number+1 for number in range(owner_count)}
    owners = [('home', sid) for sid in changes]
    metadata = {sid: dict(unclassified=number%3 == 0,
        coverage_gaps=[dict(start=95, end=96, tokens=number+10)] if number%5 == 0 else [])
        for number, sid in enumerate(changes)}
    expected_issues = sorted(
        [(sid, 90, 100) for number, sid in enumerate(changes) if number%3 == 0] +
        [(sid, 95, 96) for number, sid in enumerate(changes) if number%5 == 0])
    monkeypatch.setattr(delivery, 'OWNER_BATCH', 4)
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, changes, source_metadata=metadata)
        expected_certificates = {}
        with store.read_current() as view:
            for owner in owners:
                entry = view.ledger_source(*owner)
                assert delivery._weight(entry, allowance) < allowance//8
                expected_certificates[owner] = progress.source_stamp(entry['dependencies'])
        results = []
        for label, limit in [('control', delivery.METADATA_BYTES), ('small', allowance)]:
            with closing(QuotaLedger(tmp_path/(label+'.sqlite'))) as ledger, monkeypatch.context() as bounded:
                bounded.setattr(delivery, 'METADATA_BYTES', limit)
                adapter = delivery.PreparedLedger(ledger, store)
                for owner in owners:
                    _, weight = adapter._owner(owner, delivery.FACT_BYTES)
                    assert weight < allowance//8
                updates = list(adapter.sync(_envelope()))
                assert {owner for update in updates for owner in update['owners']} == set(owners)
                calls = [tuple(row) for row in ledger.db.execute(
                    'SELECT sid,uid,cost FROM calls ORDER BY sid')]
                assert calls == [(sid, sid+'-call', .1*revision) for sid, revision in changes.items()]
                certificates = progress.applied(ledger.db, owners)
                assert certificates == expected_certificates
                boundaries = [tuple(row) for row in ledger.db.execute(
                    'SELECT uid,started FROM tracking_call_boundaries ORDER BY uid')]
                assert boundaries == [(sid+'-call', 90) for sid in changes]
                issues = [tuple(row) for row in ledger.db.execute(
                    'SELECT sid,start,end FROM coverage_issues ORDER BY sid,start,end')]
                assert issues == expected_issues
                state = tuple(ledger.db.execute(
                    "SELECT at,complete_at,loading,unclassified FROM state WHERE home='home'").fetchone())
                assert state == (200, 200, 0, sum(number%3 == 0 for number in range(owner_count)))
                results.append((calls, certificates, boundaries, issues, state))
        assert results[0] == results[1]



@pytest.mark.parametrize('interruption', ['before', 'during', 'final'])
def test_coverage_interruption_retains_watermark_and_calls_until_exact_restart(tmp_path, monkeypatch, interruption):
    import sqlite3

    stopped = Event()
    path = tmp_path/'quota.sqlite'
    names = [f'owner-{number:03d}' for number in range(8)]
    retired = ('home', 'retained')
    monkeypatch.setattr(delivery, 'OWNER_BATCH', 4)
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        with closing(QuotaLedger(path)) as ledger:
            _publish(store, 1, {sid: 1 for sid in names[:4]+['retained']},
                     source_metadata={names[0]: dict(unclassified=True)})
            adapter = delivery.PreparedLedger(ledger, store, cancel=stopped.is_set)
            list(adapter.sync(_envelope()))
            _publish(store, 2, {sid: 2 for sid in names}, removed=['retained'],
                     source_metadata={sid: dict(unclassified=number%2 == 1)
                                      for number, sid in enumerate(names)})
            if interruption == 'during':
                ledger.db.execute("CREATE TEMP TRIGGER interrupt_coverage BEFORE INSERT ON main.coverage_issues "
                    "WHEN new.sid='owner-003' BEGIN SELECT RAISE(ABORT,'injected coverage interruption'); END")
            elif interruption == 'final':
                ledger.db.execute("CREATE TEMP TRIGGER interrupt_completion BEFORE UPDATE ON main.state "
                    "WHEN new.complete_at=201 BEGIN SELECT RAISE(ABORT,'injected completion interruption'); END")
            updates = adapter.sync(_envelope(201), removed=[retired])
            if interruption == 'before':
                applied = set()
                while applied != {('home', sid) for sid in names}:
                    applied.update(next(updates)['owners'])
                stopped.set()
                with pytest.raises(storage.ReadCancelled):
                    list(updates)
            else:
                with pytest.raises(sqlite3.IntegrityError, match='injected'):
                    list(updates)
            ledger.db.rollback()
            with closing(sqlite3.connect(path)) as reader:
                state = reader.execute("SELECT complete_at,loading,unclassified FROM state WHERE home='home'").fetchone()
                assert state == (200, int(interruption != 'before'), 1)
                assert reader.execute('SELECT sid,cost FROM calls ORDER BY sid').fetchall() == [
                    *[(sid, .2) for sid in names], ('retained', .1)]
                assert reader.execute("SELECT uid FROM tracking_call_boundaries WHERE uid='retained-call'").fetchone()
        stopped.clear()
        with closing(QuotaLedger(path)) as ledger:
            adapter = delivery.PreparedLedger(ledger, store, cancel=stopped.is_set)
            list(adapter.sync(_envelope(201), removed=[retired]))
            assert tuple(ledger.db.execute(
                "SELECT complete_at,loading,unclassified FROM state WHERE home='home'").fetchone()) == (201, 0, 4)
            assert [tuple(row) for row in ledger.db.execute('SELECT sid,start,end FROM coverage_issues ORDER BY sid')] == [
                (sid, 90, 100) for number, sid in enumerate(names) if number%2 == 1]
            assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0] == 9
            assert ledger.db.execute("SELECT cost FROM calls WHERE sid='retained'").fetchone()[0] == .1
            assert progress.applied(ledger.db, [retired]) == {}
            with store.read_current() as view:
                expected = {('home', sid): progress.source_stamp(view.owner('home', sid)['dependencies']) for sid in names}
            assert progress.applied(ledger.db, expected) == expected


def test_pending_owner_retains_issues_and_calls_during_complete_coverage_reconciliation(tmp_path):
    owner = ('home', 'pending')
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'pending': 1, 'ready': 1}, source_metadata={'pending': dict(unclassified=True)})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        previous = progress.applied(ledger.db, [owner])
        _publish(store, 2, {'pending': 2, 'ready': 2}, source_metadata={
            'pending': dict(_collection_pending=True, unclassified=False),
            'ready': dict(coverage_gaps=[dict(start=95, end=96, tokens=10)])})
        list(adapter.sync(_envelope(201)))
        assert progress.applied(ledger.db, [owner]) == previous
        assert ledger.db.execute("SELECT cost FROM calls WHERE sid='pending'").fetchone()[0] == .1
        assert ledger.db.execute("SELECT cost FROM calls WHERE sid='ready'").fetchone()[0] == .2
        assert [tuple(row) for row in ledger.db.execute('SELECT sid,start,end FROM coverage_issues ORDER BY sid')] == [
            ('pending', 90, 100), ('ready', 95, 96)]
        assert tuple(ledger.db.execute(
            "SELECT complete_at,loading,unclassified FROM state WHERE home='home'").fetchone()) == (201, 0, 1)


def test_older_recovery_heartbeat_cannot_reconcile_newer_coverage(tmp_path):
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        _publish(store, 1, {'first': 1}, source_metadata={'first': dict(unclassified=True)})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        _publish(store, 2, {'first': 1}, source_metadata={'first': dict(unclassified=False)})
        before = [tuple(row) for row in ledger.db.execute('SELECT * FROM coverage_issues')]
        list(adapter.sync(dict(_envelope(199), collection_recovery=True)))
        assert [tuple(row) for row in ledger.db.execute('SELECT * FROM coverage_issues')] == before
        assert tuple(ledger.db.execute(
            "SELECT at,complete_at,loading,unclassified FROM state WHERE home='home'").fetchone()) == (200, 200, 0, 1)



def test_fresh_recovery_continues_owner_cohorts_after_its_own_tracking_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, 'OWNER_BATCH', 4)
    names = [f'owner-{number:03d}' for number in range(12)]
    with closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        ledger.db.execute("INSERT INTO tracking_config VALUES('home',50)")
        ledger.db.execute("INSERT INTO tracking_controls VALUES('home',50,1)")
        ledger.db.commit()
        _publish(store, 1, {sid: 1 for sid in names})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        _publish(store, 2, {sid: 2 for sid in names})
        updates = list(adapter.sync(dict(_envelope(201), collection_recovery=True)))
        assert {owner for update in updates for owner in update['owners']} == {('home', sid) for sid in names}
        assert [tuple(row) for row in ledger.db.execute('SELECT sid,cost FROM calls ORDER BY sid')] == [(sid, .2) for sid in names]
        assert ledger.db.execute('SELECT count(*) FROM tracking_tasks').fetchone()[0] == len(names)
        assert tuple(ledger.db.execute("SELECT at,complete_at,loading FROM state WHERE home='home'").fetchone()) == (201, 201, 0)
        assert tuple(ledger.db.execute("SELECT at,complete FROM tracking_health WHERE home='home'").fetchone()) == (201, 1)
        with store.read_current() as view:
            expected = {('home', sid): progress.source_stamp(view.owner('home', sid)['dependencies']) for sid in names}
        assert progress.applied(ledger.db, expected) == expected


def test_recovery_cohort_cannot_overwrite_newer_state_committed_during_plan(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(delivery, 'OWNER_BATCH', 4)
    names = [f'owner-{number:03d}' for number in range(12)]
    path = tmp_path/'quota.sqlite'
    with closing(QuotaLedger(path)) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        ledger.db.execute("INSERT INTO tracking_config VALUES('home',50)")
        ledger.db.execute("INSERT INTO tracking_controls VALUES('home',50,1)")
        ledger.db.commit()
        _publish(store, 1, {sid: 1 for sid in names})
        adapter = delivery.PreparedLedger(ledger, store)
        list(adapter.sync(_envelope()))
        _publish(store, 2, {sid: 2 for sid in names})
        updates = adapter.sync(dict(_envelope(201), collection_recovery=True))
        first = next(updates)
        applied = {owner[1] for owner in first['owners']}
        assert len(applied) == 4
        with sqlite3.connect(path) as writer:
            writer.execute("UPDATE state SET at=202,complete_at=202,loading=0,unclassified=99 WHERE home='home'")
        with pytest.raises(ValueError, match='unconfirmed|newer|recovery'):
            list(updates)
        ledger.db.rollback()
        assert tuple(ledger.db.execute(
            "SELECT at,complete_at,loading,unclassified FROM state WHERE home='home'").fetchone()) == (202, 202, 0, 99)
        assert [tuple(row) for row in ledger.db.execute('SELECT sid,cost FROM calls ORDER BY sid')] == [
            (sid, .2 if sid in applied else .1) for sid in names]
