"""Process isolation, bounded delivery, and durable exclusion acknowledgements."""
from functools import partial
import multiprocessing as mp
import os
import pickle
import queue
import sqlite3
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from cachemonitor.quota_worker import QuotaComputation, QuotaWorker
from test_quota_exclusions import sample_report


def wait_until(condition, timeout=10, app=None):
    deadline=time.monotonic()+timeout
    while not condition() and time.monotonic()<deadline:
        if app is not None:app.processEvents()
        else:time.sleep(.005)
    assert condition()


def public_series(series):
    expanded={k:v for k,v in series.items() if not k.startswith('_')}
    share=expanded['model_share']
    expanded['model_share']={k:v for k,v in share.items() if k!='bucket_ids' and not k.startswith('_')}
    if 'bucket_ids' in share:expanded['model_share']['bins']=dict(zip(share['bucket_ids'],share['bins']))
    return expanded


def controlled_process(connection,path,home,entered,release):
    try:
        while True:
            request=connection.recv()
            if request['kind']=='stop':return
            if request.get('key')=='crash':os._exit(21)
            if request.get('key') in ('slow','write','delayed-write'):
                if request['key']!='delayed-write':
                    connection.send(dict(kind='phase',value='writing' if request['key']=='write' else 'reading'))
                entered.set()
                # A CPU-bound calculation in this interpreter cannot take the
                # parent's GIL or block its Qt event queue.
                value=0
                deadline=time.monotonic()+20
                while not release.is_set() and time.monotonic()<deadline:
                    for index in range(10000):value=(value+index)%1000003
                if request['key']=='delayed-write':
                    connection.send(dict(kind='phase',value='writing'))
            connection.send({key:request.get(key) for key in ('kind','id','key')} |
                            dict(value=dict(pid=os.getpid(),key=request.get('key'))))
    except (EOFError,BrokenPipeError,OSError):
        pass
    finally:connection.close()


def delayed_preview_process(connection,path,home,entered,release,calls,blocked_kind='preview'):
    computation=QuotaComputation(path,home)
    first=True
    try:
        while True:
            request=connection.recv()
            if request['kind']=='stop':return
            if request['kind']==blocked_kind:
                if calls is not None:calls.put((request.get('start'),request.get('end')))
                if first:
                    first=False
                    entered.set()
                    deadline=time.monotonic()+15
                    value=0
                    while not release.is_set() and time.monotonic()<deadline:
                        for index in range(10000):value=(value+index)%1000003
            connection.send(computation.execute(request))
    except (EOFError,BrokenPipeError,OSError):pass
    finally:
        computation.close()
        connection.close()


def incomplete_process(connection,path,home,entered,release,crash=False):
    from cachemonitor.quota_transport import frames
    try:
        request=connection.recv()
        result={key:request.get(key) for key in ('kind','id','key')} | dict(
            report={'rows':[{'at':index,'value':index/10} for index in range(5000)]})
        payloads=iter(frames(result))
        connection.send_bytes(next(payloads))
        entered.set()
        if crash:os._exit(21)
        release.wait(10)
        for payload in payloads:connection.send_bytes(payload)
    except (EOFError,BrokenPipeError,OSError):pass
    finally:connection.close()


def scalar_chart_process(connection,path,home):
    from cachemonitor.quota_transport import send_result
    try:
        request=connection.recv()
        send_result(connection,{key:request.get(key) for key in ('kind','id','key')} |
                    dict(value={name:[i/10 for i in range(100000)] for name in ('times','values','costs')}))
        connection.recv()
    except (EOFError,BrokenPipeError,OSError):pass
    finally:connection.close()


def test_chart_arrays_finish_while_the_gui_event_loop_is_active():
    app=QApplication.instance() or QApplication([])
    received=[];ticks=[];timer=QTimer()
    timer.timeout.connect(lambda:ticks.append(True));timer.start(10)
    worker=QuotaWorker(process_target=scalar_chart_process)
    worker.result.connect(received.append)
    try:
        worker.request('chart',key='complete-array')
        wait_until(lambda:bool(received),timeout=10,app=app)
        assert ticks and not received[0].get('error')
        expected=[i/10 for i in range(100000)]
        assert received[0]['value']==dict(times=expected,values=expected,costs=expected)
    finally:timer.stop();worker.close()


