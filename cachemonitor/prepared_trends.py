"""Exact chart populations from a pinned chronological scalar stream."""
from array import array
from bisect import bisect_left, bisect_right
from collections import deque
from contextlib import contextmanager
from heapq import merge
from itertools import islice
from math import ceil
from struct import pack, unpack
from sys import getsizeof

from .analytics import effort_key
from .performance_trends import (METRICS, MIN_BAND, MIN_CENTER, MIN_OUTER_BAND,
    distribution_statistics, rolling_window, valid)
from .prepared_store import CapacityExceeded, _size
from .prepared_sums import _Sum


FIELDS = ('ts', 'duration', 'output_speed', 'cost', 'input', 'cached', 'reasoning',
          'output', 'completion_latency_ms')
RAW = ('duration', 'output_speed', 'cost', 'input', 'reasoning')
RESULT_BYTES = 64 * 1024 * 1024


class Budget:
    def __init__(self, limit):
        self.limit = limit
        self.used = self.peak = self.scratch_peak = self.result = 0

    def add(self, amount):
        if self.used + amount > self.limit:
            raise CapacityExceeded('performance scalar window and exact reduction scratch exceed their object budget')
        self.used += amount
        self.peak = max(self.peak, self.used)

    def output(self, amount):
        if self.result + amount > RESULT_BYTES:
            raise CapacityExceeded('performance chart exceeds the admitted delivery budget')
        self.result += amount

    @contextmanager
    def scratch(self):
        amount = 0
        def reserve(value):
            nonlocal amount
            self.add(value)
            amount += value
            self.scratch_peak = max(self.scratch_peak, amount)
        try:
            yield reserve
        finally:
            self.add(-amount)


class Numbers:
    """Typed numeric cells; ordinary scalars occupy nine bytes plus capacity."""
    __slots__ = ('bits', 'tags', 'large', 'large_bytes')

    def __init__(self):
        self.bits = array('q')
        self.tags = bytearray()
        self.large = {}
        self.large_bytes = 0

    def append(self, value):
        if not valid(value):
            tag, bits = 0, 0
        elif type(value) is float:
            tag, bits = 2, unpack('=q', pack('=d', value))[0]
        elif -(2**63) <= value < 2**63:
            tag, bits = 1, value
        else:
            tag, bits = 3, 0
            position = len(self.tags)
            self.large[position] = value
            self.large_bytes += _size(position) + _size(value)
        self.bits.append(bits)
        self.tags.append(tag)

    def at(self, position):
        if position < 0:position += len(self.tags)
        tag = self.tags[position]
        if tag == 0:
            return None
        if tag == 1:
            return self.bits[position]
        if tag == 2:
            return unpack('=d', pack('=q', self.bits[position]))[0]
        return self.large[position]

    def __len__(self):return len(self.tags)
    def __getitem__(self,position):return self.at(position)

    def size(self):
        return (getsizeof(self) + getsizeof(self.bits) + getsizeof(self.tags) +
                getsizeof(self.large) + self.large_bytes)


class Block:
    __slots__ = ('values', 'owners', 'positions', 'conflicts')

    def __init__(self):
        self.values = {name: Numbers() for name in FIELDS}
        self.owners = array('Q')
        self.positions = {}
        self.conflicts = bytearray()

    def append(self, row, scope):
        if scope not in self.positions:self.positions[scope] = array('H')
        self.positions[scope].append(len(self.owners))
        self.owners.append(row['_owner'])
        self.conflicts.append(bool(row.get('input_conflict')))
        for name, values in self.values.items():
            values.append(row.get(name))

    def size(self):
        return (getsizeof(self) + getsizeof(self.values) + getsizeof(self.owners) +
                getsizeof(self.positions) + sum(getsizeof(key) + getsizeof(values) for key, values in self.positions.items()) +
                getsizeof(self.conflicts) + 32 +
                sum(values.size() for values in self.values.values()))


