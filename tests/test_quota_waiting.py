"""Visible operation feedback and chart reuse at normal and compact widths."""
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from cachemonitor.presentation import Scroll
from cachemonitor.quick_qa import click,dispose,mount,render_plot,walk
from cachemonitor.quota_panel import QuotaPanel
from cachemonitor.quota_worker import QuotaComputation
from test_quota_page import quota_page
from test_quota_query import QueryWorker,query_report
from test_quota_value_history import sample_report


@pytest.mark.parametrize('width',(1000,520))
def test_first_report_collection_empty_pause_and_failure_are_distinct(tmp_path,width):
    from cachemonitor.quick_qa import control
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'states.ini'),QSettings.IniFormat))
    scroll=Scroll();scroll.put(fillViewport=True);scroll.setWidget(panel)
    host=mount(scroll,width,680)
    retries=[];panel.retry_requested.connect(lambda:retries.append(True))
    try:
        loading=control(host,panel.initial_loading)
        assert loading.isVisible() and loading.width()>0 and loading.height()>0
        assert not panel.history_waiting.isVisible() and not panel.clear_cycle_button.isVisible()
        panel.set_collection_phase('preparing')
        panel.receive(dict(report=dict(cycles=[],index_loading=True),report_pending=False))
        assert panel.initial_loading.isVisible() and panel._activity_timer.isActive()
        panel.set_collection_phase('ready')
        assert panel.history_waiting.isVisible() and not panel.initial_loading.isVisible()
        assert not panel._activity_timer.isActive() and not panel.state_retry.isVisible()
        panel.set_monitoring_paused(True)
        assert panel.history_waiting.isVisible() and not panel.initial_loading.isVisible()
        assert not panel.state_retry.isVisible()
        panel.set_monitoring_paused(False)
        panel.receive(dict(report=dict(cycles=[],error=True),report_pending=False,issue='Read failed'))
        assert panel.state_retry.isVisible() and not panel.initial_loading.isVisible()
        click(host,render_plot(host,panel.state_retry))
        assert retries==[True] and not host.qml_errors
    finally:dispose(host)


def test_same_scope_report_refresh_and_failure_preserve_displayed_values(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'retained.ini'),QSettings.IniFormat))
    panel.receive(dict(report=sample_report()))
    rows=panel.history.rows
    values={key:node.text() for key,node in panel.summary_values.items()}
    panel.receive(dict(report_pending=True))
    assert panel.history.rows is rows and not panel.initial_loading.isVisible()
    assert panel.activity_note.isVisible()
    panel.receive(dict(report_pending=False,issue='Refresh failed'))
    assert panel.history.rows is rows and panel.activity_note.isVisible()
    assert {key:node.text() for key,node in panel.summary_values.items()}==values
    assert panel.state_retry.isVisible() and not panel._activity_timer.isActive()
    panel.deleteLater();app.processEvents()


def test_paused_report_reads_and_failures_are_distinct_from_paused_collection(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'paused-read.ini'),QSettings.IniFormat))
    panel.set_monitoring_paused(True)
    panel.receive(dict(report_pending=True))
    assert panel.initial_loading.isVisible() and not panel.history_waiting.isVisible()
    panel.receive(dict(report=dict(cycles=[],error=True),report_pending=False,issue='Read failed'))
    assert not panel.initial_loading.isVisible() and panel.state_retry.isVisible()
    panel.receive(dict(report=dict(cycles=[]),report_pending=False))
    assert panel.history_waiting.isVisible() and not panel.state_retry.isVisible()
    panel.deleteLater();app.processEvents()


def test_hidden_report_timer_suspends_and_refresh_labels_follow_language(tmp_path):
    from cachemonitor.i18n import set_language
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'hidden-status.ini'),QSettings.IniFormat))
    try:
        panel.receive(dict(report_pending=True))
        started=panel._loading_since[panel.initial_loading]
        panel.set_presentation_active(False)
        assert not panel._activity_timer.isActive()
        panel.set_presentation_active(True)
        assert panel._activity_timer.isActive() and panel._loading_since[panel.initial_loading]==started
        set_language('en')
        panel.receive(dict(report=sample_report(),report_pending=True))
        assert panel.activity_note.isVisible()
        assert not any('\uac00'<=char<='\ud7a3' for char in panel.activity_note.text())
    finally:
        set_language('ko');panel.deleteLater();app.processEvents()


