import os
from pathlib import Path
import re
import time

import pytest
from PySide6.QtCore import QObject, QSettings, Signal, Qt, QPointF
from PySide6.QtWidgets import QApplication

from cachemonitor.mismatch_page import MismatchPage, TEXT_RUNS, cell_value, model_values
from cachemonitor.quick_qa import click, click_row, control, walk, dispose
from tests.test_ui import wait_for_rendered


def saved_row(index=0, **values):
    return dict(home='archive-home', sid='deleted-session', key=f'private-response-{index}',
                identity=('archive-home', f'private-response-{index}'), ts=1700000000-index,
                requested_model='requested-model', response_model='observed-model',
                effort='high', requested_effort='high', requested_effort_source='turn_context',
                service_tier='Fast', request_mode_source='turn_context',
                response_effort=None, response_effort_confirmed=False,
                response_service_tier=None, response_tier_confirmed=False,
                input=1000, output=100, reasoning=40, cached=750, written=0, rate=75.,
                duration=2., output_speed=50., response_status='completed', transport='HTTP/SSE',
                transport_source='response_id', first_captured_at=1700000100, changed_at=1700000100,
                correction_at=None, correction_reason=None) | values


class ArchiveFixture:
    def __init__(self, rows):
        self.rows = rows
        self.windows = []

    def window(self, *, start, size):
        self.windows.append((start, size))
        return dict(revision=1, window=dict(start=start, total=len(self.rows), rows=self.rows[start:start+size]))

    def record(self, identity):
        return dict(revision=1, row=next((row for row in self.rows if row['identity'] == identity), None))


class QueryFixture(QObject):
    mismatch_page = Signal(object)
    mismatch_record = Signal(object)

    def __init__(self):
        super().__init__()
        self.pages = []
        self.records = []

    def request_mismatch_page(self, request_id, *, start, size):
        self.pages.append(dict(id=request_id, start=start, size=size))

    def request_mismatch_record(self, request_id, identity):
        self.records.append(dict(id=request_id, identity=identity))


@pytest.fixture
def page():
    app = QApplication.instance() or QApplication([])
    node = MismatchPage()
    node.set_active(True)
    try:
        yield node
    finally:
        node.deleteLater()
        app.processEvents()


def test_response_effort_and_mode_require_their_own_confirmation(page):
    row = saved_row(response_effort='low', response_service_tier='default')
    page.table.set_window(dict(start=0, total=1, rows=[row]), cell_value, 'saved')
    assert model_values(row) == ('requested-model', 'high', 'Fast')
    assert model_values(row, True) == ('observed-model', '미확인', '미확인')
    model = page.table.model()
    assert model.data(model.index(0, 2), Qt.DisplayRole) == 'observed-model / 미확인 / 미확인'
    assert model.data(model.index(0, 1), TEXT_RUNS) == []
    assert model.data(model.index(0, 2), TEXT_RUNS) == [dict(text='observed-model', color='accent'), dict(text=' / 미확인 / 미확인')]
    row = dict(row, response_effort_confirmed=True, response_tier_confirmed=True)
    assert model_values(row, True) == ('observed-model', 'low', 'Standard')
    assert model_values(dict(row, response_effort_conflict=True), True)[1] == '미확인'
    assert model_values(dict(row, requested_effort_conflict=True))[1] == '미확인'


def test_archive_windows_reject_stale_results_and_preserve_canonical_selection(page):
    worker = QueryFixture()
    page.set_worker(worker)
    page.table.first_visible = 210
    page.table.last_visible = 230
    page.refresh()
    first = worker.pages[-1]
    page.request_page()
    latest = worker.pages[-1]
    assert first['start'] == latest['start'] == 178
    assert 128 <= latest['size'] <= 512
    rows = [saved_row(i) for i in range(178, 306)]
    worker.mismatch_page.emit(dict(id=first['id'], revision=1, window=dict(start=178, total=600, rows=rows)))
    assert page.table.rowCount() == 0
    worker.mismatch_page.emit(dict(id=latest['id'], revision=1, window=dict(start=178, total=600, rows=rows)))
    assert page.table.rowCount() == 600
    assert len(page.table.model().rows.values) == 128
    assert page.table.model().data(page.table.model().index(500, 0), Qt.UserRole+6)
    page.table.selectRow(215)
    moved = [dict(row, sid='new-owner') for row in rows]
    page.request_page()
    worker.mismatch_page.emit(dict(id=worker.pages[-1]['id'], revision=2, window=dict(start=178, total=600, rows=[saved_row(900)]+moved[:-1])))
    assert page.table.currentRow() == 216
    assert page.table.row_key(page.table.model().rows[216]) == ('archive-home', 'private-response-215')


