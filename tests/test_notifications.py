import time

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.core import Session, Transport
from cachemonitor.dashboard import Dashboard


@pytest.mark.parametrize('legacy',[False,True])
def test_notification_settings_migrate_and_persist_independently(tmp_path,legacy):
    app=QApplication.instance() or QApplication([])
    settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat)
    settings.setValue('notifications',legacy)
    windows=[]
    try:
        first=Dashboard([],start_worker=False,settings=settings,live_limits=False)
        windows.append(first)
        assert all(option.isChecked()==legacy for option in first.notification_options.values())
        first.notification_options['HTTP 전환'].setChecked(True)
        first.notification_options['캐시 저하 의심'].setChecked(False)
        first.notification_options['모델명 불일치'].setChecked(False)
        first.notification_options['프록시 장애'].setChecked(True)
        settings.sync()
        second=Dashboard([],start_worker=False,
            settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat),live_limits=False)
        windows.append(second)
        assert second.notification_options['HTTP 전환'].isChecked()
        assert not second.notification_options['캐시 저하 의심'].isChecked()
        assert not second.notification_options['모델명 불일치'].isChecked()
        assert second.notification_options['프록시 장애'].isChecked()
    finally:
        for window in windows:
            window.quitting=True;window.tick.stop();window.tray.hide();window.close()


def test_http_notifications_do_not_replay_cached_warnings(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    window=Dashboard([],start_worker=False,settings=QSettings(str(tmp_path/'notify.ini'),QSettings.IniFormat),live_limits=False)
    sent=[];monkeypatch.setattr(window.tray,'showMessage',lambda title,*_:sent.append(title))
    now=time.time();window.started_at=now-1
    clock=[now];engine=window.confirmed_notifications
    engine.wall_clock=lambda:clock[0];engine.http_since=engine.http_floor=now-1
    session=Session('one',str(tmp_path),title='alerts')
    def receive():
        window.receive(dict(ts=clock[0],sessions=[session.view(clock[0])],homes=[],errors=[],unassigned=[]))
    try:
        receive()
        session.transports.append(Transport(now,'HTTP/SSE','/responses','HTTP 전환 기록',turn='turn'))
        receive();receive()
        assert sent==['Codexon · HTTP 전환']
        window.notification_options['HTTP 전환'].setChecked(False)
        clock[0]+=1
        session.transports.append(Transport(clock[0],'HTTP/SSE','/responses','HTTP 전환 기록',turn='second'))
        receive()
        clock[0]+=1;window.notification_options['HTTP 전환'].setChecked(True);receive()
        assert len(sent)==1
        clock[0]+=1
        session.transports.append(Transport(clock[0],'HTTP/SSE','/responses','HTTP 전환 기록',turn='third'))
        receive()
        assert len(sent)==2
    finally:window.quit_app()


def test_confirmed_alerts_reach_tray_and_click_opens_evidence(tmp_path,monkeypatch):
    import os
    from cachemonitor.model_evidence import EvidenceStore,EvidenceReader
    from cachemonitor.dashboard import STYLE
    app=QApplication.instance() or QApplication([]);app.setStyleSheet(STYLE)
    window=Dashboard([],start_worker=False,
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat),live_limits=False)
    sent=[]
    monkeypatch.setattr(window.tray,'showMessage',lambda title,*_:sent.append(title))
    now=time.time();engine=window.confirmed_notifications
    engine.model_since=engine.model_floor=now-10
    clock=[100.];engine.clock=lambda:clock[0]
    store=EvidenceStore(tmp_path/'wire.sqlite');reader=EvidenceReader(store.path)
    try:
        source=Session('task',str(tmp_path),title='알림 근거 확인')
        source.add_usage(now-1,'r',{'input_tokens':100,'output_tokens':10},'asked','turn','high')
        store.write(tmp_path,'attempt',now-1,'WebSocket','r','asked','reported','completed')
        reader.poll();view=source.view(now)
        view['history']=reader.enrich(tmp_path,view['history'])
        window.receive(dict(ts=now,sessions=[view],homes=[],errors=[],unassigned=[]))
        assert sent==['Codexon · 모델명 불일치 확인']
        state=dict(configured=True,probe_state='refused',phase='recovery_required')
        for stamp in (100,115,130):
            clock[0]=stamp;window.observer_panel.display(state)
        assert sent[-1]=='Codexon · 로컬 프록시 연결 거부'
        window.tray.messageClicked.emit();app.processEvents()
        assert window.current_page==4 and window.nav.currentRow()==-1
        assert window.notification_details.toggle.isChecked()
        rows=window.notification_log.model().rows
        assert '요청: asked → 응답: reported' in rows[1]['detail']
        assert '응답 ID: r' in rows[1]['detail']
        assert '3회 거부' in rows[0]['detail']
        window.observer_panel.display(dict(configured=True,probe_state='healthy',phase='active'))
        assert len(sent)==2 and rows[0]['resolution']=='정상 식별 응답 확인'
        window.open_notification_record(1)
        assert window.current_page==2 and window.selected_call=='r'
        assert window.selected_call_scope==(str(tmp_path),'task')
        assert window.record_section=='evidence'
        capture=os.environ.get('CACHEMONITOR_NOTIFICATION_CAPTURE')
        if capture:
            window.resize(1350,1000);app.processEvents()
            assert window.grab().save(capture)
    finally:
        reader.close();store.close()
        window.quitting=True;window.tick.stop();window.tray.hide();window.close()
