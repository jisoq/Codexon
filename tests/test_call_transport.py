import os
import time
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.core import Session, Transport
from cachemonitor.dashboard import Dashboard, STYLE
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


def test_call_column_filter_selection_refresh_and_diagnostics(tmp_path):
    app = QApplication.instance() or QApplication([])
    from cachemonitor.fonts import load_bundled_fonts
    load_bundled_fonts()
    app.setStyleSheet(STYLE)
    w = Dashboard([], start_worker=False,
                  settings=QSettings(str(tmp_path / 'ui.ini'), QSettings.IniFormat),live_limits=False)
    now = time.time()
    s = Session('s', 'h', title='연결 방식 통합 검증')
    for offset in (30, 20, 10):
        s.add_usage(now-offset, str(offset), {'input_tokens': 100, 'cached_input_tokens': 50,
                    'output_tokens': 10}, 'gpt-6-astra', 'turn', 'high', service_tier='Standard')
    s.transports.extend([
        Transport(now-25, 'WebSocket', '/responses', '요청 경로 기록', turn='turn'),
        Transport(now-15, 'HTTP/SSE', '/responses', 'HTTP 전환 기록', turn='turn'),
        Transport(now-5, 'WebSocket', '/unmatched', '요청 경로 기록', turn='other'),
    ])
    def snapshot():
        view = s.view(now)
        view['turn_states'] = {'turn': '완료'}
        return {'ts': now, 'sessions': [view], 'homes': [], 'errors': [], 'unassigned': []}
    try:
        w.receive(snapshot())
        w.nav.setCurrentRow(2)
        w.resize(1800, 1000)
        w.show()
        app.processEvents()
        from cachemonitor.quick_qa import click_row
        click_row(w,w.table,0)
        assert w.record_view=='calls' and w.table.rowCount()==3
        w.extra_column_controls['transport'].setChecked(True)
        assert w.table.model().headers[-1]=='통신 방식'
        transport_column=w.table.model().headers.index('통신 방식')
        transport_text=[w.table.item(i,transport_column).text() for i in range(3)]
        assert all(text.startswith('동시간대 통신 로그') for text in transport_text[:2])
        assert 'HTTP/SSE' in transport_text[0] and 'WebSocket' in transport_text[1] and transport_text[2]=='—'
        assert '관측 상태' not in w.table.model().headers
        click_row(w,w.table,0)
        evidence=lambda:w.detail_sections['evidence'][1].text()
        assert '통신 방식' not in w.detail_sections['conditions'][1].text()
        assert not w.detail_sections['evidence'][0].isVisible()
        assert w.exact_record['transport_evidence']=='HTTP 전환 기록'
        assert w.exact_record['transport_endpoint']=='/responses'
        click_row(w,w.table,1)
        s.transports.append(Transport(now-22, 'WebSocket', '/new-path', '갱신 근거', turn='turn'))
        w.receive(snapshot())
        assert w.selected_call=='20' and w.exact_record['key']=='20'
        assert w.exact_record['transport_endpoint']=='/new-path' and w.exact_record['transport_evidence']=='갱신 근거'
        assert '통신 방식' not in w.detail_sections['conditions'][1].text()
        assert not w.detail_sections['evidence'][0].isVisible()
        # Keep an explicit no-sample mode in the filter rather than silently clearing it.
        w.mode.addItem('Fast','Fast');w.mode.setCurrentIndex(w.mode.findData('Fast'))
        assert w.table.rowCount() == 0
        assert w.selected_call=='20'
        w.mode.setCurrentIndex(0)
        assert w.table.rowCount() == 3
        w.open_settings()
        assert w.current_page==4 and w.nav.currentRow()==-1
        assert not hasattr(w, 'connection_table')
        assert all(r.get('transport_endpoint')!='/unmatched' for r in w.record_rows)
        w.nav.setCurrentRow(2)
        from cachemonitor.model_evidence import EvidenceStore, EvidenceReader
        store=EvidenceStore(tmp_path/'wire.sqlite')
        reader=EvidenceReader(tmp_path/'wire.sqlite')
        try:
            store.write('h','a',now-15,'HTTP/SSE','10',status='completed')
            reader.poll()
            direct=snapshot()
            direct['sessions'][0]['history']=reader.enrich('h',direct['sessions'][0]['history'])
            w.receive(direct)
            assert w.table.item(0,transport_column).text()=='HTTP/SSE'
            click_row(w,w.table,0)
            assert '호출 ID  10' in w.detail_sections['identity'][1].text()
            assert '통신 방식  HTTP/SSE' in w.detail_sections['conditions'][1].text()
            assert '/responses' not in evidence()
            assert w.exact_record['transport_source']=='response_id'
            assert w.table.item(1,transport_column).text().startswith('동시간대 통신 로그')
            assert 'WebSocket' in w.table.item(1,transport_column).text()
            store.write('h','b',now-14,'WebSocket','10',status='completed')
            reader.poll()
            conflicting=snapshot()
            conflicting['sessions'][0]['history']=reader.enrich('h',conflicting['sessions'][0]['history'])
            w.receive(conflicting)
            assert w.table.item(0,transport_column).text()=='—'
            assert w.selected_call=='10' and w.exact_record['transport_source']=='conflict'
            assert evidence()=='통신 방식 기록이 서로 다릅니다.'
            assert w.detail_sections['evidence'][0].isVisible()
            assert '통신 방식' not in w.detail_sections['conditions'][1].text()
            w.receive(direct)
        finally:reader.close();store.close()
        assert not w.detail_sections['evidence'][0].isVisible()
        visible='\n'.join(text.text() for body,text in w.detail_sections.values() if body.isVisible())
        assert not any(word in visible for word in ('미확인','확인 불가','관측 없음'))
        w.table.scrollTo(w.table.model().index(0, transport_column))
        app.processEvents()
        capture = os.environ.get('CACHEMONITOR_TRANSPORT_CAPTURE')
        if capture:
            Path(capture).parent.mkdir(parents=True, exist_ok=True)
            assert w.grab().save(capture)
    finally:
        w.quitting = True
        w.tick.stop()
        w.tray.hide()
        w.close()
