"""Windows check ownership. Never kill by image name or a recycled PID."""
import ctypes
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import time
import subprocess

from tools.check_diagnostics import DiagnosticFailure, diagnostic_io, write_json


class Limits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', W.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t),
                ('active', W.DWORD), ('affinity', ctypes.c_size_t),
                ('priority', W.DWORD), ('scheduling', W.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', Limits), ('io', ctypes.c_uint64 * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]


class Job:
    def __init__(self):
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, args, result in (
            ('CreateJobObjectW', [ctypes.c_void_p, W.LPCWSTR], W.HANDLE),
            ('SetInformationJobObject', [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD], W.BOOL),
            ('QueryInformationJobObject', [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.c_void_p], W.BOOL),
            ('AssignProcessToJobObject', [W.HANDLE, W.HANDLE], W.BOOL),
            ('TerminateJobObject', [W.HANDLE, W.UINT], W.BOOL),
            ('CloseHandle', [W.HANDLE], W.BOOL),
            ('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            ('WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
        ):
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, result
        self.handle = self.api.CreateJobObjectW(None, None)
        self.check(self.handle)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        try:
            self.check(self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process):
        self.check(self.api.AssignProcessToJobObject(self.handle, process))

    def pids(self):
        # Bounded storage; retry only if the actual tree needs a larger buffer.
        size = 64
        while size <= 65536:
            class ProcessList(ctypes.Structure):
                _fields_ = [('assigned', W.DWORD), ('count', W.DWORD),
                            ('ids', ctypes.c_size_t * size)]
            value = ProcessList()
            if self.api.QueryInformationJobObject(self.handle, 3, ctypes.byref(value), ctypes.sizeof(value), None):
                alive = []
                for pid in value.ids[:value.count]:
                    handle = self.api.OpenProcess(0x100000, False, pid)
                    if handle:
                        try:
                            if self.api.WaitForSingleObject(handle, 0) == 258:
                                alive.append(pid)
                        finally:
                            self.api.CloseHandle(handle)
                    elif ctypes.get_last_error() != 87:
                        # Do not mistake an access failure for an exited child.
                        alive.append(pid)
                return alive
            if ctypes.get_last_error() != 234:
                self.check(False)
            size *= 2
        raise RuntimeError('Check process tree exceeded the diagnostic PID budget')

    def terminate(self):
        self.check(self.api.TerminateJobObject(self.handle, 124))
        deadline = time.monotonic() + 5
        while self.pids() and time.monotonic() < deadline:
            time.sleep(.05)
        return self.pids()

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def register_root(root):
    """Register a newly allocated test scope before launching external work."""
    folder = os.environ.get('CODEXON_CHECK_DIAGNOSTICS')
    if not folder:
        return
    import hashlib
    root = str(Path(root).resolve())
    destination = Path(folder) / ('owner-' + hashlib.sha256(root.encode()).hexdigest() + '.json')
    with diagnostic_io('register process scope', destination):
        destination.write_text(json.dumps({'root': root}), encoding='utf-8')


def clear_scopes(folder):
    with diagnostic_io('list process scopes', folder):
        if not any(folder.glob('owner-*.json')):
            return {}
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
        str(Path(__file__).with_name('clear_check_scopes.ps1')), '-Evidence', str(folder)],
        capture_output=True, timeout=12, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with diagnostic_io('write scope cleanup log', folder / 'scope-cleanup.log'):
            (folder / 'scope-cleanup.log').write_bytes(result.stdout + result.stderr)
    except DiagnosticFailure:
        pass
    if result.returncode:
        raise RuntimeError('External check scope cleanup failed: ' + result.stderr.decode('utf-8-sig', errors='replace'))
    with diagnostic_io('decode scope cleanup result', folder):
        evidence = json.loads(result.stdout.decode('utf-8-sig'))
    try:
        write_json(folder / 'external-processes.json', evidence)
    except DiagnosticFailure:
        pass
    return evidence
