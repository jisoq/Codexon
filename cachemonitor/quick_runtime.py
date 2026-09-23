"""Qt Quick scene hosting and native window lifetime.

QWidget is used only as the Windows native host (tray/window ownership). Every
application control and view inside the host is rendered by Qt Quick.
"""
from pathlib import Path
from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl, Qt, QTimer
from PySide6.QtGui import QFont, QPainter, QColor, QPen
from PySide6.QtQml import qmlRegisterType
from PySide6.QtQuick import QQuickPaintedItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QWidget, QVBoxLayout
from .presentation import Group, Column, Button, Row


class QuickPlot(QQuickPaintedItem):
    sourceChanged=Signal()
    tipChanged=Signal()
    detailChanged=Signal()
    def __init__(self,parent=None):
        super().__init__(parent);self._source=None;self._tip='';self._hover=None;self._hover_index=-1
        self._detail={};self._detail_pinned=False;self._detail_x=0;self._detail_y=0
        self.setAntialiasing(True)
        self.activeFocusChanged.connect(self.update)
    @Property(QObject,notify=sourceChanged)
    def source(self):return self._source
    @source.setter
    def source(self,value):
        if self._source is value:return
        if self._source:self._source.changed.disconnect(self.update)
        self._source=value
        if value:value.changed.connect(self.update)
        self.sourceChanged.emit();self.update()
    @Property(str,notify=tipChanged)
    def tip(self):return self._tip
    @Property(int,notify=tipChanged)
    def hoverIndex(self):return self._hover_index
    @Property('QVariantMap',notify=detailChanged)
    def detail(self):return self._detail
    @Property(bool,notify=detailChanged)
    def detailPinned(self):return self._detail_pinned
    @Property(float,notify=detailChanged)
    def detailX(self):return self._detail_x
    @Property(float,notify=detailChanged)
    def detailY(self):return self._detail_y
    def set_detail(self,value,x,y,pinned=False):
        self._detail=value;self._detail_x=x;self._detail_y=y;self._detail_pinned=pinned
        self.detailChanged.emit()
    @Slot()
    def dismissDetail(self):self.set_detail({},0,0)
    def paint(self,painter):
        if self._source:
            from .i18n import LocalizedPainter
            self._source._paint_width=int(self.width());self._source._paint_height=int(self.height())
            self._source._focused=self.hasActiveFocus();self._source._painter=LocalizedPainter(painter)
            self._source.paint(painter)
            if self._detail_pinned and hasattr(self._source,'refresh_detail'):
                current=self._source.refresh_detail(self._detail)
                if current!=self._detail:self._detail=current;self.detailChanged.emit()
            from .theme import shared_theme
            if self._hover is not None:
                hit=next((rect for rect,row,tip in reversed(self._source.hits) if rect.contains(self._hover)),None)
                if hit is not None:
                    color=QColor(shared_theme().palette['accent']);color.setAlpha(24)
                    painter.fillRect(hit,color);color.setAlpha(150);painter.setPen(QPen(color,1));painter.setBrush(Qt.NoBrush);painter.drawRect(hit.adjusted(1,1,-1,-1))
            self._source._painter=None
    @Slot(float,float)
    def activateAt(self,x,y):
        if self._source:
            self._source.activate_at(x,y)
            if hasattr(self._source,'detail_at'):self.set_detail(self._source.detail_at(x,y),x,y,True)
    @Slot(float,float)
    def showTip(self,x,y):
        from PySide6.QtCore import QPointF
        point=QPointF(x,y)
        index=next((i for i,(rect,row,tip) in reversed(list(enumerate(self._source.hits))) if rect.contains(point)),-1) if self._source else -1
        self._hover=point
        if index!=self._hover_index:self._hover_index=index;self.tipChanged.emit();self.update()
        value=self._source.tip_at(x,y) if self._source else ''
        if self._tip!=value:self._tip=value;self.tipChanged.emit()
        if self._source and hasattr(self._source,'detail_at') and not self._detail_pinned:
            self.set_detail(self._source.detail_at(x,y),x,y)
    @Slot()
    def clearHover(self):
        self._hover=None;self._hover_index=-1;self._tip="";self.tipChanged.emit();self.update()
        if not self._detail_pinned:self.dismissDetail()
    @Slot(int)
    def key(self,key):
        if key==Qt.Key_Escape and self._detail:self.dismissDetail();return
        if self._source:
            self._source.key(key)
            if hasattr(self._source,'detail_for') and self._source.rows:
                point=self._source.point(self._source.cursor,'remaining')
                self.set_detail(self._source.detail_for(self._source.cursor),point.x(),point.y(),True)