def test_cpu_bound_child_keeps_gui_timers_running_and_coalesces_latest_query():
    app=QApplication.instance() or QApplication([])
    context=mp.get_context('spawn')
    entered,release=context.Event(),context.Event()
    results=[];ticks=[]
    worker=QuotaWorker(on_result=results.append,
        process_target=partial(controlled_process,entered=entered,release=release))
    try:
        worker.request('query',key='slow')
        wait_until(entered.is_set,app=app)
        worker.request('query',key='superseded')
        final=worker.request('query',key='latest')
        QTimer.singleShot(0,lambda:ticks.append(True))
        wait_until(lambda:bool(ticks),app=app)
        assert results==[] and worker.process.pid!=os.getpid()
        release.set()
        wait_until(lambda:bool(results),app=app)
        assert [(r['id'],r['value']['key']) for r in results]==[(final,'latest')]
    finally:
        release.set();worker.close()
    assert not worker.process.is_alive()


def test_failed_process_is_replaced_without_replaying_uncertain_edit():
    context=mp.get_context('spawn')
    results=[]
    worker=QuotaWorker(on_result=results.append,process_target=partial(controlled_process,
        entered=context.Event(),release=context.Event()))
    try:
        edit=worker.request('exclude',key='crash',start=1,end=2)
        wait_until(lambda:bool(results))
        assert results[0]['id']==edit and results[0]['uncertain'] and results[0]['permanent']
        old_pid=worker.process.pid
        latest=worker.request('report',key='recovered')
        wait_until(lambda:len(results)==2)
        assert results[1]['id']==latest and results[1]['value']['pid']!=old_pid
    finally:worker.close()


def test_shutdown_interrupts_only_reading_and_does_not_kill_committing_child():
    context=mp.get_context('spawn')
    for kind,key in (('report','slow'),('exclude','write'),('exclude','delayed-write')):
        entered,release=context.Event(),context.Event()
        worker=QuotaWorker(process_target=partial(controlled_process,entered=entered,release=release))
        try:
            worker.request(kind,key=key)
            wait_until(entered.is_set)
            if key in ('write','delayed-write'):
                assert not worker.close(timeout=.03)
                assert worker.process.is_alive()
                release.set()
                wait_until(lambda:not worker.thread.is_alive())
            else:
                start=time.monotonic()
                assert worker.close()
                assert time.monotonic()-start<2.5
            assert not worker.process.is_alive()
        finally:
            if worker.process is not None and worker.process.is_alive():release.set()
            worker.close()


def test_incomplete_graph_is_never_published_and_shutdown_cancels_transfer():
    context=mp.get_context('spawn')
    for crash in (False,True):
        entered,release=context.Event(),context.Event();results=[]
        worker=QuotaWorker(on_result=results.append,process_target=partial(incomplete_process,
            entered=entered,release=release,crash=crash))
        try:
            worker.request('report')
            wait_until(entered.is_set)
            if crash:
                wait_until(lambda:bool(results))
                assert results[0].get('error') and 'report' not in results[0]
            else:
                assert results==[]
                assert worker.close()
                assert results==[] and not worker.process.is_alive()
        finally:
            if worker.process is not None and worker.process.is_alive():release.set()
            worker.close()


def test_real_child_prepares_compact_report_and_reuses_raw_evidence_for_preview():
    responses=queue.Queue()
    worker=QuotaWorker(on_result=responses.put)
    source=sample_report()
    source['tracking']['started']=50
    try:
        worker.request('prepare',snapshot=source)
        prepared=responses.get(timeout=15)
        assert not prepared.get('error'),prepared
        report=prepared['report']
        assert not any(field in report for field in ('cycles','history','request_windows','tracking'))
        assert report['view']['tracking_started']==50
        assert report['view']['lifetime']['intervals']==[]
        assert report['view']['lifetime']['cost']==312
        assert report['view']['chart']['series']['_content_digest']
        assert len(report['view']['default_query']['rows'])<=25
        worker.request('preview',revision=report['worker_revision'],start=110,end=130)
        preview=responses.get(timeout=15)
        assert preview['value']['removed']==dict(cost=300,delta=10,calls=2)
        assert source['exclusions']==[]
        worker.request('query',revision=report['worker_revision'],query={'start':100,'end':110})
        query=responses.get(timeout=15)
        assert not query.get('error'),query
        assert query['value']['statistics']['delta']==1
        worker.request('chart',revision=report['worker_revision'],selection='all')
        whole=responses.get(timeout=15)
        assert not whole.get('error'),whole
        from cachemonitor.quota_cycles import _PackedRows
        from cachemonitor.quota_view import prepare_quota_view
        series=whole['value']['series']
        assert isinstance(series['rows'],_PackedRows)
        assert public_series(series)==prepare_quota_view(source)['overall']
    finally:worker.close()


