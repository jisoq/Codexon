from PySide6.QtCore import Qt,QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.lazy_table import LazyTable
from cachemonitor.quick_qa import table_view,walk


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
