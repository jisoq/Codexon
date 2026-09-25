"""Rendered Qt confirmations operate the same durable hook tickets."""
import time
import pytest
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from cachemonitor.cache_control import hook_decision
from cachemonitor.cache_panel import CachePanel
from cachemonitor.quick_qa import mount,dispose,control,click,render_plot
from test_cache_product import profile
from test_cache_operating import URL,HEADERS
from test_cache_management import body,response
from cachemonitor.cache_operating import target


def test_english_cache_consent_is_complete_and_preserves_user_text(tmp_path):
    import ast
    from pathlib import Path
    from cachemonitor.i18n import set_language,tr
    from cachemonitor.translation_catalog import CATALOG
    from cachemonitor.presentation import Text,Scroll
    from cachemonitor.quick_qa import scroll_extent
    source=Path(__file__).resolve().parents[1]/'cachemonitor/cache_panel.py'
    literals={n.value for n in ast.walk(ast.parse(source.read_text(encoding='utf-8')))
              if isinstance(n,ast.Constant) and isinstance(n.value,str) and any('\uac00'<=c<='\ud7a3' for c in n.value)}
    assert not literals-set(CATALOG)
    app=QApplication.instance() or QApplication([]);set_language('en')
    panel=CachePanel('home',tmp_path/'index.sqlite');host=mount(panel)
    try:
        panel.display(dict(calls=1,priced=0,audit=[dict(title='작업 자료 원문',changes=['model_changed'],read=0,written=None)]))
        assert '작업 자료 원문' in panel.audit.state['text'] and 'Model changed' in panel.audit.state['text']
        assert tr('작업기 연결됨')=='Worker connected'
        panel.journal.operations.propose(target('home',body(),URL,HEADERS,False),.01,.1,64,'natural_output_proxy')
        panel.show_operating();QTest.qWait(100);dialog=panel.operating_dialog
        messages=[n.state['text'] for n in dialog.findChildren(Text)]
        message='\n'.join(messages)
        assert 'one call may exceed' in message and 'at most 2 sequential calls' in message
        assert not any('\uac00'<=c<='\ud7a3' for c in message)
        assert dialog.host.windowTitle()=='Limited operation without a total cost cap'
        dialog.host.resize(450,320);QTest.qWait(60)
        scroll=dialog.findChild(Scroll);assert scroll is not None
        assert scroll_extent(dialog.host,scroll)>0
        assert dialog.host.grab().save(str(tmp_path/'english-cache-consent.png'))
        dialog.reject();QTest.qWait(30);assert not panel.journal.operations.grants()
    finally:panel.stop();dispose(host);set_language('ko')


def test_panel_recovers_after_temporary_storage_error(tmp_path,monkeypatch):
    import sqlite3
    app=QApplication.instance() or QApplication([])
    panel=CachePanel('home',tmp_path/'index.sqlite',active=True)
    original=panel.control.requests
    try:
        monkeypatch.setattr(panel.control,'requests',lambda:(_ for _ in ()).throw(sqlite3.OperationalError('database is locked')))
        panel.poll();assert panel.timer.isActive()
        monkeypatch.setattr(panel.control,'requests',original)
        panel.poll();assert panel.control.get('ui_heartbeat')>=time.time()-1
        assert panel.timer.isActive()
    finally:panel.stop()


