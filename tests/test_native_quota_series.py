"""Native preparation retains gap boundaries, exact indices and completed costs."""
import random

import pytest

from cachemonitor.quota_view import completed_percent_costs, prepare_series


@pytest.fixture
def native():
    return pytest.importorskip('cachemonitor._cpu_native')


def rows_for(count):
    rng = random.Random(773)
    rows = []
    for i in range(count):
        cycle = i // 250
        part = i % 250
        cost = part / 10
        row = dict(at=1800000000 + i * 10, remaining=100 - part // 3,
            cycle_cost=cost, cycle_value=rng.randrange(100, 400), connect=i % 71 != 0,
            reset_kind='manual' if part == 0 else None, cycle_start=cycle, account='account',
            tracking_continuous=True)
        if i % 59 == 0:
            row['cycle_cost'] = None
        if i % 61 == 0:
            row['cycle_value'] = None
        if i % 67 == 0:
            row['completed_cost'] = None
        if i % 89 == 0:
            row['completed_cost'] = 2.
        if i % 97 == 0:
            row['reported_remaining'] = row['remaining']
            row['remaining'] = float(i)
            row['period_cost'] = row['cycle_cost']
        rows.append(row)
    return rows


@pytest.mark.parametrize('count', [0, 1, 512, 513, 1536, 1537, 5000])
def test_native_quota_series_matches_reference(native, monkeypatch, count):
    rows = rows_for(count)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    expected = prepare_series(rows)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'rust')
    actual = prepare_series(rows)
    assert actual == expected
    assert actual['rows'] is rows
    if count==1537:
        import pickle
        from cachemonitor.quota_cycles import _PackedRows
        packed=_PackedRows(rows)
        assert prepare_series(packed)==expected
        for index in (0,512,1024,1536,-1):assert packed[index]==rows[index]
        assert packed[510:514]==rows[510:514]
        assert len(packed._cache)<=2
        restored=pickle.loads(pickle.dumps(packed))
        assert not restored._cache and restored==rows
        assert prepare_series(restored)==expected


def test_native_completed_costs_hold_idle_plateau_and_preserve_account_boundary(native, monkeypatch):
    rows = [dict(at=i * 10, remaining=remaining, cycle_cost=cost, cycle_value=None,
        connect=connect, tracking_continuous=True, reset_kind=None, account=account)
        for i, (remaining, cost, connect, account) in enumerate([
            (100, 0, False, 'a'), (100, 1, True, 'a'), (99, 2, True, 'a'),
            (99, 2 + 1e-12, False, 'a'), (98, 3, True, 'a'), (97, 4, True, 'b')])]
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    expected = prepare_series(rows)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'rust')
    assert prepare_series(rows) == expected
    from cachemonitor.quota_cycles import _PackedRows
    assert prepare_series(_PackedRows(rows)) == expected
    assert expected['completed_costs'] == [2, 2, 1, 1, None, None]


def test_native_quota_series_preserves_arbitrary_integer_amounts(native, monkeypatch):
    rows = rows_for(10)
    for index, row in enumerate(rows):row['cycle_cost'] = 2**60 + index
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    expected = prepare_series(rows)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'rust')
    assert prepare_series(rows) == expected


def test_native_standalone_completion_ignores_display_overrides_and_needs_no_chart_fields(native, monkeypatch):
    rows = [dict(remaining=100, cycle_cost=0, connect=False, completed_cost=500),
            dict(remaining=100, cycle_cost=1, connect=True, completed_cost=None),
            dict(remaining=99, cycle_cost=3, connect=True)]
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    expected = completed_percent_costs(rows)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'rust')
    assert completed_percent_costs(rows) == expected == [3, 3, None]
