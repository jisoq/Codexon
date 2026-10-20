"""Interactive companions for the otherwise click-through overlay."""
from PySide6.QtCore import Qt, QObject, Property, Signal, Slot, QSignalBlocker, QEvent, QRect, QTimer
from PySide6.QtGui import QRegion, QWindow, QPlatformSurfaceEvent
from PySide6.QtWidgets import QApplication
from .overlay_appearance import default_appearance
from .presentation import Node, Slider, Text
from .quick_runtime import QuickHost


def named_item(root,name):
    if root.objectName()==name:return root
    for child in root.childItems():
        item=named_item(child,name)
        if item is not None:return item
    return None


class OverlayWindowStack(QObject):
    """Keep native layers and Quick popups above their own overlay surfaces."""
    def __init__(self,controller):
        super().__init__(controller)
        self.controller=controller
        self.owners={}
        QApplication.instance().installEventFilter(self)

    def bind(self,window,owner):
        native=self.controller.native
        if not native or not hasattr(native,'set_owner'):return
        hwnd,parent=int(window.winId()),int(owner.winId())
        old=self.owners.get(window)
        if old and (old[0] is not native or old[1]!=hwnd):self.release(window);old=None
        original=old[2] if old else native.owner(hwnd)
        changed=native.owner(hwnd)!=parent
        if native.set_owner(hwnd,parent):
            self.owners[window]=(native,hwnd,original,parent)
            # Setting an owner does not reorder an already visible HWND.
            if changed and window.isVisible():native.raise_companion(hwnd)

    def release(self,window):
        old=self.owners.pop(window,None)
        if old:
            native,hwnd,original,parent=old
            if native.owner(hwnd)==parent:native.set_owner(hwnd,original or 0)

    def sync(self):
        c=self.controller
        # Independent topmost siblings can overtake one another on activation.
        # Ownership expresses the visual order without raising on every tick.
        owner=c.shadow
        for window in (c.widget,c.detail,c.links,c.header,c.actions,c.toolbar):
            if not window.isVisible():
                self.release(window);continue
            self.bind(window,owner);owner=window

    def eventFilter(self,window,event):
        kind=event.type()
        if kind not in (QEvent.Show,QEvent.Expose,QEvent.Hide,QEvent.Destroy,QEvent.PlatformSurface):return False
        if not isinstance(window,QWindow):return False
        if kind==QEvent.PlatformSurface:
            if event.surfaceEventType()==QPlatformSurfaceEvent.SurfaceAboutToBeDestroyed:
                hwnd=int(window.winId())
                for child,entry in list(self.owners.items()):
                    if hwnd in (entry[1],entry[3]):self.release(child)
            return False
        if kind in (QEvent.Hide,QEvent.Destroy):
            self.release(window);return False
        c=self.controller
        for control in (c.widget,*c.chrome):
            quick=control._quick
            if quick is not None and window.transientParent() is quick.quickWindow():
                # Quick popups point at QQuickWidget's offscreen window, whose
                # HWND is not an owner. Use the highest overlay layer instead.
                top=c.toolbar if c.toolbar.isVisible() else c.actions
                self.bind(window,c.icon if control is c.icon else top)
                break
        return False

    def close(self):
        QApplication.instance().removeEventFilter(self)
        # Detach before Qt closes native surfaces: native owner destruction
        # would otherwise destroy owned HWNDs behind their QWidget wrappers.
        for window in list(self.owners):self.release(window)


class ChromeModel(Node):
    detailPressed = Signal()
    detailClicked = Signal()
    @Slot()
    def captureDetail(self): self.detailPressed.emit()
    @Slot()
    def toggleDetail(self): self.detailClicked.emit()
    opacityRequested = Signal(int)
    collapseRequested = Signal()
    opacityToggleRequested = Signal()
    settingsToggleRequested = Signal()
    chartStyleRequested = Signal(str)
    agentCountRequested = Signal(int)
    escapeRequested = Signal()
    interactionRequested = Signal()
    restoreRequested = Signal()

    @Slot(int)
    def transparency(self, value): self.opacityRequested.emit(100 - value)
    @Slot()
    def collapsePanel(self): self.collapseRequested.emit()
    @Slot()
    def toggleOpacity(self): self.opacityToggleRequested.emit()
    @Slot()
    def toggleSettings(self): self.settingsToggleRequested.emit()
    @Slot(str)
    def setChartStyle(self,value): self.chartStyleRequested.emit(value)
    @Slot(int)
    def setAgentCount(self,value): self.agentCountRequested.emit(value)
    @Slot()
    def escapePanel(self): self.escapeRequested.emit()
    @Slot()
    def startInteraction(self): self.interactionRequested.emit()
    @Slot()
    def restorePanel(self): self.restoreRequested.emit()




