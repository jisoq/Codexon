import threading

import pytest

from cachemonitor.shutdown_state import ExitCancelled, ExitControl


def test_accepted_cancellation_prevents_cutover_and_force():
    control = ExitControl()
    persisted = []
    assert control.request_cancel()
    assert not control.request_force()
    with pytest.raises(ExitCancelled):
        control.checkpoint()
    with pytest.raises(ExitCancelled):
        control.commit(lambda: persisted.append(True))
    assert control.cancelled and not control.committed and not persisted


def test_durable_cutover_rejects_cancellation_and_is_idempotent():
    control = ExitControl()
    persisted = []
    control.commit(lambda: persisted.append(True))
    control.commit(lambda: pytest.fail('cutover persisted twice'))
    assert control.committed and not control.request_cancel()
    assert not control.cancelled and persisted == [True]
    assert control.request_force() and control.force_requested
    control.checkpoint()


def test_failed_persistence_does_not_claim_cutover():
    control = ExitControl()

    def fail():
        raise OSError('disk unavailable')

    with pytest.raises(OSError, match='disk unavailable'):
        control.commit(fail)
    assert not control.committed
    assert control.request_cancel()


def test_cancellation_competing_with_cutover_has_one_winner():
    control = ExitControl()
    start = threading.Barrier(3)
    outcomes = {}
    persisted = []

    def cancel():
        start.wait(timeout=5)
        outcomes['cancel'] = control.request_cancel()

    def commit():
        start.wait(timeout=5)
        try:
            control.commit(lambda: persisted.append(True))
            outcomes['commit'] = True
        except ExitCancelled:
            outcomes['commit'] = False

    workers = [threading.Thread(target=cancel), threading.Thread(target=commit)]
    for worker in workers:
        worker.start()
    start.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()
    assert outcomes['cancel'] != outcomes['commit']
    assert control.committed == outcomes['commit'] == bool(persisted)
    assert control.cancelled == outcomes['cancel']


def test_cancel_cannot_report_success_during_durable_commit():
    control = ExitControl()
    persisting = threading.Event()
    finish_persistence = threading.Event()
    cancel_started = threading.Event()
    outcomes = []

    def persist():
        persisting.set()
        assert finish_persistence.wait(timeout=5)

    def cancel():
        cancel_started.set()
        outcomes.append(control.request_cancel())

    commit_worker = threading.Thread(target=lambda: control.commit(persist))
    cancel_worker = threading.Thread(target=cancel)
    commit_worker.start()
    try:
        assert persisting.wait(timeout=5)
        cancel_worker.start()
        assert cancel_started.wait(timeout=5)
    finally:
        finish_persistence.set()
        commit_worker.join(timeout=5)
        if cancel_worker.ident is not None:
            cancel_worker.join(timeout=5)
    assert not commit_worker.is_alive() and not cancel_worker.is_alive()
    assert control.committed and outcomes == [False]
