"""Run Windows UI checks on a hidden desktop, optionally isolating the clipboard."""
import argparse
from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes as W
import msvcrt
import os
import subprocess
import sys
import uuid


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
        (user, 'CreateWindowStationW', [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p], W.HANDLE),
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
def isolated_desktop(user, isolate_clipboard=False):
    previous = checked(user.GetProcessWindowStation())
    # An unnamed call reuses the service logon station, whose clipboard may be
    # inaccessible. Create an owned station with a distinct clipboard instead.
    # This needs a Windows token allowed to create window stations (as in CI).
    station = checked(user.CreateWindowStationW('CodexonQAClipboard-' + uuid.uuid4().hex, 0, 0x37F, None) if isolate_clipboard
                      else user.OpenWindowStationW('WinSta0', False, 0x37F))
    desktop = None
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = W.DWORD()
        checked(user.GetUserObjectInformationW(station, 2, name, ctypes.sizeof(name),
                                              ctypes.byref(needed)))
        if isolate_clipboard and name.value.lower() == 'winsta0':
            raise RuntimeError('UI checks must not use the interactive clipboard')
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


def run_on_desktop(kernel, desktop, command, env):
    # subprocess.STARTUPINFO does not forward lpDesktop to CreateProcessW.
    # Use the real structure and inherit only the three standard streams.
    startup = StartupInfoEx()
    startup.startup.cb = ctypes.sizeof(startup)
    startup.startup.lpDesktop = desktop
    startup.startup.dwFlags = 0x100  # STARTF_USESTDHANDLES
    process = ProcessInfo()
    with ExitStack() as cleanup:
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
        # CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT
        checked(kernel.CreateProcessW(None, line, None, None, True, 0x08080400,
                                      environment, None, ctypes.byref(startup), ctypes.byref(process)))
        cleanup.callback(kernel.CloseHandle, process.process)
        checked(kernel.CloseHandle(process.thread))
        try:
            while True:
                status = kernel.WaitForSingleObject(process.process, 100)
                if status == 0:
                    break
                if status != 258:  # WAIT_TIMEOUT
                    raise ctypes.WinError(ctypes.get_last_error())
            result = W.DWORD()
            checked(kernel.GetExitCodeProcess(process.process, ctypes.byref(result)))
            return result.value
        except BaseException:
            # Do not leave the direct check process running after an interrupted runner.
            kernel.TerminateProcess(process.process, 1)
            kernel.WaitForSingleObject(process.process, 5000)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale')
    parser.add_argument('--isolate-clipboard', action='store_true',
                        help='Use a noninteractive station for clipboard checks; native visibility needs the default mode')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == '--':
        command = command[1:]
    if not command:
        parser.error('A command is required')
    env = dict(os.environ)
    if args.scale:
        env['QT_SCALE_FACTOR'] = args.scale
    user, kernel = windows_api()
    with isolated_desktop(user, args.isolate_clipboard) as desktop:
        return run_on_desktop(kernel, desktop, command, env)


if __name__ == '__main__':
    raise SystemExit(main())
