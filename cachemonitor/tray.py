from __future__ import annotations
from PySide6.QtGui import QAction, QActionGroup, QCursor
from PySide6.QtCore import QThread, Qt
import os, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon
from .quick_runtime import QuickHost
from .quota import quota_display
from .icons import notification_icon, tray_icon
from .theme import shared_theme
from .i18n import tr, formatted
from .app_shutdown import ExitConnectionCheck, ExitConfirmation, ShutdownProgress
from .taskbar import TaskbarQuota
import time


class ChatGPTDiscovery(QThread):
    """Resolve the installed app away from the settings event loop."""
    def run(self):
        from .chatgpt_startup import discover_chatgpt
        try:self.result=discover_chatgpt()
        except Exception as error:self.result=error


class TrayWindow(QuickHost):
    def setup_tray(self):
        self.tray = QSystemTrayIcon(notification_icon(), self)
        menu = QMenu()
        menu.addAction(tr("대시보드 열기"), self.show_window)
        menu.addSeparator()
        self.quota_mode=self.settings.value('tray/quotaMode','weekly')
        if self.quota_mode not in ('weekly','five_hour'): self.quota_mode='weekly'
        self.quota_group=QActionGroup(self)
        self.quota_group.setExclusive(True)
        self.quota_actions={}
        for mode,title in (('weekly','주간 잔여량 표시'),('five_hour','5시간 잔여량 표시')):
            action=QAction(tr(title),self,checkable=True)
            action.setChecked(mode==self.quota_mode)
            self.quota_group.addAction(action)
            action.triggered.connect(lambda checked,m=mode:self.set_quota_mode(m))
            menu.addAction(action)
            self.quota_actions[mode]=action
        menu.addSeparator()
        self.taskbar_quota = TaskbarQuota(self.settings,activation_hint='클릭: 대시보드')
        self.taskbar_quota.activated.connect(self.show_window)
        self.taskbar_action = QAction(tr('작업표시줄 잔여량 위젯'), self, checkable=True)
        self.taskbar_action.setChecked(self.settings.value('taskbar/enabled', True, type=bool))
        self.taskbar_action.triggered.connect(self.set_taskbar_enabled)
        menu.addAction(self.taskbar_action)
        self.taskbar_quota.add_monitor_menu(menu)
        menu.addSeparator()
        self.startup = QAction(tr("Windows 로그인 시 시작"), self, checkable=True)
        self.startup.setChecked(self.startup_enabled())
        self.startup.triggered.connect(self.set_startup)
        from .chatgpt_startup import ENABLED_KEY
        self.chatgpt_discovery=None
        self.chatgpt_startup=QAction(tr('Windows 로그인 시 ChatGPT 실행'),self,checkable=True)
        self.chatgpt_startup.setChecked(self.settings.value(ENABLED_KEY,False,type=bool))
        self.chatgpt_startup.triggered.connect(self.set_chatgpt_startup)
        self.startup.changed.connect(self.sync_chatgpt_startup_enabled)
        self.sync_chatgpt_startup_enabled()
        menu.addAction(self.startup)
        menu.addAction(tr("종료"), self.quit_app)
        if hasattr(self, 'settings_page'):
            # Keep the same actions alive for the Settings page, but expose only
            # quick commands in the tray. Lite keeps its existing compact menu.
            actions = menu.actions()
            for action in actions[1:-1]: menu.removeAction(action)
            menu.insertSeparator(actions[-1])
            self.exit_action = actions[-1]
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: menu.popup(QCursor.pos()) if reason == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self.refresh_tray()
        self.taskbar_quota.set_enabled(self.taskbar_action.isChecked())

    def set_taskbar_enabled(self, enabled):
        self.settings.setValue('taskbar/enabled', bool(enabled))
        self.taskbar_action.setChecked(bool(enabled))
        self.taskbar_quota.set_enabled(enabled)

    def set_quota_mode(self,mode):
        self.quota_mode=mode
        self.settings.setValue('tray/quotaMode',mode)
        self.quota_actions[mode].setChecked(True)
        self.refresh_tray()

    def refresh_tray(self):
        quota=getattr(self,'live_quota',self.snapshot.get('quota_by_home',{}).get(self.observer_home))
        display=quota_display(quota,getattr(self,'quota_mode','weekly'),time.time())
        from .app_errors import strongest
        registry=getattr(self,'error_state',None)
        faults=registry.grouped() if registry is not None else {}
        principal=strongest(faults.values())
        if registry is None:error=bool(self.snapshot.get('errors')) or bool(getattr(self,'analysis_errors',[]))
        else:error=bool(principal and principal.severity=='error')
        warning=any(s.get('warning') for s in self.snapshot.get('sessions',[]))
        if display['remaining'] is not None and display['remaining']<10: warning=True
        if principal and principal.severity=='warning':warning=True
        signature=(shared_theme().dark,error,warning)
        if signature!=getattr(self,'_tray_icon_signature',None):
            self._tray_icon_signature=signature
            self.tray.setIcon(notification_icon(error=error,warning=warning))
        tip=display['tooltip']
        if principal:
            tip=tip.split('\n',1)[0]+'\n'+tr(principal.title)+'\n'+tr(principal.explanation)
            if len(faults)>1:tip+='\n'+formatted('추가 오류 {count}개',count=len(faults)-1)
        elif error:tip+='\n수집과 분석 상태 확인 필요'
        if len(self.snapshot.get('homes',[]))>1: tip+='\n한도 기준: 첫 번째 Codex 홈'
        if registry is None and getattr(self,'quota_issue',''):tip+='\n'+self.quota_issue
        if self.taskbar_quota.enabled and self.taskbar_quota.embedding_error:
            tip+='\n'+self.taskbar_quota.embedding_error
        self.tray.setToolTip(tr(tip))
        self.taskbar_quota.set_display(display, self.quota_mode, tip)

    def hide_to_tray(self):
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.hide()
        else:
            QMessageBox.information(self, "트레이를 사용할 수 없습니다", "현재 환경에는 시스템 트레이가 없어 창을 유지합니다.")

    def show_window(self):
        maximized=bool(self.windowState() & Qt.WindowMaximized)
        if maximized:self.showMaximized()
        else:self.showNormal()
        if sys.platform == "win32":
            import ctypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
            user32.ShowWindow.restype = ctypes.c_int
            # STARTUPINFO(SW_HIDE) can hide the first native show even when Qt reports visible.
            user32.ShowWindow(int(self.winId()), 3 if maximized else 9)
            from .windows_integration import recover_dashboard_position
            if not maximized:recover_dashboard_position(int(self.winId()))
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.quitting:
            for service in getattr(self,'quota_services',{}).values(): service.stop()
            self.taskbar_quota.close()
            QApplication.instance().removeEventFilter(self)
            if hasattr(self,'defer_timer'): self.defer_timer.stop()
            self.release_scene()
            event.accept()
        elif QSystemTrayIcon.isSystemTrayAvailable():
            event.ignore()
            self.hide()
        else:
            event.ignore()
            self.quit_app()

    @staticmethod
    def startup_enabled():
        if os.name != "nt":
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                return bool(winreg.QueryValueEx(key, "CacheMonitor")[0])
        except OSError:
            return False

    def set_startup(self, enabled, *, preserve_existing=False):
        if os.name != "nt":
            return False
        import subprocess
        import winreg
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                if enabled:
                    if preserve_existing:
                        try:existing=winreg.QueryValueEx(key,'CacheMonitor')[0]
                        except FileNotFoundError:existing=''
                        if existing:
                            from .launch_context import command_arguments
                            if '--windows-startup' not in command_arguments(existing):
                                winreg.SetValueEx(key,'CacheMonitor',0,winreg.REG_SZ,existing+' --windows-startup')
                            return True
                    if getattr(sys, "frozen", False):
                        command = [sys.executable, "--hidden", "--windows-startup"]
                    else:
                        command = [str(Path(sys.executable).with_name("pythonw.exe")), str(Path(__file__).resolve().parents[1] / "run.py"), "--hidden", "--windows-startup"]
                    for home in self.snapshot["homes"]:
                        command.extend(["--codex-home", home])
                    winreg.SetValueEx(key, "CacheMonitor", 0, winreg.REG_SZ, subprocess.list2cmdline(command))
                else:
                    try:
                        winreg.DeleteValue(key, "CacheMonitor")
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            QMessageBox.warning(self, tr("자동 시작 설정 실패"), str(exc))
            self.startup.setChecked(self.startup_enabled())
            return False
        return True

    def sync_chatgpt_startup_enabled(self):
        if self.chatgpt_discovery is not None and not self.startup.isChecked():
            from .chatgpt_startup import ENABLED_KEY
            self.chatgpt_discovery.cancelled=True
            self.chatgpt_discovery.requestInterruption()
            self.chatgpt_startup.setChecked(self.settings.value(ENABLED_KEY,False,type=bool))
        self.chatgpt_startup.setEnabled(os.name=='nt' and self.startup.isChecked() and self.chatgpt_discovery is None)

    def set_chatgpt_startup(self, enabled):
        from .chatgpt_startup import ENABLED_KEY,APP_ID_KEY
        if not enabled:
            self.settings.setValue(ENABLED_KEY,False)
            self.settings.remove(APP_ID_KEY)
            self.settings.sync()
            return
        if self.chatgpt_discovery is not None:return
        self.chatgpt_discovery=ChatGPTDiscovery(self)
        self.chatgpt_discovery.cancelled=False
        self.chatgpt_discovery.finished.connect(self.chatgpt_discovered)
        self.sync_chatgpt_startup_enabled()
        self.chatgpt_discovery.start()

    def chatgpt_discovered(self):
        from .chatgpt_startup import ENABLED_KEY,APP_ID_KEY
        operation=self.chatgpt_discovery;self.chatgpt_discovery=None
        result=operation.result;cancelled=operation.cancelled;operation.deleteLater()
        if getattr(self,'_closing',False):return
        if cancelled:
            self.sync_chatgpt_startup_enabled()
            return
        available=isinstance(result,str) and bool(result)
        enabled=available
        if enabled and self.startup.isChecked():
            enabled=self.set_startup(True,preserve_existing=True)
        self.settings.setValue(ENABLED_KEY,enabled)
        if enabled:self.settings.setValue(APP_ID_KEY,result)
        else:self.settings.remove(APP_ID_KEY)
        self.settings.sync()
        self.chatgpt_startup.setChecked(enabled)
        self.sync_chatgpt_startup_enabled()
        if not available:
            QMessageBox.warning(self,tr('ChatGPT 자동 시작 설정 실패'),
                tr('ChatGPT 설치를 확인하지 못했습니다. 설치 상태를 확인한 뒤 다시 켜세요.'))

    def quit_app(self,checked=False,*,handoff=False):
        if getattr(self,'_closing',False):return
        check=getattr(self,'exit_check',None)
        dialog=getattr(self,'exit_confirmation',None)
        if check:
            if handoff:self._exit_handoff=True
            return
        if handoff:
            if dialog:dialog.reject()
            self.begin_quit(handoff=True);return
        if dialog:return
        services=getattr(self,'app_services',None)
        manager=services.manager if services else None
        if manager is None:
            self.begin_quit();return
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QProgressDialog
        self.exit_check_dialog=QProgressDialog(tr('열린 연결 확인 중…'),'',0,0,self)
        self.exit_check_dialog.setWindowTitle(tr('Codexon 종료 확인'))
        self.exit_check_dialog.setCancelButton(None)
        self.exit_check_dialog.setWindowFlag(Qt.WindowCloseButtonHint,False)
        self.exit_check_dialog.setWindowModality(Qt.ApplicationModal)
        self.exit_check_dialog.setMinimumDuration(0)
        self.exit_check_dialog.show()
        self.exit_check=ExitConnectionCheck(manager,self)
        self.exit_check.finished.connect(self.exit_connections_checked)
        self.exit_check.start()

    def exit_connections_checked(self):
        check=self.exit_check;self.exit_check=None
        count=check.count;check.deleteLater()
        self.exit_check_dialog.close();self.exit_check_dialog.deleteLater()
        self.exit_check_dialog=None
        if getattr(self,'_exit_handoff',False):
            self._exit_handoff=False;self.begin_quit(handoff=True);return
        self._exit_force_supported=check.health.get('supports_force_shutdown') is True
        if count==0:
            self.begin_quit();return
        self.exit_confirmation=ExitConfirmation(check,self.snapshot,self)
        self.exit_confirmation.finished.connect(self.exit_decided)
        self.exit_confirmation.open()

    def exit_decided(self,result):
        dialog=self.exit_confirmation;self.exit_confirmation=None
        if dialog:dialog.deleteLater()
        if result in (1,2) and not getattr(self,'_closing',False):self.begin_quit(force=result==2)

    def begin_quit(self,*,handoff=False,force=False):
        if getattr(self,'_closing',False):return
        from .graphics_recovery import set_restart_enabled
        set_restart_enabled(False)
        self._closing=True
        monitor=getattr(self,'update_monitor',None)
        if monitor:monitor.stop()
        update=getattr(self,'update_panel',None)
        if update:
            update.timer.stop()
            if update.journal and update.journal.read().get('phase') in ('preparing','downloading'):
                update.journal.cancel()
        if hasattr(self,'save_preferences'):self.save_preferences()
        controller=getattr(self,'service_controller',None)
        if controller:controller.stop()
        panel=getattr(self,'observer_panel',None)
        if panel:
            panel.active=False;panel.pending=None;panel.manager.cancelled.set()
        if getattr(self,'overlay',None):self.overlay.stop()
        for service in getattr(self,'quota_services',{}).values(): service.stop()
        self.tick.stop()
        QApplication.instance().removeEventFilter(self)
        if hasattr(self,'defer_timer'): self.defer_timer.stop()
        if self.worker:
            self.worker.requestInterruption()
        managed=getattr(self,'manage_observer',False)
        collection=bool(getattr(self,'app_services',None) and self.app_services.collection)
        discovery=getattr(self,'chatgpt_discovery',None)
        if handoff or not (managed or collection):
            if discovery:discovery.wait()
            if update and update.operation:update.operation.wait()
            if controller and controller.operation:controller.operation.wait()
            if self.worker:self.worker.wait()
            self.finish_quit();return
        from .app_shutdown import ShutdownOperation
        self.shutdown_dialog=ShutdownProgress(self,getattr(self,'_exit_force_supported',False))
        self.shutdown_dialog.show()
        update=getattr(self,'update_panel',None)
        workers=[self.worker,panel.operation if panel else None,update.operation if update else None,
                 controller.operation if controller else None,discovery]
        self.shutdown_operation=ShutdownOperation(self.app_services,workers,self)
        self.shutdown_dialog.force.clicked.connect(self.shutdown_operation.request_force)
        if force:
            self.shutdown_operation.request_force()
            self.shutdown_dialog.force.setEnabled(False)
        self.shutdown_operation.progress.connect(lambda text:self.shutdown_dialog.setLabelText(tr(text)))
        self.shutdown_operation.finished.connect(self.shutdown_finished)
        self.shutdown_operation.start()

    def shutdown_finished(self):
        error=self.shutdown_operation.error
        self.shutdown_dialog.accept()
        self.shutdown_dialog.deleteLater()
        if error:
            self._closing=False
            from .graphics_recovery import set_restart_enabled
            set_restart_enabled(True)
            QMessageBox.warning(self,tr('안전한 종료 확인 필요'),
                tr('종료를 완료하지 못했습니다. 종료를 다시 누르면 재시도합니다.')+'\n'+error)
            return
        self.finish_quit()

    def finish_quit(self):
        self.quitting=True
        self.tray.hide()
        self.taskbar_quota.close()
        self.release_scene()
        QApplication.instance().quit()
