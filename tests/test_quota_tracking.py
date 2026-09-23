from cachemonitor.quota_tracking import Observation, TrackingState


def obs(sequence, remaining, at=None, epoch=('account', 'week')):
    at = sequence * 10 if at is None else at
    return Observation(sequence, at, at + 1, remaining, epoch)


def test_first_observation_starts_even_when_active_or_activity_unknown():
    for active in (True, False, None):
        state = TrackingState(0)
        state.activity(active, 1)
        state.observe(obs(1, 70))
        assert state.baseline.remaining == 70
        state.observe(obs(2, 69.9))
        assert state.endpoint.remaining == 69.9


def test_all_account_consumption_updates_even_without_local_work():
    state = TrackingState(0)
    state.activity(False, 1)
    for i, remaining in enumerate((70, 70, 68, 68, 64), 1):
        assert state.observe(obs(i, remaining))
    assert state.baseline.remaining - state.endpoint.remaining == 6
    assert len(state.observations) == 5
    assert not state.closed


def test_out_of_order_and_pre_activation_observations_do_not_change_totals():
    state = TrackingState(15)
    assert not state.observe(obs(1, 90))
    assert state.observe(obs(2, 70))
    assert not state.observe(obs(1, 10, at=25))
    assert not state.observe(Observation(3, 25, 24, 60, ('account','week')))
    assert state.endpoint.remaining == 70


def test_observed_increase_starts_new_segment_without_erasing_old_measurements():
    state = TrackingState(0)
    for i, remaining in enumerate((70, 68, 69, 68, 67), 1):
        state.observe(obs(i, remaining))
    assert state.closed[0]['reason']=='arbitrary_reset'
    assert state.closed[0]['start'].remaining-state.closed[0]['end'].remaining==2
    assert state.baseline.remaining-state.endpoint.remaining==2
    assert [r.remaining for r in state.observations]==[69,68,67]


def test_epoch_change_keeps_old_sum_and_starts_new_baseline():
    state = TrackingState(0)
    state.observe(obs(1, 70));state.observe(obs(2, 68))
    state.observe(obs(3, 100, epoch=('account','next_week')))
    state.observe(obs(4, 99, epoch=('account','next_week')))
    assert state.closed[0]['start'].remaining - state.closed[0]['end'].remaining == 2
    assert state.baseline.remaining - state.endpoint.remaining == 1


def test_zero_and_exhausted_are_real_readings_not_wait_states():
    state = TrackingState(0)
    state.observe(obs(1, 0));state.observe(obs(2, 0))
    assert state.baseline.remaining == state.endpoint.remaining == 0
    assert state.phase == 'on'


def test_local_collector_failure_cannot_discard_account_observations():
    state = TrackingState(0)
    state.observe(obs(1, 70))
    state.activity(None, 12)
    state.observe(obs(2, 68))
    assert state.baseline.remaining - state.endpoint.remaining == 2
