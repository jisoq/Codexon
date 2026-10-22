from dataclasses import replace
from threading import Event
import sqlite3

import pytest

from cachemonitor import prepared_store as storage
from cachemonitor.analysis_delivery import MODEL_KEYS
from cachemonitor.prepared_ingest import pages


def call(key, *, confirmed=True, **fields):
    row = dict(key=key, ts=100, session_ordinal=0, requested_model='requested',
               response_model='actual' if confirmed else 'requested',
               model_alert_confirmed=confirmed, effort='high', response_effort=None,
               requested_service_tier='priority', response_service_tier=None,
               input=20, cached=7, output=9, reasoning=3, duration=1.5,
               response_status='completed', transport='HTTP/SSE')
    row.update(fields)
    return row


def declare_owner(stage, sid, *, home='home', ordinal=0, alerts=None):
    dependency = dict.fromkeys(storage.DEPENDENCIES, 1)
    dependency['source_digest'] = 'a' * 64
    stage.owner(home, sid, dict(home=home, id=sid, title=sid), dependency,
                source_ordinal=ordinal, model_alerts=alerts)


def put_owner(stage, sid, rows, *, home='home', ordinal=0):
    rows = [dict(row, home=home, sid=sid) for row in rows]
    alerts = {row['key']: tuple(row.get(name) for name in MODEL_KEYS) for row in rows}
    declare_owner(stage, sid, home=home, ordinal=ordinal, alerts=alerts)
    stage.calls((home, sid), rows, reconcile=True)


def test_candidates_filter_confirmation_and_page_more_than_128_affected_owners(tmp_path):
    limits = replace(storage.Limits(), page_rows=7, read_seconds=10)
    with storage.PreparedStore(tmp_path/'prepared.sqlite', limits) as store:
        for revision, start in enumerate((0, 70), 1):
            stage = store.begin(revision)
            for number in range(start, start + 70):
                put_owner(stage, str(number), [call(f'response-{number}', confirmed=number != 50)],
                          ordinal=number)
            stage.publish(revision, ready=True)
        stage = store.begin(3)
        stage.catalog_order([(('home', str(number)), 139 - number) for number in range(140)])
        stage.publish(3, ready=True)
        selected = frozenset(('home', str(number)) for number in range(130))
        with store.read_current() as view:
            first = view.mismatch_candidates(selected)
            assert [row['owner'] for row in first.rows] == [('home', str(number)) for number in range(129, 122, -1)]
            assert not first.complete
            assert first.after is not None
            rows = list(pages(view.mismatch_candidates, selected))
            assert [row['owner'] for row in rows] == [('home', str(number)) for number in reversed(range(130))]
            assert [row['keys'] for row in rows] == [
                () if number == 50 else (f'response-{number}',) for number in reversed(range(130))]
            assert view.mismatch_candidates(frozenset()).rows == ()
            assert list(pages(view.mismatch_candidates, frozenset({('home', 'missing')}))) == []


def test_candidates_use_compact_packets_and_selected_records_preserve_full_facts(tmp_path):
    limits = replace(storage.Limits(), page_bytes=1000, page_objects_bytes=1200)
    path = tmp_path/'prepared.sqlite'
    with storage.PreparedStore(path, limits) as store:
        stage = store.begin(1)
        for number, sid in enumerate(('a', 'b', 'c')):
            put_owner(stage, sid, [call('response', correction_detail='x' * 5000)], ordinal=number)
        stage.publish(1, ready=True)
        with store.read_current() as view:
            page = view.mismatch_candidates()
            assert 0 < len(page.rows) < 3
            assert not page.complete
            assert page.encoded_bytes <= limits.page_bytes
            assert page.object_bytes <= limits.page_objects_bytes
            assert list(pages(view.mismatch_candidates)) == [
                dict(owner=('home', sid), keys=('response',)) for sid in ('a', 'b', 'c')]
        with storage.PreparedReader(path) as reader, reader.pin() as view:
            records = list(pages(view.records, ('home', 'a'), ('response',)))
            assert len(records) == 1
            row = records[0]
            assert row['requested_model'] == 'requested'
            assert row['response_model'] == 'actual'
            assert row['effort'] == 'high'
            assert row['response_effort'] is None
            assert row['requested_service_tier'] == 'priority'
            assert row['response_service_tier'] is None
            assert (row['input'], row['cached'], row['output'], row['reasoning'], row['duration']) == (20, 7, 9, 3, 1.5)
            assert row['correction_detail'] == 'x' * 5000


