"""Startup discovery and launch tests never activate the installed ChatGPT app."""
import ctypes as C
from ctypes import wintypes as W
import json
from types import SimpleNamespace

import pytest

from cachemonitor import chatgpt_startup as startup


APP_ID = 'OpenAI.Codex_2p2nqsd0c76g0!App'


@pytest.fixture(autouse=True)
def isolate_windows(monkeypatch):
    monkeypatch.setattr(startup, '_require_windows', lambda: None)
    monkeypatch.setattr(startup.subprocess, 'CREATE_NO_WINDOW', 0x08000000, raising=False)


def test_discovery_uses_registered_official_identity(monkeypatch):
    calls = []
    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout=json.dumps([
            'OtherVendor.ChatGPT_2p2nqsd0c76g0!App', APP_ID]).encode())
    monkeypatch.setattr(startup.subprocess, 'run', run)
    assert startup.discover_chatgpt() == APP_ID
    assert len(calls) == 1
    assert calls[0][1]['timeout'] == 15
    assert calls[0][1]['creationflags'] == 0x08000000


@pytest.mark.parametrize('output', [b'[]', b'null', b'"untrusted!App"', b'not json'])
def test_missing_or_untrusted_registration_is_not_enabled(monkeypatch, output):
    monkeypatch.setattr(startup.subprocess, 'run', lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout=output))
    with pytest.raises(RuntimeError):
        startup.discover_chatgpt()


def test_launch_skips_existing_app_and_rejects_modified_identity(monkeypatch):
    activated = []
    monkeypatch.setattr(startup, '_already_running', lambda identity: identity == APP_ID)
    monkeypatch.setattr(startup, '_activate', activated.append)
    assert startup.launch_chatgpt(APP_ID) == 'already_running'
    with pytest.raises(RuntimeError):
        startup.launch_chatgpt('malicious.exe')
    assert activated == []


def test_launch_activates_saved_identity_without_slow_rediscovery(monkeypatch):
    activated = []
    monkeypatch.setattr(startup, '_already_running', lambda identity: False)
    monkeypatch.setattr(startup, '_activate', activated.append)
    monkeypatch.setattr(startup, 'discover_chatgpt', lambda: pytest.fail('login must reuse saved identity'))
    assert startup.launch_chatgpt(APP_ID) == 'started'
    assert activated == [APP_ID]


def test_removed_app_reports_failure_without_false_success(monkeypatch):
    monkeypatch.setattr(startup, '_already_running', lambda identity: False)
    def activate(identity):
        raise OSError('App not registered')
    monkeypatch.setattr(startup, '_activate', activate)
    with pytest.raises(RuntimeError):
        startup.launch_chatgpt(APP_ID)


@pytest.mark.parametrize('current_status, expected', [(259, True), (0, False)])
def test_native_detection_matches_session_identity_and_liveness(monkeypatch, current_status, expected):
    closed = []
    processes = {11: (2, APP_ID, 259), 12: (1, 'untrusted!App', 259),
                 13: (1, APP_ID, current_status)}
    def set_value(pointer, value):
        C.cast(pointer, C.POINTER(W.DWORD)).contents.value = value
    def sessions(pid, pointer):
        set_value(pointer, 1 if pid == startup.os.getpid() else processes[pid][0])
        return True
    def enumerate_processes(buffer, size, used):
        for index, pid in enumerate(processes):
            buffer[index] = pid
        set_value(used, len(processes) * C.sizeof(W.DWORD))
        return True
    def identity(pid, size, buffer):
        buffer.value = processes[pid][1]
        return 0
    def status(pid, pointer):
        set_value(pointer, processes[pid][2])
        return True
    api = SimpleNamespace(ProcessIdToSessionId=sessions, K32EnumProcesses=enumerate_processes,
                          OpenProcess=lambda access, inherit, pid: pid,
                          GetApplicationUserModelId=identity, GetExitCodeProcess=status,
                          CloseHandle=closed.append)
    monkeypatch.setattr(startup, '_process_api', lambda: api)
    assert startup._already_running(APP_ID) is expected
    assert closed == [12, 13]


@pytest.mark.skipif(startup.sys.platform != 'win32', reason='Windows COM calling convention')
@pytest.mark.parametrize('hresult', [0, -2147024894])
def test_com_activation_contract_reports_errors_and_releases_resources(monkeypatch, hresult):
    events = []
    @C.WINFUNCTYPE(W.ULONG, C.c_void_p)
    def release(this):
        events.append('release')
        return 0
    @C.WINFUNCTYPE(C.c_long, C.c_void_p, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.POINTER(W.DWORD))
    def activate(this, app_id, arguments, options, pid):
        events.append((app_id, arguments, options))
        pid.contents.value = 123
        return hresult
    table = (C.c_void_p * 4)()
    table[2] = C.cast(release, C.c_void_p).value
    table[3] = C.cast(activate, C.c_void_p).value
    manager = C.pointer(C.cast(table, C.POINTER(C.c_void_p)))
    def initialize(reserved, apartment):
        events.append('initialize')
    def create(clsid, outer, context, iid, output):
        assert context == 4
        C.cast(output, C.POINTER(C.c_void_p)).contents.value = C.cast(manager, C.c_void_p).value
    ole = SimpleNamespace(CoInitializeEx=initialize, CoCreateInstance=create,
                          CoUninitialize=lambda: events.append('uninitialize'))
    monkeypatch.setattr(startup.C, 'OleDLL', lambda name: ole)
    if hresult < 0:
        with pytest.raises(OSError):
            startup._activate(APP_ID)
    else:
        startup._activate(APP_ID)
    assert events == ['initialize', (APP_ID, None, 2), 'release', 'uninitialize']
