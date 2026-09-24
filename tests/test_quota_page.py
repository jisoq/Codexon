"""Exercise the real Quick page, chart selection and its single detail entry."""
import time

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.presentation import Scroll
from cachemonitor.quota_panel import QuotaPanel, model_cost_intervals
from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor.quick_qa import click, control, dispose, mount, render_plot, click_row, walk
from cachemonitor.theme import shared_theme
from test_quota_tracking_integration import activity
from cachemonitor import quota_tracking_store as tracking


@pytest.fixture
def quota_page(tmp_path):
    app=QApplication.instance() or QApplication([])
    from cachemonitor.fonts import load_bundled_fonts
    load_bundled_fonts()
    shared_theme().configure('light')
    now=time.time()
    ledger=QuotaLedger(tmp_path/'page.sqlite')
    tracking.enable(ledger.db,'h',now-310)
    history=[(-300,80),(-270,78),(-240,76),(-210,100),(-180,98),(-150,96),(-120,100),(-90,99),(-60,98)]
    for offset,remaining in history:
        at=now+offset
        deadline=now-120 if offset<-120 else now+604680
        quota=dict(source='live',account='a',plan_type='pro',bucket='codex',requested_at=at,
            observed_at=at+.1,windows={
            'weekly':dict(used_percent=100-remaining,window_minutes=10080,resets_at=deadline),
            'five_hour':dict(used_percent=20-offset/300,window_minutes=300,resets_at=now+3600)})
        ledger.observe('h',quota);tracking.observe(ledger.db,'h',quota)
    activity(ledger,now,{},[now-280,now-190,now-80])
    quota={**quota,'observed_at':now}
    panel=QuotaPanel(QSettings(str(tmp_path/'panel.ini'),QSettings.IniFormat))
    panel.receive({'quota':quota,'report':ledger.report('h',now)})
    scroll=Scroll();scroll.put(fillViewport=True);scroll.setWidget(panel)
    yield app,panel,scroll
    ledger.close()


@pytest.mark.parametrize('width,height,dark', [(1120,1000,False),(520,900,False),(1120,1000,True)])
def test_responsive_page_chart_and_single_details_path(quota_page,tmp_path,width,height,dark):
    app,panel,scroll=quota_page
    if dark:shared_theme().configure('dark')
    host=mount(scroll,width,height)
    try:
        plot=render_plot(host,panel.history)
        assert panel.history.axis[0]>0 and panel.history.axis[1]==100
        assert panel.basis.text()=='9%p'
        assert '정기 초기화' in panel.cycle_choice.currentText()
        assert all(r['local_observed'] for r in panel.history.rows)
        assert panel.cycle_choice.count()==4
        assert not panel.details.content.isVisible()
        # Select the exact observation; between records the chart now reports
        # the step value at the cursor, rather than a future nearest record.
        row=panel.history.rows[-1]
        plot.activateAt(panel.history.x_at(len(panel.history.rows)-1),panel.history.box.center().y())
        assert plot.detail['title'] in row['label']
        plot.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Right);QTest.qWait(30)
        assert plot.detail['title'] in panel.history.rows[0]['label']
        for node in (panel.basis,panel.cost_value,panel.result):
            item=control(host,node)
            texts=[child for child in walk(item) if child.metaObject().indexOfProperty('truncated')>=0 and child.isVisible()]
            assert texts and all(not t.property('truncated') for t in texts)
        assert host.grab().save(str(tmp_path/f'quota-{width}-{"dark" if dark else "light"}.png'))
        scroll.ensureWidgetVisible(panel.details.toggle);QTest.qWait(80)
        click(host,control(host,panel.details.toggle))
        assert panel.details.content.isVisible()
        click_row(host,panel.intervals,1)
        assert panel.selected_id==panel.intervals.model().rows[1]['id']
        assert '정기 초기화로 종료' in panel.interval_detail.text()
        assert panel.table.rowCount()>0
        scroll.ensureWidgetVisible(panel.interval_detail);QTest.qWait(80)
        assert host.grab().save(str(tmp_path/f'quota-detail-{width}-{"dark" if dark else "light"}.png'))
        before=panel.selected_id
        panel.render(automatic=True);QTest.qWait(40)
        assert panel.selected_id==before and panel.details.content.isVisible()
        # Change the chart through its rendered control.
        scroll.ensureWidgetVisible(panel.window);QTest.qWait(80)
        choice=control(host,panel.window);choice.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Down);QTest.qWait(40)
        assert panel.window.currentData()=='five_hour'
        assert not any(r['markers'] for r in panel.history.rows)
        assert panel.history.axis[1]<100
        assert [key for key,_,_ in panel.history.curves()]==['remaining']
        assert len(panel.history.detail_for(0)['items'])==1
        assert panel.basis.text()=='9%p'
        assert not host.qml_errors
    finally:
        dispose(host);shared_theme().configure('light')


