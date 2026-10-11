"""Timeline selection retains real calls while drawing bounded geometry."""
from math import isclose

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication

from cachemonitor.history_timeline import SessionTimeline, sample_series
from cachemonitor.quick_qa import dispose, mount, render_plot


def test_screen_samples_keep_extrema_without_bridging_unknown_costs():
    times = list(range(10001))
    values = [float(index % 11) for index in times]
    values[4980] = None
    values[5000] = 500.
    samples = sample_series(times, values, 40)
    assert len(samples) <= 40 * 4
    assert any(index == 5000 and value == 500. for index, value, _ in samples)
    for previous, current in zip(samples, samples[1:]):
        if previous[2] == current[2]:
            assert None not in values[previous[0]:current[0] + 1]
    assert sample_series([1, 2], [None, None], 10) == []
    assert sample_series([1], [0.], 10) == [(0, 0., 0)]


def test_rendered_timeline_uses_time_and_retains_call_identity():
    app = QApplication.instance() or QApplication([])
    rows = [dict(home='fixture', sid='session', key=key, ordinal=index + 1,
                 ts=stamp, cost=cost, input=100 + index, output=10 + index,
                 cache_ratio=None, output_speed=None)
            for index, (key, stamp, cost) in enumerate([
                ('a', 1700000000., 1.), ('b', 1700000400., 3.),
                ('c', 1700000400., 8.), ('d', 1700000400., None),
                ('e', 1700010000., 2.)])]
    chart = SessionTimeline()
    chart.set_data(list(reversed(rows)), axis='time')
    inspected, selected = [], []
    chart.inspected.connect(inspected.append)
    chart.selected.connect(selected.append)
    host = mount(chart, 640, 380)
    try:
        plot = render_plot(host, chart)
        box = chart.plot_rect()
        # Large idle periods remain visibly larger than short call intervals.
        assert chart.x(rows[1]['ts']) - chart.x(rows[0]['ts']) < (
            chart.x(rows[4]['ts']) - chart.x(rows[1]['ts'])) / 10
        plot.activateAt(chart.x(rows[2]['ts']), chart.y(rows[2]['cost']))
        assert selected[-1] is rows[2]
        # Calls with the same timestamp remain independently keyboard-accessible.
        chart.key(Qt.Key_Right)
        assert inspected[-1] is rows[3]
        assert chart.current_row is rows[3]
        chart.key(Qt.Key_Return)
        assert selected[-1] is rows[3]
        chart.set_data(rows, 'cache_ratio', axis='time')
        assert chart.current_row is rows[3]
        render_plot(host, chart)
        assert not chart._has_values
        assert chart.nearest_index(chart.x(rows[4]['ts']), box.center().y()) == 4
        chart.set_metric('tokens')
        render_plot(host, chart)
        assert chart.current_row is rows[3]
        assert set(chart.samples()) == {'input', 'output'}
        assert not host.qml_errors
    finally:
        dispose(host)


def test_empty_single_call_and_dense_rows_keep_bounded_rendering():
    app = QApplication.instance() or QApplication([])
    chart = SessionTimeline()
    chart.empty_text = 'Waiting for a selected session'
    host = mount(chart, 480, 380)
    try:
        render_plot(host, chart)
        assert chart.empty_text == 'Waiting for a selected session'
        single = dict(home='fixture', sid='one', key='only', ordinal=1,
                      ts=1700000000.25, cost=0.)
        chart.set_data([single])
        render_plot(host, chart)
        assert chart.plot_rect().left() < chart.x_for(single) < chart.plot_rect().right()
        assert chart.nearest_index(chart.x_for(single), chart.y(0.)) == 0
        chart.set_data([single], axis='time')
        render_plot(host, chart)
        labels = [text.splitlines()[0] for _, text, _ in chart.axis_labels(QFontMetrics(chart.font()))]
        assert len(labels) == len(set(labels))
        rows = [dict(single, sid='dense', key=str(index), ordinal=index + 1,
                     ts=single['ts'] + index, cost=index % 31 / 100)
                for index in range(10001)]
        chart.set_data(rows)
        plot = render_plot(host, chart)
        assert len(chart.samples()['cost']) <= int(chart.plot_rect().width()) * 4
        picked = []
        chart.selected.connect(picked.append)
        plot.activateAt(chart.x_for(rows[1234]), chart.plot_rect().center().y())
        assert picked[-1] is rows[1234]
        calls = []
        chart.changed.connect(lambda: calls.append(True))
        chart.set_data(rows)
        assert not calls
        assert chart.current_row is rows[1234]
        assert not host.qml_errors
    finally:
        dispose(host)


