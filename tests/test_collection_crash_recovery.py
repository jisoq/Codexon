"""Kill the real collector across its source and publication transaction boundaries."""
import json
import multiprocessing as mp
import sqlite3
import time
from collections import Counter

import pytest

from cachemonitor.usage_collection import CollectionClient, CollectorService
from test_core import fixture_home, usage


def _controlled_collector(home, path, control):
    service = CollectorService([home], path)
    try:
        service.poll()
        control.send('ready')
        while True:
            command = control.recv()
            if command == 'stop':
                return
            if command == 'before_commit':
                scan = service.index.scan_file

                def pause_scan(*args):
                    value = scan(*args)
                    if value:
                        control.send('paused')
                        control.recv()
                    return value

                service.index.scan_file = pause_scan
            elif command == 'before_publication':
                publish = service.channel.publish

                def pause_publication(*args):
                    control.send('paused')
                    control.recv()
                    return publish(*args)

                service.channel.publish = pause_publication
            service.poll()
            control.send('paused' if command == 'after_publication' else 'polled')
    finally:
        service.close()
        control.close()


def _receive(control, child, expected):
    assert control.poll(15), f'collector did not reach {expected}; exit={child.exitcode}'
    assert control.recv() == expected


def _keys(snapshot):
    return Counter(row['key'] for session in snapshot['sessions'] for row in session['history'])


@pytest.mark.integration
@pytest.mark.parametrize('kill_point', ['before_commit', 'before_publication', 'after_publication'])
def test_forced_kill_replays_source_once_and_reconnects_existing_client(tmp_path, kill_point):
    home, source = fixture_home(tmp_path)
    path = tmp_path / 'index.sqlite'
    context = mp.get_context('spawn')
    processes = []
    controls = []

    def launch():
        parent, child_pipe = context.Pipe()
        child = context.Process(target=_controlled_collector, args=(str(home), str(path), child_pipe))
        child.start()
        child_pipe.close()
        processes.append(child)
        controls.append(parent)
        _receive(parent, child, 'ready')
        return child, parent

    client = CollectionClient([home], path)
    try:
        child, control = launch()
        first = client.poll()
        assert not first.get('collection_recovery'), 'the crash test requires the live transport'
        assert _keys(first) == Counter({'r1': 1})
        first_instance = first['collection']['instance']
        complete = json.dumps(usage('committed-source', time=10020)) + '\n'
        partial = json.dumps(usage('partial-source', time=10021)) + '\n'
        split = len(partial) // 2
        with source.open('a', encoding='utf-8') as stream:
            stream.write(complete + partial[:split])
        control.send(kill_point)
        _receive(control, child, 'paused')
        # A second connection observes only the collector's completed transaction.
        with sqlite3.connect(path) as db:
            count = db.execute('SELECT count(*) FROM events').fetchone()[0]
        assert count == (1 if kill_point == 'before_commit' else 2)
        if kill_point == 'after_publication':
            assert _keys(client.poll()) == Counter({'r1': 1, 'committed-source': 1})
        child.kill()
        child.join(10)
        assert not child.is_alive() and child.exitcode != 0
        # No shutdown handler runs. An old durable snapshot must not become fresh.
        stale = client.poll(time.time() + 31)
        assert not stale['usage_collection_complete'] and stale['errors']
        resumed, resumed_control = launch()
        recovered = client.poll()
        assert not recovered.get('collection_recovery')
        assert recovered['collection']['instance'] != first_instance
        assert _keys(recovered) == Counter({'r1': 1, 'committed-source': 1})
        assert recovered['usage_collection_complete'] and not recovered['usage_errors']
        with source.open('a', encoding='utf-8') as stream:
            stream.write(partial[split:])
        resumed_control.send('poll')
        _receive(resumed_control, resumed, 'polled')
        after = client.poll()
        assert not after.get('collection_recovery')
        assert _keys(after) == Counter({'r1': 1, 'committed-source': 1, 'partial-source': 1})
        assert after['usage_collection_complete'] and not after['usage_errors']
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 3
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        resumed_control.send('stop')
        resumed.join(10)
        assert resumed.exitcode == 0
    finally:
        for child in processes:
            if child.is_alive():
                child.kill()
            child.join(10)
        for control in controls:
            control.close()
        client.close()