def test_slow_accounting_does_not_delay_observation_storage(tmp_path,monkeypatch):
    from cachemonitor import quota_service
    app=QApplication.instance() or QApplication([])
    context=mp.get_context('spawn')
    entered,release=context.Event(),context.Event()
    monkeypatch.setattr(quota_service,'QuotaWorker',lambda *args,**kwargs:QuotaWorker(*args,**kwargs,
        process_target=partial(delayed_preview_process,entered=entered,release=release,
                               calls=None,blocked_kind='report')))
    service=quota_service.QuotaService(tmp_path/'home',tmp_path/'index.sqlite',live=False)
    received=[];ticks=[]
    service.updated.connect(received.append)
    at=time.time()
    def observation(offset):
        return dict(observed_at=at+offset,account='a',plan_type='pro',bucket='codex',windows={
            'weekly':dict(used_percent=10+offset,resets_at=at+604800,window_minutes=10080)})
    def stored():
        with sqlite3.connect(service.path) as db:
            return db.execute('select count(*) from observations').fetchone()[0]
    try:
        service.supply_local(observation(0));service.start()
        wait_until(entered.is_set,app=app)
        assert stored()==1
        service.supply_local(observation(1))
        QTimer.singleShot(0,lambda:ticks.append(True))
        wait_until(lambda:stored()==2 and bool(ticks),app=app)
        assert not any(value.get('report',{}).get('worker_revision') for value in received)
        release.set()
        wait_until(lambda:any(value.get('report',{}).get('worker_revision') for value in received),app=app)
    finally:
        release.set();service.stop()
    assert not service.report_worker.process.is_alive()


def test_compact_report_drops_call_graph_bytes_and_can_still_query_them():
    source=sample_report()
    # Irrelevant per-call metadata deliberately makes raw evidence large. It is
    # not a chart field and must never cross the presentation boundary.
    for cycle in source['cycles']:
        for row in cycle.get('cost_rows',[]):row['synthetic_metadata']='x'*100000
    engine=QuotaComputation()
    try:
        prepared=engine.execute(dict(kind='prepare',id=1,key=None,snapshot=source))
        assert not prepared.get('error'),prepared
        raw=next(iter(engine.reports.values()))
        packed=pickle.dumps(prepared['report'],protocol=5)
        assert len(packed)<len(pickle.dumps(raw,protocol=5))/2
        assert b'synthetic_metadata' not in packed
        assert prepared['report']['view']['lifetime']['cost']==312
    finally:engine.close()


def test_chart_delivery_contains_only_selection_and_reuses_unchanged_digest():
    from test_quota_value_history import sample_report as multiple_periods
    engine=QuotaComputation()
    source=multiple_periods()
    first=engine.execute(dict(kind='prepare',id=1,key=None,snapshot=source))['report']
    view=first['view'];chart=view['chart']
    assert chart['selection']==250
    assert all('series' not in period for period in view['periods'])
    assert 'overall' not in view and 'five_hour' not in view
    changed=engine.execute(dict(kind='chart',id=2,key='old',revision=first['worker_revision'],selection=100))['value']
    assert changed['selection']==100 and changed['series']['rows'][-1]['at']==130
    same=engine.execute(dict(kind='chart',id=3,key='same',revision=first['worker_revision'],selection=100,digest=changed['digest']))['value']
    assert same['series'] is None and same['digest']==changed['digest']
    whole=engine.execute(dict(kind='chart',id=4,key='all',revision=first['worker_revision'],selection='all'))['value']
    assert whole['series']['cumulative']
    assert len(whole['series']['rows'])==7
    from cachemonitor.quota_view import prepare_quota_view
    expected=prepare_quota_view(source)['overall']
    assert public_series(whole['series'])==expected
    assert engine.execute(dict(kind='chart',id=5,key='stale',revision='expired',selection=100))['stale']


