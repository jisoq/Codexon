import threading
import time

from cachemonitor.taskbar_clock import ClockProbe


def test_clock_bounds_accepts_secondary_container_and_rejects_other_icons():
    import ctypes
    from ctypes import wintypes
    from cachemonitor.taskbar_clock import ClockReader

    class Reader(ClockReader):
        def __init__(self, clock_class):
            self.automation = ctypes.c_void_p(1)
            self.condition = ctypes.c_void_p(2)
            self.items = [
                (clock_class, 'SystemTrayIcon', (3751, 3688, 3869, 3760)),
                (clock_class, 'OtherIcon', (3870, 3688, 3880, 3760)),
                ('OtherClass', 'SystemTrayIcon', (3881, 3688, 3890, 3760)),
            ]

        def call(self, obj, slot, types, *args):
            if obj.value == 1:
                ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p))[0] = 3
            elif obj.value == 3:
                ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p))[0] = 4
            elif obj.value == 4 and slot == 3:
                ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_int))[0] = len(self.items)
            elif obj.value == 4:
                ctypes.cast(args[-1], ctypes.POINTER(ctypes.c_void_p))[0] = 10 + args[0]
            else:
                rect = ctypes.cast(args[-1], ctypes.POINTER(wintypes.RECT)).contents
                rect.left, rect.top, rect.right, rect.bottom = self.items[obj.value - 10][2]

        def text(self, element, slot):
            return self.items[element.value - 10][0 if slot == 30 else 1]

        def release(self, obj):
            pass

    for class_name in ('SystemTray.OmniButton', 'NamedContainerAutomationPeer'):
        assert Reader(class_name).bounds(123) == (3751, 3688, 118, 72)


def wait_result(probe, host, geometry):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = probe.get(host, geometry)
        if result is not None:
            return result
        time.sleep(.01)
    raise AssertionError('Clock lookup did not complete')


def test_probe_is_nonblocking_and_rejects_stale_layout():
    entered, proceed, closed = threading.Event(), threading.Event(), threading.Event()
    threads = []
    class Reader:
        def __init__(self): threads.append(threading.get_ident())
        def bounds(self, host):
            threads.append(threading.get_ident())
            entered.set()
            assert proceed.wait(2)
            return (host, 0, 70, 48)
        def close(self):
            threads.append(threading.get_ident())
            closed.set()
    probe = ClockProbe(Reader)
    try:
        assert probe.get(1, (0, 0, 1920, 48)) is None
        assert entered.wait(1)
        # Provider is still blocked, but querying from the UI thread returns.
        assert probe.get(1, (0, 0, 1920, 48)) is None
        proceed.set()
        assert wait_result(probe, 1, (0, 0, 1920, 48)) == (1, 0, 70, 48)
        assert probe.get(1, (-1920, 0, 1920, 48)) is None
        assert wait_result(probe, 1, (-1920, 0, 1920, 48)) == (1, 0, 70, 48)
        assert probe.get(2, (-1920, 0, 1920, 48)) is None
        assert wait_result(probe, 2, (-1920, 0, 1920, 48)) == (2, 0, 70, 48)
    finally:
        proceed.set()
        probe.close()
        assert closed.wait(1)
    assert len(set(threads)) == 1 and threads[0] != threading.get_ident()


def test_probe_recovers_after_explorer_provider_error():
    instances = []
    class Reader:
        def __init__(self): instances.append(self)
        def bounds(self, host):
            if len(instances) == 1:
                raise OSError('Explorer restarting')
            return (100, 0, 70, 48)
        def close(self): pass
    probe = ClockProbe(Reader)
    try:
        assert wait_result(probe, 1, (0, 0, 1920, 48)) == (100, 0, 70, 48)
        assert len(instances) == 2
    finally:
        probe.close()
