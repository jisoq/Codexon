from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QSettings, QLocale
from PySide6.QtGui import QFont, QFontInfo
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon
from .index import UsageIndex
from .dashboard import Dashboard as UsageDashboard, STYLE, average_tokens
from .fonts import load_bundled_fonts, configure_font_rendering, configure_high_dpi
from .version import VERSION
from .i18n import set_language, tr


class Dashboard(UsageDashboard):
    def __init__(self, homes, start_worker=True, **kwargs):
        kwargs.setdefault("settings", QSettings("CacheMonitor", "CacheMonitor"))
        super().__init__(homes, start_worker=start_worker, **kwargs)


def main():
    parser = argparse.ArgumentParser(description="Read-only Codex cache monitor")
    parser.add_argument('--version',action='version',version=VERSION)
    controls=parser.add_mutually_exclusive_group()
    controls.add_argument('--enable-model-observer',action='store_true')
    controls.add_argument('--disable-model-observer',action='store_true')
    controls.add_argument('--model-observer-status',action='store_true')
    controls.add_argument('--test-model-observer',action='store_true')
    parser.add_argument('--control-report',help='Save observer control result to JSON')
    parser.add_argument('--replace-gui',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--verify-handoff',type=Path,help=argparse.SUPPRESS)
    parser.add_argument("--codex-home", action="append", help="Repeat to monitor multiple local Codex homes")
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--index-path", help="Override the app-owned usage index for isolated verification")
    parser.add_argument('--evidence-path',help='Use the matching observer evidence database')
    parser.add_argument('--cache-control',action='store_true',help='Enable cache controls for an independently managed source worker')
    parser.add_argument("--snapshot", action="store_true", help="Print sanitized live observations without starting the UI")
    parser.add_argument("--smoke", metavar="PNG", help="Render actual data, check tray lifecycle and exit")
    parser.add_argument("--smoke-depth", choices=('core','full'), default='full',
                        help="Choose the compact release check or full interaction probe")
    args = parser.parse_args()
    if args.verify_handoff and (not args.index_path or not args.codex_home or
            not all((Path(h)/'codexon-test-home').is_file() for h in args.codex_home)):
        parser.error('Handoff verification requires isolated homes and an explicit index')
    from .launch_context import resolve_homes, save_homes
    # Synthetic verification never inherits GUI preferences; controls return before saving.
    isolated = bool(args.smoke or args.index_path or args.snapshot)
    homes = (args.codex_home or [os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))]) if isolated else resolve_homes(args.codex_home)
    if args.enable_model_observer or args.disable_model_observer or args.model_observer_status or args.test_model_observer:
        from .observer_control import ObserverManager
        manager=ObserverManager(homes[0],Path(args.index_path).parent if args.index_path else None)
        try:
            result=(manager.test_connection() if args.test_model_observer else manager.turn_on() if args.enable_model_observer else manager.turn_off() if args.disable_model_observer else manager.ensure())
        except Exception as exc:result={'error':str(exc)}
        text=json.dumps(result,ensure_ascii=False,indent=2)
        if args.control_report:Path(args.control_report).write_text(text,encoding='utf-8')
        if sys.stdout is not None:print(text)
        return 1 if result.get('error') else 0
    if args.snapshot:
        monitor = UsageIndex(homes,args.index_path)
        for _ in range(1000):
            snapshot = monitor.poll()
            if not snapshot["index"]["loading"]:
                break
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        monitor.close()
        return 0 if not snapshot["errors"] else 1
    configure_font_rendering()
    configure_high_dpi()
    app = QApplication(sys.argv[:1])
    startup_settings=(QSettings(str(Path(args.index_path).parent/'handoff-settings.ini'),QSettings.IniFormat)
                      if args.verify_handoff else QSettings('CacheMonitor','CacheMonitor'))
    display_language=os.environ.get('CODEXON_LANGUAGE') or startup_settings.value('ui/language','ko')
    set_language(display_language)
    QLocale.setDefault(QLocale('en_US' if display_language=='en' else 'ko_KR'))
    app.setProperty('cachemonitorDisableShellIntegration', bool(args.smoke or args.verify_handoff))
    bundled_fonts = load_bundled_fonts()
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    font = QFont('Pretendard JP')
    font.setPixelSize(14)
    font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
    app.setFont(font)
    app.setStyleSheet(STYLE)
    server = QLocalServer()
    if not args.smoke:
        # One tray icon per Windows user. A second launch opens the existing dashboard.
        server_name = "CacheMonitor-" + os.environ.get("USERNAME", "user")
        if args.verify_handoff:
            import hashlib
            server_name = 'CodexonQA-'+hashlib.sha256(str(Path(args.index_path).resolve()).encode()).hexdigest()[:24]
        client = QLocalSocket()
        client.connectToServer(server_name)
        if client.waitForConnected(400):
            client.write(b"update-exit" if args.replace_gui else b"show")
            client.waitForBytesWritten(400)
            if not args.replace_gui:return 0
            if not client.waitForReadyRead(3000) or bytes(client.readAll())!=b'ready-to-exit':
                QMessageBox.information(None,'Codexon',tr('새 버전이 설치되었습니다. 기존 Codexon을 트레이에서 종료한 뒤 시작 메뉴에서 다시 여세요. Codex 연결은 유지됩니다.'))
                return 4
            client.waitForDisconnected(15000)
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                probe=QLocalSocket();probe.connectToServer(server_name)
                if not probe.waitForConnected(200):break
                probe.disconnectFromServer();time.sleep(.2)
            else:return 4
        QLocalServer.removeServer(server_name)
        if not server.listen(server_name):
            QMessageBox.warning(None, "Codexon", tr("실행 중인 앱 상태를 확인할 수 없습니다."))
            return 1
    smoke_settings = None
    if args.smoke or args.verify_handoff:
        smoke_directory = tempfile.TemporaryDirectory(prefix='cachemonitor-qa-')
        smoke_settings = startup_settings if args.verify_handoff else QSettings(str(Path(smoke_directory.name)/'settings.ini'),QSettings.IniFormat)
        if args.index_path is None:args.index_path=str(Path(smoke_directory.name)/'index.sqlite')
    if not isolated:save_homes(homes)
    window = Dashboard(homes,index_path=args.index_path,model_evidence_path=args.evidence_path,
        cache_control=args.cache_control or (not args.smoke and args.index_path is None),
        live_limits=not (args.smoke or args.verify_handoff),manage_observer=not args.smoke and args.index_path is None, **({'settings':smoke_settings} if smoke_settings else {}))
    from .overlay import install_overlay
    install_overlay(window, native_enabled=not (args.smoke or args.verify_handoff))
    def restart():
        from .app_restart import launch_replacement
        try:launch_replacement(homes,index_path=args.index_path,handoff=args.verify_handoff,evidence_path=args.evidence_path,cache_control=args.cache_control)
        except OSError:
            window.settings_page.refresh_restart()
            QMessageBox.warning(window,'Codexon',tr('앱을 다시 시작하지 못했습니다. 다시 시도해 주세요.'))
    window.settings_page.restartRequested.connect(restart)
    app.aboutToQuit.connect(window.observer_panel.stop)
    app.aboutToQuit.connect(window.cache_panel.stop)
    def finish_update():
        operation=window.update_panel.operation
        if operation and operation.isRunning():operation.wait()
    app.aboutToQuit.connect(finish_update)

    def connection():
        client = server.nextPendingConnection()
        if not client:return
        def receive():
            if not client.bytesAvailable():return
            command=bytes(client.readAll())
            if command==b'update-exit':
                if window.observer_panel.busy():
                    client.write(b'busy');client.flush()
                else:
                    client.write(b'ready-to-exit');client.flush()
                    QTimer.singleShot(100,window.quit_app)
            elif command==b'verify-settings-restart' and args.verify_handoff:
                def exercise_restart():
                    from .quick_qa import control, click
                    from PySide6.QtTest import QTest
                    window.open_settings();window.settings_page.navigation.setCurrentRow(0);window.show();QTest.qWait(60)
                    choice=window.settings_page.controls['language'];button=window.settings_page.controls['restart']
                    choice.setCurrentIndex(choice.findData('ko' if display_language=='en' else 'en'));QTest.qWait(30)
                    assert control(window,button).isEnabled()
                    window.grab().save(str(args.verify_handoff.with_suffix('.png')))
                    click(window,control(window,button))
                client.write(b'restarting');client.flush()
                QTimer.singleShot(100,exercise_restart)
            else:window.show_window()
            client.disconnectFromServer()
            client.deleteLater()
        client.readyRead.connect(receive)
        receive()

    server.newConnection.connect(connection)
    if args.verify_handoff:
        from .observer_control import atomic_write
        QTimer.singleShot(250,lambda:atomic_write(args.verify_handoff,json.dumps(dict(
            pid=os.getpid(),version=VERSION,homes=homes,ready=True,hwnd=int(window.winId()),language=display_language,
            restart_enabled=window.settings_page.controls['restart'].isEnabled())).encode()))
    if not args.hidden or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show_window()
    if args.smoke:
        from .quick_smoke import start_smoke
        start_smoke(window,app,args.smoke,bundled_fonts,depth=args.smoke_depth)
    return app.exec()