def test_unchanged_report_reuses_attribution_and_moves_rolling_boundary(monkeypatch):
    from cachemonitor import quota_attribution
    from cachemonitor.quota_cycles import quota_statistics
    from test_quota_attribution import example
    from test_quota_value_history import report_for
    now=10*86400;cutoff=now-7*86400
    pieces=[example([(start,100),(start+30,99),(start+60,98)],[(str(start),start+5,start+40,2)])
            for start in (100,cutoff-10,now-300,now-200,now-100)]
    source=report_for([cycle for part in pieces for cycle in part['cycles']])
    source.update(at=now,request_windows=[row for part in pieces for row in part['request_windows']])
    engine=QuotaComputation();calls=[];original=quota_attribution.local_request_intervals
    def counted(*args):calls.append(1);return original(*args)
    monkeypatch.setattr(quota_attribution,'local_request_intervals',counted)
    try:
        engine._prepare(source,history_revision=1)
        for offset in (5,20,21,65,-1):
            current={**source,'at':now+offset};calls.clear()
            engine._prepare(current,history_revision=1)
            assert len(calls)<=1
            view=next(reversed(engine.reports.values()))['view']
            for days in (7,30,90):
                assert view['summaries'][days]==quota_statistics(current,current['at']-days*86400,include_mode_assumptions=True)
        current={**source,'at':now+5,'cycles':[{**cycle,'cycle_end':now+5} for cycle in source['cycles']]}
        engine._prepare(current,history_revision=1)
        view=next(reversed(engine.reports.values()))['view']
        assert view['summaries'][7]==quota_statistics(current,current['at']-7*86400,include_mode_assumptions=True)
        assert view['lifetime']==quota_statistics(current,include_mode_assumptions=True)
    finally:engine.close()


def test_unchanged_report_above_cache_capacity_keeps_reusable_closed_intervals(monkeypatch):
    from cachemonitor import quota_attribution
    from cachemonitor.quota_cycles import quota_statistics
    from test_quota_attribution import example
    from test_quota_value_history import report_for
    now=10*86400
    starts=[100]+[now-30000+n*90 for n in range(261)]
    parts=[example([(s,100),(s+30,99),(s+60,98)],[(str(s),s+5,s+40,2)]) for s in starts]
    source=report_for([c for p in parts for c in p['cycles']])
    class CountedRequests(list):
        scans=0
        def __iter__(self):self.scans+=1;return super().__iter__()
    requests=CountedRequests(r for p in parts for r in p['request_windows'])
    source.update(at=now,request_windows=requests)
    engine=QuotaComputation();original=quota_attribution.local_request_intervals;calls=[]
    def counted(*args):calls.append(1);return original(*args)
    try:
        engine._prepare(source,history_revision=1)
        scans=requests.scans
        monkeypatch.setattr(quota_attribution,'local_request_intervals',counted)
        current={**source,'at':now+5};engine._prepare(current,history_revision=1)
        assert requests.scans==scans
        assert len(calls)<20
        view=next(reversed(engine.reports.values()))['view']
        assert len(view['_statistics_cache'])<=256
        assert view['summaries'][7]==quota_statistics(current,current['at']-7*86400,include_mode_assumptions=True)
    finally:engine.close()


def test_pause_cancels_automatic_calculation_but_keeps_manual_queries_available():
    context=mp.get_context('spawn');entered=context.Event();release=context.Event();results=[]
    worker=QuotaWorker(on_result=results.append,
        process_target=partial(controlled_process,entered=entered,release=release))
    try:
        request=worker.request('report',key='slow')
        wait_until(entered.is_set)
        process=worker.process
        worker.set_paused(True)
        wait_until(lambda:any(item.get('cancelled') for item in results))
        assert results[-1]['id']==request and not process.is_alive()
        manual=worker.request('query',key='saved-records')
        wait_until(lambda:any(item['id']==manual for item in results))
        assert results[-1]['value']['key']=='saved-records'
        assert worker.restart_count==0
    finally:
        release.set();assert worker.close()