def test_model_filter_combines_request_modes_without_assigning_account_usage():
    intervals=[dict(id='one',cost=10,delta=4,models=[
        dict(model='alpha',service_tier='Standard',calls=2,priced=2,cost=3,separate=False),
        dict(model='alpha',service_tier='Fast',calls=1,priced=1,cost=2,separate=False),
        dict(model='beta',service_tier='Standard',calls=1,priced=1,cost=5,separate=False)]),
        dict(id='two',cost=7,delta=2,models=[
            dict(model='alpha',service_tier='Standard',calls=1,priced=1,cost=7,separate=True)])]
    selected=model_cost_intervals(intervals,'alpha')
    assert len(selected)==1
    assert selected[0]['model_cost']==5
    assert selected[0]['model_calls']==3
    assert selected[0]['model_share']==50
    assert selected[0]['delta']==4
    assert intervals[0]['cost']==10


def test_cycle_selector_changes_graph_preserves_lifetime_and_selection_on_refresh(quota_page,tmp_path):
    app,panel,scroll=quota_page
    host=mount(scroll,1120,1000)
    try:
        assert panel.history.money and panel.cycle_choice.count()==4
        lifetime=panel.result.text()
        scroll.ensureWidgetVisible(panel.cycle_choice);QTest.qWait(40)
        choice=control(host,panel.cycle_choice);choice.forceActiveFocus()
        QTest.keyClick(host.quick,Qt.Key_Up);QTest.qWait(40)
        assert panel.cycle_choice.currentIndex()==2
        period=panel._view['periods'][1]
        assert panel.history.rows is period['series']['rows']
        assert panel.history.rows[0]['reset_kind']=='arbitrary_reset'
        plot=render_plot(host,panel.history)
        plot.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Home)
        assert panel.history.cursor==0 and '임의 초기화' in plot.detail['note']
        selected=panel.cycle_choice.currentData()
        panel.render(automatic=True);QTest.qWait(40)
        assert panel.cycle_choice.currentData()==selected and panel.history.cursor==0
        assert panel.result.text()==lifetime
        panel.set_history_period(time.time(),None)
        assert panel.result.text()==lifetime and panel.history.rows is period['series']['rows']
        # The all-cycles entry keeps the fourth USD series while the percent
        # axis changes to cumulative consumption. Check the real Quick scene.
        panel.cycle_choice.setCurrentIndex(0)
        plot=render_plot(host,panel.history)
        assert panel.history.series is panel._view['overall']
        assert panel.history.series['cumulative']
        assert len(panel.history.curves())==4
        assert panel.remaining_legend.text()=='━ 누적 소모량 · %p'
        # This fixture starts mid-plateau or skips percent boundaries, so it
        # must stay unpriced even in the cumulative view.
        assert all(value is None for value in panel.history.series['completed_costs'])
        index=0
        plot.activateAt(panel.history.x_at(index),panel.history.box.center().y())
        QTest.qWait(30)
        from cachemonitor.pricing import usd
        assert plot.detail['items'][0]['label']=='누적 소모량'
        assert plot.detail['items'][3]['value']==usd(panel.history.series['completed_costs'][index])
        assert host.grab().save(str(tmp_path/'quota-all-cycles.png'))
        assert not host.qml_errors
    finally:dispose(host)
