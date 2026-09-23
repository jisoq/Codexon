from PySide6.QtTest import QTest
from cachemonitor.quick_qa import mount, dispose, table_view, control
import copy
import time
from datetime import datetime,timedelta

import pytest

from cachemonitor.analytics import analyze,comparison_view,overview_view
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session,Transport,WINDOW
from test_comparison import history


def query(page=1,**kwargs):
    return {'page':page,'start':0,'end':110,'now':110,'period':'all','model':'','source':'','archived':True,
            'unit':'response','method':'mean','metric':'cost','band':None,'granularity':'auto',**kwargs}


def test_worker_payloads_cross_threads_without_recursive_conversion(tmp_path):
    import threading
    from PySide6.QtCore import QObject, Slot, Qt
    from PySide6.QtWidgets import QApplication
    from cachemonitor.analysis_worker import AnalysisBridge
    from cachemonitor.quota_service import QuotaService
    app=QApplication.instance() or QApplication([])
    class Receiver(QObject):
        @Slot(object)
        def receive(self,value):received.append(value)
    received=[];receiver=Receiver();bridge=AnalysisBridge([],static_snapshot={})
    row={'key':('home','session'),'total':2**54+1}
    payload={'rows':[row]*10000,'lookup':{('home','session'):row}}
    bridge.snapshot.connect(receiver.receive,Qt.QueuedConnection)
    bridge.result.connect(receiver.receive,Qt.QueuedConnection)
    quota=QuotaService(tmp_path,live=False)
    quota.updated.connect(receiver.receive,Qt.QueuedConnection)
    sender=threading.Thread(target=lambda:(bridge.snapshot.emit(payload),bridge.result.emit(payload),quota.updated.emit(payload)))
    sender.start();sender.join(timeout=5)
    assert not sender.is_alive()
    deadline=time.monotonic()+5
    while len(received)<3 and time.monotonic()<deadline:
        app.processEvents();QTest.qWait(1)
    assert len(received)==3 and all(value is payload for value in received)
    assert received[0]['lookup'][('home','session')] is received[1]['rows'][0]


def test_engine_matches_reference_populations_filters_and_statistics():
    source=history()
    epoch=datetime(2026,9,14).timestamp()
    for row in source['history']: row['ts']+=epoch
    for request in source['turn_records'].values():
        request['started_at']+=epoch;request['ended_at']+=epoch
    other=copy.deepcopy(source)
    other.update(id='other',home='elsewhere',source='subagent',archived=True)
    engine=AnalysisEngine(); engine.ingest([source,other])
    for page,unit,method,model,source_filter,archived,start in (
        (1,'response','mean','','',True,0),(1,'turn','median','','',True,101),
        (1,'response','mean','gpt-6-astra','',True,0),(1,'turn','mean','','subagent',True,0),
        (0,'response','mean','','',False,0),(2,'turn','median','','',True,0)):
        start=start+epoch if start else 0
        end=epoch+110
        q=query(page,unit=unit,method=method,model=model,source=source_filter,archived=archived,start=start,end=end)
        result=engine.query(q)
        expected=analyze([source,other],start,end,model if page!=1 else '',source_filter,archived,unit,method)
        for key in ('totals','priced_count','unpriced_count','missing_turn_responses','excluded_turns','turn_count','unclassified'):
            assert result['analysis'][key]==expected[key]
        if page==1:
            assert result['comparison']==comparison_view(expected,unit=unit,method=method)
            assert result['analysis']['basis']==expected['basis']
        if page==0:
            assert result['overview']==overview_view(expected,start,end)


def test_unchanged_updates_new_calls_corrections_and_metadata_invalidate_precisely():
    source=history(); engine=AnalysisEngine()
    assert engine.ingest([source])
    first=engine.query(query())
    counts=dict(engine.metrics)
    assert not engine.ingest([copy.deepcopy(source)])
    assert engine.query(query()) is first
    assert engine.metrics['priced_calls']==counts['priced_calls']
    assert engine.metrics['part_builds']==counts['part_builds']
    changed=copy.deepcopy(source)
    new=copy.deepcopy(changed['history'][0]); new.update(key='new',ts=107)
    changed['history'].append(new)
    engine.ingest([changed]); engine.query(query())
    assert engine.metrics['priced_calls']==counts['priced_calls']+1
    changed['history'][0]['output']+=100
    changed['history'][0]['total']+=100
    engine.ingest([changed]); second=engine.query(query())
    assert engine.metrics['priced_calls']==counts['priced_calls']+2
    assert second['analysis']['totals']['cost']==pytest.approx(analyze([changed])['totals']['cost'])
    priced=engine.metrics['priced_calls']
    changed['title']='Renamed'; changed['archived']=True
    changed['turn_states']['open']='완료'
    changed['turn_records']['open']={'started_at':102,'ended_at':102.5,'state':'완료'}
    engine.ingest([changed])
    assert engine.metrics['priced_calls']==priced
    assert engine.query(query(archived=False))['analysis']['responses']==[]
    assert engine.query(query())['comparison']['completed']==4
    # Truncation/replacement cannot leave stale calls in the lookup or totals.
    changed['history']=changed['history'][:1]
    engine.ingest([changed])
    result=engine.query(query())
    assert len(result['analysis']['responses'])==1
    assert result['analysis']['totals']['cost']==pytest.approx(analyze([changed])['totals']['cost'])


