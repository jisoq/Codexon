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
        assert host.grab().save(str(tmp_path/'cache-settings.png'))
        assert not host.qml_errors
    finally:
        panel.stop();pool.shutdown();dispose(host)
