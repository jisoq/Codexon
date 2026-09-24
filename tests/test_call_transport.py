

from cachemonitor.core import Session, Transport
from cachemonitor.index import UsageIndex
from cachemonitor.analysis_engine import AnalysisEngine
from test_core import fixture_home
from test_index import finish
from test_performance import query


def test_call_attribution_is_not_session_latest_or_connection_attempt():
    s = Session('s', 'h')
    for ts, turn in ((10, 'a'), (20, 'a'), (30, 'a'), (40, 'b'), (50, 'b')):
        s.add_usage(ts, str(ts), {'input_tokens': 100, 'output_tokens': 10},
                    'gpt-6-astra', turn, service_tier='Standard')
    s.transports.extend([
        Transport(15, 'WebSocket', '/responses', '요청 경로 기록', turn='a'),
        Transport(25, 'HTTP/SSE', '/responses', 'HTTP 전환 기록', turn='a'),
        Transport(45, 'WS 연결 시도', '/responses', '성공 여부 미확인', turn='b'),
        Transport(60, 'WebSocket', '/responses', '요청 경로 기록', turn='b'),
    ])
    view = s.view(61)
    assert [r['transport'] for r in view['history']] == [
        '미확인', 'WebSocket', 'HTTP/SSE', '미확인', '미확인']
    assert view['history'][2]['transport_ts'] == 25
    assert view['history'][2]['transport_evidence'] == 'HTTP 전환 기록'
    assert view['history'][4]['transport_evidence'] == '성공 여부 미확인'
    assert [t['ts'] for t in view['unmatched_transports']] == [45, 60]


def test_transport_evidence_only_update_invalidates_index_and_analysis(tmp_path):
    home, _ = fixture_home(tmp_path)
    index = UsageIndex([home], tmp_path / 'index.sqlite')
    try:
        finish(index)
        s = next(iter(index.monitor.sessions.values()))
        ts = s.requests[0].ts
        s.transports.append(Transport(ts - .8, 'WebSocket', '/responses', '요청 경로 기록'))
        before = finish(index)
        engine = AnalysisEngine()
        engine.ingest(before['sessions'])
        old = engine.query(query(2, end=10020))
        priced = engine.metrics['priced_calls']
        s.transports.append(Transport(ts - .2, 'WebSocket', '/responses', '새 연결 근거'))
        after = finish(index)
        assert after['sessions'][0]['usage_revision'] > before['sessions'][0]['usage_revision']
        assert engine.ingest(after['sessions'])
        fresh = engine.query(query(2, end=10020))
        assert fresh is not old
        assert fresh['analysis']['responses'][0]['transport_evidence'] == '새 연결 근거'
        assert fresh['analysis']['totals'] == old['analysis']['totals']
        assert engine.metrics['priced_calls'] == priced
    finally:
        index.close()
