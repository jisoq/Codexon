"""The native projections must preserve accounting and selectable source records."""
import math
import random
from types import SimpleNamespace

import pytest

from cachemonitor.performance_trends import PerformanceTrends


def assert_projection_equal(actual, expected, path='view'):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), path
        for key, value in expected.items():
            assert_projection_equal(actual[key], value, f'{path}.{key}')
    elif isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected), path
        for index, value in enumerate(expected):
            assert_projection_equal(actual[index], value, f'{path}[{index}]')
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12), path
    else:
        assert actual == expected, path


def fixture_rows():
    rng = random.Random(624)
    rows = []
    for i in range(900):
        output = rng.randrange(100, 4000)
        elapsed = rng.randrange(1, 3000)
        rows.append(dict(ts=1800000000 + (i // 3) * 311, home='test' if i % 3 else '',
            sid=str(i % 17), key=str(i), turn='t', model='m' + str(i % 2),
            service_tier='Fast' if i % 7 else 'Standard', effort='high' if i % 5 else 'low',
            cost=rng.random(), input=1000 + i, cached=i, input_conflict=i % 29 == 0,
            output=output, reasoning=output // 2, duration=elapsed / 1000,
            completion_latency_ms=elapsed, output_speed=output * 1000 / elapsed))
    invalid = [None, True, False, float('nan'), float('inf'), -1, '9']
    for i, value in enumerate(invalid):
        for key in ('cost', 'input', 'cached', 'output', 'duration', 'reasoning', 'output_speed'):
            rows[80 + i][key] = value
    rows[101]['cost'] = 100
    rows[151]['purpose'] = 'maintenance'
    rows[152]['requested_model'] = 'codex-auto-review'
    # Zero/invalid elapsed is covered by rolling population filtering. The daily
    # projector receives output_speed already validated by analytics.output_speed.
    rows[90]['completion_latency_ms'] = 0
    rows[90]['output_speed'] = None
    rows[91]['model'] = None
    rows[92]['effort'] = None
    return rows


@pytest.fixture
def native():
    return pytest.importorskip('cachemonitor._cpu_native')


@pytest.mark.parametrize('granularity', ['day', 'week', 'month', 'auto'])
@pytest.mark.parametrize('time_range', [None, [1800010000, 1800060000], [1800000000, 1800001800]])
def test_native_trend_projection_matches_reference(native, monkeypatch, granularity, time_range):
    rows = fixture_rows()
    source = SimpleNamespace(revision=1, sessions={'s': {'prepared': {'history': rows}}})
    query = dict(now=1800100000, plot_width=64, granularity=granularity, time_range=time_range)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    expected = PerformanceTrends().query(source, query)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'rust')
    actual = PerformanceTrends().query(source, query)
    assert_projection_equal(actual, expected)


def test_native_causal_baseline_does_not_include_same_timestamp_or_future(native, monkeypatch):
    rows = [dict(ts=1000 + i, home='h', sid='s', key=str(i), model='m', effort='high',
                 service_tier='Standard', cost=1.) for i in range(70)]
    rows.extend(dict(rows[-1], key=f'same-{i}', ts=1070, cost=100.) for i in range(70))
    rows.append(dict(rows[-1], key='next', ts=1071, cost=1.))
    source = SimpleNamespace(revision=1, sessions={'s': {'prepared': {'history': rows}}})
    query = dict(now=1100, plot_width=64, time_range=[1060, 1070])
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    expected = PerformanceTrends().query(source, query)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'rust')
    actual = PerformanceTrends().query(source, query)
    assert_projection_equal(actual, expected)
    cost = next(panel for panel in actual['panels'] if panel['key'] == 'cost')
    anomaly = next(point for point in cost['points'] if point.get('count'))
    assert anomaly['count'] == 70 and anomaly['baseline'] == 1


def test_native_population_rejects_bool_and_numeric_subclasses(native):
    class IntSubclass(int):
        pass
    rows = [dict(home='h', sid='s', cost=value) for value in
            [1, 3., True, IntSubclass(5), None, math.nan, math.inf, -1]]
    summary, populations = native.trend_populations(rows, 'cost', [(0, len(rows), 0, len(rows))])
    assert summary == (2, 8, 1, 3)
    eligible, total, amounts, distribution, mean, support, sessions = populations[0]
    assert (eligible, total, mean, support, sessions) == (2, 8, 2., True, 1)
    assert amounts[0] == 4 and distribution[:3] == (2., 1.5, 2.5)


def test_native_window_validation_is_a_python_error(native):
    with pytest.raises(ValueError, match='population'):
        native.trend_populations([], 'cost', [(0, 1, 0, 0)])
    with pytest.raises(ValueError, match='positive'):
        native.trend_samples([], 'cost', 1, 1, 64)


@pytest.mark.parametrize('granularity', ['day', 'auto'])
def test_native_projection_preserves_arbitrary_integer_totals(native, monkeypatch, granularity):
    rows = fixture_rows()[:50]
    for row in rows:
        row.update(input=2**60 + 1, cached=2**59 + 1, output=2**60 + 3, reasoning=2**59 + 7)
    source = SimpleNamespace(revision=1, sessions={'s': {'prepared': {'history': rows}}})
    query = dict(now=1800100000, plot_width=64, granularity=granularity)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'python')
    expected = PerformanceTrends().query(source, query)
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'rust')
    assert_projection_equal(PerformanceTrends().query(source, query), expected)
