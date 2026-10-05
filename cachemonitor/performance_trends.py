"""Worker-owned, bounded historical performance projections."""
from collections import defaultdict, deque
from math import isfinite
from bisect import bisect_right
from datetime import datetime, timedelta
from time import mktime
from statistics import median
from .analytics import effort_key, sorted_quantile

METRICS = (('cost','평균 호출 비용','cost'),
           ('output_speed','출력 속도','output_speed'), ('duration','평균 호출 소요시간','duration'),
           ('session_cache','세션 평균 캐시 적중률','cache_ratio'))

def valid(value):
    return type(value) in (int,float) and isfinite(value) and value >= 0


def calendar_edges(start,end,unit):
    """Local midnight boundaries; weeks start on Monday, months on day one."""
    first=datetime.fromtimestamp(start)
    current=first.replace(hour=0,minute=0,second=0,microsecond=0)
    if unit=='week':current-=timedelta(days=current.weekday())
    elif unit=='month':current=current.replace(day=1)
    edges=[]
    while True:
        try:stamp=mktime(current.timetuple())
        except (OSError,OverflowError):
            # Windows mktime rejects local dates before the Unix epoch.
            stamp=start+(current-first).total_seconds()
        edges.append(stamp)
        if stamp>end:return edges
        if unit=='month':current=current.replace(year=current.year+(current.month==12),month=current.month%12+1)
        else:current+=timedelta(days=7 if unit=='week' else 1)

def assessment(value, previous, cache=False, complete=True):
    n=len(previous)
    result=dict(baseline=None,delta=None,baseline_count=n,direction=0,status='비교 기록 부족')
    if not complete:
        result['status']='진행 중인 구간';return result
    if n<10:return result
    center=median(previous);mad=median(abs(x-center) for x in previous)
    delta=value-center
    abnormal=abs(delta)>3.5*1.4826*mad and abs(delta)>(10 if cache else abs(center)*.2)
    result.update(baseline=center,delta=delta,direction=(1 if delta>0 else -1) if abnormal else 0,
                  status='평소 범위 이탈' if abnormal else '평소 범위')
    return result

