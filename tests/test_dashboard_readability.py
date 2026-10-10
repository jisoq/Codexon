"""Rendered contracts for the approved dashboard pages and excluded tables."""
import pytest
from PySide6.QtCore import QSettings, Qt, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.dashboard import Dashboard, choose
from cachemonitor.fonts import load_bundled_fonts
from cachemonitor.i18n import set_language, language
from cachemonitor.quick_qa import control, walk, mount, dispose
from cachemonitor.table_model import Table
from tests.test_ui import snapshot


@pytest.mark.parametrize('locale,theme,width,height', [
    ('ko','light',1700,1000), ('en','light',1120,760),
    ('ko','dark',2400,1100), ('en','dark',1700,1000),
])
def test_approved_pages_render_with_stable_routes_and_aligned_fields(tmp_path, locale, theme, width, height):
    app=QApplication.instance() or QApplication([]);load_bundled_fonts()
    previous=app.property('cachemonitorDisableShellIntegration');old_language=language()
    app.setProperty('cachemonitorDisableShellIntegration',True);set_language(locale)
    settings=QSettings(str(tmp_path/'ui.ini'),QSettings.IniFormat);settings.setValue('ui/theme',theme)
    window=Dashboard(['fixture'],start_worker=False,live_limits=False,settings=settings,static_snapshot=snapshot())
    try:
        window.resize(width,height);window.show();QTest.qWait(90)
        for page in (0,1,2,0,2):
            window.change_page(page);QTest.qWait(70)
            assert not window.qml_errors
            # A field has exactly one rendered owner after moving between pages.
            views=[item for item in walk(window.quick.rootObject()) if item.property('node') is window.period and item.metaObject().indexOfProperty('sourceComponent')>=0]
            assert len(views)==1
            assert control(window,window.period).isVisible()
            if page==0:
                for caption in window.metric_captions:
                    item=control(window,caption)
                    rendered=next(child for child in walk(item) if child.isVisible() and child.metaObject().indexOfProperty('truncated')>=0)
                    assert not rendered.property('truncated')
                    assert item.height()>=rendered.property('contentHeight')
            if page in (0,1):assert window.common_filters._nodes==list(window.common_fields.values())
            else:assert not window.scope_note.isVisible()
            assert window.quick.grabFramebuffer().save(str(tmp_path/f'page-{page}.png'))
        assert not window.home_button.isVisible()
        assert window.history_path.entries[0]['action']=='root'
        window.activate_record(0);window.activate_record(0);QTest.qWait(70)
        assert window.record_view=='requests'
        assert not any(tab.isVisible() for tab in window.record_tabs)
        table=control(window,window.table)
        headers=[item for item in walk(table) if item.objectName()=='table-header-label']
        labels=[item for item in walk(table) if item.objectName()=='cell-label']
        assert headers and labels
        for header in headers:
            col=header.parentItem().property('column')
            for cell in (item for item in labels if item.parentItem().property('column')==col):
                assert header.property('textAlignment')==cell.property('textAlignment')
                assert abs(header.mapToScene(QPointF()).x()-cell.mapToScene(QPointF()).x())<1
                assert abs(header.width()-cell.width())<1
        assert window.quick.grabFramebuffer().save(str(tmp_path/'requests.png'))
        choose(window.sort,'time_desc');window.render_explorer()
        ordinal=window.record_rows[0]['ordinal'];window.activate_record(0)
        assert str(ordinal) in window.history_path.entries[-1]['title']
        window.activate_record(0);QTest.qWait(70)
        assert window.history_path.entries[-1]['action']=='leaf'
        assert not window.call_columns_row.isVisible()
        window.record_columns.toggle.setChecked(True);QTest.qWait(30)
        assert window.call_columns_row.isVisible()
        window.record_columns.toggle.setChecked(False);QTest.qWait(30)
        assert not window.call_columns_row.isVisible()
        assert control(window,window.close_record_button).isVisible()
        assert control(window,window.record_detail_title).isVisible()
        fields=window.detail_sections['identity'][1].state['fields']
        assert all(not field['label'].endswith(' ID') for field in fields)
        assert window.record_identifiers.state['fields']
        assert not window.record_identifiers_details.content.isVisible()
        assert window.quick.grabFramebuffer().save(str(tmp_path/'call-detail.png'))
        route=control(window,window.history_path)
        crumbs=[item for item in walk(route) if item.objectName().startswith('history-crumb-')]
        for before,after in zip(crumbs,crumbs[1:]):
            if abs(before.y()-after.y())<1:
                assert abs(after.x()-before.x()-before.width()-4)<1
        # Opt-in styling cannot reach the explicitly excluded UI surfaces.
        assert not window.quota_panel.intervals.state.get('readableColumns',False)
        assert not window.quota_panel.table.state.get('readableColumns',False)
        assert window.notification_log.state['readableColumns'] is False
        window.close_record_detail();window.go_back()
        assert not window.selected_call
        assert not window.qml_errors
        # Resize callbacks must be harmless after the user has changed tabs.
        window.change_page(0);window.apply_explorer()
    finally:
        window.quit_app();set_language(old_language);app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_table_style_is_opt_in():
    app=QApplication.instance() or QApplication([])
    node=Table(headers=['Name','Value']);node.set_rows([['Example','123']],lambda row,col,role:row[col])
    host=mount(node,600,250)
    try:
        table=control(host,node)
        assert table.property('readableColumns') is False
        assert table.property('cellPadding')==9
        assert not any(item.isVisible() for item in walk(table) if item.objectName()=='column-divider')
        node.put(readableColumns=True,leftColumns=[0]);QTest.qWait(40)
        assert table.property('cellPadding')==12
        assert any(item.isVisible() for item in walk(table) if item.objectName()=='column-divider')
    finally:dispose(host)