class Window:
    def __init__(self, budget, check):
        self.budget, self.check = budget, check
        self.blocks = deque()
        self.tail_size = 0

    def append(self, row, scope):
        # Reserve a complete block's possible capacity growth before mutation.
        reserve = 16384 + sum(_size(row.get(name)) for name in FIELDS
            if type(row.get(name)) is int and not -(2**63) <= row[name] < 2**63)
        self.budget.add(reserve)
        amount = 0
        try:
            if not self.blocks or len(self.blocks[-1].owners) >= 256:
                block = Block();self.blocks.append(block)
                self.tail_size = block.size();amount += self.tail_size + 16
            block = self.blocks[-1]
            before = self.tail_size
            block.append(row, scope)
            self.tail_size = block.size()
            amount += self.tail_size - before
        finally:
            self.budget.add(amount - reserve)

    def discard(self, before):
        while self.blocks and self.blocks[0].values['ts'].at(-1) < before:
            block = self.blocks.popleft()
            self.budget.add(-block.size() - 16)

    def entries(self, start, end, inclusive, scopes):
        for block in self.blocks:
            self.check()
            times=block.values['ts']
            # Chronological blocks allow whole-block rejection and two seeks;
            # do not decode every timestamp again for every displayed metric.
            if times.at(-1)<start:continue
            if times.at(0)>end or times.at(0)==end and not inclusive:break
            left=bisect_left(times,start)
            right=bisect_right(times,end) if inclusive else bisect_left(times,end)
            selected = [block.positions[scope] for scope in scopes if scope in block.positions]
            selected=[islice(indices,bisect_left(indices,left),bisect_left(indices,right)) for indices in selected]
            positions = selected[0] if len(selected) == 1 else merge(*selected)
            for position in positions:
                stamp = block.values['ts'].at(position)
                yield block, position, stamp


def _eligible(row, field):
    value = row.get(field)
    if not valid(value):
        return False
    return field != 'output_speed' or (valid(row.get('output')) and
        valid(row.get('completion_latency_ms')) and row['completion_latency_ms'] > 0)


class Group:
    def __init__(self, key, budget):
        self.key, self.budget = key, budget
        self.count = 0
        self.scopes = set()
        self.efforts = {}
        self.stats = {}
        self.sessions = {}
        self.points = {}
        self.raw = {}
        budget.add(_size(key) + 512)

    def observe(self, row, scope):
        self.count += 1
        effort = row.get('effort') or '미확인'
        if scope not in self.scopes:self.budget.add(96);self.scopes.add(scope)
        if effort not in self.efforts:
            self.budget.add(_size(effort) + 256);self.efforts[effort] = set()
        self.efforts[effort].add(scope)
        for field in RAW:
            metric = 'reasoning:' + effort if field == 'reasoning' else field
            if metric not in self.stats:
                self.budget.add(_size(metric) + _size([0, 0, None, None]) + 96)
                self.stats[metric] = [0, 0, None, None]
            stats = self.stats[metric]
            # These fixed-length scalar lists do not change capacity. Charge
            # only their cells' exact size changes, including arbitrary integers.
            before = sum(map(getsizeof, stats))
            stats[1] += 1
            if _eligible(row, field):
                value = row[field]
                stats[0] += 1
                if stats[2] is None or value < stats[2]:stats[2] = value
                if stats[3] is None or value > stats[3]:stats[3] = value
            self.budget.add(sum(map(getsizeof, stats)) - before)
        owner = row['_owner']
        if owner not in self.sessions:
            self.budget.add(_size(owner) + _size([0, 0, 0]) + 96)
            self.sessions[owner] = [0, 0, 0]
        totals = self.sessions[owner]
        before = sum(map(getsizeof, totals))
        if (valid(row.get('input')) and valid(row.get('cached')) and
                row['cached'] <= row['input'] and not row.get('input_conflict')):
            totals[0] += row['input'];totals[1] += row['cached'];totals[2] += 1
        self.budget.add(sum(map(getsizeof, totals)) - before)

    def summary(self, metric):
        if metric == 'count':return self.count, self.count, None, None
        if metric != 'session_cache':return self.stats.get(metric, (0, 0, None, None))
        count, low, high = 0, None, None
        for amount, cached, _ in self.sessions.values():
            if not amount:continue
            value = 100 * cached / amount;count += 1
            if low is None or value < low:low = value
            if high is None or value > high:high = value
        return count, len(self.sessions), low, high

    def samples(self, row, width, start, end):
        if end - start > 3600:return
        for field in RAW:
            if not _eligible(row, field):continue
            metric = 'reasoning:' + (row.get('effort') or '미확인') if field == 'reasoning' else field
            point = dict(ts=row['ts'], value=row[field], model=self.key[0], service_tier=self.key[1])
            column = min(width - 1, int((row['ts'] - start) / (end - start) * width))
            cells = self.raw.setdefault(metric, {})
            cell = cells.get(column)
            before = _size(cell) if cell is not None else 0
            if cell is None:
                cell = cells[column] = [point, point, point, point]
            else:
                cell[3] = point
                if point['value'] < cell[1]['value']:cell[1] = point
                if point['value'] > cell[2]['value']:cell[2] = point
            self.budget.output(_size(cell) - before + (96 if before == 0 else 0))


