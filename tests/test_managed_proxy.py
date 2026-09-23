import json
import subprocess
import sys
import time
from pathlib import Path

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
