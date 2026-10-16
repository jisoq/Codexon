"""Worker-owned dashboard projections. Historical records never cross the view wire."""
from collections import Counter
from bisect import bisect_left
from heapq import merge, nsmallest, nlargest
from itertools import islice

from .analytics import cache_rows, observation_flags, stats
from .pricing import mode_assumptions, request_tier, sum_cost
from .analysis_records import available_fields
from .history_timeline_data import project_timeline


def identity(row):
    return (row.get('home'),row.get('sid'),row.get('call_id') or row.get('key') or row.get('response_id'))


def matches(row,key):
    if key=='unpriced':return row.get('cost') is None
    if key=='unknown_mode':return request_tier(row)=='미확인'
    if key=='model_mismatch':return bool(row.get('model_alert_confirmed')) or row.get('model_match')=='모델명 불일치'
    if key=='observation_missing':return observation_flags(row)['missing']
    if key=='observation_conflict':return observation_flags(row)['conflict']
    if key=='observation_problem':return matches(row,'observation_missing') or matches(row,'observation_conflict')
    if key=='cache_zero':return type(row.get('input')) is int and row['input']>0 and type(row.get('cached')) is int and row['cached']==0
    if key=='cache_degradation':return bool(row.get('cache_degradation') or row.get('cache_incident_id'))
    if key=='http':return row.get('transport')=='HTTP/SSE'
    return False


def ordered(rows,sort):
    key='cost' if sort.startswith('cost') else 'ts';descending=sort.endswith('desc')
    return sorted(rows,key=lambda r:(r.get(key) is None,-r[key] if descending and isinstance(r.get(key),(int,float)) else r.get(key,0)))


class _OrderedRows:
    """Select a viewport from existing session order or a bounded end selection."""
    def __init__(self,rows,sort,chunks=None):
        self.rows=rows;self.sort=sort;self.chunks=chunks
        field='cost' if sort.startswith('cost') else 'ts';descending=sort.endswith('desc')
        self.key=lambda r:(r.get(field) is None,-r[field] if descending and isinstance(r.get(field),(int,float)) else r.get(field,0))

    def window(self,request):
        total=len(self.rows);start=max(0,min(int(request.get('start',0)),max(0,total-1)))
        size=max(64,min(512,int(request.get('size',128))))
        anchor=request.get('anchor')
        if anchor:
            found=next(((i,row) for i,row in enumerate(self.rows) if list(row_key(row))==anchor),None)
            if found is not None:
                position,row=found;key=self.key(row)
                rank=sum(self.key(value)<key or (self.key(value)==key and i<position) for i,value in enumerate(self.rows))
                start=max(0,rank-int(request.get('anchor_offset',0)))
        if self.chunks is not None and self.sort.startswith('ts'):
            def newest(rows):
                end=len(rows)
                while end:
                    begin=end-1;at=rows[begin]['ts']
                    while begin and rows[begin-1]['ts']==at:begin-=1
                    for index in range(begin,end):yield rows[index]
                    end=begin
            sources=(newest(rows) if self.sort.endswith('desc') else iter(rows) for rows in self.chunks)
            selected=list(islice(merge(*sources,key=self.key),start,start+size))
        elif start+size<=4096:
            selected=nsmallest(start+size,self.rows,key=self.key)[start:]
        elif 0<=total-start<=4096:
            selected=[row for _,row in reversed(nlargest(total-start,enumerate(self.rows),key=lambda pair:(self.key(pair[1]),pair[0])))][:size]
        else:selected=ordered(self.rows,self.sort)[start:start+size]
        return dict(start=start,total=total,rows=selected)