def test_occurrences_keep_conflicts_scopes_owner_moves_and_publication_isolation(tmp_path):
    limits = replace(storage.Limits(), page_rows=1, read_seconds=30)
    with storage.PreparedStore(tmp_path/'prepared.sqlite', limits) as store:
        stage = store.begin(1)
        put_owner(stage, 'old', [call('response'), call('record:local', session_ordinal=1)])
        put_owner(stage, 'other', [call('response', confirmed=False),
                                   call('record:local', session_ordinal=1)], ordinal=1)
        put_owner(stage, 'foreign', [call('response')], home='different-home', ordinal=2)
        first_token = stage.publish(1, ready=True)
        with store.read_current() as original:
            first = original.mismatch_occurrences(('home', 'response'))
            assert len(first.rows) == 1
            assert not first.complete
            assert first.encoded_bytes <= limits.page_bytes
            assert first.object_bytes <= limits.page_objects_bytes
            initial = list(pages(original.mismatch_occurrences, ('home', 'response')))
            assert [(row['sid'], row['model_alert_confirmed'], row['response_model']) for row in initial] == [
                ('old', True, 'actual'), ('other', False, 'requested')]
            assert all(row['home'] == 'home' for row in initial)
            assert all(row['input'] == 20 and row['response_effort'] is None for row in initial)
            local = list(pages(original.mismatch_occurrences, ('home', 'old', 'record:local')))
            assert [(row['sid'], row['key']) for row in local] == [('old', 'record:local')]
            affected = frozenset({('home', 'other')})
            changed = original.mismatch_changed_identities(affected)
            assert len(changed.rows) == 1
            assert not changed.complete
            assert set(pages(original.mismatch_changed_identities, affected)) == {
                ('home', 'response'), ('home', 'other', 'record:local')}
            assert original.mismatch_occurrences(('missing-home', 'response')).rows == ()
            stage = store.begin(2)
            declare_owner(stage, 'old')
            stage.remove_owner(('home', 'old'))
            put_owner(stage, 'new', [call('response', response_effort='low')], ordinal=3)
            with stage.read_snapshot() as staged:
                current = list(pages(staged.mismatch_occurrences, ('home', 'response')))
                assert [(row['sid'], row['model_alert_confirmed']) for row in current] == [
                    ('other', False), ('new', True)]
                assert current[-1]['response_effort'] == 'low'
                assert staged.mismatch_occurrences(('home', 'old', 'record:local')).rows == ()
                assert list(pages(staged.mismatch_changed_identities,
                                  frozenset({('home', 'old'), ('home', 'new')}))) == [('home', 'response')]
                assert ('home', 'old') not in {row['owner'] for row in pages(staged.mismatch_candidates)}
            with store.read_current() as committed:
                assert list(pages(committed.mismatch_occurrences, ('home', 'response'))) == initial
            stage.publish(2, ready=True)
            assert original.token == first_token
            assert list(pages(original.mismatch_occurrences, ('home', 'response'))) == initial
            assert ('home', 'old') in {row['owner'] for row in pages(original.mismatch_candidates)}
        with store.read_current() as view:
            assert [row['sid'] for row in pages(view.mismatch_occurrences, ('home', 'response'))] == ['other', 'new']
        stage = store.begin(3)
        put_owner(stage, 'other', [call('record:local', session_ordinal=1)], ordinal=1)
        stage.publish(3, ready=True)
        with store.read_current() as view:
            assert [row['sid'] for row in pages(view.mismatch_occurrences, ('home', 'response'))] == ['new']
        stage = store.begin(4)
        put_owner(stage, 'new', [call('response', confirmed=False)], ordinal=3)
        stage.abort()
        with store.read_current() as view:
            current = list(pages(view.mismatch_occurrences, ('home', 'response')))
            assert len(current) == 1
            assert current[0]['sid'] == 'new'
            assert current[0]['model_alert_confirmed']
        stage = store.begin(5)
        declare_owner(stage, 'new', ordinal=3)
        stage.remove_owner(('home', 'new'))
        stage.publish(5, ready=True)
        with store.read_current() as view:
            assert view.mismatch_occurrences(('home', 'response')).rows == ()


