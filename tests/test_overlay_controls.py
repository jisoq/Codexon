from cachemonitor.quick_qa import click
from PySide6.QtQuick import QQuickItem
import ctypes
import sys
import time
import pytest
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from cachemonitor.overlay import OverlayController
from cachemonitor.overlay_tracking import Selection,overlay_geometry,anchor_for_position


@pytest.fixture
def active_overlay_input():
    from PySide6.QtWidgets import QLineEdit,QVBoxLayout
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setWindowTitle('CacheMonitor focus regression host')
    layout=QVBoxLayout(host);editor=QLineEdit('기존 입력');layout.addWidget(editor)
    host.show();host.activateWindow();editor.setFocus();QTest.qWait(60)
    assert app.focusWidget() is editor
    yield editor
    host.close();app.processEvents()


def test_header_actions_have_fixed_targets_and_hover_press_feedback():
    from cachemonitor.overlay_chrome import OverlayChrome
    from PySide6.QtCore import QPointF,QPoint
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import QEvent
    app=QApplication.instance() or QApplication([])
    controls=[OverlayChrome(kind) for kind in ('header','actions','icon')]
    try:
        assert not controls[0].toolTip() and not controls[2].toolTip()
        toolbar=controls[1];toolbar.resize(88,32);toolbar.move(400,400);toolbar.show();QTest.qWait(60)
        button=toolbar.quick.rootObject().findChild(QQuickItem,'collapse')
        assert button.width()==24 and button.height()==24
        assert button.mapToScene(QPointF(0,0)) == QPointF(60,4)
        point=button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint()
        # Deliver to the rendered scene without depending on another app's
        # ownership of the desktop cursor during the test.
        move=QMouseEvent(QEvent.MouseMove,QPointF(point),QPointF(point),
            QPointF(toolbar.quick.mapToGlobal(point)),Qt.NoButton,Qt.NoButton,Qt.NoModifier)
        QApplication.sendEvent(toolbar.quick,move);QTest.qWait(130)
        assert button.property('hovered')
        background=button.property('background')
        assert background.opacity()>0
        hover=background.opacity()
        QTest.mousePress(toolbar.quick,Qt.LeftButton,pos=point);QTest.qWait(130)
        assert background.opacity()>hover
        QTest.mouseRelease(toolbar.quick,Qt.LeftButton,pos=point)
        from PySide6.QtQml import QQmlExpression, qmlContext
        actions=[]
        toolbar.expand.connect(lambda:actions.append('expand'))
        toolbar.opacity_toggle.connect(lambda:actions.append('opacityButton'))
        toolbar.collapse.connect(lambda:actions.append('collapse'))
        for name in ('expand','opacityButton','collapse'):
            target=toolbar.quick.rootObject().findChild(QQuickItem,name)
            point=target.mapToScene(QPointF(12,12)).toPoint()
            QApplication.sendEvent(toolbar.quick,QMouseEvent(QEvent.MouseMove,QPointF(point),QPointF(point),
                QPointF(toolbar.quick.mapToGlobal(point)),Qt.NoButton,Qt.NoButton,Qt.NoModifier))
            QTest.qWait(750)
            assert target.property('hovered')
            tooltip=QQmlExpression(qmlContext(target),target,'ToolTip.visible')
            assert tooltip.evaluate()[0] is False and not tooltip.hasError()
            accessible=QQmlExpression(qmlContext(target),target,'Accessible.name')
            assert accessible.evaluate()[0] and not accessible.hasError()
            QTest.mouseClick(toolbar.quick,Qt.LeftButton,pos=point)
            assert actions[-1:]==[name] and len(actions)==('expand','opacityButton','collapse').index(name)+1
        assert not toolbar.qml_errors
    finally:
        for control in controls:control.close()
        app.processEvents()


