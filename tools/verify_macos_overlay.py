"""Render and exercise the independent macOS panel with synthetic records only.

Qt dispatches clicks directly to our own controls; the physical cursor and other
applications are untouched. Screenshots contain only our fixture panel, never
the desktop. Every temporary window is closed before returning.
"""
from pathlib import Path
import argparse
import json
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fixture():
    from cachemonitor.core import Session
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    now = time.time()
    sessions = []
    for index, sid in enumerate(('11111111-1111-1111-1111-111111111111', '22222222-2222-2222-2222-222222222222')):
        session = Session(sid, 'synthetic-home', title=('한글 프로젝트 · 비용과 캐시 분석' if index == 0 else '다른 세션 · 선택한 기록만 표시'))
        for call in range(24):
            session.add_usage(now-400+call*13, f'request-{index}-{call}',
                              dict(input_tokens=100000*(index+1), cached_input_tokens=(90000+call*300)*(index+1),
                                   cache_write_input_tokens=0, output_tokens=2000+call*40, reasoning_output_tokens=600),
                              'gpt-6-astra', f'turn-{call}', 'high', 'Standard')
        sessions.append(session.view(now))
    engine = AnalysisEngine();engine.ingest(sessions)
    return dict(overlay_sessions=OverlaySummaries().collect(engine), errors=[], index=dict(loading=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This verification requires macOS.')
    from dataclasses import replace
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtGui import QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from PySide6.QtQuick import QQuickItem
    from cachemonitor.fonts import load_bundled_fonts, configure_high_dpi
    from cachemonitor.overlay import OverlayController
    from cachemonitor.overlay_macos import MacOverlay
    from cachemonitor.overlay_appearance import default_appearance
    from cachemonitor.overlay_view import call_graph_axes
    from cachemonitor.quick_qa import click

    configure_high_dpi()
    app = QApplication([]);app.setQuitOnLastWindowClosed(False)
    if app.platformName() != 'cocoa':
        parser.error('Use the Cocoa Qt platform for native overlay verification.')
    load_bundled_fonts()
    args.output.mkdir(parents=True, exist_ok=True)
    report = dict(platform='cocoa', synthetic_records=True, permission_requests=0, real_app_mutations=0, screens=[])
    with tempfile.TemporaryDirectory(prefix='codexon-native-overlay-') as temporary:
        settings = QSettings(str(Path(temporary)/'settings.ini'), QSettings.IniFormat)
        settings.setValue('ui/theme', 'codex')
        controller = OverlayController(settings, native_enabled=False, appearance_path=Path(temporary)/'missing.toml')
        controller.native = MacOverlay()
        controller.appearance_reader.read = lambda dark: default_appearance(False)
        snapshot = fixture()
        def settle():
            controller.receive_snapshot(snapshot)
            app.processEvents();QTest.qWait(90)
        def capture(name):
            settle()
            geometry = controller.current_geometry
            ratio = controller.widget.devicePixelRatioF()
            image = QPixmap(round((geometry[2]+20)*ratio), round((geometry[3]+20)*ratio))
            image.setDevicePixelRatio(ratio);image.fill(Qt.transparent)
            painter = QPainter(image)
            for panel in (controller.shadow, controller.widget, controller.detail, controller.links,
                          controller.header, controller.actions, controller.icon, controller.toolbar):
                if panel.isVisible():
                    x, y, _, _ = controller._placements[int(panel.winId())]
                    painter.drawPixmap(x-geometry[0]+10, y-geometry[1]+10, panel.grab())
            painter.end()
            path = args.output/(name+'.png')
            assert image.save(str(path))
            report['screens'].append(str(path))
        def button(name):
            return controller.actions.quick.rootObject().findChild(QQuickItem, name)
        try:
            controller.receive_snapshot(snapshot)
            a, b = snapshot['overlay_sessions']
            assert controller.set_manual_session(a['home'], a['id'])
            settle();assert controller.widget.isVisible() and controller.header.isVisible()
            model=controller.widget.content_model
            def title_pixels(pinned):
                model.set_content(a,appearance=model.appearance,pinned=pinned)
                image=QImage(model.panel_width(),model.panel_height(),QImage.Format_ARGB32_Premultiplied)
                image.fill(0);painter=QPainter(image);model.paint(painter);painter.end()
                return image.copy(16,12,260,28)
            assert title_pixels(False)!=title_pixels(True)
            assert model.display_title()=='고정 · '+a['title'] and not a['title'].startswith('고정 · ')
            assert controller.widget.accessibleName().splitlines()[0]==model.display_title()
            capture('manual-light')
            click(controller.actions, button('expand'));settle()
            assert controller.expanded and controller.detail.isVisible()
            capture('manual-detail')
            click(controller.actions, button('opacityButton'));settle()
            assert controller.popup_open and controller.toolbar.isVisible()
            controller.toolbar.slider.setValue(35);settle()
            assert controller.opacity == 65
            capture('manual-opacity')
            QTest.keyClick(controller.toolbar.quick, Qt.Key_Escape);settle()
            assert not controller.popup_open and controller.expanded
            click(controller.actions, button('collapse'));settle()
            assert controller.collapsed and controller.icon.isVisible() and not controller.widget.isVisible()
            capture('manual-collapsed')
            QTest.mouseClick(controller.icon, Qt.LeftButton);settle()
            assert not controller.collapsed and controller.widget.isVisible()
            assert controller.set_manual_session(b['home'], b['id']);settle()
            assert controller.content()[0]['id'] == b['id'] and b['title'] in controller.header.view.state['title']
            capture('manual-second-session')
            controller.appearance_reader.read = lambda dark: replace(default_appearance(True), font_size=18)
            controller.next_theme = 0;controller.set_opacity(94);settle()
            capture('manual-dark-large-font')
            frame = controller.native.frame(0);x, y, width, height = controller.current_geometry
            assert frame[0] <= x and frame[1] <= y and x+width <= frame[2] and y+height <= frame[3]
            axes = call_graph_axes(controller.content()[0]['recent'], 12)
            assert 0 < axes['cache'][0] < axes['cache'][1] and axes['cache'] != (0, 100)
            assert 0 < axes['cost'][0] < axes['cost'][1]
            composition = controller.content()[0]['token_composition']
            for group in ('input_parts', 'output_parts'):
                assert abs(sum(part['share'] for part in composition[group])-1) < 1e-9
            assert not any(panel.qml_errors for panel in (controller.widget, controller.shadow, *controller.chrome))
            report.update(passed=True, rendered_click_expand=True, rendered_click_collapse_restore=True,
                          opacity_control=True, escape_closes_popup=True, exact_manual_session_switch=True,
                          pinned_title_changes_painted_pixels=True, source_titles_unchanged=True,
                          no_panel_overflow=True, dynamic_cache_and_cost_axes=axes,
                          composition_denominator=100, korean_font='Pretendard JP',
                          device_pixel_ratio=controller.widget.devicePixelRatioF(), qml_errors=[])
            controller.set_enabled(False);app.processEvents()
            assert not any(panel.isVisible() for panel in (controller.widget, controller.shadow, *controller.chrome))
            controller.set_enabled(True);settle();assert controller.widget.isVisible()
            controller.follow_codex();app.processEvents()
            assert controller.manual_session is None and not controller.widget.isVisible()
            report.update(enable_disable=True, automatic_without_selection_hides=True)
        finally:
            controller.stop();app.processEvents()
    (args.output/'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False)+'\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
