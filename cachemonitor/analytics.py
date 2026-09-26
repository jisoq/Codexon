"""Canonical observed populations shared by the dashboard and session overlay."""
from __future__ import annotations

from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone
import math
import ntpath
import hashlib
from .core import METRICS, LINEAGE_FIELDS, summarize, token_number, token_parts
from .pricing import COST_KEYS, COST_COMPONENTS, token_cost, sum_cost, request_tier, display_tier, unknown_mode_calls, mode_assumptions
from .cache_misses import classify
from .cache_health import CacheHealth
from .workload import internal_review

EFFORTS=('none','minimal','low','medium','high','xhigh','max','ultra')
KEYS=('total','input','cached','written','ordinary_input','output','reasoning','uncached','non_reasoning','duration','generation_wait','rate')
INPUT_BANDS=((0,10000,'10k 미만'),(10000,50000,'10–50k'),(50000,100000,'50–100k'),
             (100000,200000,'100–200k'),(200000,272001,'200–272k'),(272001,float('inf'),'272k 초과'))
CACHE_BANDS=(('zero','0%'),((0,25),'0% 초과–25% 미만'),((25,50),'25–50% 미만'),
             ((50,75),'50–75% 미만'),((75,100),'75–100% 미만'),('full','100%'))
COMPONENT_LABELS=('일반 입력','캐시 읽기','캐시 쓰기','비캐시 입력 미분류','출력·추론 포함')


def effort_key(value):
    return (0,EFFORTS.index(value),'') if value in EFFORTS else (2,0,'') if value in ('미확인',None,'') else (1,0,value)


def project_key(path):
    from .platform_paths import path_identity
    return path_identity(path) if path else '미확인'


def project_identity(session):
    return session.get('project') or project_key(session.get('cwd',''))


def project_matches(session, selected):
    return project_identity(session)==selected or project_key(session.get('cwd',''))==project_key(selected)


def project_choices(sessions):
    labels={};paths=defaultdict(set)
    for session in sessions:
        key=project_identity(session)
        labels[key]=session.get('project_name') or ntpath.basename(session.get('cwd','').rstrip('/\\')) or '프로젝트 없는 작업'
        if session.get('cwd'):paths[key].add(session['cwd'])
    duplicates=Counter(labels.values())
    labels={key:name if duplicates[name]==1 else f'{name} · {hashlib.sha256(key.encode()).hexdigest()[:6]}'
            for key,name in labels.items()}
    return {'projects':sorted(labels,key=lambda key:(labels[key].casefold(),key)),
            'project_labels':labels,'project_paths':{key:sorted(value) for key,value in paths.items()}}


def call_identity(home,sid,key):
    return (home,sid,key) if str(key).startswith(('record:','legacy:')) else (home,key)


def quantile(values,q):
    return sorted_quantile(sorted(values),q)

def sorted_quantile(values,q):
    if not values:return None
    position=(len(values)-1)*q;index=int(position)
    return values[index]+(values[min(index+1,len(values)-1)]-values[index])*(position-index)


def finite(value):
    return type(value) in (int,float) and math.isfinite(value) and value>=0


def output_speed(row):
    """Observed output (including reasoning) per end-to-end response second."""
    output=token_number(row.get('output'));elapsed=row.get('completion_latency_ms')
    if (row.get('response_status')!='completed' or not row.get('timing_valid',True)
            or row.get('observation_missing') or row.get('model_conflict')
            or row.get('output_conflict') or row.get('transport_source')=='conflict'
            or output is None or not finite(elapsed) or elapsed<=0):return None
    reasoning=token_number(row.get('reasoning'))
    if reasoning is not None and reasoning>output:return None
    seconds=elapsed/1000
    if seconds<=0:return None
    value=output/seconds
    return value if finite(value) else None


def output_speed_summary(rows):
    """Time-weighted call speed, not wall-clock throughput of parallel calls."""
    rows=list(rows);valid=[r for r in rows if output_speed(r) is not None]
    output=sum(r['output'] for r in valid)
    seconds=sum(r['completion_latency_ms']/1000 for r in valid)
    return dict(value=output/seconds if seconds else None,output=output,seconds=seconds,
                n=len(valid),N=len(rows),missing=len(rows)-len(valid))


def cache_rows(rows):
    return [r for r in rows if token_number(r.get('input')) is not None and token_number(r.get('cached')) is not None
            and r['cached']<=r['input']]


def observation_flags(row):
    evidence=' '.join(str(row.get(k) or '') for k in ('model_match','model_evidence'))
    return {'missing':bool(row.get('observation_missing') or '누락' in evidence or row.get('input') is None or row.get('output') is None),
            'conflict':bool(any(row.get(k) for k in ('model_conflict','mode_conflict','cache_policy_conflict','input_conflict','output_conflict'))
                            or row.get('transport_source')=='conflict' or '충돌' in evidence)}


