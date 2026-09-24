"""Interactive companions for the otherwise click-through overlay."""
from PySide6.QtCore import Qt, QObject, Property, Signal, Slot, QSignalBlocker, QEvent, QRect, QTimer, QPoint
from PySide6.QtGui import QFont, QFontMetrics, QRegion
from PySide6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout
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
    opacityRequested = Signal(int)
    collapseRequested = Signal()
    expandRequested = Signal()
    opacityToggleRequested = Signal()
    escapeRequested = Signal()
    interactionRequested = Signal()
    moveRequested = Signal(int, int)
    restoreRequested = Signal()
    openSessionRequested = Signal()
    @Slot()
    def openSession(self):self.openSessionRequested.emit()

    @Slot(int)
    def transparency(self, value): self.opacityRequested.emit(100 - value)
    @Slot()
    def collapsePanel(self): self.collapseRequested.emit()
    @Slot()
    def expandPanel(self): self.expandRequested.emit()
    @Slot()
    def toggleOpacity(self): self.opacityToggleRequested.emit()
    @Slot()
    def escapePanel(self): self.escapeRequested.emit()
    @Slot()
    def startInteraction(self): self.interactionRequested.emit()
    @Slot(int, int)
    def movePanel(self, dx, dy): self.moveRequested.emit(dx, dy)
    @Slot()
    def restorePanel(self): self.restoreRequested.emit()


class OverlayHost(QuickHost):
    """Keep explicit keyboard destinations across delayed native activation."""
    def __init__(self,*args):
        super().__init__(*args)
        self._keyboard_target=None
        self._focus_generation=0

    def focus_item(self,name):
        self._focus_generation+=1;self._keyboard_target=name
        self.activateWindow();self.quick.setFocus(Qt.TabFocusReason)
        self._apply_keyboard_focus()

    def _apply_keyboard_focus(self):
        if not self.quick or not self._keyboard_target:return
        item=named_item(self.quick.rootObject(),self._keyboard_target)
        if item is None:return
        self.quick.quickWindow().contentItem().forceActiveFocus(Qt.TabFocusReason)
        if item.metaObject().indexOfProperty('pointerFocus')>=0:item.setProperty('pointerFocus',False)
        item.forceActiveFocus(Qt.TabFocusReason)
        if item.metaObject().indexOfProperty('focusReason')>=0:item.setProperty('focusReason',Qt.TabFocusReason)
        if item.metaObject().indexOfProperty('keyboardFocus')>=0:item.setProperty('keyboardFocus',True)

    def _cancel_keyboard_focus(self):
        self._focus_generation+=1;self._keyboard_target=None

    def eventFilter(self,watched,event):
        if watched is self.quick:
            if event.type()==QEvent.MouseButtonPress:self._cancel_keyboard_focus()
            elif event.type()==QEvent.FocusIn and self._keyboard_target:
                generation=self._focus_generation
                # QQuickWidget handles FocusIn after this filter, potentially
                # replacing the destination with the first tab stop.
                def restore():
                    if (generation==self._focus_generation and self.quick
                            and self.isVisible() and self.quick.hasFocus()):self._apply_keyboard_focus()
                QTimer.singleShot(0,self,restore)
        return super().eventFilter(watched,event)

    def hideEvent(self,event):
        self._cancel_keyboard_focus()
        note=getattr(getattr(self,'view',None),'content',None)
        note=getattr(note,'calculation_note',None)
        if note and note.owner is self:note.hide()
        super().hideEvent(event)

    def show_calculation(self,key,formula,x,y):
        content=self.view.content
        note=getattr(content,'calculation_note',None)
        if note is None:
            note=content.calculation_note=CalculationNote(content)
        note.present(self,key,formula,self.quick.mapToGlobal(QPoint(round(x),round(y))))

    def moveEvent(self,event):
        content=getattr(getattr(self,'view',None),'content',None)
        note=getattr(content,'calculation_note',None)
        if note and note.owner is self:note.hide()
        super().moveEvent(event)


