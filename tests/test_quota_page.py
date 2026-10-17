"""Exercise the real Quick page, chart selection and its single detail entry."""
import time

import pytest
from PySide6.QtCore import QPointF, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.presentation import Scroll
from cachemonitor.quota_panel import QuotaPanel, model_cost_intervals
from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor.quick_qa import click, control, dispose, mount, render_plot, click_row, walk, scene_view
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


@pytest.mark.integration
@pytest.mark.essential
def test_current_chart_remains_usable_until_full_history_finishes(quota_page,tmp_path):
    app,panel,scroll=quota_page
    host=mount(scroll,1000,680)
    full=panel.report
    try:
        for failed in (False,True):
            panel.receive(dict(home='h',report={**full,'history_pending':True,'history_failed':failed}))
            assert not panel.cycle_choice.isEnabled() and not panel.manage_button.isEnabled()
            assert all(panel.summary_values[key].text() in ('계산 중','확인 불가') for key in ('usage','cost','weekly'))
            assert panel.history_loading.isVisible() is not failed
            assert panel.history_loading._state['running'] is not failed
            if not failed:
                from cachemonitor.i18n import tr
                loading=render_plot(host,panel.history_loading)
                title=next(item for item in walk(loading)
                           if item.property('text')==tr(panel.history_loading.state['title']))
                assert host.quick.grabFramebuffer().save(str(tmp_path/'quota-inline-loading.png'))
                assert abs(title.mapToScene(QPointF(0,0)).x()-loading.mapToScene(QPointF(0,0)).x())<=1
                assert title.width()>=loading.width()-1
            plot=render_plot(host,panel.history);click(host,plot,x=plot.width()*.6,y=plot.height()*.5)
            QTest.keyClick(host.quick,Qt.Key_Home)
            assert panel.history.cursor==0 and panel.history.rows
        panel.receive(dict(home='h',report=full))
        assert panel.cycle_choice.isEnabled() and panel.manage_button.isEnabled()
        assert not panel.history_loading.isVisible() and not panel.history_loading._state['running']
        assert panel.summary_values['cost'].text() not in ('계산 중','확인 불가')
        assert not host.qml_errors
    finally:dispose(host)


@pytest.mark.integration
@pytest.mark.essential
def test_selected_cycle_reset_updates_visible_chart(quota_page,tmp_path):
    from cachemonitor.dashboard import Dashboard
    from test_quota_worker import wait_until
    app,fixture,_=quota_page
    quota=fixture.quota;home=str(tmp_path/'home');path=tmp_path/'live.sqlite'
    seed=QuotaLedger(path)
    tracking.enable(seed.db,home,time.time()-310)
    for row in fixture.report['history']:
        if row['window']!='weekly':continue
        q={**quota,'requested_at':row['at'],'observed_at':row['at'],'windows':{
            'weekly':dict(used_percent=row['used'],resets_at=row['reset'],window_minutes=10080)}}
        seed.observe(home,q);tracking.observe(seed.db,home,q)
    activity(seed,time.time(),{},[r['at'] for r in fixture.report['history']],home=home)
    seed.close()
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    settings=QSettings(str(tmp_path/'reset.ini'),QSettings.IniFormat)
    for key in ('updates/automatic','overlay/enabled','taskbar/enabled'):settings.setValue(key,False)
    host=Dashboard([home],start_worker=False,settings=settings,index_path=tmp_path/'index.sqlite',quota_path=path,live_limits=False)
    host.resize(1000,680);host.show();host.start_quota_service(home)
    panel=host.quota_panel
    try:
        wait_until(lambda:'worker_revision' in panel.report and not panel.report.get('history_pending'),app=app)
        plot=render_plot(host,panel.history)
        before=panel.history._picture.copy()
        start=panel.cycle_choice.currentData()
        click(host,render_plot(host,panel.clear_cycle_button))
        dialog=panel.clear_cycle_dialog
        click(dialog.host,render_plot(dialog.host,dialog.confirm))
        wait_until(lambda:panel.cycle_choice.currentData()!=start,app=app)
        wait_until(lambda:panel.history._picture!=before,app=app)
        assert panel.cycle_choice.currentData()!=start
        assert len(panel.history.rows)==1
        assert panel.history.rows[0]['at']==panel.cycle_choice.currentData()
        assert panel.history._picture!=before
        assert not panel.history_loading.isVisible()
        cleared=panel.history._picture.copy()
        at=time.time()
        quota={**quota,'requested_at':at,'observed_at':at,'windows':{
            **quota['windows'],'weekly':{**quota['windows']['weekly'],'used_percent':3}}}
        ledger=QuotaLedger(path)
        try:
            turns={'new':dict(started_at=at,ended_at=None)}
            activity(ledger,at,turns,home=home)
            ledger.observe(home,quota);tracking.observe(ledger.db,home,quota)
            call_at=time.time()
            activity(ledger,call_at,turns,[call_at],home=home)
            at=time.time()
            quota={**quota,'requested_at':at,'observed_at':at,'windows':{
                **quota['windows'],'weekly':{**quota['windows']['weekly'],'used_percent':4}}}
            ledger.observe(home,quota);tracking.observe(ledger.db,home,quota)
            activity(ledger,time.time(),turns,[call_at],home=home)
        finally:ledger.close()
        revision=panel.report['worker_revision']
        wait_until(lambda:panel.report['worker_revision']!=revision,app=app)
        wait_until(lambda:panel.history.rows[-1]['at']==at,app=app)
        wait_until(lambda:panel.history._picture!=cleared,app=app)
        assert panel.history.rows[-1]['at']==at
        assert panel.history.rows[-1]['remaining']==96
        assert not host.qml_errors
    finally:
        host.quota_service.stop();host.tick.stop();host.update_panel.timer.stop();host.tray.hide()
        dispose(host);app.setProperty('cachemonitorDisableShellIntegration',previous)


