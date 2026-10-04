from cachemonitor.quota_tracking import Observation, TrackingState


def obs(sequence, remaining, at=None, epoch=('account', 'week')):
    at = sequence * 10 if at is None else at
    return Observation(sequence, at, at + 1, remaining, epoch)


def test_out_of_order_and_pre_activation_observations_do_not_change_totals():
    state = TrackingState(15)
    assert not state.observe(obs(1, 90))
    assert state.observe(obs(2, 70))
    assert not state.observe(obs(1, 10, at=25))
    assert not state.observe(Observation(3, 25, 24, 60, ('account','week')))
    assert state.endpoint.remaining == 70
