import json
import subprocess
import sys
import time
from pathlib import Path
import pytest

from cachemonitor.observer_control import ObserverManager
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key








def test_managed_relay_is_one_process_and_explicit_off_drains_without_supervisor(tmp_path):
    import socket
    with socket.socket() as socket_:
        socket_.bind(('127.0.0.1',0));port=socket_.getsockname()[1]
    assert port!=8768
    home=tmp_path/'home';home.mkdir()
    manager=ObserverManager(home,tmp_path/'data',url=f'http://127.0.0.1:{port}')
    manager.config_path.write_text(f'openai_base_url="{manager.url}"\nmodel="keep"\n')
    manager.write_state(dict(home=home_key(home),enabled=True,phase='active'))
    command=[sys.executable,str(Path(__file__).resolve().parents[1]/'run.py'),'--model-proxy','--managed',
             '--codex-home',str(home),'--evidence-path',str(manager.evidence),'--port',str(port)]
    process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+15
        health=None
        while time.monotonic()<deadline:
            health=manager.health(timeout=3)
            if health and manager.runtime().get('phase')=='active':break
            time.sleep(.1)
        assert health and health['lifecycle']=='managed'
        # Windows venv python.exe is itself a redirector. The lifecycle and relay
        # must share the actual Python PID; frozen packaging also checks no watcher.
        assert health['pid']==manager.runtime()['pid']
        assert health['requests']==0
        assert '--proxy-supervisor' not in manager.supervisor_command('chatgpt')
        # This fixture does not register any Windows tasks; emulate the committed
        # state at the end of an explicit recovery operation.
        manager.set_url(None)
        manager.write_state(dict(home=home_key(home),enabled=False,phase='off'))
        assert process.wait(timeout=15)==0
        assert read_json(manager.runtime_path)['phase']=='stopped'
        assert manager.config()[1]=={'model':'keep'}
    finally:
        if process.poll() is None:
            manager.write_state(dict(home=home_key(home),enabled=False,phase='off'))
            process.wait(timeout=20)


