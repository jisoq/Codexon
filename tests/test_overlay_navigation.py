"""Call selection, scoped navigation, and embedded Sub viewport input."""
from copy import deepcopy
from types import SimpleNamespace

from cachemonitor.overlay_navigation import navigation_target
from test_overlay_layout_controller import layout_controller


def family_summary(count=12, children=3):
    from cachemonitor.core import Session
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    sessions = []
    for index in range(children + 1):
        sid = 'clean' if index == 0 else 'child-' + str(index)
        session = Session(sid, 'home', title='Main task' if index == 0 else 'Sub task ' + str(index))
        for call in range(count if index == 0 else 3):
            session.add_usage(1789884000 + call * 10 + index, str(index) + '-' + str(call),
                dict(input_tokens=100000, cached_input_tokens=90000, cache_write_input_tokens=0,
                     output_tokens=1000, reasoning_output_tokens=400),
                'gpt-6-astra' if index == 0 else ('gpt-6-sol' if index % 2 else 'gpt-6-luna'),
                'turn', 'high', 'Standard')
        value = session.view(1789884100)
        if index:
            value.update(parent_thread_id='clean', source='subagent')
        sessions.append(value)
    engine = AnalysisEngine()
    engine.ingest(sessions)
    return next(row for row in OverlaySummaries().collect(engine) if row['id'] == 'clean')


def publish(controller, data):
    from cachemonitor.overlay_tracking import Selection
    controller.receive_snapshot({'overlay_sessions': [data]})
    controller.receive_target({'target': {'hwnd': 1}, 'selection': Selection(data['id'])})
    return controller.widget.content_model


def test_pointer_intent_survives_call_refresh_but_not_session_change():
    from cachemonitor.overlay_chrome import NavigationModel
    content = SimpleNamespace(data={'home': 'h', 'id': 's', 'latest': {'id': 'old'}})
    model = NavigationModel(content)
    received = []
    model.navigationRequested.connect(received.append)
    old = navigation_target(content.data, 'cache')
    model.link_state([dict(id='cache', target=old)])
    model.captureNavigation('cache')
    content.data['latest'] = {'id': 'new'}
    model.link_state([dict(id='cache', target=navigation_target(content.data, 'cache'))])
    model.activateNavigation()
    assert received == [old]
    model.captureNavigation('cache')
    content.data = {'home': 'h', 'id': 'other'}
    model.activateNavigation()
    assert received == [old] and model.captured is None


def test_session_detail_target_is_independent_of_selected_call():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay import SessionOverlay
    from cachemonitor.overlay_chrome import DetailModel
    from test_overlay_presentation import summary
    app = QApplication.instance() or QApplication([])
    window = SessionOverlay()
    try:
        data = summary(count=12)
        window.set_content(data)
        content = window.content_model
        old = data['recent'][0]
        content.select(content.call_id(old))
        model = DetailModel(content)
        content.set_content(summary(count=30))
        assert old not in content.rows()
        target = model.targets['session']
        assert (target.home, target.sid) == (data['home'], data['id']) and target.call_id is None
        content.set_content(summary(count=31))
        assert model.targets['session'] == target
        assert model.state['overlaySurface'] == content.state['overlaySurface']
        model.content.changed.disconnect(model.sync)
    finally:
        window.close()
        app.processEvents()


def test_dashboard_navigation_validates_session_and_selects_old_event_call(layout_controller):
    from cachemonitor.overlay_tracking import Selection
    from test_overlay_presentation import summary
    controller = layout_controller
    data = family_summary(count=30)
    old_row = summary(count=1)['recent'][0]
    requests = []
    controller.record_loader = lambda request, identity: requests.append(request)
    data['cache_degradation']['events'] = [dict(id='event', occurrence_keys=[old_row['id']])]
    model = publish(controller, data)
    activated = []
    controller.native.confirm_selection = lambda window: Selection(data['id'])
    controller.native.activate_target = lambda hwnd: activated.append(hwnd) or True
    target = navigation_target(data, 'incident', event=data['cache_degradation']['events'][0])
    model.pin_call(model.call_id(model.rows()[0]))
    assert controller.navigate_from_dashboard(target)['enabled']
    assert not model.graph_pinned
    controller.receive_record({'id': requests[-1], 'row': old_row})
    assert activated == [1] and model.selected_id == model.call_id(old_row)
    assert model.displayed_call()==model.selected()
    assert model.displayed_call()['cost']==old_row['cost']
    assert not any(model.call_id(row)==model.selected_id for row in model.rows())
    controller.refresh()
    assert model.displayed_call()==model.selected() and model.selected_id==model.call_id(old_row)
    from PySide6.QtCore import Qt
    controller.links.view.chartKey(Qt.Key_Escape)
    assert model.displayed_call()==model.rows()[-1] and model.follow_latest
    controller.native.confirm_selection = lambda window: Selection('other')
    assert not controller.navigate_from_dashboard(target)['enabled'] and activated == [1]


