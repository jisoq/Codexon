"""Render every settings category with isolated preferences and no model requests."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.fonts import configure_font_rendering,configure_high_dpi,load_bundled_fonts


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    configure_font_rendering();configure_high_dpi()
    from PySide6.QtCore import Qt,QSettings
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.app import Dashboard
    from cachemonitor.dashboard import STYLE
    from cachemonitor.overlay import install_overlay
    from cachemonitor.analysis_worker import AnalysisBridge
    from cachemonitor.version import VERSION
    app=QApplication([]);load_bundled_fonts();app.setStyleSheet(STYLE)
    app.setProperty('cachemonitorDisableShellIntegration',True)
    settings=QSettings(str(args.output/'preferences.ini'),QSettings.IniFormat)
    window=Dashboard([],start_worker=False,settings=settings,live_limits=False,manage_observer=False)
    window.worker=AnalysisBridge([],static_snapshot={})
    overlay=install_overlay(window,native_enabled=False)
    report={'model_requests':0,'captures':[]}
    try:
        window.observer_panel.active=True;window.observer_panel.update_controls()
        window.observer_panel.display({'configured':True,'phase':'active','app_version':VERSION,
            'health':{'version':VERSION},'runtime':{'phase':'active'},
            'registration':{'autostart':True,'executable':r'C:\Apps\CacheMonitor\CacheMonitor.exe'}})
        window.nav.setCurrentRow(3)
        window.setAttribute(Qt.WA_ShowWithoutActivating);window.show()
        for width,height in ((1120,760),(1480,1000)):
            window.resize(width,height)
            for index,title in enumerate(window.settings_page.TITLES):
                window.settings_page.reveal(index);app.processEvents();QTest.qWait(30)
                assert not window.period.isVisible() and not window.model.isVisible() and not window.price_button.isVisible()
                path=args.output/f'{width}-{index}.png';assert window.grab().save(str(path))
                report['captures'].append({'category':title,'path':str(path),'size':[width,height]})
        window.settings_page.reveal(2)
        page=window.settings_page
        assert 'collapsed' not in page.controls and 'opacity' not in page.controls
        overlay.toolbar.hide_button.activate();assert overlay.collapsed
        overlay.set_collapsed(False)
        overlay.toolbar.slider.setValue(40);assert overlay.opacity==60
        overlay.anchor=(.25,.75);overlay.end_drag()
        page.controls['reset_position'].activate();assert overlay.anchor is None
        window.overlay_action.trigger();assert not page.controls['overlay'].isChecked()
        page.controls['overlay'].setChecked(True);assert window.overlay_action.isChecked()
        report['settings_and_overlay_synced']=True
        assert 'position' not in page.controls
        report['tray']=[a.text() for a in window.tray.contextMenu().actions() if not a.isSeparator()]
        assert report['tray']==['대시보드 열기','종료']
    finally:
        overlay.stop();window.observer_panel.stop();window.quitting=True
        window.tick.stop();window.tray.hide();window.close();app.processEvents()
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'captures':len(report['captures']),'synced':report['settings_and_overlay_synced']}))


if __name__=='__main__':main()
