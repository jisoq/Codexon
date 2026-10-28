import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

import pytest

from cachemonitor import usage_collection as collection
from cachemonitor.event_store import pack_event
from cachemonitor.index import UsageIndex
from test_core import TID, event, fixture_home, usage
from test_index import finish


CHILD = '12345678-abcd-abcd-abcd-222222222222'
OTHER = '12345678-abcd-abcd-abcd-333333333333'


class NoPopulation(list):
    def __iter__(self):
        pytest.fail('selected handler enumerated the complete manifest')

    def __getitem__(self, key):
        pytest.fail('selected handler scanned the complete manifest')


class PointOnly(dict):
    def __iter__(self):
        pytest.fail('selected lookup enumerated unrelated owners')

    keys = items = values = __iter__


def thread(home, sid, parent=None):
    path = home / 'sessions' / (sid + '.jsonl')
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(event('session_meta', id=sid, parent_thread_id=parent)) + '\n'
                    + json.dumps(usage('response-' + sid, tid=sid)) + '\n', encoding='utf8')
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        if 'parent_thread_id' not in {row[1] for row in db.execute('PRAGMA table_info(threads)')}:
            db.execute('ALTER TABLE threads ADD COLUMN parent_thread_id TEXT')
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)',
                   (sid, str(path), 10000, None, sid, 'test', 'openai', 'project', parent))
    return path


def archive(index, home, sid, purpose='imported'):
    index.db.execute('INSERT INTO usage_archive VALUES(?,?,?)',
                     (str(home), purpose + '-' + sid,
                      json.dumps(dict(sid=sid, purpose=purpose, ts=10000,
                                      input=7, output=3, state='completed'))))
    index.db.commit()


@pytest.fixture
def indexed_sources(tmp_path):
    home, _ = fixture_home(tmp_path)
    thread(home, CHILD, TID)
    thread(home, OTHER)
    path = tmp_path / 'index.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events(path TEXT,pos INTEGER,home TEXT,tid TEXT,ts REAL,data TEXT,PRIMARY KEY(path,pos))')
        db.execute('CREATE INDEX events_session ON events(home,tid,ts,path,pos)')
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',
                   ('legacy', 0, str(home), 'legacy-only', 10000, json.dumps(usage())))
    index = UsageIndex([home], path)
    index.discover(10010)
    index.db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',
                     ('retained', 0, str(home), 'event-only', 10000, pack_event(json.dumps(usage()))))
    index.db.execute('INSERT INTO event_sessions(home,tid) VALUES(?,?)', (str(home), 'empty-event-owner'))
    archive(index, home, TID)
    archive(index, home, 'archive-only', 'maintenance')
    key = (str(home), OTHER)
    index.db.execute('DELETE FROM metadata WHERE home=? AND tid=?', key)
    index.metadata.pop(key)
    index.lineage.pop(key, None)
    index.db.commit()
    try:
        yield index, home
    finally:
        index.close()


def test_selected_lookup_unions_source_owners_without_population_enumeration(indexed_sources, monkeypatch):
    index, home = indexed_sources
    monkeypatch.setattr(index, 'metadata', PointOnly(index.metadata))
    monkeypatch.setattr(index.event_store, 'owners', lambda *a: pytest.fail('enumerated retained event owners'))
    selected = [(None, sid) for sid in (TID, OTHER, 'event-only', 'legacy-only', 'maintenance:archive-only')]
    result = index.resolve_selection(selected)
    expected = {TID, CHILD, 'imported:' + TID, OTHER, 'event-only', 'legacy-only', 'maintenance:archive-only'}
    assert result['membership_complete']
    assert {tuple(row) for row in result['demand_members']} == {(str(home), sid) for sid in expected}
    assert result['matches'] == [[None, sid, [str(home)]] for _, sid in selected]
    assert [str(home), 'imported:' + TID, TID] in result['session_catalog']
    assert index.resolve_selection([(None, 'empty-event-owner')])['matches'] == [[None, 'empty-event-owner', []]]
    assert index.resolve_selection([(None, 'archive-only')])['matches'] == [[None, 'archive-only', []]]
    index.demand = dict(selected=selected, aggregate=False, evidence=[])
    index.admit_demand()
    assert index.active_members == {(str(home), sid) for sid in expected}


