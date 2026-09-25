"""Observable presentation models consumed by Qt Quick.

These objects own application presentation state, never widgets. Layout, input,
focus and painting are provided by the QML delegates. Services may update these
models on the GUI thread without knowing which scene currently displays them.
"""
from __future__ import annotations

from datetime import date
import re

from PySide6.QtCore import QObject, Property, Signal, Slot, Qt, QDate, QSize, QTimer
from .i18n import localize_state


class Node(QObject):
    changed = Signal()
    structureChanged = Signal()
    revealRequested = Signal(QObject)
    kind = 'group'

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nodes = []
        self._layout = None
        self._state = dict(kind=self.kind, text='', tooltip='', accessible='', visible=True,
                           enabled=True, minWidth=0, minHeight=0, maxWidth=16777215,
                           maxHeight=16777215, width=-1, height=-1, stretch=0,
                           spacing=8, margins=[0, 0, 0, 0], wrap=False,
                           rich=False, selectable=False, fontSize=14, bold=False,
                           color='ink', background='transparent', placeholder='',
                           checked=False, checkable=False, index=0, items=[],
                           minimum=0, maximum=100, value=0)

    @Property('QVariantMap', notify=changed)
    def state(self): return localize_state(self._state)

    @Property(str,constant=True)
    def uid(self):return str(id(self))

    @Property('QVariantList', notify=structureChanged)
    def nodes(self): return self._nodes

    def put(self, **values):
        if any(self._state.get(k) != v for k, v in values.items()):
            self._state.update(values)
            if self.signalsBlocked():
                if not hasattr(self,'_notify_timer'):
                    self._notify_timer=QTimer(self);self._notify_timer.setSingleShot(True)
                    self._notify_timer.timeout.connect(self.changed)
                self._notify_timer.start(0)
            else:self.changed.emit()

    def append(self, node, stretch=0, index=None):
        node.setParent(self)
        node.put(stretch=stretch)
        if index is None: self._nodes.append(node)
        else: self._nodes.insert(index, node)
        self.structureChanged.emit()

    def layout(self): return self._layout
    def itemAt(self, index): return self._nodes[index]
    def widget(self, index=None): return self if index is None else self._nodes[index]
    def count(self): return len(self._nodes)
    def addWidget(self, node, stretch=0, *args): self.append(node, stretch)
    def addLayout(self, node, stretch=0): self.append(node, stretch)
    def insertWidget(self, index, node, stretch=0): self.append(node, stretch, index)
    def addStretch(self, stretch=1):
        node=Node();node.put(kind='spacer');self.append(node, stretch)
    def addSpacing(self, value):
        node=Node();node.put(kind='spacer',width=value,height=value);self.append(node)
    def setSpacing(self, value): self.put(spacing=value)
    def setContentsMargins(self, *values): self.put(margins=list(values))
    def setMinimumHeight(self, value): self.put(minHeight=value)
    def setMaximumHeight(self, value): self.put(maxHeight=value)
    def setFixedHeight(self, value): self.put(height=value,minHeight=value,maxHeight=value)
    def setMinimumWidth(self, value): self.put(minWidth=value)
    def setMaximumWidth(self, value): self.put(maxWidth=value)
    def setFixedWidth(self, value): self.put(width=value,minWidth=value,maxWidth=value)
    def setFixedSize(self,w,h): self.setFixedWidth(w);self.setFixedHeight(h)
    def setMinimumSize(self,w,h): self.setMinimumWidth(w);self.setMinimumHeight(h)
    def setVisible(self, value): self.put(visible=bool(value))
    def isVisible(self): return self._state['visible']
    def show(self): self.setVisible(True)
    def hide(self): self.setVisible(False)
    def setEnabled(self,value): self.put(enabled=bool(value))
    def isEnabled(self): return self._state['enabled']
    def setText(self,value): self.put(text=str(value))
    def text(self): return self._state['text']
    def setToolTip(self,value): self.put(tooltip=str(value))
    def toolTip(self): return self._state['tooltip']
    def setAccessibleName(self,value): self.put(accessible=str(value))
    def setWordWrap(self,value): self.put(wrap=bool(value))
    def setTextFormat(self,value): self.put(rich=value==Qt.RichText)
    def setTextInteractionFlags(self,value): self.put(selectable=bool(value & (Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard)))
    def setOpenExternalLinks(self,value): self.put(externalLinks=bool(value))
    def setPlaceholderText(self,value): self.put(placeholder=value)
    def setReadOnly(self,value): self.put(readOnly=bool(value))
    def setPlainText(self,value): self.put(text=str(value),rich=False)
    def setHtml(self,value): self.put(text=str(value),rich=True)
    def toPlainText(self): return re.sub('<[^>]+>','',self.text())
    def clear(self): self.setText('')
    def update(self): self.changed.emit()
    def setObjectName(self,name):
        super().setObjectName(name)
        styles={'brand':dict(fontSize=21,bold=True),'heading':dict(fontSize=24,bold=True),
                'section':dict(fontSize=16,bold=True),'muted':dict(color='muted'),
                'number':dict(fontSize=30,bold=True,noElide=True),'sidebar':dict(background='surface'),
                'workspace':dict(background='background'),
                'panel':dict(background='surface',border='border',radius=10),
                'summary':dict(background='surface',border='border',radius=10)}
        self.put(style=name,**styles.get(name,{}))

    def setStyleSheet(self,style):
        """Extract the existing design tokens; QML owns control styling."""
        size=re.search(r'font-size:\s*(\d+)px',style)
        color=re.search(r'(?:^|[;{]\s*)color:\s*([a-zA-Z_]+)',style)
        weight=re.search(r'font-weight:\s*(\d+)',style)
        values={}
        if size: values['fontSize']=int(size[1])
        if color: values['color']=color[1]
        if weight: values['bold']=int(weight[1])>=500
        self.put(**values)

    def setFocusPolicy(self,value): self.put(focusable=value!=Qt.NoFocus)
    def setSizePolicy(self,*values):
        self.put(expandX=bool(values and int(values[0])==7),expandY=bool(len(values)>1 and int(values[1])==7))
    def setAlignment(self,value): self.put(alignment=int(value))
    def setFrameShape(self,value): self.put(frame=bool(value))


