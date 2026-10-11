"""Request and call flow inspection with screen-bounded chart geometry."""
from bisect import bisect_left, bisect_right
from datetime import datetime
from math import ceil, floor, isfinite, log10

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen

from .charts import AnalyticalPlot, value_text
from .i18n import tr
from .performance_panel import axis_number, time_label, time_ticks, trend_bounds


METRICS = ('cost', 'tokens', 'cache_ratio', 'output_speed', 'duration')


def finite(value):
    return value if isinstance(value, (int, float)) and isfinite(value) else None


def call_identity(row, kind='calls'):
    turn = row.get('turn')
    key = turn if kind == 'requests' and turn is not None else row.get('key')
    return row.get('home'), row.get('sid'), key


def sample_series(times, values, columns, breaks=None, start=None, end=None):
    """Keep each pixel's endpoints/extrema and never connect across missing data.

    A run identifies a continuous sequence of known values. Several runs can
    occupy one pixel, so omitted runs must still break connecting lines.
    """
    if not times:
        return []
    columns = max(1, int(columns))
    start = times[0] if start is None else start
    end = times[-1] if end is None else end
    span = end - start or 1
    samples = []
    bucket = None
    kept = None
    run = 0
    for index in range(bisect_left(times, start), bisect_right(times, end)):
        stamp, value = times[index], values[index]
        if breaks is not None and breaks[index]:
            run += 1
        if value is None:
            run += 1
            continue
        column = min(columns - 1, int((stamp - start) / span * columns))
        point = (index, value, run)
        if column != bucket:
            if kept:
                samples.extend(sorted(set(kept)))
            bucket, kept = column, [point, point, point, point]
        else:
            kept[1] = point
            if value < kept[2][1]:
                kept[2] = point
            if value > kept[3][1]:
                kept[3] = point
    if kept:
        samples.extend(sorted(set(kept)))
    return samples