def request_rows(turns,rows,scope,include_empty=True,unlinked_total=None):
    from .core import summarize, METRICS
    groups={}
    for r in rows:groups.setdefault(r.get('turn'),[]).append(r)
    result=[]
    for t in turns:
        items=groups.get(t.get('turn'),[])
        if not items and (t.get('calls') or not include_empty):continue
        row={k:v for k,v in t.items() if k not in ('calls','first_call')}
        if len(items)!=len(t.get('calls',[])):
            values=summarize(items)
            for key in METRICS:row[key]=values[key] if not values['missing'][key] else None
            row.update(sum_cost(items,strict=True),responses=len(items),duration=None,complete=False)
            row['state']='조건 일부';row['exclusions']=list(dict.fromkeys([*row.get('exclusions',[]),'조건 일부']))
            valid=cache_rows(items);inp=sum(r['input'] for r in valid)
            row['rate']=100*sum(r['cached'] for r in valid)/inp if inp and len(valid)==len(items) else None
        row['call_mean']=stats(items,'cost')['mean'];result.append(row)
    unlinked=groups.get(None,[])+groups.get('',[])
    if unlinked and scope:
        values=summarize(unlinked)
        for key in METRICS:
            if values['missing'][key]:values[key]=None
        valid=cache_rows(unlinked);inputs=sum(r['input'] for r in valid)
        values['rate']=100*sum(r['cached'] for r in valid)/inputs if inputs and len(valid)==len(unlinked) else None
        result.append(dict(values,**sum_cost(unlinked,strict=True),home=scope[0],sid=scope[1],
            turn='__unlinked__',state=f'요청 미연결 {len(unlinked):,}호출',complete=False,duration=None,
            responses=len(unlinked),total_responses=len(unlinked) if unlinked_total is None else unlinked_total,
            call_mean=stats(unlinked,'cost')['mean'],ts=max(r['ts'] for r in unlinked)))
    return result


def window(rows,request):
    if isinstance(rows,_OrderedRows):return rows.window(request)
    start=max(0,min(int(request.get('start',0)),max(0,len(rows)-1)))
    size=max(64,min(512,int(request.get('size',128))))
    anchor=request.get('anchor')
    if anchor:
        found=next((i for i,r in enumerate(rows) if list(row_key(r))==anchor),None)
        if found is not None:start=max(0,found-int(request.get('anchor_offset',0)))
    return dict(start=start,total=len(rows),rows=rows[start:start+size])


def row_key(row):
    return row.get('home'),row.get('sid'),row.get('key') or row.get('turn') or row.get('id')