def test_hidden_conversion_does_not_schedule_queries_on_refresh(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'hidden.ini'),QSettings.IniFormat))
    worker=QueryWorker();report=query_report(3)
    panel.receive(dict(home='h',report={**report,'worker_revision':1,'_worker':worker}))
    panel.model_choice.setCurrentIndex(panel.model_choice.findData('test'))
    worker.requests.clear()
    panel.receive(dict(home='h',report={**report,'worker_revision':2,'_worker':worker}))
    assert panel.conversion not in panel.layout()._nodes
    assert not worker.requests
    panel.deleteLater();app.processEvents()


@pytest.mark.parametrize('width',(1000,520))
def test_delete_reports_activity_and_is_scoped_to_home(quota_page,width):
    app,panel,scroll=quota_page
    host=mount(scroll,width,680)
    try:
        panel.cycle_choice.setCurrentIndex(2)
        click(host,render_plot(host,panel.clear_cycle_button))
        dialog=panel.clear_cycle_dialog
        click(dialog.host,render_plot(dialog.host,dialog.confirm))
        assert panel.cycle_loading.isVisible()
        assert not panel.clear_cycle_button.isEnabled()
        assert not panel.history_loading.isVisible()
        assert render_plot(host,panel.cycle_loading).width()<render_plot(host,panel.cycle_choice).width()
        panel.receive_activity(dict(home='h',operation_progress=dict(stage='reading')))
        assert '저장 완료' in panel.cycle_loading._state['tooltip']
        prior=panel.report
        panel.set_homes(['h','other']);panel.home_choice.setCurrentIndex(1)
        panel.receive(dict(home='other',report={**prior,'home':'other'}))
        assert not panel._cycle_clear_pending
        panel.receive(dict(home='h',exclusion_result=dict(operation='clear_cycle',ok=True,deleted=True)))
        panel.home_choice.setCurrentIndex(0)
        panel.receive(dict(home='h',report=prior))
        assert not panel._cycle_clear_pending
        assert panel.clear_cycle_button.isEnabled()
    finally:dispose(host)


@pytest.mark.parametrize('width',(1000,520))
def test_chart_wait_preserves_space_and_return_uses_displayed_cache(tmp_path,width):
    app=QApplication.instance() or QApplication([])
    engine=QuotaComputation();worker=QueryWorker()
    compact=engine._prepare(sample_report())
    panel=QuotaPanel(QSettings(str(tmp_path/'charts.ini'),QSettings.IniFormat))
    panel.receive(dict(report={**compact,'_worker':worker}))
    scroll=Scroll();scroll.put(fillViewport=True);scroll.setWidget(panel)
    host=mount(scroll,width,680)
    try:
        original=panel.cycle_choice.currentData()
        before=render_plot(host,panel.history).height()
        panel.cycle_choice.setCurrentIndex(panel.cycle_choice.findData(100))
        loading=render_plot(host,panel.history_loading)
        assert loading.height()>=before
        assert not any(item.objectName()=='loading-track' for item in walk(loading))
        assert panel.cycle_loading.isVisible()
        assert panel.clear_cycle_button.text()=='선택 주기 삭제'
        request=worker.requests[-1]
        response=engine.execute(dict(request,id=2))
        worker.result.emit(response)
        assert panel.history.isVisible() and not panel.cycle_loading.isVisible()
        count=len(worker.requests)
        panel.cycle_choice.setCurrentIndex(panel.cycle_choice.findData(original))
        assert len(worker.requests)==count
        assert panel.history.isVisible() and not panel.history_loading.isVisible()
    finally:dispose(host);engine.close()
