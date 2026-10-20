"""Immutable owner revisions and staging admission remain exact and bounded."""
from dataclasses import replace
import sqlite3

import pytest

from cachemonitor import prepared_store as storage
from cachemonitor.prepared_ingest import pages


def test_fresh_owner_fact_packet_is_bounded_atomic_and_recovers_after_failure(tmp_path):
    limits = replace(storage.Limits(), fact_write_rows=2)
    owner = ('home','owner')
    deps = {name:1 for name in storage.DEPENDENCIES}
    deps['source_digest'] = 'a'*64
    rows = [dict(key=str(i),ts=100+i,session_ordinal=i) for i in range(5)]
    path = tmp_path/'packet.sqlite'
    with storage.PreparedStore(path, limits) as store:
        stage = store.begin(1)
        stage.owner(*owner,dict(home=owner[0],id=owner[1]),deps,source_ordinal=0)
        writes=[]; current=[]
        def trace(sql):
            if sql == 'BEGIN IMMEDIATE': current.clear()
            elif sql.startswith('INSERT INTO calls ') or sql.startswith('INSERT INTO sections '): current.append(sql)
            elif sql == 'COMMIT': writes.append(len(current))
        store.db.set_trace_callback(trace)
        stage.facts(owner, sections=[('section','key',0,None,{'a':1})], calls=rows,
                    requests=[], headers=[])
        store.db.set_trace_callback(None)
        assert writes == [2,2,2]
        token = stage.publish(1, ready=True)
        with store.read_current() as view:
            assert list(view.iter_calls(owner)) == rows
        # Existing facts still reconcile replacements and removals exactly.
        stage = store.begin(2)
        stage.owner(*owner,dict(home=owner[0],id=owner[1]),deps)
        stage.facts(owner, sections=[], calls=[dict(rows[0],ts=500)], requests=[], headers=[])
        token = stage.publish(2, ready=True)
        with store.read_current() as view:
            assert list(view.iter_calls(owner)) == [dict(rows[0],ts=500)]
        stage = store.begin(3)
        fresh = ('home','broken')
        stage.owner(*fresh,dict(home=fresh[0],id=fresh[1]),deps,source_ordinal=1)
        with pytest.raises(sqlite3.IntegrityError):
            stage.facts(fresh,sections=[],calls=[*rows,rows[0]],requests=[],headers=[])
        with pytest.raises(storage.IncompletePublication): stage.publish(3,ready=True)
        assert store.token == token
    with storage.PreparedStore(path, limits) as store, store.read_current() as view:
        assert store.token == token
        assert view.owner(*fresh) is None
        assert list(view.iter_calls(owner)) == [dict(rows[0],ts=500)]


def test_member_packets_preserve_selected_order_revisions_and_cancel(tmp_path):
    from threading import Event
    stopped=Event()
    with storage.PreparedStore(tmp_path/'selected.sqlite') as store:
        stage=store.begin(1)
        deps={name:1 for name in storage.DEPENDENCIES};deps['source_digest']='a'*64
        for number in range(3):
            stage.owner('home',str(number),dict(id=str(number)),deps,
                        source_ordinal=number,summary={'value':number})
        token=stage.publish(1,ready=True)
        selected=[('home','2'),('home','0'),('home','missing')]
        with store.read_current() as view:
            revisions={row['owner']:row['revision'] for row in pages(view.owner_revisions)}
            for kind, single in (('ledger',view.ledger_source),('finance',view.financial_member)):
                with view.member_packets(selected,kind) as packets:
                    actual=list(packets)
                assert actual == [(key,revisions[key],single(*key)) for key in selected[:2]]
            with pytest.raises(ValueError), view.member_packets([selected[0]]*2,'ledger'): pass
        with store.read_current(cancel=stopped) as view:
            with view.member_packets(selected,'ledger') as packets:
                assert next(packets)[0] == selected[0]
                stopped.set()
                with pytest.raises(storage.ReadCancelled): next(packets)
            assert view.closed
        # Cancellation releases the cursor and lease; the next read can proceed.
        with store.read_current() as view: assert view.token == token


def test_revision_reads_track_body_replacements_not_order_and_reject_staging(tmp_path):
    with storage.PreparedStore(tmp_path/'prepared.sqlite', limits=replace(storage.Limits(), page_rows=1)) as store:
        stage = store.begin(1)
        for index, sid in enumerate(('a', 'b')):
            deps = {name: 1 for name in storage.DEPENDENCIES}
            deps['source_digest'] = 'a'*64
            stage.owner('home', sid, {'home': 'home', 'id': sid}, deps,
                        source_ordinal=index, summary={'value': sid})
        first = stage.publish(1, ready=True)
        with store.read_current() as view:
            initial = list(pages(view.owner_revisions))
            assert len(initial) == 2
            assert list(pages(view.owner_revisions, [('home','missing')])) == []
            assert view.ledger_source('home','a')['metadata'] == {'home':'home','id':'a'}
            assert view.financial_member('home','a')['summary'] == {'value':'a'}
        stage = store.begin(2)
        stage.catalog_order([(('home','a'), 1), (('home','b'), 0)])
        with store.read_current(staged=True) as view:
            with pytest.raises(storage.NotReady):
                view.owner_revisions()
        second = stage.publish(2, ready=True)
        with store.read_current() as view:
            current = list(pages(view.owner_revisions))
            assert [row['owner'] for row in current] == [('home','b'), ('home','a')]
            assert {r['owner']:r['revision'] for r in current} == {r['owner']:r['revision'] for r in initial}
            with pytest.raises(ValueError):
                view.owner_revisions([('home',str(n)) for n in range(129)])
        assert second.generation == first.generation + 1


def test_staging_charge_exact_limit_failure_and_transaction_rollback(tmp_path):
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        stage = store.begin('charge')
        stage.limits = replace(stage.limits, staging_bytes=100)
        with store._transaction():
            stage._charge(100)
            stage._charge(0)
        with pytest.raises(storage.BatchCapacityExceeded), store._transaction():
            stage._charge(1)
        assert store.db.execute('SELECT input_bytes FROM publications WHERE id=?', (stage.batch,)).fetchone()[0] == 100
        with pytest.raises(storage.BatchCapacityExceeded), store._transaction():
            stage._charge(10**100)
        stage.abort()
        with pytest.raises(storage.IncompletePublication):
            stage._charge(0)
        stage = store.begin('rollback')
        with pytest.raises(RuntimeError), store._transaction():
            stage._charge(13)
            raise RuntimeError('after charge')
        assert store.db.execute('SELECT input_bytes FROM publications WHERE id=?', (stage.batch,)).fetchone()[0] == 0
        store.db.execute("CREATE TEMP TRIGGER reject_charge BEFORE UPDATE OF input_bytes ON publications BEGIN SELECT RAISE(ABORT,'charge rejected'); END")
        with pytest.raises(sqlite3.IntegrityError), store._transaction():
            stage._charge(1)
        assert store.db.execute('SELECT input_bytes FROM publications WHERE id=?', (stage.batch,)).fetchone()[0] == 0