@pytest.mark.integration
@pytest.mark.essential
def test_selected_cycle_action_confirmation_and_completion(quota_page,tmp_path):
    from copy import deepcopy
    app,panel,scroll=quota_page
    host=mount(scroll,520,900)
    requests=[];panel.cycle_clear_requested.connect(requests.append)
    try:
        latest=panel._view['periods'][0]
        previous=panel._view['periods'][1]
        panel.cycle_choice.setCurrentIndex(2)
        button=render_plot(host,panel.clear_cycle_button)
        choice=control(host,panel.cycle_choice)
        assert choice.mapToScene(QPointF()).x()+choice.width()<=button.mapToScene(QPointF()).x()
        assert button.mapToScene(QPointF()).x()+button.width()<=host.quick.width()
        click(host,button)
        dialog=panel.clear_cycle_dialog
        click(dialog.host,render_plot(dialog.host,dialog.cancel))
        assert not requests
        click(host,render_plot(host,panel.clear_cycle_button))
        dialog=panel.clear_cycle_dialog
        click(dialog.host,render_plot(dialog.host,dialog.confirm))
        assert requests==[dict(start=previous['start'],account='a',delete=True)]
        assert not panel.clear_cycle_button.isEnabled()
        changed=deepcopy(panel.report);changed.pop('view',None)
        changed['cycle_clears']=[dict(home='h',account='a',start=previous['start'],end=latest['start'],baseline='null')]
        panel.receive(dict(home='h',report=changed,exclusion_result=dict(operation='clear_cycle',ok=True,deleted=True)))
        assert panel.cycle_choice.findData(previous['start'])==-1
        assert panel.cycle_clear_message.isVisible()
        assert panel.cycle_choice.currentData()==latest['start']
        click(host,render_plot(host,panel.clear_cycle_button))
        dialog=panel.clear_cycle_dialog
        click(dialog.host,render_plot(dialog.host,dialog.confirm))
        assert requests[-1]==dict(start=latest['start'],account='a',delete=False)
        panel.receive(dict(home='h',report=changed,exclusion_result=dict(operation='clear_cycle',ok=False,error='test failure')))
        assert panel.cycle_clear_message.isVisible() and panel.clear_cycle_button.isEnabled()
        panel.cycle_choice.setCurrentIndex(0)
        assert not panel.clear_cycle_button.isEnabled()
        assert host.grab().save(str(tmp_path/'selected-cycle-action.png'))
        assert not host.qml_errors
    finally:dispose(host)


