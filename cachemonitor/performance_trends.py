"""Worker-owned, bounded historical performance projections."""
from collections import defaultdict, deque
from math import ceil, isfinite
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from time import mktime
from statistics import median
from .analytics import effort_key, sorted_quantile

METRICS = (('cost','평균 호출 비용','cost'),
           ('output_speed','출력 속도','output_speed'), ('duration','평균 호출 소요시간','duration'),
           ('session_cache','세션 평균 캐시 적중률','cache_ratio'))

MIN_CENTER = 5
MIN_BAND = 10
MIN_OUTER_BAND = 30


def rolling_window(span):
    """One common duration for every model/mode, independent of sample density."""
    target=max(1.,span/24)
    steps=(1,2,5,10,15,30,60,120,300,600,900,1800,3600,7200,10800,21600,43200,
           86400,172800,345600,604800,1209600,2592000)
    return next((step for step in steps if step>=target),ceil(target/2592000)*2592000)


def metric_population(items,metric):
    """Values and their denominators; session cache samples retain equal session weight."""
    if metric=='count':
        return [],len(items),dict(sum=len(items))
    if metric=='session_cache':
        sessions=defaultdict(lambda:[0,0,0])
        for row in items:
            totals=sessions[(row['home'],row['sid'])]
            if (valid(row.get('input')) and valid(row.get('cached')) and row['cached']<=row['input']
                    and not row.get('input_conflict')):
                totals[0]+=row['input'];totals[1]+=row['cached'];totals[2]+=1
        eligible=[t for t in sessions.values() if t[0]>0]
        values=[100*t[1]/t[0] for t in eligible]
        return values,len(sessions),dict(input=sum(t[0] for t in eligible),cached=sum(t[1] for t in eligible),
                                        valid_calls=sum(t[2] for t in eligible))
    field='reasoning' if metric.startswith('reasoning:') else metric
    measured=[r for r in items if valid(r.get(field))]
    values=[r[field] for r in measured]
    totals=dict(sum=sum(values))
    if metric=='output_speed':
        # Use exactly the same eligible calls for numerator, denominator, and distribution.
        measured=[r for r in measured if valid(r.get('output')) and valid(r.get('completion_latency_ms'))
                  and r['completion_latency_ms']>0]
        values=[r[field] for r in measured]
        totals=dict(output=sum(r['output'] for r in measured),seconds=sum(r['completion_latency_ms']/1000 for r in measured))
    return values,len(items),totals


def distribution_statistics(values):
    ordered=sorted(values)
    return dict(median=sorted_quantile(ordered,.5),q1=sorted_quantile(ordered,.25),q3=sorted_quantile(ordered,.75),
                p10=sorted_quantile(ordered,.1) if len(ordered)>=MIN_OUTER_BAND else None,
                p90=sorted_quantile(ordered,.9) if len(ordered)>=MIN_OUTER_BAND else None,
                minimum=ordered[0] if ordered else None,maximum=ordered[-1] if ordered else None)


def representative_mean(values,totals,metric):
    if metric=='count':return totals['sum']
    if metric=='output_speed':return totals['output']/totals['seconds'] if totals['seconds'] else None
    return sum(values)/len(values) if values else None


def bounded_observations(items,metric,start,end,width):
    """Preserve extrema and endpoints per screen column without changing statistics."""
    if metric in ('session_cache','count') or end-start>3600:return []
    field='reasoning' if metric.startswith('reasoning:') else metric
    cells={}
    for row in items:
        value=row.get(field)
        if not valid(value):continue
        if metric=='output_speed' and not (valid(row.get('output')) and valid(row.get('completion_latency_ms')) and row['completion_latency_ms']>0):continue
        column=min(width-1,int((row['ts']-start)/(end-start)*width))
        point=dict(ts=row['ts'],value=value,model=row.get('model') or '미확인',service_tier=row.get('service_tier') or '미확인')
        cell=cells.setdefault(column,[point,point,point,point]);cell[3]=point
        if value<cell[1]['value']:cell[1]=point
        if value>cell[2]['value']:cell[2]=point
    return sorted((p for cell in cells.values() for p in {id(p):p for p in cell}.values()),key=lambda p:p['ts'])

def valid(value):
    return type(value) in (int,float) and isfinite(value) and value >= 0