def annotate_incidents(rows,health):
    incidents={key:event['id'] for event in health['events'] for key in event['occurrence_keys']}
    return [r if 'cache_incident_id' in r and r['cache_incident_id']==incidents.get(r['key']) else
            dict(r,cache_incident_id=incidents.get(r['key']),cache_degradation=r['key'] in incidents) for r in rows]


def population(rows,key,method='mean'):
    rows=list(rows)
    key={'cache_ratio':'rate','elapsed':'duration','call_count':'responses'}.get(key,key)
    samples=[r for r in rows if finite(r.get(key))]
    values=[r[key] for r in samples]
    total=sum(values) if values or not rows else None
    average=total/len(values) if values else None
    ordered=sorted(values)
    percentiles={name:sorted_quantile(ordered,q) for name,q in (('p10',.1),('q1',.25),('median',.5),('q3',.75),('p90',.9))}
    value=percentiles['median'] if method=='median' else percentiles['p90'] if method=='p90' and len(values)>=10 else None if method=='p90' else average
    result=dict(value=value,mean=average,sum=total,n=len(values),N=len(rows),distribution_n=len(values),missing=len(rows)-len(values),
                partial=0<len(values)<len(rows),points=values if len(values)<10 else [],
                minimum=min(values) if values else None,maximum=max(values) if values else None,
                p90_available=len(values)>=10,**percentiles)
    if key=='rate':
        pairs=cache_rows(rows);inp=sum(r['input'] for r in pairs);read=sum(r['cached'] for r in pairs)
        result.update(value=100*read/inp if inp else None,weighted=100*read/inp if inp else None,
                      input=inp,cached=read,n=len(pairs),missing=len(rows)-len(pairs),partial=0<len(pairs)<len(rows))
    result['unknown_mode_calls']=sum(unknown_mode_calls(r) for r in rows)
    result['unknown_mode_samples']=sum(bool(unknown_mode_calls(r)) for r in samples)
    return result


def stats(rows,key,method='mean'):
    return population(rows,key,method)


def prepare_row(row,session,price=None):
    """Bind original identity and historical classifications without price aliases."""
    r=dict(row)
    configured=(r.get('configured_model') if 'configured_model' in r else r.get('model')) or ''
    model='' if r.get('model_conflict') else r.get('requested_model') or configured
    r.update(home=session['home'],sid=session['id'],title=session['title'],
             model=model,analysis_model=model,configured_model=configured,
             model_source='conflict' if r.get('model_conflict') else 'wire' if r.get('requested_model') else 'settings' if configured else 'unknown',
             project=project_identity(session),project_name=session.get('project_name') or ntpath.basename(session.get('cwd','').rstrip('/\\')) or '프로젝트 없는 작업',
             cwd=session.get('cwd',''),source=session.get('source','unknown'),
             call_id=r.get('key'),request_id=r.get('turn') or '',
             service_tier=request_tier(r),state=session.get('turn_states',{}).get(r.get('turn'),'미확인'))
    r.update({key:session[key] for key in LINEAGE_FIELDS if session.get(key) is not None})
    for key in METRICS:r[key]=token_number(r.get(key))
    i,c,w,o,q=(r.get(k) for k in ('input','cached','written','output','reasoning'))
    if i is not None and c is not None and (c>i or w is not None and c+w>i):r['input_conflict']=True
    if o is not None and q is not None and q>o:r['output_conflict']=True;r['reasoning']=None;q=None
    r['total']=i+o if i is not None and o is not None else r.get('total')
    r['rate']=100*c/i if i and c is not None and c<=i else None
    r['uncached']=i-c if i is not None and c is not None and c<=i else None
    r['ordinary_input']=i-c-w if all(v is not None for v in (i,c,w)) and not r.get('input_conflict') else None
    r['non_reasoning']=o-q if o is not None and q is not None else None
    timing=(r.get('timing_valid',r.get('response_status')=='completed') and not r.get('observation_missing')
            and not r.get('model_conflict'))
    r['duration']=r['completion_latency_ms']/1000 if timing and finite(r.get('completion_latency_ms')) else None
    r['generation_wait']=r['generation_latency_ms']/1000 if timing and finite(r.get('generation_latency_ms')) else None
    r['output_speed']=output_speed(r)
    r.update(token_cost(r) if price is None else price)
    return r


