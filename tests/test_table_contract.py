from PySide6.QtCore import Qt
from PySide6.QtTest import QAbstractItemModelTester, QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.quick_qa import dispose, mount, table_view, walk
from cachemonitor.table_model import Table


def test_filter_row_changes_preserve_model_contract_without_resets():
    app=QApplication.instance() or QApplication([])
    table=Table(headers=['Value']);model=table.model()
    tester=QAbstractItemModelTester(model,QAbstractItemModelTester.FailureReportingMode.Fatal)
    events=[];model.modelReset.connect(lambda:events.append('reset'))
    model.rowsInserted.connect(lambda parent,first,last:events.append(('insert',first,last)))
    model.rowsRemoved.connect(lambda parent,first,last:events.append(('remove',first,last)))
    formatter=lambda row,column,role:str(row['value'])
    for values in ([1,2,3],[],[4],[4,5,6]):
        table.set_rows([dict(id=value,value=value) for value in values],formatter)
        assert [model.data(model.index(i,0)) for i in range(model.rowCount())]==list(map(str,values))
    assert events==[('insert',0,2),('remove',0,2),('insert',0,0),('insert',1,2)]
    assert tester.model() is model


def test_matrix_exact_values_shared_bars_and_keyboard_column_render():
    app=QApplication.instance() or QApplication([])
    table=Table(headers=['Target','A','B'])
    table.put(widths=[70,170,170],rowHeight=64,noElideColumns=[1,2],dataBars=True)
    values=['A','$123456789012345.6789\n유효 12,345 / 대상 99,999','$34.5678\n유효 4 / 대상 10']
    table.set_rows([dict(id='A',values=values,bars=[None,.25,.75])],lambda row,column,role:row['values'][column])
    activated=[];table.cellActivated.connect(lambda row,column:activated.append((row,column)))
    host=mount(table,740,180)
    try:
        view=table_view(host,table);QTest.qWait(50)
        cells={item.property('column'):item for item in walk(view)
               if item.metaObject().indexOfProperty('cellBar')>=0 and item.property('row')==0}
        for column in (1,2):
            label=next(item for item in walk(cells[column]) if item.objectName()=='cell-label')
            assert label.property('text')==values[column] and not label.property('truncated')
            assert label.property('contentWidth')<=label.width()+1
        bars=[next(item for item in walk(cells[column]) if item.objectName()=='cell-data-bar') for column in (1,2)]
        assert all(bar.isVisible() for bar in bars) and bars[1].width()==3*bars[0].width()
        table.selectRow(0);view.forceActiveFocus()
        QTest.keyClick(host.quick,Qt.Key_Right);QTest.keyClick(host.quick,Qt.Key_Right)
        QTest.keyClick(host.quick,Qt.Key_Return)
        assert activated==[(0,2)]
        assert not host.qml_errors
    finally:dispose(host)