def test_dashboard_survives_boot_schema_lock_and_restores_saved_cache_settings(tmp_path):
    import sqlite3
    from PySide6.QtCore import QSettings
    from cachemonitor.cache_control import Control,control_path
    from cachemonitor.cache_execution import Journal
    from cachemonitor.dashboard import Dashboard
    from test_ui import snapshot
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    index=tmp_path/'index.sqlite';path=control_path(index)
    saved=Control(path)
    for key in ('enabled','automatic','guard'):saved.set(key,True)
    saved.db.execute('PRAGMA user_version=0');saved.close()
    journal=Journal(path);key=journal.reserve('fixture','session',0,body(),'pending');journal.sent(key);journal.close()
    writer=sqlite3.connect(path,isolation_level=None)
    writer.execute('PRAGMA user_version=0');writer.execute('BEGIN IMMEDIATE')
    window=None
    try:
        window=Dashboard(['fixture'],start_worker=False,settings=QSettings(str(tmp_path/'ui.ini'),QSettings.IniFormat),
            index_path=index,cache_control=True,static_snapshot=snapshot(),live_limits=False)
        window.show();QTest.qWait(100)
        assert window.cache_panel.control is None and not window.cache_master.isEnabled()
        window.open_settings();QTest.qWait(30)
        assert window.current_page==4 and window.cache_panel.timer.isActive()
        writer.rollback()
        until=time.monotonic()+3
        while window.cache_panel.control is None and time.monotonic()<until:QTest.qWait(50)
        assert window.cache_master.isEnabled() and window.cache_master.isChecked()
        assert window.nav.count()==5 and window.cache_panel.control.enabled('automatic')
        assert window.cache_panel.control.enabled('guard')
        assert window.cache_panel.journal.rows()[0]['state']=='sent'
        assert not window.qml_errors
    finally:
        writer.close()
        if window:
            window.cache_panel.stop();window.observer_panel.stop();window.quitting=True;window.tick.stop();window.tray.hide();window.close()
        app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_forecast_survives_automatic_policy_and_execution_states_but_not_context_or_expiry(tmp_path):
    import asyncio
    from cachemonitor.cache_scheduler import Scheduler
    from cachemonitor.cache_integration import enrich
    app=QApplication.instance() or QApplication([])
    index=tmp_path/'index.sqlite';panel=CachePanel('home',index,active=True);host=mount(panel)
    scheduler=Scheduler('home',panel.path,continuous_capture=True)
    try:
        row=dict(profile(),turn='t',ts=time.time(),key='original')
        enrich([dict(home='home',id='session',history=[row])],index,time.time())
        request=body();result=dict(response(),id='original')
        scheduler.executor.contexts.completed(request,result)
        scheduler.snapshot(request,result,time.monotonic(),URL,HEADERS,False)
        snapshot=scheduler.snapshots['session']
        observed=scheduler.policy('session',snapshot,observe=True)
        panel.poll();label=panel.estimate.text()
        assert '유지 1회 예상' in label
        scheduler.control.set('automatic',True)
        decision=scheduler.policy('session',snapshot)
        assert decision.get('maintenance_expected')==observed['maintenance_expected']
        assert not decision.get('observation_only') and not decision.get('passive_analysis')
        for status in (decision,dict(state='reserved'),dict(state='completed'),dict(state='stopped',reason='revoked')):
            scheduler.control.status('home','session',status)
            panel.poll();assert panel.estimate.text()==label
        forecast=scheduler.control.forecasts('home',time.time())[0]
        scheduler.control.forecast('home','session',dict(forecast,cost_valid_until=time.time()-1))
        panel.poll();assert '수집하고' in panel.estimate.text()
        scheduler.control.forecast('home','session',forecast)
        scheduler.control.profile('home','session',dict(row,key='changed',ts=row['ts']+1,model='gpt-6-astra'))
        panel.poll();assert '수집하고' in panel.estimate.text()
        assert not host.qml_errors
    finally:asyncio.run(scheduler.close());panel.stop();dispose(host)


