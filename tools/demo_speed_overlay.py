"""Open an isolated, interactive speed-warning overlay with synthetic calls.

Close the demo host to close all companion windows. --verify exercises actual
Qt clicks and captures themes on an unshown desktop via run_ui_checks.py.
"""
import argparse
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def sample(mode='slow'):
    from cachemonitor.core import Session
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    speeds=[100]*10 + ([] if mode=='normal' else [35]*3) + ([100]*2 if mode=='recovered' else [])
    now=time.time()
    s=Session('speed-demo','synthetic-speed-demo',title='더미 · 속도 안내 미리보기')
    for i in range(len(speeds)):
        s.add_usage(now-3000+i*100,f'demo-{i}',dict(input_tokens=80000,cached_input_tokens=72000,
                    cache_write_input_tokens=0,output_tokens=2000,reasoning_output_tokens=500),
                    'gpt-6-astra','demo-turn','high','Standard')
    data=s.view(now)
    for row,speed in zip(data['history'],speeds):
        row.update(response_status='completed',timing_valid=True,completion_latency_ms=2000/speed*1000,
                   transport='WebSocket',transport_source='response_id',
                   requested_model='gpt-6-astra',response_model='gpt-6-astra',model_match='일치')
    engine=AnalysisEngine();engine.ingest([data])
    return OverlaySummaries().collect(engine)[0]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify',type=Path,help='Capture and verify, then exit')
    args=parser.parse_args()
    from cachemonitor.fonts import configure_font_rendering,configure_high_dpi,load_bundled_fonts
    configure_font_rendering();configure_high_dpi()
    from PySide6.QtCore import QSettings,QTimer,Qt
    from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QLabel,QPushButton
    from cachemonitor.overlay import OverlayController
    from cachemonitor.overlay_windows import WindowsOverlay
    from cachemonitor.overlay_tracking import Selection
    app=QApplication([]);load_bundled_fonts()
    temporary=tempfile.TemporaryDirectory(prefix='codexon-speed-demo-')
    settings=QSettings(str(Path(temporary.name)/'settings.ini'),QSettings.IniFormat)
    settings.setValue('ui/theme','dark')
    controller=OverlayController(settings,native_enabled=False)
    host=QWidget();host.setWindowTitle('Codexon · 속도 저하 더미 오버레이')
    layout=QVBoxLayout(host)
    label=QLabel('더미 데이터 · 기준 100 → 최근 35 tok/s\n오버레이의 ⓘ를 클릭하면 상세 안내가 열립니다.')
    label.setWordWrap(True);layout.addWidget(label)
    data=sample()
    def update(mode):
        nonlocal data
        data=sample(mode)
        label.setText({'slow':'더미 · 속도 저하 65% · ⓘ를 클릭해 상세 안내 확인',
                       'normal':'', 'recovered':''}[mode])
        poll()
    for title,mode in [('저하 상황','slow'),('정상 상황','normal'),('회복 상황','recovered')]:
        button=QPushButton(title);button.clicked.connect(lambda checked=False,m=mode:update(m));layout.addWidget(button)
    layout.addStretch()
    close=QPushButton('더미 오버레이 닫기');close.clicked.connect(host.close);layout.addWidget(close)
    # Leave the right side to the real overlay; the host is only a demo control.
    layout.setContentsMargins(24,24,680,24)
    host.resize(1100,800)
    area=app.primaryScreen().availableGeometry()
    host.resize(min(host.width(),area.width()-48),min(host.height(),area.height()-48))
    host.move(area.center()-host.rect().center());host.show()
    native=WindowsOverlay();controller.native=native
    hwnd=int(host.winId())
    # Own demo surface stays visible while users continue using other apps.
    native.visible_target=lambda h:h==hwnd and bool(native.u.IsWindowVisible(h) and not native.u.IsIconic(h))
    def poll():
        controller.receive_snapshot({'overlay_sessions':[data]})
        controller.receive_target({'target':{'hwnd':hwnd},'selection':Selection('speed-demo')})
    timer=QTimer();timer.timeout.connect(poll);timer.start(500)
    original_close=host.closeEvent
    def close_demo(event):
        timer.stop();controller.stop();original_close(event);app.quit()
    host.closeEvent=close_demo
    poll()
    if args.verify:
        from PySide6.QtTest import QTest
        from PySide6.QtGui import QPainter
        from cachemonitor.quick_qa import click
        from cachemonitor.overlay_chrome import named_item
        output=args.verify;output.mkdir(parents=True,exist_ok=True)
        def capture(name):
            QTest.qWait(80)
            pix=controller.widget.grab();p=QPainter(pix)
            if controller.detail.isVisible():p.drawPixmap(0,0,controller.detail.grab())
            x=round(controller.widget.content_model.monitor_x*controller.appearance.scale)
            if controller.links.isVisible():p.drawPixmap(x,0,controller.links.grab())
            p.drawPixmap(x+round(280*controller.appearance.scale),round(8*controller.appearance.scale),controller.actions.grab())
            p.end();assert pix.save(str(output/f'{name}.png'))
        try:
            QTest.qWait(150)
            for theme in ('dark','light'):
                settings.setValue('ui/theme',theme);controller.next_theme=0
                controller.expanded=False;update('slow');QTest.qWait(80)
                content=controller.widget.content_model
                assert content.speed_alert()['active'] and not content.speed_detail
                capture(f'{theme}-monitor')
                button=named_item(controller.links.quick.rootObject(),'nav-speed-info')
                assert button is not None
                click(controller.links,button);QTest.qWait(80)
                assert content.speed_detail and controller.expanded and controller.detail.isVisible()
                assert '세션 재생성을 권장합니다.' in content.detailBody.state['accessible']
                capture(f'{theme}-detail')
                # Routine refresh keeps the open detail and evidence stable.
                poll();assert content.speed_detail
                update('recovered')
                assert not any(x['id']=='speed-info' for x in content.monitor_links())
                assert '출력 속도 저하' not in content.detailBody.state['accessible']
                assert '세션 재생성' not in content.detailBody.state['accessible']
                capture(f'{theme}-recovered')
                controller.escape();QTest.qWait(40)
                update('normal');assert not content.speed_alert()['active']
            assert not any(w.qml_errors for w in (controller.widget,*controller.chrome))
            print('Verified: real icon click, internal detail, themes, refresh, recovery, normal suppression.',flush=True)
        finally:
            host.close();app.processEvents();temporary.cleanup()
        return
    try:app.exec()
    finally:temporary.cleanup()


if __name__=='__main__':main()
