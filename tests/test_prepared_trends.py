from contextlib import contextmanager
from sys import getsizeof
from types import SimpleNamespace

import pytest

from cachemonitor import prepared_trends as trends
from cachemonitor.prepared_store import CapacityExceeded


def rows(count=72, shape='mixed'):
    result = []
    for index in range(count):
        result.append(dict(ts=1800000000. + index // 2, _owner=index % 5,
            model='gpt-6-astra', service_tier='Standard', effort='high' if index % 2 else 'low',
            duration=None if index % 11 == 0 else index % 4,
            output_speed=None if index % 13 == 0 else index % 7 / 3,
            output=index % 7, completion_latency_ms=0 if index % 17 == 0 else 3000,
            cost=None if index % 19 == 0 else float(index % 3),
            input=2**80 + 1 if index == 1 else None if index % 23 == 0 else index % 8,
            cached=0 if index % 3 else 9, input_conflict=index % 29 == 0,
            reasoning=None if index % 7 == 0 else index))
    if count > 4:
        result[2].update(input=0.0, cached=0.0, duration=0.0, cost=0, output_speed=0.0)
        result[3].update(input=True, cached=False, duration=True)
        result[4].update(duration=-0.0, cost=-0.0, input=-0.0, output_speed=-0.0)
    for index, row in enumerate(result):
        if shape == 'ordinary':
            row['_owner'] = 0
        elif shape == 'sessions':
            row.update(_owner=index, input=index + 1, cached=index)
        elif shape == 'huge':
            row.update(input=2**(80 + index % 100) + index, cached=2**70 + index,
                cost=2**(64 + index % 70), duration=2**(64 + index % 70))
        elif shape == 'scopes':
            row['effort'] = 'scope-' + str(index)
    return result


