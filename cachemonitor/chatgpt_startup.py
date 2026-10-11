"""Discover and activate the current user's registered ChatGPT Windows app."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import subprocess
import sys

from .shell_shortcut import GUID


ENABLED_KEY = 'startup/chatgptEnabled'
APP_ID_KEY = 'startup/chatgptAppId'
# Package family IDs are stable across Store updates and include OpenAI's publisher ID.
_APP_IDS = ('OpenAI.Codex_2p2nqsd0c76g0!App',
            'OpenAI.ChatGPT-Desktop_2p2nqsd0c76g0!App')


def _require_windows():
    if sys.platform != 'win32':
        raise OSError('ChatGPT 자동 실행은 Windows에서만 지원합니다.')


def discover_chatgpt():
    """Resolve once when enabling the option, never on the login launch path."""
    _require_windows()
    script = ("$ErrorActionPreference='Stop'; "
              "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
              "@(Get-StartApps | Where-Object { $_.Name -eq 'ChatGPT' } | "
              "Select-Object -ExpandProperty AppID) | ConvertTo-Json -Compress")
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('ChatGPT 설치 확인 시간이 초과되었습니다. 잠시 후 다시 시도하세요.') from exc
    if result.returncode:
        raise RuntimeError('Windows에서 ChatGPT 설치 정보를 읽지 못했습니다.')
    try:
        app_ids = json.loads(result.stdout.decode('utf-8-sig') or '[]')
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError('Windows의 ChatGPT 설치 정보를 해석하지 못했습니다.') from exc
    if isinstance(app_ids, str):
        app_ids = [app_ids]
    if isinstance(app_ids, list):
        for app_id in _APP_IDS:
            if app_id in app_ids:
                return app_id
    raise RuntimeError('현재 Windows 계정에 공식 ChatGPT 앱을 설치한 뒤 다시 켜세요.')


def _process_api():
    api = C.WinDLL('kernel32', use_last_error=True)
    api.K32EnumProcesses.argtypes = [C.POINTER(W.DWORD), W.DWORD, C.POINTER(W.DWORD)]
    api.ProcessIdToSessionId.argtypes = [W.DWORD, C.POINTER(W.DWORD)]
    api.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    api.OpenProcess.restype = W.HANDLE
    api.GetApplicationUserModelId.argtypes = [W.HANDLE, C.POINTER(W.UINT), W.LPWSTR]
    api.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    api.CloseHandle.argtypes = [W.HANDLE]
    return api


def _already_running(app_id):
    """Match package identity and session, including apps with no visible window."""
    api = _process_api()
    own_session = W.DWORD()
    if not api.ProcessIdToSessionId(os.getpid(), C.byref(own_session)):
        raise OSError('현재 Windows 로그인 세션을 확인하지 못했습니다.')
    capacity = 1024
    while True:
        pids = (W.DWORD * capacity)()
        used = W.DWORD()
        if not api.K32EnumProcesses(pids, C.sizeof(pids), C.byref(used)):
            raise OSError('실행 중인 ChatGPT를 확인하지 못했습니다.')
        if used.value < C.sizeof(pids):
            break
        capacity *= 2
    for pid in pids[:used.value // C.sizeof(W.DWORD)]:
        session = W.DWORD()
        if not pid or not api.ProcessIdToSessionId(pid, C.byref(session)) or session.value != own_session.value:
            continue
        process = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not process:
            continue  # A process may exit or be inaccessible while enumerating.
        try:
            size = W.UINT(512)
            value = C.create_unicode_buffer(size.value)
            if api.GetApplicationUserModelId(process, C.byref(size), value) or value.value != app_id:
                continue
            exit_code = W.DWORD()
            if api.GetExitCodeProcess(process, C.byref(exit_code)) and exit_code.value == 259:
                return True
        finally:
            api.CloseHandle(process)
    return False


def _activate(app_id):
    """Activate by AUMID and surface Windows' result instead of spawning Explorer."""
    ole = C.OleDLL('ole32')
    ole.CoInitializeEx.argtypes = [C.c_void_p, W.DWORD]
    initialized = False
    try:
        ole.CoInitializeEx(None, 2)
        initialized = True
    except OSError as exc:
        if getattr(exc, 'winerror', None) != -2147417850:  # RPC_E_CHANGED_MODE
            raise
    manager = C.c_void_p()
    try:
        clsid = GUID.parse('45ba127d-10a8-46ea-8ab7-56ea9078943c')
        iid = GUID.parse('2e941141-7f97-4756-ba1d-9decde894a3d')
        ole.CoCreateInstance.argtypes = [C.POINTER(GUID), C.c_void_p, W.DWORD,
                                        C.POINTER(GUID), C.POINTER(C.c_void_p)]
        ole.CoCreateInstance(C.byref(clsid), None, 4, C.byref(iid), C.byref(manager))
        table = C.cast(manager, C.POINTER(C.POINTER(C.c_void_p))).contents
        activate = C.WINFUNCTYPE(C.c_long, C.c_void_p, W.LPCWSTR, W.LPCWSTR,
                                W.DWORD, C.POINTER(W.DWORD))(table[3])
        pid = W.DWORD()
        result = activate(manager, app_id, None, 2, C.byref(pid))  # AO_NOERRORUI
        if result < 0:
            raise OSError(f'Windows 앱 실행 오류: 0x{result & 0xffffffff:08X}')
    finally:
        if manager:
            table = C.cast(manager, C.POINTER(C.POINTER(C.c_void_p))).contents
            C.WINFUNCTYPE(W.ULONG, C.c_void_p)(table[2])(manager)
        if initialized:
            ole.CoUninitialize()


def launch_chatgpt(app_id):
    """Launch a saved trusted identity without reopening an existing app."""
    _require_windows()
    if app_id not in _APP_IDS:
        raise RuntimeError('ChatGPT 실행 정보가 유효하지 않습니다. 설정을 다시 켜세요.')
    if _already_running(app_id):
        return 'already_running'
    try:
        _activate(app_id)
    except OSError as exc:
        raise RuntimeError(f'ChatGPT를 실행하지 못했습니다. 앱 설치 상태를 확인한 뒤 설정을 다시 켜세요. ({exc})') from exc
    return 'started'
