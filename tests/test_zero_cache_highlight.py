from PySide6.QtCore import Qt,QSettings,QPointF,QPoint
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.lazy_table import LazyTable
from cachemonitor.quick_qa import mount,dispose,table_view,walk


def test_explicit_zero_role_is_lazy_and_survives_reorder_and_correction():
    app=QApplication.instance() or QApplication([])
    table=LazyTable(['ID','Cache']);table.put(highlightZeroCache=True)
    rows=[dict(key=str(i),input=100,cached=value) for i,value in enumerate((0,None,False,'0',10,0.0))]
    table.set_rows(rows,lambda r,c,role:str(r['key'] if c==0 else r.get('cached')))
    model=table.model();before=model.formatted
    assert [model.data(model.index(i,0),Qt.UserRole+2) for i in range(6)]==[True,False,False,False,False,False]
    assert model.formatted==before
    table.horizontalHeader().moveSection(0,1)
    assert model.data(model.index(0,1),Qt.UserRole+2)
    table.set_rows([{**rows[0],'cached':5},rows[1]],model.formatter)
    assert not model.data(model.index(0,0),Qt.UserRole+2)
    ordinary=LazyTable(['ID']);ordinary.set_rows([rows[0]],lambda *_:'0')
    assert not ordinary.model().data(ordinary.model().index(0,0),Qt.UserRole+2)


def test_rendered_zero_row_selection_and_hover_remain_distinct(tmp_path):
    app=QApplication.instance() or QApplication([])
    table=LazyTable(['호출','캐시']);table.put(highlightZeroCache=True)
    table.set_rows([dict(key='first',input=100,cached=0),dict(key='unknown',cached=None),dict(key='hit',cached=50)],
                   lambda r,c,role:r['key'] if c==0 else '—' if r['cached'] is None else str(r['cached']))
    host=mount(table,600,260);host.move(400,400);host.activateWindow()
    try:
        QTest.qWait(60)
        cells=[item for item in walk(table_view(host,table)) if item.metaObject().indexOfProperty('cacheZero')>=0]
        zero=next(item for item in cells if item.property('row')==0 and item.property('column')==0)
        unknown=next(item for item in cells if item.property('row')==1 and item.property('column')==0)
        assert zero.property('cacheZero') and not unknown.property('cacheZero')
        # Earlier rendered tests may leave the native pointer over this row.
        # Establish the non-hover state before checking its distinct color.
        QTest.mouseMove(host.quick.quickWindow(),QPoint(500,180));QTest.qWait(40)
        assert zero.property('color')==QColor('#fff4e4')
        point=zero.mapToScene(QPointF(30,15)).toPoint()
        QTest.mouseMove(host.quick.quickWindow(),point);QTest.qWait(80)
        assert zero.property('color')==QColor('#ffe8c2'), (point,zero.isVisible(),host.quick.size())
        table.selectRow(0);QTest.qWait(40)
        assert zero.property('color')==QColor(__import__('cachemonitor.theme',fromlist=['shared_theme']).shared_theme().palette['secondary'])
        marker=next(item for item in zero.childItems() if item.objectName()=='cache-zero-marker')
        assert marker.isVisible()
        assert host.grab().save(str(tmp_path/'cache-zero-selected.png'))
        table.selectRow(2);QTest.qWait(40)
        assert host.grab().save(str(tmp_path/'cache-zero.png'))
        assert not host.qml_errors
    finally:dispose(host)


def test_dashboard_enables_zero_highlight_only_for_call_tables(tmp_path):
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([],start_worker=False,settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat),live_limits=False)
    try:
        from cachemonitor.dashboard import choose
        from test_ui import snapshot
        window.receive(snapshot());window.nav.setCurrentRow(2)
        assert not window.table.state.get('highlightZeroCache')
        choose(window.record_view_choice,'calls');window.record_view_changed()
        assert window.table.state['highlightZeroCache']
        zeros=[r for r in window.record_rows if r['cached']==0]
        assert len(zeros)==2 and window.selected_session is not None
        row=next(i for i,r in enumerate(window.record_rows) if r['cached']==0)
        cache_column=window.table.model().headers.index('입력 캐시율')
        assert window.table.item(row,cache_column).text()=='0.0%'
        assert '관측 상태' not in window.table.model().headers
        assert window.table.model().data(window.table.model().index(row,cache_column),Qt.UserRole+2)
        window.resize(1800,1000);window.show();QTest.qWait(60)
        window.table.scrollTo(window.table.model().index(row,cache_column));QTest.qWait(60)
        cell=next(item for item in walk(table_view(window,window.table))
                  if item.property('row')==row and item.property('column')==cache_column
                  and item.metaObject().indexOfProperty('cacheZero')>=0)
        assert cell.property('cacheZero') and cell.isVisible()
        assert not window.qml_errors
    finally:window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',previous)