def explorer(engine,result,selection):
    a=result['analysis'];scope_rows=a['responses'];ctx=selection.get('context') or {}
    rows=scope_rows
    population=ctx.get('population')
    if population:
        wanted={identity(r) for r in resolve_population(engine,population)}
        rows=[r for r in rows if identity(r) in wanted]
    elif ctx.get('records') is not None:
        wanted={tuple(k) for k in ctx['records']};rows=[r for r in rows if identity(r) in wanted]
    filters=list(selection.get('filters',[]))+list(ctx.get('filters',[]))
    filtered=[r for r in rows if any(matches(r,k) for k in filters)] if filters else rows
    search=selection.get('search','').casefold().strip();sort=selection.get('sort','ts_desc')
    matching={(s['home'],s['id']) for s in a['sessions'] if search in ' '.join(str(s.get(k,'')) for k in ('title','project_name','agent_nickname','cwd','id')).casefold()} if search else None
    parents=[]
    scope=tuple(selection['session']) if selection.get('session') else None
    turn=selection.get('turn');view=selection.get('view','requests')
    if view=='requests' and not scope:view='sessions'
    rows=[r for r in filtered if (r['home'],r['sid'])==scope] if scope and view!='sessions' else filtered
    if matching is not None:rows=[r for r in rows if (r['home'],r['sid']) in matching]
    if turn:rows=[r for r in rows if (not r.get('turn') if turn=='__unlinked__' else r.get('turn')==turn)]
    session=next((s for s in a['sessions'] if (s['home'],s['id'])==scope),None)
    event_id=selection.get('event');event=None
    if event_id:
        event=next((e for e in (session or {}).get('cache_health',{}).get('events',[]) if str(e['id'])==str(event_id)),None)
        phases={str(k):name for field,name in (('baseline_keys','기준'),('occurrence_keys','발생'),('recovery_keys','회복')) for k in (event or {}).get(field,[])}
        rows=[dict(r,_incident_phase=phases[str(identity(r)[2])]) for r in rows if str(identity(r)[2]) in phases]
    turns=result['lookup']['session_turns'].get(scope,[])
    if view=='requests':
        whole=engine.sessions.get(scope,{}).get('prepared',{}).get('history',[])
        records=request_rows(turns,rows,scope,
            include_empty=not filters and (matching is None or scope in matching),
            unlinked_total=sum(not r.get('turn') for r in whole))
    else:
        records=rows
        if selection.get('outside') and turn and scope and session is not None:
            records=list(rows)
            keys={identity(r) for r in records}
            whole=engine.sessions.get(scope,{}).get('whole_turns',{})
            records.extend(dict(r,_outside=True) for r in whole.get((*scope,turn),[]) if identity(r) not in keys)
    call_rows=rows if view=='requests' else records if view=='calls' else []
    call_lookup=result['lookup']['sessions'] if view=='calls' and records is scope_rows else None
    call_identities=frozenset(identity(row) for row in call_rows) if call_lookup is None else None
    parent_kind='requests' if view=='calls' and scope else 'sessions'

    from .history_projection import prepare, session_tree, total
    cache_key=(result['key'],search,tuple(filters),repr(ctx.get('population')),repr(ctx.get('records')))
    cached=getattr(engine,'_history_prepared',None)
    hierarchy=cached[1] if cached and cached[0]==cache_key else prepare(result,filtered,search,include_empty=not filters)
    engine._history_prepared=(cache_key,hierarchy)
    project=selection.get('project','')
    if session:
        from .analytics import project_identity
        project=project_identity(session)
    navigation=sorted(hierarchy['projects'],key=lambda r:r['title'].casefold())
    if not project and view=='sessions' and navigation:project=navigation[0]['project_id']
    if view=='projects':records=hierarchy['projects']
    elif view=='sessions':
        records=session_tree(hierarchy,project,set(selection.get('expanded',[])),lambda rows:ordered(rows,sort),
                             bool(search),set(selection.get('search_collapsed',[])))
    elif view=='children':records=[hierarchy['items'][k] for k in hierarchy['children'].get(scope,[])]
    request_ordinal=None
    if scope and (view=='requests' or turn):
        full=engine.sessions.get(scope,{}).get('whole_turns',{})
        all_turns=engine.sessions.get(scope,{}).get('prepared',{}).get('turn_records',{})
        ids=set(all_turns)|{k[2] for k in full}
        def position(t):
            start=all_turns.get(t,{}).get('started_at')
            return (start if start is not None else min((r['ts'] for r in full.get((*scope,t),[])),default=float('inf')),t)
        numbers={t:i+1 for i,t in enumerate(sorted(ids,key=position))}
        request_ordinal=numbers.get(turn)
        if view=='requests':records=[dict(r,ordinal=numbers.get(r.get('turn')),
            ts=r.get('ts') if r.get('turn')=='__unlinked__' else position(r.get('turn'))[0]) for r in records]
    available=available_fields(records)
    if view=='calls' and not selection.get('timeline'):
        records=_OrderedRows(records,sort,tuple(call_lookup.values()) if call_lookup is not None else None)
    elif view!='sessions':records=ordered(records,sort)
    timeline=project_timeline(records,kind=view) if selection.get('timeline') and scope and view in ('requests','calls') else None
    parents=navigation;parent_kind='projects'
    if scope:summary=total(rows)
    elif project:summary=next((r for r in hierarchy['projects'] if r['project_id']==project),{})
    else:summary=hierarchy['summary']
    path=[]
    if project:
        label=next((r['title'] for r in hierarchy['projects'] if r['project_id']==project),project)
        path.append(dict(title=label,project_id=project))
    cursor=scope;chain=[]
    while cursor in hierarchy['items']:
        item=hierarchy['items'][cursor];chain.append({k:item[k] for k in ('title','home','sid','project_id')});cursor=hierarchy['parent'].get(cursor)
    path.extend(reversed(chain))
    selected_identity=selection.get('call_identity')
    detail=engine.record(*selected_identity) if selected_identity else None
    event_keys={str(k) for field in ('baseline_keys','occurrence_keys','recovery_keys') for k in (event or {}).get(field,[])}
    return dict(path=path,view=view,session=scope,turn=turn,request_ordinal=request_ordinal,metadata=session,group={k:v for k,v in hierarchy['rollups'].get(scope,{}).items() if k!='members'},project=project,summary=summary,child_count=len(hierarchy['children'].get(scope,[])),
        records=records,parents=parents,parent_kind=parent_kind,partial_parents=any(r.get('known',0)<r.get('calls',0) for r in parents) if parent_kind=='sessions' else False,available=available,detail=detail,
        timeline=timeline,_call_identities=call_identities,_call_lookup=call_lookup,detail_in_scope=detail_in_scope(detail,call_identities,call_lookup),
        event=event,event_rows=[r for r in scope_rows if (r['home'],r['sid'])==scope and str(identity(r)[2]) in event_keys])


