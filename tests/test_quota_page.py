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
        remaining=[row['remaining'] for row in panel.history.rows]
        assert 0<panel.history.axis[0]<min(remaining)
        assert panel.history.axis[1]>max(remaining)
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


@pytest.mark.parametrize('width,language,dark',[(1120,'ko',False),(520,'ko',False),(1120,'en',True),(520,'en',True)])
def test_current_allowance_and_compact_reset_summary(tmp_path,width,language,dark):
    from PySide6.QtCore import QPointF
    from cachemonitor.i18n import set_language
    from cachemonitor.quota_live import normalize_limits
    app=QApplication.instance() or QApplication([])
    set_language(language);shared_theme().configure('dark' if dark else 'light')
    now=time.time()
    quota=normalize_limits({'rateLimits':{'planType':'pro','secondary':None,
        'primary':{'usedPercent':36,'windowDurationMins':10080,'resetsAt':now+600},
        'credits':{'balance':'62500.0000000000','hasCredits':True,'unlimited':False}},
        'rateLimitResetCredits':{'availableCount':12,'credits':[
            {'status':'available','resetType':'codexRateLimits','grantedAt':now-86400,'expiresAt':now+i*86400}
            for i in range(12,0,-1)]}},now,'synthetic')
    panel=QuotaPanel(QSettings(str(tmp_path/'credits.ini'),QSettings.IniFormat))
    panel.receive({'quota':quota,'report':{'cycles':[]}})
    scroll=Scroll();scroll.put(fillViewport=True);scroll.setWidget(panel)
    host=mount(scroll,width,900)
    try:
        QTest.qWait(80)
        def position(node):return control(host,node).mapToScene(QPointF(0,0))
        assert position(panel.current['five_hour']['card']).y()<position(panel.current['weekly']['card']).y()
        assert position(panel.credit_card).y()>position(panel.current['weekly']['card']).y()
        assert position(panel.credit_card).y()<position(panel.reset_card).y()
        assert panel.current['five_hour']['value'].text()=='∞'
        assert not panel.current['five_hour']['bar'].isVisible()
        assert panel.credit_balance.text()=='62,500'
        assert panel.reset_count.text()==('12개' if language=='ko' else '12 resets')
        assert panel.reset_rows.isVisible()
        assert not panel.reset_extra.isVisible() and not panel.reset_grants.isChecked()
        assert panel.reset_more.text()==('+9개' if language=='ko' else '+9')
        assert not panel.credit_note.isVisible() and not panel.reset_note.isVisible()
        assert not any(item['granted'].isVisible() for item in panel.reset_items)
        assert not any(item['status'].isVisible() for item in panel.reset_items)
        from cachemonitor.quota_panel import reset_time
        from PySide6.QtCore import QTimeZone
        assert [item['expiry'].text() for item in panel.reset_items[:3]]==[
            reset_time(now+i*86400,QTimeZone.systemTimeZone()).toString('MM/dd HH:mm') for i in (1,2,3)]
        first_three=[control(host,item['card']) for item in panel.reset_items[:3]]
        assert max(item.mapToScene(QPointF(0,0)).y() for item in first_three)-min(item.mapToScene(QPointF(0,0)).y() for item in first_three)<2
        more=control(host,panel.reset_more)
        assert position(panel.reset_more).y()<position(panel.reset_items[0]['card']).y()+first_three[0].height()
        for item in panel.reset_items[:3]:
            label=control(host,item['expiry'])
            texts=[child for child in walk(label) if child.metaObject().indexOfProperty('contentWidth')>=0 and child.isVisible()]
            assert texts and all(t.property('contentWidth')<=t.width()+1 for t in texts)
        compact_height=control(host,panel.current_row).height()
        assert host.grab().save(str(tmp_path/f'allowance-{width}-{language}.png'))
        click(host,more)
        assert panel.reset_extra.isVisible()
        click(host,control(host,panel.reset_grants))
        assert all(item['granted'].isVisible() for item in panel.reset_items)
        panel.receive({'quota':quota});QTest.qWait(30)
        assert panel.reset_more.isChecked() and panel.reset_grants.isChecked()
        assert host.grab().save(str(tmp_path/f'resets-expanded-{width}-{language}.png'))
        click(host,control(host,panel.reset_more))
        click(host,control(host,panel.reset_grants))
        assert abs(control(host,panel.current_row).height()-compact_height)<2
        for count in (3,2,1):
            short=dict(quota,reset_credits=dict(available_count=count,
                credits=quota['reset_credits']['credits'][-count:]))
            panel.receive({'quota':short});QTest.qWait(20)
            assert not panel.reset_more.isVisible() and not panel.reset_extra.isVisible()
            assert sum(item['column'].isVisible() for item in panel.reset_items)==count
            assert abs(control(host,panel.current_row).height()-compact_height)<2
        panel.receive({'quota':dict(quota,observed_at=now-100)})
        assert panel.credit_balance.text()=='—'
        assert panel.current['five_hour']['value'].text()=='—'
        assert not panel.reset_rows.isVisible()
        assert not panel.reset_more.isChecked() and not panel.reset_grants.isVisible()
        assert not host.qml_errors
    finally:dispose(host);set_language('ko');shared_theme().configure('light')


