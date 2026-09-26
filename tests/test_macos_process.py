import os
from pathlib import Path
import socket
import struct
import subprocess
import sys

import pytest

from cachemonitor.macos_process import _arguments


def test_procargs_returns_argv_without_the_environment_tail():
    raw = struct.pack('i', 3) + b'/program\0\0/program\0argument with spaces\0\0SECRET=value\0'
    assert _arguments(raw) == ['/program', 'argument with spaces', '']


def test_procargs_rejects_missing_argument_terminator():
    with pytest.raises(OSError):
        _arguments(struct.pack('i', 2) + b'/program\0/program\0unfinished')


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel ownership API')
def test_macos_process_and_listener_identity():
    from cachemonitor.proxy_identity import process_identity, process_command, same_process, listener_pids, port_free, executable_matches
    identity = process_identity(os.getpid())
    assert identity['created'] > 0 and same_process(identity)
    assert executable_matches(identity['executable'], sys.executable)
    assert Path(process_command(os.getpid())[0]).resolve() == Path(identity['executable']).resolve()
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen()
        url = f'http://127.0.0.1:{server.getsockname()[1]}'
        assert listener_pids(url) == [os.getpid()]
        assert not port_free(url)
    assert port_free(url)
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.3)'])
    try:
        captured = process_identity(child.pid)
        assert captured
        child.wait(timeout=5)
        assert not same_process(captured)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_framework_executable_exception_is_confined_to_same_version(monkeypatch, tmp_path):
    from cachemonitor import proxy_identity
    monkeypatch.setattr(proxy_identity.sys, 'platform', 'darwin')
    root = tmp_path / 'Python.framework' / 'Versions' / '3.12'
    binary = root / 'Resources' / 'Python.app' / 'Contents' / 'MacOS' / 'Python'
    assert proxy_identity.executable_matches(binary, root / 'bin' / 'python3.12')
    assert not proxy_identity.executable_matches(binary, root.parent / '3.13' / 'bin' / 'python3.13')
    assert not proxy_identity.executable_matches(binary, tmp_path / 'bin' / 'python3.12')
    assert not proxy_identity.executable_matches(binary, root / 'bin' / 'python3.12-config')


def test_exit_poll_retries_unknown_identity_without_weakening_ownership(monkeypatch):
    from cachemonitor import proxy_identity
    saved=dict(pid=123,created=1,executable='/owned')
    def inaccessible(_):raise OSError('Process exiting or ownership unavailable')
    monkeypatch.setattr(proxy_identity,'process_identity',inaccessible)
    assert not proxy_identity.process_exited(saved)
    with pytest.raises(OSError):proxy_identity.same_process(saved)
    monkeypatch.setattr(proxy_identity,'process_identity',lambda _:None)
    assert proxy_identity.process_exited(saved)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS kernel ownership API')
def test_process_list_does_not_read_unrelated_arguments(tmp_path, monkeypatch):
    from cachemonitor import macos_process
    def unexpected(pid):
        pytest.fail('Unrelated process arguments must not be read')
    monkeypatch.setattr(macos_process, 'process_command', unexpected)
    assert macos_process.processes_under(tmp_path) == []


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS listener reuse policy')
def test_hidden_listener_is_preserved_but_drained_time_wait_is_free(monkeypatch):
    from cachemonitor import macos_process
    monkeypatch.setattr(macos_process.subprocess, 'run',
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, b'', b''))
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', 0));server.listen()
        url = f'http://127.0.0.1:{server.getsockname()[1]}'
        with pytest.raises(OSError, match='소유권'):
            macos_process.listener_pids(url)
        with socket.create_connection(server.getsockname()) as client:
            peer, _ = server.accept()
            with peer:
                peer.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b''
    assert macos_process.listener_pids(url) == []
