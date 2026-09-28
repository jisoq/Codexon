from cachemonitor.overlay_navigation import navigation_target
from test_overlay_layout_controller import layout_controller


def test_composition_is_passive_and_controls_persist(tmp_path):
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QSettings, QPoint
    from cachemonitor.overlay import SessionOverlay
    from cachemonitor.overlay_chrome import OverlayLinks
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();m=w.content_model
    m.settings=QSettings(str(tmp_path/'prefs.ini'),QSettings.IniFormat)
    links=OverlayLinks(m)
    try:
        w.set_content(summary());links.sync();before=m.base_height()
        links.view.keyboardActivate('unit-usd')
        assert m.composition_unit=='usd' and m.settings.value('overlay/compositionUnit')=='usd'
        links.view.keyboardActivate('tab-latest')
        assert m.monitor_tab=='latest' and m.base_height()==before
        assert not any(r.get('formula') or r['id'].startswith('composition-') for r in m.monitor_links())
        assert not links.mask().contains(QPoint(50,m.layout()['rows']+9))
        assert m.monitor_tab=='latest'
        links.view.keyboardActivate('tab-history')
        assert m.settings.value('overlay/monitorTab')=='history'
        w.set_content(dict(summary(),id='other'));assert not m.detail_links()
        assert not links.qml_errors
    finally:links.close();w.close();app.processEvents()


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
        data=summary(count=12);window.set_content(data);content=window.content_model
        old=data['recent'][0];content.select(content.call_id(old));model=DetailModel(content)
        content.set_content(summary(count=30))
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
    c=layout_controller;data=summary(count=30);old_row=summary(count=1)['recent'][0];old=old_row['id']
    requests=[];c.record_loader=lambda request,identity:requests.append(request)
    data['cache_degradation']['events']=[dict(id='event',occurrence_keys=[old])]
    c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
    activated=[];c.native.confirm_selection=lambda window:Selection(data['id'])
    c.native.activate_target=lambda hwnd:activated.append(hwnd) or True
    target=navigation_target(data,'incident',event=data['cache_degradation']['events'][0])
    c.widget.content_model.pin_call(c.widget.content_model.call_id(c.widget.content_model.rows()[0]))
    assert c.navigate_from_dashboard(target)['enabled']
    assert not c.widget.content_model.graph_pinned
    c.receive_record({'id':requests[-1],'row':old_row})
    assert activated==[1] and c.widget.content_model.selected_id==c.widget.content_model.call_id(old_row)
    received=[];c.navigation_requested.connect(received.append)
    c.detail.focus_control('openDashboard');QTest.qWait(20)
    button=named_item(c.detail.quick.rootObject(),'openDashboard')
    assert button.hasActiveFocus()
    QTest.keyClick(c.detail.quick,Qt.Key_Space)
    assert received[-1].call_id==old
    c.native.confirm_selection=lambda window:Selection('other')
    assert not c.navigate_from_dashboard(target)['enabled'] and activated==[1]


def test_current_default_and_saved_tab_restoration(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay import OverlayController
    app=QApplication.instance() or QApplication([])
    prefs=QSettings(str(tmp_path/'tabs.ini'),QSettings.IniFormat)
    first=OverlayController(prefs,native_enabled=False)
    try:
        assert first.widget.content_model.monitor_tab=='latest'
        first.widget.content_model.monitor_action('tab-history')
    finally:first.stop()
    second=OverlayController(prefs,native_enabled=False)
    try:assert second.widget.content_model.monitor_tab=='history'
    finally:second.stop()


def test_unique_entries_selection_and_captured_call(layout_controller):
    from test_overlay_presentation import summary
    from cachemonitor.overlay_tracking import Selection
    c=layout_controller;data=summary(count=12)
    c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
    m=c.widget.content_model;received=[];c.navigation_requested.connect(received.append)
    assert {r['id'] for r in m.monitor_links() if r.get('target')}=={'session'}
    c.links.view.keyboardActivate('session')
    assert received[-1].sort=='time_desc' and received[-1].view=='requests'
    m.monitor_action('tab-history');c.refresh()
    c.links.view.keyboardActivate('call-0')
    assert not c.expanded and m.selected_id==m.call_id(data['recent'][0])
    c.capture_detail()
    c.receive_snapshot({'overlay_sessions':[summary(count=40)]})
    c.activate_detail()
    assert c.expanded and m.selected()['id']==data['recent'][0]['id']
    assert not m.detail_links()
    formulas=[r[0] for r in m._detail_items]
    assert '계산 기준' in formulas
    c.escape();assert not c.expanded


def test_passive_metrics_and_inline_header_regions(layout_controller):
    from PySide6.QtCore import QPoint
    from cachemonitor.overlay_chrome import named_item
    c=layout_controller;m=c.widget.content_model
    c.links.sync()
    assert not c.links.mask().contains(QPoint(50,m.layout()['cache']+35))
    assert not c.links.mask().contains(QPoint(50,m.layout()['result_value']+10))
    assert named_item(c.actions.quick.rootObject(),'expand') is not None
    c.expanded=True;c.native.bounds=(0,0,550,1000);c.refresh()
    if m.detail_inline:
        assert c.header.width()==round(134*m.appearance.scale)
    assert named_item(c.detail.quick.rootObject(),'closeDetail') is not None
    assert not any(control.qml_errors for control in c.chrome)


def test_header_pointer_and_graph_pin_values(layout_controller,monkeypatch):
    from PySide6.QtCore import Qt,QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import Drawing
    from cachemonitor.overlay_chrome import named_item
    c=layout_controller;m=c.widget.content_model
    m.monitor_action('tab-history');c.refresh()
    def click_call(index):
        link=next(r for r in m.monitor_links() if r['id']=='call-'+str(index))
        QTest.mouseClick(c.links.quick,Qt.LeftButton,Qt.NoModifier,QPoint(round((link['x']+link['width']/2)*m.appearance.scale),round((link['y']+20)*m.appearance.scale)))
        QTest.qWait(20)
    click_call(0);assert m.graph_pinned
    values=[];original=Drawing.text
    def record(d,text,*args,**kw):
        values.append(str(text));return original(d,text,*args,**kw)
    monkeypatch.setattr(Drawing,'text',record)
    def render():
        values.clear();image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied);image.fill(0)
        painter=QPainter(image);m.paint(painter);painter.end();return list(values)
    before=render();c.links.view.inspectCall('call-1');assert render()==before
    button=named_item(c.actions.quick.rootObject(),'expand')
    point=button.mapToScene(button.boundingRect().center()).toPoint()
    QTest.mouseClick(c.actions.quick,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(30)
    assert c.expanded
    QTest.mouseClick(c.actions.quick,Qt.LeftButton,Qt.NoModifier,point);QTest.qWait(30)
    assert not c.expanded
    click_call(0);assert not m.graph_pinned
    c.capture_detail();m.set_content(dict(m.data,id='changed'));c.activate_detail()
    assert not c.expanded