@contextmanager
def population(window, group, metric, start, end, inclusive, support_start, support_end):
    """One metric's values and per-session totals; no retained call records."""
    scopes = group.efforts[metric.split(':', 1)[1]] if metric.startswith('reasoning:') else group.scopes
    field = 'reasoning' if metric.startswith('reasoning:') else metric
    with window.budget.scratch() as reserve:
        reserve(512)
        values, sessions = [], {}
        calls = 0
        support = metric == 'count'
        numerator, seconds = _Sum(), _Sum()
        for block, position, stamp in window.entries(start, end, inclusive, scopes):
            calls += 1
            owner = block.owners[position]
            if owner not in sessions:
                reserve(_size(owner) + 240)
                sessions[owner] = [0, 0, 0] if metric == 'session_cache' else None
            if metric == 'count':continue
            if metric == 'session_cache':
                amount, cached = (block.values[name].at(position) for name in ('input', 'cached'))
                if amount is None or cached is None or cached > amount or block.conflicts[position]:continue
                totals = sessions[owner]
                before = sum(map(getsizeof, totals))
                totals[0] += amount;totals[1] += cached;totals[2] += 1
                reserve(max(0, sum(map(getsizeof, totals)) - before))
                if amount and support_start <= stamp <= support_end:support = True
                continue
            value = block.values[field].at(position)
            if value is None:continue
            if metric == 'output_speed':
                output, elapsed = (block.values[name].at(position) for name in ('output', 'completion_latency_ms'))
                if output is None or elapsed is None or elapsed <= 0:continue
                numerator.add(output);seconds.add(elapsed / 1000)
            reserve(_size(value) + 24)  # list capacity, sorted copy and Timsort scratch
            values.append(value)
            if support_start <= stamp <= support_end:support = True
        if metric == 'session_cache':
            amount, cached, valid_calls = _Sum(), _Sum(), 0
            for own_input, own_cached, own_calls in sessions.values():
                if not own_input:continue
                value = 100 * own_cached / own_input
                reserve(_size(value) + 24)
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
        yield dict(eligible=calls if metric == 'count' else len(values), total=total, totals=totals,
                   distribution=distribution_statistics(values), mean=mean, support=support,
                   sessions=len(sessions), calls=calls)


def point(metric, group, event, window, now, values):
    _, index, start, end, stamp, _, _ = event
    counted = metric == 'count'
    eligible, support, mean = values['eligible'], values['support'], values['mean']
    representative = values['distribution']['median'] if metric == 'duration' else mean
    minimum = MIN_CENTER if metric == 'duration' else 1
    complete = (end - start == window and end <= now if counted else
                start == stamp - window / 2 and end == stamp + window / 2 and end <= now)
    status = ('기록 공백' if not support else '표본 부족' if eligible < minimum and not counted else
              '일부 구간' if not complete else '이동 계산 구간')
    return dict(kind='trend' if counted else 'rolling', ts=stamp,
        value=representative if counted or support and eligible >= minimum else None,
        mean=mean, representative=representative,
        statistic='median' if metric == 'duration' else 'time_weighted_mean' if metric == 'output_speed' else 'count' if counted else 'mean',
        bucket=index, start=start, end=end, complete=complete, model=group.key[0], service_tier=group.key[1],
        effort=metric.split(':', 1)[1] if metric.startswith('reasoning:') else '',
        coverage=(eligible, values['total']), distribution=values['distribution'], totals=values['totals'],
        calls=values['calls'], sessions=values['sessions'], band_valid=not counted and support and eligible >= MIN_BAND,
        outer_valid=not counted and support and eligible >= MIN_OUTER_BAND, support=support,
        baseline=None, delta=None, baseline_count=0, direction=0, status=status, previous_start=None, previous_end=None)