def box_statistics(ordered):
    """Exact quartiles/counts, with bounded representative outlier marks."""
    q1,q3=sorted_quantile(ordered,.25),sorted_quantile(ordered,.75)
    if not ordered:return dict(q1=None,q3=None,whisker_low=None,whisker_high=None,outliers=[],outlier_count=0)
    spread=q3-q1
    left=bisect_left(ordered,q1-1.5*spread);right=bisect_right(ordered,q3+1.5*spread)
    count=left+len(ordered)-right
    # Preserve both tails without sending every call through the UI snapshot.
    marks=[]
    for start,end in ((0,left),(right,len(ordered))):
        size=end-start
        indices=range(size) if size<=64 else (round(i*(size-1)/63) for i in range(64))
        marks.extend(ordered[start+i] for i in indices)
    return dict(q1=q1,q3=q3,whisker_low=ordered[left],whisker_high=ordered[right-1],
                outliers=sorted(set(marks)),outlier_count=count)


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
        if unit not in ('auto','day','week','month'):unit='day'
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
        if granularity=='auto':
            self.build_rolling(rows,q,width,start,end,full)
            return
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
                        minimum=min(ordered,default=None),maximum=max(ordered,default=None),**box_statistics(ordered))
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

    def build_rolling(self,history,q,width,start,end,full):
        """Project exact overlapping populations once in the worker, never in paint/hover."""
        now=q['now'];span=end-start;window=rolling_window(span)
        step=max(window/4,span/max(16,width//6));steps=max(1,ceil(span/step))
        centers=[start+span*i/steps for i in range(steps+1)]
        rows=[r for r in history if start<=r['ts']<=end]
        groups=defaultdict(list)
        for row in rows:groups[(row.get('model') or '미확인',row.get('service_tier') or '미확인')].append(row)
        efforts=sorted({r.get('effort') or '미확인' for r in history if r['ts']<=end},key=effort_key)
        specs=list(METRICS)+[('reasoning:'+e,'평균 추론 토큰 / '+e,'reasoning') for e in efforts]+[('input','평균 입력 토큰','input'),('count','호출 수','count')]
        panels=[]
        for metric,title,unit in specs:
            lines=[];samples=[];bounds=[];n=0;N=0
            if metric=='duration':title='호출 소요시간 중앙값'
            for (model,mode),group in groups.items():
                own=[r for r in group if not metric.startswith('reasoning:') or (r.get('effort') or '미확인')==metric.split(':',1)[1]]
                if not own:continue
                times=[r['ts'] for r in own]
                population,total,_=metric_population(own,metric)
                eligible=len(own) if metric=='count' else len(population)
                n+=eligible;N+=total;bounds.extend(population)
                samples.extend(bounded_observations(own,metric,start,end,width))
                points=[]
                # Counts are disjoint bins; they are never added across overlapping windows.
                intervals=[(start+i*window,min(end,start+(i+1)*window)) for i in range(ceil(span/window))] if metric=='count' else None
                for index,stamp in enumerate(centers if intervals is None else [(a+b)/2 for a,b in intervals]):
                    a,b=(max(start,stamp-window/2),min(end,stamp+window/2)) if intervals is None else intervals[index]
                    right=bisect_right(times,b) if b==end else bisect_left(times,b)
                    items=own[bisect_left(times,a):right]
                    values,total,totals=metric_population(items,metric)
                    stats=distribution_statistics(values);mean=representative_mean(values,totals,metric)
                    eligible=len(items) if metric=='count' else len(values)
                    # A populated broad window must not bridge an empty local neighborhood.
                    support_left=bisect_left(times,max(start,stamp-window/6))
                    support_right=bisect_right(times,min(end,stamp+window/6))
                    local=own[support_left:support_right]
                    local_values,_,_=metric_population(local,metric)
                    support=metric=='count' or bool(local_values)
                    representative=stats['median'] if metric=='duration' else mean
                    minimum=MIN_CENTER if metric=='duration' else 1
                    value=representative if metric=='count' or support and eligible>=minimum else None
                    complete=a==stamp-window/2 and b==stamp+window/2 and b<=now if intervals is None else b-a==window and b<=now
                    status='기록 공백' if not support else '표본 부족' if eligible<minimum and metric!='count' else '일부 구간' if not complete else '이동 계산 구간'
                    points.append(dict(kind='rolling' if intervals is None else 'trend',ts=stamp,value=value,mean=mean,
                        representative=representative,statistic='median' if metric=='duration' else 'time_weighted_mean' if metric=='output_speed' else 'count' if metric=='count' else 'mean',
                        bucket=index,start=a,end=b,complete=complete,model=model,service_tier=mode,
                        effort=metric.split(':',1)[1] if metric.startswith('reasoning:') else '',coverage=(eligible,total),
                        distribution=stats,totals=totals,calls=len(items),sessions=len({(r['home'],r['sid']) for r in items}),
                        band_valid=metric!='count' and support and eligible>=MIN_BAND,outer_valid=metric!='count' and support and eligible>=MIN_OUTER_BAND,
                        support=support,baseline=None,delta=None,baseline_count=0,direction=0,status=status,
                        previous_start=None,previous_end=None))
                lines.append(dict(model=model,service_tier=mode,points=points,coverage=(len(own) if metric=='count' else len(population),
                    len(own) if metric=='count' else len({(r['home'],r['sid']) for r in own}) if metric=='session_cache' else len(own))))
            panels.append(dict(key=metric,title=title,unit=unit,n=n,N=N,points=sorted(samples,key=lambda p:p['ts']),
                low=min(bounds,default=0),high=max(bounds,default=1),lines=lines,rolling=True,window_seconds=window,
                raw_visible=span<=3600 and metric not in ('session_cache','count')))
        # No model calls are triggered. Idle refresh is bounded, including a partial live window.
        self.deadline=now+max(1,min(60,step))
        self.view=dict(panels=panels,start=start,end=end,full=full,granularity='auto',window_seconds=window,
            models=sorted({r.get('model') or '미확인' for r in history if r['ts']<=end}),calls=len(rows),valid_until=self.deadline)
