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
    w.show()
    def settle():
        limit=time.monotonic()+20
        while time.monotonic()<limit:
            app.processEvents();QTest.qWait(40)
            if w.analysis_errors: raise AssertionError(str(w.analysis_errors))
            if not w.analysis_pending and w.view_result is not None and w.applied_key[1]==w.current_page: return
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
        w.nav.setCurrentRow(w.navigation_pages.index(2)); settle()
        assert w.record_view=='projects' and w.table.rowCount()==1 and w.selected_session is None
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
        assert w.analysis['response_count']==8
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
        assert not w.analysis_errors and w.analysis['response_count']==8
    finally:
        w.quitting=True; w.tick.stop()
        w.worker.requestInterruption()
        assert w.worker.wait(6000)
        assert not w.worker.process.is_alive()
        w.tray.hide(); w.close()


@pytest.mark.parametrize('shown_before',[False,True])
def test_hidden_dashboard_defers_all_page_refreshes(tmp_path,monkeypatch,shown_before):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    w=Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'hidden.ini'),QSettings.IniFormat))
    try:
        if shown_before:w.show();app.processEvents();w.hide();app.processEvents()
        w.tick.stop()
        calls=[]
        monkeypatch.setattr(w.quota_panel,'refresh_status',lambda:calls.append('quota'))
        for page in range(5):
            w.current_page=page;w._display_dirty=False
            w.render(automatic=True);w.check_stale()
            assert w._display_dirty and not calls
        w.current_page=3;w.show();app.processEvents()
        assert calls==['quota'] and not w._display_dirty
    finally:
        w.quitting=True;w.tick.stop();w.tray.hide();w.close()


def test_active_session_parts_survive_more_sessions_than_query_lru():
    source=history()
    sessions=[dict(source,id=f'session-{i}',home=f'home-{i}') for i in range(2200)]
    engine=AnalysisEngine();engine.ingest(sessions)
    first=engine.query(query(page=2,include_whole_history=False))
    builds=engine.metrics['part_builds']
    second=engine.query(query(page=2,now=111,end=111,include_whole_history=False))
    assert engine.metrics['part_builds']==builds
    assert second['analysis']['totals']==first['analysis']['totals']


def test_live_revisions_release_obsolete_views_without_rebuilding_unchanged_sessions():
    source=history();other=copy.deepcopy(source);other['id']='other';other['home']='other-home'
    engine=AnalysisEngine();engine.ingest([source,other])
    first=engine.page_query(query(page=2,presentation=True))
    builds=engine.metrics['part_builds']
    for i in range(12):
        changed=copy.deepcopy(source)
        changed['history'][0]['output']+=i+1
        changed['history'][0]['total']+=i+1
        engine.ingest([changed,other])
        latest=engine.page_query(query(page=2,presentation=True))
        assert latest['analysis']['response_count']==14
        assert engine.parts.weight<=16
        assert engine.page_results.weight==15
    assert engine.metrics['part_builds']==builds+12
    assert first['analysis']['totals']['cost']<latest['analysis']['totals']['cost']
    assert engine.page_query(query(page=2,presentation=True)) is latest


def test_dashboard_projection_bounds_rows_and_preserves_drilldown():
    import pickle
    from cachemonitor.dashboard_views import resolve_population
    source=history()
    sessions=[dict(source,id=f's-{i}',home=f'home-{i}',title=f'Session {i}') for i in range(800)]
    engine=AnalysisEngine();engine.ingest(sessions)
    overview=engine.page_query(query(page=0,presentation=True))
    assert overview['analysis']['response_count']==5600
    assert 'responses' not in overview['analysis']
    assert len(pickle.dumps(overview))<200_000
    summary=overview['summaries'][0]
    records=resolve_population(engine,summary['population'])
    assert sum(r['cost'] for r in records)==pytest.approx(overview['overview']['total'])
    q=query(page=2,presentation=True,explorer=dict(view='calls',search='',sort='cost_desc'),
        table_windows={'records':{'start':500,'size':128},'parents':{'start':0,'size':64}})
    from cachemonitor.history_projection import node_key
    from cachemonitor.analytics import project_identity
    q['explorer']['expanded']=[node_key('project',project_identity(source))]
    projected=engine.page_query(q)['explorer']
    assert projected['records']['total']==5600
    assert projected['records']['start']==500 and len(projected['records']['rows'])==128
    assert len(projected['parents']['rows'])==64
    assert all('calls' not in row or not isinstance(row['calls'],list) for row in projected['records']['rows'])
    q['explorer']['search']='Session 42'
    found=engine.page_query(q)['explorer']
    assert all('Session 42' in r['title'] for r in found['records']['rows'])
    assert found['records']['total']<5600


def test_worker_discards_superseded_result_before_sending(monkeypatch):
    from cachemonitor import analysis_worker
    source=history()
    snapshot=dict(ts=110,sessions=[source],homes=[],errors=[],unassigned=[],index={'loading':False})
    messages=[dict(kind='query',id=1,logical='first',query=query(page=2))];sent=[]
    original=AnalysisEngine.page_query
    def compute(self,q):
        result=original(self,q)
        if not any(m.get('id')==2 for m in messages) and not getattr(self,'tested_supersession',False):
            self.tested_supersession=True
            messages.append(dict(kind='query',id=2,logical='latest',query=query(page=0)))
        return result
    monkeypatch.setattr(AnalysisEngine,'page_query',compute)
    class Connection:
        def poll(self,*args):return bool(messages)
        def recv(self):return messages.pop(0)
        def send(self,value):
            sent.append(value)
            if value['kind']=='result':messages.append({'kind':'stop'})
        def close(self):pass
    analysis_worker.process_main(Connection(),[],None,static_snapshot=snapshot)
    assert [m['id'] for m in sent if m['kind']=='result']==[2]


def test_projected_cache_expires_at_sliding_boundary_and_metadata_revision():
    engine=AnalysisEngine();source=history();engine.ingest([source])
    q=query(page=2,presentation=True,period='30m',start=99,end=1899,now=1899)
    first=engine.page_query(q)
    assert first['analysis']['response_count']==7
    assert engine.page_query({**q,'now':1899.5,'end':1899.5,'start':99.5}) is first
    later={**q,'now':1900.1,'end':1900.1,'start':100.1}
    assert engine.page_query(later)['analysis']['response_count']==6
    source=copy.deepcopy(source);source['title']='Renamed session';engine.ingest([source])
    renamed=engine.page_query(later)
    assert renamed['revision']!=first['revision']
    from cachemonitor.history_projection import node_key
    from cachemonitor.analytics import project_identity
    expanded={**later,'explorer':{'view':'sessions','project':project_identity(source)}}
    assert engine.page_query(expanded)['explorer']['records']['rows'][0]['title']=='Renamed session'
    engine.ingest([])
    assert engine.page_query(later)['analysis']['response_count']==0


def test_projected_partial_bucket_updates_without_reaggregating():
    engine=AnalysisEngine();source=history()
    epoch=datetime(2026,9,14,12).timestamp()
    for row in source['history']:row['ts']+=epoch
    for row in source['turn_records'].values():
        for key in ('started_at','ended_at'):row[key]+=epoch
    engine.ingest([source])
    q=query(page=0,presentation=True,now=epoch+110,end=epoch+110)
    first=engine.page_query(q);builds=engine.metrics['view_builds']
    second=engine.page_query({**q,'now':epoch+111,'end':epoch+111})
    assert engine.metrics['view_builds']==builds
    assert second['overview']['timeline'][-1]['end_ts']==epoch+111
    assert second['overview']['total']==first['overview']['total']