class SessionTimeline(AnalyticalPlot):
    inspected = Signal(object)
    repaintRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setFixedHeight(340)
        self.times = []
        self.axis_values = []
        self.axis = 'ordinal'
        self.record_kind = 'calls'
        self.range = (0., 1.)
        self.full_range = self.range
        self.bounds = (0., 1.)
        self.series_bounds = {}
        self._values = {}
        self._duplicates = {}
        self._indices = {}
        self._breaks = []
        self._boundaries = []
        self._boundary_pixels = []
        self._ordinal_labels = {}
        self._sample_cache = None
        self._has_values = False
        self._source_rows = None
        self.setAccessibleName('세션 흐름 차트 / 방향키로 항목 확인')

    @property
    def current_row(self):
        return self.rows[self.cursor] if self.rows else None

    @property
    def series(self):
        if self.metric == 'tokens':
            return (('input', '입력', 'accent'), ('output', '출력', 'output'))
        label = {'cost': '환산액', 'cache_ratio': '캐시 적중률',
                 'output_speed': '평균 출력 속도', 'duration': '소요시간'}[self.metric]
        return ((self.metric, label, 'accent'),)

    def set_data(self, rows, metric='cost', axis='ordinal', kind='calls'):
        metric = metric if metric in METRICS else 'cost'
        axis = axis if axis in ('ordinal', 'time') else 'ordinal'
        kind = 'requests' if kind == 'requests' else 'calls'
        if rows is self._source_rows and axis == self.axis and kind == self.record_kind:
            if metric != self.metric:
                self.set_metric(metric)
            return
        previous = call_identity(self.current_row, self.record_kind) if self.current_row else None
        same_scope = (axis == self.axis and kind == self.record_kind and previous is not None
                      and bool(rows) and previous[:2] == call_identity(rows[0], kind)[:2])
        previous_range = self.range if same_scope and self.range != self.full_range else None
        self._source_rows = rows
        previous = previous if self.record_kind == kind else None
        self.axis, self.record_kind = axis, kind
        if axis == 'time':
            self.rows = sorted((row for row in rows if finite(row.get('ts')) is not None),
                               key=lambda row: (row['ts'], row.get('ordinal') or 0))
            self.axis_values = [row['ts'] for row in self.rows]
            self._ordinal_labels = {}
        else:
            self.rows = sorted(rows, key=lambda row: (finite(row.get('ordinal')) is None,
                                                     finite(row.get('ordinal')) or 0))
            last = max((row['ordinal'] for row in self.rows if finite(row.get('ordinal')) is not None), default=0)
            self.axis_values = []
            self._ordinal_labels = {}
            for row in self.rows:
                ordinal = finite(row.get('ordinal'))
                if ordinal is None:
                    last += 1
                    ordinal = last
                    self._ordinal_labels[ordinal] = '미연결'
                self.axis_values.append(ordinal)
        self.times = [row.get('ts') for row in self.rows]
        self._indices = {call_identity(row, kind): index for index, row in enumerate(self.rows)}
        self.cursor = self._indices.get(previous, 0)
        self._breaks = []
        self._boundaries = []
        for index, row in enumerate(self.rows):
            before = self.rows[index - 1] if index else None
            ordinal, prior = finite(row.get('ordinal')), finite(before.get('ordinal')) if before else None
            boundary = bool(before and kind == 'calls' and row.get('turn') != before.get('turn'))
            self._breaks.append(bool(before and (ordinal is None or prior is None or ordinal != prior + 1
                                                or boundary or call_identity(before, kind)[:2] != call_identity(row, kind)[:2])))
            if boundary:
                self._boundaries.append(index)
        if self.axis_values:
            start, end = self.axis_values[0], self.axis_values[-1]
            self.full_range = (start, end) if end > start else (max(0, start - 1), end + 1)
        else:
            self.full_range = (0., 1.)
        self.range = self.full_range
        if previous_range:
            self.range = self.clamped_range(*previous_range)
        if previous not in self._indices:
            visible = bisect_left(self.axis_values, self.range[0])
            if visible < len(self.axis_values) and self.axis_values[visible] <= self.range[1]:
                self.cursor = visible
        self.set_metric(metric, force=True)

    def capture_state(self):
        return dict(axis=self.axis, kind=self.record_kind, metric=self.metric, range=self.range,
                    selected=call_identity(self.current_row, self.record_kind) if self.current_row else None)

    def restore_state(self, state):
        if not isinstance(state, dict) or state.get('axis', self.axis) != self.axis or state.get('kind', self.record_kind) != self.record_kind:
            return
        if state.get('metric') in METRICS:
            self.set_metric(state['metric'])
        window = state.get('range')
        if isinstance(window, (list, tuple)) and len(window) == 2 and all(finite(value) is not None for value in window):
            self.set_range(*window)
        identity = state.get('selected')
        if isinstance(identity, (list, tuple)):
            index = self._indices.get(tuple(identity))
            if index is not None:
                self.inspect(index, force=True)

    def clamped_range(self, start, end):
        low, high = self.full_range
        if end <= start:
            return self.range
        span = min(max(end - start, 1 if self.axis == 'ordinal' else .001), high - low)
        start = max(low, min(high - span, start))
        return start, start + span

    def set_range(self, start, end):
        if finite(start) is None or finite(end) is None or end <= start:
            return
        window = self.clamped_range(start, end)
        if window == self.range:
            return
        self.range = window
        self._sample_cache = None
        self.refresh_bounds()
        self.update()

    def reset_range(self):
        self.set_range(*self.full_range)

    def refresh_bounds(self):
        left = bisect_left(self.axis_values, self.range[0])
        right = bisect_right(self.axis_values, self.range[1])
        self.series_bounds = {key: trend_bounds(values[index] for index in range(left, right))
                              for key, values in self._values.items()}
        self.bounds = self.series_bounds[self.series[0][0]]

    def set_metric(self, metric, force=False):
        metric = metric if metric in METRICS else 'cost'
        if metric == self.metric and not force:
            return
        self.metric = metric
        self._values = {key: [finite(row.get(key)) for row in self.rows]
                        for key, _, _ in self.series}
        self.refresh_bounds()
        self._has_values = any(value is not None for values in self._values.values() for value in values)
        self._duplicates = {}
        # A timestamp can belong to thousands of calls. Index their Y values once
        # so hovering never linearly scans a duplicate-time group.
        left = 0
        while left < len(self.axis_values):
            right = bisect_right(self.axis_values, self.axis_values[left], lo=left + 1)
            if right - left > 1:
                self._duplicates[self.axis_values[left]] = {
                    key: sorted((value, index) for index in range(left, right)
                                if (value := values[index]) is not None)
                    for key, values in self._values.items()}
            left = right
        self._sample_cache = None
        self.put(clickEnabled=bool(self.rows))
        self.update()
        self.inspect(self.cursor, force=True)

    def plot_rect(self, key=None):
        rect = QRectF(76, 30, max(1, self.width() - 96), max(1, self.height() - 82))
        if key is not None and self.metric == 'tokens':
            height = max(1, (rect.height() - 32) / 2)
            return QRectF(rect.left(), rect.top() + (height + 32 if key == 'output' else 0), rect.width(), height)
        return rect

    def bounds_for(self, key=None):
        return self.series_bounds.get(key or self.series[0][0], (0., 1.))

    def x(self, value):
        rect = self.plot_rect()
        start, end = self.range
        return rect.left() + (value - start) / (end - start) * rect.width()

    def x_for(self, row):
        index = self._indices.get(call_identity(row, self.record_kind))
        return self.x(self.axis_values[index]) if index is not None else self.plot_rect().left()

    def y(self, value, key=None):
        key = key or self.series[0][0]
        rect = self.plot_rect(key)
        low, high = self.bounds_for(key)
        return rect.bottom() - (value - low) / (high - low) * rect.height()

    def samples(self):
        width = max(1, int(self.plot_rect().width()))
        if self._sample_cache is None or self._sample_cache[0] != (width, self.range):
            self._sample_cache = ((width, self.range), {key: sample_series(self.axis_values, values, width, self._breaks, *self.range)
                                         for key, values in self._values.items()})
            # Multiple request boundaries in one pixel require one marker only.
            self._boundary_pixels = sorted({round(self.x((self.axis_values[index - 1] + self.axis_values[index]) / 2))
                                            for index in self._boundaries
                                            if self.range[0] <= (self.axis_values[index - 1] + self.axis_values[index]) / 2 <= self.range[1]})
        return self._sample_cache[1]

    def inspect(self, index, force=False, reveal=False):
        index = max(0, min(len(self.rows) - 1, index)) if self.rows else 0
        changed = self.cursor != index
        self.cursor = index
        if reveal and self.rows:
            position = self.axis_values[index]
            start, end = self.range
            if position < start:
                self.set_range(position, position + end - start)
            elif position > end:
                self.set_range(position - end + start, position)
        if changed or force:
            row = self.current_row
            if row:
                try:
                    stamp = datetime.fromtimestamp(row['ts']).strftime('%Y/%m/%d %H:%M:%S')
                except (TypeError, ValueError, OverflowError, OSError):
                    stamp = tr('시각 미확인')
                values = ' / '.join(tr(label) + ' ' + value_text(self._values[key][index], key)
                                    for key, label, _ in self.series)
                # Accessibility is live state, not a change to static geometry.
                unit = tr('요청' if self.record_kind == 'requests' else '호출')
                ordinal = row.get('ordinal') if row.get('ordinal') is not None else tr('미연결')
                status = ', ' + tr(row['state']) if row.get('state') else ''
                self._state['accessible'] = f"{unit} {ordinal}, {stamp}, {values}{status}"
            else:
                self._state['accessible'] = tr('세션 흐름 차트 / 방향키로 항목 확인')
            self.stateChanged.emit()
            self.inspected.emit(row)
            self.repaintRequested.emit()

    def nearest_index(self, x, y):
        if not self.rows:
            return None
        rect = self.plot_rect()
        start, end = self.range
        left = bisect_left(self.axis_values, start)
        right = bisect_right(self.axis_values, end)
        if left == right:
            return None
        stamp = start + (x - rect.left()) / rect.width() * (end - start)
        at = bisect_left(self.axis_values, stamp, lo=left, hi=right)
        index = min((max(left, at - 1), min(right - 1, at)),
                    key=lambda candidate: abs(self.axis_values[candidate] - stamp))
        duplicate = self._duplicates.get(self.axis_values[index])
        if duplicate:
            candidates = {index}
            if self.axis_values[self.cursor] == self.axis_values[index]:
                candidates.add(self.cursor)
            key = min(self._values, key=lambda name: abs(self.plot_rect(name).center().y() - y))
            rect = self.plot_rect(key)
            low, high = self.bounds_for(key)
            target = low + (rect.bottom() - y) / rect.height() * (high - low)
            for sequence in (duplicate[key],):
                pos = bisect_left(sequence, (target, -1))
                candidates.update(point[1] for point in sequence[max(0, pos - 1):pos + 1])
            def distance(candidate):
                value = self._values[key][candidate]
                delta = abs(self.y(value, key) - y) if value is not None else float('inf')
                return delta, candidate != self.cursor, candidate
            index = min((candidate for candidate in candidates if left <= candidate < right), key=distance)
        return index

    def tip_at(self, x, y):
        if self.plot_rect().contains(QPointF(x, y)):
            index = self.nearest_index(x, y)
            if index is not None:
                self.inspect(index)
        return ''

    def activate_at(self, x, y):
        if self.plot_rect().contains(QPointF(x, y)):
            index = self.nearest_index(x, y)
            if index is not None:
                self.inspect(index)
                self.selected.emit(self.current_row)

    def key(self, key):
        if not self.rows:
            return
        if key in (Qt.Key_Left, Qt.Key_Up, Qt.Key_Right, Qt.Key_Down):
            self.inspect(self.cursor + (-1 if key in (Qt.Key_Left, Qt.Key_Up) else 1), reveal=True)
        elif key in (Qt.Key_Home, Qt.Key_End):
            self.inspect(0 if key == Qt.Key_Home else len(self.rows) - 1, reveal=True)
        elif key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.selected.emit(self.current_row)

    def axis_labels(self, metrics):
        rect = self.plot_rect()
        if self.axis == 'time':
            ticks, step = time_ticks(*self.range, rect.width())
        else:
            start, end = self.range
            first = max(ceil(start), ceil(self.axis_values[0]))
            last = min(floor(end), floor(self.axis_values[-1]))
            interval = max(1, (end - start) / max(1, rect.width() / 90))
            magnitude = 10 ** floor(log10(interval))
            step = next(value * magnitude for value in (1, 2, 5, 10) if value * magnitude >= interval)
            ticks = sorted({first, last, *range(ceil(first / step) * step, last + 1, step)}) if first <= last else []
        labels = []
        time_keys = []
        for stamp in ticks:
            time_key = None
            if self.axis == 'time':
                precision = '%Y-%m-%d %H:%M:%S' if step < 60 else '%Y-%m-%d %H:%M' if step < 86400 else '%Y-%m-%d'
                time_key = datetime.fromtimestamp(stamp).strftime(precision)
                if time_key in time_keys:
                    continue
            while True:
                if self.axis == 'time':
                    previous = datetime.fromtimestamp(labels[-1][0]) if labels else None
                    text = time_label(stamp, previous, step)
                else:
                    text = tr(self._ordinal_labels[stamp]) if stamp in self._ordinal_labels else f'{stamp:,}'
                width = max(metrics.horizontalAdvance(line) for line in text.split('\n')) + 4
                left = max(rect.left(), min(rect.right() - width, self.x(stamp) - width / 2))
                box = QRectF(left, rect.bottom() + 10, width, 40)
                if not labels or box.left() >= labels[-1][2].right() + 12:
                    labels.append((stamp, text, box))
                    time_keys.append(time_key)
                    break
                if stamp != ticks[-1]:
                    break
                labels.pop()
                time_keys.pop()
        return labels

    def paint(self, painter):
        p = self.base()
        if not self.rows:
            return
        unit = {'cost': 'USD', 'tokens': tr('토큰'), 'cache_ratio': '%',
                'output_speed': 'tok/s', 'duration': tr('초')}[self.metric]
        sampled = self.samples()
        for lane, (key, label, color) in enumerate(self.series):
            rect = self.plot_rect(key)
            low, high = self.bounds_for(key)
            p.setPen(self.color(color))
            p.drawText(QRectF(rect.left(), rect.top() - 28, rect.width(), 22), Qt.AlignLeft | Qt.AlignVCenter,
                       tr(label) + ' / ' + unit)
            if lane == 0:
                p.setPen(self.color('muted'))
                axis_label = '기록 시각' if self.axis == 'time' else '요청 순서' if self.record_kind == 'requests' else '호출 순서'
                p.drawText(QRectF(rect.left(), rect.top() - 28, rect.width(), 22), Qt.AlignRight | Qt.AlignVCenter,
                           tr(axis_label))
            for fraction in ((0, .5, 1) if rect.height() < 110 else (0, .25, .5, .75, 1)):
                value = low + (high - low) * fraction
                y = self.y(value, key)
                p.setPen(QPen(self.color('grid'), 1, Qt.DashLine))
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
                p.setPen(self.color('muted'))
                p.drawText(QRectF(0, y - 10, rect.left() - 10, 20), Qt.AlignRight | Qt.AlignVCenter,
                           ('$' if self.metric == 'cost' else '') + axis_number(value))
            p.setPen(QPen(self.color('grid'), 1, Qt.DotLine))
            for x in self._boundary_pixels:
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            samples = sampled[key]
            if not samples:
                p.setPen(self.color('muted'))
                p.drawText(rect, Qt.AlignCenter, tr('이 지표의 유효한 기록 없음'))
                continue
            p.save()
            p.setClipRect(rect.adjusted(-5, -5, 5, 5))
            path = QPainterPath()
            previous = None
            for index, value, run in samples:
                point = QPointF(self.x(self.axis_values[index]), self.y(value, key))
                if previous == run:
                    path.lineTo(point)
                else:
                    path.moveTo(point)
                previous = run
            p.setPen(QPen(self.color(color), 2.4))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setBrush(self.color('surface'))
            for offset, (index, value, run) in enumerate(samples):
                isolated = ((offset == 0 or samples[offset - 1][2] != run)
                            and (offset == len(samples) - 1 or samples[offset + 1][2] != run))
                if len(self.rows) <= 160 or isolated:
                    p.drawEllipse(QPointF(self.x(self.axis_values[index]), self.y(value, key)), 3.5, 3.5)
            p.restore()
        p.setPen(self.color('muted'))
        for _, text, box in self.axis_labels(p.fontMetrics()):
            p.drawText(box, Qt.AlignHCenter | Qt.AlignTop, text)

    def paint_overlay(self, painter):
        row = self.current_row
        if row is None or not self.range[0] <= self.axis_values[self.cursor] <= self.range[1]:
            return
        x = self.x_for(row)
        painter.setRenderHint(QPainter.Antialiasing)
        for key, _, color in self.series:
            rect = self.plot_rect(key)
            painter.setPen(QPen(self.color('muted'), 1, Qt.DashLine))
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            value = self._values[key][self.cursor]
            if value is not None:
                painter.setPen(QPen(self.color(color), 2))
                painter.setBrush(self.color('surface'))
                painter.drawEllipse(QPointF(x, self.y(value, key)), 5, 5)