def test_selected_lookup_distinguishes_absence_ambiguity_and_pending_discovery(tmp_path):
    first, _ = fixture_home(tmp_path)
    second_root = tmp_path / 'second'
    second_root.mkdir()
    second, _ = fixture_home(second_root)
    index = UsageIndex([first, second], tmp_path / 'index.sqlite')
    try:
        index.discover(10010)
        result = index.resolve_selection([(None, TID), (str(first), TID), (None, 'missing')])
        assert result['membership_complete']
        assert result['matches'] == [
            [None, TID, [str(first), str(second)]],
            [str(first), TID, [str(first)]],
            [None, 'missing', []],
        ]
        assert result['demand_members'] == [[str(first), TID]]
        index.header_pending.add('unidentified-source')
        pending = index.resolve_selection([(None, 'missing'), (None, TID)])
        assert not pending['membership_complete']
        assert pending['matches'] == [[None, 'missing', None], [None, TID, None]]
    finally:
        index.close()


@pytest.mark.parametrize('writer', ['discover_header', 'discover', 'scan_file', 'refresh_metadata'])
def test_selected_reverse_lineage_tracks_every_writer(tmp_path, writer):
    home, _ = fixture_home(tmp_path)
    path = thread(home, CHILD, TID)
    thread(home, OTHER)
    index = UsageIndex([home], tmp_path / 'index.sqlite')
    try:
        index.discover(10010)
        assert [str(home), CHILD] in index.resolve_selection([(None, TID)])['demand_members']
        if writer in ('discover_header', 'scan_file'):
            path.write_text(json.dumps(event('session_meta', id=CHILD, parent_thread_id=OTHER)) + '\n',
                            encoding='utf8')
            getattr(index, writer)(str(path), home, CHILD)
        else:
            index.discover_header(str(path), home, CHILD)
            path.write_text(json.dumps(event('session_meta', id=CHILD)) + '\n', encoding='utf8')
            index.discover_header(str(path), home, CHILD)
            with sqlite3.connect(home / 'state_5.sqlite') as db:
                db.execute('UPDATE threads SET parent_thread_id=? WHERE id=?', (OTHER, CHILD))
            if writer == 'discover':
                index.discover(10011)
            else:
                index.refresh_metadata({home.resolve()})
        assert [str(home), CHILD] not in index.resolve_selection([(None, TID)])['demand_members']
        assert [str(home), CHILD] in index.resolve_selection([(None, OTHER)])['demand_members']
        if writer == 'scan_file':
            assert index.metadata[(str(home), CHILD)]['parent_thread_id'] == TID
            assert index.lineage[(str(home), CHILD)]['parent_thread_id'] == OTHER
        path.write_text(json.dumps(event('session_meta', id=CHILD)) + '\n', encoding='utf8')
        index.discover_header(str(path), home, CHILD)
        assert [str(home), CHILD] not in index.resolve_selection([(None, OTHER)])['demand_members']
    finally:
        index.close()


def test_selected_discovery_finishes_late_unknown_file_identity(tmp_path, monkeypatch):
    import cachemonitor.index as implementation

    home, _ = fixture_home(tmp_path)
    path = home / 'sessions' / 'unknown.jsonl'
    path.parent.mkdir()
    path.write_text(json.dumps(event('session_meta', id=CHILD, parent_thread_id=TID, ignored='x' * 4096))
                    + '\n' + json.dumps(usage('late', tid=CHILD)) + '\n', encoding='utf8')
    monkeypatch.setattr(implementation, 'FILE_READ_BUDGET', 256)
    index = UsageIndex([home], tmp_path / 'index.sqlite')
    index.demand = dict(selected=[(None, TID)], aggregate=False, evidence=[])
    try:
        index.discover(10010)
        assert not index.resolve_selection([(None, TID)])['membership_complete']
        complete = finish(index)
        selected = index.resolve_selection([(None, TID)])
        assert selected['membership_complete']
        assert [str(home), CHILD] in selected['demand_members']
        assert {session['id'] for session in complete['sessions']} == {TID, CHILD}
        assert all(session['collection_complete'] for session in complete['sessions'])
        assert complete['index']['files'] == 2
    finally:
        index.close()


