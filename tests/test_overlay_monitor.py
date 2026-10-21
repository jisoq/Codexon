"""Monitor data, drawing, freshness, and call-selection contracts."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from cachemonitor.overlay_data import cost_composition, main_usage
from cachemonitor.overlay_icons import AgentMark, agent_mark
from cachemonitor.overlay_monitor import CHARTS, graph_value, scope
from cachemonitor.overlay_view import Drawing, OverlayContent, money, percent
from cachemonitor.pricing import token_cost
from test_overlay_presentation import summary, subagent_summary


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def render(model):
    image = QImage(model.panel_width(), model.panel_height(), QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        model.paint(painter)
    finally:
        painter.end()
    return image


def capture_text(monkeypatch):
    drawn = []
    original = Drawing.text
    def record(drawing, value, x, y, *args, **kwargs):
        drawn.append((str(value), x, y))
        return original(drawing, value, x, y, *args, **kwargs)
    monkeypatch.setattr(Drawing, 'text', record)
    return drawn


def priced(**changes):
    row = dict(model='gpt-6-astra', service_tier='Standard', input=1000,
               cached=800, written=20, output=100, reasoning=40)
    row.update(changes)
    return {**row, **token_cost(row)}


def test_currency_population_matches_headline_and_unknown_output_is_preserved():
    rows = [priced(), priced(model='gpt-6-sol', service_tier='Fast', reasoning=None),
            priced(input_conflict=True)]
    composition = cost_composition(rows)
    assert composition['priced'] == 2 and composition['partial']
    assert composition['input_total'] + composition['output_total'] == pytest.approx(
        sum(row['cost'] for row in rows if row['cost'] is not None))
    for section in ('input', 'output'):
        assert sum(part['tokens'] for part in composition[section + '_parts']) == pytest.approx(
            composition[section + '_total'])
        assert sum(part['share'] for part in composition[section + '_parts']) == pytest.approx(1)
    unknown = next(part for part in composition['output_parts'] if part['key'] == 'output_unknown')
    assert unknown['tokens'] == rows[1]['cost_output']


def test_currency_unknown_and_observed_zero_are_distinct():
    assert cost_composition([priced(service_tier='unknown')])['input_total'] is None
    assert cost_composition([priced(input=0, cached=0, written=0, output=0, reasoning=0)])['input_total'] == 0


def test_active_requests_update_heartbeat_without_changing_layout(app):
    model = OverlayContent()
    data = summary()
    model.set_content(data)
    layout = model.layout()
    for count in (3, 0):
        model.set_content(dict(data, active_requests=count))
        assert model.state['heartbeatActive'] == bool(count)
        assert model.layout() == layout


def test_collection_status_replaces_heartbeat_and_keeps_summary_data(app, monkeypatch):
    model = OverlayContent()
    data = summary(misses=1)
    drawn = capture_text(monkeypatch)
    for note in ('수집 오류', '수집 지연'):
        model.set_content(data, note)
        drawn.clear()
        render(model)
        status = [item for item in drawn if item[0].startswith(note)]
        assert not status
        assert model.state['heartbeatMode']=='error'
        assert model.state['heartbeatLabel']==note
        assert not any(item['interaction']=='tooltip' for item in model.monitor_links())
        assert model.state['heartbeatY'] < model.layout()['charts'][0] < model.layout()['main']
        assert model.data['cache_misses'] == data['cache_misses']
        model.monitor_action('main-detail')
        assert any('캐시 미적중' in str(item[0]) for item in model.main_items())
        model.monitor_action('main-detail')


def test_scope_counts_only_main_calls_and_reports_pricing_coverage():
    assert scope({'calls': 28, 'priced': 24}) == 'Main Agent : 24/28호출'
    assert scope({'calls': 28, 'descendants': 2,
                  'main_usage': {'calls': 8, 'priced': 7}}) == 'Main Agent : 7/8호출'


def test_stale_content_fades_monitor_and_embedded_sub_body_then_recovers(app):
    from cachemonitor.overlay_chrome import OverlayDetail
    model = OverlayContent()
    data = subagent_summary()
    data['_collection'] = {'retained': True}
    pane = OverlayDetail(model)
    try:
        for note in ('기록 준비 중', '수집 지연', '수집 오류'):
            model.set_content(data, note)
            assert model.content_opacity() < 1
            assert pane.view.state['contentOpacity'] == model.content_opacity()
            assert model.data['cost'] == data['cost'] and model.has_subagents()
        model.set_content(data)
        assert model.content_opacity() == pane.view.state['contentOpacity'] == 1
        partial = deepcopy(data)
        partial['coverage_gap'] = True
        model.set_content(partial)
        assert model.content_opacity() == pane.view.state['contentOpacity'] == 1
    finally:
        pane.close()
        app.processEvents()


def test_rendered_main_summary_uses_own_cumulative_metrics(app, monkeypatch):
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    from test_overlay_call_data import row, session
    parent = session([row(0, cost=2, input=2000, cached=1500, output=200,
                          response_status='completed', completion_latency_ms=1000)], id='parent')
    child = session([row(1, cost=7, input=9000, cached=1000, output=900,
                         response_status='completed', completion_latency_ms=1000)],
                    id='child', parent_thread_id='parent')
    engine = AnalysisEngine()
    engine.ingest([parent, child])
    data = next(value for value in OverlaySummaries().collect(engine) if value['id'] == 'parent')
    model = OverlayContent()
    model.set_content(data)
    drawn = capture_text(monkeypatch)
    render(model)
    layout = model.layout()
    main = [text for text, x, y in drawn if layout['main'] <= y < layout['main'] + layout['main_height']]
    assert money(main_usage(data)['cost']) in main
    assert money(data['cost']) not in main
    assert money(data['cost']) in [text for text, x, y in drawn if y < layout['charts'][0]]
    assert any(percent(main_usage(data)['token_composition']['cache_hit_rate']) in value for value in main)
    assert any('200.0 tps' in value for value in main)
    assert sum(group['cost'] for group in model.detail_groups()) == pytest.approx(data['child_cost'])


def test_agent_colors_survive_rolling_calls_and_meet_theme_contrast(app):
    from cachemonitor.overlay_appearance import default_appearance
    from cachemonitor.token_colors import contrast_ratio, ui_palette
    model = OverlayContent()
    rows = [dict(home='h', sid=sid) for sid in ('parent', 'child-1', 'child-2')]
    for dark in (True, False):
        model.set_content(dict(home='h', id='parent'), appearance=default_appearance(dark))
        before = model.graph_colors(rows)
        after = model.graph_colors(rows[1:] + [dict(home='h', sid='child-3')])
        assert all(after[key] == before[key] for key in after.keys() & before.keys())
        assert model.graph_colors(rows) == before
        background = ui_palette(model.appearance)['overlay']
        assert all(contrast_ratio(color, background) >= 3 for color in after.values())


def test_agent_name_icons_share_chart_colors_after_sorting_and_rolling(app):
    model = OverlayContent()
    data = subagent_summary()
    model.set_content(data)
    colors = model.graph_colors(model.rows())
    def name_colors():
        return {item[0]: item[8]['agentMark'].color for item in model.detail_items()
                if item[8].get('agentMark')}
    names = name_colors()
    assert names == {row['title']: colors[(row['home'], row['id'])]
                     for row in data['subagent_finance']['agents']}
    changed = deepcopy(data)
    changed['recent'] = [row for row in changed['recent'] if row['sid'] == 'other']
    for index, row in enumerate(changed['subagent_finance']['agents']):
        row['latest_ts'] = 1000 + index
    model.set_content(changed)
    assert name_colors() == names
    assert all(model.graph_colors(model.rows())[key] == value for key, value in colors.items())


@pytest.mark.parametrize('task_id,index', [
    ('ab', 25), ('ba', 27), ('\U0001f331', 17),
    ('019f0000-1234-7000-8000-000000000000', 20),
    ('019f0000-1234-7000-8000-000000000077', 20)])
def test_codex_icons_follow_reference_task_ids_and_utf16(app, task_id, index):
    from cachemonitor.overlay_appearance import default_appearance
    assert agent_mark(task_id, default_appearance(True)).index == index


def test_all_codex_icon_variants_paint_from_packaged_assets(app):
    from dataclasses import replace
    from PySide6.QtCore import QRectF
    from cachemonitor.overlay_appearance import default_appearance
    appearance = default_appearance(True)
    ids = {}
    for number in range(1000):
        task_id = f'icon-catalog-{number}'
        ids.setdefault(agent_mark(task_id, appearance).index, task_id)
        if len(ids) == 28:
            break
    assert len(ids) == 28
    for task_id in ids.values():
        for dark in (True, False):
            image = QImage(24, 24, QImage.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            try:
                agent_mark(task_id, replace(appearance, dark=dark)).paint(painter, QRectF(image.rect()))
            finally:
                painter.end()
            assert any(image.pixelColor(x, y).alpha() for x in range(image.width()) for y in range(image.height()))


def test_rendered_main_and_child_icons_use_task_ids_and_fit_the_name_gutter(app, monkeypatch):
    data = subagent_summary()
    model = OverlayContent()
    model.set_content(data)
    painted = []
    original = AgentMark.paint
    def record(mark, painter, bounds):
        painted.append((mark.index, bounds))
        original(mark, painter, bounds)
    monkeypatch.setattr(AgentMark, 'paint', record)
    render(model)
    assert painted and painted[0][0] == agent_mark(data['id'], model.appearance).index
    from PySide6.QtCore import QRectF
    from cachemonitor.overlay_view import paint_items
    image = QImage(model.detail_width, model.panel_height(), QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        paint_items(Drawing(painter, model), model.detail_items())
    finally:
        painter.end()
    expected = [agent_mark(group['id'], model.appearance).index for group in model.detail_groups()]
    assert [index for index, bounds in painted[1:]] == expected
    name_items = [item for item in model.detail_items() if item[8].get('agentMark')]
    for (_, bounds), item in zip(painted[1:], name_items):
        assert bounds.width() > 0 and bounds.height() > 0
        assert bounds.left() >= 0 and bounds.right() <= item[1]
        assert QRectF(0, item[2], item[1], item[4]).contains(bounds)


def test_icon_collisions_keep_the_same_chart_and_highlight_color(app):
    model = OverlayContent()
    first = '019f0000-1234-7000-8000-000000000000'
    second = '019f0000-1234-7000-8000-000000000077'
    model.set_content(dict(home='h', id=first))
    colors = model.graph_colors([dict(home='other-home', sid=second)])
    assert colors[('h', first)] == colors[('other-home', second)]
    model.set_content(dict(home='other-home', id=second))
    assert model.graph_colors([])[('other-home', second)] == colors[('h', first)]


def test_waiting_cards_and_retained_values_are_distinct(app, monkeypatch):
    model = OverlayContent()
    for note in ('기록 확인 중', '수집 오류', '수집 지연', '수집 시작 중'):
        model.set_content(None, note)
        assert model.notice_card()
        assert not any(link.get('interaction') == 'select' for link in model.monitor_links())
    model.set_content(summary(count=0))
    assert not model.notice_card() and model.total_cost() == 0
    assert not any(link.get('interaction') == 'select' for link in model.monitor_links())
    data = summary()
    data['_collection'] = {'last_confirmed_at': 700}
    monkeypatch.setattr('cachemonitor.overlay_view.time.time', lambda: 1000)
    model.set_content(data, '수집 지연')
    assert not model.notice_card()
    assert model.data['cost'] == data['cost']
    assert model.freshness_text() == '5분 전 확인값 표시 중'
    assert model.freshness_text() in model.state['accessible']
    model.set_content(data)
    assert not model.freshness_text()


def test_heartbeat_animates_active_only_and_error_takes_priority(app, tmp_path):
    import time
    from PySide6.QtCore import QObject, QEventLoop
    from cachemonitor.overlay_view import SessionOverlay
    def until(condition):
        deadline = time.monotonic() + 3
        while not condition():
            assert time.monotonic() < deadline
            app.processEvents(QEventLoop.AllEvents, 10)
    window = SessionOverlay()
    try:
        window.set_content(None, '기록 확인 중', dark=False)
        window.show()
        until(lambda: window.quick.rootObject() is not None)
        scene = window.quick.rootObject()
        heartbeat = scene.findChild(QObject, 'overlayHeartbeat')
        assert not heartbeat.property('blinking')
        active=summary();active['active_requests']=2
        window.set_content(active,dark=False)
        until(lambda: heartbeat.property('blinking') and heartbeat.property('opacity')<.5)
        ink=heartbeat.property('ink');assert ink.green()>ink.red() and ink.green()>ink.blue()
        window.hide()
        until(lambda: not heartbeat.property('blinking'))
        window.show()
        until(lambda: heartbeat.property('blinking'))
        window.set_content(active,'수집 오류',dark=False)
        until(lambda: not heartbeat.property('blinking') and heartbeat.property('opacity')==1)
        assert window.content_model.state['heartbeatMode']=='error'
        label=scene.findChild(QObject,'overlayHeartbeatLabel')
        assert not heartbeat.property('visible') and label.property('visible')
        assert label.property('text')==window.content_model.state['heartbeatLabel']
        assert label.property('width')>0
        assert window.grab().save(str(tmp_path / 'heartbeat-error.png'))
        window.set_content(None,'기록 확인 중',dark=False)
        until(lambda: window.content_model.state['heartbeatMode']=='pending')
        assert not heartbeat.property('visible') and label.property('visible')
        assert label.property('text')==window.content_model.state['heartbeatLabel']
        window.set_content(summary(),dark=False)
        until(lambda: window.content_model.state['heartbeatMode']=='idle')
        assert heartbeat.property('visible') and not heartbeat.property('blinking')
        assert not window.qml_errors
    finally:
        window.close()
        app.processEvents()


def test_child_call_navigation_and_scope_switch_guard():
    from cachemonitor.overlay_navigation import navigation_target
    from cachemonitor.overlay_chrome import NavigationModel
    row = dict(home='h', sid='child', id='call')
    content = SimpleNamespace(data=dict(home='h', id='root', members=[('h', 'root'), ('h', 'child')], latest=row))
    target = navigation_target(content.data, 'call')
    assert target.sid == 'child'
    navigation = NavigationModel(content)
    received = []
    navigation.navigationRequested.connect(received.append)
    navigation.link_state([dict(id='call', target=target)])
    navigation.activateLink('call')
    assert received == [target]
    navigation.captureNavigation('call')
    content.data = {**content.data, 'id': 'different'}
    navigation.activateNavigation()
    assert received == [target]


def test_collection_time_does_not_mutate_details_or_scroll_charts(app):
    from test_overlay_navigation import family_summary
    model = OverlayContent()
    data = family_summary(count=100)
    model.set_content(data)
    before = deepcopy(model._detail_items)
    selected = model.selected_id
    model.set_content({**data, '_collection': {'last_confirmed_at': 100}})
    assert model._detail_items == before and model.selected_id == selected
    assert len(model.rows()) == 20 and 'all_calls' not in data
    model.set_layout(reduced=True)
    layout = model.layout()
    model.scroll_lower(999)
    assert model.layout() == layout and model.lower_offset == 0


def test_model_labels_require_confirmed_response_and_highlight_only_mismatch():
    from cachemonitor.overlay_monitor import model_text
    row = dict(model='gpt-example', requested_model='gpt-example', response_model='gpt-example',
               response_status='completed', model_match='일치')
    assert model_text(row) == ('gpt-example', '', False)
    row.update(response_model='gpt-other', model_match='불일치', model_alert_confirmed=True)
    assert model_text(row) == ('gpt-example', ' (불일치: gpt-other)', True)
    assert model_text({**row, 'observation_missing': True}) == ('gpt-example', '', False)
    assert model_text(dict(model='gpt-config', model_setting=True)) == ('설정: gpt-config', '', False)


@pytest.mark.parametrize('changes,expected', [
    ({'reasoning': 0}, 0), ({'reasoning': 40}, 40), ({'reasoning': None}, None),
    ({'reasoning': 101}, None), ({'reasoning': -1}, None), ({'reasoning': float('nan')}, None),
    ({'output_conflict': True}, None)])
def test_reasoning_preserves_zero_and_rejects_missing_or_conflicting_values(changes, expected):
    assert graph_value(dict(output=100, reasoning=40) | changes, 'reasoning') == expected


def test_three_chart_lanes_show_latest_values_without_selection_regions(app,monkeypatch):
    model=OverlayContent();data=summary(count=3)
    data['recent'][-1].update(cost=.25,cache_rate=75,output_speed=12,reasoning=0)
    model.set_content(data)
    drawn=capture_text(monkeypatch);render(model);layout=model.layout()
    assert len(CHARTS)==len(layout['charts'])==3
    chart_labels={text for text,x,y in drawn if y in layout['charts']}
    assert all(label in chart_labels for key,label in CHARTS)
    assert any(money(.25) in text and percent(75) in text for text in chart_labels)
    assert '12.0 tps' in chart_labels
    assert not any(link.get('interaction')=='select' for link in model.monitor_links())
