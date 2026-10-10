"""Read-only local-data replay on an isolated dashboard, usable by frozen builds."""
import argparse
import copy
import json
import multiprocessing
import sqlite3
import tempfile
import time
from pathlib import Path
from statistics import median


def summary(values):
    values=sorted(values)
    return dict(n=len(values),median_ms=median(values),p95_ms=values[min(len(values)-1,int(len(values)*.95))],
                p99_ms=values[min(len(values)-1,int(len(values)*.99))],max_ms=max(values)) if values else {}


def private_bytes(pid):
    """Committed private memory, without exposing a process identifier in reports."""
    import ctypes
    from ctypes import wintypes as W
    class Counters(ctypes.Structure):
        _fields_=[('cb',W.DWORD),('faults',W.DWORD)]+[(name,ctypes.c_size_t) for name in
            ('peak_working','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_pagefile','private')]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    api=ctypes.WinDLL('psapi',use_last_error=True)
    kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];kernel.OpenProcess.restype=W.HANDLE
    kernel.CloseHandle.argtypes=[W.HANDLE]
    api.GetProcessMemoryInfo.argtypes=[W.HANDLE,ctypes.POINTER(Counters),W.DWORD]
    handle=kernel.OpenProcess(0x410,False,pid)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    try:
        value=Counters();value.cb=ctypes.sizeof(value)
        if not api.GetProcessMemoryInfo(handle,ctypes.byref(value),value.cb):raise ctypes.WinError(ctypes.get_last_error())
        return value.private
    finally:kernel.CloseHandle(handle)