def test_selected_source_queries_use_indexed_owner_and_parent_access(indexed_sources):
    index, home = indexed_sources
    statements = []
    index.db.set_trace_callback(statements.append)
    try:
        result = index.resolve_selection([(None, TID), (None, 'event-only'), (None, 'legacy-only'),
                                          (None, 'maintenance:archive-only')])
        index.archive.views(10010, {str(home)}, owners={tuple(row) for row in result['demand_members']})
    finally:
        index.db.set_trace_callback(None)
    queries = [sql for sql in statements if sql.lstrip().upper().startswith('SELECT')
               and any(name in sql.lower() for name in ('metadata', 'event_sessions', 'event_records',
                                                        'events_legacy', 'usage_archive'))
               and 'sqlite_master' not in sql.lower()]
    assert any('metadata' in sql.lower() for sql in queries)
    assert any('usage_archive' in sql.lower() for sql in queries)
    assert any('event_sessions' in sql.lower() for sql in queries)
    assert any('events_legacy' in sql.lower() for sql in queries)
    for sql in queries:
        plan = [row[3] for row in index.db.execute('EXPLAIN QUERY PLAN ' + sql)]
        assert any('SEARCH ' in step for step in plan), (sql, plan)
        assert not any(step.startswith('SCAN ') and step != 'SCAN CONSTANT ROW' for step in plan), (sql, plan)


@pytest.fixture
def collector(tmp_path, monkeypatch):
    from cachemonitor import collection_transport

    home, _ = fixture_home(tmp_path)
    thread(home, CHILD, TID)
    thread(home, OTHER)
    monkeypatch.setattr(collection.CollectionChannel, 'start_live',
                        lambda channel: setattr(channel, 'live', collection.LiveCollection(channel.path, channel.scope)))
    service = collection.CollectorService([home], tmp_path / 'index.sqlite', tmp_path / 'evidence.sqlite')
    monkeypatch.setattr(collection_transport, 'request', lambda endpoint, value, **kw: service.channel.live.request(value))
    reader = collection.CollectionClient([home], service.channel.path, service.channel.evidence)
    try:
        yield service, reader, home
    finally:
        reader.close()
        service.channel.live.close()
        service.close()


def collect(service):
    deadline = monotonic() + 10
    while True:
        snapshot = service.poll(10010, detailed=False)
        if not snapshot['index']['loading']:
            return snapshot
        assert monotonic() < deadline, 'selected collection did not finish'


def selected_pages(reader):
    pages = []
    cursor = None
    while True:
        page = reader.poll_selection(10010, cursor=cursor, include_metadata=True, metadata_kind='lineage')
        assert page['authority'] == 'selected' and page['membership_format'] == 'selected-v1'
        assert page['selection_request']
        assert 'session_catalog' not in page['header'] and 'demand_members' not in page['header']
        pages.append(page)
        cursor = page['cursor']
        if cursor is None:
            return pages


def test_selected_client_collects_only_family_after_normal_discovery(collector, monkeypatch):
    service, reader, home = collector
    collect(service)
    reader.set_demand([(None, TID)])
    monkeypatch.setattr(service.index, 'metadata', PointOnly(service.index.metadata))
    monkeypatch.setattr(service.index.event_store, 'owners', lambda *a: pytest.fail('selected poll enumerated owners'))
    monkeypatch.setattr(reader, 'poll_catalog', lambda **kw: pytest.fail('selected client requested full catalog'))
    collect(service)
    pages = selected_pages(reader)
    assert all(page['membership_complete'] for page in pages)
    assert {key for page in pages for key in page['owners']} == {(str(home), TID), (str(home), CHILD)}
    assert [row for page in pages for row in page['membership']['matches']] == [[None, TID, [str(home)]]]
    assert {tuple(row) for page in pages for row in page['membership']['demand_members']} == {
        (str(home), TID), (str(home), CHILD),
    }


