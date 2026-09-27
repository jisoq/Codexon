"""Exercise adjacent record depths without touching user settings or services."""
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from verify_design import fixture_snapshot
from cachemonitor.fonts import configure_font_rendering,configure_high_dpi,load_bundled_fonts
configure_font_rendering();configure_high_dpi()
from PySide6.QtCore import QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.dashboard import Dashboard
from cachemonitor.quick_qa import click_row,table_view,walk

out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
app=QApplication([]);load_bundled_fonts();app.setProperty('cachemonitorDisableShellIntegration',True)
with tempfile.TemporaryDirectory() as tmp:
    settings=QSettings(str(Path(tmp)/'settings.ini'),QSettings.IniFormat);settings.setValue('ui/theme','light')
    w=Dashboard([],start_worker=False,settings=settings,static_snapshot=fixture_snapshot())
    w.setAttribute(Qt.WA_ShowWithoutActivating);w.show();w.nav.setCurrentRow(2)
    def settle():
        QTest.qWait(100)
        assert not w.qml_errors,w.qml_errors
    def capture(name):
        settle()
        for node in (w.parent_table,w.table):
            for cell in walk(table_view(w,node)):
                if cell.isVisible() and cell.metaObject().indexOfProperty('display')>=0 and cell.metaObject().indexOfProperty('row')>=0:
                    row,column=cell.property('row'),cell.property('column')
                    assert cell.property('display')==node.model().data(node.model().index(row,column)),(name,row,column,cell.property('display'),node.model().data(node.model().index(row,column)))
        assert w.grab().save(str(out/(name+'.png')))
    try:
        for width,height in ((1440,940),(1120,760)):
            w.resize(width,height);settle()
            w.go_home();settle()
            assert w.record_view=='projects' and w.selected_session is None
            capture(f'{width}-projects')
            click_row(w,w.table,0);settle()
            assert w.record_view=='sessions'
            click_row(w,w.table,0);settle()
            assert w.record_view=='requests'
            session=w.selected_session
            capture(f'{width}-requests')
            table_view(w,w.table).forceActiveFocus()
            QTest.keyClick(w.quick,Qt.Key_Home);QTest.keyClick(w.quick,Qt.Key_Return);settle()
            assert w.record_view=='calls'
            capture(f'{width}-calls')
            w.go_back();settle()
            assert w.record_view=='requests' and w.selected_session==session
        (out/'result.json').write_text(json.dumps({'project_first_hierarchy':True,'mouse_and_keyboard':True,'back_preserves_session':True,'sizes':[1440,1120],'qml_errors':w.qml_errors}),encoding='utf-8')
        print('History navigation verification passed')
    finally:
        w.quitting=True;w.tick.stop();w.tray.hide();w.observer_panel.stop();w.close()
