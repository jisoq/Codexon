from cachemonitor.overlay_navigation import navigation_target
from test_overlay_layout_controller import layout_controller


def family_summary(count=12, children=3):
    from cachemonitor.core import Session
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    sessions=[]
    for index in range(children+1):
        sid='clean' if index==0 else 'child-'+str(index)
        session=Session(sid,'home',title='Main task' if index==0 else 'Sub task '+str(index))
        for call in range(count if index==0 else 3):
            session.add_usage(1789884000+call*10+index,str(index)+'-'+str(call),
                dict(input_tokens=100000,cached_input_tokens=90000,cache_write_input_tokens=0,
                     output_tokens=1000,reasoning_output_tokens=400),
                'gpt-6-astra' if index==0 else ('gpt-6-sol' if index%2 else 'gpt-6-luna'),
                'turn','high','Standard')
        value=session.view(1789884100)
        if index:value.update(parent_thread_id='clean',source='subagent')
        sessions.append(value)
    engine=AnalysisEngine();engine.ingest(sessions)
    return next(row for row in OverlaySummaries().collect(engine) if row['id']=='clean')


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
        links.view.activateLink('unit-usd')
        assert m.composition_unit=='usd' and m.settings.value('overlay/compositionUnit')=='usd'
        links.view.activateLink('tab-latest')
        assert m.monitor_tab=='history' and m.base_height()==before
        assert not any(r.get('formula') or r['id'].startswith('composition-') for r in m.monitor_links())
        assert links.mask().contains(QPoint(50,m.layout()['rows']+9))
        assert all(r.get('interaction')=='tooltip' for r in m.monitor_links() if r['x']<=50<r['x']+r['width'] and r['y']<=m.layout()['rows']+9<r['y']+r['height'])
        assert m.monitor_tab=='history'
        links.view.activateLink('tab-history')
        assert not any(r['id'].startswith('tab-') for r in m.monitor_links())
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


def test_session_detail_target_is_independent_of_selected_call():
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
        target=model.targets['session']
        assert (target.home,target.sid)==(data['home'],data['id']) and target.call_id is None
        content.set_content(summary(count=31))
        assert model.targets['session']==target
        assert model.state['overlaySurface']==content.state['overlaySurface']
        model.content.changed.disconnect(model.sync)
    finally:window.close();app.processEvents()


def test_dashboard_navigation_validates_session_and_selects_old_event_call(layout_controller):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_chrome import named_item
    from cachemonitor.overlay_tracking import Selection
    from test_overlay_presentation import summary
    c=layout_controller;data=family_summary(count=30);old_row=summary(count=1)['recent'][0];old=old_row['id']
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
    button=named_item(c.detail.quick.rootObject(),'openDashboard')
    from cachemonitor.quick_qa import click
    click(c.detail,button)
    assert received[-1].call_id is None and received[-1].sid==data['id']
    c.native.confirm_selection=lambda window:Selection('other')
    assert not c.navigate_from_dashboard(target)['enabled'] and activated==[1]