def test_selected_pages_keep_frozen_positions_with_an_aggregate_lease(collector, monkeypatch):
    service, reader, home = collector
    aggregate = collection.CollectionClient([home], service.channel.path, service.channel.evidence)
    monkeypatch.setattr(collection, 'CATALOG_PAGE_ROWS', 2)
    try:
        aggregate.set_demand([], aggregate=True)
        reader.set_demand([(None, TID)])
        collect(service)
        manifest = service.channel.live.current['envelope']['sessions']
        expected = {(entry['home'], entry['sid']): position for position, entry in enumerate(manifest)
                    if entry['sid'] in (TID, CHILD)}
        with monkeypatch.context() as patch:
            patch.setattr(service.index, 'resolve_selection', lambda *a, **kw: pytest.fail('handler read writer state'))
            patch.setattr(reader, 'poll_catalog', lambda **kw: pytest.fail('selected client requested full catalog'))
            patch.setitem(service.channel.live.current['header'], 'session_catalog', PointOnly())
            patch.setitem(service.channel.live.current['envelope'], 'sessions', NoPopulation(manifest))
            with ThreadPoolExecutor(max_workers=1) as workers:
                pages = workers.submit(selected_pages, reader).result(timeout=5)
        assert len(pages) > 1
        assert {key: entry['source_ordinal'] for page in pages for key, entry in page['owners'].items()} == expected
        assert all(len(page['owners']) + sum(map(len, page['membership'].values())) <= 2 for page in pages)
        assert all(page['catalog_identity'] == pages[0]['catalog_identity'] for page in pages)
    finally:
        aggregate.close()


def test_selected_publication_changes_for_family_metadata_with_identical_bodies(collector, monkeypatch):
    service, reader, home = collector
    aggregate = collection.CollectionClient([home], service.channel.path, service.channel.evidence)
    try:
        aggregate.set_demand([], aggregate=True)
        reader.set_demand([(None, TID)])
        collect(service)
        before = selected_pages(reader)
        body = service.channel.live.current['entries'][(str(home), CHILD)]['body_digest']
        child_path = home / 'sessions' / (CHILD + '.jsonl')
        child_path.write_text(json.dumps(event('session_meta', id=CHILD, parent_thread_id=OTHER)) + '\n',
                              encoding='utf8')
        service.index.scan_file(str(child_path), home, CHILD)
        service.index.db.commit()
        monkeypatch.setattr(service.index, 'scan_file', lambda *a: None)
        collect(service)
        after = selected_pages(reader)
        assert service.channel.live.current['entries'][(str(home), CHILD)]['body_digest'] == body
        assert after[0]['catalog_identity'] != before[0]['catalog_identity']
        assert [str(home), CHILD] in [row for page in before for row in page['membership']['demand_members']]
        assert [str(home), CHILD] not in [row for page in after for row in page['membership']['demand_members']]
    finally:
        aggregate.close()


def test_selected_cursor_rejects_changed_demand_and_expired_lease(collector, monkeypatch):
    service, reader, home = collector
    monkeypatch.setattr(collection, 'CATALOG_PAGE_ROWS', 1)
    reader.set_demand([(None, TID)])
    collect(service)
    first = reader.poll_selection(10010)
    assert first['cursor'] is not None
    reader.set_demand([(None, OTHER)])
    with pytest.raises(collection.CollectionCatalogUnavailable):
        reader.poll_selection(10010, cursor=first['cursor'])
    collect(service)
    replacement = reader.poll_selection(10010)
    assert replacement['selection_request'] != first['selection_request']
    with service.channel.live.lock:
        homes, _, demand = service.channel.live.leases[reader.channel.client]
        service.channel.live.leases[reader.channel.client] = (homes, 0, demand)
    collect(service)
    with pytest.raises(collection.CollectionCatalogUnavailable):
        reader.poll_selection(10010, cursor=replacement['cursor'])
    reader.set_demand([])
    collect(service)
    cleared = selected_pages(reader)
    assert all(not page['membership']['demand_members'] for page in cleared)


def test_selected_mode_context_preserves_family_and_body_parent_roles(collector):
    service, reader, home = collector
    reader.set_demand([(None, TID)])
    collect(service)
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('UPDATE threads SET parent_thread_id=? WHERE id=?', (OTHER, CHILD))
    service.index.refresh_metadata({home.resolve()})
    collect(service)
    pages = selected_pages(reader)
    membership = [row for page in pages for row in page['membership']['session_catalog']]
    family = [row for page in pages for row in page['membership']['demand_members']]
    refs = {key: value for page in pages for key, value in page['owners'].items()}
    assert [str(home), CHILD, TID] in membership
    assert [str(home), OTHER, None] in membership
    assert [str(home), CHILD] in family and [str(home), OTHER] not in family
    assert refs[(str(home), CHILD)]['owner_lineage']['parent_thread_id'] == OTHER
    reader.request_sources([(str(home), OTHER)])
    collect(service)
    expanded = selected_pages(reader)
    assert (str(home), OTHER) in {key for page in expanded for key in page['owners']}
    assert [str(home), OTHER] not in [row for page in expanded for row in page['membership']['demand_members']]


