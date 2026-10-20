"""Writer-side snapshots release ownership before the next read starts."""
from dataclasses import replace
from threading import Event

import pytest

from cachemonitor import prepared_store as storage
from test_prepared_analysis import OWNER, calls, publish


def test_nested_writer_reads_preserve_each_committed_generation(tmp_path):
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        first = publish(store, 1, calls())
        with store.read_current() as outer:
            second = publish(store, 2, [dict(row, cost=2.) for row in calls()])
            with store.read_current() as inner:
                assert inner.token == second
                assert inner.record(*OWNER, 'call-0')['cost'] == 2.
            assert outer.token == first
            assert outer.record(*OWNER, 'call-0')['cost'] == 1.
        store.limits = replace(store.limits, page_rows=1)
        with store.read_current() as current:
            page = current.calls(OWNER)
            assert len(page.rows) == 1 and not page.complete
            assert current.token == second
        assert store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0


def test_cancelled_and_explicitly_closed_reads_do_not_own_a_nested_read(tmp_path):
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        publish(store, 1, calls())
        cancelled = Event()
        with store.read_current(cancel=cancelled) as outer:
            cancelled.set()
            with pytest.raises(storage.ReadCancelled):
                outer.record(*OWNER, 'call-0')
            with store.read_current() as inner:
                outer.close()
                assert inner.record(*OWNER, 'call-1')['cost'] == 1.
        with pytest.raises(storage.ReadCancelled):
            with store.read_current(cancel=cancelled):
                pytest.fail('a cancelled read must not start')
        with store.read_current() as current:
            assert current.record(*OWNER, 'call-2')['cost'] == 1.


def test_suspended_speed_finalizer_cannot_clear_a_later_speed_read(tmp_path):
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        publish(store, 1, calls())
        with store.read_current() as old:
            old_stream = old.iter_speed_rows({OWNER: None})
            first = next(old_stream)
        with store.read_current() as current:
            current_stream = current.iter_speed_rows({OWNER: None})
            assert next(current_stream) == first
            old_stream.close()
            remaining = list(current_stream)
            assert len(remaining) == 2
            assert [row[1] for row in remaining] == [1, 2]
        with store.read_current() as current:
            assert len(list(current.iter_speed_rows({OWNER: None}))) == 3


def test_store_close_during_read_releases_writer_and_rejects_new_reads(tmp_path):
    path = tmp_path/'prepared.sqlite'
    store = storage.PreparedStore(path)
    first = publish(store, 1, calls())
    with store.read_current() as view:
        store.close()
        assert view.record(*OWNER, 'call-0')['cost'] == 1.
        with pytest.raises(storage.PreparedError):
            with store.read_current():
                pytest.fail('a closed store must not open a new reader')
    store.close()
    with storage.PreparedStore(path) as reopened:
        assert reopened.token == first
        assert reopened.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
