"""Ready-store restarts retain exact speed checkpoints across fitting updates."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import sqlite3
import time

import pytest

from cachemonitor import prepared_speed_writer, prepared_store as storage
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.analysis_records import public_record
from cachemonitor.core import Session
from cachemonitor.prepared_ingest import PreparedCohort, preparation_signature, read_catalog
from cachemonitor.prepared_speed_writer import PreparedSpeedWriter, SPEED_CHECKPOINT_NAMESPACE
from cachemonitor.speed_health import classify
from cachemonitor.usage_collection import CollectionChannel, CollectionClient, empty_snapshot


def test_ready_restart_updates_twenty_owners_without_replaying_unchanged_speed_history(tmp_path, monkeypatch):
    writer = CollectionChannel([tmp_path/'home'], tmp_path/'source.sqlite')
    reader = CollectionClient(writer.homes, writer.path)
    writer.start_live()
    path = tmp_path/'prepared.sqlite'
    sources = []
    for number in range(24):
        sid = f'owner-{number:02d}'
        session = Session(sid, writer.homes[0], title=sid)
        count = 3 if number < 20 else 48
        for ordinal in range(count):
            session.add_usage(1000 + number*1000 + ordinal*10, f'{sid}-call-{ordinal}',
                dict(input_tokens=80000, cached_input_tokens=72000, output_tokens=2000),
                'gpt-6-astra', 'request', 'medium', service_tier='Standard')
        source = deepcopy(session.view(26000))
        for row in source['history']:
            row.update(reasoning=500, transport='WebSocket', transport_source='response_id',
                response_status='completed', timing_valid=True, completion_latency_ms=20000)
        sources.append(source)

    def publish_sources(sequence):
        now = time.time()
        snapshot = empty_snapshot(writer.homes, writer.path, now)
        snapshot.update(sessions=sources, usage_collection_complete=True,
            last_usage_collection_success=now, request_activity=[], quota_by_home={})
        snapshot['index'].update(loading=False, usage_complete=True, last_usage_success=now)
        writer.publish(snapshot, 'restart-incremental', sequence)
        return read_catalog(reader)

    try:
        catalog = publish_sources(1)
        with storage.PreparedStore(path) as store, closing(PreparedSpeedWriter(
                store, preparation=preparation_signature())) as speed:
            coordinator = PreparedCohort(store, reader, None, speed_writer=speed)
            assert list(coordinator.convert(catalog))[-1]['ready']
            previous = store.token
            with store.read_current() as view:
                checkpoint = view.checkpoint_state(SPEED_CHECKPOINT_NAMESPACE)
                assert tuple(checkpoint['token']) == (previous.instance, previous.generation)

        dirty = {(source['home'], source['id']) for source in sources[:20]}
        dirty_history_rows = sum(len(source['history']) for source in sources[:20])
        for source in sources[:20]:
            source['title'] += ' updated'
            source['usage_revision'] = source.get('usage_revision', 0) + 1
        catalog = publish_sources(2)

        prefix_reads, classifier_reads = Counter(), Counter()
        prefix_active = [False]
        original_rows = storage.Snapshot.iter_speed_rows
        original_certificate = prepared_speed_writer.certify_speed_prefix

        def observed_rows(snapshot, ranges):
            for value in original_rows(snapshot, ranges):
                (prefix_reads if prefix_active[0] else classifier_reads)[value[0]] += 1
                yield value

        def observed_certificate(*args, **kwargs):
            prefix_active[0] = True
            try:
                return original_certificate(*args, **kwargs)
            finally:
                prefix_active[0] = False

        monkeypatch.setattr(storage.Snapshot, 'iter_speed_rows', observed_rows)
        monkeypatch.setattr(prepared_speed_writer, 'certify_speed_prefix', observed_certificate)
        with storage.PreparedStore(path) as store, closing(PreparedSpeedWriter(
                store, preparation=preparation_signature())) as speed:
            assert store.token == previous
            coordinator = PreparedCohort(store, reader, None, speed_writer=speed)
            updates = list(coordinator.convert(catalog))
            assert len(updates) == 1 and updates[0]['ready']
            assert set(updates[0]['changed']) == dirty
            assert store.token.generation == previous.generation + 1
            # Capture before current_speeds(), which resets read-only diagnostics.
            assert speed.last_checkpoint['restored']
            assert speed.last_checkpoint['recovery'] is None
            assert speed.last_checkpoint['mode'] == 'delta'
            assert not speed.tracker.metrics['capacity_fallback']
            assert speed.tracker.metrics['replay_rows'] == 0
            assert speed.tracker.metrics['proof_rows'] == 0
            assert speed.last_read_rows == 0
            assert not classifier_reads
            assert set(prefix_reads) <= dirty
            assert sum(prefix_reads.values()) <= dirty_history_rows

            reference = AnalysisEngine()
            reference.ingest(sources)
            assert coordinator.speed_results == classify(
                state['prepared'] for state in reference.sessions.values())
            with store.read_current() as view:
                checkpoint = view.checkpoint_state(SPEED_CHECKPOINT_NAMESPACE)
                assert tuple(checkpoint['token']) == (store.token.instance, store.token.generation)
                for owner, state in reference.sessions.items():
                    assert list(view.iter_calls(owner)) == [
                        public_record(row) for row in state['prepared']['history']]
                    assert view.derived(*owner, 'summary')['own']['title'] == state['prepared']['title']
    finally:
        reader.close()
        writer.close()


def test_ready_restart_falls_back_when_combined_update_exceeds_staging_budget(tmp_path):
    writer = CollectionChannel([tmp_path/'home'], tmp_path/'source.sqlite')
    reader = CollectionClient(writer.homes, writer.path)
    writer.start_live()
    sources = []
    for number in range(20):
        sid = f'owner-{number:02d}'
        session = Session(sid, writer.homes[0], title=sid)
        session.add_usage(1000+number, f'{sid}-call',
            dict(input_tokens=100, cached_input_tokens=20, output_tokens=10),
            'gpt-6-astra', 'request', 'medium', service_tier='Standard')
        sources.append(deepcopy(session.view(1100)))

    def publish_sources(sequence):
        now = time.time()
        snapshot = empty_snapshot(writer.homes, writer.path, now)
        snapshot.update(sessions=sources, usage_collection_complete=True,
            last_usage_collection_success=now, request_activity=[], quota_by_home={})
        snapshot['index'].update(loading=False, usage_complete=True, last_usage_success=now)
        writer.publish(snapshot, 'restart-staging-budget', sequence)
        return read_catalog(reader)

    def contents(store):
        with store.read_current() as view:
            return {owner:(list(view.iter_calls(owner)), view.derived(*owner, 'summary'))
                    for owner in catalog['owners']}

    reference_path, tested_path = tmp_path/'reference.sqlite', tmp_path/'tested.sqlite'
    try:
        catalog = publish_sources(1)
        with storage.PreparedStore(tmp_path/'base.sqlite') as store:
            assert list(PreparedCohort(store, reader, None).convert(catalog))[-1]['ready']
            last_batch = store.db.execute('SELECT max(id) FROM publications').fetchone()[0]
            previous = store.token
            for path in (reference_path, tested_path):
                with closing(sqlite3.connect(path)) as backup:
                    store.db.backup(backup)

        for source in sources:
            source['title'] += ' updated ' + 'x'*2000
            source['usage_revision'] = source.get('usage_revision', 0) + 1
        catalog = publish_sources(2)

        with storage.PreparedStore(reference_path) as reference:
            updates = list(PreparedCohort(reference, reader, None).convert(catalog, cohort_size=16))
            assert updates[-1]['ready']
            charges = [row[0] for row in reference.db.execute(
                'SELECT input_bytes FROM publications WHERE id>? AND state=1', (last_batch,))]
            assert len(charges) > 1 and min(charges) > 0
            # Derive a real aggregate boundary from this fixture's successful
            # bounded publications, rather than assuming encoded packet sizes.
            budget = max(charges) + max(1, min(charges)//4)
            assert max(charges) < budget < sum(charges)
            expected = contents(reference)

        with storage.PreparedStore(tested_path) as tested:
            tested.limits = replace(tested.limits, staging_bytes=budget)
            coordinator = PreparedCohort(tested, reader, None)
            updates = []
            for update in coordinator.convert(catalog):
                updates.append(update)
                if not update['ready']:
                    with storage.PreparedReader(tested_path) as public:
                        with pytest.raises(storage.NotReady):
                            public.pin(tested.token)
            assert updates and updates[-1]['ready']
            assert tested.token.generation > previous.generation
            attempted = tested.db.execute(
                'SELECT state,input_bytes FROM publications WHERE id>?', (last_batch,)).fetchall()
            assert any(row['state'] == -1 for row in attempted)
            assert all(row['input_bytes'] <= budget for row in attempted if row['state'] == 1)
            assert contents(tested) == expected
            with storage.PreparedReader(tested_path) as public, public.pin(tested.token) as view:
                assert all(view.owner(*owner)['dependencies']['prepared'] for owner in catalog['owners'])
    finally:
        reader.close()
        writer.close()
