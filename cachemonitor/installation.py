"""Installed paths shared by the desktop app and the independent recovery tool."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

KEY = r'Software\Codexon'


def installed():
    if os.name != 'nt':
        return {}
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0]
                    for name in ('InstallRoot', 'AppPath', 'RecoveryPath')}
    except OSError:
        return {}


def recovery_command(home=None, directory=None, url=None, *, check=False):
    if getattr(sys, 'frozen', False):
        candidate = Path(installed().get('RecoveryPath', ''))
        if not candidate.is_file():
            candidate = Path(sys.executable).with_name('CodexonRecovery.exe')
        if not candidate.is_file():
            raise RuntimeError('연결 복구 도구가 없습니다. Codexon 설치 프로그램을 다시 실행하세요.')
        command = [str(candidate)]
    else:
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'recovery_main.py')]
    if check:
        command.append('--check')
    if home is not None:
        command += ['--codex-home', str(home)]
    if directory is not None:
        command += ['--data-dir', str(directory)]
    if url is not None:
        command += ['--proxy-url', url]
    return command


def open_recovery(home=None, directory=None, url=None):
    return subprocess.Popen(recovery_command(home, directory, url),
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
