from PySide6.QtTest import QTest
from cachemonitor.quick_qa import mount, dispose, table_view
import copy
import time
from datetime import datetime

import pytest

from cachemonitor.analytics import analyze
from cachemonitor.analysis_engine import AnalysisEngine
from test_comparison import history


def query(page=1,**kwargs):
    return {'page':page,'start':0,'end':110,'now':110,'period':'all','model':'','source':'','archived':True,
            'unit':'response','method':'mean','metric':'cost','band':None,'granularity':'auto',**kwargs}


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
