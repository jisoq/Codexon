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


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows disclosure input')
@pytest.mark.parametrize('activation',['mouse','keyboard'])
def test_subagent_disclosure_retains_keyboard_target_without_qml_errors(native_detail,activation):
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail
    host.resize(420,1200);refresh()
    key=c.widget.content_model.detail_groups()[0]['key']
    root=c.detail.quick.rootObject()
    def group():return named_item(root,'detailAction-group-'+key)
    scroll=named_item(root,'detailScroll')
    wait_for_rendered(lambda:group() is not None and group().isVisible() and group().width()>0)
    assert not group().property('checked')
    closed_height=scroll.property('contentHeight')
    if activation=='keyboard':
        c.detail.begin_interaction()
        group().forceActiveFocus(Qt.TabFocusReason)
        wait_for_rendered(lambda:group().hasActiveFocus())
    for expanded in (True,False):
        if activation=='mouse':click(c.detail,group())
        else:QTest.keyClick(c.detail.quick,Qt.Key_Space)
        wait_for_rendered(lambda:group() is not None and group().property('checked') is expanded
                          and group().hasActiveFocus()
                          and c.detail.quick.quickWindow().activeFocusItem() is group())
        if expanded:assert scroll.property('contentHeight')>closed_height
        else:assert scroll.property('contentHeight')==closed_height
        assert group().hasActiveFocus(), c.detail.qml_errors
        assert c.detail.quick.quickWindow().activeFocusItem() is group()
        assert not c.detail.qml_errors
        assert not c.detail.quick.grabFramebuffer().isNull()
    QTest.keyClick(c.detail.quick,Qt.Key_Space)
    wait_for_rendered(lambda:group().property('checked') and group().hasActiveFocus()
                      and c.detail.quick.quickWindow().activeFocusItem() is group())
    assert group().hasActiveFocus() and not c.detail.qml_errors


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
    def capture_geometry(stage):
        import os
        import time
        from pathlib import Path
        destination=os.environ.get('CODEXON_MISMATCH_EVIDENCE')
        if not destination:return
        output=Path(destination);output.mkdir(parents=True,exist_ok=True)
        host_id=int(host.winId());dpi=c.native.u.GetDpiForWindow(host_id) or 96
        target_frame=c.native.frame(host_id);viewport=scroll()
        def rect(value):return [value.x(),value.y(),value.width(),value.height()]
        screen=host.windowHandle().screen()
        sample=dict(stage=stage,at=time.monotonic(),host_size=[host.width(),host.height()],
            host_geometry=rect(host.geometry()),host_frame_geometry=rect(host.frameGeometry()),
            host_dpr=host.devicePixelRatioF(),dpi=dpi,native_factor=dpi/96,
            native_host_frame=target_frame,native_panel_frame=c.native.frame(int(c.widget.winId())),
            native_detail_frame=c.native.frame(int(c.detail.winId())),
            screen_geometry=rect(screen.geometry()),screen_dpr=screen.devicePixelRatio(),
            appearance_scale=m.appearance.scale,appearance_font_size=m.appearance.font_size,
            monitor_geometry=c.monitor_geometry,current_geometry=c.current_geometry,
            closed_geometry=closed,automatic_mode=c.automatic_mode,compact=m.compact,
            available_height=m.available_height,layout=m.layout(),unconstrained_layout=m.layout(constrain=False),
            automatic_sub_height=m.automatic_sub_height(),summary_heights=m._sub_summary_heights,
            expanded_height=m._sub_expanded_height,expanded_groups=sorted(m.expanded_groups),
            detail_body_height=m.state['detailBodyHeight'],detail_view_body_height=c.detail.view.state['detailBodyHeight'],
            detail_widget_size=[c.detail.width(),c.detail.height()],
            detail_root_geometry=rect(c.detail.quick.rootObject().boundingRect()),
            viewport_geometry=rect(viewport.boundingRect()),viewport_height=viewport.height(),
            content_height=viewport.property('contentHeight'),content_y=viewport.property('contentY'))
        path=output/'native-subagent-height.json'
        samples=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        samples.append(sample);path.write_text(json.dumps(samples,indent=2),encoding='utf-8')
    capture_geometry('closed')
    click(c.detail,group())
    wait_for_rendered(lambda:c.monitor_geometry[3]>closed[3] and scroll().height()>=m.state['detailBodyHeight'])
    opened=c.monitor_geometry
    capture_geometry('opened')
    assert opened[3]-closed[3]>=m.state['detailBodyHeight']-body
    assert (opened[0],opened[2])==(closed[0],closed[2]) and c.anchor==anchor
    frame=c.native.frame(int(c.widget.winId()))
    assert frame[3]-frame[1]==opened[3]
    host.resize(420,round(closed[3]/host.devicePixelRatioF())+100);refresh()
    capture_geometry('limited-refresh')
    try:wait_for_rendered(lambda:scroll().property('contentHeight')>scroll().height())
    finally:capture_geometry('limited-after-wait')
    assert c.widget.isVisible() and c.detail.isVisible()
    assert c.monitor_geometry[3]<opened[3] and key in m.expanded_groups
    scroll().setProperty('contentY',scroll().property('contentHeight')-scroll().height())
    host.resize(420,1200);refresh()
    wait_for_rendered(lambda:scroll().height()>=m.state['detailBodyHeight'])
    capture_geometry('restored')
    click(c.detail,group())
    wait_for_rendered(lambda:c.monitor_geometry==closed and scroll().property('contentY')==0)
    capture_geometry('closed-restored')
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
@pytest.mark.parametrize('locale',['ko','en'])
def test_popup_controls_fit_native_viewport_and_remain_clickable(native_detail,tmp_path,locale,request):
    from PySide6.QtCore import QPointF
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    from cachemonitor.i18n import set_language,language
    original=language();set_language(locale)
    request.addfinalizer(lambda:set_language(original))
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
    for name in ('chartStyle-bar','chartStyle-filled-line','highlightStyle','highlightPreview',*(f'agentCount-{n}' for n in range(1,5))):item(name)
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


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows heartbeat hover')
def test_refresh_freshness_uses_activity_label_native_hit_region(native_detail, monkeypatch):
    from copy import deepcopy
    from ctypes import wintypes as W
    from PySide6.QtCore import QObject,QPointF,QEvent
    from PySide6.QtGui import QAccessible,QEnterEvent,QMouseEvent
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail;app=QApplication.instance();model=c.widget.content_model
    monkeypatch.setattr('cachemonitor.overlay_view.time.time',lambda:1000)
    data=deepcopy(model.data);data['active_requests']=2
    for compact,reason in ((False,'analysis_delay'),(True,'detail_pending')):
        data['_collection']={'reason':reason,'received_age':16,'last_confirmed_at':700}
        c.widget.set_layout(reduced=compact)
        c.widget.set_content(deepcopy(data),'갱신 중')
        wait_for_rendered(lambda:c.links.quick is not None and c.links.quick.rootObject() is not None)
        link=named_item(c.links.quick.rootObject(),'nav-heartbeat')
        assert link is not None
        wait_for_rendered(lambda:link.width()>0 and link.height()>0)
        tip=next(item for item in link.findChildren(QObject) if item.metaObject().indexOfProperty('popupType')>=0)
        tip.setProperty('delay',0)
        detail=model.status_detail()
        assert detail and model.freshness_text() in detail
        accessible=QAccessible.queryAccessibleInterface(link)
        assert accessible is not None and detail in accessible.text(QAccessible.Name)
        assert tip.property('text')==detail
        center=link.mapToScene(QPointF(link.width()/2,link.height()/2)).toPoint()
        assert c.links.mask().contains(center)
        left,top,right,bottom=c.native.frame(int(c.links.winId()));scale=(right-left)/c.links.width()
        api=c.native.u;api.WindowFromPoint.argtypes=[W.POINT];api.WindowFromPoint.restype=W.HWND
        assert api.WindowFromPoint(W.POINT(left+round(center.x()*scale),top+round(center.y()*scale)))==int(c.links.winId())
        local=QPointF(center);global_pos=QPointF(c.links.quick.mapToGlobal(center))
        app.sendEvent(c.links.quick,QEnterEvent(local,local,global_pos))
        app.sendEvent(c.links.quick,QMouseEvent(QEvent.MouseMove,local,global_pos,Qt.NoButton,Qt.NoButton,Qt.NoModifier))
        wait_for_rendered(lambda:tip.property('visible'))
        assert model.state['heartbeatMode']=='pending'
        label=named_item(c.widget.quick.rootObject(),'overlayHeartbeatLabel')
        accessible=QAccessible.queryAccessibleInterface(label)
        assert accessible is not None and detail in accessible.text(QAccessible.Description)
        assert not c.links.qml_errors and not c.widget.qml_errors
        tip.setProperty('visible',False)


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows dropdown input')
def test_highlight_choice_retains_settings_with_native_popup_foreground(native_detail,monkeypatch):
    import ctypes
    import time
    from ctypes import wintypes as W
    from types import SimpleNamespace
    from PySide6.QtCore import QPointF
    from cachemonitor.overlay_chrome import named_item
    from cachemonitor.overlay_icons import agent_palette
    from cachemonitor.overlay_view import HighlightStyle
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail;host.resize(420,900);refresh()
    native=c.native
    state=SimpleNamespace(foreground=int(host.winId()),pressed=False,position=(0,0))
    monkeypatch.delattr(native,'visible_target')
    monkeypatch.delattr(native,'primary_down')
    monkeypatch.setattr(native.u,'GetForegroundWindow',lambda:state.foreground)
    monkeypatch.setattr(native.u,'GetAsyncKeyState',lambda key:0x8000 if state.pressed else 0)
    def cursor(point):
        position=ctypes.cast(point,ctypes.POINTER(W.POINT)).contents
        position.x,position.y=state.position
        return True
    monkeypatch.setattr(native.u,'GetCursorPos',cursor)
    c.toggle_settings();refresh()
    state.foreground=int(c.toolbar.winId())
    choice=named_item(c.toolbar.quick.rootObject(),'highlightStyle')
    def popup():
        return next((window for window in QApplication.topLevelWindows() if window.isVisible()
                     and window.transientParent() is c.toolbar.quick.quickWindow()),None)
    click(c.toolbar,choice)
    wait_for_rendered(lambda:popup() is not None)
    window=popup();option=named_item(window.contentItem(),'highlightStyle-trail')
    wait_for_rendered(lambda:option is not None and option.isVisible())
    point=option.mapToScene(QPointF(option.width()/2,option.height()/2)).toPoint()
    left,top,right,bottom=native.frame(int(window.winId()))
    factor=(right-left)/window.width()
    state.position=(left+round(point.x()*factor),top+round(point.y()*factor))
    x,y,w,h=c.popup_geometry
    assert not (x<=state.position[0]<x+w and y<=state.position[1]<y+h)
    state.foreground=int(window.winId());state.pressed=True
    QTest.mousePress(window,Qt.LeftButton,pos=point)
    c.poll_popup()
    assert c.popup_open and c.toolbar.isVisible(), (
        f'Settings closed during option press with foreground={state.foreground}, '
        f'popup_owner={native.owner(state.foreground)}, toolbar={int(c.toolbar.winId())}')
    QTest.mouseRelease(window,Qt.LeftButton,pos=point)
    state.foreground=int(c.toolbar.winId());state.pressed=False
    c.poll_popup()
    wait_for_rendered(lambda:c.highlight_style is HighlightStyle.TRAIL and popup() is None)
    preview=c.toolbar.view.highlightPreview
    observed=[]
    def completed():
        assert c.popup_open and c.toolbar.isVisible()
        if preview.glow is None:return True
        if preview.color not in observed and preview.glow.progress(time.monotonic()*1000)>=.15:
            observed.append(preview.color)
        return False
    wait_for_rendered(completed,timeout=4)
    assert observed==list(agent_palette(c.appearance)[:3])
    click(c.toolbar,choice)
    wait_for_rendered(lambda:popup() is not None)
    window=popup();state.foreground=int(window.winId())
    QTest.keyClick(window,Qt.Key_Escape)
    state.foreground=int(c.toolbar.winId())
    wait_for_rendered(lambda:popup() is None)
    assert c.popup_open and c.toolbar.isVisible()
    QTest.keyClick(c.toolbar.quick,Qt.Key_Escape)
    assert not c.popup_open and not c.toolbar.isVisible()
    c.toggle_settings();refresh()
    state.position=(0,0);state.foreground=int(host.winId());state.pressed=True
    c.poll_popup()
    assert not c.popup_open and not c.toolbar.isVisible()