def test_moving_time_window_and_calendar_invalidate_without_new_data():
    engine=AnalysisEngine(); engine.ingest([history()])
    first=engine.query(query(start=100))
    counts=dict(engine.metrics)
    same_calls=engine.query(query(start=100,end=111))
    assert same_calls['analysis']['totals']==first['analysis']['totals']
    assert engine.metrics['priced_calls']==counts['priced_calls']
    expired=engine.query(query(start=101,end=111))
    assert len(expired['analysis']['responses'])==len(first['analysis']['responses'])-1
    assert engine.metrics['priced_calls']==counts['priced_calls']
    now=datetime(2026,9,14,12).timestamp()
    source=history()
    for row in source['history']: row['ts']=now
    engine.ingest([source])
    today=engine.query(query(0,end=now+1))
    tomorrow=engine.query(query(0,end=now+86400))
    assert tomorrow['overview']['days']==today['overview']['days']+1


def test_collector_view_cache_keeps_history_and_refreshes_expiry_and_transport():
    now=datetime(2026,9,14,12).timestamp()
    s=Session('s','h')
    s.add_usage(now,'r',{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10},'m','t','high', service_tier='Standard')
    first=s.view(now+1,use_cache=True)
    later=s.view(now+2,use_cache=True)
    assert later['history'] is first['history']
    assert later['remaining']==first['remaining']-1
    expired=s.view(now+WINDOW+1,use_cache=True)
    assert expired['history'] is first['history'] and expired['requests']==[]
    s.transports.append(Transport(now,'WebSocket','/responses'))
    transport=s.view(now+WINDOW+2,use_cache=True)
    assert transport['transport']=='WebSocket'


