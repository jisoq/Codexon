"""Render both themes and optionally observe the live, unmodified Codex window.

Live mode runs the production analysis bridge against an isolated verification index.
It makes no model requests and changes neither Codex settings nor window layout.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cachemonitor.fonts import configure_font_rendering, configure_high_dpi, load_bundled_fonts
from cachemonitor.overlay import OverlayController, SessionOverlay
from cachemonitor.overlay_appearance import CodexAppearance, resolve_appearance
from cachemonitor.overlay_data import token_composition


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live-seconds', type=int, default=0)
    parser.add_argument('--index', type=Path)
    args = parser.parse_args()
    configure_font_rendering(); configure_high_dpi()
    from PySide6.QtCore import QSettings, QTimer, Qt
    from PySide6.QtGui import QPainter, QPixmap, QColor, QLinearGradient, QFontDatabase
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    app = QApplication([]); app.setQuitOnLastWindowClosed(False); load_bundled_fonts()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    from copy import deepcopy
    from cachemonitor.core import Session
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    from cachemonitor.overlay_appearance import default_appearance
    from cachemonitor.overlay_chrome import OverlayChrome, OverlayDetail
    # Explicit fixture calls exercise the production summary; no history is
    # reconstructed from an aggregate. These images are render-test artifacts.
    session=Session('render-fixture','render-fixture',title='예시 작업 · 사용량 살펴보기')
    for i,rate in enumerate((0,72,83,90,96,96,92,88,94,91,0,86,92,96,91,98,95,96,98,91,96,98,97,96)):
        session.add_usage(1789884000+i*13,str(i),dict(input_tokens=100000,cached_input_tokens=rate*1000,
            cache_write_input_tokens=0,output_tokens=2000+i*40,reasoning_output_tokens=600+i*10),
            'gpt-6-astra','turn','high','Standard')
    engine=AnalysisEngine();engine.ingest([session.view(1789884400)])
    sample=OverlaySummaries().collect(engine)[0]
    widget=SessionOverlay();actions=OverlayChrome('actions');detail=OverlayDetail(widget.content_model)
    popup=OverlayChrome('toolbar')
    def capture(name,value,note='',dark=True,expanded=False,inline=False,opacity_popup=False,reduced=False,scroll_label=None):
        theme=default_appearance(dark);widget.set_content(value,note,appearance=theme)
        widget.set_layout(detail=expanded,inline=inline,reduced=reduced)
        actions.apply_appearance(theme,94);actions.view.put(expanded=expanded,popupOpen=opacity_popup)
        actions.resize(88,32);actions.show();widget.show()
        if expanded:detail.resize(380 if inline else 240,widget.panel_height());detail.show()
        else:detail.hide()
        if opacity_popup:popup.resize(160,40);popup.apply_appearance(theme,94);popup.show()
        else:popup.hide()
        QTest.qWait(70)
        if scroll_label:
            from PySide6.QtQuick import QQuickItem
            row=next(item for item in widget.content_model._detail_items if item[0]==scroll_label)
            scroller=detail.quick.rootObject().findChild(QQuickItem,'detailScroll')
            scroller.setProperty('contentY',min(row[2],scroller.property('contentHeight')-scroller.height()))
            QTest.qWait(40)
        result=widget.grab();painter=QPainter(result)
        if expanded:painter.drawPixmap(0,0,detail.grab())
        painter.drawPixmap(round(widget.content_model.monitor_x+280),8,actions.grab())
        if opacity_popup:painter.drawPixmap(round(widget.content_model.monitor_x+204),44,popup.grab())
        painter.end()
        path=args.output.with_name(args.output.stem+'-'+name).with_suffix('.png');result.save(str(path))
        assert not widget.qml_errors and not detail.qml_errors and not actions.qml_errors
        return result
    images=[capture('light',sample,dark=False),capture('dark',sample)]
    capture('detail-light',sample,dark=False,expanded=True)
    capture('detail-dark',sample,expanded=True)
    capture('inline',sample,expanded=True,inline=True)
    capture('popup',sample,opacity_popup=True)
    capture('loading',sample,'기록 확인 중')
    capture('error',sample,'수집 오류')
    unknown=deepcopy(sample)
    unknown['token_composition']=token_composition({'history':[dict(input=10000,cached=9000,written=None,output=1000,reasoning=None)]})
    capture('unknown',unknown)
    large=deepcopy(sample);large.update(latest_cost=999999,cost=23456789,mean_cost=9999,partial=True,missing=2,priced=22)
    large.update(model='gpt-very-long-shared-prefix-2026-09-special-requested-model',response_model='gpt-very-long-shared-prefix-2026-09-special-response-model',model_mismatch=True)
    capture('large-models',large)
    capture('compact-tokens',sample,expanded=True,reduced=True,scroll_label='토큰 구성')
    capture('compact-tokens-inline',sample,expanded=True,inline=True,reduced=True,scroll_label='토큰 구성')
    incident=Session('incident-fixture','fixture',title='캐시 관측 근거 확인')
    for i,cached in enumerate((90000,90000,90000,90000,10000,8000,90000,91000)):
        incident.add_usage(1789884000+i*13,str(i),dict(input_tokens=100000,cached_input_tokens=cached,
            cache_write_input_tokens=0,output_tokens=100,reasoning_output_tokens=50),'gpt-6-astra','turn','high','Standard')
    incident_engine=AnalysisEngine();incident_engine.ingest([incident.view(1789884400)])
    incident_data=OverlaySummaries().collect(incident_engine)[0]
    capture('degradation-evidence',incident_data,expanded=True,scroll_label='세션 저하 의심')
    canvas=QPixmap(380*2+48,widget.panel_height()+32);canvas.fill(QColor('#BAC4BE'))
    painter=QPainter(canvas)
    for i,image in enumerate(images):painter.drawPixmap(16+i*396,16,image)
    painter.end();canvas.save(str(args.output.with_suffix('.png')))
    for window in (popup,detail,actions,widget):window.close()
    families=QFontDatabase.families()
    if not args.live_seconds: return
    if not args.index: parser.error('--index must name an isolated verification index')
    if not args.index.resolve().is_relative_to(Path(__file__).resolve().parents[1]/'artifacts'):
        parser.error('Verification index must be inside this checkout\'s artifacts directory')
    settings = QSettings(str(args.output.with_suffix('.ini')), QSettings.IniFormat)
    settings.setValue('overlay/enabled', True)
    controller = OverlayController(settings)
    report = {'samples': [], 'errors': [], 'source': str(args.index), 'model_requests': 0}
    from cachemonitor.analysis_worker import AnalysisBridge
    from cachemonitor.model_evidence import default_path as evidence_path
    worker = AnalysisBridge([os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))], args.index,
                            model_evidence_path=evidence_path())
    worker.snapshot.connect(controller.receive_snapshot)
    worker.failure.connect(report['errors'].append)
    worker.start()
    previous = None
    def poll():
        nonlocal previous
        try:
            selected = controller.target_state.get('selection')
            state = controller.target_state
            shown = controller.widget.isVisible()
            row = {'time': time.time(), 'shown': shown, 'issue': controller.issue,
                   'appearance': asdict(controller.appearance),
                   'thread': selected.thread_id if selected else None,
                   'host': selected.host if selected else None,
                   'summary': controller.content()[0], 'loading': controller.loading,
                   'text': controller.widget.lines() if shown else [],
                   'frame': controller.native.frame(int(controller.widget.winId())) if shown else None,
                   'target_frame': controller.native.frame(state['target']['hwnd']) if state.get('target') else None}
            report['samples'].append(row)
            if shown and (selected.thread_id, controller.dark) != previous:
                controller.widget.grab().save(str(args.output.with_name(args.output.stem+'-live').with_suffix('.png')))
                previous = (selected.thread_id, controller.dark)
        except Exception as error: report['errors'].append(str(error))
    timer = QTimer(); timer.timeout.connect(poll); timer.start(1000)
    def finish():
        timer.stop(); controller.stop()
        worker.requestInterruption(); worker.wait()
        args.output.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'samples': len(report['samples']), 'shown': sum(r['shown'] for r in report['samples']),
                          'errors': report['errors']}, ensure_ascii=False), flush=True)
        app.quit()
    QTimer.singleShot(args.live_seconds*1000, finish)
    app.exec()


if __name__ == '__main__': main()