def test_existing_prepared_database_adds_lookup_without_replacing_publication(tmp_path):
    path = tmp_path/'prepared.sqlite'
    with storage.PreparedStore(path) as store:
        stage = store.begin(1)
        put_owner(stage, 'owner', [call('response')])
        token = stage.publish(1, ready=True)
        store.db.execute('DROP INDEX calls_identity')
    with storage.PreparedStore(path) as store:
        assert store.token == token
        with store.read_current() as view:
            assert view.token == token
            assert list(pages(view.mismatch_candidates)) == [dict(owner=('home', 'owner'), keys=('response',))]
            rows = list(pages(view.mismatch_occurrences, ('home', 'response')))
            assert len(rows) == 1
            assert rows[0]['model_alert_confirmed']
            assert (rows[0]['home'], rows[0]['sid'], rows[0]['key']) == ('home', 'owner', 'response')


@pytest.mark.parametrize('method, arguments', [
    ('mismatch_candidates', ()), ('mismatch_occurrences', (('home', 'response'),)),
    ('mismatch_changed_identities', (frozenset({('home', 'owner')}),))])
def test_source_cancellation_releases_snapshot(tmp_path, method, arguments):
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        stage = store.begin(1)
        put_owner(stage, 'owner', [call('response')])
        token = stage.publish(1, ready=True)
        cancelled = Event()
        with store.read_current(cancel=cancelled) as view:
            cancelled.set()
            with pytest.raises(storage.ReadCancelled):
                getattr(view, method)(*arguments)
            assert view.closed
        with store.read_current() as view:
            assert view.token == token
            assert getattr(view, method)(*arguments).rows


def test_real_prepared_records_feed_archive_and_saved_detail_survives_owner_deletion(tmp_path):
    from contextlib import closing
    from cachemonitor.mismatch_history import MismatchArchive, MismatchReader
    archive=tmp_path/'usage.mismatches.sqlite'
    prepared=tmp_path/'usage.prepared.sqlite'
    with storage.PreparedStore(prepared) as store, closing(MismatchArchive(archive)) as writer:
        stage=store.begin(1)
        put_owner(stage,'owner',[call('response',model='requested',completion_latency_ms=1500)])
        stage.publish(1,ready=True)
        with store.read_current() as view:
            assert writer.capture(view)['status']=='ready'
        stage=store.begin(2)
        declare_owner(stage,'owner')
        stage.remove_owner(('home','owner'))
        stage.publish(2,ready=True)
        with store.read_current() as view:
            writer.capture(view,owners=frozenset({('home','owner')}))
    with closing(MismatchReader(archive)) as reader:
        saved=reader.record(('home','response'))['row']
        assert saved['requested_model']=='requested'
        assert saved['response_model']=='actual'
        assert saved['correction_at'] is None


def test_selected_source_reads_use_indexed_narrow_queries_under_a_vm_budget(tmp_path):
    path = tmp_path/'prepared.sqlite'
    unrelated, affected_count = 2048, 130
    with storage.PreparedStore(path) as store:
        for start in range(0, unrelated + affected_count, 128):
            stage = store.begin(start + 1)
            for number in range(start, min(start + 128, unrelated + affected_count)):
                if number < unrelated:
                    declare_owner(stage, f'cold-{number}', ordinal=number)
                else:
                    changed = number - unrelated
                    put_owner(stage, f'changed-{changed}',
                              [call(f'response-{changed}', confirmed=changed % 2 == 0)], ordinal=number)
            stage.publish(start + 1, ready=True)
        def narrow(action, table, column, database, origin):
            if action == sqlite3.SQLITE_READ:
                if table == 'owner_versions' and column in {
                        'metadata', 'call_metadata', 'summary', 'choices', 'dependencies', 'checkpoint'}:
                    return sqlite3.SQLITE_DENY
                if table == 'calls' and column not in {'owner', 'identity', 'batch', 'deleted', 'key_value'}:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        limits = replace(storage.Limits(), page_rows=7, read_vm_steps=5000, read_seconds=30)
        with storage.PreparedReader(path, limits) as reader, reader.pin() as view:
            view.db.set_authorizer(narrow)
            selected = frozenset({('home', 'changed-129')})
            assert view.mismatch_candidates(selected).rows == (
                dict(owner=('home', 'changed-129'), keys=()),)
            assert view.mismatch_changed_identities(selected).rows == (('home', 'response-129'),)
        limits = replace(limits, read_vm_steps=20000)
        with storage.PreparedReader(path, limits) as reader, reader.pin() as view:
            view.db.set_authorizer(narrow)
            selected = frozenset(('home', f'changed-{number}') for number in range(affected_count))
            first = view.mismatch_candidates(selected)
            assert len(first.rows) == 7
            assert not first.complete
            assert first.encoded_bytes <= limits.page_bytes
            assert first.object_bytes <= limits.page_objects_bytes
            candidates = list(pages(view.mismatch_candidates, selected))
            assert [row['owner'] for row in candidates] == [
                ('home', f'changed-{number}') for number in range(affected_count)]
            assert [row['keys'] for row in candidates] == [
                (f'response-{number}',) if number % 2 == 0 else () for number in range(affected_count)]
            identities = list(pages(view.mismatch_changed_identities, selected))
            assert len(identities) == affected_count
            assert set(identities) == {('home', f'response-{number}') for number in range(affected_count)}


