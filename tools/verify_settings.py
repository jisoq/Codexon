"""Capture actual settings scenes with isolated data, without external requests."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import re
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    from cachemonitor.fonts import configure_font_rendering,configure_high_dpi,load_bundled_fonts
    configure_font_rendering();configure_high_dpi()
    from PySide6.QtCore import QSettings,Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.overlay import install_overlay
    from cachemonitor.i18n import set_language
    from cachemonitor.theme import shared_theme
    app=QApplication([]);load_bundled_fonts();app.setProperty('cachemonitorDisableShellIntegration',True)
    report={'model_requests':0,'captures':[]}
    for language in ('ko','en'):
        set_language(language)
        with tempfile.TemporaryDirectory(prefix='codexon-settings-') as folder:
            settings=QSettings(str(Path(folder)/'preferences.ini'),QSettings.IniFormat)
            window=Dashboard([],start_worker=False,settings=settings,live_limits=False,manage_observer=False)
            from cachemonitor.analysis_worker import AnalysisBridge
            window.worker=AnalysisBridge([],static_snapshot={})
            overlay=install_overlay(window,native_enabled=False)
            try:
                window.setAttribute(Qt.WA_ShowWithoutActivating);window.show();window.open_settings()
                for theme in ('light','dark'):
                    shared_theme().configure(theme)
                    for width,height in ((1000,700),(1440,940),(1920,1080)):
                        window.resize(width,height)
                        for category in (*window.settings_page.IDS,'search'):
                            if category=='search':window.settings_page.search.setText('cache' if language=='en' else '캐시')
                            else:window.settings_page.reveal(category)
                            app.processEvents();QTest.qWait(100)
                            assert not window.qml_errors,window.qml_errors
                            if language=='en':
                                from cachemonitor.quick_qa import walk
                                for item in walk(window.quick.rootObject()):
                                    if not item.isVisible() or item.metaObject().indexOfProperty('text')<0:continue
                                    text=item.property('text')
                                    if isinstance(text,str):assert not re.search('[가-힣]',text) or text=='한국어',text
                            path=args.output/f'{language}-{theme}-{width}-{category}.png'
                            assert window.grab().save(str(path))
                            report['captures'].append({'path':str(path),'actual_size':[window.width(),window.height()]})
            finally:
                overlay.stop();window.quit_app();app.processEvents()
    set_language('ko')
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'captures':len(report['captures']),'qml_errors':[]}))

if __name__=='__main__':main()
