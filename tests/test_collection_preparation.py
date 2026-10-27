"""Pending replay recovery stays visible and keeps the collector working."""
import json
from pathlib import Path
import sqlite3
from time import monotonic
from types import SimpleNamespace

import pytest

from cachemonitor import index as index_module
from cachemonitor import usage_collection
from cachemonitor.collection_replay import CollectionReplay
from cachemonitor.index import UsageIndex
from cachemonitor.collection_wake import SourceHint
from cachemonitor.core import TITLE_HANDLER, TITLE_PROMPT
from cachemonitor.model_evidence import EvidenceReader, EvidenceStore
from test_core import TID, fixture_home, usage


@pytest.fixture
def restoring_index(tmp_path):
    index = UsageIndex([], tmp_path/'index.sqlite', tmp_path/'evidence.sqlite')
    index.replay = SimpleNamespace(begin=lambda now: None, pending=set(),
                                   restore_job=iter((None,)), close=lambda: None)
    try:
        yield index
    finally:
        index.close()


def test_replay_recovery_remains_loading_until_restore_finishes(restoring_index):
    index = restoring_index
    pending = index.poll(1000)
    assert not pending['usage_errors'] and not pending['collection_fault']
    assert not pending['usage_collection_complete']
    assert pending['index']['loading']

    index.replay.restore_job = None
    complete = index.poll(1001)
    assert complete['usage_collection_complete']
    assert not complete['index']['loading']


def _next_collector_wait(monkeypatch, snapshot, path):
    clock = [0.0]
    waits = []
    polled = []

    class Changes:
        def __init__(self, *args, evidence=None):
            pass

        def sync(self, homes):
            pass

        def wait(self, deadline):
            waits.append(deadline-clock[0])
            clock[0] = deadline

        def close(self):
            pass

    def poll(**kwargs):
        polled.append(True)
        return snapshot

    channel = SimpleNamespace(scope='fixture', path=path, snapshot_path=path, homes=[])
    service = SimpleNamespace(channel=channel, instance='fixture',
                              stopping=lambda: bool(polled), poll=poll,
                              maintain=lambda: False, close=lambda: None)
    with monkeypatch.context() as patch:
        patch.setattr(usage_collection, 'CollectorService', lambda *args: service)
        patch.setattr('cachemonitor.collection_wake.SourceChanges', Changes)
        patch.setattr('cachemonitor.observer_control.atomic_write', lambda *args: None)
        patch.setattr(usage_collection.time, 'monotonic', lambda: clock[0])
        patch.setattr('sys.argv', ['collector', '--codex-home', str(path.parent),
                                  '--index-path', str(path)])
        usage_collection.main()
    assert len(waits) == 1
    return waits[0]


def test_pending_replay_uses_active_wait_instead_of_idle_wait(restoring_index, monkeypatch):
    index = restoring_index
    pending = index.poll(1000)
    index.replay.restore_job = None
    complete = index.poll(1001)

    pending_wait = _next_collector_wait(monkeypatch, pending, index.path)
    idle_wait = _next_collector_wait(monkeypatch, complete, index.path)
    assert pending_wait < idle_wait


def _collect_complete(service, now):
    deadline = monotonic() + 5
    while True:
        snapshot = service.poll(now)
        assert not snapshot['usage_errors'] and not snapshot['collection_fault']
        if snapshot['usage_collection_complete']:
            assert not snapshot['index']['loading']
            return snapshot
        assert monotonic() < deadline, 'Replayed owner never completes after metadata invalidation'


@pytest.fixture
def replayed_owner(tmp_path):
    home, path = fixture_home(tmp_path)
    with path.open('a', encoding='utf8') as stream:
        for number in range(24):
            item = usage(f'row-{number}', time=10001+number)
            if number == 23:
                item['payload']['model'] = 'source-before-rewrite'
            stream.write(json.dumps(item) + '\n')
    target = tmp_path/'index.sqlite'
    evidence = tmp_path/'evidence.sqlite'
    service = usage_collection.CollectorService([home], target, evidence)
    try:
        before = _collect_complete(service, 20000)['sessions'][0]['history']
        assert len(before) == 25
        assert [(row['input'], row['cached'], row['output'], row['total']) for row in before] == [
            (10000, 9000, 10, 10010)] * 25
        deadline = monotonic() + 5
        while service.maintain():
            assert monotonic() < deadline
    finally:
        service.close()

    service = usage_collection.CollectorService([home], target, evidence)
    try:
        assert _collect_complete(service, 20001)['sessions'][0]['history'] == before
        assert hasattr(service.index.monitor.sessions[(str(home), TID)], '_replay_body')
        yield home, path, evidence, service, before
    finally:
        service.close()