_registered=False


def register_types():
    global _registered
    if not _registered:
        QQuickStyle.setStyle('Fusion')
        qmlRegisterType(QuickPlot,'CacheMonitor',6,0,'QuickPlot')
        _registered=True


class QuickHost(QWidget):
    def __init__(self,parent=None,flags=Qt.Window):
        super().__init__(parent,flags);self.quick=None;self.presentation=None;self.qml_errors=[]
    def setCentralWidget(self,node):
        self.set_scene(node,'Main.qml')

    def set_scene(self,node,filename,transparent=False):
        register_types();self.presentation=node;node.setParent(self)
        self.quick=QQuickWidget(self);self.quick.setResizeMode(QQuickWidget.SizeRootObjectToView)
        from .theme import shared_theme
        from .i18n import Translator
        self.quick.rootContext().setContextProperty('appTheme', shared_theme())
        self._translator=Translator(self)
        self.quick.rootContext().setContextProperty('appLanguage', self._translator)
        self.quick.engine().warnings.connect(lambda errors:self.qml_errors.extend(e.toString() for e in errors))
        if transparent:self.quick.setClearColor(Qt.transparent)
        self.quick.setInitialProperties({'presentation':node})
        self.quick.setSource(QUrl.fromLocalFile(str(Path(__file__).parent/'qml'/filename)))
        layout=QVBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.addWidget(self.quick)
        if self.quick.status()==QQuickWidget.Error:
            raise RuntimeError('\n'.join(error.toString() for error in self.quick.errors()))

    def release_scene(self):
        if self.quick is not None:
            self.quick.setSource(QUrl())
            from shiboken6 import delete
            delete(self.quick)
            self.quick=None


class Dialog(Group):
    finished=Signal(int)
    def __init__(self,parent=None):
        super().__init__(parent);self._title='';self._size=(790,650);self.host=None;self.result=0
    def setWindowTitle(self,title):self._title=title
    def resize(self,w,h):self._size=(w,h)
    def exec(self):self.open()
    def open(self):
        self.host=DialogHost(self);self.host.setWindowTitle(self._title)
        self.host.resize(*self._size);self.host.setCentralWidget(self)
        self.host.setWindowModality(Qt.ApplicationModal);self.host.show()
    @Slot()
    def reject(self):
        self.finish(0)
    def finish(self,result):
        self.result=result
        if self.host:self.host.close()
        else:self.finished.emit(result)


class DialogHost(QuickHost):
    def __init__(self,dialog):super().__init__(None,Qt.Dialog);self.dialog=dialog
    def keyPressEvent(self,event):
        if event.key()==Qt.Key_Escape:self.dialog.reject();event.accept()
        else:super().keyPressEvent(event)
    def closeEvent(self,event):
        event.accept()
        if getattr(self,'_closing',False):return
        self._closing=True
        # A QML button can request this close from its own signal handler.
        # Destroy its engine only after that handler has returned.
        QTimer.singleShot(0,self.finish_close)

    def finish_close(self):
        self.release_scene()
        self.dialog.finished.emit(self.dialog.result)
        self.deleteLater()


class Confirmation(Dialog):
    def __init__(self,title,message,parent=None):
        super().__init__(parent);self.setWindowTitle(title);self.resize(530,270)
        from .presentation import Text
        layout=Column(self);layout.setContentsMargins(22,20,22,20)
        text=Text(message);text.setWordWrap(True);layout.addWidget(text,1)
        row=Row();row.addStretch()
        self.confirm=Button('확인');self.cancel=Button('취소');self.cancel.put(defaultFocus=True)
        self.confirm.clicked.connect(lambda:self.finish(1));self.cancel.clicked.connect(self.reject)
        row.addWidget(self.confirm);row.addWidget(self.cancel);layout.addLayout(row)


class DialogButtons(Row):
    rejected=Signal()
    Close=0
    def __init__(self,buttons=0):
        super().__init__();self.addStretch();button=Button('닫기')
        button.clicked.connect(self.rejected);self.addWidget(button)
