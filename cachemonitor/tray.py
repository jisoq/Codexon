from __future__ import annotations
from PySide6.QtGui import QAction, QActionGroup, QCursor
from PySide6.QtCore import QThread, QTimer, Qt
import os, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon
from .quick_runtime import QuickHost
from .quota import quota_display
from .icons import notification_icon, tray_icon
from .theme import shared_theme
from .i18n import tr, formatted
from .app_shutdown import ShutdownProgress
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

    def quit_app(self,checked=False,*,handoff=False,handoff_deadline=None):
        if getattr(self,'_closing',False):
            dialog=getattr(self,'shutdown_dialog',None)
            if dialog:dialog.raise_();dialog.activateWindow()
            return
        self.begin_quit(handoff=handoff,handoff_deadline=handoff_deadline)

    def set_exit_pending(self,pending):
        self._closing=bool(pending)
        controller=getattr(self,'service_controller',None)
        if controller:controller.set_exit_pending(pending)
        panel=getattr(self,'observer_panel',None)
        if panel:panel.set_exit_pending(pending)
        update=getattr(self,'update_panel',None)
        if update:update.set_exit_pending(pending)
        if not pending and hasattr(self,'chatgpt_startup'):self.sync_chatgpt_startup_enabled()
        settings=getattr(self,'settings_page',None)
        restart=getattr(settings,'controls',{}).get('restart')
        if restart:
            if pending:restart.setEnabled(False)
            else:settings.refresh_restart()

    def begin_quit(self,*,handoff=False,handoff_deadline=None):
        if getattr(self,'_closing',False):return
        from .graphics_recovery import set_restart_enabled
        from .app_shutdown import ShutdownOperation
        set_restart_enabled(False)
        self.set_exit_pending(True)
        self.shutdown_dialog=ShutdownProgress(self,handoff=handoff)
        if handoff:self.shutdown_dialog.setWindowTitle(tr('Codexon 다시 시작 중'))
        self.shutdown_dialog.show()
        panel=getattr(self,'observer_panel',None)
        update=getattr(self,'update_panel',None)
        controller=getattr(self,'service_controller',None)
        workers=[getattr(panel,'operation',None),getattr(update,'operation',None),
                 getattr(controller,'operation',None),getattr(self,'chatgpt_discovery',None)]
        display=[getattr(self,'worker',None),*getattr(self,'quota_services',{}).values()]
        self.shutdown_operation=ShutdownOperation(getattr(self,'app_services',None),workers,self,
            handoff=handoff,display_workers=display,handoff_deadline=handoff_deadline)
        self.shutdown_dialog.cancel_requested.connect(self.cancel_quit)
        self.shutdown_dialog.force_requested.connect(self.force_quit)
        self.shutdown_operation.progress.connect(self.shutdown_dialog.display)
        self.shutdown_operation.finished.connect(self.shutdown_finished)
        self.shutdown_operation.start()

    def cancel_quit(self):
        if self.shutdown_operation.request_cancel():self.shutdown_dialog.cancellation_requested()
        else:self.shutdown_dialog.cancel.setEnabled(False)

    def force_quit(self):
        if self.shutdown_dialog.force.isEnabled() and self.shutdown_operation.request_force():
            self.shutdown_dialog.forcing_requested()

    def resume_display_workers(self):
        def resume_when_ready(worker,restore):
            if getattr(worker,'_exit_restore_pending',False):return
            worker._exit_restore_pending=True
            def ready():
                if self.quitting:return
                if getattr(self,'_closing',False):
                    QTimer.singleShot(100,ready);return
                worker._exit_restore_pending=False;restore()
            if worker.isRunning():
                def finished():
                    worker.finished.disconnect(finished);ready()
                worker.finished.connect(finished)
            else:ready()
        worker=getattr(self,'worker',None)
        if worker:resume_when_ready(worker,worker.start)
        for home,service in list(getattr(self,'quota_services',{}).items()):
            def resume_quota(home=home,service=service):
                if self.quota_services.get(home) is not service:return
                selected=getattr(getattr(self,'quota_service',None),'home',home)
                del self.quota_services[home];self.start_quota_service(home)
                if selected!=home and selected in self.quota_services:self.start_quota_service(selected)
            resume_when_ready(service,resume_quota)

    def shutdown_finished(self):
        operation=self.shutdown_operation
        outcome=operation.outcome
        if outcome.status=='completed' and operation.handoff_expired():
            from .shutdown_state import ExitResult
            outcome=ExitResult('failed',committed=outcome.committed,
                error='앱 다시 시작 대기 시간이 지났습니다. 기존 Codexon을 계속 사용할 수 있습니다.')
        self.shutdown_dialog.accept();self.shutdown_dialog.deleteLater();self.shutdown_dialog=None
        self.shutdown_operation=None;operation.deleteLater()
        if outcome.status!='completed':
            if operation.display_stopping and not operation.handoff:self.app_services.release_exit()
            self.set_exit_pending(False)
            if operation.display_stopping:self.resume_display_workers()
            from .graphics_recovery import set_restart_enabled
            set_restart_enabled(True)
            if outcome.status=='failed':
                QMessageBox.warning(self,tr('안전한 종료 확인 필요'),
                    tr('종료를 완료하지 못했습니다. 현재 상태를 확인한 뒤 종료를 다시 시도할 수 있습니다.')+'\n'+tr(outcome.error))
            return
        if outcome.warning:
            QMessageBox.warning(self,tr('기록 저장 확인 필요'),tr(outcome.warning))
        self.finish_quit()

    def finish_quit(self):
        if hasattr(self,'save_preferences'):self.save_preferences()
        monitor=getattr(self,'update_monitor',None)
        if monitor:monitor.stop()
        update=getattr(self,'update_panel',None)
        if update:update.stop()
        controller=getattr(self,'service_controller',None)
        if controller:controller.stop()
        panel=getattr(self,'observer_panel',None)
        if panel:panel.stop()
        if getattr(self,'overlay',None):self.overlay.stop()
        for service in getattr(self,'quota_services',{}).values():service.stop()
        self.tick.stop()
        QApplication.instance().removeEventFilter(self)
        if hasattr(self,'defer_timer'):self.defer_timer.stop()
        self.quitting=True
        self.tray.hide();self.taskbar_quota.close();self.release_scene()
        QApplication.instance().quit()