def test_reset_dates_follow_system_timezone_on_refresh(tmp_path,monkeypatch):
    from datetime import datetime
    from PySide6.QtCore import QTimeZone
    from cachemonitor.quota_panel import reset_zone_label
    app=QApplication.instance() or QApplication([])
    now=time.time()
    stamp=datetime.fromisoformat('2026-10-23T05:23:00+09:00').timestamp()
    panel=QuotaPanel(QSettings(str(tmp_path/'reset-timezone.ini'),QSettings.IniFormat))
    panel.receive({'quota':dict(source='live',observed_at=now,reset_credits=dict(available_count=1,
        credits=[dict(status='available',expires_at=stamp,granted_at=stamp-86400)]))})
    for name,expected in [('Asia/Seoul','10/23 05:23'),('UTC','10/22 20:23'),('America/New_York','10/22 16:23')]:
        zone=QTimeZone(name.encode())
        monkeypatch.setattr(QTimeZone,'systemTimeZone',lambda z=zone:z)
        panel.refresh_status()
        assert panel.reset_items[0]['expiry'].text()==expected
        assert reset_zone_label(zone) in panel.reset_zone.text()
        assert panel.reset_zone.toolTip()==name


@pytest.mark.parametrize('width,dark',[(520,False),(1120,True)])
def test_waiting_state_becomes_observed_zero_and_keeps_history_during_lookup_failure(tmp_path,width,dark):
    from test_quota_value_history import interval, report_for
    app=QApplication.instance() or QApplication([])
    shared_theme().configure('dark' if dark else 'light')
    panel=QuotaPanel(QSettings(str(tmp_path/'대기상태.ini'),QSettings.IniFormat))
    scroll=Scroll();scroll.put(fillViewport=True);scroll.setWidget(panel)
    host=mount(scroll,width,900)
    try:
        def visible_text():
            return '\n'.join(str(item.property('text')) for item in walk(host.quick.rootObject())
                             if item.isVisible() and item.metaObject().indexOfProperty('text')>=0)
        QTest.qWait(40)
        text=visible_text()
        assert '사용량 기록 수집 대기' in text
        assert '0%p' not in text and '$0.0000' not in text
        assert not panel.history.isVisible() and not panel.history_controls.isVisible()
        assert not panel.history_legend.isVisible() and not panel.conversion.isVisible()
        assert host.grab().save(str(tmp_path/f'한도수집대기_{width}.png'))
        # A five-hour observation remains reachable before weekly usage exists.
        panel.receive({'report':{'cycles':[],'history':[
            dict(at=100,used=0,window='five_hour',source='live')]}})
        QTest.qWait(40)
        assert panel.history_controls.isVisible() and panel.history_empty.isVisible()
        choice=control(host,panel.window);choice.forceActiveFocus()
        QTest.keyClick(host.quick,Qt.Key_Down);QTest.qWait(40)
        assert panel.history.isVisible() and panel.history.rows[0]['remaining']==100
        assert panel.history_waiting.isVisible()
        QTest.keyClick(host.quick,Qt.Key_Up);QTest.qWait(40)
        # A confirmed zero interval must not be mistaken for missing evidence.
        cycle=interval([(100,80),(130,80)],[(115,0)])
        report=report_for([cycle])
        panel.receive({'report':report})
        QTest.qWait(40)
        assert panel.statistics['total'] and panel.basis.text()=='0%p'
        assert panel.cost_value.text()=='$0.0000'
        assert panel.history.isVisible() and panel.history_legend.isVisible()
        assert not panel.history_waiting.isVisible() and panel.conversion.isVisible()
        assert panel.history.rows
        rows=panel.history.rows
        panel.receive({'issue':'Codex 한도 조회 실패'})
        QTest.qWait(40)
        assert panel.status.isVisible() and panel.history.rows is rows
        assert panel.basis.text()=='0%p' and panel.cost_value.text()=='$0.0000'
        assert not panel.history_waiting.isVisible()
        assert not host.qml_errors
    finally:dispose(host);shared_theme().configure('light')


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
        assert panel.remaining_legend.text()=='━ 누적 소모량 (%p)'
        # The last cycle has a complete 99 -> 98 boundary, even though its
        # opening idle observation is hidden from the display.
        assert panel.history.series['completed_costs']==[None]*6+[pytest.approx(.0105),None]
        index=0
        plot.activateAt(panel.history.x_at(index),panel.history.box.center().y())
        QTest.qWait(30)
        from cachemonitor.pricing import usd
        assert plot.detail['items'][0]['label']=='누적 소모량'
        assert plot.detail['items'][3]['value']==usd(panel.history.series['completed_costs'][index])
        assert host.grab().save(str(tmp_path/'quota-all-cycles.png'))
        assert not host.qml_errors
    finally:dispose(host)


