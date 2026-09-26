"""Run isolated UI checks; macOS/Linux use Qt offscreen unless --native is set."""
import argparse
import ctypes
from ctypes import wintypes as W
import os
import subprocess
import sys
import uuid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scale')
    parser.add_argument('--native', action='store_true',
                        help='Use the current desktop on macOS/Linux for native integration checks')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0]=='--':command=command[1:]
    if not command:parser.error('A command is required')
    env = dict(os.environ)
    if args.scale:env['QT_SCALE_FACTOR']=args.scale
    if os.name != 'nt':
        if not args.native:
            env['QT_QPA_PLATFORM'] = 'offscreen'
            env.setdefault('QT_QUICK_BACKEND', 'software')
        # Cocoa has no equivalent of an unshown Windows desktop. Offscreen
        # validates Qt rendering; separate native checks cover AppKit/Spaces.
        return subprocess.run(command, env=env).returncode
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.CreateDesktopW.argtypes = [W.LPCWSTR, W.LPCWSTR, ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.c_void_p]
    api.CreateDesktopW.restype = W.HANDLE
    api.CloseDesktop.argtypes = [W.HANDLE]
    name = 'CodexonQA-'+uuid.uuid4().hex
    desktop = api.CreateDesktopW(name, None, None, 0, 0x10000000, None)
    if not desktop:raise ctypes.WinError(ctypes.get_last_error())
    try:
        startup = subprocess.STARTUPINFO();startup.lpDesktop = name
        # The desktop is never switched to; Qt events and captured frames remain real.
        return subprocess.run(command, env=env, startupinfo=startup).returncode
    finally:api.CloseDesktop(desktop)


if __name__=='__main__':raise SystemExit(main())
