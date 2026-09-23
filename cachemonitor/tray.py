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
        if os.name != "nt":
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                return bool(winreg.QueryValueEx(key, "CacheMonitor")[0])
        except OSError:
            return False

    def set_startup(self, enabled):
        if os.name != "nt":
            return
        import subprocess
        import winreg
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                if enabled:
                    if getattr(sys, "frozen", False):
                        command = [sys.executable, "--hidden"]
                    else:
                        command = [str(Path(sys.executable).with_name("pythonw.exe")), str(Path(__file__).resolve().parents[1] / "run.py"), "--hidden"]
                    for home in self.snapshot["homes"]:
                        command.extend(["--codex-home", home])
                    winreg.SetValueEx(key, "CacheMonitor", 0, winreg.REG_SZ, subprocess.list2cmdline(command))
                else:
                    try:
                        winreg.DeleteValue(key, "CacheMonitor")
                    except FileNotFoundError:
                        pass
        except OSError as exc:
            QMessageBox.warning(self, "자동 시작 설정 실패", str(exc))
            self.startup.setChecked(self.startup_enabled())

    def quit_app(self):
        self.quitting = True
        if getattr(self,'overlay',None):self.overlay.stop()
        for service in getattr(self,'quota_services',{}).values(): service.stop()
        self.tick.stop()
        QApplication.instance().removeEventFilter(self)
        if hasattr(self,'defer_timer'): self.defer_timer.stop()
        if self.worker:
            self.worker.requestInterruption()
            self.worker.wait()
        self.tray.hide()
        self.taskbar_quota.close()
        self.release_scene()
        QApplication.instance().quit()
