"""Rendered Qt confirmations operate the same durable hook tickets."""
import time
from concurrent.futures import ThreadPoolExecutor
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from cachemonitor.cache_control import hook_decision
from cachemonitor.cache_panel import CachePanel
from cachemonitor.quick_qa import mount,dispose,control,click
from test_cache_product import profile
from test_cache_operating import URL,HEADERS
from test_cache_management import body,response
from cachemonitor.cache_operating import target


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
            model='gpt-6-astra',effort='high',service_tier='Standard',source_transport='WebSocket',maintenance_transport='HTTP',
            maintenance_expected=.012,maintenance_adverse=.1,cost_stop_scenario=.024,operating_scope_available=False))
        panel.poll()
        assert '관측 전용' in panel.status.text() and '사용자 WebSocket' in panel.forecast.text()
        assert '현재 모델은 초기 운용 대상 밖' in panel.forecast.text()
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