def detail_in_scope(detail,identities,lookup):
    if not detail:return False
    if lookup is None:return identity(detail) in identities
    rows=lookup.get((detail['home'],detail['sid']),[])
    position=bisect_left(rows,detail['ts'],key=lambda row:row['ts'])
    for index in range(position,len(rows)):
        row=rows[index]
        if row['ts']!=detail['ts']:break
        if identity(row)==identity(detail):return True
    return False


def explorer_window(engine,q,data,view):
    selection=q.get('explorer',{})
    detail=engine.record(*selection['call_identity']) if selection.get('call_identity') else None
    projected={key:value for key,value in data.items() if key not in ('_call_identities','_call_lookup')}
    ranges=q.get('table_windows',{})
    projected.update(detail=detail,detail_in_scope=detail_in_scope(detail,data['_call_identities'],data['_call_lookup']),
        records=window(data['records'],ranges.get('records',{})),parents=window(data['parents'],ranges.get('parents',{})))
    return {**view,'explorer':projected,
        'analysis':{**view['analysis'],'sessions':[data['metadata']] if data['metadata'] else [],'responses':data['event_rows']}}


def reference(query,path):
    return {'query':query,'path':path}


def resolve_population(engine,ref,flatten=True):
    if 'union' in ref:
        return list({(identity(r),r.get('turn')):r for part in ref['union'] for r in resolve_population(engine,part,flatten)}.values())
    result=engine.query(ref['query']);value=result
    if ref['path'][0]=='summary_populations':
        a=result['analysis'];rows=a['responses'];turns=[t for t in a['turns'] if t['complete']]
        selected=([r for r in rows if r.get('cost') is not None],)*2+([t for t in turns if t.get('cost') is not None],cache_rows(rows),[r for r in rows if r.get('output_speed') is not None])
        value={**result,'summary_populations':selected}
    if ref['path'][0]=='unknown_population':
        return [r for r in result['analysis']['responses'] if r.get('model')==ref.get('model') and request_tier(r)=='미확인']
    if ref['path']==['overview','attention','cache_degradation']:
        events=result['overview']['attention']['cache_degradation']
        keys={(e['home'],e['sid'],str(k)) for e in events for k in e['occurrence_keys']}
        return [r for r in result['analysis']['responses'] if (r['home'],r['sid'],str(identity(r)[2])) in keys]
    for key in ref['path']:
        if isinstance(key,dict):
            value=next((r for r in value if all(r.get(k)==v or isinstance(r.get(k),tuple) and list(r[k])==v for k,v in key.items())),[])
        elif isinstance(value,dict):value=value.get(key,[])
        elif isinstance(value,(tuple,list)) and isinstance(key,int):value=value[key] if 0<=key<len(value) else []
        else:return []
    rows=value if isinstance(value,list) else [value]
    return [r for sample in rows for r in sample.get('calls',[sample])] if flatten else rows


def compact(query,value,path=()):
    if isinstance(value,list):
        result=[]
        for i,row in enumerate(value):
            selector={k:row[k] for k in ('id','label','dimension','key','start_ts') if k in row} if isinstance(row,dict) else {}
            result.append(compact(query,row,(*path,selector or i)))
        return result
    if not isinstance(value,dict):return value
    result={}
    for key,item in value.items():
        if key in ('records','rows') and isinstance(item,list):
            result['population']=reference(query,[*path,key]);result['population_count']=len(item)

        elif key=='scatter':
            result[key]=[{k:r.get(k) for k in ('home','sid','key','turn','cost','duration','input','output','rate','cached','cwd','source')}
                         | {'population':reference(query,[*path,key,i]),'sample_unit':'turn' if 'calls' in r else 'response'} for i,r in enumerate(item)]
        else:result[key]=compact(query,item,(*path,key))
    return result


