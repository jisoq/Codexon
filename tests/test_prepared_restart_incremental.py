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

def test_no_change_speed_recovery_publishes_once_and_survives_restart(tmp_path, monkeypatch):
    from test_speed_health import session, appended
    from cachemonitor.prepared_delivery import PreparedPublisher
    writer = CollectionChannel([tmp_path/'home'], tmp_path/'source.sqlite')
    client = CollectionClient(writer.homes, writer.path)
    writer.start_live()
    source = session([100]*10 + [35]*3, home=writer.homes[0])
    path = tmp_path/'prepared.sqlite'

    def collect(sequence):
        now = time.time()
        value = empty_snapshot(writer.homes, writer.path, now)
        value.update(sessions=[source], usage_collection_complete=True,
            last_usage_collection_success=now, request_activity=[])
        value['index'].update(loading=False, usage_complete=True)
        writer.publish(value, 'checkpoint-recovery', sequence)
        return read_catalog(client)

    def expected():
        engine = AnalysisEngine()
        engine.ingest([source])
        return classify(state['prepared'] for state in engine.sessions.values())

    try:
        catalog = collect(1)
        with storage.PreparedStore(path) as store:
            list(PreparedCohort(store, client, None).convert(catalog))
            original = store.token
            publisher = PreparedPublisher(0)
            publisher.adopt(store, ())
            with closing(PreparedSpeedWriter(store, preparation=preparation_signature())) as speed:
                cohort = PreparedCohort(store, client, None, speed_writer=speed)
                assert list(cohort.convert(catalog)) == []
                result = cohort.current_speeds()
                recovered = store.token
                assert recovered.generation == original.generation+1
                assert store.changes.between(original, recovered) == (set(), set())
                assert result == expected()
                assert not speed.checkpoint_recovery_needed
                publisher.adopt(store, ())
                delivered = publisher.publish(dict(homes=writer.homes), store, result)
                assert delivered['prepared_token'] == (recovered.instance, recovered.generation)
                assert len(delivered['overlay_sessions']) == 1
                def forbidden(*args, **kwargs):
                    raise AssertionError('completed recovery reread historical rows')
                with monkeypatch.context() as patch:
                    patch.setattr(storage.Snapshot, 'iter_speed_rows', forbidden)
                    assert cohort.current_speeds() == result
                    assert store.token == recovered
            with closing(PreparedSpeedWriter(store, preparation=preparation_signature())) as speed:
                cohort = PreparedCohort(store, client, None, speed_writer=speed)
                assert list(cohort.convert(catalog)) == []
                with monkeypatch.context() as patch:
                    patch.setattr(storage.Snapshot, 'iter_speed_rows', forbidden)
                    assert cohort.current_speeds() == result
                assert store.token == recovered and speed.last_read_rows == 0
                appended(source, [35])
                source['usage_revision'] += 1
                catalog = collect(2)
                assert list(cohort.convert(catalog))[-1]['ready']
                assert speed.tracker.metrics['proof_rows'] == 1
                assert speed.tracker.results == expected()
                source['history'][0]['completion_latency_ms'] *= 2
                source['usage_revision'] += 1
                catalog = collect(3)
                assert list(cohort.convert(catalog))[-1]['ready']
                assert speed.tracker.metrics['replayed_scopes'] == 1
                assert speed.tracker.results == expected()
    finally:
        client.close()
        writer.close()