def test_chart_click_selects_call_without_opening_main_or_sub_details(layout_controller):
    controller = layout_controller
    model = publish(controller, family_summary())
    own = deepcopy(model.data['main_usage'])
    sub = deepcopy(model.detail_data())
    expanded = set(model.expanded_groups)
    layout = model.layout()
    controller.links.view.activateLink('call-0')
    assert model.graph_pinned and model.selected_id == model.call_id(model.rows()[0])
    assert not model.main_expanded and model.expanded_groups == expanded
    assert model.layout() == layout
    assert model.data['main_usage'] == own and model.detail_data() == sub
    controller.links.view.activateLink('call-0')
    assert not model.graph_pinned and model.follow_latest


def test_chart_keyboard_moves_pinned_selection_and_latest_escape_release_it(layout_controller):
    from PySide6.QtCore import Qt
    controller = layout_controller
    model = publish(controller, family_summary())
    navigation = controller.links.view
    rows = model.rows()
    assert navigation.chartKey(Qt.Key_Home)
    assert not model.graph_pinned and model.selected_id == model.call_id(rows[0])
    assert navigation.chartKey(Qt.Key_Left)
    assert model.selected_id == model.call_id(rows[0])
    assert navigation.chartKey(Qt.Key_Right)
    assert model.selected_id == model.call_id(rows[1])
    assert navigation.chartKey(Qt.Key_Return) and model.graph_pinned
    assert navigation.chartKey(Qt.Key_End)
    assert model.graph_pinned and model.selected_id == model.call_id(rows[-1])
    assert navigation.chartKey(Qt.Key_Return) and not model.graph_pinned
    navigation.chartKey(Qt.Key_Home)
    navigation.activateLink('latest')
    assert not model.graph_pinned and model.follow_latest
    navigation.chartKey(Qt.Key_Home)
    navigation.chartKey(Qt.Key_Escape)
    assert not model.graph_pinned and model.follow_latest
    assert not model.main_expanded and not model.expanded_groups
    assert not navigation.chartKey(Qt.Key_A)


def test_chart_root_receives_keyboard_navigation_and_enter_pin_toggle(layout_controller):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from tests.test_ui import wait_for_rendered
    c=layout_controller;m=publish(c,family_summary());rows=m.rows()
    c.links.begin_interaction();root=c.links.quick.rootObject();root.forceActiveFocus(Qt.OtherFocusReason)
    wait_for_rendered(lambda:root.hasActiveFocus())
    QTest.keyClick(c.links.quick,Qt.Key_Home)
    assert m.displayed_call()==rows[0] and not m.graph_pinned
    QTest.keyClick(c.links.quick,Qt.Key_Return)
    assert m.graph_pinned and m.selected_id==m.call_id(rows[0])
    QTest.keyClick(c.links.quick,Qt.Key_Right)
    assert m.graph_pinned and m.displayed_call()==rows[1]
    QTest.keyClick(c.links.quick,Qt.Key_Escape)
    assert not m.graph_pinned and m.displayed_call()==rows[-1]
    QTest.keyClick(c.links.quick,Qt.Key_Home)
    QTest.keyClick(c.links.quick,Qt.Key_End)
    assert not m.graph_pinned and m.displayed_call()==rows[-1]


def test_hover_cannot_replace_pinned_values_and_latest_clears_hover(layout_controller):
    controller = layout_controller
    model = publish(controller, family_summary())
    controller.links.view.activateLink('call-0')
    pinned = model.displayed_call()
    controller.links.view.inspectCall('call-1')
    assert model.displayed_call() == pinned
    controller.links.view.activateLink('latest')
    assert model.displayed_call() == model.rows()[-1]
    assert model.inspected_call is None


def test_agent_focus_targets_displayed_call_without_expanding_details(layout_controller):
    controller = layout_controller
    model = publish(controller, family_summary())
    child = next(row for row in model.rows() if row['sid'] != model.data['id'])
    child_index = model.rows().index(child)
    controller.links.view.inspectCall('call-' + str(child_index))
    expanded = set(model.expanded_groups)
    selected = model.selected_id
    controller.links.view.activateLink('focus-agent')
    key = model.detail_group_key(dict(home=child['home'], id=child['sid']), 'sub')
    assert model.state['detailFocusKey'] == 'group-' + key
    assert model.expanded_groups == expanded and not model.main_expanded
    assert model.selected_id == selected and not model.graph_pinned
    own = next(row for row in model.rows() if row['sid'] == model.data['id'])
    model.pin_call(model.call_id(own))
    controller.links.view.activateLink('focus-agent')
    assert model.state['mainFocused'] and not model.main_expanded
    assert model.expanded_groups == expanded