@pytest.fixture
def replay_budget(replayed_owner, monkeypatch):
    clock = [0.0]
    replayed = set()
    consume = index_module.Session.consume

    def consume_with_budget(session, event, now):
        consume(session, event, now)
        clock[0] += .12
        replayed.add(event.get('payload', {}).get('response_id'))

    with monkeypatch.context() as patch:
        patch.setattr(index_module, 'REBUILD_BATCH', 4)
        patch.setattr(index_module.time, 'monotonic', lambda: clock[0])
        patch.setattr(index_module.Session, 'consume', consume_with_budget)
        yield clock, replayed, patch


@pytest.mark.parametrize('during_rebuild', ['unchanged', 'metadata', 'source', 'evidence'])
def test_replayed_owner_metadata_rebuild_completes(replayed_owner, replay_budget, during_rebuild):
    home, path, evidence, service, before = replayed_owner
    clock, replayed, _ = replay_budget
    cwd = 'D:/changed-project'
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('UPDATE threads SET cwd=?', (cwd,))
    now = 20020
    deadline = monotonic() + 5
    while not clock[0]:
        pending = service.poll(now)
        assert not pending['usage_collection_complete']
        assert monotonic() < deadline
    if during_rebuild == 'metadata':
        while 'row-22' not in replayed:
            pending = service.poll(now)
            assert not pending['usage_collection_complete']
            assert monotonic() < deadline
        cwd = 'D:/latest-project'
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET cwd=?', (cwd,))
        now += 20
        latest = service.poll(now, source_hint=SourceHint(metadata=frozenset({home.resolve()})))
        if latest['usage_collection_complete']:
            assert latest['sessions'][0]['cwd'] == cwd
    elif during_rebuild == 'source':
        path.write_text(json.dumps(usage('rewritten', input=200, cached=50)) + '\n', encoding='utf8')
    elif during_rebuild == 'evidence':
        store = EvidenceStore(evidence)
        try:
            store.write(home, 'attempt', 10000, 'WebSocket', 'r1', 'first-wire', 'first-wire', 'completed')
            assert not service.poll(now)['usage_collection_complete']
            store.write(home, 'attempt', 10000, 'WebSocket', 'r1', 'latest-wire', 'latest-wire', 'completed')
        finally:
            store.close()
    complete = _collect_complete(service, now)
    assert len(complete['sessions']) == 1
    session = complete['sessions'][0]
    assert session['collection_complete']
    assert session['cwd'] == cwd
    if during_rebuild == 'source':
        assert [(row['key'], row['input'], row['cached'], row['output'], row['total'])
                for row in session['history']] == [('rewritten', 200, 50, 10, 210)]
        assert session['model'] == 'test'
        assert session['activity'] == 10000
    elif during_rebuild == 'evidence':
        assert [(row['key'], row['input'], row['cached'], row['output'], row['total'])
                for row in session['history']] == [
                    (row['key'], row['input'], row['cached'], row['output'], row['total']) for row in before]
        assert session['history'][0]['model'] == 'latest-wire'
        assert session['history'][0]['requested_model'] == 'latest-wire'
        assert session['history'][0]['response_model'] == 'latest-wire'
        assert session['history'][1:] == before[1:]
    else:
        assert session['history'] == before
    published = service.channel.read(shared=True)
    assert published['usage_collection_complete']
    assert published['sessions'][0]['cwd'] == cwd
    assert published['sessions'][0]['history'] == session['history']