class OverlayChrome(QuickHost):
    collapse = Signal()
    restore = Signal()
    opacity_toggle = Signal()
    settings_toggle = Signal()
    chart_style_changed = Signal(str)
    agent_count_changed = Signal(int)
    escape = Signal()
    interaction_started = Signal()
    opacity_changed = Signal(int)
    drag_started = Signal(object)
    drag_moved = Signal(object)
    drag_finished = Signal(object)
    drag_cancelled = Signal()

    def __init__(self, kind):
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.kind = kind
        self.appearance = default_appearance(True)
        self.opacity = 94
        self.press = None
        self.moved = False
        names = {'header': '세션 제목 이동', 'toolbar': '투명도', 'icon': '복원', 'actions': '세션 조작'}
        self.setWindowTitle('Cache Monitor - ' + names[kind])
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAccessibleName(names[kind])
        if kind in ('header', 'icon'): self.setCursor(Qt.OpenHandCursor)
        self.view = ChromeModel()
        from .overlay_view import palette
        initial = palette(self.appearance)
        self.view.put(chromeKind=kind, scale=1., opacity=94, hovered=False, pressed=False,
                      expanded=False, canExpand=True, popupOpen=False,
                      **{key:initial[key].name() for key in ('surface','ink','meta','border','accent')},
                      family='Pretendard JP', reducedMotion=False)
        self.set_scene(self.view, 'OverlayControls.qml', transparent=True,deferred=True,shared=True)
        self.view.opacityRequested.connect(self.opacity_changed)
        self.view.collapseRequested.connect(self.collapse)
        self.view.opacityToggleRequested.connect(self.opacity_toggle)
        self.view.settingsToggleRequested.connect(self.settings_toggle)
        self.view.chartStyleRequested.connect(self.chart_style_changed)
        self.view.agentCountRequested.connect(self.agent_count_changed)
        self.view.escapeRequested.connect(self.escape)
        self.view.interactionRequested.connect(self.begin_interaction)
        self.view.restoreRequested.connect(self.restore)
        if kind == 'toolbar':
            # Existing settings adapter; the sole visible input is the QML slider.
            self.slider = Slider()
            self.slider.setRange(0, 80)
            self.slider.valueChanged.connect(lambda value: self.opacity_changed.emit(100 - value))
            self.percent = Text()
        self.apply_appearance(self.appearance, self.opacity)

    def configure_quick(self,quick):
        quick.setFocusPolicy(Qt.NoFocus)
        quick.installEventFilter(self)
        if self.kind in ('header','icon'):quick.setAttribute(Qt.WA_TransparentForMouseEvents)

    def apply_appearance(self, appearance, opacity):
        if (appearance, opacity) == (getattr(self, '_applied_appearance', None), getattr(self, '_applied_opacity', None)): return
        self._applied_appearance, self._applied_opacity = appearance, opacity
        self.appearance, self.opacity = appearance, opacity
        from .overlay_view import palette
        colors = palette(appearance)
        self.view.put(scale=appearance.scale, opacity=opacity,
                      surface=colors['surface'].name(), ink=colors['ink'].name(),
                      meta=colors['meta'].name(), border=colors['border'].name(),
                      accent=colors['accent'].name(), family=appearance.family)
        if self.kind == 'toolbar':
            with QSignalBlocker(self.slider): self.slider.setValue(100 - opacity)
            self.percent.setText(f'{100 - opacity}%')

    def set_title(self, title, model_summary=''):
        key=(title,model_summary,self.appearance.family,self.appearance.scale)
        if key==getattr(self,'_title_key',None):return
        self._title_key=key
        self.setAccessibleName('\n'.join(value for value in (title,model_summary) if value))
        self.view.put(title=title)



    def begin_interaction(self):
        self.interaction_started.emit()
        self.activateWindow()
        self.quick.setFocus(Qt.MouseFocusReason)
        self.quick.quickWindow().contentItem().forceActiveFocus(Qt.MouseFocusReason)


    def closeEvent(self, event):
        self.release_scene()
        super().closeEvent(event)

    def enterEvent(self, event):
        self.view.put(hovered=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.view.put(hovered=False)
        super().leaveEvent(event)


    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.kind in ('header', 'icon'):
            self.view.put(pressed=True)
            self.pressed_scope=getattr(self,'session_scope',None)
            self.press = event.globalPosition().toPoint()
            self.moved = False
            self.setCursor(Qt.ClosedHandCursor)
            self.drag_started.emit(self.event_position(event))
            event.accept()

    def cancel_drag(self):
        self.press=None;self.moved=False;self.pressed_scope=None
        self.view.put(pressed=False)
        if self.kind in ('header','icon'):self.setCursor(Qt.OpenHandCursor)

    def event(self,event):
        if (event.type() in (QEvent.UngrabMouse,QEvent.Hide)
                and getattr(self,'press',None) is not None):
            self.cancel_drag();self.drag_cancelled.emit()
        return super().event(event)

    def nativeEvent(self, event_type, message):
        if self.kind in ('header','icon'):
            import os
            if os.name=='nt':
                from ctypes import wintypes
                native_message=wintypes.MSG.from_address(int(message))
                if native_message.message==0x21:  # WM_MOUSEACTIVATE / MA_NOACTIVATE
                    return True,3
        return super().nativeEvent(event_type,message)

    def event_position(self, event):
        point = event.globalPosition().toPoint()
        screen = QApplication.screenAt(point) or self.windowHandle().screen()
        origin, scale = screen.geometry().topLeft(), screen.devicePixelRatio()
        # Event coordinates are immutable; a later cursor sample loses fast drags.
        return (round(origin.x() + (point.x() - origin.x()) * scale),
                round(origin.y() + (point.y() - origin.y()) * scale))

    def mouseMoveEvent(self, event):
        if self.press is not None:
            if (event.globalPosition().toPoint() - self.press).manhattanLength() >= QApplication.startDragDistance(): self.moved = True
            if self.moved: self.drag_moved.emit(self.event_position(event))
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.press is not None:
            self.view.put(pressed=False)
            clicked, self.press = not self.moved, None
            self.setCursor(Qt.OpenHandCursor)
            self.drag_finished.emit(self.event_position(event))
            if clicked and self.kind == 'icon': self.restore.emit()
            # Header clicks only finish a drag gesture.
            self.pressed_scope=None
            event.accept()


class NavigationModel(Node):
    navigationRequested=Signal(object)
    escapeRequested=Signal()
    def __init__(self,content):
        super().__init__();self.content=content;self.targets={};self.captured=None
    @Slot()
    def escapePanel(self):self.escapeRequested.emit()
    def link_state(self,links):
        self.inspections={link['id']:link['hoverCall'] for link in links if link.get('hoverCall')}
        self.interactions={link['id']:link.get('interaction','navigate') for link in links}
        self.targets={link['id']:link['target'] for link in links if link.get('target')}
        self.actions={link['id']:link['action'] for link in links if link.get('action')}
        return [{k:v for k,v in link.items() if k!='target'} for link in links]
    @Slot(str,bool)
    def hoverLink(self,key,entered):
        identity=getattr(self,'inspections',{}).get(key)
        if identity is None:return
        current=getattr(self.content,'inspected_call',None)
        if entered:
            if identity==current:return
            self.content.inspected_call=identity
        elif current==identity:self.content.inspected_call=None
        else:return
        self.content.update()
    @Slot(str)
    def captureNavigation(self,key):
        self.captured=self.targets.get(key) or getattr(self,'actions',{}).get(key)
        self.captured_interaction=getattr(self,'interactions',{}).get(key,'navigate')
        self.captured_scope=((self.content.data or {}).get('home'),(self.content.data or {}).get('id'))
    @Slot()
    def activateNavigation(self):
        target,self.captured=self.captured,None;data=self.content.data or {}
        if getattr(self,'captured_scope',None)!=(data.get('home'),data.get('id')):return
        if isinstance(target,str):self.content.monitor_action(target);return
        if target and (target.home,target.sid) in [tuple(v) for v in data.get('members',[(data.get('home'),data.get('id'))])]:
            self.navigationRequested.emit(target)
    @Slot(str)
    def activateLink(self,key):self.captureNavigation(key);self.activateNavigation()


class DetailModel(NavigationModel):
    interactionRequested = Signal()

    def __init__(self, content):
        super().__init__(content)
        content.changed.connect(self.sync)
        self.sync()

    @Property(QObject, constant=True)
    def detailBody(self): return self.content.detailBody

    def sync(self):
        from .overlay_navigation import navigation_target
        links=self.link_state(self.content.detail_links())
        self.targets['session']=navigation_target(self.content.data,'session')
        self.put(**self.content.state,detailLinks=links)

    @Slot(str)
    def activateDetail(self,key):
        if key.startswith('mode-'):self.content.set_detail_group(key[5:])
        elif key.startswith('group-'):self.content.toggle_detail_group(key[6:])

    @Slot()
    def escapePanel(self): self.escapeRequested.emit()

    @Slot()
    def startInteraction(self): self.interactionRequested.emit()


class OverlayDetail(QuickHost):
    """Subagent cost groups and scrolling input beside the monitor."""
    escape = Signal()
    interaction_started = Signal()

    def __init__(self, content):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Cache Monitor - Sub 상세')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.view = DetailModel(content)
        self.view.escapeRequested.connect(self.escape)
        self.view.interactionRequested.connect(self.begin_interaction)
        self.set_scene(self.view, 'OverlayDetail.qml', transparent=True,deferred=True,shared=True)

    def configure_quick(self,quick):
        quick.setFocusPolicy(Qt.NoFocus)
        quick.installEventFilter(self)

    def apply_appearance(self, appearance, opacity): self.view.sync()


    def begin_interaction(self):
        self.interaction_started.emit()
        self.activateWindow()
        self.quick.setFocus(Qt.MouseFocusReason)
        self.quick.quickWindow().contentItem().forceActiveFocus(Qt.MouseFocusReason)



    def closeEvent(self, event):
        self.view.content.changed.disconnect(self.view.sync)
        self.release_scene()
        super().closeEvent(event)


class OverlayLinks(QuickHost):
    escape=Signal()
    interaction_started=Signal()
    def __init__(self,content):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Cache Monitor - 기록 링크');self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.view=NavigationModel(content);self.wheel_forwarder=None
        self.view.escapeRequested.connect(self.escape)
        content.changed.connect(self.sync)
        self.set_scene(self.view,'OverlayLinks.qml',transparent=True,deferred=True,shared=True)
    def configure_quick(self,quick):
        quick.installEventFilter(self)
    def begin_interaction(self):
        self.interaction_started.emit()
        self.activateWindow()
        self.quick.setFocus(Qt.OtherFocusReason)
        self.quick.quickWindow().contentItem().forceActiveFocus(Qt.OtherFocusReason)
    def sync(self):
        from .overlay_view import palette
        c=self.view.content;scale=c.appearance.scale;links=self.view.link_state(c.monitor_links())
        self.view.put(links=links,scale=scale,accent=palette(c.appearance)['accent'].name(),
                      warning=palette(c.appearance)['warning'].name())
        region=QRegion()
        for link in links:
            region|=QRegion(QRect(round(link['x']*scale),round(link['y']*scale),round(link['width']*scale),round(link['height']*scale)))
        if links:
            if region!=self.mask():self.setMask(region)
        else:self.hide()
        return bool(links)
    def eventFilter(self,watched,event):
        if event.type()==QEvent.Wheel and self.wheel_forwarder:
            self.wheel_forwarder(event);event.accept();return True
        if event.type()==QEvent.MouseButtonPress:self.interaction_started.emit()
        return super().eventFilter(watched,event)
    def closeEvent(self,event):self.release_scene();super().closeEvent(event)
