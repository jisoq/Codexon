"""Interaction helpers that exercise the rendered Quick scene, not model facades."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from .quick_runtime import QuickHost


def walk(item):
    yield item
    for child in item.childItems():yield from walk(child)


def scene_view(host,node):
    # Popups live in the window overlay, alongside the presentation root.
    for item in walk(host.quick.quickWindow().contentItem()):
        if item.property('node') is node and item.metaObject().indexOfProperty('sourceComponent')>=0:
            return item
    raise AssertionError(f'Presentation node not rendered: {node.objectName()} ({node.kind})')


def control(host,node):
    import time
    from shiboken6 import isValid
    from .presentation import Stack
    incubation=getattr(host,'_page_incubation',None)
    deadline=time.monotonic()+10
    view=None
    while True:
        item=None;parents_ready=False;model_ready=True
        try:
            # Follow the small model ancestry before searching a partial scene.
            # Its deferred page must finish before any contained control can
            # satisfy the corresponding QML ancestor readiness check below.
            child=node;parent=child.parent()
            while parent is not None:
                if (isinstance(parent,Stack) and parent._state.get('kind')=='stack'
                        and parent._state.get('deferPages') and child in parent.nodes
                        and not parent.isPageReady(parent.nodes.index(child))):
                    model_ready=False;break
                child=parent;parent=parent.parent()
            if model_ready:
                # Cache a found view, but follow deletion or model reassignment.
                if view is None or not isValid(view) or view.property('node') is not node:
                    view=None
                    view=scene_view(host,node)
                item=view.property('item')
                parents_ready=True;parent=view.parentItem()
                while parent is not None:
                    meta=parent.metaObject()
                    if meta.indexOfProperty('loadRequested')>=0 and meta.indexOfProperty('ready')>=0 and not parent.property('ready'):
                        parents_ready=False;break
                    parent=parent.parentItem()
            # Other hidden pages may still be loading. Only this control's
            # enclosing loaders must be ready, including an outer page loader.
            if item is not None and isValid(item) and parents_ready:return item
        except (AssertionError,RuntimeError):
            view=None
        if time.monotonic()>=deadline:
            pending=incubation.incubatingObjectCount() if incubation else 0
            raise AssertionError(f'Control did not finish rendering: {node.objectName()} ({node.kind}); '
                                 f'view={view is not None}, item={item is not None}, model_ready={model_ready}, parents_ready={parents_ready}, incubating={pending}; '
                                 f'QML errors: {host.qml_errors}')
        QTest.qWait(10)


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
    import time
    control(host,node)
    parent=node.parent();scrolls=[]
    while parent:
        if isinstance(parent,Scroll):
            parent.ensureWidgetVisible(node);scrolls.append(parent)
        parent=parent.parent()
    deadline=time.monotonic()+3
    while True:
        QTest.qWait(20);host.quick.grabFramebuffer()
        if all(control(host,s).property('revealTarget') is None for s in scrolls):break
        if time.monotonic()>deadline:raise AssertionError('Scroll reveal did not settle')
    return control(host,node)


def click_plot(host,node,point):
    item=render_plot(host,node);click(host,item,point.x(),point.y())


def click_row(host,node,row,column=0):
    import time
    render_plot(host,node)
    table=table_view(host,node)
    if node.state.get('freezeFirstColumn') and column==0:
        table=next(item for item in walk(control(host,node)) if item.objectName()=='frozen-column')
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
    raise AssertionError(f'Visible table delegate missing: {row}, {column}; '
        f'window={host.width()}x{host.height()}, scale={host.devicePixelRatioF()}, '
        f'table={table.width()}x{table.height()}, visible={node.isVisible()}')


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
