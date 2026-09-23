"""Packaged update smoke: isolated home, evidence DB, port and scheduler tasks."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_control import ObserverManager
from cachemonitor.observer_task import ObserverTask
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key
from cachemonitor.proxy_update import ProxyUpdate
from cachemonitor.version import PROXY_VERSION


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-exe',type=Path,required=True)
    parser.add_argument('--new-exe',type=Path,required=True)
    parser.add_argument('--port',type=int,default=18769)
    parser.add_argument('--output',type=Path,required=True)
    options=parser.parse_args()
    if options.port==8768 or not 1024<=options.port<=65535:
        parser.error('운영 프록시 포트를 제외한 테스트 전용 포트를 지정하세요.')
    old=options.old_exe.resolve();new=options.new_exe.resolve()
    if not old.is_file() or not new.is_file():
        parser.error('두 배포 실행 파일이 모두 필요합니다.')
    root=options.output.resolve();root.mkdir(parents=True,exist_ok=True)
    old_report=root/'old-runtime.json'
    verified=subprocess.run([str(old),'--verify-runtime',str(old_report)],timeout=30)
    old_runtime=read_json(old_report)
    assert verified.returncode==0 and old_runtime.get('errors')==[]
    old_version=old_runtime['version']
    home=root/'home';home.mkdir(exist_ok=True)
    m=ObserverManager(home,root/'data',url=f'http://127.0.0.1:{options.port}')
    updater=ObserverTask(home_key(home),role='ProxyUpdate')
    home.joinpath('config.toml').write_text(f'openai_base_url="http://127.0.0.1:{options.port}"\n')
    m.write_state(dict(home=home_key(home),enabled=True,phase='active',upstream='chatgpt'))
    args=['--codex-home',str(home),'--evidence-path',str(m.evidence),'--upstream','chatgpt','--port',str(options.port)]
    before=m.config_path.read_bytes()
    try:
        m.task.start([str(old),'--proxy-supervisor',*args])
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            health=m.health(timeout=1)
            if health and m.runtime().get('phase')=='active':break
            time.sleep(.5)
        assert health and health['version']==old_version
        ProxyUpdate(m).publish('queued',source_instance=health['instance'],cancel_requested=False)
        updater.start([str(new),'--proxy-update',*args])
        deadline=time.monotonic()+75
        while time.monotonic()<deadline:
            state=read_json(m.directory/'proxy-update.json')
            if state.get('phase') in ('complete','failed'):break
            time.sleep(.5)
        assert state.get('phase')=='complete',state
        deadline=time.monotonic()+20
        health=None
        while time.monotonic()<deadline:
            health=m.health(timeout=3)
            if health and health.get('version')==PROXY_VERSION and health.get('status')=='ok':
                break
            time.sleep(.5)
        assert health and health['version']==PROXY_VERSION and health['status']=='ok', (state,health)
        assert m.config_path.read_bytes()==before
        print(json.dumps({'phase':state['phase'],'version':health['version'],'configuration_preserved':True}))
    finally:
        # Stop the isolated updater before taking the same control lock during
        # cleanup, including when a timed-out transition is still in progress.
        updater.stop()
        m.turn_off()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if not m.health(timeout=1):break
            time.sleep(.5)
        updater.remove()

if __name__=='__main__':main()