@pytest.mark.integration
@pytest.mark.essential
def test_large_summary_values_fit_compact_cards(quota_page,tmp_path):
    from cachemonitor.pricing import usd
    app,panel,scroll=quota_page
    host=mount(scroll,520,900)
    try:
        for amount in (1_000_000,1e30):
            panel.lifetime_statistics['cost']=amount
            panel.lifetime_value=amount
            panel.update_summary()
            for key in ('cost','weekly'):
                node=panel.summary_values[key]
                text=next(item for item in walk(control(host,node))
                          if item.metaObject().indexOfProperty('contentWidth')>=0)
                def fits():
                    return (text.property('text')==usd(amount) and
                            text.property('contentWidth')<=text.width()+1 and
                            text.property('contentHeight')<=text.height()+1)
                deadline=time.monotonic()+2
                while not fits() and time.monotonic()<deadline:
                    app.processEvents();QTest.qWait(10)
                assert node.text()==usd(amount)
                assert fits()
                if amount==1_000_000:assert text.property('lineCount')==1
            assert host.grab().save(str(tmp_path/f'quota-large-{amount}.png'))
        assert not host.qml_errors
    finally:dispose(host)


@pytest.mark.integration
@pytest.mark.parametrize('width,height,dark', [(1120,1000,False),pytest.param(520,900,False,marks=pytest.mark.essential),(1120,1000,True)])
def test_responsive_page_chart_and_single_details_path(quota_page,tmp_path,width,height,dark):
    app,panel,scroll=quota_page
    if dark:shared_theme().configure('dark')
    host=mount(scroll,width,height)
    try:
        plot=render_plot(host,panel.history)
        remaining=[row['remaining'] for row in panel.history.rows]
        assert 0<panel.history.axis[0]<min(remaining)
        assert panel.history.axis[1]>max(remaining)
        assert panel.summary_values['usage'].text()=='9%'
        cards=[scene_view(host,node) for node in panel.summary_cards.values()]
        positions=[card.mapToScene(QPointF()) for card in cards]
        assert positions[0].y()==pytest.approx(positions[1].y())
        assert positions[2].y()==pytest.approx(positions[3].y())
        if width<1000:
            assert positions[2].y()>positions[0].y()+cards[0].height()
            assert positions[2].x()==pytest.approx(positions[0].x())
        else:
            assert positions[2].y()==pytest.approx(positions[0].y())
        for card in cards:assert card.width()==pytest.approx(cards[0].width(),abs=1)
        for node in panel.summary_values.values():
            text=next(item for item in walk(control(host,node))
                      if item.metaObject().indexOfProperty('contentWidth')>=0)
            assert text.property('contentWidth')<=text.width()+1
        assert all(r['local_observed'] for r in panel.history.rows)
        assert panel.cycle_choice.count()==4
        assert panel.cycle_choice.currentData()==max(p['start'] for p in panel._view['periods'])
        assert list(panel.legend_toggles)==['cycle_value','remaining','cycle_cost']
        assert list(panel.history.row_boxes)==['cycle_value','remaining','cycle_cost']
        assert panel.conversion not in panel.layout()._nodes
        assert panel.history.series is panel._view['periods'][0]['series']
        row=panel.history.rows[-1]
        plot.activateAt(panel.history.x_at(len(panel.history.rows)-1),panel.history.box.center().y())
        assert plot.detail['at']==row['at']
        assert plot.detail['items'][1]['metric']=='reference'
        assert plot.detail['items'][1]['subordinate']
        assert host.grab().save(str(tmp_path/f'quota-{width}-{"dark" if dark else "light"}.png'))
        panel.open_dialog();app.processEvents()
        assert panel.dialog.host is not None
        modal=panel.dialog.host
        assert not modal.qml_errors
        assert control(modal,panel.exclusions.start).width()>0
        panel.select_graph.clicked.emit()
        app.processEvents()
        assert panel.dialog.host is None
        plot=render_plot(host,panel.history)
        start,end=panel.history.series['times'][0],panel.history.series['times'][-1]
        panel.history.beginRangeSelection(panel.history.x_at(0),panel.history.box.center().y())
        panel.history.selectionRelease(panel.history.x_at(len(panel.history.rows)-1),panel.history.box.center().y())
        app.processEvents()
        assert panel.dialog.host is not None
        assert panel.exclusions.start.currentData()==start and panel.exclusions.end.currentData()==end
        assert panel.dialog.host.grab().save(str(tmp_path/f'quota-modal-{width}.png'))
        panel.dialog.reject();app.processEvents()
        assert panel.history.state['manageExclusions']
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


