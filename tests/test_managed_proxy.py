import json
import subprocess
import sys
import time
from pathlib import Path
import pytest

from cachemonitor.observer_control import ObserverManager
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key


@pytest.mark.parametrize('first_mode',['managed','cache-worker'])
def test_same_journal_relay_transition_cannot_recover_a_live_request(tmp_path,first_mode):
    import socket
    import os
    from cachemonitor.cache_execution import Journal
    from test_cache_management import body,response
    def port():
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));return listener.getsockname()[1]
    home=tmp_path/'home';home.mkdir();directory=tmp_path/'data'
    manager=ObserverManager(home,directory,url=f'http://127.0.0.1:{port()}')
    manager.set_url(manager.url);manager.write_state(dict(home=home_key(home),enabled=True,phase='active'))
    def command(mode,url):
        value=[sys.executable,str(Path(__file__).resolve().parents[1]/'run.py'),'--model-proxy','--'+mode,
            '--codex-home',str(home),'--evidence-path',str(manager.evidence),'--port',url.rsplit(':',1)[1]]
        if mode=='cache-worker':value+=['--observation-index',str(directory/'index.sqlite')]
        return value
    processes=[];controls={}
    def start(mode):
        process=subprocess.Popen(command(mode,manager.url),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        processes.append(process);deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            health=manager.health(timeout=.5)
            if health:
                controls[process.pid]=health.get('control_id')
                return process,health
            if process.poll() is not None:raise AssertionError(process.communicate())
            time.sleep(.1)
        raise AssertionError('fixture relay did not start')
    def stop(process,graceful=False):
        # Only this fixture's process tree; Windows venv uses a redirector parent.
        if process.poll() is not None:return
        if graceful:
            from cachemonitor.observer_control import atomic_write
            identity=controls[process.pid];assert identity
            atomic_write(directory/('proxy-control-'+identity+'.json'),json.dumps(dict(action='drain',id=identity)).encode())
            process.communicate(timeout=35)
            assert process.returncode==0
            return
        if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=10)
        else:process.terminate()
        process.communicate(timeout=10)
    journal=None
    try:
        first,health=start(first_mode);assert health['cache_management']
        journal=Journal(directory/'cache-control.sqlite')
        live=journal.reserve('home','live',0,body(),'live');journal.sent(live)
        abandoned=journal.reserve('home','abandoned',0,body(),'abandoned');journal.sent(abandoned)
        other='cache-worker' if first_mode=='managed' else 'managed'
        contender=subprocess.run(command(other,f'http://127.0.0.1:{port()}'),capture_output=True,timeout=20)
        assert contender.returncode!=0
        assert {row['state'] for row in journal.rows()}=={'sent'}
        assert manager.health(timeout=1)['pid']==health['pid']
        journal.finish(live,'completed',response())
        # This fixture deliberately left an unowned sent row: simulate a crash
        # to test recovery, not a graceful shutdown that must preserve settlement.
        stop(first)
        second,health=start(other)
        rows={row['sid']:row for row in journal.rows()}
        assert rows['live']['state']=='completed' and rows['live']['usage_known']
        assert rows['abandoned']['state']=='unknown' and not rows['abandoned']['usage_known']
        assert health['requests']==0
        stop(second,graceful=other=='cache-worker')
    finally:
        for process in reversed(processes):stop(process)
        if journal:journal.close()
        from cachemonitor.observer_task import ObserverTask
        collector=ObserverTask(str((directory/'index.sqlite').resolve()),role='UsageCollector')
        collector.stop();collector.remove()


def test_execution_owner_respects_both_legacy_lifetime_locks(tmp_path):
    from cachemonitor.cache_execution import execution_owner
    from cachemonitor.observer_state import ProcessLock
    path=tmp_path/'cache-control.sqlite'
    for name in ('proxy-supervisor.lock','cache-worker.lock'):
        with ProcessLock(tmp_path/name):
            with pytest.raises(RuntimeError):
                with execution_owner(path):pytest.fail('legacy executor is alive')
        with execution_owner(path):pass


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
        collector=ObserverTask(str((tmp_path/'data'/'usage-index.sqlite').resolve()),role='UsageCollector')
        collector.stop();collector.remove()


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
