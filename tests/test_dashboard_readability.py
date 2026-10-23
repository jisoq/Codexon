"""Rendered contracts for the approved dashboard pages and excluded tables."""
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.dashboard import Dashboard, PAGE_INDEX
from cachemonitor.fonts import load_bundled_fonts
from cachemonitor.i18n import set_language, language
from cachemonitor.quick_qa import control
from tests.test_ui import snapshot, wait_for_rendered

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('locale,theme,width,height', [
    pytest.param('en','light',1120,760,marks=pytest.mark.essential), ('ko','dark',1700,1000),
])
def test_approved_pages_render_with_stable_routes_and_aligned_fields(tmp_path, locale, theme, width, height):
    app=QApplication.instance() or QApplication([]);load_bundled_fonts()
    previous=app.property('cachemonitorDisableShellIntegration');old_language=language()
    app.setProperty('cachemonitorDisableShellIntegration',True);set_language(locale)
    settings=QSettings(str(tmp_path/'ui.ini'),QSettings.IniFormat);settings.setValue('ui/theme',theme)
    window=Dashboard(['fixture'],start_worker=False,live_limits=False,settings=settings,static_snapshot=snapshot())
    try:
        window.resize(width,height);window.show();QTest.qWait(90)
        for page in (3,1,5,6,7,4):
            window.change_page(page)
            rendered=control(window,window.pages.widget(PAGE_INDEX[page]))
            wait_for_rendered(lambda:rendered.isVisible())
            assert not window.qml_errors
            assert window.quick.grabFramebuffer().save(str(tmp_path/f'page-{page}.png'))
        assert not window.quota_panel.intervals.state.get('readableColumns',False)
        assert not window.quota_panel.table.state.get('readableColumns',False)
        assert window.notification_log.state['readableColumns'] is False
    finally:
        window.quit_app();set_language(old_language);app.setProperty('cachemonitorDisableShellIntegration',previous)