def rolling(source, q, width, start, end, full, scopes):
    span, now = end - start, q['now']
    duration = rolling_window(span)
    step = max(duration / 4, span / max(16, width // 6))
    steps = max(1, ceil(span / step))
    centers = [start + span * index / steps for index in range(steps + 1)]
    events = [('rolling', index, max(start, stamp - duration / 2), min(end, stamp + duration / 2),
               stamp, max(start, stamp - duration / 6), min(end, stamp + duration / 6))
              for index, stamp in enumerate(centers)]
    bins = [('count', index, start + index * duration, min(end, start + (index + 1) * duration),
             (start + index * duration + min(end, start + (index + 1) * duration)) / 2, start, end)
            for index in range(ceil(span / duration))]
    events.extend(bins);events.sort(key=lambda event: event[3])
    lefts = [event[2] for event in events]
    for index in range(len(lefts) - 2, -1, -1):lefts[index] = min(lefts[index], lefts[index + 1])
    budget = Budget(source.limit)
    window = Window(budget, source.snapshot._check)
    groups, scope_ids = {}, {}
    efforts = sorted({scope[2] for scope in scopes}, key=effort_key)
    specs = list(METRICS) + [('reasoning:' + effort, '평균 추론 토큰 / ' + effort, 'reasoning') for effort in efforts] + [('count', '호출 수', 'count')]
    rows = iter(source.ordered_rows(start, end))
    upcoming = next(rows, None)
    count = 0
    progress=getattr(source.engine,'progress',None)
    try:
        for event_index, event in enumerate(events):
            _, _, a, b, _, support_a, support_b = event
            while upcoming is not None and (upcoming['ts'] < b or b == end and upcoming['ts'] == b):
                row = upcoming
                scope = tuple(row.get(name) or '미확인' for name in ('model', 'service_tier', 'effort'))
                if scope not in scope_ids:
                    budget.add(_size(scope) + 96);scope_ids[scope] = len(scope_ids)
                identity = scope_ids[scope]
                key = scope[:2]
                if key not in groups:groups[key] = Group(key, budget)
                group = groups[key]
                group.observe(row, identity);group.samples(row, width, start, end)
                window.append(row, identity);count += 1
                if progress and count%512==0:
                    progress(dict(stage='aggregate',processed=event_index,total=len(events),
                                  unit='intervals',calls=count))
                upcoming = next(rows, None)
            for group in groups.values():
                for metric, _, _ in specs:
                    if (metric == 'count') != (event[0] == 'count'):continue
                    if metric.startswith('reasoning:') and metric.split(':', 1)[1] not in group.efforts:continue
                    with population(window, group, metric, a, b, b == end, support_a, support_b) as values:
                        value = point(metric, group, event, duration, now, values)
                    budget.output(_size(value) + 32)
                    group.points.setdefault(metric, {})[event[1]] = value
            if event_index + 1 < len(events):window.discard(lefts[event_index + 1])
            if progress:progress(dict(stage='aggregate',processed=event_index+1,total=len(events),
                                      unit='intervals',calls=count))
    finally:
        rows.close()
    panels = []
    for metric, title, unit in specs:
        if metric == 'duration':title = '호출 소요시간 중앙값'
        lines, samples, lows, highs, n, total = [], [], [], [], 0, 0
        for group in groups.values():
            if metric.startswith('reasoning:') and metric.split(':', 1)[1] not in group.efforts:continue
            eligible, denominator, low, high = group.summary(metric)
            n += eligible;total += denominator
            if low is not None:lows.append(low);highs.append(high)
            grid = bins if metric == 'count' else [event for event in events if event[0] == 'rolling']
            grid = sorted(grid, key=lambda event: event[1])
            points = []
            for event in grid:
                value = group.points.get(metric, {}).get(event[1])
                if value is None:
                    empty = dict(eligible=0, total=0, totals=(dict(input=0, cached=0, valid_calls=0) if metric == 'session_cache' else
                        dict(output=0, seconds=0) if metric == 'output_speed' else dict(sum=0)),
                        distribution=distribution_statistics([]), mean=0 if metric == 'count' else None,
                        support=metric == 'count', sessions=0, calls=0)
                    value = point(metric, group, event, duration, now, empty)
                    budget.output(_size(value) + 32)
                points.append(value)
            for cell in group.raw.get(metric, {}).values():
                samples.extend({id(value): value for value in cell}.values())
            lines.append(dict(model=group.key[0], service_tier=group.key[1], points=points, coverage=(eligible, denominator)))
        panels.append(dict(key=metric, title=title, unit=unit, n=n, N=total,
            points=sorted(samples, key=lambda value: value['ts']), low=min(lows, default=0), high=max(highs, default=1),
            lines=lines, rolling=True, window_seconds=duration, raw_visible=span <= 3600 and metric not in ('session_cache', 'count')))
    source.stats.update(scalar_peak_bytes=budget.peak, scratch_peak_bytes=budget.scratch_peak, output_bytes=budget.result)
    return dict(panels=panels, start=start, end=end, full=full, granularity='auto', window_seconds=duration,
        models=sorted({scope[0] for scope in scopes}), calls=count, valid_until=now + max(1, min(60, step)))
