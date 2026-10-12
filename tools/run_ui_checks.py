"""Run Windows UI checks on a hidden desktop."""
import argparse
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes as W
import msvcrt
import os
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.check_processes import Job, clear_scopes


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ('cb', W.DWORD), ('lpReserved', W.LPWSTR), ('lpDesktop', W.LPWSTR),
        ('lpTitle', W.LPWSTR), ('dwX', W.DWORD), ('dwY', W.DWORD),
        ('dwXSize', W.DWORD), ('dwYSize', W.DWORD),
        ('dwXCountChars', W.DWORD), ('dwYCountChars', W.DWORD),
        ('dwFillAttribute', W.DWORD), ('dwFlags', W.DWORD),
        ('wShowWindow', W.WORD), ('cbReserved2', W.WORD),
        ('lpReserved2', ctypes.c_void_p), ('hStdInput', W.HANDLE),
        ('hStdOutput', W.HANDLE), ('hStdError', W.HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [('startup', StartupInfo), ('attributes', ctypes.c_void_p)]


class ProcessInfo(ctypes.Structure):
    _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE),
               ('process_id', W.DWORD), ('thread_id', W.DWORD)]


def windows_api():
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = (
        (user, 'OpenWindowStationW', [W.LPCWSTR, W.BOOL, W.DWORD], W.HANDLE),
        (user, 'GetProcessWindowStation', [], W.HANDLE),
        (user, 'SetProcessWindowStation', [W.HANDLE], W.BOOL),
        (user, 'CloseWindowStation', [W.HANDLE], W.BOOL),
        (user, 'GetUserObjectInformationW',
         [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
        (user, 'CreateDesktopW',
         [W.LPCWSTR, W.LPCWSTR, ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.c_void_p], W.HANDLE),
        (user, 'CloseDesktop', [W.HANDLE], W.BOOL),
        (kernel, 'InitializeProcThreadAttributeList',
         [ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.POINTER(ctypes.c_size_t)], W.BOOL),
        (kernel, 'UpdateProcThreadAttribute',
         [ctypes.c_void_p, W.DWORD, ctypes.c_size_t, ctypes.c_void_p,
          ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], W.BOOL),
        (kernel, 'DeleteProcThreadAttributeList', [ctypes.c_void_p], None),
        (kernel, 'CreateProcessW',
         [W.LPCWSTR, W.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, W.BOOL,
          W.DWORD, ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(StartupInfoEx),
          ctypes.POINTER(ProcessInfo)], W.BOOL),
        (kernel, 'WaitForSingleObject', [W.HANDLE, W.DWORD], W.DWORD),
        (kernel, 'GetExitCodeProcess', [W.HANDLE, ctypes.POINTER(W.DWORD)], W.BOOL),
        (kernel, 'TerminateProcess', [W.HANDLE, W.UINT], W.BOOL),
        (kernel, 'ResumeThread', [W.HANDLE], W.DWORD),
        (kernel, 'CloseHandle', [W.HANDLE], W.BOOL),
    )
    for library, name, arguments, result in signatures:
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result
    return user, kernel


def checked(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


@contextmanager
def isolated_desktop(user):
    previous = checked(user.GetProcessWindowStation())
    station = checked(user.OpenWindowStationW('WinSta0', False, 0x37F))
    desktop = None
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = W.DWORD()
        checked(user.GetUserObjectInformationW(station, 2, name, ctypes.sizeof(name),
                                              ctypes.byref(needed)))
        checked(user.SetProcessWindowStation(station))
        desktop_name = 'CodexonQA-' + uuid.uuid4().hex
        desktop = checked(user.CreateDesktopW(desktop_name, None, None, 0, 0x10000000, None))
        checked(user.SetProcessWindowStation(previous))
        yield name.value + '\\' + desktop_name
    finally:
        checked(user.SetProcessWindowStation(previous))
        if desktop:
            checked(user.CloseDesktop(desktop))
        checked(user.CloseWindowStation(station))


def run_on_desktop(kernel, desktop, command, env, *, timeout=720, diagnostics=None):
    # subprocess.STARTUPINFO does not forward lpDesktop to CreateProcessW.
    # Use the real structure and inherit only the three standard streams.
    startup = StartupInfoEx()
    startup.startup.cb = ctypes.sizeof(startup)
    startup.startup.lpDesktop = desktop
    startup.startup.dwFlags = 0x100  # STARTF_USESTDHANDLES
    process = ProcessInfo()
    with ExitStack() as cleanup:
        job = Job()
        cleanup.callback(job.close)
        handles = []
        for descriptor in range(3):
            try:
                duplicate = os.dup(descriptor)
            except OSError:
                duplicate = os.open(os.devnull, os.O_RDONLY if descriptor == 0 else os.O_WRONLY)
            cleanup.callback(os.close, duplicate)
            os.set_inheritable(duplicate, True)
            handles.append(msvcrt.get_osfhandle(duplicate))
        startup.startup.hStdInput, startup.startup.hStdOutput, startup.startup.hStdError = handles
        inherited = (W.HANDLE * len(handles))(*handles)
        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = ctypes.create_string_buffer(size.value)
        checked(kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)))
        cleanup.callback(kernel.DeleteProcThreadAttributeList, attributes)
        checked(kernel.UpdateProcThreadAttribute(attributes, 0, 0x20002, inherited,
                                                  ctypes.sizeof(inherited), None, None))
        startup.attributes = ctypes.cast(attributes, ctypes.c_void_p)
        environment = ctypes.create_unicode_buffer(
            '\0'.join(f'{key}={value}' for key, value in sorted(env.items(), key=lambda pair: pair[0].upper())) + '\0')
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        for stream in (sys.stdout, sys.stderr):
            if stream:
                stream.flush()
        # Start suspended so no child can escape before Job Object assignment.
        checked(kernel.CreateProcessW(None, line, None, None, True, 0x08080404,
                                      environment, None, ctypes.byref(startup), ctypes.byref(process)))
        cleanup.callback(kernel.CloseHandle, process.process)
        cleanup.callback(kernel.CloseHandle, process.thread)
        started = time.monotonic()
        log_check = started
        reason = 'completed'
        try:
            job.assign(process.process)
            if kernel.ResumeThread(process.thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            while True:
                status = kernel.WaitForSingleObject(process.process, 100)
                if status == 0:
                    break
                if status != 258:  # WAIT_TIMEOUT
                    raise ctypes.WinError(ctypes.get_last_error())
                if time.monotonic() - started >= timeout:
                    reason = 'deadline'
                    break
                if diagnostics and time.monotonic() - log_check >= 1:
                    log_check = time.monotonic()
                    logs = list(diagnostics.glob('*.log'))
                    if env.get('CODEXON_CHECK_LOG'):
                        logs.append(Path(env['CODEXON_CHECK_LOG']))
                    if sum(path.stat().st_size for path in logs if path.exists()) > 64 * 1024 * 1024:
                        reason = 'diagnostic_budget'
                        break
            result = W.DWORD()
            checked(kernel.GetExitCodeProcess(process.process, ctypes.byref(result)))
            live = job.pids()
            settled = time.monotonic() + .5
            while reason == 'completed' and live and time.monotonic() < settled:
                time.sleep(.02)
                live = job.pids()
            if reason == 'completed' and live:
                reason = 'descendants_remain'
            if reason != 'completed' and diagnostics:
                (diagnostics / 'stack-request').touch()
                # The worker's stack watcher polls every 200 ms. This bounded
                # diagnostic grace is included in the cleanup reserve.
                stack_deadline = time.monotonic() + 2
                while time.monotonic() < stack_deadline and not any(diagnostics.glob('stack-dumped-*')):
                    if kernel.WaitForSingleObject(process.process, 100) == 0:
                        break
            evidence = dict(reason=reason, command=command, pid=process.process_id,
                            live_pids=live, process_exit_code=result.value, timeout_seconds=timeout,
                            seconds=round(time.monotonic() - started, 3))
            if diagnostics:
                active = diagnostics / 'active-test.json'
                if active.exists():
                    evidence['active_test'] = json.loads(active.read_text(encoding='utf-8'))
                (diagnostics / 'processes.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
            evidence['remaining_pids'] = job.terminate() if live else []
            # Scheduler and packaged activation can start outside this Job.
            # The test registers fresh scopes before launching those processes.
            if diagnostics:
                evidence['external'] = clear_scopes(diagnostics)
                if evidence['external'].get('processes') or any(task['running'] for task in evidence['external'].get('tasks', [])):
                    reason = evidence['reason'] = 'external_descendants_remain' if reason == 'completed' else reason
            if diagnostics:
                (diagnostics / 'result.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
            return 124 if reason == 'deadline' else 1 if reason != 'completed' or evidence['remaining_pids'] else result.value
        except BaseException:
            job.terminate()
            # Assignment can fail before the suspended process joins the job.
            kernel.TerminateProcess(process.process, 1)
            kernel.WaitForSingleObject(process.process, 5000)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale')
    parser.add_argument('--kind', choices=('gui', 'installation', 'core'), default='gui')
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--diagnostics', type=Path)
    parser.add_argument('--no-desktop', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        parser.error('A command is required')
    timeout = args.timeout if args.timeout is not None else dict(gui=720, installation=540, core=180)[args.kind]
    if not 0 < timeout <= 840:
        parser.error('Check budget must be positive and at most 840 seconds')
    diagnostics = (args.diagnostics or Path(__file__).resolve().parents[1] / 'artifacts/verification' / ('runner-' + uuid.uuid4().hex)).resolve()
    diagnostics.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    env['CODEXON_CHECK_DIAGNOSTICS'] = str(diagnostics)
    env['CODEXON_CHECK_TIMEOUT'] = str(timeout)
    # Bootstrap only understood Python invocations; native programs still get
    # the same Job Object deadline and PID evidence.
    if Path(command[0]).name.lower() in ('python', 'python.exe', 'python3', 'python3.exe'):
        command = [command[0], '-u', '-B', str(Path(__file__).with_name('check_worker.py')), *command[1:]]
    if args.scale:
        env['QT_SCALE_FACTOR'] = args.scale
    user, kernel = windows_api()
    if args.no_desktop:
        return run_on_desktop(kernel, None, command, env, timeout=timeout, diagnostics=diagnostics)
    with isolated_desktop(user) as desktop:
        return run_on_desktop(kernel, desktop, command, env, timeout=timeout, diagnostics=diagnostics)


if __name__ == '__main__':
    raise SystemExit(main())
