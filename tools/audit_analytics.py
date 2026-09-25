"""Compare optimized results with a supplied pre-change analytics module on an index snapshot."""
import argparse,importlib.util,json,math,sqlite3,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.index import UsageIndex
from cachemonitor.core import SessionRegistry
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.analytics import comparison_view,overview_view

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--index',required=True)
    parser.add_argument('--reference',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    spec=importlib.util.spec_from_file_location('cachemonitor._reference',args.reference)
    reference=importlib.util.module_from_spec(spec); spec.loader.exec_module(reference)
    idx=UsageIndex.__new__(UsageIndex)
    idx.db=sqlite3.connect(Path(args.index).resolve().as_uri()+'?mode=ro',uri=True)
    idx.metadata={(r[0],r[1]):json.loads(r[2]) for r in idx.db.execute('select home,tid,data from metadata')}
    idx.monitor=SessionRegistry(); idx.loaded=set(); idx.turn_states={}
    now=time.time()
    for key in idx.db.execute('select distinct home,tid from events').fetchall(): idx.rebuild(key,now)
    sessions=[]
    for key,s in idx.monitor.sessions.items():
        if s.excluded_title or not s.requests: continue
        v=s.view(now); m=idx.metadata.get(key,{})
        v.update(title=m.get('name') or m.get('title') or s.title,archived=bool(m.get('archived')),
                 source=m.get('thread_source') or 'unknown',turn_states=idx.turn_states.get(key,{}))
        sessions.append(v)
    idx.db.close()
    engine=AnalysisEngine(); engine.ingest(sessions)
    compared=0; max_error=0
    def equal(a,b,path='root'):
        nonlocal compared,max_error
        if isinstance(a,dict):
            assert a.keys()==b.keys(),path
            for k in a: equal(a[k],b[k],path+'.'+str(k))
        elif isinstance(a,(list,tuple)):
            assert len(a)==len(b),path
            for i,(x,y) in enumerate(zip(a,b)): equal(x,y,path+f'[{i}]')
        elif isinstance(a,(int,float)) and not isinstance(a,bool):
            assert isinstance(b,(int,float)),path
            if isinstance(a,int): assert a==b,(path,a,b)
            else:
                assert math.isclose(a,b,rel_tol=1e-12,abs_tol=1e-10),(path,a,b)
                max_error=max(max_error,abs(a-b))
            compared+=1
        else: assert a==b,(path,a,b)
    cases=[]
    for page,unit,method,model,source,archived,start,metric,band in (
        (0,'response','mean','','',True,0,'cost',None),
        (0,'response','mean','','',False,now-30*86400,'total',None),
        (1,'response','mean','','',True,0,'cost',None),
        (1,'turn','median','','',True,0,'cost',None),
        (1,'response','mean','gpt-6-astra','',True,0,'cost',(100000,200000)),
        (1,'turn','mean','gpt-6-astra','user',False,now-7*86400,'total',None),
        (2,'response','mean','','subagent',True,now-86400,'cost',None),
        (2,'turn','median','','',True,now-1800,'cost',None)):
        q={'page':page,'start':start,'end':now+.001,'model':model,'source':source,'archived':archived,
           'unit':unit,'method':method,'metric':metric,'band':band,'granularity':'auto'}
        optimized=engine.query(q)
        original=reference.analyze(sessions,start,now+.001,model,source,archived,unit,method)
        for key in ('totals','priced_count','unpriced_count','turn_count','excluded_turns','missing_turn_responses','unclassified','unclassified_sessions'):
            equal(original[key],optimized['analysis'][key],key)
        if page==0: equal(reference.overview_view(original,start,now+.001,metric),optimized['overview'],'overview')
        if page==1:
            equal(original['basis'],optimized['analysis']['basis'],'basis')
            equal(reference.comparison_view(original,metric,band),optimized['comparison'],'comparison')
        cases.append({'page':page,'unit':unit,'method':method,'model':model,'source':source,'archived':archived,'metric':metric,'passed':True})
    report={'sessions':len(sessions),'calls':sum(len(s['history']) for s in sessions),'cases':cases,
            'numeric_values_compared':compared,'max_absolute_numeric_error':max_error,'metrics':engine.metrics}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
