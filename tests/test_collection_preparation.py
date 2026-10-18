"""Pending replay recovery stays visible and keeps the collector working."""
from types import SimpleNamespace

import pytest

from cachemonitor.index import UsageIndex
from cachemonitor import usage_collection


@pytest.fixture
def restoring_index(tmp_path):
    index = UsageIndex([], tmp_path/'index.sqlite', tmp_path/'evidence.sqlite')
    index.replay = SimpleNamespace(begin=lambda now: None, pending=set(),
                                   restore_job=iter((None,)), close=lambda: None)
    try:
        yield index
    finally:
        index.close()


def test_replay_recovery_remains_loading_until_restore_finishes(restoring_index):
    index = restoring_index
    pending = index.poll(1000)
    assert not pending['usage_errors'] and not pending['collection_fault']
    assert not pending['usage_collection_complete']
    assert pending['index']['loading']

    index.replay.restore_job = None
    complete = index.poll(1001)
    assert complete['usage_collection_complete']
    assert not complete['index']['loading']


def _next_collector_wait(monkeypatch, snapshot, path):
    clock = [0.0]
    waits = []
    polled = []

    class Changes:
        def __init__(self, *args):
            pass

        def sync(self, homes):
            pass

        def wait(self, deadline):
            waits.append(deadline-clock[0])
            clock[0] = deadline

        def close(self):
            pass

    def poll(**kwargs):
        polled.append(True)
        return snapshot

    channel = SimpleNamespace(scope='fixture', path=path, snapshot_path=path, homes=[])
    service = SimpleNamespace(channel=channel, instance='fixture',
                              stopping=lambda: bool(polled), poll=poll,
                              maintain=lambda: False, close=lambda: None)
    with monkeypatch.context() as patch:
        patch.setattr(usage_collection, 'CollectorService', lambda *args: service)
        patch.setattr('cachemonitor.collection_wake.SourceChanges', Changes)
        patch.setattr('cachemonitor.observer_control.atomic_write', lambda *args: None)
        patch.setattr(usage_collection.time, 'monotonic', lambda: clock[0])
        patch.setattr('sys.argv', ['collector', '--codex-home', str(path.parent),
                                  '--index-path', str(path)])
        usage_collection.main()
    assert len(waits) == 1
    return waits[0]


def test_pending_replay_uses_active_wait_instead_of_idle_wait(restoring_index, monkeypatch):
    index = restoring_index
    pending = index.poll(1000)
    index.replay.restore_job = None
    complete = index.poll(1001)

    pending_wait = _next_collector_wait(monkeypatch, pending, index.path)
    idle_wait = _next_collector_wait(monkeypatch, complete, index.path)
    assert pending_wait < idle_wait