@pytest.mark.skipif(sys.platform != 'win32', reason='Winsock invalid descriptor regression')
def test_invalid_selector_socket_exits_and_app_recovers_same_address(tmp_path, monkeypatch):
    """Inject the actual select(WSAENOTSOCK), not a request-handler exception."""
    import socket
    from cachemonitor.app_services import AppServices
    from cachemonitor.observer_state import ProcessLock
    from types import SimpleNamespace
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0)); port = listener.getsockname()[1]
    assert port not in (8768, 8771)
    home = tmp_path / 'home'; home.mkdir()
    manager = ObserverManager(home, tmp_path / 'data', url=f'http://127.0.0.1:{port}')
    manager.config_path.write_text(f'openai_base_url="{manager.url}"\nmodel="keep"\n')
    manager.write_state(dict(home=home_key(home), enabled=True, phase='active'))
    original_config = manager.config_path.read_bytes()
    trigger = tmp_path / 'inject'
    script = '''import asyncio, selectors, socket, sys
from pathlib import Path
import cachemonitor.model_proxy as proxy
trigger=Path(sys.argv.pop(1))
def factory():
    loop=asyncio.SelectorEventLoop()
    select=loop._selector.select
    def poll(timeout=None):
        if trigger.exists():
            trigger.unlink()
            broken=socket.socket()
            loop._selector.register(broken,selectors.EVENT_READ)
            broken.close()
        return select(timeout)
    loop._selector.select=poll
    return loop
proxy.proxy_loop=factory
raise SystemExit(proxy.main())
'''
    arguments = ['--managed', '--codex-home', str(home), '--evidence-path', str(manager.evidence), '--port', str(port)]
    process = subprocess.Popen([sys.executable, '-c', script, str(trigger), *arguments],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    children = [process]
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            health = manager.health(timeout=.5)
            if health and manager.runtime().get('phase') == 'active':break
            time.sleep(.05)
        assert health
        previous = health['instance']
        trigger.touch()
        _, stderr = process.communicate(timeout=12)
        assert process.returncode == 1, stderr.decode(errors='replace')
        report = read_json(manager.directory / 'proxy-failure.json')
        assert report['error']['winerror'] == 10038
        assert report['error']['type'] == 'OSError'
        assert report['phase'] == 'event_loop' and report['storage_flushed']
        assert report['snapshot']['active_connections'] == 0
        assert manager.runtime()['phase'] == 'failed'
        assert read_json(manager.runtime()['control_file'])['action'] == 'failed'
        assert manager.config_path.read_bytes() == original_config
        with ProcessLock(manager.directory / 'proxy-supervisor.lock'):pass
        with socket.socket() as check:check.bind(('127.0.0.1', port))

        # Exercise the actual AppServices gate and readiness handshake, with only
        # Windows task registration replaced by a fixture-owned subprocess.
        def start(*args, **kwargs):
            children.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1]/'run.py'),
                            '--model-proxy', *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        manager.task = SimpleNamespace(start=start, inspect=lambda:dict(running=children[-1].poll() is None),
                                       configure=lambda *a, **kw:None)
        manager.legacy_task = SimpleNamespace(remove=lambda:None)
        now = [0.]
        owner = AppServices(manager, [], collection=False, clock=lambda:now[0])
        owner.active = True
        monkeypatch.setattr(owner, 'remember_proxy', lambda result:None)
        owner.poll()
        assert len(children) == 1
        now[0] = 60
        detected = time.monotonic()
        owner.poll()
        recovered = manager.health(timeout=1)
        assert len(children) == 2 and recovered and recovered['instance'] != previous
        assert recovered['requests'] == 0 and recovered['event_loop'] == 'ProactorEventLoop'
        assert owner.retries['proxy']['count'] == 1
        assert manager.config_path.read_bytes() == original_config
        (tmp_path/'recovery-measurement.json').write_text(json.dumps(dict(
            simulated_retry_wait_seconds=60, launch_to_ready_seconds=time.monotonic()-detected)))
    finally:
        manager.write_state(dict(home=home_key(home), enabled=False, phase='off'))
        for child in children:
            if child.poll() is None:
                try:child.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill();child.communicate(timeout=5)


def test_fatal_report_preserves_original_failure_and_redacts_messages(tmp_path):
    from cachemonitor.proxy_runtime import ProxyDiagnostics, run_loop
    from cachemonitor.model_proxy import proxy_loop
    diagnostic = ProxyDiagnostics(tmp_path)
    async def fail():
        raise RuntimeError('Bearer SECRET and private request content')
    assert run_loop(fail, diagnostic, proxy_loop) == 1
    diagnostic.fail(OSError('PRIVATE cleanup detail'), 'cleanup')
    report = read_json(diagnostic.path)
    assert report['error']['type'] == 'RuntimeError'
    assert report['cleanup_errors'][0]['type'] == 'OSError'
    assert report['error']['frames'][-1]['function'] == 'fail'
    raw = diagnostic.path.read_text()
    assert all(secret not in raw for secret in ('SECRET','PRIVATE','private request'))
    second = ProxyDiagnostics(tmp_path)
    second.fail(ValueError('second incident'), 'startup')
    assert read_json(tmp_path/'proxy-failure.previous.json')['id'] == report['id']


def test_background_proxy_bind_failure_exits_without_uncaught_traceback(tmp_path):
    import socket
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0)); occupied.listen()
        port = occupied.getsockname()[1]
        home=tmp_path/'home'; home.mkdir()
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'run.py'),
                 '--model-proxy','--codex-home',str(home),'--evidence-path',str(tmp_path/'data'/'e.sqlite'),
                 '--port',str(port)], capture_output=True, timeout=15)
    assert result.returncode == 1
    assert b'Traceback (most recent call last)' not in result.stderr
    report = read_json(tmp_path/'data'/'proxy-failure.json')
    assert report['error']['type'] == 'OSError' and report['storage_flushed']
