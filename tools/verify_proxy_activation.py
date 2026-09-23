"""Explicit one-call activation QA; real auth, isolated route, task, port and evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_control import ObserverManager
from cachemonitor.observer_state import read_json


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path,required=True)
    parser.add_argument('--auth-home',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    root=args.output.resolve();workspace=Path(__file__).resolve().parents[1]
    if not root.is_relative_to(workspace/'artifacts'):parser.error('Output must stay in artifacts')
    root.mkdir(parents=True,exist_ok=True)
    home=root/('fixture-'+uuid.uuid4().hex[:8])/'home';home.mkdir(parents=True)
    real_config=args.auth_home/'config.toml'
    original=real_config.read_bytes()
    home.joinpath('config.toml').write_text('# isolated verification\nmodel="gpt-6-astra"\n',encoding='utf-8')
    # Metadata only; no auth token is copied. Codex reads its existing login in
    # the per-process CODEX_HOME and overrides routing for this one invocation.
    home.joinpath('auth.json').write_text('{"auth_mode":"chatgpt"}',encoding='utf-8')
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port!=8768
    class PackagedManager(ObserverManager):
        def command(self,upstream):
            command=super().command(upstream)
            return [str(args.exe.resolve()),*command[command.index('--model-proxy'):]]
        def verification_environment(self):
            return {**os.environ,'CODEX_HOME':str(args.auth_home.resolve())}
    manager=PackagedManager(home,home.parent/'data',url=f'http://127.0.0.1:{port}')
    report={'port':port,'fixture':str(home.parent),'exe':str(args.exe.resolve()),'model_probe_invocations':0}
    try:
        report['model_probe_invocations']=1
        result=manager.turn_on()
        assert result['configured'] and result['validated']
        assert manager.task.inspect()['autostart']
        health=manager.health();report['worker_version']=health['version']
        assert health['storage_errors']==0 and health['relay_errors']==0
        with sqlite3.connect(manager.evidence) as db:
            completed=db.execute("SELECT count(*) FROM model_observations WHERE status='completed'").fetchone()[0]
        # One CLI invocation may include a WebSocket warmup response. The
        # metadata ledger does not distinguish that from a generated response.
        assert completed>=1
        report.update(on_verified=True,completed_response_events=completed,autostart_registered=True)
        manager.turn_off()
        assert manager.config()[1].get('openai_base_url') is None
        assert not manager.task.inspect()['registered']
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if read_json(manager.runtime_path).get('phase')=='stopped':break
            time.sleep(.25)
        assert read_json(manager.runtime_path).get('phase')=='stopped'
        report.update(off_verified=True,worker_drained=True,autostart_removed=True)
    except Exception as exc:
        report['error']=str(exc)
        raise
    finally:
        manager.turn_off()
        report['user_config_unchanged']=real_config.read_bytes()==original
        report['user_config_sha256']=hashlib.sha256(original).hexdigest()
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    assert report['user_config_unchanged']
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
