from PySide6.QtWidgets import QApplication


def test_unloaded_window_rows_are_identified_until_their_window_arrives():
    from PySide6.QtCore import Qt
    from cachemonitor.table_model import Table
    app=QApplication.instance() or QApplication([])
    table=Table();table.setColumnCount(1)
    formatter=lambda row,column,role:row['id'] if role==Qt.DisplayRole else ''
    table.set_window(dict(start=0,total=3,rows=[dict(id='first')]),formatter,'one')
    model=table.rowModel
    assert model.data(model.index(0,0),Qt.UserRole+6) is False
    assert model.data(model.index(2,0),Qt.UserRole+6) is True
    table.set_window(dict(start=2,total=3,rows=[dict(id='third')]),formatter,'one')
    assert model.data(model.index(2,0),Qt.UserRole+6) is False
    assert model.data(model.index(2,0),Qt.DisplayRole)=='third'
    table.deleteLater();app.processEvents()