def test_restored_speed_evidence_shares_validated_keys_and_stays_within_bound(tmp_path):
    from cachemonitor import speed_checkpoint as codec
    from cachemonitor.prepared_ingest import prepare_owner, stage_owner
    from cachemonitor.prepared_store import pack
    from cachemonitor.speed_health import SpeedHealth
    from test_speed_health import session
    from tools.benchmark_prepared_speed import retained_graph_bytes

    peer = session([100]*30, sid='reference')
    for number, row in enumerate(peer['history']):
        row['key'] = 'peer-' + str(number) + '界'*100
    values = [peer] + [session([35]*3+[100], sid=f'owner-{number}', start=5000+number*1000) for number in range(24)]
    values.extend(session([100]*4, sid=f'wide-{number}', model=f'model-{number}-'+'x'*6000) for number in range(4))
    path = tmp_path/'evidence-keys.sqlite'
    with storage.PreparedStore(path) as store:
        stage = store.begin(('key-aliases', 1))
        for ordinal, value in enumerate(values):
            dependency = dict.fromkeys(storage.DEPENDENCIES, 1)
            dependency['source_digest'] = f'{ordinal:064x}'
            dependency['prepared'] = True
            stage_owner(stage, prepare_owner(value, owns=lambda *_:True), dependency, ordinal)
        stage.publish(('key-aliases', 1), ready=True)
        with closing(PreparedSpeedWriter(store, preparation=preparation_signature())) as writer:
            stamps = PreparedCohort(store, None, None, speed_writer=writer)._speed_inventory()
            with store.read_current() as view:
                source = view.source_identity
            encoded = pack(source, store.limits)
            result = writer.current_results(stamps, encoded)
            stage = store.begin(source)
            writer.prepare(stage, stamps, stage.source, stamps)
            writer.publish(stage, source, ready=True)
        with closing(PreparedSpeedWriter(store, preparation=preparation_signature())) as writer:
            restored = writer.current_results(stamps, encoded)
            assert restored == result and writer.last_read_rows == 0
            assert retained_graph_bytes(writer.tracker) <= writer.tracker.metrics['estimated_metadata_bytes']
            for (_, condition), replay in writer.tracker.replays.items():
                assert condition is replay.condition
                assert any(condition is key for proof in writer.tracker.proofs.values() for key in proof.buckets)
            for owner, value in restored.items():
                if not value['active']:
                    continue
                proof = writer.tracker.proofs[owner]
                replay = writer.tracker.replays[(owner[0], proof.condition)]
                state = replay.states[owner]
                assert value['baseline_keys'] is state['baseline']['baseline_keys']
                assert all(a is b['key'] for a, b in zip(value['recent_keys'], state['recent']))
                assert value['call_id'] is state['recent'][-1]['key']
                assert value['incident_id'] is state['incident_id']
                assert state['recovery'][0] is state['recent'][-1]
                assert all(row.condition is replay.condition for row in state['recent'])
            with store.read_current() as view:
                head = view.checkpoint_state(SPEED_CHECKPOINT_NAMESPACE)
                sections = prepared_speed_writer._stored_sections(view, head, len(stamps))
            active_name = codec._owner_name(('h', 'owner-0'))
            index = next(i for i, section in enumerate(sections) if section.name == active_name)
            records = list(codec._records(sections[index], [0]))
            for broken in ('result', 'sample-bound', 'peer-bound'):
                changed = deepcopy(records)
                if broken == 'result':
                    changed[2][1]['baseline_keys'][0] = 'different'
                elif broken == 'sample-bound':
                    for bucket in changed[0][5]:
                        bucket[2] = 1
                else:
                    changed[1][1]['baseline']['baseline_keys'][0] = 'z'*2000
                    changed[2][1]['baseline_keys'][0] = 'z'*2000
                altered = list(sections)
                altered[index] = codec._section(active_name, changed, [0])
                target = SpeedHealth()
                with pytest.raises(codec.CheckpointRejected):
                    codec.restore_checkpoint(target, writer.source, tuple(altered), preparation=preparation_signature())
                assert target.results == {} and target.proofs == {}