@pytest.mark.parametrize('width,dark',[(520,False),(1120,True)])
def test_legend_visibility_survives_refresh_and_updates_pinned_details(quota_page,width,dark):
    app,panel,scroll=quota_page
    shared_theme().configure('dark' if dark else 'light')
    host=mount(scroll,width,1000)
    try:
        chart=panel.history
        plot=render_plot(host,chart)
        series=chart.series
        plot.activateAt(chart.x_at(0),chart.box.center().y())
        toggle=panel.legend_toggles['cycle_cost']
        click(host,render_plot(host,toggle));render_plot(host,chart)
        assert chart.series is series
        assert not chart.visible('cycle_cost')
        assert all(i['label']!='누적 구독 가치 환산액' for i in plot.detail['items'])
        panel.render(automatic=True)
        assert not chart.visible('cycle_cost') and not toggle.isChecked()
        render_plot(host,toggle).forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Space)
        render_plot(host,chart)
        assert chart.visible('cycle_cost')
        for toggle in panel.legend_toggles.values():toggle.setChecked(False)
        render_plot(host,chart)
        assert chart.curves()==[] and chart.box.width()>width*.7
        assert not chart.detail_for(0)['items']
        panel.window.setCurrentIndex(1);render_plot(host,chart)
        assert panel.remaining_legend.isVisible()
        assert not panel.legend_toggles['cycle_cost'].isVisible()
        panel.remaining_legend.setChecked(True);render_plot(host,chart)
        assert [c[0] for c in chart.curves()]==['remaining']
        assert not host.qml_errors
    finally:dispose(host);shared_theme().configure('light')


@pytest.mark.parametrize('raw,expected', [('62500.0000000000','62,500'),('1234.5600000','1,234.56'),('0.000120000','0.00012'),('0.000000','0'),('1000','1,000')])
def test_credit_balance_drops_only_trailing_fractional_zeros(raw,expected):
    from cachemonitor.quota_panel import format_credit_balance
    from cachemonitor.quota_live import normalize_credits
    credits=normalize_credits({'balance':raw})
    assert format_credit_balance(credits['balance'])==expected
    assert credits['balance']==raw