def test_changed_identities_advance_past_empty_owners_and_respect_tombstones(tmp_path):
    limits = replace(storage.Limits(), page_rows=1)
    selected = frozenset({('home', 'empty'), ('home', 'changed')})
    with storage.PreparedStore(tmp_path/'prepared.sqlite', limits) as store:
        stage = store.begin(1)
        declare_owner(stage, 'empty')
        put_owner(stage, 'changed', [call('response', confirmed=False),
                                    call('legacy:local', confirmed=False, session_ordinal=1)], ordinal=1)
        stage.publish(1, ready=True)
        with store.read_current() as view:
            first = view.mismatch_changed_identities(selected)
            assert first.rows == ()
            assert not first.complete
            assert first.after is not None
            assert set(pages(view.mismatch_changed_identities, selected)) == {
                ('home', 'response'), ('home', 'changed', 'legacy:local')}
        stage = store.begin(2)
        put_owner(stage, 'changed', [call('legacy:local', confirmed=False, session_ordinal=1)], ordinal=1)
        with stage.read_snapshot() as view:
            assert list(pages(view.mismatch_changed_identities, selected)) == [('home', 'changed', 'legacy:local')]
        stage.publish(2, ready=True)
        stage = store.begin(3)
        declare_owner(stage, 'changed', ordinal=1)
        stage.remove_owner(('home', 'changed'))
        stage.publish(3, ready=True)
        with store.read_current() as view:
            assert list(pages(view.mismatch_changed_identities, selected)) == []


def test_added_unconfirmed_owner_retracts_saved_real_identity_when_only_that_owner_changes(tmp_path):
    from contextlib import closing
    import time
    from cachemonitor.mismatch_history import MismatchArchive, MismatchReader
    archive = tmp_path/'usage.mismatches.sqlite'
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store, closing(MismatchArchive(archive)) as writer:
        def capture(view, **kwargs):
            writer.capture(view, **kwargs)
            deadline = time.monotonic() + 5
            while writer.has_work:
                assert time.monotonic() < deadline
                writer.step(view)
        stage = store.begin(1)
        put_owner(stage, 'confirmed', [call('response', model='requested', completion_latency_ms=1500)])
        stage.publish(1, ready=True)
        with store.read_current() as view:
            capture(view)
        stage = store.begin(2)
        put_owner(stage, 'new-owner', [call('response', confirmed=False),
                                      call('never-confirmed', confirmed=False, session_ordinal=1)], ordinal=1)
        stage.publish(2, ready=True)
        with store.read_current() as view:
            capture(view, owners=frozenset({('home', 'new-owner')}))
    with closing(MismatchReader(archive)) as reader:
        rows = reader.window()['window']['rows']
        assert len(rows) == 1
        saved = rows[0]
        assert saved['identity'] == ('home', 'response')
        assert saved['requested_model'] == 'requested'
        assert saved['response_model'] == 'actual'
        assert saved['correction_at'] is not None
        assert saved['correction_reason'] == 'conflicting_occurrences'