def test_opacity_popup_has_one_slider_and_keyboard_escape(active_overlay_input):
    from cachemonitor.overlay_chrome import OverlayChrome
    app=QApplication.instance() or QApplication([])
    popup=OverlayChrome('toolbar');popup.resize(160,40);popup.move(400,450)
    changes=[];escapes=[];popup.opacity_changed.connect(changes.append);popup.escape.connect(lambda:escapes.append(True))
    popup.view.opacityRequested.connect(lambda opacity:popup.apply_appearance(popup.appearance,opacity))
    try:
        popup.show();QTest.qWait(60)
        assert app.focusWidget() is active_overlay_input
        slider=popup.quick.rootObject().findChild(QQuickItem,'opacity')
        assert slider.property('from')==0 and slider.property('to')==80
        assert slider.property('value')==6
        click(popup,slider,slider.width()-4,slider.height()/2)
        assert changes and 20<=changes[-1]<=100
        assert app.focusWidget() is popup.quick and slider.hasActiveFocus()
        before=changes[-1]
        QTest.keyClick(popup.quick,Qt.Key_Left);QTest.qWait(30)
        assert changes[-1]==before+1
        QTest.keyClick(popup.quick,Qt.Key_Escape)
        assert escapes==[True] and not popup.qml_errors
    finally:popup.close();app.processEvents()


def test_detail_graph_owns_navigation_and_body_alone_scrolls(active_overlay_input):
    from PySide6.QtCore import Property,QObject,Slot,QPointF
    from cachemonitor.presentation import Node
    from cachemonitor.overlay_chrome import OverlayDetail
    class Graph(Node):
        def __init__(self):super().__init__();self.events=[]
        def paint(self,painter):pass
        @Slot(float,float)
        def hover_at(self,x,y):self.events.append(('hover',x,y))
        @Slot()
        def clear_hover(self):self.events.append(('exit',))
        @Slot(float,float)
        def activate_at(self,x,y):self.events.append(('select',x,y))
        @Slot(int)
        def key(self,key):self.events.append(('key',key))
        @Slot(float)
        def setScrollOffset(self,value):self.events.append(('scroll',value))
    class Content(Node):
        def __init__(self):
            super().__init__();self.graph=Graph();self.body=Graph();self.data=None
            self.put(overlayScale=1.,detailBodyHeight=900)
        def detail_links(self):return []
        def selected(self):return {}
        @Property(QObject,constant=True)
        def detailGraph(self):return self.graph
        @Property(QObject,constant=True)
        def detailBody(self):return self.body
    app=QApplication.instance() or QApplication([])
    content=Content();detail=OverlayDetail(content);detail.resize(240,580);detail.move(500,200)
    interactions=[];detail.interaction_started.connect(lambda:interactions.append(True))
    try:
        detail.show();QTest.qWait(70)
        assert app.focusWidget() is active_overlay_input
        root=detail.quick.rootObject()
        graph=root.findChild(QQuickItem,'detailGraph');body=root.findChild(QQuickItem,'detailScroll')
        assert (graph.x(),graph.y(),graph.width(),graph.height())==(16,52,208,152)
        assert (body.x(),body.y(),body.width(),body.height())==(16,216,208,348)
        assert body.property('contentWidth')==body.width()
        scrollbar=root.findChild(QQuickItem,'detailScrollBar')
        assert scrollbar.parentItem() is root
        assert scrollbar.x()==body.x()+body.width()+6
        assert scrollbar.x()+scrollbar.width()<=root.width()-6
        assert scrollbar.y()==body.y() and scrollbar.height()==body.height()
        QTest.mouseMove(detail.quick,graph.mapToScene(QPointF(80,40)).toPoint());QTest.qWait(30)
        assert not interactions
        assert app.focusWidget() is active_overlay_input
        click(detail,graph,80,40)
        assert interactions and ('select',80.,40.) in content.graph.events
        assert app.focusWidget() is detail.quick and graph.hasActiveFocus()
        assert not graph.property('keyboardFocus')
        for key in (Qt.Key_Left,Qt.Key_Right,Qt.Key_Home,Qt.Key_End):
            QTest.keyClick(detail.quick,key)
            assert ('key',key) in content.graph.events
            assert graph.property('keyboardFocus')
        body.setProperty('contentY',300);QTest.qWait(30)
        assert graph.y()==52 and body.property('contentY')==300
        click(detail,scrollbar,scrollbar.width()/2,scrollbar.height()-4)
        assert body.property('contentY')>300 and body.hasActiveFocus()
        assert content.parent() is None and not detail.qml_errors
    finally:detail.close();app.processEvents()


