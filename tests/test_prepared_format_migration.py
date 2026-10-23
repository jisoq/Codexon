"""Recognized record-page caches regenerate only retained prepared facts."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from cachemonitor import prepared_ingest as ingest
from cachemonitor import prepared_store as storage
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.analysis_records import public_record
from test_prepared_mode_dependencies import _catalog


@pytest.mark.parametrize('version', [5, 6])
def test_old_record_cache_regenerates_shared_facts_and_reuses_ready_restart(tmp_path, monkeypatch, version):
    fixtures = Path(__file__).with_name('fixtures')
    source_bytes = (fixtures/'prepared-format6-source.json').read_bytes()
    source = json.loads(source_bytes)
    owner = source['home'], source['id']
    path = tmp_path/'prepared.sqlite'
    shutil.copyfile(fixtures/'prepared-format6.sqlite', path)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        assert db.execute('SELECT format FROM meta').fetchone()[0] == 6
        previous_instance = db.execute('SELECT instance FROM meta').fetchone()[0]
        assert {'display_ordinal', 'effective_at', 'request_present'} <= {
            row[1] for row in db.execute('PRAGMA table_info(headers)')}
        assert sorted(row[0] > len(storage.METRICS) for row in db.execute('SELECT length(types) FROM calls')) == [False, True]
        old_checkpoint = storage.unpack(db.execute('SELECT checkpoint FROM owner_versions').fetchone()[0])
        assert old_checkpoint['format'] == ('prepared-owner-append', 3)
        assert old_checkpoint['header_count'] == 2 and old_checkpoint['last_header_order']
        if version == 5:
            db.execute('DROP TABLE catalog_orders')
            db.execute('ALTER TABLE meta DROP COLUMN order_batch')
            db.execute('UPDATE meta SET format=5 WHERE singleton=1')

    catalog = _catalog({source['id']: source})
    def read_source(client, selected, selected_catalog, *, limits, cancel=None):
        assert selected == owner
        value = deepcopy(source)
        return value, ingest._weight(value, limits.owner_source_bytes)
    monkeypatch.setattr(ingest, 'read_source', read_source)
    reference = AnalysisEngine()
    reference.ingest([deepcopy(source)])
    expected = [public_record(row) for row in reference.sessions[owner]['prepared']['history']]
    with storage.PreparedStore(path) as store:
        assert store.token.instance != previous_instance and store.token.generation == 0
        with storage.PreparedReader(path) as reader, pytest.raises(storage.NotReady):
            reader.pin()
        assert list(ingest.PreparedCohort(store, None, None).convert(catalog))[-1]['ready']
        token = store.token
        assert store.db.execute('SELECT format FROM meta').fetchone()[0] == 7
        columns = {row[1] for row in store.db.execute('PRAGMA table_info(headers)')}
        assert not {'display_ordinal', 'effective_at', 'request_present'} & columns
        indexes = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert not {'headers_display', 'headers_effective'} & indexes
        assert {row[0] for row in store.db.execute('SELECT length(types) FROM calls')} == {len(storage.METRICS)}
        with storage.PreparedReader(path, replace(storage.Limits(), page_rows=1)) as reader, reader.pin(token) as view:
            assert list(view.iter_calls(owner)) == expected
            assert view.record(*owner, 'legacy-0') == expected[0]
            headers = list(ingest.pages(view.request_headers, owner, with_first=True))
            assert len(headers) == 3 and len({row['key'] for row in headers}) == 3
            request = next(row for row in headers if row['key'] == 'request')
            assert request['call_count'] == 2 and request['first_call'] == expected[0]
            state_only = next(row for row in headers if row['key'] == 'state-only')
            assert state_only['call_count'] == 0 and state_only['first_call'] is None
            assert state_only['turn_state'] == source['turn_states']['state-only']
            checkpoint = view.derived(*owner, 'checkpoint')
            assert checkpoint['format'] == ('prepared-owner-append', 4)
            assert not {'header_count', 'last_header_order'} & checkpoint.keys()
            assert view.owner(*owner)['dependencies']['preparation'] == ('canonical-owner', 9)
    def unexpected_read(*args, **kwargs):
        raise AssertionError('unchanged ready restart reread source calls')
    monkeypatch.setattr(ingest, 'read_source', unexpected_read)
    with storage.PreparedStore(path) as store:
        assert store.token == token
        assert list(ingest.PreparedCohort(store, None, None).convert(catalog)) == []
        with store.read_current() as view:
            assert list(view.iter_calls(owner)) == expected
    assert (fixtures/'prepared-format6-source.json').read_bytes() == source_bytes
