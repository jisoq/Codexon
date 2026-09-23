"""Stable Windows display identifiers across Qt 5 and Qt 6.

Qt 6 may report the monitor's friendly name as QScreen.name(). Existing stored
selections use the Windows device name, so never use the friendly name as a key.
"""
import sys


def screen_id(screen):
    if sys.platform!='win32':return screen.name()
    import ctypes
    from ctypes import wintypes as W
    class Info(ctypes.Structure):
        _fields_=[('size',W.DWORD),('monitor',W.RECT),('work',W.RECT),('flags',W.DWORD),('device',W.WCHAR*32)]
    api=ctypes.WinDLL('user32',use_last_error=True)
    api.MonitorFromPoint.argtypes=[W.POINT,W.DWORD];api.MonitorFromPoint.restype=W.HMONITOR
    api.GetMonitorInfoW.argtypes=[W.HMONITOR,ctypes.POINTER(Info)];api.GetMonitorInfoW.restype=W.BOOL
    geometry=screen.geometry();handle=api.MonitorFromPoint(W.POINT(geometry.x()+1,geometry.y()+1),0)
    info=Info();info.size=ctypes.sizeof(info)
    if handle and api.GetMonitorInfoW(handle,ctypes.byref(info)):return info.device
    return screen.name()
