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
