"""Never let routine tests reparent Qt windows into the user's Explorer."""
import os
import sys

import pytest


# GitHub's hosted Windows runner has no interactive desktop. These checks
# depend on native focus, window visibility, or rendered delegate hit testing.
# The release verification runs the unfiltered suite on an interactive PC.
INTERACTIVE_DESKTOP_TESTS = {
    'tests/test_call_transport.py::test_call_column_filter_selection_refresh_and_diagnostics',
    'tests/test_model_ui.py::test_model_columns_counts_filter_and_refresh',
    'tests/test_overlay_controls.py::test_opacity_popup_has_one_slider_and_keyboard_escape',
    'tests/test_overlay_controls.py::test_detail_graph_owns_navigation_and_body_alone_scrolls',
    'tests/test_overlay_controls.py::test_rendered_detail_selection_preserves_latest_monitor_values',
    'tests/test_overlay_layout_controller.py::test_keyboard_focus_connects_existing_header_graph_scroll_and_popup',
    'tests/test_overlay_layout_controller.py::test_keyboard_minimize_restore_and_move_keep_existing_control_paths',
    'tests/test_overlay_layout_controller.py::test_queued_keyboard_focus_cannot_override_new_target_or_hidden_window',
    'tests/test_overlay_layout_controller.py::test_native_companion_keyboard_routes',
    'tests/test_overlay_navigation.py::test_monitor_link_mask_keyboard_and_hover_do_not_paint_text',
    'tests/test_overlay_navigation.py::test_dashboard_navigation_validates_session_and_selects_old_event_call',
    'tests/test_ui.py::test_sequential_drilldown_and_full_call_detail',
    'tests/test_ui.py::test_record_actions_do_not_shift_table',
    'tests/test_ui.py::test_filter_layout_and_pending_states_keep_table_stationary',
}


def pytest_collection_modifyitems(items):
    if os.environ.get('CODEXON_CI_HEADLESS') != '1':
        return
    desktop_only = pytest.mark.skip(reason='Requires an interactive Windows desktop; run in release verification')
    for item in items:
        if item.nodeid.split('[', 1)[0] in INTERACTIVE_DESKTOP_TESTS:
            item.add_marker(desktop_only)


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
    parser.addoption('--live-taskbar', action='store_true', default=False,
                     help='Run Explorer integration tests on a disposable Windows desktop only')


def pytest_configure(config):
    config.addinivalue_line('markers', 'live_taskbar: requires a disposable Windows desktop')


@pytest.fixture(autouse=True)
def isolate_taskbar(request, monkeypatch):
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