def session_summary(rows):
    rows=list(rows);priced=[r for r in rows if r.get('cost') is not None]
    cost=population(rows,'cost');cache=population(rows,'rate')
    composition={}
    for side,keys in (('input',('ordinary','read','write','unclassified')),('output',('ordinary','reasoning','unclassified'))):
        parts=[token_parts(r)[side] for r in rows];known=[p for p in parts if p['total'] is not None]
        total=sum(p['total'] for p in known) if known or not rows else None
        composition[side+'_parts']={k:sum(p[k] for p in known) for k in keys}
        composition[side+'_parts'].update(total=total,n=len(known),N=len(rows),missing=len(rows)-len(known))
    components=[dict(key=k,label=label,total=sum(r[k] for r in priced) if priced or not rows else None,
                     share=sum(r[k] for r in priced)/cost['sum'] if cost['sum'] else None,n=len(priced),N=len(rows))
                for k,label in zip(COST_COMPONENTS,COMPONENT_LABELS)]
    return dict(calls=len(rows),cost=cost,cache=cache,output_speed=output_speed_summary(rows),components=components,**composition)


def condition_match(row,conditions,unit='response'):
    first=row.get('first_call') if unit in ('turn','request') else row
    first=first or {}
    band=conditions.get('input_band')
    if band and not (token_number(first.get('input')) is not None and band[0]<=first['input']<band[1]):return False
    band=conditions.get('cache_band')
    if band is not None:
        value=first.get('rate')
        if value is None:return False
        if band=='zero' and value!=0:return False
        if band=='full' and value!=100:return False
        if isinstance(band,(list,tuple)) and not (band[0]<=value<band[1] and (band[0]!=0 or value>0)):return False
    calls=row.get('calls',[]) if unit in ('turn','request') else [row]
    for key in ('transport','transport_source','cache_policy'):
        chosen=conditions.get(key)
        if chosen and any((r.get(key) or '미확인')!=chosen for r in calls):return False
    return True


def request_record(key,items,whole,session,start,end):
    metadata=session.get('turn_records',{}).get(key[2],{})
    state=metadata.get('state') or session.get('turn_states',{}).get(key[2],'미확인')
    started,ended=metadata.get('started_at'),metadata.get('ended_at')
    reasons=[]
    if state!='완료':reasons.append({'진행':'진행 중','중단':'중단'}.get(state,'종료 미확인'))
    if started is None:reasons.append('시작 기록 없음')
    if ended is None:reasons.append('완료 기록 없음')
    if started is not None and ended is not None:
        if ended<started:reasons.append('시간 관측 충돌')
        if not(start<=started and ended<end):reasons.append('기간 일부')
    if len(items)!=len(whole):
        reasons.append('기간 일부' if any(not(start<=r['ts']<end) for r in whole) else '조건 일부')
    if not session.get('collection_complete',not session.get('pending',False)):reasons.append('수집 중')
    if started is not None and ended is not None and any(g['start']<=ended and g['end']>=started for g in session.get('coverage_gaps',[])):
        reasons.append('기록 불완전')
    dimensions={}
    for dim in ('model','effort','service_tier'):
        values={r.get(dim) or '미확인' for r in whole};dimensions[dim]=next(iter(values)) if len(values)==1 else '혼합'
    values=summarize(items)
    for metric in METRICS:
        if values['missing'][metric]:values[metric]=None
    cost=sum_cost(items,strict=True)
    if not whole:
        cost=dict.fromkeys(COST_KEYS)
        for metric in METRICS:values[metric]=None
    first=min(whole,key=lambda r:r['ts']) if whole and started is not None else None
    complete=not reasons
    result={**values,**cost,**dimensions,'home':key[0],'sid':key[1],'turn':key[2],'request_id':key[2],
            'title':session['title'],'project':project_identity(session),'project_name':session.get('project_name') or ntpath.basename(session.get('cwd','').rstrip('/\\')) or '프로젝트 없는 작업','cwd':session.get('cwd',''),
            'source':session.get('source','unknown'),'ts':ended if ended is not None else max((r['ts'] for r in items),default=started or 0),
            'started_at':started,'ended_at':ended,'responses':len(items),'total_responses':len(whole),'calls':items,'first_call':first,
            'start_input':first.get('input') if first else None,'start_rate':first.get('rate') if first else None,
            'duration':ended-started if complete and started is not None and ended is not None else None,
            'state':state if complete or state!='완료' else reasons[0], 'complete':complete,'exclusions':list(dict.fromkeys(reasons)),
            'unknown_mode_calls':sum(unknown_mode_calls(r) for r in items),
            'display_service_tier':dimensions['service_tier'],'long_context':any(r.get('long_context') for r in items),
            'price_issue':'포함 호출 중 환산액 미산정' if cost['cost'] is None else '',
            'call_mean':cost['cost']/len(items) if cost['cost'] is not None and items else None}
    valid=cache_rows(items)
    result['rate']=100*sum(r['cached'] for r in items)/sum(r['input'] for r in items) if len(valid)==len(items) and sum(r['input'] for r in valid)>0 else None
    return result