def test_archive_query_error_survives_healthy_capture_and_detail_uses_saved_metadata(page):
    worker = QueryFixture()
    page.set_worker(worker)
    page.refresh()
    failed = worker.pages[-1]['id']
    worker.mismatch_page.emit(dict(id=failed, error='reader unavailable'))
    page.receive_health(dict(revision=1, status='ready', error='', pending=0, gap=False))
    assert page.error.isVisible() and 'reader unavailable' in page.error.text()
    rows = [saved_row(), saved_row(1, response_effort='low', response_effort_confirmed=False)]
    page.request_page()
    worker.mismatch_page.emit(dict(id=worker.pages[-1]['id'], revision=1, window=dict(start=0, total=2, rows=rows)))
    assert not page.error.isVisible()
    page.open_record(0)
    old = worker.records[-1]
    page.open_record(1)
    latest = worker.records[-1]
    assert latest['identity'] == ('archive-home', 'private-response-1')
    worker.mismatch_record.emit(dict(id=old['id'], row=rows[0], revision=1))
    assert page.saved_record is None
    worker.mismatch_record.emit(dict(id=latest['id'], row=rows[1], revision=1))
    assert page.saved_record == rows[1] and page.detail.isVisible()
    text = page.fields.text()
    assert 'observed-model / 미확인 / 미확인' in text
    assert 'HTTP/SSE' in text
    assert 'deleted-session' not in text and 'private-response-1' not in text
    page.receive_health(dict(revision=1, status='capture_gap', error='disk full', gap=True))
    assert page.error.isVisible() and 'disk full' in page.error.text()
    page.close_detail()
    worker.mismatch_record.emit(dict(id=latest['id'], row=rows[1], revision=1))
    assert not page.detail.isVisible()


@pytest.mark.integration
@pytest.mark.essential
@pytest.mark.parametrize('width,height,name', [(1800, 1000, 'regular'), (1000, 680, 'compact')])
def test_mismatch_navigation_renders_saved_detail_and_full_models(tmp_path, width, height, name):
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.fonts import load_bundled_fonts
    app = QApplication.instance() or QApplication([])
    load_bundled_fonts()
    previous = app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration', True)
    settings = QSettings(str(tmp_path/'mismatch.ini'), QSettings.IniFormat)
    settings.setValue('ui/theme', 'light')
    response = 'gpt-6-luna-with-a-long-observed-snapshot-<value>&-suffix'
    rows = [saved_row(0, requested_model='gpt-6.1-sol', response_model=response),
            saved_row(1, requested_model='gpt-6.1-sol', response_model='gpt-6-sol',
                      response_effort='low', response_effort_confirmed=True,
                      response_effort_source='wire', response_service_tier='default', response_tier_confirmed=True),
            saved_row(2, requested_model='gpt-6-sol', response_model='gpt-6-luna',
                      correction_at=1700000200, correction_reason='model_agrees')]
    reader = ArchiveFixture(rows)
    snapshot = dict(ts=time.time(), sessions=[], homes=[], errors=[], unassigned=[], index={'loading': False})
    evidence = Path(os.environ.get('CODEXON_MISMATCH_EVIDENCE', str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    window = Dashboard([], start_worker=False, live_limits=False, settings=settings,
                       static_snapshot=snapshot, mismatch_reader=reader, collection_autostart=False)
    try:
        window.resize(width, height)
        window.show()
        navigation = control(window, window.nav)
        item = next(item for item in walk(navigation) if item.property('index') == window.navigation_pages.index(7))
        click(window, item)
        assert window.current_page == 7
        page = window.mismatch_page
        table = control(window, page.table)
        wait_for_rendered(lambda: any(item.objectName() == 'cell-label' and item.isVisible() for item in walk(table)))
        assert page.table.rowCount() == 3
        assert reader.windows and all(size <= 512 for _, size in reader.windows)
        position = table.mapToScene(QPointF(0, 0))
        assert position.y() >= 0 and position.y()+table.height() <= window.quick.height()+1
        footer = next(item for item in walk(table) if item.objectName() == 'table-footer-text')
        assert re.search(r'\(UTC[+-]\d{2}:\d{2}\)$', footer.property('text'))
        assert response in page.table.cellDetails(0, 2)
        assert window.quick.grabFramebuffer().save(str(evidence/f'mismatch-{name}-table.png'))
        click_row(window, page.table, 0, 2)
        wait_for_rendered(lambda: page.detail.isVisible())
        assert response in page.fields.text()
        assert 'private-response-0' not in page.fields.text()
        detail = control(window, page.fields)
        rendered = [item.property('text') for item in walk(detail) if item.objectName() == 'record-field-value']
        assert any(response in str(value) for value in rendered)
        assert window.quick.grabFramebuffer().save(str(evidence/f'mismatch-{name}-detail.png'))
        assert not window.qml_errors
    finally:
        window.quitting=True
        window.tick.stop()
        window.tray.hide()
        window.observer_panel.stop()
        dispose(window)
        app.setProperty('cachemonitorDisableShellIntegration', previous)


def test_saved_gap_is_visible_before_prepared_health_arrives(page):
    worker=QueryFixture()
    page.set_worker(worker)
    page.request_page()
    page.page_ready(dict(id=page.page_request,revision=1,capture_gap=True,
                         window=dict(start=0,total=0,rows=[])))
    assert page.error.isVisible()
    assert page.health['gap']