class Group(Node): pass


class Column(Node):
    kind='column'
    def __init__(self,parent=None):
        super().__init__(parent)
        if isinstance(parent,Node):
            parent._layout=self
            parent.append(self)


class Row(Column): kind='row'


class Form(Column):
    def addRow(self,label,control):
        row=Row();row.addWidget(label);row.addWidget(control,1);self.addLayout(row)
    def setVerticalSpacing(self,value): self.setSpacing(value)
    def setHorizontalSpacing(self,value): self.put(horizontalSpacing=value)


class Text(Node):
    kind='text'
    def __init__(self,text='',parent=None):
        super().__init__(parent);self.setText(text)


class TextArea(Text):
    kind='textarea'
    def __init__(self,parent=None):
        super().__init__('',parent);self.put(wrap=True,minHeight=140)


class Button(Text):
    kind='button'
    def __init__(self,text='',parent=None):
        super().__init__(text,parent)
        self.put(flat=True)
        icon={'닫기':'close','제거':'minus','추가':'plus','대상 추가':'plus','이전 구간':'left','다음 구간':'right'}.get(text)
        if icon:
            self.put(iconName=icon);self.setFixedSize(36,36)
            self.setAccessibleName(text);self.setToolTip(text)

    clicked=Signal()
    toggled=Signal(bool)
    def setCheckable(self,value): self.put(checkable=bool(value))
    def isChecked(self): return self._state['checked']
    def setChecked(self,value):
        if self.isChecked()!=bool(value):
            self.put(checked=bool(value));self.toggled.emit(bool(value))
    @Slot()
    def activate(self):
        if not self.isEnabled(): return
        if self._state['checkable']: self.setChecked(not self.isChecked())
        self.clicked.emit()


class Toggle(Button):
    kind='toggle'
    def __init__(self,text='',parent=None):
        super().__init__(text,parent);self.setCheckable(True)


class Choice(Node):
    kind='choice'
    currentIndexChanged=Signal(int)
    currentRowChanged=Signal(int)
    def __init__(self,parent=None):
        super().__init__(parent);self._items=[];self.put(index=-1)
    def addItem(self,text,value=None):
        self._items.append(dict(text=str(text),value=value,tooltip=''))
        self.put(items=list(self._items))
        if self.currentIndex()<0:self.setCurrentIndex(0)
    def addItems(self,items):
        for item in items:self.addItem(item)
    def clear(self):
        self._items=[];self.put(items=[]);self.setCurrentIndex(-1)
    def count(self):return len(self._items)
    def currentIndex(self):return self._state['index']
    def currentRow(self):return self.currentIndex()
    def currentData(self):return self.itemData(self.currentIndex())
    def currentText(self):return self.itemText(self.currentIndex())
    def itemData(self,index):return self._items[index]['value'] if 0<=index<len(self._items) else None
    def itemText(self,index):return self._items[index]['text'] if 0<=index<len(self._items) else ''
    def setItemText(self,index,text):
        if 0<=index<len(self._items) and self._items[index]['text']!=str(text):
            self._items[index]['text']=str(text);self.put(items=list(self._items))
    def findData(self,value):return next((i for i,item in enumerate(self._items) if item['value']==value),-1)
    def setItemData(self,index,value,role=Qt.UserRole):
        self._items[index]['tooltip' if role==Qt.ToolTipRole else 'value']=value
        self.put(items=list(self._items))
    @Slot(int)
    def choose(self,index): self.setCurrentIndex(index)
    def setCurrentIndex(self,index):
        if self.currentIndex()!=index:
            self.put(index=index);self.currentIndexChanged.emit(index);self.currentRowChanged.emit(index)
    def setCurrentRow(self,index):self.setCurrentIndex(index)