@pytest.mark.parametrize('deleted',[False,True])
@pytest.mark.parametrize('unavailable',[False,True])
def test_sliced_archive_retains_pinned_confirmation_before_later_correction_or_deletion(tmp_path,deleted,unavailable):
    from contextlib import closing
    from cachemonitor.mismatch_history import MismatchArchive, MismatchReader
    archive=tmp_path/'usage.mismatches.sqlite'
    released=[]
    holder=MismatchArchive(archive) if unavailable else None
    if holder is not None:holder.retry(force=True)
    with storage.PreparedStore(tmp_path/'usage.prepared.sqlite') as store, closing(MismatchArchive(archive)) as writer:
        stage=store.begin(1)
        put_owner(stage,'owner',[call('a-first'),call('z-transient',session_ordinal=1)])
        stage.publish(1,ready=True)
        first_scope=store.read_current()
        first_view=first_scope.__enter__()
        def release_first():
            first_scope.__exit__(None,None,None)
            released.append('first')
        writer.capture(first_view,limit=1,release=release_first)
        assert writer.has_work and released==[]
        stage=store.begin(2)
        rows=[call('a-first')]
        if not deleted:rows.append(call('z-transient',confirmed=False,session_ordinal=1))
        put_owner(stage,'owner',rows)
        stage.publish(2,ready=True)
        second_scope=store.read_current()
        second_view=second_scope.__enter__()
        def release_second():
            second_scope.__exit__(None,None,None)
            released.append('second')
        writer.capture(second_view,owners=frozenset({('home','owner')}),limit=1,release=release_second)
        while writer.has_work:writer.step(limit=1)
        assert released==['first','second']
        if holder is not None:
            assert writer.health()['pending']==2
            holder.close()
            writer.retry(force=True)
    with closing(MismatchReader(archive)) as reader:
        saved=reader.record(('home','z-transient'))['row']
        assert saved is not None and saved['response_model']=='actual'
        assert (saved['correction_at'] is not None)==(not deleted)


def test_normal_shutdown_drains_transient_pinned_confirmation_after_writer_cancellation(tmp_path):
    from contextlib import closing
    from threading import Event
    from cachemonitor.mismatch_history import MismatchArchive, MismatchReader
    archive=tmp_path/'usage.mismatches.sqlite'
    stopped=Event()
    released=[]
    with storage.PreparedStore(tmp_path/'usage.prepared.sqlite') as store:
        writer=MismatchArchive(archive,stopped.is_set)
        try:
            stage=store.begin(1)
            put_owner(stage,'owner',[call('a-first'),call('z-transient',session_ordinal=1)])
            stage.publish(1,ready=True)
            first_scope=store.read_current(cancel=stopped)
            view=first_scope.__enter__()
            def release_first():
                first_scope.__exit__(None,None,None)
                released.append('first')
            writer.capture(view,limit=1,release=release_first)
            stage=store.begin(2)
            put_owner(stage,'owner',[call('a-first')])
            stage.publish(2,ready=True)
            second_scope=store.read_current(cancel=stopped)
            view=second_scope.__enter__()
            def release_second():
                second_scope.__exit__(None,None,None)
                released.append('second')
            writer.capture(view,owners=frozenset({('home','owner')}),limit=0,release=release_second)
            stopped.set()
            writer.close()
            assert released==['first','second']
        finally:
            writer.close()
    with closing(MismatchReader(archive)) as reader:
        assert reader.record(('home','z-transient'))['row']['response_model']=='actual'
        assert not reader.window()['capture_gap']


WAL_OWNER=("home","session")

def _wal_confirmed_calls():
    return [dict(home=WAL_OWNER[0], sid=WAL_OWNER[1], key=f'response-{number}',
                 ts=100 + number, session_ordinal=number, model='requested',
                 requested_model='requested', response_model='actual',
                 model_alert_confirmed=True, effort='high', input=100,
                 cached=40, output=20, reasoning=5, response_status='completed',
                 timing_valid=True, completion_latency_ms=2000)
            for number in range(64)]


def _wal_facts(stage, revision, rows):
    dependencies = dict.fromkeys(storage.DEPENDENCIES, 1)
    dependencies['source_digest'] = f'{revision:064x}'
    alerts = {row['key']: tuple(row.get(field) for field in MODEL_KEYS)
              for row in rows if row.get('model_alert_confirmed')}
    stage.owner(*WAL_OWNER, dict(home=WAL_OWNER[0], sid=WAL_OWNER[1], title='Session'),
                dependencies, source_ordinal=0, model_alerts=alerts)
    stage.calls(WAL_OWNER, rows, reconcile=True)


