import ctypes
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cachemonitor.graphics_recovery import WindowsRestart, restart_arguments


def test_restart_preserves_paths_without_replaying_handoff(tmp_path):
    homes = [tmp_path/'한글 홈', tmp_path/'second home']
    arguments = restart_arguments(homes, index_path=tmp_path/'index.sqlite',
        evidence_path=tmp_path/'evidence.sqlite', quota_path=tmp_path/'quota.sqlite', frozen=True)
    expected = ['--software-rendering', '--managed-services']
    for home in homes:
        expected += ['--codex-home', str(home.resolve())]
    for flag, name in (('--index-path', 'index.sqlite'), ('--evidence-path', 'evidence.sqlite'),
                       ('--quota-path', 'quota.sqlite')):
        expected += [flag, str((tmp_path/name).resolve())]
    assert arguments == subprocess.list2cmdline(expected)
    assert '--hidden' not in arguments and '--replace-gui' not in arguments
    source = restart_arguments(homes, frozen=False)
    assert source.startswith(subprocess.list2cmdline([str(Path(__file__).resolve().parents[1]/'run.py')]))


def test_registration_failure_is_visible_and_does_not_claim_protection():
    registration = WindowsRestart('x'*1100, api=SimpleNamespace(
        RegisterApplicationRestart=lambda *_: -2147024809))
    assert not registration.arm()
    assert not registration.active and '0x80070057' in registration.error
    assert registration.disarm()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows restart API')
def test_native_restart_registration_roundtrip_and_unregister(tmp_path):
    api = ctypes.WinDLL('kernel32')
    api.GetCurrentProcess.restype = ctypes.c_void_p
    api.GetApplicationRestartSettings.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
    api.GetApplicationRestartSettings.restype = ctypes.c_long
    def read():
        buffer = ctypes.create_unicode_buffer(1024)
        length = ctypes.c_uint32(len(buffer));flags = ctypes.c_uint32()
        result = api.GetApplicationRestartSettings(api.GetCurrentProcess(), buffer,
                                                   ctypes.byref(length), ctypes.byref(flags))
        return result, buffer.value, flags.value
    registration = WindowsRestart(restart_arguments([tmp_path/'한글 home'], frozen=True))
    try:
        assert registration.arm(), registration.error
        result, command, flags = read()
        assert result == 0 and command == registration.arguments and flags == 12
        assert registration.disarm()
        assert read()[0] & 0xffffffff == 0x80070490  # ERROR_NOT_FOUND
    finally:
        registration.disarm()


@pytest.mark.parametrize('handoff', [False, True])
def test_accepted_exit_disarms_before_scene_teardown_and_cancel_does_not(tmp_path, handoff):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app = QApplication.instance() or QApplication([])
    events = []
    previous = getattr(app, '_windows_restart', None)
    registration = SimpleNamespace(disarm=lambda: events.append('disarm'), arm=lambda: events.append('arm'))
    app._windows_restart = registration
    window = Dashboard([], start_worker=False, settings=QSettings(str(tmp_path/'ui.ini'), QSettings.IniFormat))
    try:
        window.exit_confirmation = None
        window.exit_decided(0)
        assert events == []
        original_finish = window.finish_quit
        def finish():
            events.append('teardown')
            original_finish()
        window.finish_quit = finish
        window.begin_quit(handoff=handoff)
        assert events == ['disarm', 'teardown']
    finally:
        app._windows_restart = previous
        window.quitting = True;window.release_scene();window.close();window.deleteLater();app.processEvents()


def test_shutdown_failure_rearms_restart(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from cachemonitor.tray import TrayWindow, QMessageBox
    app = QApplication.instance() or QApplication([])
    events = []
    previous = getattr(app, '_windows_restart', None)
    app._windows_restart = SimpleNamespace(arm=lambda: events.append('arm'))
    monkeypatch.setattr(QMessageBox, 'warning', lambda *_: events.append('warning'))
    window = SimpleNamespace(_closing=True, shutdown_operation=SimpleNamespace(error='failed'),
        shutdown_dialog=SimpleNamespace(accept=lambda: None, deleteLater=lambda: None))
    try:
        TrayWindow.shutdown_finished(window)
        assert not window._closing and events == ['arm', 'warning']
    finally:
        app._windows_restart = previous


def test_software_renderer_uses_cpu_even_with_inherited_d3d_override(tmp_path):
    code = '''
from cachemonitor.graphics_recovery import use_software_renderer
use_software_renderer()
from PySide6.QtWidgets import QApplication
from PySide6.QtQuick import QSGRendererInterface
from cachemonitor.quick_runtime import QuickHost
from cachemonitor.presentation import Text
app = QApplication([])
host = QuickHost();host.resize(240,100)
host.set_scene(Text('CPU renderer'), 'Main.qml');host.show()
app.processEvents()
assert host.quick.quickWindow().rendererInterface().graphicsApi() == QSGRendererInterface.Software
assert not host.quick.grabFramebuffer().isNull()
assert not host.qml_errors
host.release_scene();host.close()
'''
    result = subprocess.run([sys.executable, '-B', '-c', code], cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, QT_QUICK_BACKEND='rhi', QSG_RHI_BACKEND='d3d11', QT_WIDGETS_RHI='1'),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