class Navigation(Choice):kind='navigation'


class Stack(Node):
    kind='stack'
    def setCurrentIndex(self,index):self.put(index=index)
    def currentIndex(self):return self._state['index']


class Tabs(Stack):
    kind='tabs'
    @Slot(int)
    def choose(self,index):self.setCurrentIndex(index)
    def addTab(self,node,title):
        node.put(tabTitle=title);self.append(node)


class Scroll(Node):
    def __init__(self,parent=None):
        super().__init__(parent); self._position=ScrollPosition(self)
    @Property(QObject,constant=True)
    def verticalPosition(self): return self._position
    kind='scroll'
    def setWidget(self,node):self.append(node)
    def setWidgetResizable(self,value):self.put(resizable=bool(value))
    def setVerticalScrollBarPolicy(self,value):self.put(verticalScroll=value!=Qt.ScrollBarAlwaysOff)
    def setHorizontalScrollBarPolicy(self,value):self.put(horizontalScroll=value!=Qt.ScrollBarAlwaysOff)
    def ensureWidgetVisible(self,node):self.revealRequested.emit(node)


class Split(Node):
    kind='split'
    splitterMoved=Signal(int,int)
    fitRequested=Signal()
    def __init__(self,orientation=Qt.Horizontal,parent=None):
        super().__init__(parent);self.put(horizontal=orientation==Qt.Horizontal,spacing=6,sizes=[])
    def setSizes(self,values):self.put(sizes=list(values))
    def setHandleWidth(self,value):self.put(spacing=value)
    def handleWidth(self):return self._state['spacing']
    def saveState(self):return str(self._state['sizes'])
    @Slot()
    def requestFit(self):self.fitRequested.emit()
    @Slot('QVariantList')
    def resized(self,values):
        self.put(sizes=list(values));self.splitterMoved.emit(0,0)


class Input(Text):
    kind='input'
    textChanged=Signal(str)
    @Slot(str)
    def edit(self,value):self.setText(value)
    def setText(self,value):
        if self.text()!=value:
            super().setText(value);self.textChanged.emit(value)


class DateInput(Input):
    kind='date'
    dateChanged=Signal(QDate)
    def __init__(self,value=None,parent=None):
        super().__init__('',parent);self.setDate(value if isinstance(value,QDate) else QDate.currentDate())
    def date(self):return QDate.fromString(self.text(),Qt.ISODate)
    def setDate(self,value):
        self.setText(value.toString(Qt.ISODate));self.dateChanged.emit(value)
    @Slot(str)
    def edit(self,value):
        parsed=QDate.fromString(value,Qt.ISODate)
        if parsed.isValid():self.setDate(parsed)
        else:self.changed.emit()


class Slider(Node):
    kind='slider'
    valueChanged=Signal(int)
    def __init__(self,orientation=Qt.Horizontal,parent=None):super().__init__(parent)
    def setRange(self,lo,hi):self.put(minimum=lo,maximum=hi)
    def value(self):return self._state['value']
    @Slot(int)
    def slide(self,value):self.setValue(value)
    def setValue(self,value):
        value=max(self._state['minimum'],min(self._state['maximum'],value))
        if value!=self.value():self.put(value=value);self.valueChanged.emit(value)


class ScrollPosition(QObject):
    changed=Signal()
    requested=Signal(float)
    def __init__(self,parent=None):super().__init__(parent);self._value=0
    @Property(float,notify=changed)
    def position(self):return self._value
    @Slot(float)
    def setValue(self,value):
        self.requested.emit(value)
        self.observe(value)
    @Slot(float)
    def observe(self,value):
        if self._value!=value:self._value=value;self.changed.emit()
    def value(self):return self._value
    def sizeHint(self):return QSize(12,12)
