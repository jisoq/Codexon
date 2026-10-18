"""Delivery contracts for completed and superseded prepared reads."""
from contextlib import contextmanager
from threading import Event

import pytest

from cachemonitor.analysis_engine import retained_bytes
from cachemonitor.analysis_query_actor import QueryActor


def overlay_reply(request_id, payload=''):
    return dict(kind='overlay_detail', id=request_id,
                summary=dict(home='home', id=request_id, payload=payload))


@contextmanager
def query_actor(handle):
    class Handler:
        def __call__(self, message, token, cancel):
            return handle(message, cancel)

        def close(self):
            pass

    actor = QueryActor(Handler)
    try:
        yield actor
    finally:
        actor.close()


def wait_for_completion(actor):
    # Observe the actor's completion boundary without draining retained delivery.
    with actor.condition:
        assert actor.condition.wait_for(
            lambda: actor.active is None and not actor.pending, timeout=5)


def test_completed_overlay_survives_new_selection_and_drains_once():
    started, release = Event(), Event()

    def handle(message, cancel):
        if message['id'] == 'B':
            started.set()
            assert release.wait(5)
        return overlay_reply(message['id'])

    with query_actor(handle) as actor:
        try:
            actor.submit(dict(kind='overlay_detail', id='A'), None)
            wait_for_completion(actor)
            actor.submit(dict(kind='overlay_detail', id='B'), None)
            assert started.wait(5)
            assert [value['id'] for value in actor.drain()] == ['A']
            release.set()
            wait_for_completion(actor)
            assert [value['id'] for value in actor.drain()] == ['B']
            assert actor.drain() == []
        finally:
            release.set()


def test_active_superseded_overlay_remains_cancelled():
    started, release = Event(), Event()
    cancelled = []

    def handle(message, cancel):
        if message['id'] == 'A':
            started.set()
            assert release.wait(5)
            cancelled.append(cancel())
        return overlay_reply(message['id'])

    with query_actor(handle) as actor:
        try:
            actor.submit(dict(kind='overlay_detail', id='A'), None)
            assert started.wait(5)
            actor.submit(dict(kind='overlay_detail', id='B'), None)
            release.set()
            wait_for_completion(actor)
            assert cancelled == [True]
            assert [value['id'] for value in actor.drain()] == ['B']
        finally:
            release.set()


@pytest.mark.parametrize('byte_limited', [False, True])
def test_completed_overlay_retention_evicts_oldest_with_bounded_delivery(monkeypatch, byte_limited):
    import cachemonitor.analysis_query_actor as module

    payload = 'x' * 2000
    size = retained_bytes(overlay_reply('00', payload), 100000)
    if byte_limited:
        monkeypatch.setattr(module, 'RESULT_BYTES', size * 2 - 1)
    with query_actor(lambda message, cancel: overlay_reply(message['id'], payload)) as actor:
        for index in range(10):
            actor.submit(dict(kind='overlay_detail', id=f'{index:02}'), None)
            wait_for_completion(actor)
        values = actor.drain()
        assert [value['id'] for value in values] == (
            ['09'] if byte_limited else [f'{index:02}' for index in range(2, 10)])
        assert sum(retained_bytes(value, module.RESULT_BYTES) for value in values) <= module.RESULT_BYTES
        assert actor.drain() == []


def test_completed_query_keeps_latest_only_delivery():
    with query_actor(lambda message, cancel: dict(kind='result', id=message['id'])) as actor:
        actor.submit(dict(kind='query', id='A'), None)
        wait_for_completion(actor)
        actor.submit(dict(kind='query', id='B'), None)
        wait_for_completion(actor)
        assert actor.drain() == [dict(kind='result', id='B')]


@pytest.mark.parametrize('kind', [None, 'overlay_detail'])
def test_cancellation_retires_completed_overlay_delivery(kind):
    with query_actor(lambda message, cancel: overlay_reply(message['id'])) as actor:
        actor.submit(dict(kind='overlay_detail', id='A'), None)
        wait_for_completion(actor)
        actor.cancel(kind)
        assert actor.drain() == []
