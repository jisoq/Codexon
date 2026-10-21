from cachemonitor.quick_qa import click
from PySide6.QtQuick import QQuickItem
import sys
import json
import pytest
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_tracking import Selection


@pytest.mark.skipif(sys.platform!='win32',reason='Real Windows companion windows')
def test_native_collapse_restore_opacity_drag_and_control_window_styles(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setWindowTitle('Cache Monitor owned control test');host.resize(900,760);host.show()
    native=WindowsOverlay();settings=QSettings(str(tmp_path/'overlay.ini'),QSettings.IniFormat)
    # The popup timer must consume owned input, never a user's desktop click.
    cursor=[(0,0)];pressed=[False]
    native.cursor=lambda:cursor[0]
    native.primary_down=lambda:pressed[0]
    controller=OverlayController(settings,native_enabled=False,appearance_path=tmp_path/'missing.toml')
    controller.native=native
    native.configure(int(controller.widget.winId()))
    native.visible_target=lambda hwnd:bool(native.u.IsWindowVisible(hwnd) and not native.u.IsIconic(hwnd))
    hwnd=int(host.winId())
    def refresh():
        controller.receive_target({'target':{'hwnd':hwnd},'selection':Selection('fixture')})
        app.processEvents();QTest.qWait(40)
    try:
        refresh()
        assert controller.widget.isVisible() and controller.header.isVisible() and not controller.toolbar.isVisible()
        panel=int(controller.widget.winId());before=native.frame(panel)
        assert native.u.GetWindowLongPtrW(panel,-20)&0x20
        for child in controller.chrome:
            flags=native.u.GetWindowLongPtrW(int(child.winId()),-20)
            assert flags&0x08000000 and not flags&0x20
        controller.toggle_opacity();refresh()
        assert controller.toolbar.isVisible()
        controller.toolbar.slider.setValue(60)
        assert controller.opacity==40 and settings.value('overlay/opacity',type=int)==40
        assert controller.input_timer.isActive()
        x,y,_,_=controller.popup_geometry
        cursor[0]=(x+1,y+1);pressed[0]=True;controller.poll_popup()
        assert controller.popup_open and controller.toolbar.isVisible()
        pressed[0]=False;controller.poll_popup()
        cursor[0]=(-10000,-10000);pressed[0]=True;controller.poll_popup()
        assert not controller.popup_open and not controller.toolbar.isVisible() and not controller.input_timer.isActive()
        pressed[0]=False
        pixels=controller.widget.grab().toImage();scale=pixels.devicePixelRatio()
        assert 85<=pixels.pixelColor(round(6*scale),pixels.height()//2).alpha()<=110
        click(controller.actions,controller.actions.quick.rootObject().findChild(QQuickItem,'collapse'));refresh()
        assert controller.collapsed and controller.icon.isVisible() and not controller.widget.isVisible()
        assert not controller.header.isVisible() and not controller.toolbar.isVisible()
        QTest.mouseClick(controller.icon,Qt.LeftButton);refresh()
        assert not controller.collapsed and controller.widget.isVisible()
        cursor[0]=(0,0)
        start=controller.current_geometry
        controller.begin_drag();cursor[0]=(-120,-90);controller.move_drag();controller.end_drag();refresh()
        moved=native.frame(panel)
        assert abs(moved[0]-start[0]+120)<=1 and abs(moved[1]-start[1]+90)<=1
        # Fast queued input must use event positions even if the live pointer
        # has already reached the final point when the press is handled.
        start=controller.current_geometry
        native.cursor=lambda:(9999,9999)
        controller.begin_drag((500,500));controller.move_drag((420,440));controller.end_drag((420,440));refresh()
        moved=native.frame(panel)
        assert abs(moved[0]-start[0]+80)<=1 and abs(moved[1]-start[1]+60)<=1
        stored=controller.anchor
        restored=OverlayController(settings,native_enabled=False,appearance_path=tmp_path/'missing.toml')
        assert restored.anchor==stored and restored.opacity==40
        restored.stop()
        controller.set_enabled(False)
        assert not any(w.isVisible() for w in (controller.widget,*controller.chrome))
    finally:controller.stop();host.close();app.processEvents()


@pytest.fixture
def native_detail(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    from test_overlay_layout_controller import publish_subagents,A
    from tests.test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.resize(1280,900);host.show()
    native=WindowsOverlay()
    native.visible_target=lambda hwnd:bool(native.u.IsWindowVisible(hwnd))
    native.primary_down=lambda:False
    c=OverlayController(QSettings(str(tmp_path/'stack.ini'),QSettings.IniFormat),native_enabled=False)
    c.native=native
    def refresh():
        c.receive_target({'target':{'hwnd':int(host.winId())},'selection':Selection(A)})
        c.refresh();app.processEvents()
    try:
        refresh();publish_subagents(c);refresh()
        wait_for_rendered(lambda:c.detail.isVisible() and c.actions.quick is not None)
        yield c,host,refresh
    finally:c.stop();host.close();app.processEvents()


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows hit testing')
def test_subagent_disclosure_resizes_native_panel_and_scrolls_at_frame_limit(native_detail):
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail
    host.resize(420,1200);refresh()
    m=c.widget.content_model;key=m.detail_groups()[0]['key']
    closed=c.monitor_geometry;anchor=c.anchor
    body=m.state['detailBodyHeight']
    def group():return named_item(c.detail.quick.rootObject(),'detailAction-group-'+key)
    def scroll():return named_item(c.detail.quick.rootObject(),'detailScroll')
    click(c.detail,group())
    wait_for_rendered(lambda:c.monitor_geometry[3]>closed[3] and scroll().height()>=m.state['detailBodyHeight'])
    opened=c.monitor_geometry
    assert opened[3]-closed[3]>=m.state['detailBodyHeight']-body
    assert (opened[0],opened[2])==(closed[0],closed[2]) and c.anchor==anchor
    frame=c.native.frame(int(c.widget.winId()))
    assert frame[3]-frame[1]==opened[3]
    host.resize(420,closed[3]+100);refresh()
    wait_for_rendered(lambda:scroll().property('contentHeight')>scroll().height())
    assert c.widget.isVisible() and c.detail.isVisible()
    assert c.monitor_geometry[3]<opened[3] and key in m.expanded_groups
    scroll().setProperty('contentY',scroll().property('contentHeight')-scroll().height())
    host.resize(420,1200);refresh()
    wait_for_rendered(lambda:scroll().height()>=m.state['detailBodyHeight'])
    click(c.detail,group())
    wait_for_rendered(lambda:c.monitor_geometry==closed and scroll().property('contentY')==0)
    assert not m.expanded_groups and c.anchor==anchor


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows hit testing')
@pytest.mark.parametrize('width',[1280,420])
def test_embedded_subagent_interaction_keeps_gear_and_settings_reachable(native_detail,width):
    from ctypes import wintypes as W
    from PySide6.QtCore import QPointF
    from cachemonitor.overlay_chrome import named_item
    c,host,refresh=native_detail
    host.resize(width,900);refresh()
    assert c.detail.isVisible() and c.automatic_mode=='monitor'
    button=named_item(c.actions.quick.rootObject(),'settingsButton')
    model=c.widget.content_model;key=model.detail_groups()[0]['key']
    group=named_item(c.detail.quick.rootObject(),'detailAction-group-'+key)
    click(c.detail,group);refresh()
    assert key in model.expanded_groups and c.detail.isVisible()
    point=button.mapToScene(QPointF(button.width()/2,button.height()/2))
    x,y,w,h=c.actions_geometry;scale=w/c.actions.width()
    native_point=W.POINT(x+round(point.x()*scale),y+round(point.y()*scale))
    api=c.native.u
    api.WindowFromPoint.argtypes=[W.POINT];api.WindowFromPoint.restype=W.HWND
    assert api.WindowFromPoint(native_point)==int(c.actions.winId())
    click(c.actions,button);refresh()
    assert c.popup_open and c.popup_kind=='settings' and c.toolbar.isVisible()
    line=named_item(c.toolbar.quick.rootObject(),'chartStyle-filled-line')
    click(c.toolbar,line);refresh()
    assert c.chart_style=='filled-line'
    from tests.test_ui import wait_for_rendered
    count=named_item(c.toolbar.quick.rootObject(),'agentCount-4')
    wait_for_rendered(lambda:count.isVisible() and count.isEnabled() and count.width()>0)
    click(c.toolbar,count)
    wait_for_rendered(lambda:c.widget.content_model.visible_agent_count()==4)
    assert json.loads(c.settings.value('overlay/visibleAgentCounts'))[c.widget.content_model.agent_count_key()]==4
    click(c.toolbar,count)
    assert count.property('checked')
    refresh()
    x,y,w,h=c.popup_geometry
    assert api.WindowFromPoint(W.POINT(x+w//2,y+h//2))==int(c.toolbar.winId())
    c.escape();assert not c.popup_open
    click(c.actions,named_item(c.actions.quick.rootObject(),'opacityButton'));refresh()
    assert c.popup_kind=='opacity' and c.toolbar.isVisible()
    c.close_popup()
    click(c.actions,named_item(c.actions.quick.rootObject(),'collapse'));refresh()
    assert c.collapsed and c.icon.isVisible()
    QTest.mouseClick(c.icon,Qt.LeftButton);refresh()
    assert not c.collapsed and c.detail.isVisible()

@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows popup geometry')
def test_popup_controls_fit_native_viewport_and_remain_clickable(native_detail,tmp_path):
    from PySide6.QtCore import QPointF
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail
    host.resize(420,900);refresh()
    def item(name):
        wait_for_rendered(lambda:c.toolbar.quick is not None and c.toolbar.quick.rootObject() is not None)
        control=named_item(c.toolbar.quick.rootObject(),name)
        assert control is not None and control.isVisible()
        top=control.mapToScene(QPointF(0,0))
        bottom=control.mapToScene(QPointF(control.width(),control.height()))
        assert top.x()>=0 and top.y()>=0
        assert bottom.x()<=c.toolbar.quick.width()+1 and bottom.y()<=c.toolbar.quick.height()+1, name
        return control
    c.toggle_settings();refresh()
    for name in ('chartStyle-bar','chartStyle-filled-line',*(f'agentCount-{n}' for n in range(1,5))):item(name)
    click(c.toolbar,item('chartStyle-filled-line'))
    wait_for_rendered(lambda:c.chart_style=='filled-line')
    click(c.toolbar,item('agentCount-4'))
    wait_for_rendered(lambda:c.widget.content_model.visible_agent_count()==4)
    assert c.toolbar.grab().save(str(tmp_path/'settings.png'))
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    from test_overlay import source
    engine=AnalysisEngine();engine.ingest([source()])
    c.receive_snapshot(dict(overlay_sessions=OverlaySummaries().collect(engine)));refresh()
    assert not c.widget.content_model.has_subagents()
    click(c.toolbar,item('chartStyle-bar'))
    wait_for_rendered(lambda:c.chart_style=='bar')
    c.toggle_opacity();refresh()
    slider=item('opacity');before=c.opacity
    click(c.toolbar,slider,x=slider.width()*.75)
    wait_for_rendered(lambda:c.opacity!=before)
    assert c.settings.value('overlay/opacity',type=int)==c.opacity
    assert c.toolbar.grab().save(str(tmp_path/'opacity.png'))
    c.escape();assert not c.popup_open


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows popup stacking')
def test_overlay_tooltip_stays_above_panels_after_activation(native_detail):
    from ctypes import wintypes as W
    from PySide6.QtCore import QObject
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail;app=QApplication.instance()
    owner=c.actions
    button=named_item(owner.quick.rootObject(),'settingsButton')
    tip=next(o for o in button.findChildren(QObject) if o.metaObject().indexOfProperty('popupType')>=0)
    tip.setProperty('delay',0);tip.setProperty('visible',True)
    def popup():
        return next((w for w in app.topLevelWindows() if w.isVisible() and
                     w.transientParent() is owner.quick.quickWindow()),None)
    wait_for_rendered(lambda:popup() is not None)
    window=popup();api=c.native.u
    api.GetWindow.argtypes=[W.HWND,W.UINT];api.GetWindow.restype=W.HWND
    for layer in (c.detail,c.actions):
        layer.begin_interaction();refresh()
        assert window.isVisible()
        order=[];handle=api.GetWindow(int(window.winId()),0)
        while handle:
            order.append(handle);handle=api.GetWindow(handle,2)
        assert all(order.index(int(window.winId()))<order.index(int(w.winId()))
                   for w in (c.widget,c.detail,c.actions))
    tip.setProperty('visible',False)


@pytest.mark.skipif(sys.platform!='win32',reason='Native passive overlay updates')
def test_latest_agent_updates_preserve_manual_scroll_and_focus(native_detail):
    from copy import deepcopy
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail;m=c.widget.content_model;app=QApplication.instance()
    data=deepcopy(m.data);prototype=data['subagent_finance']['agents'][0]
    agents=[dict(prototype,id='passive-'+str(i),title='Agent '+str(i),latest_ts=100-i) for i in range(12)]
    data['subagent_finance']['agents']=agents;data['descendants']=len(agents)
    data['members']=[(data['home'],data['id']),*((agent['home'],agent['id']) for agent in agents)]
    c.receive_snapshot({'overlay_sessions':[data]});refresh()
    scroll=named_item(c.detail.quick.rootObject(),'detailScroll')
    wait_for_rendered(lambda:scroll.property('contentHeight')>scroll.height())
    offset=min(scroll.height()/2,scroll.property('contentHeight')-scroll.height())
    scroll.setProperty('contentY',offset)
    host.activateWindow();app.setActiveWindow(host);active=app.activeWindow()
    child=next(row for row in data['recent'] if row['sid']!=data['id'])
    latest=dict(child,sid=agents[-1]['id'],id='passive-call-1',key='passive-call-1')
    data['recent'].append(latest);data['latest']=latest
    data['subagent_finance']['agents'][-1]['latest_ts']=200
    c.receive_snapshot({'overlay_sessions':[data]});refresh()
    assert m.detail_groups()[0]['id']==agents[-1]['id']
    assert scroll.property('contentY')==offset
    assert app.activeWindow() is active and not m.expanded_groups and not m.main_expanded
    c.receive_snapshot({'overlay_sessions':[deepcopy(data)]});refresh()
    assert scroll.property('contentY')==offset and app.activeWindow() is active
    main=named_item(c.links.quick.rootObject(),'nav-main-detail')
    click(c.links,main)
    wait_for_rendered(lambda:m.main_expanded)
