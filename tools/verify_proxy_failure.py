"""Frozen relay failure exit and AppServices recovery, using only owned loopback fixtures."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cachemonitor.app_services import AppServices
from cachemonitor.model_evidence import home_key
from cachemonitor.observer_control import ObserverManager, atomic_write
from cachemonitor.observer_state import read_json


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    executable=args.executable.resolve()
    if not executable.is_file():parser.error('Executable does not exist')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    home=root/'home';home.mkdir()
    children=[]
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0));occupied.listen()
        port=occupied.getsockname()[1]
        if port in (8768,8771):raise RuntimeError('Refusing production proxy port')
        manager=ObserverManager(home,root/'data',url=f'http://127.0.0.1:{port}')
        manager.config_path.write_text(f'openai_base_url="{manager.url}"\nmodel="fixture"\n')
        manager.write_state(dict(home=home_key(home),enabled=True,phase='active',upstream='chatgpt'))
        original=manager.config_path.read_bytes()
        command=[str(executable),'--model-proxy','--managed','--codex-home',str(home),
                 '--evidence-path',str(manager.evidence),'--port',str(port)]
        started=time.monotonic()
        result=subprocess.run(command,capture_output=True,timeout=15)
        failure=read_json(manager.directory/'proxy-failure.json')
        assert result.returncode==1 and failure['error']['type']=='OSError', (result.returncode,failure)
        assert failure['storage_flushed'] and manager.runtime()['phase']=='failed'
        assert b'Traceback (most recent call last)' not in result.stderr
        report=dict(fatal_exit_code=result.returncode,fatal_exit_seconds=time.monotonic()-started,
                    failure_id=failure['id'],model_requests=0)

    class OwnedTask:
        def start(self,*args,**kwargs):
            children.append(subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        def inspect(self):return dict(running=bool(children and children[-1].poll() is None))
        def configure(self,*args,**kwargs):pass
        def remove(self):pass
    manager.task=OwnedTask();manager.legacy_task=OwnedTask()
    def ready():
        health=manager.health(timeout=.5)
        return health if health and manager.runtime().get('phase')=='active' else None
    try:
        manager.task.start()
        deadline=time.monotonic()+15
        health=None
        while time.monotonic()<deadline:
            health=ready()
            if health:break
            time.sleep(.1)
        assert health
        previous=health['instance']
        owner=AppServices(manager,[],collection=False)
        owner.active=True
        owner.remember_proxy(manager.status())
        # Test-owned process only. Keep its stale active receipt to challenge readiness.
        children[-1].terminate();children[-1].wait(timeout=10)
        detected=time.monotonic()
        owner.poll()
        assert len(children)==1
        deadline=detected+100
        while time.monotonic()<deadline:
            time.sleep(1)
            owner.poll()
            health=ready()
            if health and health['instance']!=previous:break
        assert health and health['instance']!=previous and len(children)==2
        assert health['requests']==0 and health['event_loop']=='ProactorEventLoop'
        assert manager.config_path.read_bytes()==original
        assert owner.retries['proxy']['count']==1
        report.update(crash_detection_to_ready_seconds=round(time.monotonic()-detected,3),
                      restart_attempts=1,config_preserved=True,event_loop=health['event_loop'])
    finally:
        manager.write_state(dict(home=home_key(home),enabled=False,phase='off'))
        for child in children:
            if child.poll() is None:
                try:child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill();child.wait(timeout=5)
    report['owned_processes_exited']=all(child.poll() is not None for child in children)
    atomic_write(root/'result.json',json.dumps(report,indent=2).encode())
    print(json.dumps(report))


if __name__=='__main__':main()
