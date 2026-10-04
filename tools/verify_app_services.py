"""Packaged Windows service exit/resume using isolated homes, tasks and ports."""
import argparse
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cachemonitor.app_services import AppServices, resume_proxy
from cachemonitor.observer_control import ObserverManager, atomic_write
from cachemonitor.observer_task import ObserverTask
from cachemonitor.proxy_target import ProxyTarget
from cachemonitor.observer_state import read_json
from cachemonitor.model_evidence import home_key
from cachemonitor.usage_collection import CollectionClient
from cachemonitor import proxy_identity as identity
from verify_proxy_update import IdleConnections
from tools.qa_ipc import command_reply

def await_value(read, predicate, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.2)
    raise AssertionError('Readiness timeout')

def legacy_schedule(task):
    """Seed an old repeating/restarting definition without starting it."""
    import base64
    assert all((c.isalnum() or c == '-' for c in task.name))
    script = "\n$service=New-Object -ComObject Schedule.Service\n$service.Connect()\n$folder=$service.GetFolder('\\')\n$task=$folder.GetTask('__TASK__')\n$definition=$task.Definition\n$trigger=$definition.Triggers.Create(1)\n$trigger.StartBoundary=(Get-Date).AddDays(1).ToString('yyyy-MM-ddTHH:mm:ss')\n$trigger.Repetition.Interval='PT1M'\n$definition.Settings.RestartCount=3\n$definition.Settings.RestartInterval='PT1M'\n[void]$folder.RegisterTaskDefinition('__TASK__',$definition,6,$definition.Principal.UserId,$null,3)\n".replace('__TASK__', task.name)
    encoded = base64.b64encode(script.encode('utf-16-le')).decode()
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded], capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    registration = task.inspect()
    assert registration['periodic'] and registration['restartCount'] == 3

def quit_gui(executable, home, index, evidence, cache, *, force=False):
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    import hashlib
    app = QCoreApplication.instance() or QCoreApplication([])
    report = index.parent / 'gui.json'
    report.unlink(missing_ok=True)
    command = [str(executable), '--codex-home', str(home), '--index-path', str(index), '--evidence-path', str(evidence), '--verify-handoff', str(report), '--verify-services', '--hidden']
    process = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        await_value(lambda: read_json(report), lambda r: r.get('ready'))
        client = QLocalSocket()
        client.connectToServer('CodexonQA-' + hashlib.sha256(str(index.resolve()).encode()).hexdigest()[:24])
        assert client.waitForConnected(3000)
        assert command_reply(client,b'verify-quit',b'quitting') == b'quitting'
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.2)
            choice = QLocalSocket()
            choice.connectToServer('CodexonQA-' + hashlib.sha256(str(index.resolve()).encode()).hexdigest()[:24])
            if not choice.waitForConnected(1000):
                continue
            try:
                command_reply(choice,b'verify-exit-force' if force else b'verify-exit-safe',
                              b'accepted',timeout_ms=1000)
            except RuntimeError:
                continue  # The GUI can close the socket while exiting; require zero exit below.
            break
        assert process.wait(timeout=90) == 0
    finally:
        if process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=15)
            process.wait(timeout=10)

def read_collection_snapshot(path):
    """Read both released IPC formats without initializing or changing the DB."""
    from contextlib import closing
    import sqlite3, zlib
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        row = db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
        if not row:
            return {}
        value = json.loads(zlib.decompress(row[0]))
        schema = value.get('collection_schema', 1)
        if schema == 1:
            return value
        if schema != 2:
            raise ValueError(f'Unsupported collector snapshot schema: {schema}')
        sessions = []
        for entry in value.pop('session_manifest'):
            stored = db.execute('SELECT revision,payload FROM sessions WHERE home=? AND sid=?', (entry['home'], entry['sid'])).fetchone()
            if stored is None or stored[0] != entry['revision']:
                raise ValueError('Collector session revision does not match the snapshot')
            sessions.append({**json.loads(zlib.decompress(stored[1])), **entry['state']})
        value['sessions'] = sessions
        if 'activity_revision' in value:
            value['request_activity'] = [json.loads(zlib.decompress(row[0])) for row in db.execute('SELECT payload FROM activity ORDER BY position')]
            value.pop('activity_revision')
        return value