def test_captured_main_action_is_rejected_after_session_change(layout_controller):
    controller = layout_controller
    model = publish(controller, family_summary())
    controller.links.view.captureNavigation('main-detail')
    changed = deepcopy(model.data)
    changed['id'] = 'other-session'
    model.set_content(changed)
    controller.links.view.activateNavigation()
    assert not model.main_expanded


def test_chart_wheel_forwards_to_target_without_scrolling_overlay(layout_controller):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    controller = layout_controller
    model = publish(controller, family_summary())
    forwarded = []
    controller.native.forward_wheel = lambda *arguments: forwarded.append(arguments)
    link = next(link for link in model.monitor_links() if link['id'] == 'call-0')
    scale = model.appearance.scale
    point = QPointF((link['x'] + link['width'] / 2) * scale,
                    (link['y'] + link['height'] / 2) * scale)
    global_point = QPointF(controller.links.quick.mapToGlobal(point.toPoint()))
    event = QWheelEvent(point, global_point, QPoint(), QPoint(0, -120),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    before = model.layout(), model.selected_id, model.main_expanded, set(model.expanded_groups)
    QApplication.sendEvent(controller.links.quick, event)
    assert len(forwarded) == 1
    assert forwarded[0][0] == controller.target_state['target']['hwnd']
    assert forwarded[0][1] == event.angleDelta()
    assert (model.layout(), model.selected_id, model.main_expanded, set(model.expanded_groups)) == before


def test_sub_scroll_keeps_main_charts_costs_and_selection_fixed(layout_controller):
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    controller = layout_controller
    model = publish(controller, family_summary(count=3, children=8))
    for bounds in ((0, 0, 1300, 1000), (0, 0, 550, 580)):
        controller.native.bounds = bounds
        controller.refresh()
        assert controller.widget.isVisible() and controller.detail.isVisible()
        root = controller.detail.quick.rootObject()
        scroll = named_item(root, 'detailScroll')
        wait_for_rendered(lambda: scroll.property('contentHeight') > scroll.height())
        layout = model.layout()
        geometry = controller.monitor_geometry
        selected = model.selected_id
        total = model.total_cost()
        own = deepcopy(model.data['main_usage'])
        maximum = scroll.property('contentHeight') - scroll.height()
        scroll.setProperty('contentY', maximum)
        wait_for_rendered(lambda: scroll.property('contentY') > 0)
        assert model.layout() == layout and controller.monitor_geometry == geometry
        assert model.selected_id == selected and model.total_cost() == total
        assert model.data['main_usage'] == own
        assert model.detailBody.scroll_offset == scroll.property('contentY')
        assert not controller.detail.qml_errors
    assert model.compact


def test_sub_disclosure_changes_only_its_own_details(layout_controller):
    from cachemonitor.overlay_chrome import named_item
    from cachemonitor.quick_qa import click
    from tests.test_ui import wait_for_rendered
    controller = layout_controller
    model = publish(controller, family_summary(children=4))
    group = model.detail_groups()[0]
    layout = model.layout()
    selected = model.selected_id
    own = deepcopy(model.data['main_usage'])
    root = controller.detail.quick.rootObject()
    action = named_item(root, 'detailAction-group-' + group['key'])
    click(controller.detail, action)
    wait_for_rendered(lambda: group['key'] in model.expanded_groups)
    assert model.layout() == layout and model.selected_id == selected
    assert not model.main_expanded and model.data['main_usage'] == own
    action = named_item(root, 'detailAction-group-' + group['key'])
    click(controller.detail, action)
    wait_for_rendered(lambda: group['key'] not in model.expanded_groups)


def test_tooltip_translations_and_theme_contrast():
    from cachemonitor.tooltips import TEXT
    from cachemonitor.i18n import tr, set_language
    from cachemonitor.token_colors import ui_palette, contrast_ratio
    from cachemonitor.overlay_appearance import default_appearance
    try:
        set_language('en')
        for value in TEXT.values():
            assert tr(value) != value
        for dark in (True, False):
            colors = ui_palette(default_appearance(dark))
            assert colors['tooltip_surface'] != colors['panel']
            assert contrast_ratio(colors['tooltip_surface'], colors['tooltip_ink']) >= 4.5
    finally:
        set_language('ko')