def read_snapshot(index):
    from .usage_collection import CollectionChannel
    channel=CollectionChannel.__new__(CollectionChannel)
    channel.path=Path(index).resolve();channel.snapshot_path=channel.companion('.collection.sqlite')
    with sqlite3.connect(channel.snapshot_path.as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT scope FROM snapshot WHERE id=1').fetchone()
    if not row:raise ValueError('No published collection snapshot')
    channel.scope=row[0];channel.identity=None;channel.cached=None;channel.session_cache={};channel.activity_cache={};channel.activity_identity=None
    return channel.read(shared=True)


def publish_replay(connection,snapshot,index):
    from .usage_collection import CollectionChannel
    channel=CollectionChannel(snapshot['homes'],index)
    snapshot['index']={**snapshot.get('index',{}),'path':str(Path(index).resolve()),'loading':False}
    sequence=0
    try:
        while True:
            sequence+=1;snapshot['ts']+=2
            source=next((s for s in reversed(snapshot['sessions']) if s['history']),None)
            if source:
                source['usage_revision']=f'replay-{sequence}'
                row=source['history'][-1]
                for field in ('output','total'):
                    if type(row.get(field)) is int:row[field]+=1
            channel.publish(snapshot,'replay',sequence)
            if sequence==1:connection.send(True)
            if connection.poll(2):break
    finally:channel.close();connection.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--samples',type=int,default=30)
    parser.add_argument('--scale',type=int,default=1)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--profile',action='store_true')
    args=parser.parse_args()
    from collections import defaultdict
    from PySide6.QtCore import QSettings,QTimer,Qt,QPointF,QEvent
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from .dashboard import Dashboard
    from .quick_qa import control,walk,wheel,render_plot,table_view
    from .overlay_view import SessionOverlay
    from .analysis_engine import AnalysisEngine
    from .overlay_data import OverlaySummaries
    snapshot=read_snapshot(args.index)
    original=snapshot['sessions'];sessions=list(original)
    for copy_index in range(1,args.scale):
        copied=copy.deepcopy(original)
        for session in copied:session['home']+=f'/replay-{copy_index}'
        sessions.extend(copied)
    snapshot={**snapshot,'sessions':sessions,'homes':sorted({s['home'] for s in sessions})}
    app=QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration',True)
    from . import quick_runtime
    original_widget=quick_runtime.QQuickWidget
    class MeasuredWidget(original_widget):
        def paintEvent(self,event):
            super().paintEvent(event)
            callback=getattr(self,'frame_probe',None)
            if callback:callback()
    quick_runtime.QQuickWidget=MeasuredWidget
    samples=defaultdict(list);heartbeat=[];frame_gaps=[];last=[time.perf_counter()];last_frame=[None];tracking=[False]
    import cProfile
    profiler=cProfile.Profile() if args.profile else None
    instrumented=[]
    if args.profile:
        from .table_model import Rows,Table
        from .presentation import Node
        for cls,name in [(Rows,'replace'),(Table,'set_window'),(Dashboard,'apply_explorer'),(Dashboard,'render_record_parent'),(Dashboard,'save_preferences'),(Node,'notify_state')]:
            original_method=getattr(cls,name)
            def timed(self,*a,_original=original_method,_name=name,**kw):
                started=time.perf_counter()
                try:return _original(self,*a,**kw)
                finally:samples['span_'+_name].append((time.perf_counter()-started)*1000)
            instrumented.append((cls,name,original_method));setattr(cls,name,timed)
    original_apply=Dashboard.apply_result
    def apply(window,*a,**kw):
        started=time.perf_counter()
        try:
            if profiler:profiler.enable()
            return original_apply(window,*a,**kw)
        finally:
            if profiler:profiler.disable()
            elapsed=(time.perf_counter()-started)*1000
            samples['gui_apply'].append(elapsed)
            samples['gui_apply_page_'+str(a[1]['page'])].append(elapsed)
    Dashboard.apply_result=apply
    process=None;connection=None;overlay=None;window=None;memory=[];queue_sizes=[]
    with tempfile.TemporaryDirectory(prefix='codexon-dashboard-replay-') as folder:
        index=str(Path(folder)/'index.sqlite')
        try:
            if args.live:
                context=multiprocessing.get_context('spawn');connection,child=context.Pipe()
                process=context.Process(target=publish_replay,args=(child,snapshot,index))
                process.start();child.close()
                if not connection.poll(60):raise TimeoutError('Replay publisher startup')
                connection.recv()
            window=Dashboard(snapshot['homes'] if args.live else [],index_path=index,
                quota_path=str(Path(folder)/'quota.sqlite'),static_snapshot=None if args.live else snapshot,
                settings=QSettings(str(Path(folder)/'settings.ini'),QSettings.IniFormat),
                live_limits=False,collection_autostart=False)
            window.resize(1480,1000);window.show()
            def pending():
                return window.analysis_pending or window.deferred_result or window.search_timer.isActive() or window.range_timer.isActive() or window.pending_population
            def settle():
                deadline=time.perf_counter()+60
                while True:
                    app.processEvents()
                    if window.analysis_errors:raise RuntimeError('Replay analysis failed')
                    if window.view_result is not None and not pending():break
                    if time.perf_counter()>deadline:raise TimeoutError('Replay interaction')
                    QTest.qWait(1)
            def sample(name,action):
                settle();started=time.perf_counter();action();settle()
                samples[name].append((time.perf_counter()-started)*1000)
                probe.stop();window.quick.grabFramebuffer();last[0]=time.perf_counter();probe.start(10)
            def resources():
                import os
                memory.append(dict(gui=private_bytes(os.getpid()),worker=private_bytes(window.worker.process.pid)))
                with window.worker.lock:queue_sizes.append(len(window.worker.commands)+int(window.worker.pending is not None))
            def navigate(page):
                item=control(window,window.nav)
                row=window.navigation_pages.index(page)
                delegate=next(x for x in walk(item) if x.property('index')==row and x.metaObject().indexOfProperty('modelData')>=0)
                point=delegate.mapToScene(QPointF(delegate.width()/2,delegate.height()/2))
                QTest.mouseClick(window.quick,Qt.LeftButton,pos=point.toPoint())
            def type_search(clear=False):
                control(window,window.search).forceActiveFocus()
                QTest.keyClick(window.quick,Qt.Key_A,Qt.ControlModifier)
                QTest.keyClick(window.quick,Qt.Key_Backspace)
                if not clear:QTest.keyClicks(window.quick,'no-matching-replay-session')
            def choose_model(index):
                control(window,window.model).forceActiveFocus()
                QTest.keyClick(window.quick,Qt.Key_Home)
                for _ in range(index):QTest.keyClick(window.quick,Qt.Key_Down)
                QTest.keyClick(window.quick,Qt.Key_Escape)
                assert window.model.currentIndex()==index
            def pulse():
                now=time.perf_counter();heartbeat.append((now-last[0])*1000);last[0]=now
            def frame():
                now=time.perf_counter()
                if tracking[0] and last_frame[0] is not None:frame_gaps.append((now-last_frame[0])*1000)
                last_frame[0]=now
            probe=QTimer();probe.timeout.connect(pulse)
            render_start=[None]
            def rendering_begin():
                if tracking[0]:render_start[0]=time.perf_counter()
            def rendering_end():
                if tracking[0] and render_start[0] is not None:
                    frame_gaps.append((time.perf_counter()-render_start[0])*1000);render_start[0]=None
            window.quick.quickWindow().beforeSynchronizing.connect(rendering_begin)
            window.quick.quickWindow().afterRendering.connect(rendering_end)
            settle()
            # A real overlay scene remains mounted throughout interaction replay.
            engine=AnalysisEngine();engine.ingest(snapshot['sessions'][:1])
            content=OverlaySummaries().collect(engine)
            overlay=SessionOverlay()
            if content:overlay.set_content(content[0])
            overlay.show();QTest.qWait(50)
            window.quick.grabFramebuffer()
            samples.clear();last[0]=time.perf_counter();probe.start(10)
            cpu_started=time.process_time();work_started=time.perf_counter()
            for page in (1,2,0):sample('page_first',lambda page=page:navigate(page))
            for iteration in range(args.samples):
                for page in (1,2,0):sample('page_revisit',lambda page=page:navigate(page))
                navigate(2);settle()
                sample('search',type_search);sample('search_clear',lambda:type_search(True))
                if window.model.count()>1:
                    sample('filter',lambda:choose_model(1));sample('filter',lambda:choose_model(0))
                resources()
            def select_parent(iteration):
                table_view(window,window.parent_table).forceActiveFocus()
                QTest.keyClick(window.quick,Qt.Key_Home)
                for _ in range(iteration%min(8,window.parent_table.rowCount())):QTest.keyClick(window.quick,Qt.Key_Down)
                QTest.keyClick(window.quick,Qt.Key_Return)
            def back():
                item=control(window,window.back_button)
                point=item.mapToScene(QPointF(item.width()/2,item.height()/2))
                QTest.mouseClick(window.quick,Qt.LeftButton,pos=point.toPoint())
            for i in range(args.samples):
                if not window.parent_table.rowCount():break
                sample('selection',lambda i=i:select_parent(i));sample('selection_back',back)
                resources()
            # Unseen model/period combinations exercise actual query work rather than cache hits.
            def new_filter(iteration):
                period=control(window,window.period);period.forceActiveFocus()
                QTest.keyClick(window.quick,Qt.Key_Home)
                for _ in range(iteration%5):QTest.keyClick(window.quick,Qt.Key_Down)
                QTest.keyClick(window.quick,Qt.Key_Escape)
                choose_model((iteration//5)%max(1,window.model.count()))
            for i in range(args.samples):sample('filter_new',lambda i=i:new_filter(i))
            # Restore the broad population for scrolling.
            control(window,window.period).forceActiveFocus();QTest.keyClick(window.quick,Qt.Key_Home)
            for _ in range(4):QTest.keyClick(window.quick,Qt.Key_Down)
            QTest.keyClick(window.quick,Qt.Key_Escape);choose_model(0);settle()
            # Render-completion cadence while actual wheel events are delivered.
            navigate(2);settle();window.selected_session=None;window.selected_turn=None;window.record_view='calls';window.render_explorer();settle()
            tracking[0]=True;last_frame[0]=None
            for i in range(180):
                deadline=time.perf_counter()+1/60
                wheel(window,window.table,-120 if i<90 else 120)
                app.processEvents()
                probe.stop();window.quick.grabFramebuffer();last[0]=time.perf_counter();probe.start(10)
                while time.perf_counter()<deadline:app.processEvents();QTest.qWait(1)
            tracking[0]=False;settle()
            # Cursor-independent hover dispatch through the rendered chart scene.
            navigate(0);settle();plot=render_plot(window,window.timeline)
            for i in range(max(30,args.samples)):
                point=plot.mapToScene(QPointF(100+i%20*8,80+i%5*12))
                started=time.perf_counter()
                event=QMouseEvent(QEvent.MouseMove,point,point,Qt.NoButton,Qt.NoButton,Qt.NoModifier)
                QApplication.sendEvent(window.quick,event);app.processEvents()
                samples['hover_dispatch'].append((time.perf_counter()-started)*1000)
            for i in range(30):sample('resize',lambda i=i:window.resize(1480+i%2*20,1000))
            probe.stop()
            cpu_seconds=time.process_time()-cpu_started;work_seconds=time.perf_counter()-work_started
            report=dict(sessions=len(sessions),calls=sum(len(s['history']) for s in sessions),live=args.live,
                scale=args.scale,renderer=str(window.quick.quickWindow().rendererInterface().graphicsApi()),dpr=window.devicePixelRatioF(),
                measurement='Qt input delivery through settled view; scroll render synchronization through afterRendering, excluding framebuffer readback; physical presentation latency unavailable on hidden desktop',
                timings={k:summary(v) for k,v in samples.items()},event_loop_intervals=summary(heartbeat),
                scroll_render_work=summary(frame_gaps),qml_errors=len(window.qml_errors),
                memory_private_bytes=memory,pending_commands_max=max(queue_sizes,default=0),
                gui_cpu_seconds=cpu_seconds,workload_seconds=work_seconds,
                gui_cpu_one_core_percent=100*cpu_seconds/work_seconds)
            if profiler:
                import io,pstats
                stream=io.StringIO();pstats.Stats(profiler,stream=stream).sort_stats('cumulative').print_stats(25)
                report['profile']=stream.getvalue()
            target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True)
            for width,height in ((1120,760),(1440,940)):
                window.resize(width,height);settle()
                window.grab().save(str(target.with_name(target.stem+f'-{width}.png')))
            overlay.grab().save(str(target.with_name(target.stem+'-overlay.png')))
            target.write_text(json.dumps(report,indent=2),encoding='utf-8')
            import sys
            if sys.stdout:print(json.dumps(report),flush=True)
            return 0
        finally:
            for cls,name,method in instrumented:setattr(cls,name,method)
            Dashboard.apply_result=original_apply
            quick_runtime.QQuickWidget=original_widget
            if overlay:overlay.release_scene();overlay.close()
            if window:window.quit_app()
            if connection:
                connection.send('stop');process.join(10);connection.close()
                if process.is_alive():process.terminate();process.join();raise RuntimeError('Replay publisher did not stop')


if __name__=='__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