def exact_scalars(value):
    if type(value) is float:return ('float', value.hex())
    if isinstance(value, dict):return {key:exact_scalars(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):return [exact_scalars(child) for child in value]
    return (type(value).__name__, value)


def window_for(data):
    budget = trends.Budget(16 * 1024 * 1024)
    window = trends.Window(budget, lambda: None)
    for row in data:
        window.append(row, row['effort'])
    return budget, window


def reference_populations(members, budget, metrics, check):
    for metric in metrics:
        field = 'reasoning' if metric.startswith('reasoning:') else metric
        with budget.scratch() as reserve:
            reserve(512)
            values, sessions = [], {}
            calls = 0
            support = metric == 'count'
            numerator, seconds = trends._Sum(), trends._Sum()
            for block, position, supported in members():
                calls += 1
                owner = block.owners[position]
                if owner not in sessions:
                    reserve(trends._size(owner) + 240)
                    sessions[owner] = [0, 0, 0] if metric == 'session_cache' else None
                if metric == 'count':continue
                if metric == 'session_cache':
                    amount, cached = (block.values[name].at(position) for name in ('input', 'cached'))
                    if amount is None or cached is None or cached > amount or block.conflicts[position]:continue
                    totals = sessions[owner]
                    before = sum(map(getsizeof, totals))
                    totals[0] += amount;totals[1] += cached;totals[2] += 1
                    reserve(max(0, sum(map(getsizeof, totals)) - before))
                    if amount and supported:support = True
                    continue
                value = block.values[field].at(position)
                if value is None:continue
                if metric == 'output_speed':
                    output, elapsed = (block.values[name].at(position) for name in ('output', 'completion_latency_ms'))
                    if output is None or elapsed is None or elapsed <= 0:continue
                    numerator.add(output);seconds.add(elapsed / 1000)
                reserve(trends._size(value) + 24)
                values.append(value)
                if supported:support = True
            if metric == 'session_cache':
                amount, cached, valid_calls = trends._Sum(), trends._Sum(), 0
                for own_input, own_cached, own_calls in sessions.values():
                    if not own_input:continue
                    value = 100 * own_cached / own_input
                    reserve(trends._size(value) + 24)
                    values.append(value)
                    amount.add(own_input);cached.add(own_cached);valid_calls += own_calls
                total = len(sessions)
                totals = dict(input=amount.value(), cached=cached.value(), valid_calls=valid_calls)
            elif metric == 'output_speed':
                total = calls;totals = dict(output=numerator.value(), seconds=seconds.value())
            else:
                total = calls;totals = dict(sum=calls if metric == 'count' else sum(values))
            mean = (calls if metric == 'count' else
                totals['output'] / totals['seconds'] if metric == 'output_speed' and totals['seconds'] else
                None if metric == 'output_speed' else sum(values) / len(values) if values else None)
            yield metric, dict(eligible=calls if metric == 'count' else len(values), total=total, totals=totals,
                distribution=trends.distribution_statistics(values), mean=mean, support=support,
                sessions=len(sessions), calls=calls)


@pytest.mark.parametrize('support', [(1800000000., 1800000036.),
    (1800000010., 1800000020.), (1800000040., 1800000050.)])
def test_populations_preserve_exact_values_types_weights_and_support(support):
    budget, window = window_for(rows())
    members = lambda: window.entries(1800000000., 1800000036., True, {'low', 'high'}, *support)
    metrics = [metric for metric, _, _ in trends.METRICS] + ['count']
    expected = dict(reference_populations(members, budget, metrics, window.check))
    retained = budget.used
    actual = dict(trends.populations(members, budget, metrics, window.check))
    assert exact_scalars(actual) == exact_scalars(expected)
    assert actual['count']['calls'] == 72
    assert actual['input']['distribution']['maximum'] == 2**80 + 1
    assert type(actual['input']['distribution']['maximum']) is int
    assert type(actual['input']['totals']['sum']) is float
    assert actual['duration']['eligible'] == 64
    assert actual['output_speed']['totals']['seconds'] == 186.0
    assert budget.used == retained


@pytest.mark.parametrize('count', [255, 256, 257])
@pytest.mark.parametrize('shape', ['ordinary', 'sessions', 'huge', 'scopes'])
def test_populations_fit_the_exact_baseline_peak(count, shape):
    data = rows(count, shape)
    budget, window = window_for(data)
    scopes = {row['effort'] for row in data}
    start, end = data[0]['ts'], data[-1]['ts']
    members = lambda: window.entries(start, end, True, scopes, start, end)
    metrics = [metric for metric, _, _ in trends.METRICS] + ['count']
    expected = dict(reference_populations(members, budget, metrics, window.check))
    baseline_peak, baseline_scratch = budget.peak, budget.scratch_peak
    retained = budget.used
    budget.limit = baseline_peak
    budget.peak, budget.scratch_peak = retained, 0
    actual = dict(trends.populations(members, budget, metrics, window.check))
    assert exact_scalars(actual) == exact_scalars(expected)
    assert budget.used == retained
    assert budget.peak <= baseline_peak
    assert budget.scratch_peak <= baseline_scratch


@pytest.mark.parametrize('inclusive', [False, True])
def test_support_position_bounds_keep_ties_and_inclusive_endpoints(inclusive):
    data = rows(257, 'scopes')
    _, window = window_for(data)
    start, end = data[2]['ts'], data[-2]['ts']
    support = (data[16]['ts'], data[30]['ts'])
    actual = [(block.values['ts'].at(position), supported) for block, position, supported in
        window.entries(start, end, inclusive, {row['effort'] for row in data}, *support)]
    expected = [(row['ts'], support[0] <= row['ts'] <= support[1]) for row in data
        if start <= row['ts'] and (row['ts'] <= end if inclusive else row['ts'] < end)]
    assert actual == expected


def test_numeric_and_block_append_deltas_equal_full_sizes():
    numbers = trends.Numbers()
    values = [None, True, 0, -0.0, 2**53 + 1, -(2**63), 2**63 - 1,
        2**63, -(2**80), 2**300 + 1] * 27
    for value in values:
        before = numbers.size()
        assert numbers.append(value) == numbers.size() - before
    assert type(numbers.at(3)) is float and numbers.at(3).hex() == '-0x0.0p+0'
    for count in (255, 256):
        block = trends.Block()
        for index, row in enumerate(rows(count, 'huge')):
            before = block.size()
            assert block.append(row, ('scope', index)) == block.size() - before
    budget, window = window_for(rows(257, 'scopes'))
    assert budget.used == sum(block.size() + 16 for block in window.blocks)
    window.discard(1800000200.)
    assert budget.used == 0


def test_group_changed_cell_charges_match_full_rescans():
    budget = trends.Budget(16 * 1024 * 1024)
    group = trends.Group(('model', 'tier'), budget)
    group.stats['duration'] = [2**30 - 1, 2**30 - 1, 2**300, 2**300]
    group.sessions[0] = [2**30 - 1, 2**30 - 1, 2**30 - 1]
    data = rows(24, 'huge')
    data[2].update(input=None, cached=None, duration=None)
    for row in data:
        scope = row['effort']
        old_stats = {key:sum(map(getsizeof, stats)) for key, stats in group.stats.items()}
        old_totals = {key:sum(map(getsizeof, totals)) for key, totals in group.sessions.items()}
        new_scope = scope not in group.scopes
        new_effort = scope not in group.efforts
        new_owner = row['_owner'] not in group.sessions
        before = budget.used
        group.observe(row, scope)
        expected = 96 if new_scope else 0
        if new_effort:expected += trends._size(scope) + 256
        for key, stats in group.stats.items():
            if key not in old_stats:
                expected += trends._size(key) + trends._size([0, 0, None, None]) + 96
            expected += sum(map(getsizeof, stats)) - old_stats.get(key, sum(map(getsizeof, [0, 0, None, None])))
        if new_owner:expected += trends._size(row['_owner']) + trends._size([0, 0, 0]) + 96
        expected += sum(map(getsizeof, group.sessions[row['_owner']])) - old_totals.get(row['_owner'], 3 * getsizeof(0))
        assert budget.used - before == expected


def test_count_reuses_only_cohort_counts_and_reasoning_keeps_its_owners():
    data = rows()
    for row in data:
        if row['effort'] == 'low':row['_owner'] = 99
    budget, window = window_for(data)
    traversals = []
    def members():
        traversals.append(True)
        return window.entries(data[0]['ts'], data[-1]['ts'], True, {'low', 'high'}, 0, 2**40)
    values = dict(trends.populations(members, budget, ('duration', 'cost', 'count'), window.check))
    assert len(traversals) == 2
    assert values['count']['calls'] == 72 and values['count']['sessions'] == 6
    low = lambda: window.entries(data[0]['ts'], data[-1]['ts'], True, {'low'}, 0, 2**40)
    reasoning = dict(trends.populations(low, budget, ('reasoning:low',), window.check))['reasoning:low']
    assert reasoning['calls'] == 36 and reasoning['sessions'] == 1
    empty = dict(trends.populations(lambda: iter(()), budget, ('count',), window.check))['count']
    assert empty['calls'] == 0 and empty['support'] is True


def test_capacity_failure_after_a_completed_metric_never_retries(monkeypatch):
    data = rows(32, 'huge')
    budget, window = window_for(data)
    retained = budget.used
    members = lambda: window.entries(data[0]['ts'], data[-1]['ts'], True, {'low', 'high'}, 0, 2**40)
    list(reference_populations(members, budget, ('count',), window.check))
    budget.limit = retained + budget.scratch_peak
    traversals, reductions = [], []
    def selected():
        traversals.append(True)
        return members()
    original = trends.distribution_statistics
    def reduce(values):
        reductions.append(True)
        return original(values)
    monkeypatch.setattr(trends, 'distribution_statistics', reduce)
    work = trends.populations(selected, budget, ('count', 'input'), window.check)
    assert next(work)[0] == 'count'
    with pytest.raises(CapacityExceeded):
        next(work)
    assert len(traversals) == 2 and len(reductions) == 1
    assert budget.used == retained


def test_population_containers_are_released_before_scratch(monkeypatch):
    budget, window = window_for(rows())
    retained = budget.used
    original = trends.distribution_statistics
    samples = []
    def reduce(values):
        samples.append(values)
        return original(values)
    monkeypatch.setattr(trends, 'distribution_statistics', reduce)
    scratch = budget.scratch
    @contextmanager
    def checked_scratch():
        with scratch() as reserve:
            try:
                yield reserve
            finally:
                assert samples and not samples[-1]
    monkeypatch.setattr(budget, 'scratch', checked_scratch)
    members = lambda: window.entries(1800000000., 1800000036., True, {'low', 'high'}, 0, 2**40)
    work = trends.populations(members, budget, ('duration', 'session_cache'), window.check)
    next(work)
    assert not samples[0] and budget.used == retained
    next(work)
    assert not samples[1] and budget.used == retained
    work.close()
    assert budget.used == retained


class ScalarSource:
    limit = 16 * 1024 * 1024
    retained = 0

    def __init__(self, data):
        self.data = data
        self.engine = SimpleNamespace(progress=None)
        self.snapshot = SimpleNamespace(_check=lambda: None)
        self.stats = {}
        self.closed = 0

    def ordered_rows(self, start, end, variants=None):
        selected = None if variants is None else {raw[:2] for raw, _ in variants}
        try:
            for row in self.data:
                self.snapshot._check()
                if start <= row['ts'] <= end and (selected is None or (row['model'], row['service_tier']) in selected):
                    yield row
        finally:
            self.closed += 1


@pytest.mark.parametrize('mode', ['timeline', 'hour_of_day'])
@pytest.mark.parametrize('shape', ['mixed', 'sessions', 'huge', 'scopes'])
def test_complete_trends_match_baseline_at_its_exact_peak(monkeypatch, mode, shape):
    data = rows(257 if shape == 'scopes' else 72, shape)
    scopes = {(row['model'], row['service_tier'], row['effort']):
        [((row['model'], row['service_tier'], row['effort']), ('str', 'str', 'str'))] for row in data}
    start, end = data[0]['ts'], data[-1]['ts']
    query = dict(now=end + 1)
    def project(source):
        return (trends.hour_of_day(source, query, start, end, (start, end), scopes) if mode == 'hour_of_day'
            else trends.rolling(source, query, 64, start, end, (start, end), scopes))
    optimized = trends.populations
    monkeypatch.setattr(trends, 'populations', reference_populations)
    baseline = ScalarSource(data)
    expected = project(baseline)
    monkeypatch.setattr(trends, 'populations', optimized)
    source = ScalarSource(data)
    source.limit = baseline.stats['scalar_peak_bytes']
    actual = project(source)
    assert exact_scalars(actual) == exact_scalars(expected)
    assert source.stats['scalar_peak_bytes'] <= baseline.stats['scalar_peak_bytes']
    assert source.closed == baseline.closed == 1


@pytest.mark.parametrize('mode', ['timeline', 'hour_of_day'])
def test_cancel_after_distribution_releases_cursor_and_does_not_publish(monkeypatch, mode):
    data = rows()
    source = ScalarSource(data)
    start, end = data[0]['ts'], data[-1]['ts']
    scopes = {(row['model'], row['service_tier'], row['effort']):
        [((row['model'], row['service_tier'], row['effort']), ('str', 'str', 'str'))] for row in data}
    cancelled = False
    class Cancelled(Exception):
        pass
    def check():
        if cancelled:raise Cancelled()
    source.snapshot._check = check
    original = trends.distribution_statistics
    def reduce(values):
        nonlocal cancelled
        result = original(values)
        cancelled = True
        return result
    monkeypatch.setattr(trends, 'distribution_statistics', reduce)
    with pytest.raises(Cancelled):
        if mode == 'hour_of_day':
            trends.hour_of_day(source, dict(now=end + 1), start, end, (start, end), scopes)
        else:
            trends.rolling(source, dict(now=end + 1), 64, start, end, (start, end), scopes)
    assert source.closed == 1 and source.stats == {}


def test_fixed_population_headers_and_saved_counts_fit_existing_scratch():
    headers = getsizeof([]) + getsizeof({}) + 2 * getsizeof(trends._Sum())
    counters = 4 * getsizeof(0) + 2 * getsizeof(False)
    assert headers + counters + getsizeof(iter(())) <= 512


@pytest.mark.parametrize('stage', ['members', 'before_distribution', 'after_distribution', 'between_metrics'])
def test_cancelled_population_releases_scratch_without_repeating_work(monkeypatch, stage):
    from cachemonitor.prepared_store import ReadCancelled
    budget, window = window_for(rows())
    retained = budget.used
    reductions, traversals, checks = [], [], []
    cancelled = False
    def check():
        checks.append(True)
        if cancelled or stage == 'before_distribution' and len(checks) == 2:
            raise ReadCancelled()
    def members():
        traversals.append(True)
        for index, member in enumerate(window.entries(1800000000., 1800000036., True,
                {'low', 'high'}, 0, 2**40)):
            if stage == 'members' and index == 8:raise ReadCancelled()
            yield member
    original = trends.distribution_statistics
    def reduce(values):
        nonlocal cancelled
        reductions.append(True)
        result = original(values)
        if stage == 'after_distribution':cancelled = True
        return result
    monkeypatch.setattr(trends, 'distribution_statistics', reduce)
    work = trends.populations(members, budget, ('duration', 'input'), check)
    if stage == 'between_metrics':
        assert next(work)[0] == 'duration'
        cancelled = True
    with pytest.raises(ReadCancelled):
        next(work)
    assert budget.used == retained and len(traversals) == 1
    assert len(reductions) == (stage in ('after_distribution', 'between_metrics'))
