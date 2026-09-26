"""Installed paths shared by the desktop app and the independent recovery tool."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

KEY = r'Software\Codexon'


def pointer_path():
    # Registry/AppData overlays in a packaged caller can retain an old install.
    return Path.home()/'.cachemonitor'/'installation.json'


def valid_paths(value):
    try:
        root=Path(value['InstallRoot']).resolve()
        app=Path(value['AppPath']).resolve();recovery=Path(value['RecoveryPath']).resolve()
        return (app.name=='Codexon.exe' and recovery.name=='CodexonRecovery.exe'
                and app.is_relative_to(root/'versions') and recovery.is_relative_to(root/'maintenance')
                and app.is_file() and recovery.is_file())
    except (KeyError,TypeError,OSError,ValueError):return False


def installed():
    if os.name != 'nt':
        return {}
    from .observer_state import read_json
    shared=read_json(pointer_path())
    if valid_paths(shared):return shared
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as key:
            value={name: winreg.QueryValueEx(key, name)[0]
                   for name in ('InstallRoot', 'AppPath', 'RecoveryPath')}
    except OSError:
        return {}
    # Migrate readers of installations that predate the shared pointer.
    receipt=read_json(Path(value['InstallRoot'])/'installation.json')
    if receipt.get('product') and receipt.get('recovery'):
        current=dict(InstallRoot=value['InstallRoot'],AppPath=str(Path(receipt['product'])/'Codexon.exe'),
                     RecoveryPath=receipt['recovery'])
        if valid_paths(current):return current
    return value


def recovery_command(home=None, directory=None, url=None):
    if getattr(sys, 'frozen', False):
        candidate = Path(installed().get('RecoveryPath', ''))
        if not candidate.is_file():
            candidate = Path(sys.executable).with_name('CodexonRecovery.exe')
        if not candidate.is_file():
            raise RuntimeError('연결 복구 도구가 없습니다. Codexon 설치 프로그램을 다시 실행하세요.')
        command = [str(candidate)]
    else:
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'recovery_main.py')]
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
