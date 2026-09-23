"""Read Explorer's composited clock bounds off the Qt thread, without dependencies.

COM slots follow Microsoft's UIAutomationClient.h. All COM objects are created,
used and released on the same MTA worker; only physical-pixel tuples cross threads.
"""
import ctypes
from ctypes import wintypes
import threading
import time
import uuid


class ClockReader:
    def __init__(self):
        self.ole = ctypes.OleDLL('ole32')
        self.automation = ctypes.c_void_p()
        self.condition = ctypes.c_void_p()
        self.ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.ole.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        self.ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.c_void_p)]
        self.strings = ctypes.OleDLL('oleaut32')
        self.strings.SysFreeString.argtypes = [ctypes.c_void_p]
        self.strings.SysFreeString.restype = None
        clsid = ctypes.create_string_buffer(uuid.UUID('ff48dba4-60ef-4201-aa87-54103eef594e').bytes_le)
        iid = ctypes.create_string_buffer(uuid.UUID('30cbe57d-d9d0-452a-ab13-7ac5ac4825ee').bytes_le)
        try:
            self.ole.CoCreateInstance(clsid, None, 1, iid, ctypes.byref(self.automation))
            self.call(self.automation, 21, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(self.condition))
        except Exception:
            self.close()
            raise

    @staticmethod
    def call(obj, slot, types, *args):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        function = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[slot])
        result = function(obj, *args)
        if result < 0:
            raise OSError(f'UI Automation HRESULT 0x{result & 0xffffffff:08x}')
        return result

    @classmethod
    def release(cls, obj):
        if obj:
            cls.call(obj, 2, [])
            obj.value = None

    def text(self, element, slot):
        value = ctypes.c_void_p()
        try:
            self.call(element, slot, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(value))
            return ctypes.wstring_at(value) if value else ''
        finally:
            if value:
                self.strings.SysFreeString(value)

    def bounds(self, host):
        root, elements = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            self.call(self.automation, 6, [wintypes.HWND, ctypes.POINTER(ctypes.c_void_p)],
                      host, ctypes.byref(root))
            self.call(root, 6, [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                      4, self.condition, ctypes.byref(elements))  # TreeScope_Descendants
            count = ctypes.c_int()
            self.call(elements, 3, [ctypes.POINTER(ctypes.c_int)], ctypes.byref(count))
            candidates = []
            for index in range(count.value):
                element = ctypes.c_void_p()
                try:
                    self.call(elements, 4, [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                              index, ctypes.byref(element))
                    # Secondary taskbars also expose the clock as a named
                    # container on current Windows builds.
                    if (self.text(element, 30) not in ('SystemTray.OmniButton', 'NamedContainerAutomationPeer')
                            or self.text(element, 29) != 'SystemTrayIcon'):
                        continue
                    rect = wintypes.RECT()
                    self.call(element, 43, [ctypes.POINTER(wintypes.RECT)], ctypes.byref(rect))
                    if rect.right > rect.left and rect.bottom > rect.top:
                        candidates.append((rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top))
                finally:
                    self.release(element)
            return max(candidates, key=lambda r: r[0]) if candidates else None
        finally:
            self.release(elements)
            self.release(root)

    def close(self):
        self.release(self.condition)
        self.release(self.automation)
        self.ole.CoUninitialize()


class ClockProbe:
    """Nonblocking, throttled lookup; discard stale results after shell/layout changes."""
    def __init__(self, reader_factory=ClockReader):
        self.reader_factory = reader_factory
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.pending = None
        self.result = None
        self.last_error = None
        self.last_request = None
        self.requested_at = 0
        self.thread = threading.Thread(target=self.run, name='TaskbarClock', daemon=True)
        self.thread.start()

    def get(self, host, geometry):
        key = (host, geometry)
        now = time.monotonic()
        with self.lock:
            if key != self.last_request or now - self.requested_at >= 1:
                self.pending = key
                self.last_request, self.requested_at = key, now
                self.wake.set()
            if self.result and self.result[0] == key and now - self.result[2] < 3:
                return self.result[1]
        return None

    def run(self):
        reader = None
        try:
            while not self.stopped.is_set():
                self.wake.wait()
                self.wake.clear()
                with self.lock:
                    request, self.pending = self.pending, None
                if self.stopped.is_set():
                    break
                if request is None:
                    continue
                try:
                    if reader is None:
                        reader = self.reader_factory()
                    rect = reader.bounds(request[0])
                    self.last_error = None
                except Exception as error:
                    self.last_error = str(error)
                    rect = None
                    if reader is not None:
                        reader.close()
                        reader = None
                with self.lock:
                    self.result = (request, rect, time.monotonic())
        finally:
            if reader is not None:
                reader.close()

    def close(self):
        self.stopped.set()
        self.wake.set()