def test_large_table_formats_only_visible_cells_and_preserves_scroll():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.lazy_table import LazyTable
    app=QApplication.instance() or QApplication([])
    w=LazyTable(['id','value']); host=mount(w,500,400)
    rows=list(range(50000))
    def cell(row,column,role): return str(row)
    w.set_rows(rows,cell); app.processEvents();QTest.qWait(40)
    assert w.model().formatted<300
    w.verticalScrollBar().setValue((table_view(host,w).property('contentHeight')-400)//2)
    app.processEvents();QTest.qWait(40)
    old=w.verticalScrollBar().value(); count=w.model().formatted
    w.set_rows(rows,cell); app.processEvents();QTest.qWait(40)
    assert w.verticalScrollBar().value()==old and w.model().formatted==count
    assert w.item(49999,0).text()=='49999'
    before_row=w.model().rows[w.first_visible]
    shifted=[-1]+rows
    w.set_rows(shifted,cell); app.processEvents();QTest.qWait(40)
    assert w.model().rows[w.first_visible]==before_row
    dispose(host)


def test_direct_drilldown_reveals_target_while_regular_updates_keep_scroll(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    now=time.time(); s=Session('s','h',title='Long session')
    for i in range(250):
        s.add_usage(now-300+i,str(i),{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10},'m',f't{i}','high', service_tier='Standard')
    source=s.view(now); source['turn_states']={f't{i}':'완료' for i in range(250)}
    snapshot={'ts':now,'sessions':[source],'homes':[],'errors':[],'unassigned':[]}
    w=Dashboard([],start_worker=False,settings=QSettings(str(tmp_path/'focus.ini'),QSettings.IniFormat))
    try:
        w.receive(snapshot); w.show(); app.processEvents();QTest.qWait(40)
        w.open_record({'home':'h','sid':'s','turn':'t0','key':'0'})
        app.processEvents();QTest.qWait(40)
        QTest.qWait(80)
        assert w.selected_turn=='t0' and w.selected_call=='0' and w.exact_record['key']=='0'
        assert w.detail_scroll.isVisible()
        position=w.detail_scroll.verticalPosition.value()
        w.receive(snapshot); app.processEvents();QTest.qWait(40)
        assert w.detail_scroll.verticalPosition.value()==position
    finally:
        w.quitting=True; w.tick.stop(); w.tray.hide(); w.close()


def test_process_queries_latest_selection_cache_and_clean_shutdown(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    source=history()
    epoch=datetime(2026,9,14).timestamp()
    for row in source['history']: row['ts']+=epoch
    snapshot={'ts':epoch+110,'sessions':[source],'homes':[],'errors':[],'unassigned':[],
              'index':{'loading':False,'done':1,'files':1}}
    w=Dashboard([],settings=QSettings(str(tmp_path/'worker.ini'),QSettings.IniFormat),static_snapshot=snapshot)
    def settle():
        limit=time.monotonic()+20
        while time.monotonic()<limit:
            app.processEvents();QTest.qWait(40)
            if w.analysis_errors: raise AssertionError(str(w.analysis_errors))
            if not w.analysis_pending and w.view_result is not None and w.applied_key[1]==w.nav.currentRow(): return
            QTest.qWait(10)
        raise AssertionError(str(w.analysis_errors))
    try:
        settle()
        w.model.setCurrentIndex(0)
        w.nav.setCurrentRow(0); settle()
        assert w.overview['calls']==7
        w.nav.setCurrentRow(1)
        w.source.setCurrentIndex(w.source.findData('subagent'))
        w.source.setCurrentIndex(0)
        w.nav.setCurrentRow(2); settle()
        assert w.parent_kind=='sessions' and w.parent_table.rowCount()==1
        assert w.worker.process.pid is not None
        assert not w.analysis_errors
        count=w.worker_metrics['view_builds']
        w.render(); settle()
        assert w.worker_metrics['view_builds']==count
        old_revision=w.snapshot['data_revision']
        priced=w.worker_metrics['priced_calls']
        updated=copy.deepcopy(snapshot)
        updated['ts']+=10
        row=copy.deepcopy(updated['sessions'][0]['history'][0])
        row.update(key='new-from-worker',ts=updated['ts']-1)
        updated['sessions'][0]['history'].append(row)
        w.worker.replace_snapshot(updated)
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            app.processEvents();QTest.qWait(40)
            if w.snapshot['data_revision']>old_revision and not w.analysis_pending: break
            QTest.qWait(10)
        assert len(w.analysis['responses'])==8
        assert w.worker_metrics['priced_calls']==priced+1
        assert not w.analysis_errors
        # Only the worker created by this test is terminated; it must recover the latest snapshot.
        old_pid=w.worker.process.pid
        w.worker.process.terminate()
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            app.processEvents();QTest.qWait(40)
            if w.worker.process.pid!=old_pid and w.worker.process.is_alive() and not w.analysis_pending and not w.analysis_errors and w.analysis_error_history:
                break
            QTest.qWait(20)
        assert w.worker.process.pid!=old_pid
        assert not w.analysis_errors and len(w.analysis['responses'])==8
    finally:
        w.quitting=True; w.tick.stop()
        w.worker.requestInterruption()
        assert w.worker.wait(6000)
        assert not w.worker.process.is_alive()
        w.tray.hide(); w.close()


def test_part_cache_reuses_time_ticks_but_respects_request_boundaries():
    engine=AnalysisEngine();engine.ingest([history()])
    before=engine.query(query(end=101.25,unit='turn'))
    builds=engine.metrics['part_builds']
    engine.query(query(end=101.4,unit='turn'))
    assert engine.metrics['part_builds']==builds
    after=engine.query(query(end=101.75,unit='turn'))
    assert engine.metrics['part_builds']==builds+1
    assert after['analysis']['excluded_turns']==before['analysis']['excluded_turns']-1


def test_revision_fast_path_and_correction():
    source=history();source['usage_revision']=1
    engine=AnalysisEngine();engine.ingest([source]);prepared=engine.sessions[('h','s')]['prepared']
    assert not engine.ingest([copy.deepcopy(source)])
    assert engine.sessions[('h','s')]['prepared'] is prepared
    source['usage_revision']=2;source['history'][0]['output']+=1
    assert engine.ingest([source])
    assert engine.sessions[('h','s')]['prepared']['history'][0]['output']==source['history'][0]['output']


def test_display_query_omits_unused_full_history():
    engine=AnalysisEngine();engine.ingest([history()])
    result=engine.query(query(0,include_whole_history=False))
    assert result['lookup']['whole_turns']=={}
    assert 'explorer' not in result and 'whole_sessions' not in result['lookup']


def test_hidden_dashboard_defers_all_page_refreshes(tmp_path,monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    w=Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'hidden.ini'),QSettings.IniFormat))
    try:
        w.show();app.processEvents();w.hide();app.processEvents();w.tick.stop()
        calls=[]
        monkeypatch.setattr(w.quota_panel,'refresh_status',lambda:calls.append('quota'))
        monkeypatch.setattr(w,'render_diagnostics',lambda:calls.append('diagnostics'))
        for page in range(5):
            w.current_page=page;w._display_dirty=False
            w.render(automatic=True);w.check_stale()
            assert w._display_dirty and not calls
        w.current_page=3;w.show();app.processEvents()
        assert calls==['quota'] and not w._display_dirty
    finally:
        w.quitting=True;w.tick.stop();w.tray.hide();w.close()
