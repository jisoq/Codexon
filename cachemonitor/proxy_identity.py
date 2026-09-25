"""Process and deployment identity for cooperative proxy replacement on Windows."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import urlsplit

from .observer_state import ProcessLock, read_json


def digest(path):
    with Path(path).open('rb') as source:return hashlib.file_digest(source,'sha256').hexdigest()


def process_identity(pid):
    from ctypes import wintypes as W
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];api.OpenProcess.restype=W.HANDLE
    api.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,ctypes.POINTER(W.DWORD)]
    api.GetProcessTimes.argtypes=[W.HANDLE,*([ctypes.POINTER(W.FILETIME)]*4)]
    api.GetExitCodeProcess.argtypes=[W.HANDLE,ctypes.POINTER(W.DWORD)]
    api.CloseHandle.argtypes=[W.HANDLE]
    handle=api.OpenProcess(0x1000,False,int(pid))
    if not handle:
        if ctypes.get_last_error()==87:return None
        raise OSError('프로세스 소유권을 읽지 못했습니다.')
    try:
        size=W.DWORD(32768);value=ctypes.create_unicode_buffer(size.value)
        created,exited,kernel,user=(W.FILETIME() for _ in range(4))
        if not api.GetProcessTimes(handle,*[ctypes.byref(x) for x in (created,exited,kernel,user)]):raise OSError('생성 시각 확인 실패')
        if exited.dwHighDateTime or exited.dwLowDateTime:return None
        if not api.QueryFullProcessImageNameW(handle,0,value,ctypes.byref(size)):
            code=W.DWORD()
            if api.GetExitCodeProcess(handle,ctypes.byref(code)) and code.value!=259:return None
            raise OSError('실행 경로 확인 실패')
        return dict(pid=int(pid),created=(created.dwHighDateTime<<32)|created.dwLowDateTime,executable=value.value)
    finally:api.CloseHandle(handle)


def process_command(pid):
    from .launch_context import command_arguments
    script=f"[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); (Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine | ConvertTo-Json -Compress"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
                          capture_output=True,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise RuntimeError('실제 프록시 실행 명령을 읽지 못했습니다.')
    command=json.loads(result.stdout.decode('utf-8-sig'))
    if not command:raise RuntimeError('프록시 실행 명령이 없습니다.')
    return command_arguments(command)


def same_process(saved):
    return process_identity(saved['pid'])==saved


def listener_pids(url):
    """Read the OS listener table; Windows refusals can outlast short probes."""
    from ctypes import wintypes as W
    api=ctypes.WinDLL('iphlpapi')
    api.GetExtendedTcpTable.argtypes=[ctypes.c_void_p,ctypes.POINTER(W.DWORD),W.BOOL,W.ULONG,ctypes.c_int,W.ULONG]
    size=W.DWORD()
    api.GetExtendedTcpTable(None,ctypes.byref(size),False,socket.AF_INET,3,0)
    for _ in range(3):
        buffer=ctypes.create_string_buffer(size.value)
        result=api.GetExtendedTcpTable(buffer,ctypes.byref(size),False,socket.AF_INET,3,0)
        if result==122:continue
        if result:raise OSError('리스너 소유권 확인 실패')
        words=ctypes.cast(buffer,ctypes.POINTER(W.DWORD));port=urlsplit(url).port
        return [int(words[1+i*6+5]) for i in range(words[0]) if socket.ntohs(words[1+i*6+2]&65535)==port]
    raise OSError('리스너 정보가 계속 변경되고 있습니다.')


def port_free(url):
    return not listener_pids(url)


def locks_free(paths):
    from contextlib import ExitStack
    try:
        with ExitStack() as locks:
            for path in paths:locks.enter_context(ProcessLock(path))
        return True
    except RuntimeError:return False


def deployment(executable):
    executable=Path(executable).resolve()
    manifest=read_json(executable.parent/'build-manifest.json')
    sha=digest(executable)
    if manifest.get('product')!='Codexon' or manifest.get('sha256')!=sha or not manifest.get('commit'):
        raise RuntimeError('배포 파일 무결성 확인 실패 · 기존 프록시를 유지합니다.')
    return dict(executable=str(executable),sha256=sha,commit=manifest['commit'])


def runtime_identity(home,evidence,*,role,upstream,index=None):
    executable=str(Path(sys.executable).resolve())
    return dict(role=role,home=str(Path(home).resolve()),evidence_path=str(Path(evidence).resolve()),
                index_path=str(Path(index).resolve()) if index else None,upstream=upstream,
                executable=executable,executable_sha256=digest(executable),
                deployment_commit=read_json(Path(executable).parent/'build-manifest.json').get('commit'),
                process_created=process_identity(os.getpid())['created'] if os.name=='nt' else None)
