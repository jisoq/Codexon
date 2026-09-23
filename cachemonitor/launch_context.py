"""Qt-free launch preferences shared by all installed entry points."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .observer_control import atomic_write
from .observer_state import read_json


def preference_path():
    return Path(os.environ.get('LOCALAPPDATA', Path.home()))/'CacheMonitor'/'launch.json'


def resolve_homes(explicit=None, *, path=None):
    saved = read_json(path or preference_path()).get('homes')
    if not isinstance(saved, list) or not all(isinstance(h, str) and h for h in saved):
        saved = None
    return list(explicit or saved or [os.environ.get('CODEX_HOME') or str(Path.home()/'.codex')])


def save_homes(homes, *, path=None):
    path = Path(path or preference_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({'homes': [str(Path(h).resolve()) for h in homes]}, ensure_ascii=False).encode())


def command_arguments(command):
    """Parse legacy Windows launch arguments without losing spaces or repeats."""
    import ctypes
    from ctypes import wintypes as W
    shell = ctypes.WinDLL('shell32')
    shell.CommandLineToArgvW.argtypes = [W.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(W.LPWSTR)
    kernel = ctypes.WinDLL('kernel32')
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:raise OSError('Cannot read existing launch arguments')
    try:args = list(argv[:count.value])
    finally:kernel.LocalFree(argv)
    return args


def homes_from_command(command):
    args=command_arguments(command)
    homes = []
    for i, arg in enumerate(args):
        if arg == '--codex-home' and i+1 < len(args):homes.append(args[i+1])
        elif arg.startswith('--codex-home='):homes.append(arg.split('=', 1)[1])
    return homes


def running_homes(root):
    """Migrate explicit homes from an older GUI before asking it to exit."""
    from .install_management import processes_under
    for process in processes_under(root):
        if Path(process['ExecutablePath']).name.lower() != 'codexon.exe':continue
        command = process.get('CommandLine') or ''
        arguments=command_arguments(command)
        if any(flag in arguments for flag in ('--model-proxy','--proxy-supervisor','--proxy-update',
                '--verify-runtime','--complete-install','--smoke','--snapshot','--index-path')):continue
        homes = homes_from_command(command)
        if homes:return homes
    return None
