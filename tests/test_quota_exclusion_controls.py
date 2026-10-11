"""Exercise the compact editor and restore path through the rendered page."""
from copy import deepcopy
from time import monotonic

import pytest
from PySide6.QtCore import QSettings, Qt, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.presentation import Scroll
from cachemonitor.quota_panel import QuotaPanel
from cachemonitor.quick_qa import click, control, dispose, mount, render_plot, walk
from cachemonitor.theme import shared_theme
from test_quota_exclusions import sample_report, exclusion


def wait_until(app, condition):
    deadline = monotonic()+10
    while not condition() and monotonic()<deadline:
        app.processEvents()
    assert condition()


def test_slow_preview_keeps_ui_responsive_and_applies_only_latest_range(monkeypatch):
    from functools import partial
    import multiprocessing as mp
    from PySide6.QtCore import QTimer
    from cachemonitor import quota_worker
    from cachemonitor.quota_exclusion_controls import QuotaExclusions
    from cachemonitor.quota_view import prepare_quota_view
    from test_quota_worker import delayed_preview_process
    app=QApplication.instance() or QApplication([])
    report=sample_report();report['view']=prepare_quota_view(report)
    editor=QuotaExclusions()
    editor.set_context(report,report['view']['periods'][0])
    context=mp.get_context('spawn')
    entered,release=context.Event(),context.Event()
    calls=context.Queue();ticks=[]
    original=quota_worker.QuotaWorker
    monkeypatch.setattr(quota_worker,'QuotaWorker',lambda:original(process_target=partial(
        delayed_preview_process,entered=entered,release=release,calls=calls)))
    try:
        editor.select_range(100,110)
        wait_until(app,entered.is_set)
        QTimer.singleShot(0,lambda:ticks.append(True))
        editor.select_range(110,120)
        editor.select_range(110,130)
        editor.reason.setText('keep this reason')
        wait_until(app,lambda:bool(ticks))
        assert not editor.apply.isEnabled() and editor._preview is None
        release.set()
        wait_until(app,lambda:editor._preview is not None)
        assert [calls.get(timeout=2),calls.get(timeout=2)]==[(100,110),(110,130)]
        assert editor._preview['removed']['cost']==300
        assert editor.reason.text()=='keep this reason'
        assert editor.apply.isEnabled()
        editor.cancel_edit()
        assert editor._preview_request is None
    finally:
        release.set()
        wait_until(app,lambda:editor._preview_task is None)
        if editor._owned_worker:editor._owned_worker.close()
        calls.close();calls.join_thread()
        editor.deleteLater()