@pytest.mark.parametrize('size',[(1440,940),(1000,700)])
def test_master_switch_navigation_and_independent_features(tmp_path,size):
    from PySide6.QtCore import QSettings
    from cachemonitor.dashboard import Dashboard
    from test_ui import snapshot
    from cachemonitor.fonts import load_bundled_fonts
    app=QApplication.instance() or QApplication([]);load_bundled_fonts()
    previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat)
    settings.setValue('ui/theme','light')
    def create():
        window=Dashboard(['fixture'],start_worker=False,settings=settings,index_path=tmp_path/'index.sqlite',
                         cache_control=True,static_snapshot=snapshot(),live_limits=False)
        window.resize(*size);window.show();QTest.qWait(80);return window
    def close(window):
        window.cache_panel.stop();window.observer_panel.stop();window.quitting=True;window.tick.stop();window.tray.hide();window.close()
    window=create()
    try:
        assert window.nav.count()==4
        window.open_settings();window.settings_page.reveal(6);QTest.qWait(60)
        assert window.settings_page.navigation.currentText()=='캐시 관리'
        assert [window.settings_page.navigation.itemText(i) for i in range(7)]==[
            '일반','작업표시줄 위젯','세션 오버레이','캐시 관리','알림','프록시','정보·문제 해결']
        click(window,control(window,window.cache_master));assert window.nav.count()==5
        window.nav.setCurrentRow(4);QTest.qWait(80)
        assert window.current_page==5 and window.heading.text()=='캐시 관리'
        panel=window.cache_panel
        # Narrow windows stack these cards. Reveal each switch inside its real
        # scroll viewport before clicking; scene coordinates alone can be clipped.
        click(window,render_plot(window,panel.toggles['guard']))
        assert panel.control.enabled('guard') and not panel.control.enabled('automatic')
        click(window,render_plot(window,panel.toggles['automatic']))
        assert panel.control.enabled('guard') and panel.control.enabled('automatic')
        assert window.grab().save(str(tmp_path/'cache-dashboard.png'))
        window.resize(1000,700);QTest.qWait(100)
        assert window.grab().save(str(tmp_path/'cache-dashboard-small.png'))
        window.cache_master.setChecked(False)
        assert window.current_page==4 and window.nav.count()==4
        assert not panel.control.enabled('guard') and not panel.control.enabled('automatic')
        assert panel.control.get('selection:guard') and panel.control.get('selection:automatic')
        assert not panel.control.get('guard') and not panel.control.get('automatic')
        assert not window.qml_errors
        close(window);window=create()
        assert not window.cache_master.isChecked() and window.nav.count()==4
        window.cache_master.setChecked(True)
        assert window.cache_panel.control.enabled('guard') and window.cache_panel.control.enabled('automatic')
        assert not window.cache_panel.journal.operations.grants()
    finally:
        close(window);app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_rendered_hook_approval_cancel_close_timeout_and_disconnect(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=CachePanel('home',tmp_path/'index.sqlite',active=True)
    panel.control.set('guard',True);panel.control.profile('home','s',dict(profile(),written=0))
    host=mount(panel);pool=ThreadPoolExecutor(1)
    try:
        for choice in ('approve','cancel','close','timeout','disconnect'):
            panel.timer.start();panel.poll()
            event=dict(hook_event_name='UserPromptSubmit',session_id='s',turn_id=choice,model='gpt-6-sol',prompt='synthetic')
            future=pool.submit(hook_decision,panel.path,'home',event,1 if choice=='timeout' else 10)
            until=time.monotonic()+4
            while panel.dialog is None and time.monotonic()<until:QTest.qWait(30)
            assert panel.dialog is not None
            dialog=panel.dialog;QTest.qWait(80)
            assert dialog.host.grab().save(str(tmp_path/(choice+'.png')))
            assert not dialog.host.qml_errors
            if choice in ('approve','cancel'):
                node=dialog.confirm if choice=='approve' else dialog.cancel
                click(dialog.host,control(dialog.host,node))
            elif choice=='close':dialog.host.close()
            elif choice=='disconnect':panel.timer.stop();panel.control.set('ui_heartbeat',0)
            until=time.monotonic()+4
            while not future.done() and time.monotonic()<until:QTest.qWait(30)
            result=future.result(timeout=1)
            assert bool(result)==(choice in ('cancel','close','timeout'))
            panel.poll();QTest.qWait(50)
            assert panel.dialog is None
            if choice=='approve':assert '진행 허용' in panel.hook_status.text()
        panel.display(dict(calls=2,priced=1,known_cost=.01,shortfalls=1,audit=[]))
        assert '비용 미확인 1회' in panel.summary.text()
        panel.control.set('automatic',True);panel.proxy_ready=True
        panel.control.status('home','s',dict(state='waiting',reason='output_bound_not_verified',history_can_unlock=False))
        panel.poll()
        assert '이력 추가만으로 활성화되지 않음' in panel.status.text()
        assert '시험 호출은 자동으로 보내지 않습니다' in panel.activation.text()
        panel.control.status('home','s',dict(state='observing',observation_only=True,observed_at=time.time(),
            snapshot='original',
            model='gpt-6-astra',effort='high',service_tier='Standard',source_transport='WebSocket',maintenance_transport='HTTP',
            maintenance_expected=.012,maintenance_adverse=.1,cost_stop_scenario=.024,operating_scope_available=False))
        panel.poll()
        assert '관측 전용' in panel.status.text() and '사용자 WebSocket' in panel.forecast.text()
        assert '현재 문맥은 실행 범위 밖' in panel.forecast.text()
        assert not panel.consent_button.isEnabled()
        assert host.grab().save(str(tmp_path/'cache-settings.png'))
        assert not host.qml_errors
    finally:
        panel.stop();pool.shutdown();dispose(host)


def test_rendered_operating_scope_consent_close_revoke_and_cost_status(tmp_path):
    app=QApplication.instance() or QApplication([])
    panel=CachePanel('home',tmp_path/'index.sqlite',active=True);host=mount(panel)
    try:
        proposal=panel.journal.operations.propose(target('home',body(),URL,HEADERS,False),.01,.1,64,'natural_output_proxy')
        panel.poll();QTest.qWait(50)
        assert panel.consent_button.isEnabled()
        # Closing/cancelling the concrete scope never grants permission.
        for choice in ('close','cancel','approve'):
            panel.show_operating();QTest.qWait(80);dialog=panel.operating_dialog
            assert dialog is not None and not dialog.host.qml_errors
            if choice=='approve':assert dialog.host.grab().save(str(tmp_path/'operating-consent.png'))
            if choice=='close':dialog.host.close()
            else:click(dialog.host,control(dialog.host,dialog.confirm if choice=='approve' else dialog.cancel))
            QTest.qWait(60)
            assert panel.operating_dialog is None
            assert bool(panel.journal.operations.grants())==(choice=='approve')
        grant=panel.journal.operations.grants()[0]
        assert grant['scope']==proposal['scope'] and grant['max_calls']==2
        assert not panel.control.get('automatic',False)  # Permission is separate from activation.
        assert panel.control.get('bounded_provider:chatgpt.com') is None
        assert '남은 2/2회' in panel.operating_status.text()
        operation=dict(id=grant['id'],scope=grant['scope'],expected=.01,adverse=.1,output_high=64)
        key=panel.journal.reserve('home','s',0,body(),'original',operation=operation)
        assert panel.journal.permit_operation(key,operation)
        result=response();result['usage']['output_tokens']=1000000
        panel.journal.finish(key,'completed',result,scope_read_lower=80)
        panel.poll();assert '관측 비용 기준 도달' in panel.operating_status.text()
        assert '남은 1/2회' in panel.operating_status.text()
        assert host.grab().save(str(tmp_path/'operating-cost-stop.png')) and not host.qml_errors
        # New consent is explicit after a stop; it does not reuse the old allowance.
        panel.show_operating();QTest.qWait(60)
        dialog=panel.operating_dialog;click(dialog.host,control(dialog.host,dialog.confirm));QTest.qWait(50)
        assert len(panel.journal.operations.grants())==2
        panel.revoke_operating();assert panel.journal.operations.grants()[0]['stopped']=='revoked'
        panel.control.set('automatic',False);panel.control.set('automatic',True);panel.poll()
        assert '철회' in panel.operating_status.text()
    finally:
        panel.stop();dispose(host)