def project(engine,q):
    query={k:v for k,v in q.items() if k not in ('explorer','table_windows','presentation','compare_model','result_view')}
    # The only consumer of out-of-range calls is the selected request. Read
    # that owner directly instead of merging every owner's history per view.
    query['include_whole_history']=False
    # The displayed hierarchy computes its own project-restricted lineage.
    # General query callers still receive their ordinary session cost rollups.
    if q['page']==2:
        query['_session_costs']=False
        selection=q.get('explorer',{})
        stable={k:v for k,v in query.items() if k!='now' and (q.get('period')=='custom' or k not in ('start','end'))}
        key=(engine.query_key(stable),engine.query_key({'page':2,**{k:v for k,v in selection.items() if k!='call_identity'}}))
        cached=getattr(engine,'_explorer_projection',None)
        if cached and cached[0]==key and q['now']<cached[3]:return explorer_window(engine,q,cached[1],cached[2])
    result=engine.query(query);a=result['analysis'];responses=a['responses'];turns=a['turns']
    view=dict(key=result['key'],revision=result['revision'],filter_choices=result['filter_choices'],
        analysis={k:v for k,v in a.items() if k not in ('sessions','responses','turns','units','groups')},lookup={})
    view['analysis'].update(response_count=len(responses),session_count=len({(r['home'],r['sid']) for r in responses}))
    if q['page']==2:
        # Range movement reuses the prepared population; the GUI receives only
        # the requested window. Search/sort/selection never run on the GUI thread.
        data=explorer(engine,result,selection)
        from .analysis_worker import expiry
        engine._explorer_projection=(key,data,view,expiry(engine,q))
        return explorer_window(engine,q,data,view)
    if q['page']==0:
        overview=result['overview']
        # Only displayed series belong on the wire. Full model/session groupings
        # remain in the engine for drill-down through population references.
        view['overview']=compact(query,{k:v for k,v in overview.items() if k not in ('models','sessions','attention')},('overview',))
        totals={'calls':len(responses)}
        for key in ('input','output'):
            known=[r[key] for r in responses if r.get(key) is not None]
            totals[key]=sum(known) if len(known)==len(responses) else None
            totals[key+'_known']=len(known)
        view['overview']['usage_totals']=totals
        view['overview']['attention']={k:{'count':len(v),'population':reference(query,['overview','attention',k])} for k,v in overview['attention'].items()}
        completed=[t for t in turns if t['complete']];priced=[r for r in responses if r.get('cost') is not None]
        selections=[priced,priced,[t for t in completed if t['cost'] is not None],cache_rows(responses),[r for r in responses if r.get('output_speed') is not None]]
        # Give summary drill-down the same reproducible path as chart groups.
        summaries=[]
        values=[overview['total'],overview['call_stats']['mean'],overview['turn_stats']['mean'],overview['summary']['cache']['value'],overview['summary']['output_speed']['value']]
        for i,rows in enumerate(selections):
            summaries.append(dict(value=values[i],known=len(rows),N=len(turns) if i==2 else len(responses),
                population=reference(query,['summary_populations',i]),population_count=len(rows),
                assumption_population=reference(query,['analysis','turns'] if i==2 else ['analysis','responses']),
                request_exclusions=dict(Counter(reason for t in turns if not t['complete'] for reason in t.get('exclusions',[]) or [t.get('state','기록 불완전')])) if i==2 else {}))
        view['summaries']=summaries
        view['all_population']=reference(query,['analysis','responses'])
    else:
        comparison={k:v for k,v in result['comparison'].items() if k not in ('scatter','repricing')}
        if q.get('result_view')!='scatter':
            comparison={**comparison,'groups':[{k:v for k,v in g.items() if k!='scatter'} for g in comparison.get('groups',[])]}
        view['comparison']=compact(query,comparison,('comparison',))
        unknown=[r for r in responses if r.get('model')==q.get('compare_model') and request_tier(r)=='미확인']
        view['unknown_population']=dict(count=len(unknown),population=dict(reference(query,['unknown_population']),model=q.get('compare_model')))
    return view


def population_summary(engine,population,assumptions=None):
    calls=resolve_population(engine,assumptions or population)
    return dict(assumptions=mode_assumptions(calls))


def refresh_bounds(value,q):
    if q.get("category_usage"):return value
    """Refresh partial bucket labels without rebuilding an unchanged population."""
    if q['page']!=0 or q.get('period')=='custom':return value
    from datetime import datetime
    overview=value['overview'];timeline=overview.get('timeline',[])
    if overview.get('end')==datetime.fromtimestamp(q['end']).isoformat():return value
    if not timeline:return value
    timeline=list(timeline)
    timeline[-1]={**timeline[-1],'end_ts':q['end'],'end':datetime.fromtimestamp(q['end']).isoformat()}
    if q.get('start'):
        timeline[0]={**timeline[0],'start_ts':q['start'],'start':datetime.fromtimestamp(q['start']).isoformat()}
    return {**value,'overview':{**overview,'timeline':timeline,'end':datetime.fromtimestamp(q['end']).isoformat()}}