def test_archive_lease_releases_during_partial_conversion_and_wal_cleanup_recovers(tmp_path):
    from contextlib import closing
    import time
    from cachemonitor.mismatch_history import MismatchArchive, MismatchReader
    from cachemonitor.prepared_runtime import PreparedWriter
    path = tmp_path/'prepared.sqlite'
    archive_path = tmp_path/'mismatches.sqlite'
    limits = replace(storage.Limits(), wal_bytes=2 * 1024 * 1024,
                     write_reserve_bytes=128 * 1024, page_rows=1, read_seconds=30)
    writer = object.__new__(PreparedWriter)
    writer.stopped = Event()
    initial = _wal_confirmed_calls()
    released = []
    with (storage.PreparedStore(path, limits) as store,
          closing(MismatchArchive(archive_path)) as archive,
          storage.PreparedReader(path) as reader):
        stage = store.begin(('source', 1))
        _wal_facts(stage, 1, initial)
        initial_batch = stage.batch
        first = stage.publish(('source', 1), ready=True)
        assert store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        scope = store.read_current(seconds=30)
        pinned = scope.__enter__()
        def release():
            assert not released
            released.append(pinned.token)
            scope.__exit__(None, None, None)
        try:
            archive.capture(pinned, limit=0, release=release)
        except BaseException:
            scope.__exit__(None, None, None)
            raise
        assert archive.has_work
        assert not pinned.closed
        assert not released

        partial = [dict(initial[0], output=30, response_model='requested', model_alert_confirmed=False)]
        stage = store.begin(('source', 2))
        _wal_facts(stage, 2, partial)
        second = stage.publish(('source', 2), ready=False)
        assert second.generation == first.generation + 1
        with pytest.raises(storage.NotReady):
            reader.pin(second)
        assert pinned.token == first
        assert pinned.record(*WAL_OWNER, 'response-1')['model_alert_confirmed']

        unfinished = store.begin(('source', 3))
        before = store.disk_usage()['wal']
        assert before > 64 * 1024
        store.limits = replace(store.limits, wal_bytes=before + 64 * 1024,
                               write_reserve_bytes=128 * 1024)
        with pytest.raises(storage.WriteDeferred):
            store.maintain()
        assert store.disk_usage()['wal'] == before
        assert store.token == second
        assert archive.has_work and not released

        deadline = time.monotonic() + 5
        maintenance_due = True
        while maintenance_due or archive.has_work:
            assert time.monotonic() < deadline, 'archive lease prevented partial-conversion cleanup'
            maintenance_due = writer._maintain(store, archive)
        assert released == [first]
        assert pinned.closed
        assert not archive.has_work
        assert archive.health()['status'] == 'ready'
        assert store.token == second
        with pytest.raises(storage.NotReady):
            reader.pin(second)
        assert store.db.execute('SELECT 1 FROM owner_versions WHERE batch=? LIMIT 1',
                                (initial_batch,)).fetchone() is None
        assert store.db.execute('SELECT 1 FROM calls WHERE batch=? LIMIT 1',
                                (initial_batch,)).fetchone() is None
        assert store.disk_usage()['wal'] <= store.limits.wal_bytes
        assert store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
        assert store.disk_usage()['wal'] == 0
        with closing(MismatchReader(archive_path)) as saved:
            window = saved.window()['window']
            assert window['total'] == len(initial)
            assert len(window['rows']) == len(initial)
            assert all(row['response_model'] == 'actual' and row['correction_at'] is None
                       for row in window['rows'])

        final_rows = [dict(partial[0], output=40),
                      dict(partial[0], key='next-cohort', ts=200, session_ordinal=64, output=40)]
        _wal_facts(unfinished, 3, final_rows)
        third = unfinished.publish(('source', 3), ready=True)
        deadline = time.monotonic() + 5
        while writer._maintain(store, archive):
            assert time.monotonic() < deadline
        assert third.generation == second.generation + 1
        with reader.pin(third) as current:
            assert current.ready
            assert current.record(*WAL_OWNER, 'response-0')['output'] == 40
            assert current.record(*WAL_OWNER, 'response-1') is None
            assert current.record(*WAL_OWNER, 'next-cohort')['output'] == 40
            assert len(tuple(current.iter_calls(WAL_OWNER))) == 2
        assert store.disk_usage()['wal'] <= store.limits.wal_bytes
        with closing(MismatchReader(archive_path)) as saved:
            assert saved.record(('home', 'response-1'))['row']['response_model'] == 'actual'