def verify_legacy_collector(root, old, new):
    import sqlite3, zlib
    from cachemonitor.usage_collection import locked
    from verify_changes import fixture_home
    root.mkdir()
    home = fixture_home(root)
    index = root / 'custom index.sqlite'
    evidence = root / 'custom evidence.sqlite'
    task = ObserverTask(str(index), role='UsageCollector')
    channels = {index.with_suffix('.collection.sqlite'): index.with_suffix('.collector.lock'), index.with_name(index.name + '.codexon-collection.sqlite'): index.with_name(index.name + '.codexon-collector.lock')}
    previous_channel = None
    client = None

    def snapshot():
        nonlocal previous_channel
        for path in channels:
            if not path.exists():
                continue
            try:
                value = read_collection_snapshot(path)
                if value.get('collection'):
                    previous_channel = path
                    return value
            except sqlite3.OperationalError:
                pass
        return {}
    try:
        task.start([str(old), '--usage-collector', '--codex-home', str(home), '--index-path', str(index), '--evidence-path', str(evidence)])
        original = await_value(snapshot, lambda s: s.get('collection') and sum((len(v.get('history', [])) for v in s.get('sessions', []))) >= 2)
        before = original['collection']

        def records(value):
            return {(v['id'], r['key']): {k: r.get(k) for k in ('ts', 'input', 'cached', 'written', 'output', 'reasoning', 'total')} for v in value['sessions'] for r in v['history']}
        expected = records(original)
        process = identity.process_identity(before['pid'])
        AppServices(None, [str(home)], index, evidence).start_collection()
        client = CollectionClient([home], index, evidence)
        migrated = await_value(client.poll, lambda s: s.get('collection', {}).get('pid') not in (None, before['pid']) and len(records(s)) == len(expected))
        after = migrated['collection']
        assert records(migrated) == expected
        with sqlite3.connect(client.channel.snapshot_path) as db:
            schema = json.loads(zlib.decompress(db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0]))['collection_schema']
        assert schema == 2
        assert after['executable'] == str(new) and (not identity.same_process(process))
        if previous_channel != client.channel.snapshot_path:
            assert not locked(channels[previous_channel])
        assert locked(client.channel.companion('.collector.lock'))
        client.close()
        client = None
        AppServices(None, [str(home)], index, evidence).stop_collection()
        assert not identity.process_identity(after['pid'])
        assert all((not locked(lock) for lock in channels.values()))
        AppServices(None, [str(home)], index, evidence).start_collection()
        client = CollectionClient([home], index, evidence)
        resumed = await_value(client.poll, lambda s: s.get('collection', {}).get('pid') not in (None, after['pid']) and len(records(s)) == len(expected))
        assert records(resumed) == expected
        client.close()
        client = None
        AppServices(None, [str(home)], index, evidence).stop_collection()
        assert not identity.process_identity(resumed['collection']['pid'])
        assert all((not locked(lock) for lock in channels.values()))
        return dict(before=before, after=after, previous_channel=previous_channel.name, cooperative=True, old_instance_exited=True, previous_schema=original.get('collection_schema', 1), current_schema=schema, preserved_calls=len(expected), preserved_sessions=len(migrated['sessions']), restart_preserved=True)
    finally:
        if client:
            client.close()
        task.stop()
        task.remove()

