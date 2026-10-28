"""Notifications keep owner identity, evidence floors and generation isolation."""
from copy import deepcopy
from threading import Event

import pytest

from cachemonitor import prepared_store as storage
from cachemonitor.prepared_health import notification_health, split_summary


OWNER = ('home', 'child')


def publish_health(store, revision, *, title='Child', incident=True):
    events = [dict(ts=stamp, key=f'call-{stamp}') for stamp in (99, 100, 101)]
    own = dict(home=OWNER[0], id=OWNER[1], title=title,
               cache_misses=dict(count=3, events=events),
               cache_health=dict(incident=dict(id='incident', ts=101) if incident else None))
    summary, sections = split_summary(dict(own=own))
    source = ('source', revision)
    stage = store.begin(source)
    dependency = dict.fromkeys(storage.DEPENDENCIES, 1)
    dependency['source_digest'] = f'{revision:064x}'
    stage.owner(*OWNER, dict(home=OWNER[0], id=OWNER[1], title=title),
                dependency, source_ordinal=0, summary=summary)
    stage.calls(OWNER, [], reconcile=True)
    stage.sections(OWNER, sections, reconcile=True)
    token = stage.publish(source, ready=True)
    return summary['own'], (token.instance, token.generation)


def test_notifications_keep_exact_own_incident_and_strict_evidence_floor(tmp_path):
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        own, revision = publish_health(store, 1)
        original = deepcopy(own)
        with store.read_current() as snapshot:
            result = notification_health(snapshot, OWNER, own=own, revision=revision, since=100)
        assert result == dict(home='home', id='child', title='Child',
                              cache_misses=dict(count=3, events=[dict(ts=101, key='call-101')]),
                              cache_health=dict(incident=dict(id='incident', ts=101)))
        assert own == original
        renamed, revision = publish_health(store, 2, title='Renamed child', incident=False)
        with store.read_current() as snapshot:
            result = notification_health(snapshot, OWNER, own=renamed, revision=revision, since=101)
        assert result['title'] == 'Renamed child'
        assert result['cache_misses'] == dict(count=3, events=[])
        assert result['cache_health'] == dict(incident=None)


def test_notification_head_rejects_another_owner_or_generation(tmp_path):
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        own, revision = publish_health(store, 1)
        with store.read_current() as snapshot:
            with pytest.raises(storage.RebuildRequired):
                notification_health(snapshot, ('home', 'parent'), own=own, revision=revision, since=0)
            for wrong in ((revision[0], revision[1]-1), ('another-store', revision[1])):
                with pytest.raises(storage.StaleGeneration):
                    notification_health(snapshot, OWNER, own=own, revision=wrong, since=0)
        publish_health(store, 2)
        with store.read_current() as snapshot:
            with pytest.raises(storage.StaleGeneration):
                notification_health(snapshot, OWNER, own=own, revision=revision, since=0)


def test_notification_cancellation_does_not_leave_a_pinned_reader(tmp_path):
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        own, revision = publish_health(store, 1)
        cancelled = Event()
        with store.read_current(cancel=cancelled) as snapshot:
            cancelled.set()
            with pytest.raises(storage.ReadCancelled):
                notification_health(snapshot, OWNER, own=own, revision=revision, since=0)
        with store.read_current() as snapshot:
            assert len(notification_health(snapshot, OWNER, own=own, revision=revision, since=0)
                       ['cache_misses']['events']) == 3


@pytest.mark.parametrize('selected', [[('home', 'a')], []], ids=['selected-a', 'aggregate-empty'])
@pytest.mark.parametrize('resolution', ['corrected', 'recovered'])
def test_publication_preserves_unselected_cache_incident_lifecycle(tmp_path, selected, resolution):
    from cachemonitor.notifications import ConfirmedNotifications
    from cachemonitor.prepared_delivery import PreparedPublisher
    from cachemonitor.prepared_ingest import prepare_owner, stage_owner
    from cachemonitor.prepared_summary import summary_for_storage
    from test_data_contract import source

    notifications = ConfirmedNotifications(wall_clock=lambda: 100)
    publisher = PreparedPublisher(100)
    owner = ('home', 'b')

    with storage.PreparedStore(tmp_path/'notifications.sqlite') as store:
        def commit(revision, *, incident=None, title='B'):
            stage = store.begin(('source', revision))
            for ordinal, sid in enumerate(('a', 'b')):
                if revision != 1 and sid != 'b':
                    continue
                value = source()
                value.update(id=sid, title=title if sid == 'b' else 'A')
                material = prepare_owner(value, owns=lambda *_: True)
                key = stage_owner(stage, material,
                    dict(dict.fromkeys(storage.DEPENDENCIES, revision), source_digest=f'{revision:064x}'), ordinal)
                own = dict(material['summary']['own'],
                    cache_misses=dict(count=3, events=[
                        dict(ts=stamp, key=f'{sid}-{stamp}') for stamp in (99, 100, 101)]),
                    cache_health=dict(incident=incident if sid == 'b' else None, reason='cache evidence'))
                summary, sections = split_summary(dict(material['summary'], own=own))
                stage.derived(key, summary=summary_for_storage(summary))
                stage.sections(key, sections, reconcile=True)
            return stage.publish(('source', revision), ready=True)

        commit(1)
        publisher.adopt(store, [('home', 'a'), owner], selected=selected)
        initial = publisher.publish(dict(homes=['home']), store)
        assert notifications.cache(initial['cache_candidates']) == []

        incident = dict(id='b-incident', ts=101, state='cache anomaly',
                        count=3, latest_key='b-101', resolved=False)
        commit(2, incident=incident)
        publisher.adopt(store, [owner], selected=selected)
        current = publisher.publish(dict(homes=['home']), store)
        assert set(publisher.owners) == set(selected)
        assert {row['id'] for row in current['sessions']} <= {key[1] for key in selected}
        visible = notifications.cache(current['cache_candidates'])
        assert [(row['home'], row['sid'], row['incident_id']) for row in visible] == [
            ('home', 'b', 'b-incident')]
        assert ('home', 'b', 'b-99') not in notifications.cache_seen
        assert ('home', 'b', 'b-100') not in notifications.cache_seen
        assert ('home', 'b', 'b-101') in notifications.cache_seen
        record = visible[0]

        commit(3, incident=dict(incident, resolved=True) if resolution == 'recovered' else None,
               title='Renamed B')
        publisher.adopt(store, [owner], selected=selected)
        final = publisher.publish(dict(homes=['home']), store)
        assert notifications.cache(final['cache_candidates']) == []
        expected = '회복 확인' if resolution == 'recovered' else '기록 정정: 이상 판정 해제'
        assert record['resolution'] == expected
        assert len(notifications.records) == 1
