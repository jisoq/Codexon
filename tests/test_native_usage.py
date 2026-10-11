"""Usage and attribution contracts shared by the Python and Rust backends."""
from collections import deque
from types import SimpleNamespace

import pytest

from cachemonitor.core import (
    METRICS, Session, Transport, _group_usage_python, _summarize_python,
    group_usage, summarize,
)


@pytest.fixture(params=('python', 'rust'))
def backend(request, monkeypatch):
    if request.param == 'rust':
        pytest.importorskip('cachemonitor._cpu_native')
    monkeypatch.setenv('CODEXON_CPU_BACKEND', request.param)
    return request.param


def test_partial_usage_keeps_missing_counts_and_weighted_cache(backend):
    class IntSubclass(int):
        pass

    rows = [dict(input=100, cached=50, total=100),
            dict(input=900, cached=900, total=900),
            dict(input=10, cached=20, total=10),
            dict(input=True, cached=0, total=0),
            dict(input=-1, cached=IntSubclass(1), output=2.5)]
    result = summarize(iter(rows))
    assert result == _summarize_python(rows)
    assert result['count'] == 5
    assert result['input'] == 1010 and result['cached'] == 970
    assert result['missing']['input'] == 2 and result['missing']['cached'] == 1
    assert result['rate'] == 95 and result['output'] is None
    empty = summarize([])
    assert all(empty[key] == 0 and empty['missing'][key] == 0 for key in METRICS)
    assert empty['rate'] is None


def test_group_order_preserves_equal_totals_and_unknown_labels(backend):
    rows = [dict(model='second', effort='high', total=2),
            dict(model='first', effort='high', total=2),
            dict(total=None), dict(model='', effort=None, total=0)]
    result = group_usage(iter(rows))
    assert result == _group_usage_python(rows)
    assert [row['model'] for row in result] == ['second', 'first', '미확인']
    assert result[-1]['count'] == 2


def test_large_integer_and_custom_mapping_accounting_remains_exact(backend):
    class Row(dict):
        pass

    huge = 2 ** 200
    for rows in ([dict(input=huge, cached=huge - 1, total=huge)],
                 [dict(input=2 ** 100, cached=2 ** 99 + 1, total=2 ** 100)],
                 [Row(input=30, cached=7, total=30, model=3, effort='high')]):
        assert summarize(rows) == _summarize_python(rows)
        assert group_usage(rows) == _group_usage_python(rows)


def test_transport_batch_preserves_turn_scope_time_order_and_ties(backend):
    session = Session('session', 'home')
    session.transports = deque([
        Transport(10, 'HTTP/SSE', evidence='first', turn='b'),
        Transport(5, 'WebSocket', evidence='wildcard'),
        Transport(10, 'WebSocket', evidence='a', turn='a'),
        Transport(10, 'WS 연결 시도', evidence='unknown'),
        Transport(12, 'HTTP/SSE', evidence='later', turn='a'),
    ])
    requests = [SimpleNamespace(ts=ts, turn=turn) for ts, turn in
                [(13, 'a'), (10, 'a'), (10, ''), (4, 'a'), (10, 'b'), (9, 'c'), (10, 'c')]]
    result = session.transport_details_many(iter(requests))
    assert result == [session.transport_details(request) for request in requests]
    assert [row['transport_evidence'] for row in result] == [
        'later', 'a', 'first', '대응하는 연결 기록 없음', 'first', 'wildcard', 'unknown']
    assert result[-1]['transport'] == '미확인' and result[-1]['transport_ts'] == 10


def test_transport_batch_preserves_exact_large_integer_timestamps(backend):
    session = Session('session', 'home')
    session.transports = deque([Transport(2 ** 60 + 1, 'WebSocket'),
                                Transport(2 ** 60, 'HTTP/SSE')])
    requests = [SimpleNamespace(ts=2 ** 60, turn='')]
    assert session.transport_details_many(requests) == [session.transport_details(requests[0])]
    assert session.transport_details_many(requests)[0]['transport'] == 'HTTP/SSE'
