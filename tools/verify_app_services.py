"""Packaged Windows service exit/resume using isolated homes, tasks and ports."""
import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.app_services import AppServices,resume_proxy
from cachemonitor.observer_control import ObserverManager,atomic_write
from cachemonitor.cache_worker_control import CacheWorkerManager
from cachemonitor.observer_task import ObserverTask
from cachemonitor.proxy_target import ProxyTarget
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key
from cachemonitor.usage_collection import CollectionClient,resume_collection
from cachemonitor import proxy_identity as identity
from verify_proxy_update import IdleConnections


def await_value(read,predicate,seconds=30):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=read()
        if predicate(value):return value
        time.sleep(.2)
    raise AssertionError('Readiness timeout')


def quit_gui(executable,home,index,evidence,cache):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    import hashlib
    app=QCoreApplication.instance() or QCoreApplication([])
    report=index.parent/'gui.json'
    command=[str(executable),'--codex-home',str(home),'--index-path',str(index),'--evidence-path',str(evidence),
             '--verify-handoff',str(report),'--verify-services','--hidden']
    if cache:command.append('--cache-control')
    process=subprocess.Popen(command,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        await_value(lambda:read_json(report),lambda r:r.get('ready'))
        client=QLocalSocket()
        client.connectToServer('CodexonQA-'+hashlib.sha256(str(index.resolve()).encode()).hexdigest()[:24])
        assert client.waitForConnected(3000)
        client.write(b'verify-quit');assert client.waitForBytesWritten(3000)
        assert client.waitForReadyRead(3000) and bytes(client.readAll())==b'quitting'
        assert process.wait(timeout=90)==0
    finally:
        if process.poll() is None:
            # The isolated fixture owns this GUI and its analysis child only.
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
            process.wait(timeout=10)


def verify_legacy_collector(root,old,new):
    import sqlite3,zlib
    from cachemonitor.usage_collection import locked
    root.mkdir();home=root/'home';home.mkdir();index=root/'index.sqlite';evidence=root/'evidence.sqlite'
    task=ObserverTask(str(index),role='UsageCollector')
    legacy=index.with_suffix('.collection.sqlite');client=None
    def snapshot():
        if not legacy.exists():return {}
        try:
            with sqlite3.connect(legacy.as_uri()+'?mode=ro',uri=True) as db:
                row=db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
                return json.loads(zlib.decompress(row[0])) if row else {}
        except sqlite3.OperationalError:return {}
    try:
        task.start([str(old),'--usage-collector','--codex-home',str(home),'--index-path',str(index),'--evidence-path',str(evidence)])
        before=await_value(snapshot,lambda s:s.get('collection'))['collection']
        process=identity.process_identity(before['pid'])
        client=CollectionClient([home],index,evidence)
        after=await_value(client.poll,lambda s:s.get('collection',{}).get('pid') not in (None,before['pid']))['collection']
        assert after['executable']==str(new) and not identity.same_process(process)
        assert not locked(index.with_suffix('.collector.lock'))
        client.close();client=None
        AppServices(None,[str(home)],index,evidence).stop_collection()
        assert not identity.process_identity(after['pid'])
        return dict(before=before,after=after,cooperative=True,old_instance_exited=True)
    finally:
        if client:client.close()
        task.stop();task.remove()


def verify(root,executable,role):
    home=root/'home';home.mkdir(parents=True)
    (home/'codexon-test-home').touch()
    data=root/'data';data.mkdir()
    index=data/'index.sqlite';evidence=data/'model-evidence.sqlite'
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
    assert port not in (8768,8771)
    url=f'http://127.0.0.1:{port}'
    cache=role!='observer'
    if cache:
        atomic_write(index.with_name('cache-route.json'),json.dumps({'url':url}).encode())
        manager=CacheWorkerManager(home,index,evidence)
    else:manager=ObserverManager(home,data,url)
    manager.write_state(dict(home=home_key(home),enabled=True,phase='active',previous_url=None,url=url))
    manager.set_url(url)
    upstream=IdleConnections()
    command=[str(executable),'--model-proxy','--codex-home',str(home),'--evidence-path',str(evidence),
             '--port',str(port),'--upstream-url',upstream.url]
    command+=['--cache-observe-only' if role=='cache-observer' else '--cache-worker','--observation-index',str(index)] if cache else ['--managed']
    collector=ObserverTask(str(index.resolve()),role='UsageCollector')
    client=None
    tasks=[manager.task,manager.check_task,manager.legacy_task,collector,ObserverTask(home_key(home),role='ProxyUpdate')]
    services=AppServices(manager,[str(home)],index,evidence)
    try:
        resume_collection([home],index,evidence)
        manager.task.start(command,autostart=True)
        before=await_value(lambda:manager.health(timeout=1),lambda h:bool(h))
        source=ProxyTarget(manager).capture(before)
        assert source['role']==role
        collector.start([str(executable),'--usage-collector','--codex-home',str(home),
                         '--index-path',str(index),'--evidence-path',str(evidence)])
        client=CollectionClient([home],index,evidence,autostart=False)
        snapshot=await_value(client.poll,lambda s:bool(s.get('collection')))
        old_collector=identity.process_identity(snapshot['collection']['pid'])
        client.close();client=None
        upstream.open(url,8)
        await_value(lambda:manager.health(timeout=1),lambda h:h and h['websocket_states']['idle']==8)
        # Disabling restart/triggers must not terminate a running relay.
        manager.task.suspend()
        assert manager.health(timeout=2)['instance']==before['instance']
        quit_gui(executable,home,index,evidence,cache)
        assert ProxyTarget(manager).stopped(source) and not identity.same_process(old_collector)
        assert manager.config()[1].get('openai_base_url') is None
        for task in tasks:
            registered=task.inspect()
            assert not registered.get('running') and not registered.get('autostart') and not registered.get('periodic')
            if registered['registered']:assert not registered['enabled'] and registered['restartCount']==0
        resume_collection([home],index,evidence)
        assert resume_proxy(manager)
        after=manager.health(timeout=2)
        assert after['instance']!=before['instance'] and after['role']==role and not after['draining']
        assert not manager.task.inspect()['autostart']
        assert manager.config()[1]['openai_base_url']==url
        assert not resume_proxy(manager)
        services.stop()
        assert identity.port_free(url) and identity.locks_free(ProxyTarget(manager).locks)
        return dict(role=role,before={k:before.get(k) for k in ('pid','version','instance','executable')},
                    resumed={k:after.get(k) for k in ('pid','version','instance','executable')},
                    gui_exited=True,collector_stopped=True,slot_cleanup=True,tasks_suspended=True,route_restored=True)
    finally:
        if client:client.close()
        upstream.close()
        # Scoped QA cleanup only, including a failed assertion's own children.
        for task in tasks:
            task.stop();task.remove()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--legacy-collector-exe',type=Path)
    args=parser.parse_args();exe=args.executable.resolve();root=args.output.resolve()
    assert exe.is_file() and not root.exists()
    root.mkdir(parents=True)
    original=sys.executable;frozen=getattr(sys,'frozen',None)
    sys.executable=str(exe);sys.frozen=True
    try:
        legacy=verify_legacy_collector(root/'legacy-collector',args.legacy_collector_exe.resolve(),exe) if args.legacy_collector_exe else None
        results=[verify(root/role,exe,role) for role in ('observer','cache-worker','cache-observer')]
        if legacy:(root/'legacy-collector.json').write_text(json.dumps(legacy,indent=2),encoding='utf-8')
        (root/'result.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(json.dumps(results),flush=True)
    finally:
        sys.executable=original
        if frozen is None:del sys.frozen
        else:sys.frozen=frozen


if __name__=='__main__':main()
