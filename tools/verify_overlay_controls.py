"""Interactive native QA on a window owned by this process, never on Codex."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.fonts import configure_font_rendering,configure_high_dpi,load_bundled_fonts


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=int,default=120);parser.add_argument('--benchmark',action='store_true');args=parser.parse_args()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    configure_font_rendering();configure_high_dpi()
    from PySide6.QtCore import QSettings,QTimer
    from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton, QLabel, QSizePolicy
    from cachemonitor.overlay import OverlayController
    from cachemonitor.overlay_windows import WindowsOverlay
    from cachemonitor.overlay_tracking import Selection
    from cachemonitor.overlay_data import summarize_session
    from cachemonitor.core import Session
    from cachemonitor.analysis_engine import AnalysisEngine
    app=QApplication([]);load_bundled_fonts();app.setQuitOnLastWindowClosed(False)
    host=QWidget();host.setWindowTitle('Cache Monitor overlay controls verification')
    layout=QVBoxLayout(host)
    layout.addWidget(QLabel('Owned test surface · controls and input pass-through'))
    button=QPushButton('Click-through target');layout.addWidget(button,1)
    button.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
    host.resize(1000,760);host.show()
    settings=QSettings(str(args.output.with_suffix('.ini')),QSettings.IniFormat)
    settings.setValue('overlay/enabled',True);settings.setValue('overlay/collapsed',False)
    controller=OverlayController(settings,native_enabled=False)
    native=WindowsOverlay();controller.native=native;native.configure(int(controller.widget.winId()))
    hwnd=int(host.winId())
    # Input tools may explicitly activate a non-activating companion. Keep QA
    # visible for our own windows only; record this exception in the report.
    own={hwnd,int(controller.widget.winId()),*(int(w.winId()) for w in controller.chrome)}
    native.visible_target=lambda h: bool(native.u.IsWindowVisible(h) and not native.u.IsIconic(h)
                                         and native.u.GetForegroundWindow() in own)
    fixture=Session('fixture','fixture',title='오버레이 조작 검증')
    for i,cached in enumerate((0,6000,8000,9000,9200,9400,9500,9000,9600,9400,9600,9800)):
        fixture.add_usage(time.time()-120+i,str(i),dict(input_tokens=10000,cached_input_tokens=cached,
            cache_write_input_tokens=0,output_tokens=200,reasoning_output_tokens=100),
            'gpt-6-astra','fixture-turn','high','Standard')
    engine=AnalysisEngine();engine.ingest([fixture.view(time.time())])
    data=summarize_session(next(iter(engine.sessions.values()))['prepared'])
    controller.receive_snapshot({'overlay_sessions':[data]})
    report={'pid':os.getpid(),'host':hwnd,'events':[],'foreground_gate':'own test windows only',
            'model_requests':0,'finished':False}
    def snapshot():
        return {'time':time.time(),'collapsed':controller.collapsed,'opacity':controller.opacity,
                'anchor':controller.anchor,'panel_visible':controller.widget.isVisible(),
                'icon_visible':controller.icon.isVisible(),'geometry':controller.current_geometry,
                'foreground':native.u.GetForegroundWindow(),'expanded':controller.expanded,
                'selected':controller.widget.content_model.selected_id,
                'popup':controller.popup_open,
                'windows':{w.windowTitle():int(w.winId()) for w in controller.chrome if w.isVisible()}}
    def save():args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def event(kind):report['events'].append({'kind':kind,**snapshot()});save()
    button.clicked.connect(lambda:event('click-through'))
    controller.actions.collapse.connect(lambda:event('collapse'))
    controller.actions.expand.connect(lambda:event('detail'))
    controller.icon.restore.connect(lambda:event('restore'))
    controller.toolbar.opacity_changed.connect(lambda _:event('opacity'))
    controller.header.drag_finished.connect(lambda:event('drag-panel'))
    controller.icon.drag_finished.connect(lambda:event('drag-icon'))
    def drag_sample(kind):
        report['events'].append({'kind':kind,'cursor':native.cursor(),'context':controller.drag_context,
                                 'anchor':controller.anchor})
    controller.header.drag_started.connect(lambda:drag_sample('press'))
    controller.header.drag_moved.connect(lambda:drag_sample('move'))
    def poll():
        controller.receive_snapshot({'overlay_sessions':[data]})
        controller.receive_target({'target':{'hwnd':hwnd},'selection':Selection('fixture')})
        report['current']=snapshot()
    timer=QTimer();timer.timeout.connect(poll);timer.start(100)
    def finish():
        timer.stop();report['finished']=True;report['current']=snapshot();save()
        controller.stop();host.close();app.quit()
    if args.benchmark:
        timer.stop()
        def benchmark():
            from statistics import median
            from PySide6.QtCore import QPoint,QPointF,Qt,QEvent
            from PySide6.QtGui import QMouseEvent
            from PySide6.QtTest import QTest
            native.visible_target=lambda h:h==hwnd and bool(native.u.IsWindowVisible(h) and not native.u.IsIconic(h))
            poll();QTest.qWait(150)
            samples=[];errors=[];scenarios=[];pending_at=[None];queue_wait=[]
            original_move=controller.move_drag
            def measured_move(position=None):
                if pending_at[0] is not None:
                    queue_wait.append((time.perf_counter()-pending_at[0])*1000);pending_at[0]=None
                return original_move(position)
            controller.move_drag=measured_move
            def stats(values):
                values=sorted(values)
                return dict(n=len(values),median_ms=median(values),p95_ms=values[int(.95*(len(values)-1))],max_ms=max(values)) if values else {}
            for scenario,expanded in ((s,e) for s in ('steady','updates','burst') for e in (False,True)):
                start_index=len(samples);queue_wait.clear();controller.anchor=(1,1)
                controller.expanded=expanded;controller.refresh();QTest.qWait(100)
                point=QPoint(20,20);origin=controller.header.mapToGlobal(point)
                def mouse(kind,global_point,button,buttons):
                    event=QMouseEvent(kind,QPointF(controller.header.mapFromGlobal(global_point)),QPointF(global_point),button,buttons,Qt.NoModifier)
                    app.sendEvent(controller.header,event)
                mouse(QEvent.MouseButtonPress,origin,Qt.LeftButton,Qt.LeftButton)
                previous_pointer=origin
                for i in range(80):
                    controller.observed_at=time.monotonic()
                    pointer=origin-QPoint(i%40,i%20)
                    if scenario=='burst':pointer=origin-QPoint((i%2)*160,(i%2)*80)
                    started=time.perf_counter()
                    if scenario=='updates' and i%12==0:poll()
                    count=16 if scenario=='burst' else 1
                    for j in range(1,count+1):
                        pending_at[0]=time.perf_counter()
                        point=previous_pointer+(pointer-previous_pointer)* (j/count)
                        mouse(QEvent.MouseMove,point,Qt.NoButton,Qt.LeftButton)
                    previous_pointer=pointer
                    app.processEvents()
                    rect=native.frame(int(controller.widget.winId()))
                    box=controller.current_geometry
                    errors.append(max(abs(rect[0]-box[0]),abs(rect[1]-box[1])))
                    samples.append((time.perf_counter()-started)*1000)
                    if scenario=='steady' and i in (0,40,79):
                        controller.widget.quick.grabFramebuffer().save(str(args.output.with_name(args.output.stem+f'-{expanded}-{i}.png')))
                    QTest.qWait(16)
                mouse(QEvent.MouseButtonRelease,pointer,Qt.LeftButton,Qt.NoButton)
                scenarios.append(dict(scenario=scenario,expanded=expanded,processing=stats(samples[start_index:]),queue_wait=stats(queue_wait)))
            values=sorted(samples)
            report['drag_benchmark']={'n':len(values),'median_ms':median(values),'p95_ms':values[int(.95*(len(values)-1))],
                'max_ms':max(values),'max_geometry_error_px':max(errors),'raw_ms':samples,
                'measurement':'Qt mouse event dispatch through native position readback; physical display latency not measured'}
            report['drag_benchmark']['scenarios']=scenarios
            finish()
        QTimer.singleShot(0,benchmark)
        QTimer.singleShot(30000,finish)
    else:QTimer.singleShot(args.seconds*1000,finish)
    app.exec()


if __name__=='__main__':main()
