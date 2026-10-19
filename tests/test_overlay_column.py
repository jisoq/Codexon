"""Behavior shared by the vertical overlay's charts and fixed summaries."""
from copy import deepcopy
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication

from cachemonitor.overlay_appearance import default_appearance
from cachemonitor.overlay_view import OverlayContent
from test_overlay_presentation import subagent_summary, summary


@pytest.fixture
def column_model():
    app = QApplication.instance() or QApplication([])
    model = OverlayContent()
    yield model
    model.deleteLater()
    app.processEvents()


def test_agent_color_identity_does_not_depend_on_discovery_order(column_model):
    data = subagent_summary()
    rows = [dict(home='home', sid=sid) for sid in ('parent', 'child', 'other')]
    column_model.set_content(data)
    original = column_model.graph_colors(rows)
    fresh = OverlayContent()
    try:
        fresh.set_content(data)
        reordered = fresh.graph_colors(list(reversed(rows)))
        assert {key: original[key] for key in reordered} == reordered
    finally:
        fresh.deleteLater()


def test_agent_color_survives_rolling_calls_and_subagent_sorting(column_model):
    data = subagent_summary()
    column_model.set_content(data)
    colors = column_model.graph_colors(column_model.rows())
    updated = deepcopy(data)
    updated['recent'] = [row for row in updated['recent'] if row['sid'] == 'other']
    updated['subagent_finance']['agents'].reverse()
    column_model.set_content(updated)
    after = column_model.graph_colors(column_model.rows())
    assert all(after[key] == color for key, color in colors.items())


def test_agent_colors_return_after_switching_away_from_incremental_discovery(column_model):
    first=dict(home='h',id='owner',calls=2,cost=0,recent=[dict(home='h',sid='z',id='z-call')])
    column_model.set_content(first)
    z_color=column_model.graph_colors(column_model.rows())[('h','z')]
    expanded=dict(first,recent=[dict(home='h',sid='a',id='a-call'),*first['recent']])
    column_model.set_content(expanded)
    colors=column_model.graph_colors(column_model.rows())
    assert colors[('h','z')]==z_color and colors[('h','a')]!=z_color
    column_model.set_content(dict(home='h',id='another',calls=0,cost=0,recent=[]))
    column_model.set_content(expanded)
    assert column_model.graph_colors(column_model.rows())==colors


def test_token_details_keep_explicit_unknown_and_confirmed_zero_distinct(column_model):
    group=dict(calls=4,priced=3,cost=.5,
        token_composition=dict(known=True,category_known={'written':True},input_total=10,output_total=20,
            input_parts=[dict(key='cached',tokens=10,known=True)],
            output_parts=[dict(key='reasoning',tokens=0,known=False),dict(key='output',tokens=0,known=True)]),
        cost_composition=dict(known=True,input_total=.1,output_total=.4,
            input_parts=[dict(key='cached',tokens=.1,known=True)],
            output_parts=[dict(key='reasoning',tokens=0,known=False),dict(key='output',tokens=0,known=True)]))
    items=column_model.group_details(group)
    def value(label):
        item=next(item for item in items if item[0]==label)
        result=next(row[0] for row in items if row[2]==item[2] and row[8].get('right'))
        return ''.join(result) if isinstance(result,list) else result
    assert value('추론')=='— 토큰 / —'
    assert value('추론 외')=='0 토큰 / $0.00'
    assert value('캐시 쓰기')=='0 토큰 / —'
    assert value('입력 (확인분)')=='10 토큰 / $0.1'
    assert value('출력 (확인분)')=='20 토큰 / $0.4'
    assert any(item[0]=='호출 3/4' for item in items)


def test_codex_accent_changes_chart_palette_without_changing_agent_identity(column_model):
    data = subagent_summary()
    first = replace(default_appearance(True), accent='#287cf6')
    second = replace(first, accent='#e04c9a')
    column_model.set_content(data, appearance=first)
    before = column_model.graph_colors(column_model.rows())
    column_model.set_content(data, appearance=second)
    after = column_model.graph_colors(column_model.rows())
    assert before.keys() == after.keys()
    assert any(before[key] != after[key] for key in before)


def test_recent_call_population_keeps_missing_slots_and_original_order(column_model):
    data = summary(count=25)
    data['recent'][-3].update(cost=None, output_speed=None, reasoning=None)
    expected = data['recent'][-20:]
    column_model.set_content(data)
    assert column_model.rows() == expected
    assert len(column_model.rows()) == 20
    assert column_model.rows()[-3]['cost'] is None


def test_main_disclosure_keeps_selected_call_and_cumulative_population(column_model):
    data = subagent_summary()
    column_model.set_content(data)
    column_model.select(column_model.call_id(column_model.rows()[0]))
    selected = column_model.selected_id
    rows = deepcopy(column_model.rows())
    own = deepcopy(data['main_usage'])
    initial = column_model.main_expanded
    column_model.monitor_action('main-detail')
    assert column_model.main_expanded is not initial
    assert column_model.selected_id == selected
    assert column_model.rows() == rows
    assert column_model.data['main_usage'] == own
    column_model.monitor_action('main-detail')
    assert column_model.main_expanded is initial


