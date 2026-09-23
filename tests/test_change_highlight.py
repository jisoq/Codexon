from PySide6.QtTest import QTest
from cachemonitor.quick_qa import mount, dispose
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from cachemonitor.dashboard import table,fill
from cachemonitor.lazy_table import LazyTable


def test_refresh_highlights_changed_cell_but_not_initial_unchanged_or_filter():
    app=QApplication.instance() or QApplication([])
    view=table(['Model','Calls','USD']);host=mount(view,600,250)
    view.live_update=True
    fill(view,[['A','1','$2'],['B','2','$3']]);app.processEvents();QTest.qWait(40)
    assert not view.changes.active
    view.selectRow(1)
    fill(view,[['A','1','$2'],['B','2','$4']]);app.processEvents();QTest.qWait(40)
    assert set(view.changes.active)=={('B',2)}
    assert view.currentRow()==1
    view.changes.clear()
    fill(view,[['A','1','$2'],['B','2','$4']])
    assert not view.changes.active
    view.live_update=False
    fill(view,[['B','20','$40']])
    assert not view.changes.active
    dispose(host)


def test_lazy_refresh_compares_identity_when_rows_move_without_full_formatting():
    app=QApplication.instance() or QApplication([])
    view=LazyTable(['ID','Count']);host=mount(view,500,250)
    formatter=lambda r,c,role: str(r['id'] if c==0 else r['count'])
    rows=[{'id':str(i),'count':i} for i in range(10000)]
    view.set_rows(rows,formatter);app.processEvents();QTest.qWait(40)
    view.live_update=True
    view.set_rows([rows[1],{**rows[0],'count':99},*rows[2:]],formatter);app.processEvents();QTest.qWait(40)
    assert set(view.changes.active)=={((None,None,'0'),1)}
    assert view.model().formatted<200
    view.changes.active={k:0 for k in view.changes.active};view.changes.tick()
    assert not view.changes.active and not view.changes.timer.isActive()
    dispose(host)
