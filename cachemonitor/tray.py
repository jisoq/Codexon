from __future__ import annotations
from PySide6.QtGui import QAction, QActionGroup, QCursor
import os, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon
from .quick_runtime import QuickHost
from .quota import quota_display
from .icons import tray_icon
from .i18n import tr
from .taskbar import TaskbarQuota
import time



class TrayWindow(QuickHost):
    def setup_tray(self):
        native_mac = (sys.platform == 'darwin'
                      and QApplication.platformName() == 'cocoa'
                      and not QApplication.instance().property('cachemonitorDisableShellIntegration'))
        if native_mac:
            from .macos_status import MacStatusTray
            self.tray = MacStatusTray(tray_icon(), self)
        else:
            self.tray = QSystemTrayIcon(tray_icon(), self)
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
        if sys.platform == 'darwin':
            from .macos_status import MacQuotaIndicator
            self.taskbar_quota = MacQuotaIndicator(self.settings, self.tray, self)
        else:
            self.taskbar_quota = TaskbarQuota(self.settings,activation_hint='클릭: 대시보드')
        self.taskbar_quota.activated.connect(self.show_window)
        self.taskbar_action = QAction(tr('메뉴 막대 잔여량 표시' if sys.platform == 'darwin' else '작업표시줄 잔여량 위젯'), self, checkable=True)
        self.taskbar_action.setChecked(self.settings.value('taskbar/enabled', True, type=bool))
        self.taskbar_action.triggered.connect(self.set_taskbar_enabled)
        menu.addAction(self.taskbar_action)
        self.taskbar_quota.add_monitor_menu(menu)
        menu.addSeparator()
        self.startup = QAction(tr('로그인 시 시작' if sys.platform == 'darwin' else 'Windows 로그인 시 시작'), self, checkable=True)
        self._startup_last_verified = None
        try:
            self._startup_last_verified = self.startup_enabled()
        except (OSError, RuntimeError):
            self.startup_unavailable()
        self.startup.setChecked(bool(self._startup_last_verified))
        self.startup.triggered.connect(self.set_startup)
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
        if not native_mac:
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
        quota=getattr(self,'live_quota',self.snapshot.get('quota'))
        display=quota_display(quota,getattr(self,'quota_mode','weekly'),time.time())
        error=bool(self.snapshot.get('errors')) or bool(getattr(self,'analysis_errors',[]))
        warning=any(s.get('warning') for s in self.snapshot.get('sessions',[]))
        if display['remaining'] is not None and display['remaining']<10: warning=True
        signature=(display['text'],error,warning)
        if signature!=getattr(self,'_quota_icon_signature',None):
            self._quota_icon_signature=signature
            self.tray.setIcon(tray_icon(error=error,warning=warning,text=display['text']))
        tip=display['tooltip']
        if error: tip+='\n수집·분석 상태 확인 필요'
        if len(self.snapshot.get('homes',[]))>1: tip+='\n한도 기준: 첫 번째 Codex 홈'
        if getattr(self,'quota_issue',''): tip+='\n'+self.quota_issue
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
        self.showNormal()
        if sys.platform == "win32":
            import ctypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.ShowWindow.argtypes = (ctypes.c_void_p, ctypes.c_int)
            user32.ShowWindow.restype = ctypes.c_int
            # STARTUPINFO(SW_HIDE) can hide the first native show even when Qt reports visible.
            user32.ShowWindow(int(self.winId()), 9)  # SW_RESTORE
            from .windows_integration import recover_dashboard_position
            recover_dashboard_position(int(self.winId()))
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
        from .windows_integration import WindowsStartup
        return WindowsStartup('CacheMonitor').enabled()

    def startup_unavailable(self):
        self.startup.setEnabled(False)
        self.startup.setText(tr('자동 시작 상태 확인 실패'))
        note=tr('자동 시작 상태를 확인하지 못해 설정을 잠갔습니다. 앱을 다시 열어 확인하세요.')
        self.startup.setStatusTip(note)
        self.startup.setToolTip(note)

    def set_startup(self, enabled):
        from .windows_integration import WindowsStartup
        try:
            if getattr(sys, 'frozen', False):
                command = [sys.executable, '--hidden']
            else:
                executable = str(Path(sys.executable).with_name('pythonw.exe')) if os.name == 'nt' else sys.executable
                command = [executable, str(Path(__file__).resolve().parents[1] / 'run.py'), '--hidden']
            for home in self.snapshot['homes']:
                command.extend(['--codex-home', home])
            WindowsStartup('CacheMonitor').set_enabled(enabled, command)
            self._startup_last_verified = bool(enabled)
        except (OSError, RuntimeError) as exc:
            QMessageBox.warning(self, "자동 시작 설정 실패", str(exc))
            try:
                self._startup_last_verified = self.startup_enabled()
            except (OSError, RuntimeError):
                self.startup_unavailable()
            self.startup.setChecked(bool(self._startup_last_verified))

    def quit_app(self,checked=False,*,handoff=False):
        if getattr(self,'_closing',False):return
        self._closing=True
        if hasattr(self,'save_preferences'):self.save_preferences()
        panel=getattr(self,'observer_panel',None)
        if panel:
            panel.active=False;panel.timer.stop();panel.pending=None;panel.manager.cancelled.set()
        if getattr(self,'cache_panel',None):self.cache_panel.stop()
        if getattr(self,'overlay',None):self.overlay.stop()
        for service in getattr(self,'quota_services',{}).values(): service.stop()
        self.tick.stop()
        QApplication.instance().removeEventFilter(self)
        if hasattr(self,'defer_timer'): self.defer_timer.stop()
        if self.worker:
            self.worker.requestInterruption()
        managed=getattr(self,'manage_observer',False)
        collection=bool(self.worker and self.worker.collection_autostart)
        if handoff or not (managed or collection):
            if self.worker:self.worker.wait()
            self.finish_quit();return
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QProgressDialog
        from .app_shutdown import ShutdownOperation
        self.shutdown_dialog=QProgressDialog(tr('관련 작업 마무리 중…'),'',0,0,self)
        self.shutdown_dialog.setWindowTitle(tr('Codexon 종료 중'))
        self.shutdown_dialog.setCancelButton(None)
        self.shutdown_dialog.setWindowFlag(Qt.WindowCloseButtonHint,False)
        self.shutdown_dialog.setWindowModality(Qt.ApplicationModal)
        self.shutdown_dialog.setMinimumDuration(0)
        self.shutdown_dialog.show()
        update=getattr(self,'update_panel',None)
        workers=[self.worker,panel.operation if panel else None,update.operation if update else None]
        self.shutdown_operation=ShutdownOperation(panel.manager if managed else None,
            self.snapshot['homes'],self.index_path,self.model_evidence_path,workers,self)
        self.shutdown_operation.progress.connect(lambda text:self.shutdown_dialog.setLabelText(tr(text)))
        self.shutdown_operation.finished.connect(self.shutdown_finished)
        self.shutdown_operation.start()

    def shutdown_finished(self):
        error=self.shutdown_operation.error
        self.shutdown_dialog.close()
        if error:
            self._closing=False
            QMessageBox.warning(self,tr('안전한 종료 확인 필요'),
                tr('진행 중 작업을 강제로 끊지 않았습니다. 종료를 다시 누르면 이어서 확인합니다.')+'\n'+error)
            return
        self.finish_quit()

    def finish_quit(self):
        self.quitting=True
        self.tray.hide()
        self.taskbar_quota.close()
        self.release_scene()
        QApplication.instance().quit()
