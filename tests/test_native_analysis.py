"""Ingest bookkeeping must retain original ownership and object identity."""
from copy import deepcopy

import pytest

from cachemonitor.analysis_engine import (
    AnalysisEngine, ROW_SIGNATURE_FIELDS, _owned_histories_python,
    owned_histories, row_signature, row_signatures,
)
from cachemonitor.core import Session


@pytest.fixture(params=('python', 'rust'))
def backend(request, monkeypatch):
    if request.param == 'rust':
        pytest.importorskip('cachemonitor._cpu_native')
    monkeypatch.setenv('CODEXON_CPU_BACKEND', request.param)
    return request.param


def test_batched_signatures_preserve_values_references_and_mapping_fallback(backend):
    class Row(dict):
        def get(self, key, default=None):
            return 'custom' if key == 'model' else super().get(key, default)

    token = object()
    for row in (dict(key='r', total=2 ** 200, purpose=token, input=False), Row(key='r')):
        signature, = row_signatures([row])
        assert signature == row_signature(row)
        if 'purpose' in row:
            assert signature[ROW_SIGNATURE_FIELDS.index('purpose')] is token
        assert signature[ROW_SIGNATURE_FIELDS.index('cached')] is None


def test_call_ownership_keeps_home_scope_local_ids_and_original_row_objects(backend):
    keys = ['response', 'record:offset', 'legacy:offset', None]
    sessions = [dict(home=home, id=sid, history=[dict(key=key) for key in keys])
                for home, sid in [('home', 'z'), ('home', 'a'), ('other', 'b')]]
    before = deepcopy(sessions)
    result = owned_histories(sessions)
    assert result == _owned_histories_python(sessions)
    assert [[row['key'] for row in rows] for rows in result] == [keys[1:3], keys, keys]
    assert result[0][0] is sessions[0]['history'][1]
    assert result[1][0] is sessions[1]['history'][0]
    assert sessions == before
    # Non-string keys retain Python's str/hash semantics through the reference path.
    sessions[0]['history'][0]['key'] = 7
    assert owned_histories(sessions) == _owned_histories_python(sessions)


def test_ingest_revision_fast_path_and_corrected_historical_usage(backend):
    session = Session('session', 'home', title='Observed')
    session.add_usage(100, 'response', dict(input_tokens=100, cached_input_tokens=80,
                                          cache_write_input_tokens=0, output_tokens=20),
                      'gpt-6-astra', 'turn', 'high', service_tier='Standard')
    source = session.view(110)
    source.update(usage_revision=1, collection_complete=True)
    engine = AnalysisEngine()
    assert engine.ingest([source])
    first = engine.record('home', 'session', 'response')
    assert first['input'] == 100 and first['cached'] == 80
    metrics = dict(engine.metrics)
    assert not engine.ingest([dict(source)])
    assert engine.metrics == metrics
    corrected = deepcopy(source)
    corrected['usage_revision'] = 2
    corrected['history'][0].update(input=120, uncached=40, ordinary_input=40, total=140)
    assert engine.ingest([corrected])
    record = engine.record('home', 'session', 'response')
    assert record['input'] == 120 and record['cached'] == 80 and record['total'] == 140
    assert record['call_id'] == 'response' and record['session_ordinal'] == 1
    pending=dict(source,id='a',collection_complete=False,_collection_pending=True)
    engine.ingest([pending,corrected])
    assert engine.record('home','session','response')['input']==120
    assert engine.record('home','a','response') is None
    # Once confirmed, the normal deterministic owner rule applies again.
    engine.ingest([dict(pending,_collection_pending=False),corrected])
    assert engine.record('home','a','response')['input']==100
    assert engine.record('home','session','response') is None
