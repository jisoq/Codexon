import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows startup visibility regression")
def test_hidden_startup_double_click_really_shows_native_window(tmp_path):
    code = r'''
import ctypes
import json
import sys
from pathlib import Path
from PySide6.QtCore import QRect, QSettings, QTimer, Qt
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget
import cachemonitor.app as ui

root = Path(sys.argv[1])
ui.QSettings = lambda *_: QSettings(str(root / "settings.ini"), QSettings.IniFormat)
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
# Exercise real native parenting without requiring Explorer on the QA desktop.
from cachemonitor.taskbar import NativeTaskbar
host_window = QWidget(None, Qt.Tool | Qt.WindowDoesNotAcceptFocus)
host_window.resize(600, 48)
host_window.show()
host_id = int(host_window.winId())
NativeTaskbar.host = lambda self: host_id
NativeTaskbar.dock_geometry = lambda self, host, ratio: QRect(450, 6, 100, 36)
window = ui.Dashboard([str(root)], start_worker=False)
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.IsWindowVisible.argtypes = (ctypes.c_void_p,)
user32.IsWindowVisible.restype = ctypes.c_int
user32.IsIconic.argtypes = (ctypes.c_void_p,)
user32.IsIconic.restype = ctypes.c_int

def visible():
    return bool(user32.IsWindowVisible(int(window.winId())))

def check_window():
    result = {"initially_hidden": not visible()}
    indicator = window.taskbar_quota
    result["taskbar_native_visible_at_hidden_startup"] = bool(user32.IsWindowVisible(int(indicator.winId())))
    native = indicator.native
    host = native.host()
    result["taskbar_is_native_child"] = native.api.GetParent(int(indicator.winId())) == host
    result["taskbar_position_inside_primary_taskbar"] = native.rect(host).contains(native.rect(int(indicator.winId())))
    window.tray.activated.emit(QSystemTrayIcon.Trigger)
    result["single_click_keeps_hidden"] = not visible()
    window.tray.contextMenu().close(); window.tray.contextMenu().actions()[0].trigger()
    result["open_action_native_visible"] = visible()
    window.hide()
    result["taskbar_stays_visible_when_dashboard_hides"] = bool(user32.IsWindowVisible(int(indicator.winId())))
    window.tray.contextMenu().close(); window.tray.contextMenu().actions()[0].trigger()
    result["reopen_native_visible"] = visible()
    window.showMinimized()
    window.tray.contextMenu().close(); window.tray.contextMenu().actions()[0].trigger()
    result["restored_from_minimized"] = visible() and not user32.IsIconic(int(window.winId()))
    print(json.dumps(result), flush=True)
    window.tick.stop()
    window.tray.hide()
    window.quitting = True
    window.close()
    host_window.close()
    app.quit()

def check():
    try:
        check_window()
    except Exception:
        import traceback
        traceback.print_exc()
        app.exit(1)

QTimer.singleShot(100, check)
sys.exit(app.exec())
'''
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        startupinfo=startup, cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=20,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    visibility = json.loads(result.stdout)
    assert all(visibility.values()), visibility
@pytest.mark.skipif(sys.platform != 'win32',reason='Windows Task Scheduler')
def test_installer_retires_only_owned_gui_logon_tasks(tmp_path):
    from cachemonitor.observer_task import ObserverTask,retire_desktop_startups
    root=tmp_path/'installation';exe=root/'versions'/'old'/'Codexon.exe'
    outside=tmp_path/'other-installation'/'versions'/'old'/'Codexon.exe'
    desktop=ObserverTask(str(exe),role='Desktop');other=ObserverTask(str(outside),role='Desktop')
    worker=ObserverTask(str(exe),role='CacheWorker')
    try:
        for task,path in ((desktop,exe),(worker,exe),(other,outside)):
            path.parent.mkdir(parents=True,exist_ok=True)
            task.configure([str(path),'--hidden'],autostart=True)
        result=retire_desktop_startups(root)
        assert result['retired']==[desktop.name]
        assert not desktop.inspect()['autostart']
        assert not worker.inspect()['autostart'] and other.inspect()['autostart']
        worker.suspend()
        suspended=worker.inspect()
        assert not suspended['enabled'] and not suspended['autostart'] and suspended['restartCount']==0
        assert other.inspect()['autostart']
        assert retire_desktop_startups(root)['retired']==[]
    finally:
        for task in (desktop,worker,other):task.remove()