def test_zoomed_chart_selects_only_visible_calls_and_ignores_empty_ranges():
    app = QApplication.instance() or QApplication([])
    rows = [dict(home='fixture', sid='session', key=str(ordinal), ordinal=ordinal,
                 ts=1700000000. + ordinal, cost=float(ordinal)) for ordinal in (1, 4, 10)]
    chart = SessionTimeline()
    chart.set_data(rows)
    host = mount(chart, 640, 380)
    try:
        selected, inspected = [], []
        chart.selected.connect(selected.append)
        chart.inspected.connect(inspected.append)
        chart.set_range(5, 10)
        plot = render_plot(host, chart)
        box = chart.plot_rect()
        # The hidden call at 4 is closer, but the visible call at 10 owns this range.
        plot.activateAt(box.left(), box.center().y())
        assert selected == [rows[2]] and inspected == [rows[2]]
        chart.set_data([rows[0], rows[1], dict(rows[2], key='8', ordinal=8)])
        assert chart.current_row is rows[1]
        assert chart.range[0] <= chart.axis_values[chart.cursor] <= chart.range[1]
        selected.clear()
        inspected.clear()
        chart.set_range(5, 7)
        render_plot(host, chart)
        plot.showTip(box.center().x(), box.center().y())
        plot.activateAt(box.center().x(), box.center().y())
        assert chart.nearest_index(box.center().x(), box.center().y()) is None
        assert not selected and not inspected
        assert not host.qml_errors
    finally:
        dispose(host)


def test_compact_token_panels_keep_independent_scales_and_original_call_gaps():
    app = QApplication.instance() or QApplication([])
    rows = [dict(home='fixture', sid='session', key=str(ordinal), ordinal=ordinal,
                 turn=turn, ts=1700000000. + index * index * 100,
                 input=input_tokens, output=output_tokens)
            for index, (ordinal, turn, input_tokens, output_tokens) in enumerate([
                (1, 'first', 100000, 50), (2, 'first', 200000, 600),
                (5, 'second', 150000, 300), (6, 'second', None, 100)])]
    chart = SessionTimeline()
    chart.put(height=200, minHeight=160, maxHeight=200)
    chart.set_data(rows, 'tokens')
    host = mount(chart, 640, 240)
    try:
        plot = render_plot(host, chart)
        assert chart.axis == 'ordinal'
        first, second = chart.plot_rect('input'), chart.plot_rect('output')
        assert first.bottom() < second.top()
        assert chart.bounds_for('input')[1] > chart.bounds_for('output')[1] * 100
        assert abs(chart.y(50, 'output') - chart.y(600, 'output')) > second.height() / 2
        assert isclose(chart.x_for(rows[2]) - chart.x_for(rows[1]),
                       3 * (chart.x_for(rows[1]) - chart.x_for(rows[0])))
        samples = chart.samples()
        assert [point[0] for point in samples['input']] == [0, 1, 2]
        assert samples['output'][1][2] != samples['output'][2][2]
        selected = []
        chart.selected.connect(selected.append)
        plot.activateAt(chart.x_for(rows[1]), chart.y(rows[1]['output'], 'output'))
        assert selected[-1] is rows[1]
        assert chart._boundary_pixels
        assert not host.qml_errors
    finally:
        dispose(host)


def test_request_flow_keeps_pending_rows_and_restores_range_and_turn_identity():
    app = QApplication.instance() or QApplication([])
    rows = [dict(home='fixture', sid='session', key=turn, turn=turn, ordinal=ordinal,
                 ts=stamp, cost=cost, duration=duration, state=state,
                 input=100, output=10, calls=1, total_calls=1)
            for turn, ordinal, stamp, cost, duration, state in [
                ('pending', 1, None, None, None, '진행 중'),
                ('done', 2, 1700000010., .25, 4., '완료'),
                ('__unlinked__', None, None, None, None, '미연결')]]
    chart = SessionTimeline()
    chart.set_data(rows, 'duration', kind='requests')
    host = mount(chart, 640, 380)
    try:
        plot = render_plot(host, chart)
        assert chart.rows == rows
        assert chart.samples()['duration'] == [(1, 4., 1)]
        selected = []
        chart.selected.connect(selected.append)
        plot.activateAt(chart.x_for(rows[0]), chart.plot_rect().center().y())
        assert selected[-1] is rows[0]
        chart.key(Qt.Key_Right)
        chart.set_range(1, 2)
        state = chart.capture_state()
        assert state['selected'] == ('fixture', 'session', 'done')
        chart.set_data([dict(rows[1], sid='another')])
        updated = [dict(row, key='new-' + row['key']) for row in rows]
        chart.set_data(updated, kind='requests')
        chart.restore_state(state)
        assert chart.current_row is updated[1]
        assert chart.metric == 'duration' and chart.range == (1, 2)
        chart.key(Qt.Key_End)
        assert chart.current_row is updated[2]
        assert chart.range[0] <= chart.axis_values[chart.cursor] <= chart.range[1]
        assert chart._ordinal_labels[chart.axis_values[-1]] == '미연결'
        chart.set_data(rows, 'duration', axis='time', kind='requests')
        assert chart.rows == [rows[1]]
        render_plot(host, chart)
        assert not host.qml_errors
    finally:
        dispose(host)
