"""Windows-owned, user-approved GUI restart; no resident watchdog or service owner."""
from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path
import subprocess
import sys


def use_software_renderer():
    """Select before creating any Quick scene, including translucent helper windows."""
    from PySide6.QtQuick import QQuickWindow
    os.environ['QT_QUICK_BACKEND'] = 'software'
    # Do not opt QWidget composition back into the D3D path through an inherited override.
    os.environ['QT_WIDGETS_RHI'] = '0'
    QQuickWindow.setSceneGraphBackend('software')


def restart_arguments(homes, *, index_path=None, evidence_path=None, quota_path=None,
                      frozen=None):
    """WER adds the executable; source runs must supply the absolute script path."""
    frozen = getattr(sys, 'frozen', False) if frozen is None else frozen
    arguments = [] if frozen else [str(Path(__file__).resolve().parents[1] / 'run.py')]
    arguments += ['--software-rendering', '--managed-services']
    for home in homes:
        arguments += ['--codex-home', str(Path(home).resolve())]
    for flag, path in (('--index-path', index_path), ('--evidence-path', evidence_path),
                       ('--quota-path', quota_path)):
        if path is not None:
            arguments += [flag, str(Path(path).resolve())]
    # Never inherit --hidden or --replace-gui: recovery opens the dashboard and
    # an independently relaunched GUI must not be displaced by a late WER action.
    return subprocess.list2cmdline(arguments)


class WindowsRestart:
    """WER may offer restart after 60s uptime; OS policy and user consent still apply."""
    def __init__(self, arguments, *, api=None):
        self.arguments = arguments
        self.active = False
        self.error = None
        self.api = api
        if self.api is None and sys.platform == 'win32':
            self.api = ctypes.WinDLL('kernel32', use_last_error=True)
            self.api.RegisterApplicationRestart.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            self.api.RegisterApplicationRestart.restype = ctypes.c_long
            self.api.UnregisterApplicationRestart.argtypes = []
            self.api.UnregisterApplicationRestart.restype = ctypes.c_long

    def _result(self, operation, result):
        self.error = None if result == 0 else f'{operation}: 0x{result & 0xffffffff:08x}'
        if self.error:
            logging.getLogger(__name__).warning('Windows restart registration failed: %s', self.error)
        return result == 0

    def arm(self):
        if self.api is None:
            return False
        # Crash/hang recovery only. Installation and Windows reboot keep their
        # existing, explicit ownership and startup preference semantics.
        self.active = self._result('register', self.api.RegisterApplicationRestart(self.arguments, 4 | 8))
        return self.active

    def disarm(self):
        if self.api is None or not self.active:
            return True
        if not self._result('unregister', self.api.UnregisterApplicationRestart()):
            return False
        self.active = False
        return True


def set_restart_enabled(enabled):
    """Called only after exit is accepted; cancellation must leave recovery armed."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    registration = getattr(app, '_windows_restart', None)
    if registration:
        return registration.arm() if enabled else registration.disarm()
    return True
