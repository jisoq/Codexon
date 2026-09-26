"""Small Windows helpers shared by the quota-only executable."""
import ctypes
import os
import subprocess
import sys


def visible_window_rect(rect, work):
    """Keep an accessible title bar; recover off-screen/oversized native frames."""
    x,y,width,height=rect
    left,top,right,bottom=work
    available_width,available_height=right-left,bottom-top
    if available_width<=0 or available_height<=0:
        return rect
    visible_width=max(0,min(x+width,right)-max(x,left))
    if (visible_width>=min(width,160) and top<=y and y+min(height,48)<=bottom
            and width<=available_width and height<=available_height):
        return rect
    width=min(width,available_width)
    height=min(height,available_height)
    return (left+(available_width-width)//2,top+(available_height-height)//2,width,height)


def recover_dashboard_position(hwnd):
    """Query actual HWND coordinates, which may differ from Qt's cached frame."""
    from ctypes import wintypes as W
    user=ctypes.WinDLL('user32',use_last_error=True)
    class MonitorInfo(ctypes.Structure):
        _fields_=[('size',W.DWORD),('monitor',W.RECT),('work',W.RECT),('flags',W.DWORD)]
    user.GetWindowRect.argtypes=[W.HWND,ctypes.POINTER(W.RECT)]
    user.GetWindowRect.restype=W.BOOL
    user.MonitorFromWindow.argtypes=[W.HWND,W.DWORD]
    user.MonitorFromWindow.restype=W.HMONITOR
    user.GetMonitorInfoW.argtypes=[W.HMONITOR,ctypes.POINTER(MonitorInfo)]
    user.GetMonitorInfoW.restype=W.BOOL
    user.SetWindowPos.argtypes=[W.HWND,W.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,W.UINT]
    user.SetWindowPos.restype=W.BOOL
    rect=W.RECT();info=MonitorInfo();info.size=ctypes.sizeof(info)
    if not user.GetWindowRect(hwnd,ctypes.byref(rect)):
        return False
    if not user.GetMonitorInfoW(user.MonitorFromWindow(hwnd,2),ctypes.byref(info)):
        return False
    before=(rect.left,rect.top,rect.right-rect.left,rect.bottom-rect.top)
    work=info.work
    after=visible_window_rect(before,(work.left,work.top,work.right,work.bottom))
    if before==after:
        return True
    # Preserve z-order and activation; show_window owns foreground activation.
    return bool(user.SetWindowPos(hwnd,None,*after,0x0014))


class SingleInstance:
    def __init__(self, name):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.kernel.CreateMutexW(None, False, 'Local\\' + name)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.acquired = ctypes.get_last_error() != 183

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WindowsStartup:
    def __init__(self, name='CacheMonitorLite', key_path=r'Software\Microsoft\Windows\CurrentVersion\Run'):
        self.name, self.key_path = name, key_path

    def enabled(self):
        if sys.platform == 'darwin':
            from .macos_startup import MacStartup
            return MacStartup().enabled()
        if os.name != 'nt':
            return False
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path) as key:
                return bool(winreg.QueryValueEx(key, self.name)[0])
        except OSError:
            return False

    def set_enabled(self, enabled, command):
        if sys.platform == 'darwin':
            from .macos_startup import MacStartup
            return MacStartup().set_enabled(enabled, command)
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.key_path) as key:
            if enabled:
                winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, subprocess.list2cmdline(command))
            else:
                try:
                    winreg.DeleteValue(key, self.name)
                except FileNotFoundError:
                    pass