def test_replayed_owner_preserves_live_log_facts(replayed_owner, replay_budget, tmp_path):
    home, path, evidence, service, before = replayed_owner
    clock, _, budget = replay_budget
    title = ('session_loop{thread_id=' + TID + '}: Submission sub=Submission { id: "t", '
             'op: TurnInput { request: TurnInputRequest { input: UserInput { content: [Text { text: "'
             + TITLE_PROMPT + '" }], "required": Array [String("title"), String("description")]')
    with sqlite3.connect(home/'logs_2.sqlite') as db:
        db.execute('INSERT INTO logs VALUES(2,20018,0,?,?,?,?)', (TITLE_HANDLER, TID, 'p1', title))
        for number, timestamp, model in [(3, 20017, 'first-wire'), (4, 20016, 'second-wire')]:
            db.execute("INSERT INTO logs VALUES(?,?,0,'feedback_tags',?,'p1',?)", (number, timestamp, TID,
                       f'turn{{model={model}}}:request{{transport="responses_websocket" api.path="/responses"}}: auth_header_attached=true'))
    now = 20020
    deadline = monotonic() + 5
    while not clock[0]:
        pending = service.poll(now)
        assert not pending['usage_collection_complete']
        assert monotonic() < deadline
    with sqlite3.connect(home/'logs_2.sqlite') as db:
        db.execute("INSERT INTO logs VALUES(5,20015,0,'feedback_tags',?,'p2',?)", (TID,
                   'turn{model=last-wire}:request{transport="responses_http" api.path="/responses"}: auth_header_attached=true'))
    complete = _collect_complete(service, now)
    published = service.channel.read(shared=True)
    budget.undo()
    cold = usage_collection.CollectorService([home], tmp_path/'cold.sqlite', evidence)
    try:
        fresh = _collect_complete(cold, now)
        fields = ('model', 'activity', 'transport', 'transport_ts', 'endpoint', 'transport_evidence',
                  'transports', 'unmatched_transports', 'history')
        for snapshot in (complete, published):
            assert snapshot['usage_collection_complete']
            assert len(snapshot['sessions']) == len(fresh['sessions']) == 1
            assert {field: snapshot['sessions'][0][field] for field in fields} == {
                field: fresh['sessions'][0][field] for field in fields}
            assert snapshot['excluded_title_sessions'] == fresh['excluded_title_sessions'] == 0
        assert complete['sessions'][0]['model'] == 'last-wire'
        assert complete['sessions'][0]['activity'] == 20017
        assert [row['ts'] for row in complete['sessions'][0]['transports']] == [20017, 20016, 20015]
        assert [(row['key'], row['total']) for row in complete['sessions'][0]['history']] == [
            (row['key'], row['total']) for row in before]
        key = str(home), TID
        assert service.index.monitor.sessions[key].title_request
        assert cold.index.monitor.sessions[key].title_request
    finally:
        cold.close()


@pytest.fixture
def cached_sources(tmp_path):
    home, path = fixture_home(tmp_path)
    other = '23456789-abcd-abcd-abcd-123456789012'
    second = home/'second.jsonl'
    second.write_text(json.dumps(usage('r2',tid=other,input=500,cached=200))+'\n',encoding='utf8')
    with sqlite3.connect(home/'logs_2.sqlite') as db:
        db.execute('DELETE FROM logs')
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('INSERT INTO threads(id,rollout_path,model) VALUES(?,?,?)',(other,str(second),'test'))
    evidence = tmp_path/'evidence.sqlite'
    store = EvidenceStore(evidence)
    try:
        for number, response in enumerate(('r1','r2','outside')):
            store.write(home,str(number),10000+number,'WebSocket',response,'test','test','completed')
    finally:
        store.close()
    target = tmp_path/'index.sqlite'
    service = usage_collection.CollectorService([home],target,evidence)
    try:
        first = _collect_complete(service,20000)
        histories = {row['id']:row['history'] for row in first['sessions']}
        assert {sid:[(r['key'],r['input'],r['cached'],r['output'],r['total']) for r in rows]
                for sid,rows in histories.items()} == {
            TID:[('r1',10000,9000,10,10010)],other:[('r2',500,200,10,510)]}
        deadline = monotonic()+5
        while service.maintain():
            assert monotonic() < deadline
        assert service.index.replay.db.execute('SELECT count(*) FROM owners').fetchone()[0] == 2
        replay_path = service.channel.companion('.collection-replay.sqlite')
    finally:
        service.close()
    return SimpleNamespace(home=home,path=path,evidence=evidence,target=target,
                           other=other,histories=histories,replay_path=replay_path)