class OverlayChrome(OverlayHost):
    open_session = Signal()
    collapse = Signal()
    expand = Signal()
    restore = Signal()
    opacity_toggle = Signal()
    escape = Signal()
    interaction_started = Signal()
    opacity_changed = Signal(int)
    drag_started = Signal(object)
    drag_moved = Signal(object)
    drag_finished = Signal(object)
    focus_requested = Signal(str)
    move_requested = Signal(int, int)

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
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(names[kind])
        if kind in ('header', 'icon'): self.setCursor(Qt.OpenHandCursor)
        self.view = ChromeModel()
        self.view.put(chromeKind=kind, scale=1., opacity=94, hovered=False, pressed=False,
                      expanded=False, canExpand=True, popupOpen=False, surface='#1C211E',
                      ink='#F1F5F2', meta='#95A59B', border='#455048', accent='#79C4A5',
                      family='Pretendard JP', reducedMotion=False)
        self.set_scene(self.view, 'OverlayControls.qml', transparent=True)
        self.quick.setFocusPolicy(Qt.StrongFocus)
        self.quick.installEventFilter(self)
        self.quick.setMouseTracking(True)
        self.view.opacityRequested.connect(self.opacity_changed)
        self.view.collapseRequested.connect(self.collapse)
        self.view.expandRequested.connect(self.expand)
        self.view.opacityToggleRequested.connect(self.opacity_toggle)
        self.view.escapeRequested.connect(self.escape)
        self.view.interactionRequested.connect(self.begin_interaction)
        self.view.moveRequested.connect(self.move_requested)
        self.view.restoreRequested.connect(self.restore)
        self.view.openSessionRequested.connect(self.open_session)
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
        font = QFont(self.appearance.family)
        font.setPixelSize(round(14 * self.appearance.scale))
        font.setWeight(QFont.DemiBold)
        elided = QFontMetrics(font).horizontalAdvance(title) > 260 * self.appearance.scale
        self.setToolTip('\n'.join(value for value in (title if elided else '',model_summary) if value))
        self.setAccessibleName('\n'.join(value for value in (title,model_summary) if value))
        self.view.put(title=title)

    def focus_control(self, name=None):
        """Called only after the controller opens or activates this companion."""
        name = name or {'toolbar': 'opacity', 'icon': 'restore', 'header': 'dragTitle'}.get(self.kind, 'expand')
        self.focus_item(name)

    def keyboard_focus(self, name=None):
        from PySide6.QtQuick import QQuickItem
        name = name or ('restore' if self.kind == 'icon' else 'collapse')
        item = named_item(self.quick.rootObject(), name)
        return bool(item is not None and item.hasActiveFocus() and
                    item.property('keyboardFocus' if self.kind == 'icon' else 'visualFocus'))

    def begin_interaction(self):
        self.interaction_started.emit()
        self.activateWindow()
        self.quick.setFocus(Qt.MouseFocusReason)
        self.quick.quickWindow().contentItem().forceActiveFocus(Qt.MouseFocusReason)

    def eventFilter(self, watched, event):
        if watched is self.quick and event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Tab,Qt.Key_Backtab):
            reverse=event.key()==Qt.Key_Backtab or bool(event.modifiers() & Qt.ShiftModifier)
            if self.kind=='header':target='monitorLinksLast' if reverse else 'expand'
            elif self.kind in ('toolbar','icon'):target=None
            else:
                active=self.quick.quickWindow().activeFocusItem()
                name=active.objectName() if active else 'expand'
                expanded=self.view.state.get('expanded',False)
                targets={'expand':('dragTitle','detailGraph' if expanded else 'opacityButton'),
                         'opacityButton':('detailScroll' if expanded else 'expand','collapse'),
                         'collapse':('opacityButton','monitorLinks')}
                target=targets.get(name,targets['expand'])[0 if reverse else 1]
            if target:self.focus_requested.emit(target)
            event.accept();return True
        return super().eventFilter(watched,event)

    def closeEvent(self, event):
        self.release_scene()
        super().closeEvent(event)

    def enterEvent(self, event):
        self.view.put(hovered=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.view.put(hovered=False)
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.escape.emit()
            event.accept()
        elif self.kind == 'icon' and event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.restore.emit()
            event.accept()
        else: super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.kind in ('header', 'icon'):
            self._cancel_keyboard_focus()
            from PySide6.QtQuick import QQuickItem
            target=self.quick.rootObject().findChild(QQuickItem,'restore' if self.kind=='icon' else 'dragTitle')
            if target is not None:target.setProperty('keyboardFocus',False)
            self.view.put(pressed=True)
            self.pressed_scope=getattr(self,'session_scope',None)
            self.press = event.globalPosition().toPoint()
            self.moved = False
            self.setCursor(Qt.ClosedHandCursor)
            self.drag_started.emit(self.event_position(event))
            event.accept()

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
            elif clicked and self.kind == 'header': self.open_session.emit()
            self.pressed_scope=None
            event.accept()


class CalculationNote(QWidget):
    """One click-opened line shared by the monitor and its detail companion."""
    def __init__(self,content):
        super().__init__(None,Qt.Popup|Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_NoMouseReplay)
        self.setWindowTitle('계산 방법');self.content=content;self.owner=None;self.key=None
        self.setObjectName('calculationNote')
        layout=QVBoxLayout(self);layout.setContentsMargins(10,7,10,7)
        self.label=QLabel(self);self.label.setObjectName('calculationText')
        self.label.setTextFormat(Qt.PlainText);self.label.setWordWrap(False)
        layout.addWidget(self.label);content.changed.connect(self.refresh_scope)

    def present(self,owner,key,formula,anchor):
        if self.isVisible() and self.owner is owner and self.key==key:self.hide();return
        from .overlay_view import palette, font
        from .i18n import tr
        self.owner=owner;self.key=key;self.scope=self.identity();appearance=self.content.appearance
        colors=palette(appearance);size=round(12*appearance.scale)
        text=tr(formula);self.label.setText(text)
        self.setStyleSheet('QWidget#calculationNote { background: '+colors['surface'].name()+'; border: 1px solid '+colors['border'].name()+'; } '
                          'QLabel { color: '+colors['ink'].name()+'; background: transparent; border: none; }')
        self.setAccessibleName(text)
        screen=QApplication.screenAt(anchor) or owner.screen();area=screen.availableGeometry()
        metrics=QFontMetrics(font(appearance.family,size))
        # Keep one readable line, including at the edge of a small display.
        while metrics.horizontalAdvance(text)+24>area.width() and size>10:
            size-=1;metrics=QFontMetrics(font(appearance.family,size))
        self.label.setFont(font(appearance.family,size))
        self.resize(min(area.width(),metrics.horizontalAdvance(text)+24),metrics.height()+18)
        x=max(area.left(),min(anchor.x(),area.right()-self.width()+1))
        y=anchor.y()+4
        if y+self.height()>area.bottom()+1:y=anchor.y()-self.height()-22
        self.move(x,max(area.top(),y));self.show()

    def identity(self):
        d=self.content.data or {}
        return d.get('home'),d.get('id'),self.content.compact,self.content.detail_inline,self.content.appearance

    def refresh_scope(self):
        if self.isVisible() and self.scope!=self.identity():self.hide()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape:self.hide();event.accept()
        else:super().keyPressEvent(event)

class NavigationModel(Node):
    navigationRequested=Signal(object)
    calculationRequested=Signal(str,str,float,float)
    def __init__(self,content):
        super().__init__();self.content=content;self.targets={};self.formulas={};self.captured=None
    def link_state(self,links):
        self.targets={link['id']:link['target'] for link in links if link.get('target')}
        self.formulas={link['id']:link['formula'] for link in links if link.get('formula')}
        return [{k:v for k,v in link.items() if k!='target'} for link in links]
    @Slot(str,float,float)
    def showCalculation(self,key,x,y):
        formula=self.formulas.get(key)
        if formula:self.calculationRequested.emit(key,formula,x,y)
    @Slot(str)
    def captureNavigation(self,key):self.captured=self.targets.get(key)
    @Slot()
    def activateNavigation(self):
        target,self.captured=self.captured,None;data=self.content.data or {}
        if target and (target.home,target.sid)==(data.get('home'),data.get('id')):
            self.navigationRequested.emit(target)
    @Slot(str)
    def keyboardActivate(self,key):self.captureNavigation(key);self.activateNavigation()


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


class OverlayDetail(OverlayHost):
    """Only this pane receives graph and scrolling input beside the monitor."""
    escape = Signal()
    interaction_started = Signal()
    focus_requested = Signal(str)

    def __init__(self, content):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Cache Monitor · 호출 상세')
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.StrongFocus)
        self.view = DetailModel(content)
        self.view.calculationRequested.connect(self.show_calculation)
        self.view.escapeRequested.connect(self.escape)
        self.view.interactionRequested.connect(self.begin_interaction)
        self.set_scene(self.view, 'OverlayDetail.qml', transparent=True)
        self.quick.setFocusPolicy(Qt.StrongFocus)
        self.quick.installEventFilter(self)

    def apply_appearance(self, appearance, opacity): self.view.sync()

    def focus_control(self, name='detailGraph'):
        self.focus_item(name)

    def begin_interaction(self):
        self.interaction_started.emit()
        self.activateWindow()
        self.quick.setFocus(Qt.MouseFocusReason)
        self.quick.quickWindow().contentItem().forceActiveFocus(Qt.MouseFocusReason)

    def eventFilter(self, watched, event):
        # QQuickWidget otherwise moves Tab out of the last QML item before
        # that item's Keys handler runs, losing the companion-window route.
        if watched is self.quick and event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Tab,Qt.Key_Backtab):
            reverse=event.key()==Qt.Key_Backtab or bool(event.modifiers() & Qt.ShiftModifier)
            active=self.quick.quickWindow().activeFocusItem()
            name=active.objectName() if active else 'detailGraph'
            names=['detailGraph','detailScroll']+['evidence-'+item['id'] for item in self.view.state.get('detailLinks',[])]+(['openDashboard'] if self.view.state.get('detailHasSelection') else [])
            index=names.index(name) if name in names else 0;index+=-1 if reverse else 1
            if 0<=index<len(names):self.focus_control(names[index])
            else:self.focus_requested.emit('expand' if reverse else 'opacityButton')
            event.accept();return True
        return super().eventFilter(watched,event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.escape.emit()
            event.accept()
        else: super().keyPressEvent(event)

    def closeEvent(self, event):
        self.view.content.changed.disconnect(self.view.sync)
        self.release_scene()
        super().closeEvent(event)


class OverlayLinks(OverlayHost):
    escape=Signal()
    focus_requested=Signal(str)
    interaction_started=Signal()
    def __init__(self,content):
        super().__init__(None,Qt.Tool|Qt.FramelessWindowHint|Qt.WindowStaysOnTopHint)
        self.setWindowTitle('Cache Monitor · 기록 링크');self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.view=NavigationModel(content);self.wheel_forwarder=None
        self.view.calculationRequested.connect(self.show_calculation)
        self.set_scene(self.view,'OverlayLinks.qml',transparent=True)
        self.quick.installEventFilter(self)
    def focus_control(self,name='monitorLinks'):
        links=self.view.state.get('links',[])
        if not links:return
        key=links[-1 if name=='monitorLinksLast' else 0]['id']
        self.focus_item('nav-'+key)
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
        if event.type()==QEvent.KeyPress and event.key()==Qt.Key_Escape:
            self.escape.emit();return True
        if event.type()==QEvent.KeyPress and event.key() in (Qt.Key_Tab,Qt.Key_Backtab):
            reverse=event.key()==Qt.Key_Backtab or bool(event.modifiers() & Qt.ShiftModifier)
            active=self.quick.quickWindow().activeFocusItem();name=active.objectName() if active else ''
            names=['nav-'+link['id'] for link in self.view.state.get('links',[])]
            if name in names:
                index=names.index(name)+(-1 if reverse else 1)
                if 0<=index<len(names):self.focus_item(names[index])
                else:self.focus_requested.emit('collapse' if reverse else 'dragTitle')
                return True
        if event.type()==QEvent.MouseButtonPress:self.interaction_started.emit()
        return super().eventFilter(watched,event)
    def closeEvent(self,event):self.release_scene();super().closeEvent(event)
