"""User-visible refresh, text selection and project-name regressions."""
import copy
import time

import pytest
from PySide6.QtCore import QMetaObject, QMimeData, QObject, QPointF, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.core import Session
from cachemonitor.dashboard import Dashboard
from cachemonitor.presentation import Choice, Column, Group
from cachemonitor.quick_qa import click, control, dispose, mount, walk


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
def test_overview_selection_refresh_keeps_top_and_middle(completed_dashboard, width, height):
    window = completed_dashboard
    window.resize(width, height)
    window.open_summary(0)
    scroll = control(window, window.scrollers[0])
    viewport = scroll.property('contentItem')
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and (viewport.property('contentY') <= 0 or scroll.property('revealTarget') is not None):
        QTest.qWait(10)
    assert viewport.property('contentY') > 0 and scroll.property('revealTarget') is None
    extent = viewport.property('contentHeight') - viewport.height()
    assert extent > 200
    initial_chart_height = window.source_bars.state['height']
    base_snapshot = copy.deepcopy(window.snapshot)
    # Explicit selection reveals the detail; later refreshes must leave the
    # position chosen by the reader alone, even while that detail stays open.
    for position in (0, extent / 2):
        window.receive(copy.deepcopy(base_snapshot))
        QTest.qWait(60)
        window.scrollers[0].verticalPosition.setValue(position)
        QTest.qWait(30)
        before = viewport.property('contentY')
        for refresh in range(4):
            snapshot = copy.deepcopy(window.snapshot)
            snapshot['ts'] += 1
            new_source = copy.deepcopy(snapshot['sessions'][-1])
            new_source['id'] = f"added-{len(snapshot['sessions'])}"
            new_source['cwd'] = f"V:/engineering/added-{len(snapshot['sessions'])}"
            new_source['title'] = f"추가된 작업 {len(snapshot['sessions'])}"
            for row in new_source['history']:
                row['key'] = 'call-' + new_source['id']
                row['turn'] = 'turn-' + new_source['id']
            snapshot['sessions'].append(new_source)
            window.receive(snapshot)
            QTest.qWait(60)
            assert viewport.property('contentY') == pytest.approx(before, abs=1)
            assert scroll.property('revealTarget') is None
    assert window.source_bars.state['height'] > initial_chart_height
    assert not window.qml_errors


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
    for mime_type in clipboard.mimeData().formats():
        previous.setData(mime_type, clipboard.mimeData().data(mime_type))
    try:
        assert QMetaObject.invokeMethod(editors[-1], 'copy')
        assert clipboard.text() == long_text
    finally:
        clipboard.setMimeData(previous)
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


def test_project_popup_expands_and_wraps_full_names():
    app = QApplication.instance() or QApplication([])
    group = Group()
    layout = Column(group)
    choice = Choice()
    choice.setFixedWidth(220)
    long_name = '냉매 회로 성능 분석과 구김 평가 프로젝트 - ' * 6 + '최종 식별 이름'
    choice.addItem('모든 프로젝트', '')
    choice.addItem(long_name, 'long-project')
    choice.setItemData(1, 'D:/engineering/실제프로젝트', Qt.ToolTipRole)
    layout.addWidget(choice)
    layout.addStretch()
    host = mount(group, 800, 600)
    try:
        combo = control(host, choice)
        click(host, combo)
        popup = combo.findChild(QObject, 'choice-popup')
        QTest.qWait(60)
        assert popup.property('visible')
        assert choice.state['width'] < popup.property('width') <= 560
        option_text = next(item for item in walk(popup.property('contentItem'))
                           if item.property('text') == long_name and item.metaObject().indexOfProperty('contentHeight') >= 0)
        assert option_text.property('lineCount') > 1
        assert not option_text.property('truncated')
        assert option_text.height() >= option_text.property('contentHeight')
        combo.forceActiveFocus()
        QTest.keyClick(host.quick, Qt.Key_Down)
        QTest.keyClick(host.quick, Qt.Key_Return)
        QTest.qWait(30)
        assert choice.currentData() == 'long-project'
        assert combo.property('displayText') == long_name
        assert not host.qml_errors
    finally:
        dispose(host)
