"""Kernel process identity and listener ownership, without reading credentials."""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import socket
import shlex
import struct
import subprocess
from urllib.parse import urlsplit


class BSDInfo(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ('nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64), ('start_usec', ctypes.c_uint64)]


def _libproc():
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    lib.proc_pidpath.restype = ctypes.c_int
    return lib


def _missing(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def process_identity(pid):
    pid = int(pid)
    if pid <= 0:
        raise ValueError('A positive process ID is required')
    lib = _libproc()
    info = BSDInfo()
    size = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        if _missing(pid):
            return None
        raise OSError('프로세스 생성 시각과 소유권을 읽지 못했습니다.')
    # A zombie no longer owns a running executable or a listening socket.
    if info.status == 5:
        return None
    path = ctypes.create_string_buffer(4096)
    if lib.proc_pidpath(pid, path, len(path)) <= 0:
        if _missing(pid):
            return None
        raise OSError('프로세스 실행 경로를 읽지 못했습니다.')
    result = dict(pid=pid, created=info.start_sec * 1_000_000 + info.start_usec,
                  executable=str(Path(os.fsdecode(path.value)).resolve()))
    again = BSDInfo()
    if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(again), ctypes.sizeof(again)) != ctypes.sizeof(again):
        return None if _missing(pid) else _changed()
    if (again.start_sec, again.start_usec) != (info.start_sec, info.start_usec):
        return None
    return result


def _changed():
    raise OSError('확인 도중 프로세스가 변경되었습니다.')


def _arguments(raw):
    """KERN_PROCARGS2 includes an environment tail; return only argc entries."""
    if len(raw) < 5:
        raise OSError('프로세스 인자를 읽지 못했습니다.')
    count = struct.unpack_from('i', raw)[0]
    if not 0 < count <= 65536:
        raise OSError('프로세스 인자 개수가 올바르지 않습니다.')
    start = raw.find(b'\0', 4)
    if start < 0:
        raise OSError('프로세스 인자 경계를 확인하지 못했습니다.')
    while start < len(raw) and raw[start] == 0:
        start += 1
    values = raw[start:].split(b'\0', count)
    if len(values) <= count:
        raise OSError('프로세스 인자가 완전하지 않습니다.')
    return [os.fsdecode(value) for value in values[:count]]


def process_command(pid):
    before = process_identity(pid)
    if before is None:
        raise OSError('프로세스가 종료되었습니다.')
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, int(pid))  # CTL_KERN, KERN_PROCARGS2
    length = ctypes.c_size_t()
    if libc.sysctl(mib, 3, None, ctypes.byref(length), None, 0) or not 0 < length.value <= 4 * 1024 * 1024:
        raise OSError('프로세스 실행 인자를 읽지 못했습니다.')
    data = ctypes.create_string_buffer(length.value)
    if libc.sysctl(mib, 3, data, ctypes.byref(length), None, 0):
        raise OSError('프로세스 실행 인자를 읽지 못했습니다.')
    arguments = _arguments(data.raw[:length.value])
    if process_identity(pid) != before:
        _changed()
    return arguments


def listener_pids(url):
    address = urlsplit(url)
    if address.hostname not in ('127.0.0.1', 'localhost', '::1') or not address.port:
        raise ValueError('A loopback listener address is required')
    result = subprocess.run(['/usr/sbin/lsof', '-nP', f'-iTCP:{address.port}', '-sTCP:LISTEN', '-F', 'p'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise OSError('리스너 소유권을 확인하지 못했습니다.')
    pids = sorted({int(line[1:]) for line in result.stdout.splitlines()
                   if line.startswith(b'p') and line[1:].isdigit()})
    if not pids:
        # lsof can omit a process inaccessible to this user. Never report its
        # occupied port as free merely because no owner was visible.
        with socket.socket(socket.AF_INET6 if address.hostname == '::1' else socket.AF_INET) as probe:
            # Closed relay sockets may retain TIME_WAIT after LISTEN is gone.
            # Match aiohttp's listener reuse policy so drain is not mistaken
            # for a rival process, while a live listener still blocks bind.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((address.hostname, address.port))
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    raise OSError('포트가 사용 중이지만 소유권을 확인하지 못했습니다.') from None
                raise
    return pids


def processes_under(root):
    """Read arguments only after the kernel path is inside this installation."""
    root = Path(root).resolve()
    lib = _libproc()
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    lib.proc_listpids.restype = ctypes.c_int
    required = lib.proc_listpids(1, 0, None, 0)
    if required <= 0:
        raise OSError('프로세스 목록을 읽지 못했습니다.')
    pids = (ctypes.c_int * (required // ctypes.sizeof(ctypes.c_int) + 1024))()
    size = lib.proc_listpids(1, 0, pids, ctypes.sizeof(pids))
    if size < 0 or size >= ctypes.sizeof(pids):
        raise OSError('프로세스 목록을 완전히 읽지 못했습니다.')
    result = []
    for pid in pids[:size // ctypes.sizeof(ctypes.c_int)]:
        if pid <= 0:
            continue
        info = BSDInfo()
        if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info):
            continue
        if info.uid != os.getuid() or info.status == 5:
            continue
        path = ctypes.create_string_buffer(4096)
        if lib.proc_pidpath(pid, path, len(path)) <= 0:
            continue
        executable = Path(os.fsdecode(path.value)).resolve()
        if not executable.is_relative_to(root):
            continue
        try:
            arguments = process_command(pid)
        except OSError:
            if _missing(pid):
                continue
            raise
        result.append(dict(ProcessId=pid, ParentProcessId=info.ppid, ExecutablePath=str(executable),
                           CommandLine=shlex.join(arguments), Arguments=arguments))
    return result