@pytest.mark.skipif(sys.platform!='win32',reason='Native Windows dropdown input')
def test_highlight_dropdown_autoplays_three_icon_colors_without_changing_live_rows(native_detail,tmp_path):
    import time
    from PySide6.QtCore import QPointF
    from cachemonitor.overlay_chrome import named_item
    from cachemonitor.overlay_icons import agent_palette
    from cachemonitor.overlay_view import HighlightStyle
    from tests.test_ui import wait_for_rendered
    c,host,refresh=native_detail;host.resize(420,900);refresh()
    m=c.widget.content_model
    m.pin_call(m.call_id(m.rows()[0]));m.toggle_detail_group(m.detail_groups()[0]['key'])
    m.detailBody.setScrollOffset(10)
    c.toggle_settings();refresh()
    selected=m.selected_id;expanded=set(m.expanded_groups);data=m.data;offset=m.detailBody.scroll_offset
    preview=c.toolbar.view.highlightPreview
    assert preview.glow is not None and preview.timer.isActive()
    choice=named_item(c.toolbar.quick.rootObject(),'highlightStyle')
    def popup():
        return next((w for w in QApplication.topLevelWindows() if w.isVisible()
                     and w.transientParent() is c.toolbar.quick.quickWindow()),None)
    for style in reversed(list(HighlightStyle)):
        click(c.toolbar,choice)
        wait_for_rendered(lambda:popup() is not None)
        window=popup();option=named_item(window.contentItem(),'highlightStyle-'+style.key)
        wait_for_rendered(lambda:option is not None and option.isVisible())
        option_width_ratio=option.width()/choice.width()
        point=option.mapToScene(QPointF(option.width()/2,option.height()/2)).toPoint()
        left,top,right,bottom=c.native.frame(int(window.winId()))
        factor=(right-left)/window.width()
        native_point=(left+round(point.x()*factor),top+round(point.y()*factor))
        if style is HighlightStyle.TRAIL:
            x,y,w,h=c.popup_geometry
            assert not (x<=native_point[0]<x+w and y<=native_point[1]<y+h)
            assert c.toolbar.grab().save(str(tmp_path/'dropdown-toolbar.png'))
            assert window.grabWindow().save(str(tmp_path/'dropdown-options.png'))
        effects=dict(m.row_glows.active)
        c.native.primary_down=lambda:True;c.native.cursor=lambda:native_point
        QTest.mousePress(window,Qt.LeftButton,pos=point)
        c.poll_popup()
        assert c.popup_open and c.toolbar.isVisible()
        QTest.mouseRelease(window,Qt.LeftButton,pos=point)
        c.native.primary_down=lambda:False;c.poll_popup()
        wait_for_rendered(lambda:c.highlight_style is style and popup() is None)
        assert c.settings.value('overlay/highlightStyle')==style.key
        assert preview.glow is not None and preview.timer.isActive()
        assert m.selected_id==selected and m.graph_pinned and m.expanded_groups==expanded
        assert m.data is data and m.detailBody.scroll_offset==offset and m.row_glows.active==effects
    choice.forceActiveFocus(Qt.TabFocusReason)
    QTest.keyClick(c.toolbar.quick,Qt.Key_Space)
    wait_for_rendered(lambda:popup() is not None)
    QTest.keyClick(c.toolbar.quick,Qt.Key_End);QTest.keyClick(c.toolbar.quick,Qt.Key_Return)
    wait_for_rendered(lambda:c.highlight_style is HighlightStyle.TRAIL and popup() is None)
    colors=agent_palette(c.appearance)[:3];observed=[]
    def completed():
        if preview.glow is None:return True
        if preview.color not in observed and preview.glow.progress(time.monotonic()*1000)>=.15:
            observed.append(preview.color)
            assert c.toolbar.grab().save(str(tmp_path/f'preview-{len(observed)}.png'))
        return False
    wait_for_rendered(completed,timeout=4)
    assert observed==list(colors) and not preview.timer.isActive()
    c.choose_highlight_style(HighlightStyle.SCAN.key)
    assert preview.glow is not None and preview.timer.isActive()
    c.toggle_opacity();refresh()
    preview.advance();assert preview.glow is None and not preview.timer.isActive()
    c.toggle_settings();refresh();assert preview.glow is not None
    click(c.toolbar,choice)
    wait_for_rendered(lambda:popup() is not None)
    window=popup()
    assert window.width()>=choice.width()-1
    for style in HighlightStyle:
        option=named_item(window.contentItem(),'highlightStyle-'+style.key)
        wait_for_rendered(lambda:option is not None and option.isVisible())
        assert option.width()>=choice.width()*option_width_ratio-1
    point=option.mapToScene(QPointF(option.width()/2,option.height()/2)).toPoint()
    QTest.mouseClick(window,Qt.LeftButton,pos=point)
    wait_for_rendered(lambda:c.highlight_style is style and popup() is None)
    c.close_popup();preview.advance();assert preview.glow is None and not preview.timer.isActive()
    c.toggle_settings();refresh();assert preview.glow is not None
    c.toolbar.close();preview.advance();assert preview.glow is None and not preview.timer.isActive()
