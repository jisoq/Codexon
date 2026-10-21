"""Pending replay recovery stays visible and keeps the collector working."""
import json
import sqlite3
from time import monotonic
from types import SimpleNamespace

import pytest

from cachemonitor import index as index_module
from cachemonitor import usage_collection
from cachemonitor.index import UsageIndex
from test_core import TID, fixture_home, usage


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


def _collect_complete(service, now):
    deadline = monotonic() + 5
    while True:
        snapshot = service.poll(now)
        assert not snapshot['usage_errors'] and not snapshot['collection_fault']
        if snapshot['usage_collection_complete']:
            assert not snapshot['index']['loading']
            return snapshot
        assert monotonic() < deadline, 'Replayed owner never completes after metadata invalidation'


@pytest.mark.parametrize('during_rebuild', ['unchanged', 'metadata', 'source'])
def test_replayed_owner_metadata_rebuild_completes(tmp_path, monkeypatch, during_rebuild):
    home, path = fixture_home(tmp_path)
    with path.open('a', encoding='utf8') as stream:
        for number in range(24):
            stream.write(json.dumps(usage(f'row-{number}', time=10001+number)) + '\n')
    target = tmp_path/'index.sqlite'
    evidence = tmp_path/'evidence.sqlite'
    service = usage_collection.CollectorService([home], target, evidence)
    try:
        before = _collect_complete(service, 20000)['sessions'][0]['history']
        assert len(before) == 25
        assert [(row['input'], row['cached'], row['output'], row['total']) for row in before] == [
            (10000, 9000, 10, 10010)] * 25
        deadline = monotonic() + 5
        while service.maintain():
            assert monotonic() < deadline
    finally:
        service.close()

    service = usage_collection.CollectorService([home], target, evidence)
    try:
        assert _collect_complete(service, 20001)['sessions'][0]['history'] == before
        assert hasattr(service.index.monitor.sessions[(str(home), TID)], '_replay_body')
        cwd = 'D:/changed-project'
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET cwd=?', (cwd,))
        clock = [0.0]
        replayed = set()
        consume = index_module.Session.consume

        def consume_with_budget(session, event, now):
            consume(session, event, now)
            clock[0] += .12
            replayed.add(event.get('payload', {}).get('response_id'))

        with monkeypatch.context() as patch:
            patch.setattr(index_module, 'REBUILD_BATCH', 4)
            patch.setattr(index_module.time, 'monotonic', lambda: clock[0])
            patch.setattr(index_module.Session, 'consume', consume_with_budget)
            now = 20020
            deadline = monotonic() + 5
            while not clock[0]:
                pending = service.poll(now)
                assert not pending['usage_collection_complete']
                assert monotonic() < deadline
            if during_rebuild == 'metadata':
                while 'row-22' not in replayed:
                    pending = service.poll(now)
                    assert not pending['usage_collection_complete']
                    assert monotonic() < deadline
                cwd = 'D:/latest-project'
                with sqlite3.connect(home/'state_5.sqlite') as db:
                    db.execute('UPDATE threads SET cwd=?', (cwd,))
                now += 20
            elif during_rebuild == 'source':
                path.write_text(json.dumps(usage('rewritten', input=200, cached=50)) + '\n', encoding='utf8')
            complete = _collect_complete(service, now)
        assert len(complete['sessions']) == 1
        session = complete['sessions'][0]
        assert session['collection_complete']
        assert session['cwd'] == cwd
        if during_rebuild == 'source':
            assert [(row['key'], row['input'], row['cached'], row['output'], row['total'])
                    for row in session['history']] == [('rewritten', 200, 50, 10, 210)]
        else:
            assert session['history'] == before
        published = service.channel.read(shared=True)
        assert published['usage_collection_complete']
        assert published['sessions'][0]['cwd'] == cwd
        assert published['sessions'][0]['history'] == session['history']
    finally:
        service.close()