def verify(root, executable, role):
    home = root / 'home'
    home.mkdir(parents=True)
    (home / 'codexon-test-home').touch()
    data = root / 'data'
    data.mkdir()
    index = data / 'index.sqlite'
    evidence = data / 'model-evidence.sqlite'
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    assert port not in (8768, 8771)
    url = f'http://127.0.0.1:{port}'
    cache = False
    manager = ObserverManager(home, data, url)
    manager.write_state(dict(home=home_key(home), enabled=True, phase='active', previous_url=None, url=url))
    manager.set_url(url)
    upstream = IdleConnections()
    command = [str(executable), '--model-proxy', '--codex-home', str(home), '--evidence-path', str(evidence), '--port', str(port), '--upstream-url', upstream.url]
    command += ['--managed']
    collector = ObserverTask(str(index.resolve()), role='UsageCollector')
    client = None
    tasks = [manager.task, manager.legacy_task, ObserverTask(home_key(home), role='ConnectionCheck'), collector, ObserverTask(home_key(home), role='ProxyUpdate')]
    services = AppServices(manager, [str(home)], index, evidence)
    try:
        checker = tasks[2]
        checker.configure([str(executable.with_name('CodexonRecovery.exe')), '--check', '--codex-home', str(home), '--data-dir', str(data), '--proxy-url', url], autostart=True)
        legacy_schedule(checker)
        manager.task.start(command, autostart=True)
        legacy_schedule(manager.task)
        before = await_value(lambda: manager.health(timeout=1), lambda h: bool(h))
        source = ProxyTarget(manager).capture(before)
        assert source['role'] == role
        reboot_recovered = False
        collector.start([str(executable), '--usage-collector', '--codex-home', str(home), '--index-path', str(index), '--evidence-path', str(evidence)])
        client = CollectionClient([home], index, evidence)
        snapshot = await_value(client.poll, lambda s: bool(s.get('collection')))
        old_collector = identity.process_identity(snapshot['collection']['pid'])
        client.close()
        client = None
        upstream.open(url, 8)
        await_value(lambda: manager.health(timeout=1), lambda h: h and h['websocket_states']['idle'] == 8)
        manager.task.suspend()
        assert manager.health(timeout=2)['instance'] == before['instance']
        quit_gui(executable, home, index, evidence, False)
        assert not checker.inspect()['registered']
        assert ProxyTarget(manager).stopped(source) and (not identity.same_process(old_collector))
        assert manager.config()[1].get('openai_base_url') is None
        for task in tasks:
            registered = task.inspect()
            assert not registered.get('running') and (not registered.get('autostart')) and (not registered.get('periodic'))
            if registered['registered']:
                assert not registered['enabled'] and registered['restartCount'] == 0
        assert resume_proxy(manager)
        after = manager.health(timeout=2)
        assert after['instance'] != before['instance'] and after['role'] == role and (not after['draining'])
        assert not manager.task.inspect()['autostart']
        assert manager.config()[1]['openai_base_url'] == url
        assert not resume_proxy(manager)
        upstream.open(url, 2)
        import asyncio

        async def busy():
            for ws in upstream.sockets:
                if not ws.closed:
                    await ws.send_json({'type': 'response.create', 'model': 'synthetic'})
        asyncio.run_coroutine_threadsafe(busy(), upstream.loop).result(5)
        await_value(lambda: manager.health(timeout=1), lambda h: h and h['websocket_states']['responding'] == 2)
        forced_source = ProxyTarget(manager).capture(manager.health(timeout=2))
        quit_gui(executable, home, index, evidence, False, force=True)
        assert ProxyTarget(manager).stopped(forced_source)
        assert identity.port_free(url) and identity.locks_free(ProxyTarget(manager).locks)
        return dict(role=role, before={k: before.get(k) for k in ('pid', 'version', 'instance', 'executable')}, resumed={k: after.get(k) for k in ('pid', 'version', 'instance', 'executable')}, gui_exited=True, collector_stopped=True, slot_cleanup=True, tasks_suspended=True, route_restored=True, reboot_recovered=reboot_recovered, legacy_check_removed=True)
    finally:
        if client:
            client.close()
        upstream.close()
        for task in tasks:
            task.stop()
            task.remove()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--legacy-collector-exe', type=Path)
    args = parser.parse_args()
    exe = args.executable.resolve()
    root = args.output.resolve()
    assert exe.is_file() and (not root.exists())
    root.mkdir(parents=True)
    original = sys.executable
    frozen = getattr(sys, 'frozen', None)
    sys.executable = str(exe)
    sys.frozen = True
    try:
        legacy = verify_legacy_collector(root / 'legacy-collector', args.legacy_collector_exe.resolve(), exe) if args.legacy_collector_exe else None
        results = [verify(root / role, exe, role) for role in ('observer',)]
        if legacy:
            (root / 'legacy-collector.json').write_text(json.dumps(legacy, indent=2), encoding='utf-8')
        (root / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(json.dumps(results), flush=True)
    finally:
        sys.executable = original
        if frozen is None:
            del sys.frozen
        else:
            sys.frozen = frozen
if __name__ == '__main__':
    main()
