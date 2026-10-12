"""Never let routine tests reparent Qt windows into the user's Explorer."""
import os
import sys

import pytest


@pytest.fixture(autouse=True)
def isolate_installed_proxy(monkeypatch):
    import urllib.request
    from urllib.parse import urlsplit
    original=urllib.request.OpenerDirector.open
    def open_only_test_endpoints(opener,request,*args,**kwargs):
        url=urlsplit(request.full_url if hasattr(request,'full_url') else request)
        assert not (url.hostname in ('127.0.0.1','localhost') and url.port==8768), 'Tests must not contact the installed proxy'
        return original(opener,request,*args,**kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector,'open',open_only_test_endpoints)


def pytest_addoption(parser):
    parser.addoption('--check-kind', choices=('core', 'gui', 'installation'), default='gui',
                     help='Core checks must not load Qt or change native window state')
    parser.addoption('--live-taskbar', action='store_true', default=False,
                     help='Run Explorer integration tests on a disposable Windows desktop only')


def pytest_configure(config):
    config.addinivalue_line('markers', 'live_taskbar: requires a disposable Windows desktop')
    if config.getoption('--check-kind') == 'core':
        return
    from cachemonitor.fonts import configure_font_rendering, configure_high_dpi
    configure_font_rendering()
    configure_high_dpi()


def pytest_sessionfinish(session, exitstatus):
    if session.config.getoption('--check-kind') == 'core' and any(name.startswith('PySide6') for name in sys.modules):
        session.config.pluginmanager.getplugin('terminalreporter').write_line('Core checks imported Qt; move this contract to the GUI group.')
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def isolate_taskbar(request, monkeypatch):
    if request.config.getoption('--check-kind') == 'core':
        return
    if request.node.get_closest_marker('live_taskbar'):
        if not request.config.getoption('--live-taskbar'):
            pytest.skip('Live Explorer test excluded; use a disposable Windows desktop')
        return
    if sys.platform != 'win32':
        return
    from cachemonitor.taskbar import NativeTaskbar
    original_attach = NativeTaskbar.attach

    def own_process_only(native, hwnd, host):
        pid = native.types.DWORD()
        function = native.api.GetWindowThreadProcessId
        function.argtypes = [native.types.HWND, native.ctypes.POINTER(native.types.DWORD)]
        function(host, native.ctypes.byref(pid))
        assert pid.value == os.getpid(), 'Tests may only attach to their own windows'
        return original_attach(native, hwnd, host)

    monkeypatch.setattr(NativeTaskbar, 'host', lambda self: None)
    monkeypatch.setattr(NativeTaskbar, 'host_for_screen', lambda self, name: None)
    monkeypatch.setattr(NativeTaskbar, 'clock_rect', lambda self, host: None)
    monkeypatch.setattr(NativeTaskbar, 'attach', own_process_only)


@pytest.fixture
def owned_taskbar(monkeypatch):
    """Real native parenting, but all windows belong to this test GUI thread."""
    import ctypes
    from ctypes import wintypes as W
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QApplication
    from cachemonitor.taskbar import NativeTaskbar
    app = QApplication.instance() or QApplication([])
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        W.HWND, W.HMENU, W.HINSTANCE, ctypes.c_void_p]
    api.CreateWindowExW.restype = W.HWND
    api.DestroyWindow.argtypes = [W.HWND]
    host = api.CreateWindowExW(0x08000080, 'STATIC', 'CacheMonitor isolated test host',
                               0x90000000, 100, 100, 600, 48, None, None, None, None)
    assert host
    monkeypatch.setattr(NativeTaskbar, 'host', lambda self: host)
    monkeypatch.setattr(NativeTaskbar, 'dock_geometry', lambda self, hwnd, ratio: QRect(450, 6, 100, 36))
    yield host
    app.processEvents()
    api.DestroyWindow(host)
