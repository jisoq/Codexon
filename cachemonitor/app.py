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
    parser.add_argument("--codex-home", action="append", help="Repeat to monitor multiple local Codex homes")
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--index-path", help="Override the app-owned usage index for isolated verification")
    parser.add_argument("--snapshot", action="store_true", help="Print sanitized live observations without starting the UI")
    parser.add_argument("--smoke", metavar="PNG", help="Render actual data, check tray lifecycle and exit")
    parser.add_argument("--smoke-depth", choices=('core','full'), default='full',
                        help="Choose the compact release check or full interaction probe")
    args = parser.parse_args()
    homes = args.codex_home or [os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))]
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
    display_language=os.environ.get('CODEXON_LANGUAGE') or QSettings('CacheMonitor','CacheMonitor').value('ui/language','ko')
    set_language(display_language)
    QLocale.setDefault(QLocale('en_US' if display_language=='en' else 'ko_KR'))
    app.setProperty('cachemonitorDisableShellIntegration', bool(args.smoke))
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
        client = QLocalSocket()
        client.connectToServer(server_name)
        if client.waitForConnected(400):
            client.write(b"show")
            client.waitForBytesWritten(400)
            return 0
        QLocalServer.removeServer(server_name)
        if not server.listen(server_name):
            QMessageBox.warning(None, "Codexon", tr("실행 중인 앱 상태를 확인할 수 없습니다."))
            return 1
    smoke_settings = None
    if args.smoke:
        smoke_directory = tempfile.TemporaryDirectory(prefix='cachemonitor-qa-')
        smoke_settings = QSettings(str(Path(smoke_directory.name)/'settings.ini'),QSettings.IniFormat)
        if args.index_path is None:args.index_path=str(Path(smoke_directory.name)/'index.sqlite')
    window = Dashboard(homes,index_path=args.index_path,live_limits=not args.smoke,manage_observer=not args.smoke and args.index_path is None, **({'settings':smoke_settings} if smoke_settings else {}))
    from .overlay import install_overlay
    install_overlay(window, native_enabled=not args.smoke)
    app.aboutToQuit.connect(window.observer_panel.stop)

    def connection():
        client = server.nextPendingConnection()
        if client:
            client.disconnectFromServer()
            client.deleteLater()
        window.show_window()

    server.newConnection.connect(connection)
    if not args.hidden or not QSystemTrayIcon.isSystemTrayAvailable():
        window.show_window()
    if args.smoke:
        from .quick_smoke import start_smoke
        start_smoke(window,app,args.smoke,bundled_fonts,depth=args.smoke_depth)
    return app.exec()
