import json
import subprocess
import sys
import time
from pathlib import Path

from cachemonitor.observer_control import ObserverManager
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key


def test_observation_task_survives_launcher_exit(tmp_path):
    import os
    import socket
    import pytest
    from cachemonitor.observer_task import ObserverTask
    if os.name != 'nt':
        pytest.skip('Windows Task Scheduler lifecycle')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    assert port not in (8768, 8769)
    home = tmp_path / 'home'
    home.mkdir()
    root = Path(__file__).resolve().parents[1]
    manager = ObserverManager(home, tmp_path / 'data', url=f'http://127.0.0.1:{port}')
    task = ObserverTask(home, role='CacheObservationV2')
    command = [str(Path(sys.executable).with_name('pythonw.exe')), str(root / 'run.py'),
               '--model-proxy', '--cache-observe-only', '--codex-home', str(home),
               '--evidence-path', str(manager.evidence), '--observation-index',
               str(tmp_path / 'data' / 'usage-index.sqlite'), '--port', str(port)]
    launcher = tmp_path / 'launch.py'
    launcher.write_text('import sys\nsys.path.insert(0, ' + repr(str(root)) + ')\n'
                        'from cachemonitor.observer_task import ObserverTask\n'
                        f'ObserverTask({str(home)!r}, role="CacheObservationV2").start({command!r})\n')
    try:
        # The launcher exits completely before the independent worker is checked.
        subprocess.run([sys.executable, str(launcher)], check=True, timeout=30)
        deadline = time.monotonic() + 20
        health = None
        while time.monotonic() < deadline:
            health = manager.health(timeout=1)
            if health:
                break
            time.sleep(.1)
        assert health and health['cache_observation_only'] is True
        assert health['requests'] == 0
        state = task.inspect()
        assert state['state'] == 4 and state['autostart'] is False
        assert '--cache-observe-only' in state['arguments']
        task.configure(command, autostart=True)
        assert task.inspect()['restartCount'] == 3
        assert manager.health(timeout=1)['pid'] == health['pid']
        # No URL is installed merely by starting the observer.
        assert not manager.config_path.exists()
    finally:
        task.stop()
        task.remove()


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
