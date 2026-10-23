"""Dashboard contract and actual Qt Quick interactions for the replacement UI."""
import copy
import time

import pytest
from PySide6.QtCore import QSettings, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.dashboard import Dashboard
from cachemonitor.core import Session
from cachemonitor.quick_qa import control, click, dispose

pytestmark = pytest.mark.integration


def snapshot(now=None):
    now=now or time.time();sessions=[]
    for si in range(3):
        s=Session(f's{si}','fixture',title=f'분석 세션 {si}');s.cwd=f'V:/work/project-{si}'
        for i in range(16):
            s.add_usage(now-3600+i*60,f'r{si}-{i}',dict(input_tokens=10000+i*100,
                cached_input_tokens=0 if i in (4,5) else 8000,cache_write_input_tokens=100,
                output_tokens=100+i,reasoning_output_tokens=10),
                'gpt-6-astra',f't{i//2}','high',service_tier=None if i==15 else 'Fast' if i%2 else 'Standard')
        view=s.view(now);view.update(source='user',archived=False,collection_complete=True,
            turn_states={f't{i}':'완료' for i in range(8)},
            turn_records={f't{i}':dict(started_at=now-3610+i*120,ended_at=now-3500+i*120,state='완료') for i in range(8)})
        for row in view['history']:
            row.update(response_status='completed',completion_latency_ms=1500,generation_latency_ms=500,
                       request_observed_at=row['ts']-1.5,generation_observed_at=row['ts']-1,completed_observed_at=row['ts'])
        sessions.append(view)
    return dict(ts=now,sessions=sessions,homes=['fixture'],errors=[],unassigned=[],index={'loading':False})


@pytest.fixture
def dashboard(tmp_path):
    app=QApplication.instance() or QApplication([])
    from cachemonitor.fonts import load_bundled_fonts
    load_bundled_fonts();previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    settings=QSettings(str(tmp_path/'dashboard.ini'),QSettings.IniFormat);settings.setValue('ui/theme','light')
    window=Dashboard(['fixture'],start_worker=False,settings=settings,static_snapshot=snapshot())
    window.show();QTest.qWait(70)
    try:
        yield window
    finally:
        window.quitting=True;window.tick.stop();window.tray.hide();window.observer_panel.stop()
        dispose(window)
        app.setProperty('cachemonitorDisableShellIntegration',previous)


@pytest.mark.essential
@pytest.mark.parametrize('finite_update',[False,True])
def test_quota_sidebar_updates_outside_cost_page_and_fits_compact_window(dashboard,tmp_path,finite_update):
    from PySide6.QtCore import QPointF
    w=dashboard;now=time.time();w.resize(1000,680);w.change_page(1)
    quota=dict(source='live',observed_at=now,plan_type='pro',unlimited_windows=['five_hour'],
        windows={'weekly':dict(used_percent=77,window_minutes=10080,resets_at=now+3600)},
        credits={'balance':'62500'},reset_credits={'available_count':2,'credits':[
            dict(status='available',expires_at=now+86400),dict(status='available',expires_at=now+172800)]})
    w.receive_quota({'home':'fixture','quota':quota})
    assert w.current_page==1 and w.quota_panel._render_dirty
    assert w.quota_sidebar.quota==quota
    w.change_page(4)
    updated={**quota,'windows':{'weekly':dict(quota['windows']['weekly'],used_percent=81)}}
    if finite_update:
        updated['unlimited_windows']=[]
        updated['windows']['five_hour']=dict(used_percent=20,window_minutes=300,resets_at=now+7200)
        w.update_available.show()
    w.receive_quota({'home':'fixture','quota':updated})
    assert w.quota_sidebar.quota==updated
    assert w.quota_sidebar.weekly.text()=='19.0%'
    QTest.qWait(80)
    for node in (w.quota_sidebar,w.settings_button,w.index_status):
        item=control(w,node);position=item.mapToScene(QPointF(0,0))
        assert item.isVisible() and item.width()>0 and item.height()>0
        assert 0<=position.y()<position.y()+item.height()<=w.quick.height()+1
    assert w.grab().save(str(tmp_path/'sidebar-compact-settings.png'))
    w.receive_quota({'home':'another-home','quota':quota})
    assert w.quota_sidebar.quota==updated
    w.change_page(3);QTest.qWait(50)
    assert not w.quota_panel._render_dirty
    assert not w.qml_errors


def test_quota_and_settings_have_independent_scope(dashboard):
    w=dashboard;w.nav.setCurrentRow(w.navigation_pages.index(3))
    click(w,control(w,w.settings_button));assert w.current_page==4
    w.settings_page.navigation.setCurrentRow(0)
    toggle=w.settings_page.controls['weekly_tracking']
    assert toggle.isChecked()
    click(w,control(w,toggle))
    assert not w.settings.value('quota/trackingEnabled',True,type=bool)
    from cachemonitor.settings_page import SettingsPage
    restored=SettingsPage(w)
    assert not restored.controls['weekly_tracking'].isChecked()
    restored.deleteLater()
    assert not w.price_button.isVisible()
    assert not w.qml_errors


def wait_for_rendered(predicate,timeout=1.5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        QTest.qWait(10)
        if predicate():return
    assert predicate()