def analyze(sessions,start=0,end=float('inf'),model='',source='',archived=True,
            unit='response',method='mean',input_band=None,with_comparisons=True,service_tier='',effort='',home='',project='',sid=''):
    selected=[s for s in sessions if not internal_review(s) and (archived or not s.get('archived')) and (not source or s.get('source','unknown')==source)
              and (not home or s['home']==home) and (not sid or s['id']==sid) and (not project or project_matches(s,project))]
    responses=[];whole_turns=defaultdict(list);groups=defaultdict(list);session_map={}
    seen=set()
    for index,s in enumerate(selected):
        if not s.get('_prepared'):
            misses=classify(s['history']);miss_keys={e['key'] for e in misses['events']}
            prepared=[dict(prepare_row(r,s),session_ordinal=i+1,cache_miss=r.get('key') in miss_keys)
                      for i,r in enumerate(sorted(s['history'],key=lambda row:row['ts']))]
            health=CacheHealth().update(prepared)
            s={**s,'history':annotate_incidents(prepared,health),'cache_health':health,'cache_misses':misses,'_prepared':True}
            selected[index]=s
        session_map[(s['home'],s['id'])]=s
        for ordinal,r in enumerate(sorted(s['history'],key=lambda row:row['ts']),1):
            identity=call_identity(s['home'],s['id'],r.get('key'))
            if identity in seen:continue
            seen.add(identity)
            row=r
            if row.get('turn'):whole_turns[(s['home'],s['id'],row['turn'])].append(row)
            if start<=row['ts']<end and (not model or (row.get('model') or '미확인')==model) and (not effort or (row.get('effort') or '미확인')==effort) and (not service_tier or display_tier(row)==service_tier):
                responses.append(row)
                if row.get('turn'):groups[(s['home'],s['id'],row['turn'])].append(row)
    # Boundary-only and zero-call requests remain visible and contribute to exclusions.
    for sk,s in session_map.items():
        for turn,meta in s.get('turn_records',{}).items():
            a,b=meta.get('started_at'),meta.get('ended_at')
            if any(value is not None and start<=value<end for value in (a,b)) and not any((model,effort,service_tier)):
                groups.setdefault((*sk,turn),[])
    turns=[request_record(key,items,whole_turns.get(key,[]),session_map[key[:2]],start,end) for key,items in groups.items()]
    units=responses if unit in ('response','call') else [t for t in turns if t['complete']]
    if input_band:units=[r for r in units if condition_match(r,{'input_band':input_band},unit)]
    buckets=defaultdict(list)
    for r in units if with_comparisons else ():
        buckets[(r.get('model') or '미확인',r.get('effort') or '미확인',display_tier(r))].append(r)
    comparisons=[dict(model=m,effort=e,service_tier=t,n=len(items),unknown_mode_calls=sum(unknown_mode_calls(r) for r in items),
        response_count=sum(r.get('responses',1) for r in items),turn_count=len({(r['home'],r['sid'],r['turn']) for r in items if r.get('turn')}),
        total=summarize(items)['total'],cost=sum_cost(items)['cost'],stats={k:stats(items,k,method) for k in (*KEYS,*COST_KEYS)},items=items)
        for (m,e,t),items in buckets.items()]
    comparisons.sort(key=lambda g:(g['model'],effort_key(g['effort']),g['service_tier']))
    paired=defaultdict(list)
    for t in turns if with_comparisons else ():
        if t['complete'] and t['cost'] is not None and (not input_band or condition_match(t,{'input_band':input_band},'turn')):
            paired[(t['model'],t['effort'],display_tier(t))].append(t)
    basis=[]
    for (m,e,tier),ts in paired.items():
        calls=[r for t in ts for r in t['calls']]
        basis.append(dict(model=m,effort=e,service_tier=tier,turns=len(ts),calls=len(calls),total_cost=sum(t['cost'] for t in ts),
                          unknown_mode_calls=sum(unknown_mode_calls(r) for r in calls),call_cost=stats(calls,'cost',method)['value'],turn_cost=stats(ts,'cost',method)['value']))
    basis.sort(key=lambda r:(r['model'],effort_key(r['effort']),r['service_tier']))
    unclassified=[s['unclassified'] for s in selected if s.get('unclassified')]
    return dict(sessions=selected,responses=responses,turns=turns,units=units,groups=comparisons,
                totals={**summarize(units),**sum_cost(units)},basis=basis,
                priced_count=sum(r['cost'] is not None for r in units),unpriced_count=sum(r['cost'] is None for r in units),
                missing_turn_responses=sum(not r.get('turn') for r in responses),excluded_turns=sum(not t['complete'] for t in turns),
                turn_count=len(turns),unclassified=summarize(unclassified),unclassified_sessions=len(unclassified))