@pytest.mark.parametrize('width,dark',[(520,False),(1120,True)])
@pytest.mark.integration
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
        assert not control(host,panel.manage_button).isVisible()
        assert host.grab().save(str(tmp_path/f'한도수집대기_{width}.png'))
        # Five-hour allowances belong to the sidebar and cannot stand in
        # for the weekly cost evidence this page is waiting for.
        panel.receive({'report':{'cycles':[],'history':[
            dict(at=100,used=0,window='five_hour',source='live')]}})
        QTest.qWait(40)
        assert not panel.history_controls.isVisible() and not panel.history_empty.isVisible()
        assert not panel.history.isVisible() and not panel.history.rows
        assert panel.history_waiting.isVisible()
        # A confirmed zero interval must not be mistaken for missing evidence.
        cycle=interval([(100,80),(130,80)],[(115,0)])
        report=report_for([cycle])
        panel.receive({'report':report})
        QTest.qWait(40)
        assert panel.statistics['total'] and panel.basis.text()=='0%p'
        assert panel.cost_value.text()=='$0.0000'
        assert panel.history.isVisible() and panel.history_legend.isVisible()
        assert control(host,panel.manage_button).isVisible()
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


@pytest.mark.integration
@pytest.mark.parametrize('width',[520,1120])
def test_cycle_selector_changes_graph_preserves_lifetime_and_selection_on_refresh(quota_page,tmp_path,width):
    app,panel,scroll=quota_page
    host=mount(scroll,width,1000)
    try:
        assert panel.history.money and panel.cycle_choice.count()==4
        lifetime=panel.result.text()
        scroll.ensureWidgetVisible(panel.cycle_choice);QTest.qWait(40)
        choice=control(host,panel.cycle_choice);choice.forceActiveFocus()
        QTest.keyClick(host.quick,Qt.Key_Down);QTest.qWait(40)
        assert panel.cycle_choice.currentIndex()==2
        period=panel._view['periods'][1]
        assert panel.history.rows is period['series']['rows']
        assert panel.history.rows[0]['reset_kind']=='arbitrary_reset'
        plot=render_plot(host,panel.history)
        plot.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Home)
        assert panel.history.cursor==0 and '임의 초기화' in plot.detail['note']
        selected=panel.cycle_choice.currentData()
        final_values=panel.history.legend_values()
        expected=next(p['value'] for p in panel._view['periods'] if p['start']==selected)
        from cachemonitor.pricing import usd
        assert final_values['cycle_value']==usd(expected)
        assert next(item['value'] for item in plot.detail['items'] if item['metric']=='reference')==usd(expected)
        plot.activateAt(panel.history.x_at(len(panel.history.rows)-1),panel.history.box.center().y())
        assert panel.history.legend_values()==final_values
        plot.activateAt(panel.history.x_at(0),panel.history.box.center().y())
        panel.render(automatic=True);QTest.qWait(40)
        assert panel.cycle_choice.currentData()==selected and panel.history.cursor==0
        assert panel.result.text()==lifetime
        panel.set_history_period(time.time(),None)
        assert panel.result.text()==lifetime and panel.history.rows is period['series']['rows']
        # The overview has one exact aggregate per cycle, and opens its detail.
        panel.cycle_choice.setCurrentIndex(0)
        plot=render_plot(host,panel.history)
        assert panel.history.series is panel._view['overall']
        assert panel.history.series['cycle_summary']
        assert len(panel.history.curves())==3
        assert '주기 소모량' in panel.remaining_legend.text() and '%p' in panel.remaining_legend.text()
        assert len(panel.history.rows)==len(panel._view['periods'])==3
        assert panel.history.series['completed_costs']==[None]*3
        expected=panel.history.rows[0]['cycle_start']
        assert host.grab().save(str(tmp_path/'quota-all-cycles.png'))
        point=panel.history.point(0,'remaining')
        click(host,plot,point.x(),point.y())
        deadline=time.monotonic()+3
        while panel.cycle_choice.currentData()=='all' and time.monotonic()<deadline:QTest.qWait(5)
        assert panel.cycle_choice.currentData()==expected
        chosen=next(p for p in panel._view['periods'] if p['start']==expected)
        assert panel.history.rows is chosen['series']['rows']
        scroll.ensureWidgetVisible(panel.cycle_choice)
        choice=control(host,panel.cycle_choice);choice.forceActiveFocus()
        QTest.keyClick(host.quick,Qt.Key_Home)
        assert panel.cycle_choice.currentData()=='all'
        assert panel.history.series is panel._view['overall']
        plot=render_plot(host,panel.history);plot.forceActiveFocus()
        QTest.keyClick(host.quick,Qt.Key_End)
        expected=panel.history.rows[-1]['cycle_start']
        QTest.keyClick(host.quick,Qt.Key_Return)
        deadline=time.monotonic()+3
        while panel.cycle_choice.currentData()=='all' and time.monotonic()<deadline:QTest.qWait(5)
        assert panel.cycle_choice.currentData()==expected
        assert not host.qml_errors
    finally:dispose(host)


