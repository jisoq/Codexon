"""Call selection, scoped navigation, and embedded Sub viewport input."""
from copy import deepcopy

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


def test_charts_have_no_navigation_or_selection_hit_regions(layout_controller):
    controller=layout_controller
    model=publish(controller,family_summary())
    links=model.monitor_links()
    assert not any(link.get('interaction')=='select' for link in links)
    chart_top=model.layout()['charts'][0]
    chart_bottom=model.layout()['charts'][-1]+model.layout()['chart_height']
    chart_links=[link for link in links if link['y']+link['height']>chart_top and link['y']<chart_bottom]
    assert chart_links and all(link['interaction']=='inspect' and not link.get('action') and not link.get('target') for link in chart_links)
    assert {link['id'] for link in links}.isdisjoint({'latest','focus-agent'})


def test_chart_keyboard_does_not_replace_latest_call_or_open_rows(layout_controller):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from tests.test_ui import wait_for_rendered
    c=layout_controller;m=publish(c,family_summary())
    c.links.begin_interaction();root=c.links.quick.rootObject();root.forceActiveFocus(Qt.OtherFocusReason)
    wait_for_rendered(lambda:root.hasActiveFocus())
    latest=m.latest_call()
    for key in (Qt.Key_Home,Qt.Key_Left,Qt.Key_Right,Qt.Key_End,Qt.Key_Return):
        QTest.keyClick(c.links.quick,key)
        assert m.latest_call()==latest and not m.main_expanded and not m.expanded_groups


def test_latest_child_sorts_without_glow_until_its_visible_content_changes(layout_controller):
    from test_ui import wait_for_rendered
    c=layout_controller;data=family_summary();m=publish(c,data)
    wait_for_rendered(lambda:not m.row_glows.needs_frame)
    child=next(row for row in data['recent'] if row['sid']!=data['id'])
    updated=deepcopy(data)
    latest=dict(child,id='new-child-call',key='new-child-call')
    updated['recent'].append(latest);updated['latest']=latest
    target=next(agent for agent in updated['subagent_finance']['agents'] if agent['id']==child['sid'])
    target['latest_ts']=max(agent.get('latest_ts') or 0 for agent in updated['subagent_finance']['agents'])+1
    m.set_content(updated)
    assert m.detail_groups()[0]['id']==child['sid']
    assert not m.row_glows.needs_frame
    changed=deepcopy(updated)
    target=next(agent for agent in changed['subagent_finance']['agents'] if agent['id']==child['sid'])
    target['cost']+=.5;m.set_content(changed)
    owner=(child['home'],child['sid'])
    wait_for_rendered(lambda:m.row_glow_opacity(owner)>0)
    highlights=[item for item in m.detail_items() if item[8].get('kind')=='agent-update' and item[8]['alpha']>0]
    assert len(highlights)==1 and highlights[0][8]['owner']==owner
    assert not m.expanded_groups and not m.main_expanded


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
    layout = model.layout()
    scale = model.appearance.scale
    point = QPointF(controller.links.quick.width() / 2,
                    (layout['charts'][0] + layout['chart_height'] / 2) * scale)
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
    from test_overlay_layout_controller import compact_frame
    from tests.test_ui import wait_for_rendered
    controller = layout_controller
    model = publish(controller, family_summary(count=3, children=8))
    short=compact_frame(controller)
    for bounds,compact in (((0,0,1300,1000),False),((0,0,550,short[3]),True)):
        controller.native.bounds = bounds
        controller.refresh()
        assert controller.widget.isVisible() and controller.detail.isVisible()
        assert model.compact is compact
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


def test_chart_hover_inspects_values_without_click_selection_or_agent_changes(layout_controller):
    from PySide6.QtCore import QPoint, QPointF, Qt, QEvent
    from PySide6.QtGui import QEnterEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_chrome import named_item
    from test_overlay_layout_controller import compact_frame
    from tests.test_ui import wait_for_rendered
    controller=layout_controller
    model=publish(controller,family_summary(count=12,children=3))
    controller.native.bounds=compact_frame(controller)
    controller.refresh()
    assert model.compact
    before=(model.selected_id,model.graph_pinned,model.main_expanded,set(model.expanded_groups),model.detail_items())
    latest=model.latest_call();inspected=model.rows()[0]
    assert model.call_id(inspected)!=model.call_id(latest)
    def hover(point):
        # The fake native placer intentionally leaves companion windows stacked.
        # Deliver real hover events to the rendered chart's Quick surface.
        quick=controller.links.quick;local=QPointF(point);global_pos=QPointF(quick.mapToGlobal(point))
        QApplication.sendEvent(quick,QEnterEvent(local,local,global_pos))
        QApplication.sendEvent(quick,QMouseEvent(QEvent.MouseMove,local,global_pos,Qt.NoButton,Qt.NoButton,Qt.NoModifier))
    for style in ('bar','filled-line'):
        model.chart_style=style;model.update()
        for chart in range(3):
            item=named_item(controller.links.quick.rootObject(),f'nav-inspect-{chart}-0')
            wait_for_rendered(lambda:item is not None and item.isVisible() and item.width()>0)
            point=item.mapToScene(QPointF(item.width()/2,item.height()/2)).toPoint()
            hover(point)
            wait_for_rendered(lambda:model.displayed_call()==inspected)
            QTest.mouseClick(controller.links.quick,Qt.LeftButton,pos=point)
            assert before==(model.selected_id,model.graph_pinned,model.main_expanded,set(model.expanded_groups),model.detail_items())
            assert model.latest_call()==latest
            hover(QPoint(1,1))
            wait_for_rendered(lambda:model.displayed_call()==latest)
    assert not controller.links.qml_errors


def test_sub_disclosure_grows_viewport_without_changing_main_usage(layout_controller):
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
    opened = model.layout()
    assert opened['height'] > layout['height'] and opened['sub_y'] == layout['sub_y']
    assert model.expanded_groups == {group['key']} and model.selected_id == selected
    assert not model.main_expanded and model.data['main_usage'] == own
    action = named_item(root, 'detailAction-group-' + group['key'])
    click(controller.detail, action)
    wait_for_rendered(lambda: group['key'] not in model.expanded_groups)
    assert model.layout() == layout


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
