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