@pytest.mark.parametrize('width,dark',[(520,False),(1120,True)])
@pytest.mark.integration
def test_legend_visibility_survives_refresh_and_updates_pinned_details(quota_page,tmp_path,width,dark):
    app,panel,scroll=quota_page
    shared_theme().configure('dark' if dark else 'light')
    host=mount(scroll,width,1000)
    try:
        chart=panel.history
        plot=render_plot(host,chart)
        series=chart.series
        plot.activateAt(chart.x_at(0),chart.box.center().y())
        toggle=panel.legend_toggles['cycle_cost']
        legend=render_plot(host,toggle)
        captions=[item for item in walk(legend) if item.isVisible() and item.metaObject().indexOfProperty('truncated')>=0]
        assert captions and all(not item.property('truncated') for item in captions)
        assert host.grab().save(str(tmp_path/f'legend-enabled-{width}.png'))
        click(host,legend);render_plot(host,chart)
        assert chart.series is series
        assert not chart.visible('cycle_cost')
        assert all(i['color']!='output' for i in plot.detail['items'])
        render_plot(host,toggle)
        assert host.grab().save(str(tmp_path/f'legend-hidden-{width}.png'))
        panel.render(automatic=True)
        assert not chart.visible('cycle_cost') and not toggle.isChecked()
        render_plot(host,toggle).forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Space)
        render_plot(host,chart)
        assert chart.visible('cycle_cost')
        for toggle in panel.legend_toggles.values():toggle.setChecked(False)
        render_plot(host,chart)
        assert chart.curves()==[] and chart.box.width()>width*.7
        assert not chart.detail_for(0)['items']
        assert panel.remaining_legend.isVisible()
        assert panel.legend_toggles['cycle_cost'].isVisible()
        panel.remaining_legend.setChecked(True);render_plot(host,chart)
        assert [c[0] for c in chart.curves()]==['remaining']
        assert not host.qml_errors
    finally:dispose(host);shared_theme().configure('light')


@pytest.mark.parametrize('raw,expected', [('62500.0000000000','62,500'),('1234.5600000','1,234.56'),('0.000120000','0.00012'),('0.000000','0'),('1000','1,000')])
def test_credit_balance_drops_only_trailing_fractional_zeros(raw,expected):
    from cachemonitor.quota_sidebar import format_credit_balance
    from cachemonitor.quota_live import normalize_credits
    credits=normalize_credits({'balance':raw})
    assert format_credit_balance(credits['balance'])==expected
    assert credits['balance']==raw


