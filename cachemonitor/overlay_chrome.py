"""Interactive companions for the otherwise click-through overlay."""
from PySide6.QtCore import Qt, QObject, Property, Signal, Slot, QSignalBlocker, QEvent, QRect, QTimer
from PySide6.QtGui import QRegion
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
    def escapePanel(self): self.escapeRequested.emit()
    @Slot()
    def startInteraction(self): self.interactionRequested.emit()
    @Slot()
    def restorePanel(self): self.restoreRequested.emit()




class OverlayChrome(QuickHost):
    collapse = Signal()
    restore = Signal()
    opacity_toggle = Signal()
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
        self.setWindowTitle('Cache Monitor · ' + names[kind])
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
        self.set_scene(self.view, 'OverlayControls.qml', transparent=True)
        self.quick.setFocusPolicy(Qt.NoFocus)
        self.quick.installEventFilter(self)
        self.quick.setMouseTracking(True)
        self.view.opacityRequested.connect(self.opacity_changed)
        self.view.collapseRequested.connect(self.collapse)
        self.view.opacityToggleRequested.connect(self.opacity_toggle)
        self.view.escapeRequested.connect(self.escape)
        self.view.interactionRequested.connect(self.begin_interaction)
        self.view.restoreRequested.connect(self.restore)
        if kind in ('header', 'icon'): self.quick.setAttribute(Qt.WA_TransparentForMouseEvents)
        if kind == 'toolbar':
            # Existing settings adapter; the sole visible input is the QML slider.
            self.slider = Slider()
            self.slider.setRange(0, 80)
            self.slider.valueChanged.connect(lambda value: self.opacity_changed.emit(100 - value))
            self.percent = Text()
        self.apply_appearance(self.appearance, self.opacity)

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
    detailRequested=Signal(object)
    callSelected=Signal(object)
    def __init__(self,content):
        super().__init__();self.content=content;self.targets={};self.captured=None
    def link_state(self,links):
        self.interactions={link['id']:link.get('interaction','navigate') for link in links}
        self.targets={link['id']:link['target'] for link in links if link.get('target')}
        self.actions={link['id']:link['action'] for link in links if link.get('action')}
        return [{k:v for k,v in link.items() if k!='target'} for link in links]
    @Slot(str)
    def captureNavigation(self,key):
        self.captured=self.targets.get(key) or getattr(self,'actions',{}).get(key)
        self.captured_interaction=getattr(self,'interactions',{}).get(key,'navigate')
        rows=[*self.content.rows(),self.content.selected()] if hasattr(self.content,'rows') else []
        self.captured_row=next((dict(r) for r in rows if r and self.captured and not isinstance(self.captured,str) and (r.get('home'),r.get('sid'),str(r.get('id')))==(self.captured.home,self.captured.sid,self.captured.call_id)),None)
        self.captured_scope=((self.content.data or {}).get('home'),(self.content.data or {}).get('id'))
    @Slot()
    def activateNavigation(self):
        target,self.captured=self.captured,None;data=self.content.data or {}
        if getattr(self,'captured_scope',None)!=(data.get('home'),data.get('id')):return
        if isinstance(target,str):self.content.monitor_action(target);return
        if target and (target.home,target.sid) in [tuple(v) for v in data.get('members',[(data.get('home'),data.get('id'))])]:
            signal={'detail':self.detailRequested,'select':self.callSelected}.get(self.captured_interaction,self.navigationRequested)
            signal.emit((target,self.captured_row) if self.captured_interaction in ('detail','select') else target)
    @Slot(str)
    def inspectCall(self,key):
        row=None
        if key.startswith('call-') and key[5:].isdigit():
            index=int(key[5:]);rows=self.content.rows()
            if index<len(rows):row=rows[index]
        identity=self.content.call_id(row) if row else None
        if getattr(self.content,'inspected_call',None)!=identity:
            self.content.inspected_call=identity;self.content.update()

    @Slot(int)
    def scrollLower(self,delta):self.content.scroll_lower(delta)
    @Slot(str)
    def activateLink(self,key):self.captureNavigation(key);self.activateNavigation()


class DetailModel(NavigationModel):
    escapeRequested = Signal()
    interactionRequested = Signal()

    def __init__(self, content):
        super().__init__(content)
        content.changed.connect(self.sync)
        self.sync()

    @Property(QObject, constant=True)
    def detailGraph(self): return self.content.detailGraph

    @Property(QObject, constant=True)
    def detailBody(self): return self.content.detailBody

    def sync(self):
        from .overlay_navigation import navigation_target
        links=self.link_state(self.content.detail_links())
        self.targets['selected']=navigation_target(self.content.data,'call',call=self.content.selected())
        self.put(**self.content.state,detailLinks=links)

    @Slot()
    def escapePanel(self): self.escapeRequested.emit()

    @Slot()
    def startInteraction(self): self.interactionRequested.emit()


class OverlayDetail(QuickHost):
    """Only this pane receives graph and scrolling input beside the monitor."""
    escape = Signal()
    interaction_started = Signal()

    def __init__(self, content):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Cache Monitor · 호출 상세')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.view = DetailModel(content)
        self.view.escapeRequested.connect(self.escape)
        self.view.interactionRequested.connect(self.begin_interaction)
        self.set_scene(self.view, 'OverlayDetail.qml', transparent=True)
        self.quick.setFocusPolicy(Qt.NoFocus)
        self.quick.installEventFilter(self)

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
        self.setWindowTitle('Cache Monitor · 기록 링크');self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.view=NavigationModel(content);self.wheel_forwarder=None
        content.changed.connect(self.sync)
        self.set_scene(self.view,'OverlayLinks.qml',transparent=True)
        self.quick.installEventFilter(self)
        self.quick.setMouseTracking(True)
        self._hover_position=None
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
        if self._hover_position is not None:self.inspect_position(self._hover_position)
        return bool(links)
    def inspect_position(self,position):
        scale=self.view.content.appearance.scale
        x,y=position.x()/scale,position.y()/scale
        key=next((r['id'] for r in self.view.state.get('links',[])
                  if r['id'].startswith('call-') and r['x']<=x<r['x']+r['width']
                  and r['y']<=y<r['y']+r['height']), '')
        self.view.inspectCall(key)

    def hideEvent(self,event):
        self._hover_position=None;self.view.inspectCall('')
        super().hideEvent(event)

    def eventFilter(self,watched,event):
        if watched is self.quick:
            if event.type()==QEvent.MouseMove:
                self._hover_position=event.position();self.inspect_position(self._hover_position)
            elif event.type()==QEvent.Leave:
                self._hover_position=None;self.view.inspectCall('')

        if event.type()==QEvent.Wheel and self.wheel_forwarder:
            c=self.view.content
            if c.compact and event.position().y()/c.appearance.scale>=c.layout()['context']:
                c.scroll_lower(-event.angleDelta().y()/4);event.accept();return True
            self.wheel_forwarder(event);event.accept();return True
        if event.type()==QEvent.MouseButtonPress:self.interaction_started.emit()
        return super().eventFilter(watched,event)
    def closeEvent(self,event):self.release_scene();super().closeEvent(event)