@pytest.fixture
def catching_service(cached_sources,monkeypatch):
    source = cached_sources
    monkeypatch.setattr(EvidenceReader,'MAX_POLL_ROWS',1)
    rebuilt = []
    original = UsageIndex.rebuild_steps
    def track(index,key,now):
        rebuilt.append(key[1])
        yield from original(index,key,now)
    monkeypatch.setattr(UsageIndex,'rebuild_steps',track)
    service = usage_collection.CollectorService([source.home],source.target,source.evidence)
    try:
        yield source,service,rebuilt
    finally:
        service.close()


def test_evidence_catchup_defers_unchanged_cached_owners(catching_service):
    source,service,rebuilt = catching_service
    pending = service.poll(20001)
    replay = service.index.replay
    assert service.index.model_evidence.catching_up
    assert replay.pending == {(str(source.home),sid) for sid in source.histories}
    assert not replay.started and replay.restore_job is None
    assert pending['index']['loading'] and not pending['usage_collection_complete']
    assert not rebuilt
    complete = _collect_complete(service,20002)
    assert {row['id']:row['history'] for row in complete['sessions']} == source.histories
    assert not rebuilt and not replay.pending and replay.restore_job is None


@pytest.mark.parametrize('change',['append','rewrite'])
def test_evidence_catchup_does_not_defer_changed_source(catching_service,change):
    source,service,rebuilt = catching_service
    if change == 'append':
        with source.path.open('a',encoding='utf8') as output:
            output.write(json.dumps(usage('new',time=10003,input=200,cached=50))+'\n')
        expected = [('r1',10000,9000,10,10010),('new',200,50,10,210)]
    else:
        source.path.write_text(json.dumps(usage('new',input=200,cached=50))+'\n',encoding='utf8')
        expected = [('new',200,50,10,210)]
    pending = service.poll(20001)
    assert service.index.model_evidence.catching_up
    assert TID in rebuilt and source.other not in rebuilt
    assert (str(source.home),TID) not in service.index.replay.pending
    assert pending['index']['loading']
    complete = _collect_complete(service,20002)
    histories = {row['id']:row['history'] for row in complete['sessions']}
    assert [(r['key'],r['input'],r['cached'],r['output'],r['total']) for r in histories[TID]] == expected
    assert histories[source.other] == source.histories[source.other]
    assert set(rebuilt) == {TID}


def test_early_pending_catalog_cap_keeps_uncatalogued_source_fallback(catching_service,monkeypatch):
    source,service,rebuilt = catching_service
    opening = CollectionReplay.open
    oversized = ('','z'*(4*1024*1024))
    admitted = ('','admitted')
    def open_catalog(*args,**kwargs):
        replay = opening(*args,**kwargs)
        database = replay.db
        class Catalog:
            def execute(self,sql,*values):
                if sql == 'SELECT home,sid FROM owners':
                    return iter([admitted,oversized,*database.execute(sql,*values)])
                return database.execute(sql,*values)
            def __getattr__(self,name):
                return getattr(database,name)
            def __enter__(self):
                database.__enter__()
                return self
            def __exit__(self,*values):
                return database.__exit__(*values)
        replay.db = Catalog()
        return replay
    monkeypatch.setattr(CollectionReplay,'open',open_catalog)
    service.poll(20001)
    assert service.index.model_evidence.catching_up
    assert service.index.replay.pending == {admitted}
    assert set(rebuilt) == set(source.histories)
    complete = _collect_complete(service,20002)
    assert {row['id']:row['history'] for row in complete['sessions']} == source.histories


def test_evidence_read_error_releases_early_deferrals(catching_service,monkeypatch):
    source,service,rebuilt = catching_service
    service.poll(20001)
    assert service.index.replay.pending and not rebuilt
    stat = Path.stat
    def unreadable(path,*args,**kwargs):
        if path == source.evidence:
            raise PermissionError('blocked evidence stat')
        return stat(path,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'stat',unreadable)
        for now in (20002,20003,20004):
            pending = service.poll(now)
            assert service.index.model_evidence.error
            assert not service.index.replay.pending
            assert service.index.replay.restore_job is None
            assert pending['index']['loading'] and not pending['usage_collection_complete']
        assert set(rebuilt) == set(source.histories)
        assert service.index.loaded == {(str(source.home),sid) for sid in source.histories}
    complete = _collect_complete(service,20005)
    assert {row['id']:row['history'] for row in complete['sessions']} == source.histories


