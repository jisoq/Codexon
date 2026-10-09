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
    from cachemonitor.version import VERSION
    app=QApplication([]);load_bundled_fonts();app.setProperty('cachemonitorDisableShellIntegration',True)
    report={'model_requests':0,'captures':[]}
    for language in ('ko','en'):
        set_language(language)
        with tempfile.TemporaryDirectory(prefix='settings-',dir=args.output.resolve()) as folder:
            settings=QSettings(str(Path(folder)/'preferences.ini'),QSettings.IniFormat)
            window=Dashboard([str(Path(folder)/'home')],start_worker=False,settings=settings,live_limits=False,manage_observer=False,
                             index_path=str(Path(folder)/'index.sqlite'),quota_path=str(Path(folder)/'quota.sqlite'),collection_autostart=False)
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
                        if width==1440:
                            panel=window.update_panel;window.settings_page.reveal('about')
                            record=panel.journal.begin('proxy',VERSION,replace=True)
                            for phase,proxy in (
                                ('waiting',dict(responding=2)),
                                ('needs_exit',dict(unknown_connections=1,required_action='close_client')),
                                ('verifying',dict(cutover_started=True)),
                                ('partial',dict(restored=True)),
                                ('complete',{}),
                            ):
                                panel.offer=None;panel.journal.change(record['operation_id'],phase=phase,
                                    app=dict(state='verified'),proxy=proxy)
                                panel.refresh();app.processEvents();QTest.qWait(60)
                                if phase=='needs_exit':
                                    assert panel.recheck.isVisible() and panel.later.isVisible()
                                    assert not panel.button.isVisible() and not panel.execute.isVisible()
                                if phase=='verifying':assert not any(node.isVisible() for node in (panel.cancel,panel.later,panel.execute,panel.button))
                                assert not window.qml_errors,window.qml_errors
                                path=args.output/f'{language}-{theme}-{width}-update-{phase}.png'
                                assert window.grab().save(str(path));report['captures'].append({'path':str(path)})
                            next_version=VERSION.rsplit('.',1)[0]+'.'+str(int(VERSION.rsplit('.',1)[1])+1)
                            panel.show_plan(dict(kind='app',release=dict(tag_name='v'+next_version)))
                            assert panel.execute.isVisible() and not panel.button.isVisible()
                            QTest.qWait(60)
                            path=args.output/f'{language}-{theme}-{width}-update-offered.png'
                            assert window.grab().save(str(path));report['captures'].append({'path':str(path)})
            finally:
                overlay.stop();window.quit_app();app.processEvents()
    set_language('ko')
    (args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'captures':len(report['captures']),'qml_errors':[]}))

if __name__=='__main__':main()
