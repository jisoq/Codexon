"""User-visible refresh, text selection and project-name regressions."""
import copy
import time

import pytest
from PySide6.QtCore import QMetaObject, QMimeData, QPointF, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.core import Session
from cachemonitor.dashboard import Dashboard
from cachemonitor.quick_qa import control, walk


def _snapshot():
    now = time.time()
    sources = []
    for index in range(7):
        session = Session(f'layout-{index}', 'fixture', title=f'실제 작업 이름 {index}')
        session.cwd = f'V:/engineering/프로젝트-{index}'
        session.add_usage(now - 30, f'call-{index}',
                          dict(input_tokens=10000, cached_input_tokens=8000, output_tokens=100),
                          'gpt-6-astra', f'turn-{index}', 'high', service_tier='Standard')
        sources.append(session.view(now))
    return dict(ts=now, sessions=sources, homes=[], errors=[], unassigned=[])


@pytest.fixture
def completed_dashboard(tmp_path):
    app = QApplication.instance() or QApplication([])
    previous = app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration', True)
    settings = QSettings(str(tmp_path / 'layout.ini'), QSettings.IniFormat)
    settings.setValue('ui/theme', 'light')
    window = Dashboard([], start_worker=False, settings=settings, static_snapshot=_snapshot())
    window.show()
    QTest.qWait(100)
    try:
        yield window
    finally:
        window.quit_app()
        app.setProperty('cachemonitorDisableShellIntegration', previous)


@pytest.mark.parametrize('width,height', [(1120, 760), (1800, 1000)])
def test_detail_last_wrapped_line_is_visible_and_selectable(completed_dashboard, tmp_path, width, height):
    window = completed_dashboard
    window.resize(width, height)
    window.open_record({'home': 'fixture', 'sid': 'layout-0', 'turn': 'turn-0', 'key': 'call-0'})
    QTest.qWait(100)
    assert window.detail_scroll.isVisible()
    long_text = '\n'.join(
        f'{index}: C:/Users/User/Documents/engineering/long-project-name/실제프로젝트/자료.txt 실제 기록'
        for index in range(16)) + '\n마지막 줄까지 선택 및 복사 가능'
    editors = []
    sections = [(group, text) for group, text in window.detail_sections.values() if group.isVisible()]
    assert sections
    for group, text in sections:
        text.setText(long_text)
    QTest.qWait(100)
    for group, text in sections:
        item = control(window, text)
        editor = next(child for child in walk(item)
                      if child.metaObject().indexOfProperty('selectedText') >= 0)
        assert editor.height() >= editor.property('contentHeight')
        assert item.height() >= editor.property('contentHeight')
        assert QMetaObject.invokeMethod(editor, 'selectAll')
        assert editor.property('selectedText') == long_text
        editors.append(editor)
    clipboard = QApplication.clipboard()
    previous = QMimeData()
    previous_data = clipboard.mimeData()
    previous_formats = previous_data.formats() if previous_data is not None else ()
    for mime_type in previous_formats:
        previous.setData(mime_type, previous_data.data(mime_type))
    try:
        assert QMetaObject.invokeMethod(editors[-1], 'copy')
        assert clipboard.text() == long_text
    finally:
        if previous_formats:
            clipboard.setMimeData(previous)
        else:
            clipboard.clear()
    for editor in editors:
        QMetaObject.invokeMethod(editor, 'deselect')
    scroll = control(window, window.detail_scroll)
    viewport = scroll.property('contentItem')
    window.detail_scroll.verticalPosition.setValue(viewport.property('contentHeight') - viewport.height())
    QTest.qWait(60)
    last = editors[-1]
    bottom = last.mapToItem(viewport, QPointF(0, last.property('contentHeight'))).y()
    assert 0 < bottom <= viewport.height() + 1
    assert window.grab().save(str(tmp_path / f'full-detail-{width}.png'))
    assert not window.qml_errors