def test_rendered_detail_selection_preserves_latest_monitor_values(tmp_path,active_overlay_input):
    from PySide6.QtCore import QPointF,QEvent
    from PySide6.QtGui import QMouseEvent,QPainter
    from cachemonitor.overlay import SessionOverlay
    from cachemonitor.overlay_chrome import OverlayDetail
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    monitor=SessionOverlay();data=summary();monitor.set_content(data)
    model=monitor.content_model;model.set_layout(detail=True)
    monitor.resize(model.panel_width(),model.panel_height());monitor.move(300,200)
    detail=OverlayDetail(model);detail.resize(240,model.panel_height());detail.move(300,200)
    try:
        monitor.show();detail.show();QTest.qWait(80)
        graph=detail.quick.rootObject().findChild(QQuickItem,'detailGraph')
        body=detail.quick.rootObject().findChild(QQuickItem,'detailScroll')
        initial=model.lines();latest_id=model.call_id(model.rows()[-1])
        assert model.selected_id==latest_id
        first_x=36+(24-len(model.rows())+.5)*(208-36)/24
        body.setProperty('contentY',120)
        click(detail,graph,first_x,40)
        assert model.selected_id==model.call_id(model.rows()[0])
        assert body.property('contentY')==0
        assert model.lines()==initial
        QTest.keyClick(detail.quick,Qt.Key_End)
        assert model.selected_id==latest_id and model.lines()==initial
        selected_body=model._detail_items.copy()
        body.setProperty('contentY',80)
        # A key selection leaves the pointer at the preceding click position.
        # Move within that column so MouseArea receives a real position change;
        # replaying the same position need not emit onPositionChanged.
        for delta in (2.,0.):
            local=graph.mapToScene(QPointF(first_x+delta,40))
            move=QMouseEvent(QEvent.MouseMove,local,local,
                 QPointF(detail.quick.mapToGlobal(local.toPoint())),Qt.NoButton,Qt.NoButton,Qt.NoModifier)
            QApplication.sendEvent(detail.quick,move)
        assert model.hover_id==model.call_id(model.rows()[0]) and model.selected_id==latest_id
        assert model.call_id(model.selected())==latest_id and model._detail_items==selected_body
        assert body.property('contentY')==80
        outside=QPointF(8,32)
        QApplication.sendEvent(detail.quick,QMouseEvent(QEvent.MouseMove,outside,outside,
             QPointF(detail.quick.mapToGlobal(outside.toPoint())),Qt.NoButton,Qt.NoButton,Qt.NoModifier))
        QTest.qWait(30)
        assert model.hover_id is None and model.selected_id==latest_id
        frame=monitor.grab().toImage();painter=QPainter(frame);painter.drawImage(0,0,detail.grab().toImage());painter.end()
        assert frame.save(str(tmp_path/'detail-selected-latest.png'))
        assert not detail.qml_errors and not monitor.qml_errors
    finally:detail.close();monitor.close();app.processEvents()