def test_modern_selected_capability_failure_never_reads_full_catalog(collector, monkeypatch):
    service, reader, home = collector
    reader.set_demand([(None, TID)])
    collect(service)
    def unsupported(current, value):
        if value.get('read') == 'selection':
            return dict(ready=False, protocol=collection.CATALOG_PROTOCOL, error='unsupported'), b''
        pytest.fail('modern selected request fell back to another catalog operation')
    monkeypatch.setattr(service.channel.live, 'catalog_request', unsupported)
    with pytest.raises(collection.CollectionCatalogUnavailable, match='unsupported'):
        reader.poll_selection(10010)

def test_selected_wildcards_use_global_homes_but_bodies_keep_client_scope(collector, tmp_path):
    service, reader, home = collector
    root = tmp_path / 'second'
    root.mkdir()
    other_home, _ = fixture_home(root)
    thread(other_home, 'outside')
    other = collection.CollectionClient([other_home], service.channel.path, service.channel.evidence)
    try:
        other.set_demand([], aggregate=True)
        reader.set_demand([(None, TID)])
        collect(service)
        pages = selected_pages(reader)
        assert [row for page in pages for row in page['membership']['matches']] == [
            [None, TID, [str(home), str(other_home)]],
        ]
        assert all(not page['membership']['demand_members'] for page in pages)
        reader.set_demand([(None, 'outside')])
        collect(service)
        with pytest.raises(collection.CollectionScopeError):
            reader.poll_selection(10010)
        reader.set_demand([(str(home), TID)])
        collect(service)
        own = selected_pages(reader)
        assert [row for page in own for row in page['membership']['matches']] == [
            [str(home), TID, [str(home)]],
        ]
    finally:
        other.close()


def test_selected_known_unavailable_source_never_becomes_absent(collector, monkeypatch):
    import io

    service, reader, home = collector
    path = thread(home, 'unavailable')
    original = io.open
    def unavailable(filename, *args, **kwargs):
        if str(filename) == str(path):
            raise PermissionError('source is unavailable')
        return original(filename, *args, **kwargs)
    monkeypatch.setattr(io, 'open', unavailable)
    reader.set_demand([(None, 'unavailable')])
    collect(service)
    pages = selected_pages(reader)
    assert [row for page in pages for row in page['membership']['matches']] == [
        [None, 'unavailable', [str(home)]],
    ]
    refs = {key: entry for page in pages for key, entry in page['owners'].items()}
    key = (str(home), 'unavailable')
    assert key not in refs or not refs[key]['state']['collection_complete']


def test_selected_context_captures_the_whole_path_entering_a_mode_cycle(collector):
    service, reader, home = collector
    thread(home, 'cycle', OTHER)
    reader.set_demand([(None, TID)])
    collect(service)
    with sqlite3.connect(home / 'state_5.sqlite') as db:
        db.execute('UPDATE threads SET parent_thread_id=? WHERE id=?', (OTHER, CHILD))
        db.execute('UPDATE threads SET parent_thread_id=? WHERE id=?', ('cycle', OTHER))
    service.index.refresh_metadata({home.resolve()})
    collect(service)
    pages = selected_pages(reader)
    context = {tuple(row) for page in pages for row in page['membership']['session_catalog']}
    assert {(str(home), OTHER, 'cycle'), (str(home), 'cycle', OTHER)} <= context
    refs = {key: entry for page in pages for key, entry in page['owners'].items()}
    assert refs[(str(home), CHILD)]['owner_lineage']['parent_thread_id'] == OTHER
    assert {tuple(row) for page in pages for row in page['membership']['demand_members']} == {
        (str(home), TID), (str(home), CHILD),
    }
