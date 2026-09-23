"""Interaction helpers that exercise the rendered Quick scene, not model facades."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from .quick_runtime import QuickHost


def walk(item):
    yield item
    for child in item.childItems():yield from walk(child)


def scene_view(host,node):
    matches=[item for item in walk(host.quick.rootObject())
             if item.property('node') is node and item.metaObject().indexOfProperty('sourceComponent')>=0]
    if not matches:raise AssertionError(f'Presentation node not rendered: {node.objectName()} ({node.kind})')
    return matches[0]


def control(host,node):
    view=scene_view(host,node)
    return view.property('item')


def click(host,item,x=None,y=None):
    point=item.mapToScene(QPointF(item.width()/2 if x is None else x,item.height()/2 if y is None else y))
    assert host.quick.rect().contains(point.toPoint()), f'Click outside visible scene: {point}, {host.quick.size()}'
    QTest.mouseClick(host.quick,Qt.LeftButton,pos=point.toPoint());QTest.qWait(30)


def mount(node,width=800,height=600):
    host=QuickHost();host.resize(width,height);host.setCentralWidget(node);host.show();QTest.qWait(60)
    return host


def table_view(host,node):
    table=next((item for item in walk(control(host,node)) if item.objectName()=='dataTable'),None)
    if table is None:raise AssertionError('TableView not rendered')
    return table


def dispose(host):
    host.release_scene();host.close();host.deleteLater();QTest.qWait(1)


def render_plot(host,node):
    """Bring the plot into its real scroll view and wait for a painted frame."""
    from .presentation import Scroll
    parent=node.parent()
    while parent:
        if isinstance(parent,Scroll):parent.ensureWidgetVisible(node)
        parent=parent.parent()
    QTest.qWait(50)
    host.quick.grabFramebuffer()
    return control(host,node)


def click_plot(host,node,point):
    item=render_plot(host,node);click(host,item,point.x(),point.y())


def click_row(host,node,row,column=0):
    import time
    render_plot(host,node)
    table=table_view(host,node)
    node.scrollTo(node.model().index(row,column))
    # Find the actual delegate, including rows which were virtualized previously.
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        QTest.qWait(10)
        cells=[item for item in walk(table) if item.isVisible() and item.property('row')==row
               and item.property('column')==column and item.metaObject().indexOfProperty('display')>=0]
        for cell in cells:
            point=cell.mapToItem(table,QPointF(cell.width()/2,cell.height()/2))
            if 0<=point.x()<table.width() and 0<=point.y()<table.height():
                scene=cell.mapToScene(QPointF(cell.width()/2,cell.height()/2))
                if host.quick.rect().contains(scene.toPoint()):
                    click(host,cell);return
    raise AssertionError(f'Visible table delegate missing: {row}, {column}')


def flickable(host,node):
    return table_view(host,node) if node.kind=='table' else control(host,node).property('contentItem')


def scroll_extent(host,node):
    item=flickable(host,node)
    return max(0,item.property('contentHeight')-item.height())


def scroll_middle(host,node):
    item=flickable(host,node);item.setProperty('contentY',scroll_extent(host,node)/2);QTest.qWait(20)


def wheel(host,node,delta):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication
    item=flickable(host,node)
    point=item.mapToScene(QPointF(item.width()/2,item.height()/2))
    event=QWheelEvent(point,QPointF(host.quick.mapToGlobal(point.toPoint())),QPoint(),QPoint(0,delta),
                      Qt.NoButton,Qt.NoModifier,Qt.NoScrollPhase,False)
    QApplication.sendEvent(host.quick,event)