class PerformanceTrends:
    def __init__(self):
        self.signature=None;self.view=None

    def query(self,engine,q):
        width=max(64,min(1024,int(q.get('plot_width',768))))
        unit=q.get('granularity','day')
        if unit not in ('day','week','month'):unit='day'
        key=(engine.revision,tuple(q.get('time_range') or ()),width,unit)
        now=q['now']
        if key!=self.signature or now>=self.deadline:
            self.build(engine,q,width,unit);self.signature=key
        return dict(self.view)

    def build(self,engine,q,width,granularity):
        rows=sorted((r for s in engine.sessions.values() for r in s['prepared']['history']
                     if r.get('purpose')!='maintenance' and (r.get('requested_model') or r.get('model'))!='codex-auto-review' and valid(r.get('ts'))),key=lambda r:r['ts'])
        now=q['now'];first=rows[0]['ts'] if rows else now
        full=(first,max(now,rows[-1]['ts'] if rows else now,first+1))
        start,end=q.get('time_range') or full
        if end<=start:start,end=full
        rows=[r for r in rows if r['ts']<=end]
        edges=calendar_edges(min(first,start),end,granularity)
        self.deadline=calendar_edges(now,now,granularity)[-1]
        efforts=sorted({r.get('effort') or '미확인' for r in rows},key=effort_key)
        specs=list(METRICS)+[('reasoning:'+e,'평균 추론 토큰 / '+e,'reasoning') for e in efforts]+[('input','평균 입력 토큰','input'),('count','호출 수','count')]
        buckets=defaultdict(list);panels=[]
        for r in rows:
            buckets[(bisect_right(edges,r['ts'])-1,r.get('model') or '미확인',r.get('service_tier') or '미확인')].append(r)
        for metric,title,unit in specs:
            pools=defaultdict(lambda:deque(maxlen=50));pending={};pixels={};lines=defaultdict(list);n=0;N=0;bounds=[]
            def add(value,ts,model,mode,effort,record,complete=True,coverage=None):
                nonlocal n
                if not valid(value):return
                scope=(model,mode,effort if metric not in ('session_cache','count') else '')
                pool=pools[scope]
                prior=pending.get(scope)
                if prior and prior[0]<ts:
                    pool.extend(prior[1]);pending.pop(scope)
                verdict=assessment(value,pool,metric=='session_cache',complete)
                if complete:pending.setdefault(scope,(ts,[]))[1].append(value)
                if not start<=ts<=end:return
                n+=1;bounds.append(value)
                point=dict(ts=ts,value=value,model=model,service_tier=mode,effort=effort,
                           record=record,coverage=coverage,**verdict)
                pixel=min(width-1,max(0,int((ts-start)/(end-start)*width)))
                pk=(model,mode,pixel)
                cell=pixels.setdefault(pk,dict(low=point,high=point,first=point,last=point,anomalies=[]))
                cell['last']=point
                if value<cell['low']['value']:cell['low']=point
                if value>cell['high']['value']:cell['high']=point
                if verdict['direction']:cell['anomalies'].append(point)
            if metric not in ('session_cache','count'):
                field='reasoning' if metric.startswith('reasoning:') else metric
                for r in rows:
                    effort=r.get('effort') or '미확인'
                    if metric.startswith('reasoning:') and effort!=metric.split(':',1)[1]:continue
                    if start<=r['ts']<=end:N+=1
                    add(r.get(field),r['ts'],r.get('model') or '미확인',r.get('service_tier') or '미확인',effort,
                        {k:r.get(k) for k in ('home','sid','key','turn')})
            for (b,model,mode),items in sorted(buckets.items()):
                a,z=edges[b:b+2]
                if a>end:continue
                own=[r for r in items if not metric.startswith('reasoning:') or (r.get('effort') or '미확인')==metric.split(':',1)[1]]
                visible=a<=end and z>start
                if visible:own=[r for r in own if start<=r['ts']<=end]
                if not own:continue
                totals={}
                if metric=='session_cache':
                    sessions=defaultdict(list)
                    for r in own:sessions[(r['home'],r['sid'])].append(r)
                    values=[];input_total=0;cached_total=0;valid_calls=0
                    for records in sessions.values():
                        pairs=[r for r in records if valid(r.get('input')) and valid(r.get('cached')) and r['cached']<=r['input'] and not r.get('input_conflict')]
                        inp=sum(r['input'] for r in pairs)
                        if inp:
                            cached=sum(r['cached'] for r in pairs)
                            input_total+=inp;cached_total+=cached;valid_calls+=len(pairs)
                            value=100*cached/inp;values.append(value)
                            r=records[-1];add(value,min(z,end),model,mode,'', {k:r.get(k) for k in ('home','sid','key','turn')},coverage=(len(pairs),len(records)))
                    if visible:N+=len(sessions)
                    mean=sum(values)/len(values) if values else None
                    eligible,total=len(values),len(sessions)
                    totals=dict(input=input_total,cached=cached_total,valid_calls=valid_calls)
                else:
                    field='reasoning' if metric.startswith('reasoning:') else metric
                    values=[r[field] for r in own if valid(r.get(field))]
                    mean=sum(values)/len(values) if values else None
                    eligible,total=len(values),len(own)
                    totals={'sum':sum(values)} if values else {}
                    if metric=='output_speed':
                        measured=[r for r in own if valid(r.get('output_speed'))]
                        seconds=sum(r['completion_latency_ms']/1000 for r in measured)
                        mean=sum(r['output'] for r in measured)/seconds if seconds else None
                        totals=dict(output=sum(r['output'] for r in measured),seconds=seconds)
                    if metric=='count':
                        mean=len(own);eligible=len(own)
                        if visible:N+=1
                        r=own[-1];add(mean,min(z,end),model,mode,'', {k:r.get(k) for k in ('home','sid','key','turn')},z<=now and z<=end and (z<=start or a>=start), (len(own),len(own)))
                if visible:
                    ordered=sorted(values)
                    distribution=dict(median=sorted_quantile(ordered,.5),
                        p10=sorted_quantile(ordered,.1) if len(ordered)>=10 else None,
                        p90=sorted_quantile(ordered,.9) if len(ordered)>=10 else None,
                        minimum=min(ordered,default=None),maximum=max(ordered,default=None))
                    series=lines[(model,mode)]
                    complete=a>=start and z<=min(now,end)
                    previous=series[-1] if series else None
                    comparable=(complete and previous and previous['complete'] and previous['bucket']==b-1
                                and valid(mean) and valid(previous['value']))
                    baseline=previous['value'] if comparable else None
                    status='진행 중인 구간' if z>now else '일부 구간' if not complete else '비교 구간 없음'
                    series.append(dict(kind='trend',ts=(max(a,start)+min(z,end))/2,value=mean,bucket=b,
                        start=max(a,start),end=min(z,end),complete=complete,model=model,service_tier=mode,
                        effort=metric.split(':',1)[1] if metric.startswith('reasoning:') else '',
                        coverage=(eligible,total),baseline=baseline,delta=mean-baseline if baseline is not None else None,
                        distribution=distribution,totals=totals,calls=len(own),sessions=len({(r['home'],r['sid']) for r in own}),
                        baseline_count=previous['coverage'][0] if comparable else 0,direction=0,status=status,
                        previous_start=previous['start'] if comparable else None,previous_end=previous['end'] if comparable else None))
            samples=[]
            for (model,mode,pixel),cell in pixels.items():
                unique={id(cell[k]):cell[k] for k in ('first','low','high','last')}
                samples.extend(unique.values())
                for direction in (-1,1):
                    points=[p for p in cell['anomalies'] if p['direction']==direction]
                    if points:
                        samples.append(dict(points[0],count=len(points)))
            panels.append(dict(key=metric,title=title,unit=unit,n=n,N=N,points=sorted(samples,key=lambda p:p['ts']),
                               low=min(bounds,default=0),high=max(bounds,default=1),
                               lines=[dict(model=m,service_tier=t,points=v) for (m,t),v in lines.items()]))
        self.view=dict(panels=panels,start=start,end=end,full=full,granularity=granularity,models=sorted({r.get('model') or '미확인' for r in rows}),
                       calls=len(rows),valid_until=self.deadline)