def test_checkpoint_batches_rows_with_existing_limits_and_preserves_complete_tombstones(tmp_path,monkeypatch):
    from contextlib import contextmanager
    from hashlib import sha256
    from cachemonitor.prepared_ingest import pages

    limits=replace(storage.Limits(),write_rows=16)
    payloads={'header':b'header',**{f'part-{i:02d}':bytes([i])*64 for i in range(33)}}
    def sections(values):
        return ((name,sha256(payload).hexdigest(),(payload,)) for name,payload in values.items())
    path=tmp_path/'checkpoint-batching.sqlite'
    with storage.PreparedStore(path,limits) as store:
        transactions=[]
        original=store._transaction
        @contextmanager
        def observed():
            before=store.db.total_changes
            with original():yield
            transactions.append(store.db.total_changes-before)
        monkeypatch.setattr(store,'_transaction',observed)
        stage=store.begin(('source',1))
        transactions.clear()
        stage.checkpoint('speed-test',sections(payloads),base=None,
            token=stage.prospective_token,complete=True)
        assert max(transactions)<=limits.write_rows+1
        assert len(transactions)<len(payloads)
        first=stage.publish(('source',1),ready=True)
        with store.read_current() as view:
            assert view.checkpoint_state('speed-test')['sections']==len(payloads)
            for info in pages(view.checkpoint_sections,'speed-test'):
                assert b''.join(row['payload'] for row in pages(
                    view.checkpoint_chunks,'speed-test',info['name']))==payloads[info['name']]
        stage=store.begin(('source',1))
        keep=dict(header=b'updated',**{'part-01':payloads['part-01']})
        stage.checkpoint('speed-test',sections(keep),base=(first.instance,first.generation),
            token=stage.prospective_token,tombstones=('part-02',),complete=True)
        token=stage.publish(('source',1),ready=True)
    with storage.PreparedReader(path,limits) as reader,reader.pin(token) as view:
        assert {info['name'] for info in pages(view.checkpoint_sections,'speed-test')}==set(keep)
        assert view.checkpoint_state('speed-test')['encoded_bytes']==sum(map(len,keep.values()))
        for name,payload in keep.items():
            assert b''.join(row['payload'] for row in pages(
                view.checkpoint_chunks,'speed-test',name))==payload


def test_public_snapshot_speed_input_keeps_its_actual_pin_and_raw_frontiers(tmp_path):
    from types import SimpleNamespace
    from cachemonitor.prepared_ingest import dependencies
    from cachemonitor.prepared_speed import PreparedSpeedInput

    owner=('home','owner')
    source=('scope','source',('home',),('epoch',1))
    rows=[dict(home='home',sid='owner',key=f'call-{ordinal}',ts=100+ordinal,
        session_ordinal=ordinal,model='gpt-6-astra',effort='medium',service_tier='Standard',
        input=80000,cached=72000,output=2000,reasoning=500,response_status='completed',
        timing_valid=True,completion_latency_ms=20000) for ordinal in (1,3)]
    path=tmp_path/'public-speed.sqlite'
    with storage.PreparedStore(path) as store,storage.PreparedReader(path) as reader:
        def publish(revision,values):
            stage=store.begin(source)
            deps=dict(dependencies(str(revision)*64,'mode','ownership'),prepared=True,
                call_count=len(values),speed_frontier=values[-1]['session_ordinal'])
            stage.owner(*owner,dict(home='home',id='owner'),deps,source_ordinal=0)
            stage.calls(owner,values,reconcile=True)
            return stage.publish(source,ready=True)
        token=publish(1,rows)
        with reader.pin(token) as view:
            value=PreparedSpeedInput.from_snapshot(view)
            assert value.binding is reader and value.owners[owner].length==2
            assert value.owners[owner].last_ordinal==3
            publish(2,[dict(row,output=5000) for row in rows])
            with value.read_scope():
                actual=list(value.ordered({owner:0}))
            assert [(identity,ordinal,row['session_ordinal'],row['output'])
                for identity,ordinal,row in actual]==[(owner,0,1,2000),(owner,1,3,2000)]
            assert reader.active is view and not view.closed
            value.close()
            assert reader.active is view and not view.closed
        with store.read_current() as private:
            with pytest.raises(TypeError,match='actual ready public Snapshot'):
                PreparedSpeedInput.from_snapshot(private)
        with pytest.raises(TypeError,match='actual ready public Snapshot'):
            PreparedSpeedInput.from_snapshot(SimpleNamespace(public=True,ready=True))