@pytest.mark.skipif(sys.platform!='win32',reason='Owned native overlay preview')
def test_icon_detail_toggle_native_layout_and_preference_restore(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setWindowTitle('CacheMonitor owned design preview');host.resize(1000,1000);host.show()
    settings=QSettings(str(tmp_path/'display.ini'),QSettings.IniFormat)
    settings.setValue('overlay/theme','light')
    c=OverlayController(settings,native_enabled=False);native=WindowsOverlay();c.native=native
    native.configure(int(c.widget.winId()))
    native.visible_target=lambda hwnd:bool(native.u.IsWindowVisible(hwnd) and not native.u.IsIconic(hwnd))
    data=summary();data['request']['started_at']=time.time()-20
    c.receive_snapshot(dict(overlay_sessions=[data],index={'loading':False}))
    def capture(name):
        screen=c.widget.screen();origin=screen.geometry().topLeft();ratio=screen.devicePixelRatio()
        left,top,right,bottom=native.frame(int(c.widget.winId()))
        shot=screen.grabWindow(0,round((left-origin.x())/ratio),round((top-origin.y())/ratio),round((right-left)/ratio),round((bottom-top)/ratio))
        assert shot.save(str(tmp_path/name))
    def refresh():
        c.receive_target(dict(target={'hwnd':int(host.winId())},selection=Selection('clean')))
        app.processEvents();QTest.qWait(70)
    try:
        refresh();assert not c.expanded and c.automatic_mode=='monitor'
        button=c.actions.quick.rootObject().findChild(QQuickItem,'expand')
        assert button.property('enabled') and not button.property('text')
        for dark in (False,True):
            settings.setValue('overlay/theme','dark' if dark else 'light');c.next_theme=0;refresh()
            assert not c.expanded
            capture(f'monitor-{dark}.png')
            click(c.actions,button);refresh()
            assert c.expanded and c.automatic_mode=='detail' and settings.value('overlay/expanded',type=bool)
            QTest.qWait(200)
            capture(f'detail-{dark}.png')
            assert all(not control.qml_errors for control in c.chrome) and not c.widget.qml_errors
            click(c.actions,button);refresh();assert not c.expanded
        c.toggle_expanded();refresh()
        host.resize(500,500);refresh()
        assert c.expanded and c.automatic_mode=='detail-inline' and button.property('enabled')
        host.resize(1000,1000);refresh();assert c.automatic_mode=='detail'
        restored=OverlayController(settings,native_enabled=False)
        assert restored.expanded;restored.stop()
        panel=native.frame(int(c.widget.winId()));header=native.frame(int(c.header.winId()));actions=native.frame(int(c.actions.winId()))
        assert panel[0]<=header[0]<header[2]<=actions[0]<actions[2]<=panel[2]
        assert native.u.GetWindowLongPtrW(int(c.widget.winId()),-20)&0x20
        assert not native.u.GetWindowLongPtrW(int(c.actions.winId()),-20)&0x20
    finally:c.stop();host.close();app.processEvents()


@pytest.mark.parametrize('dpi',[96,120,144,192])
def test_drag_anchors_roundtrip_and_clamp_inside_negative_monitor(dpi):
    frame=(-3000,-800,-100,1600)
    old=overlay_geometry(frame,dpi,anchor=(.5,.5))
    anchor=anchor_for_position(frame,old,old[0]+123,old[1]-87,dpi)
    new=overlay_geometry(frame,dpi,anchor=anchor)
    assert abs(new[0]-old[0]-123)<=1 and abs(new[1]-old[1]+87)<=1
    assert anchor_for_position(frame,old,-100000,100000,dpi)==(0,1)


@pytest.mark.skipif(sys.platform!='win32',reason='Real Windows companion windows')
def test_native_collapse_restore_opacity_drag_and_control_window_styles(tmp_path):
    from cachemonitor.overlay_windows import WindowsOverlay
    app=QApplication.instance() or QApplication([])
    host=QWidget();host.setWindowTitle('Cache Monitor owned control test');host.resize(900,760);host.show()
    native=WindowsOverlay();settings=QSettings(str(tmp_path/'overlay.ini'),QSettings.IniFormat)
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
        pixels=controller.widget.grab().toImage();scale=pixels.devicePixelRatio()
        assert 85<=pixels.pixelColor(round(12*scale),round(100*scale)).alpha()<=110
        click(controller.actions,controller.actions.quick.rootObject().findChild(QQuickItem,'collapse'));refresh()
        assert controller.collapsed and controller.icon.isVisible() and not controller.widget.isVisible()
        assert not controller.header.isVisible() and not controller.toolbar.isVisible()
        QTest.mouseClick(controller.icon,Qt.LeftButton);refresh()
        assert not controller.collapsed and controller.widget.isVisible()
        cursor=[(0,0)];native.cursor=lambda:cursor[0]
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
