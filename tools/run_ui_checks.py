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
from tools.check_diagnostics import (DIAGNOSTIC_EXIT, DiagnosticFailure, diagnostic_io,
                                     emit, exit_code, read_json, retain, status, write_json)


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
                wait_status = kernel.WaitForSingleObject(process.process, 100)
                if wait_status == 0:
                    break
                if wait_status != 258:  # WAIT_TIMEOUT
                    raise ctypes.WinError(ctypes.get_last_error())
                if time.monotonic() - started >= timeout:
                    reason = 'deadline'
                    break
                if diagnostics and time.monotonic() - log_check >= 1:
                    log_check = time.monotonic()
                    try:
                        with diagnostic_io('inspect diagnostic budget', diagnostics):
                            logs = list(diagnostics.glob('*.log'))
                            if env.get('CODEXON_CHECK_LOG'):
                                logs.append(Path(env['CODEXON_CHECK_LOG']))
                            over_budget = sum(path.stat().st_size for path in logs if path.exists()) > 64 * 1024 * 1024
                        if over_budget:
                            reason = 'diagnostic_budget'
                            break
                    except DiagnosticFailure:
                        reason = 'diagnostics_failed'
                        break
            result = W.DWORD()
            checked(kernel.GetExitCodeProcess(process.process, ctypes.byref(result)))
            live = job.pids()
            # Native uninstallers may finish self-cleanup after their parent exits.
            settled = min(time.monotonic() + 2, started + timeout)
            while reason == 'completed' and live and time.monotonic() < settled:
                time.sleep(.02)
                live = job.pids()
            if reason == 'completed' and live:
                reason = 'descendants_remain'
            if reason != 'completed' and diagnostics:
                try:
                    with diagnostic_io('request worker stacks', diagnostics / 'stack-request'):
                        (diagnostics / 'stack-request').touch()
                except DiagnosticFailure:
                    pass
                # The worker's stack watcher polls every 200 ms. This bounded
                # diagnostic grace is included in the cleanup reserve.
                stack_deadline = time.monotonic() + 2
                while time.monotonic() < stack_deadline and not any(diagnostics.glob('stack-dumped-*')):
                    if kernel.WaitForSingleObject(process.process, 100) == 0:
                        break
            evidence = dict(reason=reason, command=command, pid=process.process_id,
                            live_pids=live, process_exit_code=result.value, timeout_seconds=timeout,
                            seconds=round(time.monotonic() - started, 3))
            product_exit_code = result.value if reason == 'completed' else None
            if diagnostics and env.get('CODEXON_CHECK_WORKER'):
                try:
                    with diagnostic_io('find worker outcome', diagnostics):
                        states = list(diagnostics.glob('worker-state-*.json'))
                        if len(states) != 1:
                            raise ValueError('Expected one worker outcome file')
                    state = read_json(states[0])
                    with diagnostic_io('validate worker outcome', states[0]):
                        if (not isinstance(state, dict) or state.get('phase') not in ('executing', 'interpreter_shutdown')
                                or not isinstance(state.get('diagnostics', {}), dict)
                                or (state.get('product_exit_code') is not None and type(state['product_exit_code']) is not int)):
                            raise ValueError('Invalid worker outcome')
                    retain(state.get('diagnostics', {}).get('first_error'))
                    product_exit_code = state.get('product_exit_code') if state.get('phase') == 'interpreter_shutdown' else None
                except DiagnosticFailure:
                    product_exit_code = result.value if reason == 'completed' and result.value not in (0, DIAGNOSTIC_EXIT) else None
            if diagnostics:
                try:
                    active = diagnostics / 'active-test.json'
                    with diagnostic_io('inspect active test', active):
                        has_active = active.exists()
                    if has_active:
                        evidence['active_test'] = read_json(active)
                    write_json(diagnostics / 'processes.json', evidence)
                except DiagnosticFailure:
                    pass
            evidence['remaining_pids'] = job.terminate() if live else []
        except BaseException:
            job.terminate()
            # Assignment can fail before the suspended process joins the job.
            kernel.TerminateProcess(process.process, 1)
            kernel.WaitForSingleObject(process.process, 5000)
            raise
        finally:
            if diagnostics:
                try:
                    external = clear_scopes(diagnostics)
                except DiagnosticFailure:
                    external = {}
        evidence['external'] = external if diagnostics else {}
        if evidence['external'].get('processes') or any(task['running'] for task in evidence['external'].get('tasks', [])):
            reason = evidence['reason'] = 'external_descendants_remain' if reason == 'completed' else reason
        evidence.update(product_exit_code=product_exit_code, diagnostics=status(),
                        diagnostics_directory=str(diagnostics) if diagnostics else None)
        if reason == 'deadline':
            code = 124
        elif reason not in ('completed', 'diagnostics_failed') or evidence['remaining_pids']:
            code = 1
        elif result.value not in (0, DIAGNOSTIC_EXIT, 259):
            code = result.value
        else:
            code = exit_code(product_exit_code)
        try:
            if diagnostics:
                write_json(diagnostics / 'result.json', evidence)
        except DiagnosticFailure:
            pass
        code = code or exit_code(product_exit_code)
        if status()['first_error']:
            emit(dict(product_exit_code=product_exit_code, diagnostics=status()))
        return code



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
    from tools.verify_changes import validate_diagnostics_path
    try:
        validate_diagnostics_path(diagnostics)
    except ValueError as error:
        parser.error(str(error))
    print('Diagnostics directory ' + str(diagnostics), file=sys.stderr, flush=True)
    try:
        with diagnostic_io('create diagnostics directory', diagnostics):
            diagnostics.mkdir(parents=True, exist_ok=False)
    except DiagnosticFailure:
        emit(dict(product_exit_code=None, diagnostics=status()))
        return DIAGNOSTIC_EXIT
    env = dict(os.environ)
    env.pop('CODEXON_CHECK_WORKER', None)
    env['CODEXON_CHECK_DIAGNOSTICS'] = str(diagnostics)
    env['CODEXON_CHECK_TIMEOUT'] = str(timeout)
    # Bootstrap only understood Python invocations; native programs still get
    # the same Job Object deadline and PID evidence.
    if Path(command[0]).name.lower() in ('python', 'python.exe', 'python3', 'python3.exe'):
        command = [command[0], '-u', '-B', str(Path(__file__).with_name('check_worker.py')), *command[1:]]
        env['CODEXON_CHECK_WORKER'] = '1'
    if args.scale:
        env['QT_SCALE_FACTOR'] = args.scale
    user, kernel = windows_api()
    if args.no_desktop:
        return run_on_desktop(kernel, None, command, env, timeout=timeout, diagnostics=diagnostics)
    with isolated_desktop(user) as desktop:
        return run_on_desktop(kernel, desktop, command, env, timeout=timeout, diagnostics=diagnostics)


if __name__ == '__main__':
    raise SystemExit(main())