def test_column_places_three_charts_then_main_before_sub_viewport():
    from cachemonitor.overlay_layout import ColumnLayout
    layout = ColumnLayout.create(has_subagents=True)
    assert layout.width == 300
    assert len(layout.charts) == 3
    assert all(start + layout.chart_height <= end
               for start, end in zip(layout.charts, layout.charts[1:]))
    assert layout.charts[-1] + layout.chart_height <= layout.main
    assert layout.main + layout.main_height <= layout.sub_heading
    assert layout.sub_heading < layout.sub_y
    assert layout.sub_height >= 80
    assert layout.sub_y + layout.sub_height <= layout.height


def test_main_expansion_adds_space_without_reducing_sub_viewport():
    from cachemonitor.overlay_layout import ColumnLayout
    closed = ColumnLayout.create(has_subagents=True)
    opened = ColumnLayout.create(has_subagents=True, main_detail_height=120)
    assert opened.charts == closed.charts
    assert opened.main == closed.main
    assert opened.main_detail_height == 120
    assert opened.sub_y - closed.sub_y == 120
    assert opened.height - closed.height == 120
    assert opened.sub_height == closed.sub_height


def test_length_preference_only_extends_subagent_viewport():
    from cachemonitor.overlay_layout import ColumnLayout
    short = ColumnLayout.create(preferred_height=720, has_subagents=True)
    tall = ColumnLayout.create(preferred_height=960, has_subagents=True)
    assert (tall.charts, tall.main, tall.main_height, tall.sub_y) == (
        short.charts, short.main, short.main_height, short.sub_y)
    assert tall.height - short.height == tall.sub_height - short.sub_height
    assert tall.sub_height > short.sub_height
    absent = ColumnLayout.create(preferred_height=960)
    assert absent.sub_height == 0
    assert absent.height < tall.height


@pytest.mark.parametrize('key', ['cost', 'output_speed', 'reasoning'])
def test_filled_line_keeps_missing_call_slot_and_does_not_bridge_it(key):
    from cachemonitor.overlay_monitor import chart_segments
    rows = [{key: value, 'output': 100} for value in (1, 2, None, 4, 5)]
    assert chart_segments(rows, key) == (
        ((0, 1), (1, 2)), ((3, 4), (4, 5)))


@pytest.mark.parametrize('field', ['model', 'effort', 'mode', 'transport'])
def test_speed_line_keeps_global_adjacency_when_request_condition_changes(field):
    from cachemonitor.overlay_monitor import chart_segments
    first = dict(output_speed=10, model='model', effort='high',
                 mode='Standard', transport='WebSocket')
    changed = dict(first, output_speed=20)
    changed[field] = 'changed'
    rows = [first, changed, dict(changed, output_speed=30)]
    assert chart_segments(rows, 'output_speed') == (
        ((0, 10), (1, 20)), ((1, 20), (2, 30)))


@pytest.mark.parametrize('key', ['cost', 'output_speed', 'reasoning'])
def test_filled_line_distinguishes_observed_zero_from_unknown_values(key):
    from cachemonitor.overlay_monitor import chart_segments
    observed = [{key: 0, 'output': 0}, {key: 0, 'output': 0}]
    assert chart_segments(observed, key) == (((0, 0), (1, 0)),)
    unknown = [{key: None}, {key: float('nan')}, {key: float('inf')}]
    assert chart_segments(unknown, key) == ()


def test_zero_call_summary_preserves_zero_total_and_has_no_chart_segments(column_model):
    from cachemonitor.overlay_monitor import chart_segments
    data = summary(count=0)
    column_model.set_content(data)
    assert column_model.data['calls'] == 0
    assert column_model.total_cost() == 0
    assert column_model.rows() == []
    assert all(chart_segments(column_model.rows(), key) == ()
               for key in ('cost', 'output_speed', 'reasoning'))


def test_unpriced_calls_keep_unknown_total_distinct_from_zero(column_model):
    data = summary(count=2)
    data['cost'] = None
    column_model.set_content(data)
    assert column_model.total_cost() is None


@pytest.fixture
def column_controller(tmp_path):
    from PySide6.QtCore import QSettings
    from cachemonitor.overlay import OverlayController
    app = QApplication.instance() or QApplication([])
    settings = QSettings(str(tmp_path / 'column.ini'), QSettings.IniFormat)
    controller = OverlayController(settings, native_enabled=False)
    yield controller
    controller.stop()
    app.processEvents()


def test_chart_style_change_preserves_selected_call_and_finance(column_controller):
    controller = column_controller
    model = controller.widget.content_model
    data = subagent_summary()
    model.set_content(data)
    model.select(model.call_id(model.rows()[0]))
    selected = model.selected_id
    own = deepcopy(model.data['main_usage'])
    rows = deepcopy(model.rows())
    for style in ('filled-line', 'bar'):
        controller.set_chart_style(style)
        assert controller.chart_style == model.chart_style == style
        assert model.selected_id == selected
        assert model.rows() == rows
        assert model.data['main_usage'] == own


def test_length_buttons_and_chart_style_preferences_survive_restart(column_controller):
    from cachemonitor.overlay import OverlayController
    controller = column_controller
    controller.set_preferred_height(720)
    controller.change_length(80)
    assert controller.preferred_height == 800
    controller.change_length(-80)
    assert controller.preferred_height == 720
    controller.set_chart_style('filled-line')
    controller.settings.sync()
    restored = OverlayController(controller.settings, native_enabled=False)
    try:
        assert restored.preferred_height == 720
        assert restored.chart_style == 'filled-line'
        assert restored.widget.content_model.chart_style == 'filled-line'
    finally:
        restored.stop()
