"""Replay a read-only index against an isolated native dashboard.

Timings start at control activation, exclude fixture waits, and include a forced
framebuffer completion. They do not claim physical display or mouse latency.
"""
import argparse,json,sqlite3,sys,tempfile,time,pickle
from pathlib import Path
from statistics import median

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--index',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--source-root',default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument('--async-worker',action='store_true')
    parser.add_argument('--snapshot',help='Reuse/create one frozen local snapshot for both builds')
    args=parser.parse_args();sys.path.insert(0,args.source_root)
    from cachemonitor.index import UsageIndex
    from cachemonitor.core import SessionRegistry
    from cachemonitor.dashboard import Dashboard,STYLE
    from PySide6.QtCore import QSettings,QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    if args.snapshot and Path(args.snapshot).exists():
        snapshot=pickle.loads(Path(args.snapshot).read_bytes())
    else:
        index=UsageIndex.__new__(UsageIndex)
        index.db=sqlite3.connect(Path(args.index).resolve().as_uri()+'?mode=ro',uri=True)
        index.metadata={(r[0],r[1]):json.loads(r[2]) for r in index.db.execute('select home,tid,data from metadata')}
        index.monitor=SessionRegistry(); index.loaded=set(); index.turn_states={}
        from collections import defaultdict
        index.new_events=defaultdict(list);index.event_tails={};index.rebuild_required=set()
        now=time.time()
        for key in index.db.execute('select distinct home,tid from events').fetchall(): index.rebuild(key,now)
        sessions=[]
        for key,s in index.monitor.sessions.items():
            if s.excluded_title or not s.requests: continue
            view=s.view(now); meta=index.metadata.get(key,{})
            view.update(title=meta.get('name') or meta.get('title') or s.title,source=meta.get('thread_source') or 'unknown',
                        archived=bool(meta.get('archived')),turn_states=index.turn_states.get(key,{}))
            sessions.append(view)
        index.db.close()
        snapshot={'ts':now,'sessions':sessions,'homes':[],'errors':[],'unassigned':[],'index':{'loading':False,'done':1,'files':1}}
        if args.snapshot:Path(args.snapshot).write_bytes(pickle.dumps(snapshot))
    app=QApplication([]);app.setProperty('cachemonitorDisableShellIntegration',True);app.setStyle('Fusion');app.setStyleSheet(STYLE)
    with tempfile.TemporaryDirectory(prefix='cachemonitor-benchmark-') as temp:
        options={'static_snapshot':snapshot} if args.async_worker else {'start_worker':False}
        w=Dashboard([],settings=QSettings(str(Path(temp)/'settings.ini'),QSettings.IniFormat),live_limits=False,**options)
        w.tick.stop()
        if not args.async_worker:w.receive(snapshot)
        w.resize(1480,1000);w.show()
        samples={};heartbeat=[];last=[time.perf_counter()];hidden=[0]
        def pulse():
            now=time.perf_counter();heartbeat.append((now-last[0])*1000);last[0]=now
            if not w.pages.isVisible():hidden[0]+=1
        probe=QTimer();probe.timeout.connect(pulse)
        def settle():
            deadline=time.perf_counter()+60
            while True:
                app.processEvents()
                assert not w.analysis_errors,w.analysis_errors
                pending=w.analysis_pending or w.deferred_result or getattr(w,'search_timer',None) and w.search_timer.isActive()
                if not pending:return
                if time.perf_counter()>deadline:raise RuntimeError('analysis timeout')
                QTest.qWait(1)
        def sample(name,action):
            settle();started=time.perf_counter();action();settle()
            frame=w.quick.grabFramebuffer();assert not frame.isNull()
            samples.setdefault(name,[]).append((time.perf_counter()-started)*1000)
        def summary(values):
            values=sorted(values)
            return dict(n=len(values),median_ms=median(values),p95_ms=values[int((len(values)-1)*.95)],max_ms=max(values)) if values else {}
        try:
            settle();last[0]=time.perf_counter();probe.start(10)
            for page in (0,1,2):sample('page_switch_first_cycle',lambda p=page:w.nav.setCurrentRow(p))
            for _ in range(10):
                for page in (0,1,2):sample('page_revisit',lambda p=page:w.nav.setCurrentRow(p))
                if w.model.count()>1:
                    sample('filter',lambda:w.model.setCurrentIndex(1))
                    sample('filter',lambda:w.model.setCurrentIndex(0))
                sample('search',lambda:w.search.setText('no-matching-session-benchmark'))
                sample('search',lambda:w.search.clear())
            probe.stop()
            report={'sessions':len(snapshot['sessions']),'calls':sum(len(s['history']) for s in snapshot['sessions']),
                    'mode':'async' if args.async_worker else 'sync','measurement':'control activation to forced framebuffer; not physical display latency',
                    'timings':{k:summary(v) for k,v in samples.items()},'raw_ms':samples,
                    'event_loop_intervals':summary(heartbeat),'body_hidden_samples':hidden[0],'qml_errors':w.qml_errors}
            w.quick.grabFramebuffer().save(str(Path(args.output).with_suffix('.png')))
            Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps({k:v for k,v in report.items() if k!='raw_ms'},ensure_ascii=False),flush=True)
        finally:w.quit_app()
    return 0

if __name__=='__main__':
    from multiprocessing import freeze_support
    freeze_support();raise SystemExit(main())
