"""Migrate actual legacy worker binaries through the new packaged GUI."""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.observer_control import ObserverManager,atomic_write
from cachemonitor.observer_task import ObserverTask
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key
from cachemonitor.version import PROXY_VERSION


def wait(read,valid,seconds=90):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=read()
        if valid(value):return value
        time.sleep(.2)
    raise RuntimeError('Packaged retirement verification timeout')


def verify(root,old,new,role):
    root.mkdir(parents=True,exist_ok=False);home=root/'custom home';home.mkdir();(home/'codexon-test-home').touch()
    data=root/'data';data.mkdir();index=data/'index.sqlite';evidence=data/'model-evidence.sqlite'
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    assert port!=8768
    manager=ObserverManager(home,data,f'http://127.0.0.1:{port}');manager.retirement_index=index
    manager.write_state(dict(home=home_key(home),url=manager.url,enabled=True,phase='active',previous_url=None,upstream='chatgpt'))
    manager.set_url(manager.url)
    atomic_write(data/'cache-route.json',json.dumps(dict(url=manager.url)).encode())
    hooks=home/'hooks.json';foreign=dict(description='synthetic user hook',hooks=[])
    hooks.write_text(json.dumps(dict(hooks={'Stop':[dict(description='codexon-cache-control',hooks=[]),foreign]})),encoding='utf-8')
    task=ObserverTask(str(home),role='CacheWorker');gui=None
    try:
        task.start([str(old),'--model-proxy',role,'--codex-home',str(home),'--evidence-path',str(evidence),
            '--observation-index',str(index),'--port',str(port)],autostart=False)
        before=wait(lambda:manager.health(timeout=1),lambda v:bool(v and v.get('cache_management')))
        database=data/'cache-control.sqlite'
        with closing(sqlite3.connect(database)) as db,db:
            cols={r[1] for r in db.execute('PRAGMA table_info(cache_jobs)')}
            record=dict(id='synthetic-retirement',home=str(home),sid='synthetic-thread',generation=1,state='completed',started=1.,ended=2.,
                response_id='synthetic-retired-response',model='gpt-6.1-sol',effort='xhigh',tier='default',
                usage=json.dumps(dict(input=100,cached=80,written=0,output=5,reasoning=0,total=105)),purpose='maintenance')
            record={k:v for k,v in record.items() if k in cols}
            db.execute('INSERT INTO cache_jobs('+','.join(record)+') VALUES('+','.join('?' for _ in record)+')',tuple(record.values()))
        report=data/'gui.json'
        gui=subprocess.Popen([str(new),'--codex-home',str(home),'--index-path',str(index),'--evidence-path',str(evidence),
            '--verify-handoff',str(report),'--verify-services','--hidden'],creationflags=subprocess.CREATE_NO_WINDOW)
        wait(lambda:read_json(report),lambda v:v.get('ready'))
        after=wait(lambda:manager.health(timeout=1),lambda v:bool(v and v.get('version')==PROXY_VERSION and
            v.get('lifecycle')=='managed' and v.get('instance')!=before['instance'] and not v.get('cache_management')))
        wait(lambda:database.exists(),lambda exists:not exists)
        assert manager.config()[1]['openai_base_url']==manager.url
        assert not task.inspect().get('registered')
        assert json.loads(hooks.read_text())['hooks']['Stop']==[foreign]
        with closing(sqlite3.connect(index)) as db:
            row=db.execute('SELECT data FROM usage_archive WHERE home=? AND response_id=?',(str(home),'synthetic-retired-response')).fetchone()
            assert row and json.loads(row[0])['cached']==80
        # Request cooperative GUI shutdown through its existing isolated QA IPC.
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtNetwork import QLocalSocket
        app=QCoreApplication.instance() or QCoreApplication([])
        name='CodexonQA-'+hashlib.sha256(str(index.resolve()).encode()).hexdigest()[:24]
        def send(command):
            client=QLocalSocket();client.connectToServer(name);assert client.waitForConnected(3000)
            client.write(command);assert client.waitForBytesWritten(3000)
            client.waitForReadyRead(3000);return bytes(client.readAll())
        assert send(b'verify-quit')==b'quitting'
        wait(lambda:send(b'verify-exit-safe') if gui.poll() is None else b'accepted',lambda v:v==b'accepted')
        assert gui.wait(timeout=90)==0
        assert not database.exists()
        return dict(role=role,route_preserved=True,usage_preserved=True,hooks_removed=True,registration_removed=True,old_instance=before['instance'],new_instance=after['instance'])
    finally:
        if gui and gui.poll() is None:
            subprocess.run(['taskkill','/PID',str(gui.pid),'/T','/F'],capture_output=True,timeout=15)
        for owner in (task,manager.task,manager.legacy_task,ObserverTask(str(index.resolve()),role='UsageCollector')):
            owner.stop();owner.remove()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-exe',type=Path,required=True);parser.add_argument('--new-exe',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    records=[verify(root/str(i),args.old_exe.resolve(),args.new_exe.resolve(),role) for i,role in enumerate(('--cache-worker','--cache-observe-only'))]
    (root/'result.json').write_text(json.dumps(dict(passed=True,records=records),indent=2),encoding='utf-8')
    return 0


if __name__=='__main__':raise SystemExit(main())
