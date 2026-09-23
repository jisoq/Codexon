from cachemonitor.overlay_navigation import navigation_target,NavigationTarget
from test_overlay_layout_controller import layout_controller


def test_navigation_keeps_exact_session_and_call_identity():
    data={'home':'h','id':'s','latest':{'id':'call','turn':'turn'}}
    assert navigation_target(data,'session')==NavigationTarget('h','s')
    target=navigation_target(data,'cost')
    assert (target.home,target.sid,target.call_id,target.request_id,target.section)==('h','s','call','turn','pricing')
    assert navigation_target(None,'session') is None


def test_pointer_intent_survives_call_refresh_but_not_session_change():
    from types import SimpleNamespace
    from cachemonitor.overlay_chrome import NavigationModel
    content=SimpleNamespace(data={'home':'h','id':'s','latest':{'id':'old'}})
    model=NavigationModel(content);received=[];model.navigationRequested.connect(received.append)
    old=navigation_target(content.data,'cache')
    model.link_state([dict(id='cache',target=old)])
    model.captureNavigation('cache')
    content.data['latest']={'id':'new'}
    model.link_state([dict(id='cache',target=navigation_target(content.data,'cache'))])
    model.activateNavigation()
    assert received==[old]
    model.captureNavigation('cache');content.data={'home':'h','id':'other'}
    model.activateNavigation()
    assert received==[old] and model.captured is None


def test_selected_call_outside_recent_window_is_footer_target():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay import SessionOverlay
    from cachemonitor.overlay_chrome import DetailModel
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([]);window=SessionOverlay()
    try:
        data=summary(count=30);window.set_content(data);content=window.content_model
        old=data['all_calls'][0];content.select(old['id']);model=DetailModel(content)
        assert old not in content.rows()
        assert model.targets['selected'].call_id==old['id']
        content.set_content(summary(count=31))
        assert model.targets['selected'].call_id==old['id']
        assert model.state['overlaySurface']==content.state['overlaySurface']
        model.content.changed.disconnect(model.sync)
    finally:window.close();app.processEvents()


def test_dashboard_navigation_validates_session_and_selects_old_event_call(layout_controller):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_chrome import named_item
    from cachemonitor.overlay_tracking import Selection
    from test_overlay_presentation import summary
    c=layout_controller;data=summary(count=30);old=data['all_calls'][0]['id']
    data['cache_degradation']['events']=[dict(id='event',occurrence_keys=[old])]
    c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
    activated=[];c.native.confirm_selection=lambda window:Selection(data['id'])
    c.native.activate_target=lambda hwnd:activated.append(hwnd) or True
    target=navigation_target(data,'incident',event=data['cache_degradation']['events'][0])
    assert c.navigate_from_dashboard(target)['enabled']
    assert activated==[1] and c.widget.content_model.selected_id==old
    received=[];c.navigation_requested.connect(received.append)
    c.detail.focus_control('openDashboard');QTest.qWait(20)
    button=named_item(c.detail.quick.rootObject(),'openDashboard')
    assert button.hasActiveFocus()
    QTest.keyClick(c.detail.quick,Qt.Key_Space)
    assert received[-1].call_id==old
    c.native.confirm_selection=lambda window:Selection('other')
    assert not c.navigate_from_dashboard(target)['enabled'] and activated==[1]