def _bucket_start(ts,unit):
    dt=datetime.fromtimestamp(ts)
    if unit=='5min':return dt.replace(minute=dt.minute//5*5,second=0,microsecond=0)
    if unit=='hour':return dt.replace(minute=0,second=0,microsecond=0)
    dt=dt.replace(hour=0,minute=0,second=0,microsecond=0)
    if unit=='week':return dt-timedelta(days=dt.weekday())
    if unit=='month':return dt.replace(day=1)
    return dt


def _next_bucket(dt,unit):
    if unit=='month':return dt.replace(year=dt.year+(dt.month==12),month=dt.month%12+1,day=1)
    return dt+{'5min':timedelta(minutes=5),'hour':timedelta(hours=1),'day':timedelta(days=1),'week':timedelta(days=7)}[unit]


def _local_timestamp(value):
    try:return value.timestamp()
    except (OSError,ValueError):
        # Windows CRT cannot mktime some synthetic dates at the Unix epoch.
        offset=datetime.fromtimestamp(86400).astimezone().utcoffset()
        return (value-datetime(1970,1,1)-offset).total_seconds()


def overview_view(analysis,start,end,metric='cost',granularity='auto',basis='total',group_by='project',statistic=stats,summarizer=session_summary):
    rows=analysis['responses'];turns=analysis['turns'];completed=[t for t in turns if t['complete']]
    summary=summarizer(rows);cost=summary['cost'];cache=summary['cache']
    first=start or (min(r['ts'] for r in rows) if rows else None)
    if not math.isfinite(end):end=max((r['ts'] for r in rows),default=first or 0)+.000001
    seconds=end-first if first is not None else 0
    bucket_unit=granularity if granularity in ('5min','hour','day','week','month') else '5min' if seconds<=7200 else 'hour' if seconds<=172800 else 'day' if seconds<=62*86400 else 'week' if seconds<=366*86400 else 'month'
    buckets=defaultdict(list);request_buckets=defaultdict(list)
    for row in rows:buckets[_local_timestamp(_bucket_start(row['ts'],bucket_unit))].append(row)
    for row in completed:request_buckets[_local_timestamp(_bucket_start(row['ended_at'],bucket_unit))].append(row)
    timeline=[];cursor=_local_timestamp(_bucket_start(first,bucket_unit)) if first is not None and end>first else None
    while cursor is not None and cursor<end:
        dt=datetime.fromtimestamp(cursor)
        following=cursor+(300 if bucket_unit=='5min' else 3600) if bucket_unit in ('5min','hour') else _local_timestamp(_next_bucket(dt,bucket_unit))
        lo=max(first,cursor);hi=min(end,following)
        calls=buckets[cursor];requests=request_buckets[cursor]
        call_stats=statistic(calls,'cost');turn_stats=statistic(requests,'cost');cache_stats=statistic(calls,'rate')
        active=({'n':len(calls),'N':len(calls),'missing':0,'partial':False} if metric=='count' else
                turn_stats if basis=='turn_mean' and metric=='cost' else cache_stats if metric in ('cache_ratio','rate') else call_stats)
        value=len(calls) if metric=='count' else cache_stats['value'] if metric in ('cache_ratio','rate') else active['mean'] if basis in ('call_mean','turn_mean') else call_stats['sum']
        label=dt.strftime('%m/%d %H:%M' if bucket_unit in ('5min','hour') else '%Y/%m' if bucket_unit=='month' else '%m/%d')
        timeline.append(dict(label=label,start=datetime.fromtimestamp(lo).isoformat(),end=datetime.fromtimestamp(hi).isoformat(),
            start_ts=lo,end_ts=hi,total=call_stats['sum'],value=value,calls=len(calls),known=active['n'],n=active['n'],N=active['N'],missing=active['missing'],
            partial=lo>cursor or hi<following,partial_sum=active['partial'],
            call_stats=call_stats,turn_stats=turn_stats,cache_stats=cache_stats,records=requests if basis=='turn_mean' and metric=='cost' else calls))
        cursor=following
    def aggregate(items):
        st=statistic(items,'cost');return dict(total=st['sum'],calls=len(items),known=st['n'],missing=st['missing'],partial=st['partial'],
            share=st['sum']/cost['sum'] if st['sum'] is not None and cost['sum'] else None,call_stats=st,records=items)
    groupings={dim:defaultdict(list) for dim in ('project','model','source','session')}
    for r in rows:
        for dim in groupings:
            identity=(r['home'],r['sid']) if dim=='session' else r.get(dim) or '미확인'
            groupings[dim][identity].append(r)
    completed_by_session=defaultdict(list)
    for t in completed:completed_by_session[(t['home'],t['sid'])].append(t)
    results={};project_labels=project_choices(analysis['sessions'])['project_labels']
    for dim,buckets_for_dim in groupings.items():
        entries=[]
        for key,items in buckets_for_dim.items():
            label=items[0]['title'] if dim=='session' else project_labels.get(key,str(key)) if dim=='project' else str(key)
            item=dict(key=key,label=label,dimension=dim,**aggregate(items))
            if dim=='model':item['model']=key
            if dim=='session':
                subset=completed_by_session[key]
                item.update(home=key[0],sid=key[1],title=label,turn=None,completed=len(subset),turn_stats=statistic(subset,'cost'),ts=max(r['ts'] for r in items))
            entries.append(item)
        entries.sort(key=lambda r:(r['total'] is None,-(r['total'] or 0),r['label']))
        results[dim]=entries
    sources=results.get(group_by,results['project']);unknown=[r for r in sources if r['key'] in ('미확인','unknown','')]
    identified=[r for r in sources if r not in unknown]
    if len(identified)>8:
        others=identified[8:];items=[r for group in others for r in group['records']]
        identified=identified[:8]+[dict(key='other',label=f'기타 {len(others)}개',dimension=group_by,**aggregate(items))]
    sources=identified+unknown
    selected_ids={(r['home'],r['sid'],r['key']) for r in rows}
    events=[]
    for s in analysis['sessions']:
        for event in s.get('cache_health',{}).get('events',[]):
            if any((s['home'],s['id'],key) in selected_ids for key in event['occurrence_keys']):events.append(dict(event,home=s['home'],sid=s['id']))
    attention={'unpriced':[r for r in rows if r['cost'] is None],
        'model_mismatch':[r for r in rows if r.get('model_alert_confirmed')],
        'cache_degradation':events,'observation_missing':[r for r in rows if observation_flags(r)['missing']]}
    turn_stats=statistic(completed,'cost')
    days=(datetime.fromtimestamp(end-.000001).date()-datetime.fromtimestamp(first).date()).days+1 if first is not None and end>first else 0
    return dict(total=cost['sum'],known=cost['n'],missing=cost['missing'],calls=len(rows),call_stats=cost,turn_stats=turn_stats,
        completed=len(completed),request_calls=sum(t['responses'] for t in completed if t['cost'] is not None),
        request_exclusions=dict(Counter(reason for t in turns for reason in t['exclusions'])),
        cache_ratio=cache['value']/100 if cache['value'] is not None else None,cache_known=cache['n'],cache_input=cache['input'],
        summary=summary,components=summary['components'],sources=sources,models=results['model'],sessions=results['session'],
        session_count=len(groupings['session']),timeline=timeline,granularity=bucket_unit,basis=basis,metric=metric,
        days=days,active_days=len({datetime.fromtimestamp(r['ts']).date() for r in rows}),daily=cost['sum']/days if cost['sum'] is not None and days else None,
        start=datetime.fromtimestamp(first).isoformat() if first is not None else None,end=datetime.fromtimestamp(end).isoformat(),
        attention=attention,assumptions=mode_assumptions(rows))


def observed_choices(rows):
    combos=Counter((r.get('model') or '미확인',r.get('effort') or '미확인',display_tier(r)) for r in rows)
    return dict(models=sorted({k[0] for k in combos}),efforts=sorted({k[1] for k in combos},key=effort_key),
                modes=sorted({k[2] for k in combos}),combinations=[dict(model=m,effort=e,service_tier=t,count=n) for (m,e,t),n in sorted(combos.items())])


def initial_targets(rows):
    pairs=defaultdict(lambda:defaultdict(list))
    for r in rows:pairs[(r.get('model') or '미확인',r.get('effort') or '미확인')][display_tier(r)].append(r)
    choices=[(min(len(modes['Standard']),len(modes['Fast'])),max(r['ts'] for mode in ('Standard','Fast') for r in modes[mode]),m,e)
             for (m,e),modes in pairs.items() if modes.get('Standard') and modes.get('Fast')]
    if choices:
        _,_,model,effort=max(choices)
        return [dict(id=label,model=model,effort=effort,service_tier=mode) for label,mode in zip('AB',('Standard','Fast'))]
    alternatives=defaultdict(list)
    for r in rows:alternatives[(r.get('model') or '미확인',display_tier(r))].append(r)
    candidates=[(len(rs),max(r['ts'] for r in rs),model,mode,sorted({r.get('effort') or '미확인' for r in rs},key=effort_key))
                for (model,mode),rs in alternatives.items() if len({r.get('effort') or '미확인' for r in rs})>=2]
    if candidates:
        _,_,model,mode,efforts=max(candidates)
        return [dict(id=label,model=model,effort=effort,service_tier=mode) for label,effort in zip('ABCDEFGH',efforts[:8])]
    return []


def _target_key(row):
    return (row.get('model') or '미확인',row.get('effort') or '미확인',display_tier(row))


def comparison_view(analysis,metric='cost',input_band=None,stat_fn=stats,targets=None,baseline='A',
                    unit='response',method='mean',conditions=None,matrix_by='input',comparison_type=''):
    calls=analysis['responses'];completed=[t for t in analysis['turns'] if t['complete']]
    conditions=dict(conditions or {})
    if input_band:conditions['input_band']=input_band
    # An explicit empty editor must stay empty on refresh.
    targets=initial_targets(calls) if targets is None else list(targets)
    unique=set();clean=[]
    for i,target in enumerate(targets if comparison_type in ('effort','mode') else targets[:8]):
        target=dict(target);key=_target_key(target)
        if key in unique:continue
        unique.add(key);target.update(id=target.get('id') or chr(65+i),model=key[0],effort=key[1],service_tier=key[2]);clean.append(target)
    targets=clean
    metric={'cache_ratio':'rate','elapsed':'duration','call_count':'responses'}.get(metric,metric)
    sample_rows=calls if unit in ('response','call') else completed
    groups=[];matrix=[]
    for target in targets:
        key=_target_key(target)
        candidates=[r for r in sample_rows if _target_key(r)==key]
        rows=[r for r in candidates if condition_match(r,conditions,unit)]
        st=stat_fn(rows,metric,method)
        priced=[r for r in rows if r.get('cost') is not None]
        mean_budget={k:sum(r[k] for r in priced)/len(priced) if priced else None for k in COST_COMPONENTS}
        mean_budget.update(cost=sum(r['cost'] for r in priced)/len(priced) if priced else None,n=len(priced),N=len(rows),missing=len(rows)-len(priced),calls=len(rows))
        scatter=[r for r in rows if r.get('cost') is not None and r.get('duration') is not None]
        axis_filter='input_band' if matrix_by=='input' else 'cache_band' if matrix_by=='cache' else None
        conditions_without_band={k:v for k,v in conditions.items() if k!=axis_filter}
        matrix_rows=[r for r in candidates if condition_match(r,conditions_without_band,unit)]
        if matrix_by=='cache':cells=[(label,{'cache_band':band}) for band,label in CACHE_BANDS]
        elif matrix_by=='project':
            choices=project_choices(sample_rows)
            cells=[(choices['project_labels'][value],{'project':value}) for value in choices['projects']]
        elif matrix_by=='source':cells=[(value,{'source':value}) for value in sorted({r.get('source') or '미확인' for r in sample_rows})]
        else:cells=[(label,{'input_band':(lo,hi)}) for lo,hi,label in INPUT_BANDS]
        bands=[]
        for label,condition in cells:
            rs=[r for r in matrix_rows if condition_match(r,condition,unit) and all(r.get(k)==v for k,v in condition.items() if k in ('source','project'))]
            cell=dict(stat_fn(rs,metric,method),label=label,condition=condition,records=rs,cache=stats(rs,'rate')['value'])
            if 'input_band' in condition:cell['band']=condition['input_band']
            bands.append(cell)
        paired=[r for t in priced for r in t['calls']] if unit not in ('response','call') else []
        decomposition=dict(requests=len(priced),calls=len(paired),request_mean=sum(t['cost'] for t in priced)/len(priced) if priced else None,
            call_mean=sum(r['cost'] for r in paired)/len(paired) if paired else None,calls_per_request=len(paired)/len(priced) if priced else None) if unit not in ('response','call') else None
        call_rows=[r for r in calls if _target_key(r)==key and condition_match(r,conditions,'response')]
        turn_rows=[r for r in completed if _target_key(r)==key and condition_match(r,conditions,'turn')]
        group=dict(target,group=key,stats=st,value=st['value'],n=st['n'],N=st['N'],missing=st['missing'],points=st['points'],rows=rows,
            call=stat_fn(call_rows,metric,method),turn=stat_fn(turn_rows,metric,method),
            condition_excluded=len(candidates)-len(rows),metric_excluded=st['missing'],
            exclusions={'조건 미충족':len(candidates)-len(rows),'지표 미확인':st['missing']},
            scatter=scatter,scatter_stats={'cost':stats(scatter,'cost'),'duration':stats(scatter,'duration'),'n':len(scatter),'N':len(rows)},
            bands=bands,mean_budget=mean_budget,decomposition=decomposition,
            budget={**sum_cost(call_rows), 'calls':len(call_rows),'n':sum(r['cost'] is not None for r in call_rows),'missing':sum(r['cost'] is None for r in call_rows)},
            reasoning_tokens=stats(rows,'reasoning'),unknown_mode_calls=sum(unknown_mode_calls(r) for r in rows))
        related=[t for t in analysis['turns'] if any(_target_key(r)==key for r in t['calls'])]
        group['request_exclusions']=dict(Counter(reason for t in related for reason in (
            t['exclusions'] if not t['complete'] else ['혼합 요청'] if _target_key(t)!=key else [])))
        groups.append(group);matrix.append({'id':target['id'],'cells':bands})
    reference=next((g for g in groups if g['id']==baseline),groups[0] if groups else None)
    baseline=reference['id'] if reference else baseline
    for group in groups:
        a=reference['value'] if reference else None;b=group['value']
        group['delta']=b-a if a is not None and b is not None else None
        group['relative']=100*(b-a)/a if a is not None and a!=0 and b is not None else None
        group['baseline']=group['id']==baseline
    reprice=None;repricing_groups=[]
    mode_comparison=len(targets)==2 and len({(t['model'],t['effort']) for t in targets})==1 and {t['service_tier'] for t in targets}=={'Fast','Standard'}
    pairs={}
    for target in targets:
        if target['service_tier'] in ('Standard','Fast'):
            pairs.setdefault((target['model'],target['effort']),{})[target['service_tier']]=target
    if mode_comparison or comparison_type=='mode':
        for pair in pairs.values():
            if set(pair)!={'Standard','Fast'}:continue
            pair_keys={_target_key(t) for t in pair.values()}
            eligible=[r for r in sample_rows if (all(_target_key(call) in pair_keys for call in r['calls']) and bool(r['calls'])
                if unit not in ('response','call') else _target_key(r) in pair_keys) and condition_match(r,conditions,unit)]
            paired=[]
            for sample in eligible:
                members=sample['calls'] if unit not in ('response','call') else [sample]
                values={mode:[token_cost(dict(r,service_tier=mode,mode_conflict=False,service_tier_source='scenario'))['cost'] for r in members] for mode in ('Standard','Fast')}
                if all(v is not None for vs in values.values() for v in vs):
                    paired.append(dict(record=sample,**{mode:sum(vs) for mode,vs in values.items()}))
            estimate=dict(n=len(paired),N=len(eligible),missing=len(eligible)-len(paired),records=paired,
                Standard=stats([{'cost':r['Standard']} for r in paired],'cost',method),Fast=stats([{'cost':r['Fast']} for r in paired],'cost',method))
            base_mode=next((t['service_tier'] for t in pair.values() if t['id']==baseline),'Standard')
            reference=estimate[base_mode]['value']
            for target in pair.values():
                result=estimate[target['service_tier']];value=result['value']
                result.update(id=target['id'],N=len(eligible),missing=len(eligible)-len(paired),
                    baseline=target['id']==baseline,delta=value-reference if value is not None and reference is not None else None,
                    relative=100*(value-reference)/reference if value is not None and reference not in (0,None) else None)
                repricing_groups.append({**result,**target,'stats':dict(result),'rows':[r['record'] for r in paired]})
            if mode_comparison:reprice=estimate
    return dict(targets=targets,groups=groups,baseline=baseline,metric=metric,unit=unit,method='weighted' if metric=='rate' else method,
        matrix=matrix,matrix_by=matrix_by,repricing=reprice,repricing_groups=repricing_groups,mode_comparison=mode_comparison,
        multiple_conditions=len({i for i in range(3) if len({g['group'][i] for g in groups})>1})>1,
        choices=observed_choices(calls),total=sum_cost(calls)['cost'],known=sum(r['cost'] is not None for r in calls),missing=sum(r['cost'] is None for r in calls),
        completed=len(completed),turn_known=sum(r['cost'] is not None for r in completed),
        states=dict(Counter(t['state'] for t in analysis['turns'] if not t['complete'])),
        mixed=sum(any(t[k]=='혼합' for k in ('model','effort','service_tier')) for t in completed),
        unknown_effort=sum(not r.get('effort') or r.get('effort')=='미확인' for r in calls),
        scatter=[dict(r,target=g['id']) for g in groups for r in g['scatter']])