def test_overlay_defaults_and_saved_selection_restoration(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay import OverlayController
    app=QApplication.instance() or QApplication([])
    prefs=QSettings(str(tmp_path/'tabs.ini'),QSettings.IniFormat)
    first=OverlayController(prefs,native_enabled=False)
    try:
        m=first.widget.content_model
        assert (m.monitor_tab,m.composition_unit,m.detail_group)==('history','usd','sub')
        from cachemonitor.overlay_monitor import toggle_segments
        assert not any(r['id'].startswith('tab-') for r in m.monitor_links())
        assert [segment[2] for segment in toggle_segments(m,'unit')]==['usd','tokens']
        assert [segment['id'] for segment in m.state['detailGroupSegments']]==['sub','model']
        m.monitor_action('tab-latest');m.monitor_action('unit-tokens')
    finally:first.stop()
    second=OverlayController(prefs,native_enabled=False)
    try:
        assert second.widget.content_model.monitor_tab=='history'
        assert second.widget.content_model.composition_unit=='tokens'
    finally:second.stop()


def test_unique_entries_selection_and_session_detail(layout_controller):
    from test_overlay_presentation import summary
    from cachemonitor.overlay_tracking import Selection
    c=layout_controller;data=family_summary(count=12)
    c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
    m=c.widget.content_model;m.monitor_action('tab-latest');received=[];c.navigation_requested.connect(received.append)
    assert {r['id'] for r in m.monitor_links() if r.get('interaction')=='navigate'}=={'session'}
    c.links.view.activateLink('session')
    assert received[-1].sort=='time_desc' and received[-1].view=='requests'
    m.monitor_action('tab-history');c.refresh()
    c.links.view.activateLink('call-0')
    assert not c.expanded and m.selected_id==m.call_id(data['recent'][0])
    c.capture_detail()
    c.receive_snapshot({'overlay_sessions':[family_summary(count=40)]})
    current_selection=m.selected_id
    c.activate_detail()
    assert c.expanded and m.selected_id==current_selection
    assert not m.detail_links()
    assert m.detail_groups() and m.detail_data()['cost']==data['subagent_finance']['cost']
    c.escape();assert not c.expanded


def test_passive_metrics_and_inline_header_regions(layout_controller):
    from PySide6.QtCore import QPoint
    from cachemonitor.overlay_chrome import named_item
    c=layout_controller;m=c.widget.content_model
    from cachemonitor.overlay_tracking import Selection
    data=family_summary()
    c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
    c.links.sync()
    assert c.links.mask().contains(QPoint(50,m.layout()['cache']+35))
    assert c.links.mask().contains(QPoint(50,m.layout()['result_value']+10))
    before=m.selected_id
    for link in m.monitor_links():
        if link.get('interaction')=='tooltip':c.links.view.activateLink(link['id'])
    assert m.selected_id==before
    assert named_item(c.actions.quick.rootObject(),'expand') is not None
    c.expanded=True;c.native.bounds=(0,0,550,1000);c.refresh()
    assert c.header.width()>0
    assert named_item(c.detail.quick.rootObject(),'closeDetail') is None
    assert not any(control.qml_errors for control in c.chrome)


def test_detail_grouping_and_scrolling_keep_costs_and_panel_height(layout_controller):
    import time
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_chrome import named_item
    from cachemonitor.overlay_tracking import Selection
    from cachemonitor.quick_qa import click
    c=layout_controller;m=c.widget.content_model;m.set_detail_group('model');data=family_summary(count=3,children=8)
    c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
    c.expanded=True
    def wait_for(condition):
        deadline=time.monotonic()+3
        while not condition():
            assert time.monotonic()<deadline
            QTest.qWait(10)
    for bounds in ((0,0,1300,500),(0,0,550,500)):
        c.native.bounds=bounds;c.refresh()
        root=c.detail.quick.rootObject();scroll=named_item(root,'detailScroll')
        height=c.detail.height();selected=m.selected_id
        model_cost=m.state['detailAgentCount'];sub_cost=m.state['detailSubCost']
        costs=named_item(root,'detailCostHeader')
        cost_position=costs.mapRectToScene(costs.boundingRect())
        assert {row['model'] for row in m.detail_groups()}=={'gpt-6-sol','gpt-6-luna'}
        click(c.detail,named_item(root,'detailMode-sub'))
        wait_for(lambda:len(m.state['detailActions'])==8)
        assert len(m.detail_groups())==8
        group=m.detail_groups()[0]
        expanded=group['key'] in m.expanded_groups
        action=named_item(root,'detailAction-group-'+group['key'])
        click(c.detail,action)
        wait_for(lambda:(group['key'] in m.expanded_groups)!=expanded)
        def current_action():return named_item(root,'detailAction-group-'+group['key'])
        wait_for(lambda:current_action().hasActiveFocus())
        assert not current_action().property('visualFocus')
        QTest.keyClick(c.detail.quick,Qt.Key_Tab)
        QTest.keyClick(c.detail.quick,Qt.Key_Tab,Qt.ShiftModifier)
        wait_for(lambda:current_action().hasActiveFocus() and current_action().property('visualFocus'))
        QTest.keyClick(c.detail.quick,Qt.Key_Space)
        wait_for(lambda:(group['key'] in m.expanded_groups)==expanded)
        wait_for(lambda:current_action().hasActiveFocus())
        assert current_action().property('visualFocus')
        maximum=scroll.property('contentHeight')-scroll.height()
        assert maximum>0
        scroll.setProperty('contentY',maximum)
        wait_for(lambda:scroll.property('contentY')>0)
        assert c.detail.height()==height and m.selected_id==selected
        assert costs.mapRectToScene(costs.boundingRect())==cost_position
        assert (m.state['detailAgentCount'],m.state['detailSubCost'])==(model_cost,sub_cost)
        click(c.detail,named_item(root,'detailMode-model'))
        wait_for(lambda:scroll.property('contentY')==0)
        assert c.detail.height()==height
        assert not c.detail.qml_errors
    c.toggle_expanded()
    assert not m.state['detailActions']


def test_header_pointer_and_graph_pin_values(layout_controller,monkeypatch):
    from PySide6.QtCore import Qt,QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import Drawing
    from cachemonitor.overlay_chrome import named_item
    c=layout_controller;m=c.widget.content_model
    from cachemonitor.overlay_tracking import Selection
    data=family_summary()
    c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
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




def test_tooltip_translations_and_theme_contrast():
    from cachemonitor.tooltips import TEXT
    from cachemonitor.i18n import tr,set_language
    from cachemonitor.token_colors import ui_palette,contrast_ratio
    from cachemonitor.overlay_appearance import default_appearance
    try:
        set_language('en')
        for value in TEXT.values():assert tr(value)!=value
        for dark in (True,False):
            colors=ui_palette(default_appearance(dark))
            assert colors['tooltip_surface']!=colors['panel']
            assert contrast_ratio(colors['tooltip_surface'],colors['tooltip_ink'])>=4.5
    finally:set_language('ko')