@pytest.mark.integration
@pytest.mark.parametrize('width,dark', [(520, False), (1120, True)])
def test_compact_exclusion_reason_restore_and_all_cycles(tmp_path, width, dark):
    app = QApplication.instance() or QApplication([])
    from cachemonitor.fonts import load_bundled_fonts
    load_bundled_fonts()
    shared_theme().configure('dark' if dark else 'light')
    panel = QuotaPanel(QSettings(str(tmp_path/'panel.ini'), QSettings.IniFormat))
    report = sample_report()
    panel.receive({'home':'h', 'report':report})
    scroll = Scroll(); scroll.put(fillViewport=True); scroll.setWidget(panel)
    host = mount(scroll, width, 1000)
    editor = panel.exclusions
    requests = []; restores = []
    panel.exclusion_requested.connect(requests.append)
    panel.exclusion_restore_requested.connect(restores.append)
    try:
        assert editor.toggle.isEnabled() and panel.dialog.host is None
        plot = render_plot(host, panel.history)
        # Direct chart events keep this test independent from hover behavior.
        chart = panel.history
        y = chart.box.center().y()
        chart.beginRangeSelection(chart.x_at(1), y)
        chart.selectionRelease(chart.x_at(3), y)
        assert editor.start.currentData() == 110 and editor.end.currentData() == 130
        wait_until(app, lambda: editor._preview is not None)
        assert editor._preview['removed']['cost'] == 300
        assert editor.impact_details.content.isVisible()
        modal=panel.dialog.host
        assert modal is not None
        render_plot(modal, editor.reason)
        field = control(modal, editor.reason)
        field.forceActiveFocus(); QTest.keyClicks(modal.quick, 'benchmark run')
        assert editor.reason.text() == 'benchmark run'
        assert field.property('maximumLength') == 200
        for node in (editor.start, editor.end, editor.reason, editor.apply, editor.cancel):
            item = control(modal, node)
            left = item.mapToScene(QPointF(0, 0)).x()
            assert left >= 0 and left+item.width() <= modal.quick.width()+1
        render_plot(modal, editor)
        assert modal.grab().save(str(tmp_path/f'quota-editor-{width}.png'))
        click(modal, render_plot(modal, editor.apply))
        assert requests == [dict(start=110, end=130, reason='benchmark run')]
        assert not editor.apply.isEnabled()
        changed = deepcopy(report)
        changed['exclusions'] = [{**exclusion(), 'reason':'benchmark run'}]
        panel.receive({'home':'h', 'report':changed,
                       'exclusion_result':dict(operation='exclude', ok=True, id='110:130')})
        wait_until(app,lambda:panel.dialog.host is None)
        assert panel.lifetime_statistics['cost'] == 12
        assert [r['remaining'] for r in chart.rows] == [100,99,94,89,88]
        click(host,render_plot(host,panel.manage_button))
        modal=panel.dialog.host
        render_plot(modal,editor.history)
        assert editor.history.isVisible()
        panel.cycle_choice.setCurrentIndex(0)
        assert editor.history.isVisible() and not editor.toggle.isEnabled()
        panel.cycle_choice.setCurrentIndex(1)
        assert editor.history.isVisible()
        reasons = [n.text() for n in editor.history_layout._nodes[0]._nodes[0]._nodes if n.kind == 'text']
        assert 'benchmark run' in reasons
        assert modal.grab().save(str(tmp_path/f'quota-excluded-{width}.png'))
        click(modal, render_plot(modal, editor._restores[0]))
        assert restores == ['110:130']
        panel.receive({'home':'h', 'report':report,
                       'exclusion_result':dict(operation='restore', ok=True, id='110:130')})
        wait_until(app,lambda:panel.dialog.host is None)
        assert panel.lifetime_statistics['cost'] == 312
        assert not editor.history.isVisible()
        panel.cycle_choice.setCurrentIndex(0)
        assert not editor.toggle.isEnabled()
        panel.cycle_choice.setCurrentIndex(1)
        assert editor.toggle.isEnabled()
        panel.render(automatic=True)
        assert editor.toggle.isVisible() and editor.toggle.isEnabled()
        assert not host.qml_errors
    finally:
        if panel.dialog.host:panel.dialog.reject();app.processEvents()
        dispose(host); shared_theme().configure('light')


def test_failed_save_retains_range_and_reason_until_retry(tmp_path):
    app = QApplication.instance() or QApplication([])
    panel = QuotaPanel(QSettings(str(tmp_path/'panel.ini'), QSettings.IniFormat))
    panel.receive({'home':'h', 'report':sample_report()})
    editor = panel.exclusions
    editor.select_range(110, 130); editor.reason.setText('테스트 실행')
    wait_until(app, lambda: editor._preview is not None)
    editor.submit()
    panel.receive({'home':'h', 'report':sample_report(),
                   'exclusion_result':dict(operation='exclude', ok=False, error='저장 실패')})
    wait_until(app, lambda: editor._preview is not None)
    assert editor.isVisible() and editor.apply.isEnabled()
    assert editor.reason.text() == '테스트 실행'
    assert (editor.start.currentData(), editor.end.currentData()) == (110, 130)
    assert editor.message.text() == '저장 실패'


def test_full_exclusion_is_a_zero_result_and_reason_is_never_translated(tmp_path):
    from cachemonitor.i18n import set_language
    app = QApplication.instance() or QApplication([])
    panel = QuotaPanel(QSettings(str(tmp_path/'panel.ini'), QSettings.IniFormat))
    report = sample_report()
    report['exclusions'] = [exclusion(100, 140)]
    panel.receive({'home':'h', 'report':report})
    assert not panel.history_waiting.isVisible()
    assert panel.cycle_value.isVisible() and panel.conversion.isVisible()
    assert panel.lifetime_statistics['cost'] == panel.lifetime_statistics['delta'] == 0
    assert len(panel.exclusions._restores) == 1
    try:
        set_language('en')
        panel.exclusions.reason.setText('취소')
        assert panel.exclusions.reason.textValue == '취소'
    finally:
        set_language('ko')