@pytest.mark.parametrize('compact',[False,True])
def test_current_quota_refresh_keeps_history_until_report_or_issue_changes(quota_page,monkeypatch,compact):
    _,panel,_=quota_page
    started=panel.report['tracking']['started']-3600
    panel.report['tracking']['started']=started
    if compact:
        from cachemonitor.quota_worker import compact_report
        panel.receive(dict(home='h',quota=panel.quota,report=compact_report(panel.report,'test')))
    monkeypatch.setattr('cachemonitor.quota_layout.time',lambda:started+72*3600)
    rendered=[]
    original=panel.render
    def render(*args,**kwargs):
        rendered.append(panel.report)
        original(*args,**kwargs)
    monkeypatch.setattr(panel,'render',render)
    panel.history.cursor=0
    quota={**panel.quota,'observed_at':panel.quota['observed_at']+1}
    assert panel.receive(dict(home='h',quota=quota,report=panel.report))
    assert panel.quota is quota and panel.history.cursor==0 and not rendered
    assert panel.summary_values['elapsed'].text()=='72 hr'
    monkeypatch.setattr('cachemonitor.quota_layout.time',lambda:started+72.5*3600)
    assert panel.receive(dict(home='h',quota=quota,report=panel.report))
    assert panel.summary_values['elapsed'].text()=='72.5 hr' and not rendered
    assert panel.receive(dict(home='h',quota=quota,report=panel.report,issue='조회 지연'))
    assert len(rendered)==1 and panel.status.text()=='조회 지연'
    assert panel.receive(dict(home='h',quota=quota,report=dict(panel.report),issue='조회 지연'))
    assert len(rendered)==2
    # A period boundary cannot substitute for an unknown measurement start.
    panel.report.pop('tracking',None)
    panel.report['view'].pop('tracking_started',None)
    panel.update_summary()
    assert panel.summary_values['elapsed'].text()=='—'


@pytest.mark.integration
@pytest.mark.essential
def test_monitoring_pause_persists_and_restores_readable_page(quota_page,tmp_path):
    from cachemonitor.dashboard import Dashboard
    app,fixture,_=quota_page
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    settings=QSettings(str(tmp_path/'pause.ini'),QSettings.IniFormat)
    settings.setValue('updates/automatic',False)
    settings.setValue('overlay/enabled',False)
    settings.setValue('taskbar/enabled',False)
    window=Dashboard([],start_worker=False,settings=settings)
    reopened=None
    try:
        window.resize(1120,740);window.show();window.change_page(3)
        window.receive_quota(dict(home='h',quota=fixture.quota,report=fixture.report))
        before=window.quota_sidebar.weekly.text()
        click(window,control(window,window.quota_pause_button))
        assert settings.value('quota/paused',False,type=bool)
        assert window.quota_pause_badge.isVisible()
        control(window,window.quota_panel.summary)
        assert scene_view(window,window.quota_panel.summary).opacity()<1
        assert window.quota_sidebar.weekly.text()==before
        assert window.quota_panel.history.isEnabled()
        assert window.grab().save(str(tmp_path/'monitoring-paused.png'))
        window.change_page(1)
        assert not window.quota_pause_button.isVisible()
        from copy import deepcopy
        updated=deepcopy(fixture.quota)
        updated['windows']['weekly']['used_percent']=45
        window.receive_quota(dict(home='h',quota=updated,report=fixture.report))
        assert window.quota_sidebar.weekly.text()!=before
        reopened=Dashboard([],start_worker=False,settings=settings)
        reopened.resize(1120,740);reopened.show();reopened.change_page(3)
        assert reopened.quota_paused and reopened.quota_pause_badge.isVisible()
        click(reopened,control(reopened,reopened.quota_pause_button))
        assert not settings.value('quota/paused',True,type=bool)
        assert not reopened.quota_pause_badge.isVisible()
        control(reopened,reopened.quota_panel.summary)
        assert scene_view(reopened,reopened.quota_panel.summary).opacity()==1
        assert not window.qml_errors and not reopened.qml_errors
    finally:
        for candidate in (reopened,window):
            if candidate is not None:
                candidate.tick.stop();candidate.update_panel.timer.stop();candidate.tray.hide();dispose(candidate)
        app.setProperty('cachemonitorDisableShellIntegration',previous)