@pytest.mark.parametrize('presence',['loaded','log_only'])
def test_present_owners_skip_observation_proof_scan(catching_service,presence):
    source,service,rebuilt = catching_service
    service.poll(20001)
    index = service.index
    if presence == 'loaded':
        for sid in source.histories:
            index.rebuild((str(source.home),sid),20001)
    else:
        with sqlite3.connect(source.home/'logs_2.sqlite') as db:
            for number,sid in enumerate(source.histories,1):
                db.execute("INSERT INTO logs VALUES(?,?,0,'feedback_tags',?,'p1',?)",(number,19999,sid,
                    'request{transport="responses_websocket" api.path="/responses"}: auth_header_attached=true'))
        index.monitor.read_logs(source.home,20001)
        assert not index.loaded
    assert set(index.monitor.sessions)=={(str(source.home),sid) for sid in source.histories}
    while index.model_evidence.catching_up:
        index.model_evidence.poll()
    queries = []
    index.model_evidence.db.set_trace_callback(queries.append)
    try:
        index.replay.begin(20001)
    finally:
        index.model_evidence.db.set_trace_callback(None)
    assert not any('model_observations' in query for query in queries)
    assert not index.replay.pending and index.replay.restore_job is None
    if presence == 'log_only':
        assert not index.loaded
    complete = _collect_complete(service,20002)
    assert {row['id']:row['history'] for row in complete['sessions']} == source.histories
    assert set(rebuilt) == set(source.histories)


def test_present_owners_close_active_proof_job(catching_service,monkeypatch):
    source,service,rebuilt = catching_service
    store = EvidenceStore(source.evidence)
    try:
        for number in range(128):
            store.write(source.home,'extra-'+str(number),10003+number,'WebSocket',
                        'outside-'+str(number),'test','test','completed')
    finally:
        store.close()
    monkeypatch.setattr(EvidenceReader,'MAX_POLL_ROWS',5000)
    clock = [0.]
    original = CollectionReplay._restore
    jobs = []
    def yielding(*args):
        inner = original(*args)
        jobs.append(inner)
        try:
            for value in inner:
                clock[0] += .2
                yield value
        finally:
            inner.close()
    monkeypatch.setattr(CollectionReplay,'_restore',yielding)
    monkeypatch.setattr(index_module.time,'monotonic',lambda:clock[0])
    service.poll(20001)
    replay = service.index.replay
    active = replay.restore_job
    assert active is not None and jobs[0].gi_frame is not None
    for sid in source.histories:
        service.index.rebuild((str(source.home),sid),20001)
    service.index.model_evidence.poll()
    queries = []
    replay.db.set_trace_callback(queries.append)
    try:
        replay.begin(20001)
    finally:
        replay.db.set_trace_callback(None)
    assert not any('FROM responses WHERE key' in query for query in queries)
    assert replay.restore_job is None and active.gi_frame is None and jobs[0].gi_frame is None
    assert not replay.pending
    assert {row['id']:row['history'] for row in _collect_complete(service,20002)['sessions']} == source.histories


def test_mixed_present_and_absent_owners_still_verify_absent_proof(catching_service):
    source,service,rebuilt = catching_service
    service.poll(20001)
    index = service.index
    with sqlite3.connect(source.home/'logs_2.sqlite') as db:
        db.execute("INSERT INTO logs VALUES(1,19999,0,'feedback_tags',?,'p1',?)",(TID,
            'request{transport="responses_websocket" api.path="/responses"}: auth_header_attached=true'))
    index.monitor.read_logs(source.home,20001)
    assert (str(source.home),TID) in index.monitor.sessions
    assert (str(source.home),source.other) not in index.monitor.sessions
    while index.model_evidence.catching_up:
        index.model_evidence.poll()
    queries = []
    index.model_evidence.db.set_trace_callback(queries.append)
    try:
        index.replay.begin(20001)
    finally:
        index.model_evidence.db.set_trace_callback(None)
    assert any('GROUP BY home,response_id,attempt' in query for query in queries)
    assert (str(source.home),TID) not in index.loaded
    assert (str(source.home),source.other) in index.loaded
    assert hasattr(index.monitor.sessions[(str(source.home),source.other)],'_replay_body')
    complete = _collect_complete(service,20002)
    assert {row['id']:row['history'] for row in complete['sessions']} == source.histories
    assert rebuilt == [TID]
