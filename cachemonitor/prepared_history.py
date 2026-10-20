"""Bounded History scalar reductions and selected table/detail streams."""
from collections import Counter, defaultdict
from contextlib import ExitStack
import json
import ntpath
import sqlite3

from .analysis_engine import AnalysisEngine, retained_bytes
from .analysis_records import available_fields, compact_record, record_items
from .analytics import effort_key, finite, project_choices, project_identity, project_matches
from .core import METRICS, summarize, token_number
from .dashboard_views import identity, matches, ordered, resolve_population, row_key, window
from .history_projection import node_key, session_tree
from .history_timeline_data import project_timeline
from .pricing import COST_KEYS, display_tier, unknown_mode_calls
from .prepared_store import CapacityExceeded, NotReady, ReadCancelled, pack, unpack
from .prepared_sums import _Sum
from .session_costs import session_costs

QUERY_BYTES = 64 * 1024 * 1024
SELECTED_BYTES = 32 * 1024 * 1024
SORT_BYTES = 64 * 1024 * 1024
CACHE_BYTES = 16 * 1024 * 1024
FIELDS = tuple(dict.fromkeys((*METRICS, *COST_KEYS, 'key', 'turn', 'ts',
    'home', 'sid', 'model', 'effort', 'service_tier', 'display_service_tier',
    'mode_conflict', 'service_tier_source', 'transport', 'cache_degradation', 'cache_incident_id')))
BODY_FILTERS = frozenset(('model_mismatch', 'observation_missing',
                         'observation_conflict', 'observation_problem'))


class Budget:
    def __init__(self, maximum=QUERY_BYTES):
        self.maximum = maximum
        self.used = 0

    def add(self, value, overhead=0):
        size = retained_bytes(value, self.maximum) + overhead
        if self.used + size > self.maximum:
            raise CapacityExceeded('History projection exceeds its retained-object budget')
        self.used += size
        return value


class Totals:
    """Preserve the Python sum order and missing-value rules without row lists."""
    def __init__(self):
        self.count = 0
        self.sums = {name:_Sum() for name in (*METRICS, *COST_KEYS)}
        self.known = dict.fromkeys(self.sums, 0)
        self.raw_sums = dict.fromkeys(('input', 'output'), 0)
        self.raw_known = dict.fromkeys(self.raw_sums, 0)
        self.cache_input = self.cache_read = self.cache_count = 0
        self.own = dict(cost=0.0, calls=0, priced=0, cache_input=0,
                        cache_read=0, latest_ts=None, gap=False)
        self.ts = None
        self.mean_sum = _Sum()
        self.mean_count = 0
        self.unknown = 0
        self.long = False

    def add(self, row):
        self.count += 1
        for name in self.sums:
            value = row.get(name)
            if value is not None and (name in COST_KEYS or token_number(value) is not None):
                self.sums[name].add(value)
                self.known[name] += 1
        for name in self.raw_sums:
            if row.get(name) is not None:
                self.raw_sums[name] += row[name]
                self.raw_known[name] += 1
        inp, cached = row.get('input'), row.get('cached')
        if token_number(inp) is not None and token_number(cached) is not None and cached <= inp:
            self.cache_input += inp
            self.cache_read += cached
            self.cache_count += 1
        own = self.own
        own['calls'] += 1
        if row.get('cost') is not None:
            own['cost'] += row['cost'];own['priced'] += 1
        if type(inp) is int and type(cached) is int and inp > 0 and 0 <= cached <= inp:
            own['cache_input'] += inp;own['cache_read'] += cached
        stamp = row.get('ts')
        if stamp is not None:
            own['latest_ts'] = max(own['latest_ts'] or stamp, stamp)
            self.ts = max(self.ts, stamp) if self.ts is not None else stamp
        if finite(row.get('cost')):
            self.mean_sum.add(row['cost']);self.mean_count += 1
        self.unknown += unknown_mode_calls(row)
        self.long |= bool(row.get('long_context'))

    def values(self, strict=False):
        result = {name: (None if strict and self.known[name] != self.count else
                        self.sums[name].value() if self.known[name] or not self.count else None)
                  for name in self.sums}
        result.update(count=self.count,
                      missing={name:self.count-self.known[name] for name in METRICS},rate=self.rate(strict))
        return result

    def rate(self, strict=False):
        return (100 * self.cache_read / self.cache_input
                if self.cache_input and (not strict or self.cache_count == self.count) else None)

    def mean(self):
        return self.mean_sum.value() / self.mean_count if self.mean_count else None

    def history(self):
        return dict(cost=self.sums['cost'].value() if self.known['cost'] else None,
                    partial=self.known['cost'] < self.count,calls=self.count,known=self.known['cost'],
                    **{name:self.raw_sums[name] if self.count and self.raw_known[name] == self.count else None
                       for name in self.raw_sums},cache_ratio=self.rate(),ts=self.ts)


def batches(values, size=128):
    batch=[]
    for value in values:
        batch.append(value)
        if len(batch)==size:
            yield batch;batch=[]
    if batch:yield batch


def rows(engine, owner, query, *, bodies=False):
    if bodies:
        yield from (compact_record(row) for row in engine.snapshot.iter_calls(
            owner,start=query['start'],end=query['end']))
    else:
        yield from engine._pages(engine.snapshot.projected_calls,owner,FIELDS,
                                 start=query['start'],end=query['end'],history=True)


def selected_match(row,key):
    if key in BODY_FILTERS and '_history_matches' in row:return key in row['_history_matches']
    return matches(row,key)


def dimensions(row, query):
    from .comparison_usage import matches as comparison_matches
    return ((not query.get('model') or (row.get('model') or '미확인')==query['model']) and
            (not query.get('effort') or (row.get('effort') or '미확인')==query['effort']) and
            (not query.get('service_tier') or display_tier(row)==query['service_tier']) and comparison_matches(row,query))


def source_matches(source, owner, query):
    return ((query.get('archived',True) or not source.get('archived')) and
            (not query.get('source') or source.get('source','unknown')==query['source']) and
            (not query.get('home') or owner[0]==query['home']) and
            (not query.get('sid') or owner[1]==query['sid']) and
            (not query.get('project') or project_matches(source,query['project'])))


def legacy_population(engine, reference):
    """Bound explicit legacy aggregate scans before invoking their projector."""
    clone=AnalysisEngine();budget=Budget(SELECTED_BYTES)
    for owner,state in engine._selected(reference['query']):
        source=dict(state['prepared']);history=[];records={};states={}
        for row in engine.snapshot.iter_calls(owner):history.append(budget.add(compact_record(row),32))
        for record in engine._pages(engine.snapshot.requests,owner):
            budget.add(record,96);records[record['key']]=record['metadata']
        for header in engine._pages(engine.snapshot.request_headers,owner):
            if 'turn_state' in header:
                budget.add((header['key'],header['turn_state']),96);states[header['key']]=header['turn_state']
        source.update(history=history,turn_records=records,turn_states=states,_prepared=True)
        whole=defaultdict(list)
        for row in history:
            if row.get('turn'):whole[(*owner,row['turn'])].append(row)
        boundaries=sorted((value,index) for index,metadata in enumerate(records.values())
            for value in (metadata.get('started_at'),metadata.get('ended_at')) if value is not None)
        clone.sessions[owner]=dict(prepared=source,source=source,revision=state['revision'],
            timestamps=[row['ts'] for row in history],whole_turns=dict(whole),
            boundaries=[p[0] for p in boundaries],boundary_turns=[p[1] for p in boundaries],turn_ids=list(records))
    return {identity(row) for row in resolve_population(clone,reference)}


def context_filter(engine, reference):
    if not reference:return lambda row,owner,source: True
    if 'union' in reference:
        predicates=[context_filter(engine,part) for part in reference['union']]
        return lambda row,owner,source: any(predicate(row,owner,source) for predicate in predicates)
    if reference.get('path')==['analysis','responses']:
        query=reference['query']
        return lambda row,owner,source: (source_matches(source,owner,query) and
            query['start']<=row['ts']<query['end'] and dimensions(row,query))
    wanted=legacy_population(engine,reference)
    return lambda row,owner,source: identity(row) in wanted


def reasons(metadata, state, source, count, header, query):
    started,ended=metadata.get('started_at'),metadata.get('ended_at');result=[]
    if state!='완료':result.append({'진행':'진행 중','중단':'중단'}.get(state,'종료 미확인'))
    if started is None:result.append('시작 기록 없음')
    if ended is None:result.append('완료 기록 없음')
    if started is not None and ended is not None:
        if ended<started:result.append('시간 관측 충돌')
        if not (query['start']<=started and ended<query['end']):result.append('기간 일부')
    if count!=header['call_count']:
        outside=header['call_count'] and (header['minimum']<query['start'] or header['maximum']>=query['end'])
        result.append('기간 일부' if outside else '조건 일부')
    if not source.get('collection_complete',not source.get('pending',False)):result.append('수집 중')
    if started is not None and ended is not None and any(g['start']<=ended and g['end']>=started for g in source.get('coverage_gaps',[])):
        result.append('기록 불완전')
    return list(dict.fromkeys(result))


def request_row(owner, turn, aggregate, header, metadata, source, query):
    state=metadata.get('state') or header.get('turn_state','미확인')
    exclusions=reasons(metadata,state,source,aggregate.count,header,query)
    started,ended=metadata.get('started_at'),metadata.get('ended_at')
    values=aggregate.values(strict=True)
    if not header['call_count']:values.update(dict.fromkeys((*METRICS,*COST_KEYS)))
    first=header.get('first_call') if header['call_count'] and started is not None else None
    complete=not exclusions
    return dict(values,**header['dimensions'],home=owner[0],sid=owner[1],turn=turn,request_id=turn,
        title=source['title'],project=project_identity(source),
        project_name=source.get('project_name') or ntpath.basename(source.get('cwd','').rstrip('/\\')) or '프로젝트 없는 작업',
        cwd=source.get('cwd',''),source=source.get('source','unknown'),
        ts=ended if ended is not None else aggregate.ts if aggregate.ts is not None else started or 0,
        started_at=started,ended_at=ended,responses=aggregate.count,total_responses=header['call_count'],
        start_input=first.get('input') if first else None,start_rate=first.get('rate') if first else None,
        duration=ended-started if complete and started is not None and ended is not None else None,
        state=state if complete or state!='완료' else exclusions[0],complete=complete,exclusions=exclusions,
        unknown_mode_calls=aggregate.unknown,display_service_tier=header['dimensions']['service_tier'],
        long_context=aggregate.long,price_issue='포함 호출 중 환산액 미산정' if values['cost'] is None else '',
        call_mean=values['cost']/aggregate.count if values['cost'] is not None and aggregate.count else None)


def hierarchy(engine, sessions, search, own, projects_total, global_total, request_counts, budget):
    indexed={(s['home'],s['id']):s for s in sessions};projects=project_choices(sessions)['project_labels'];parent={}
    for key,source in indexed.items():
        candidate=(key[0],source.get('parent_thread_id'))
        if candidate in indexed and candidate!=key and project_identity(source)==project_identity(indexed[candidate]):parent[key]=candidate
    for key in list(parent):
        seen={key};cursor=key
        while cursor in parent:
            cursor=parent[cursor]
            if cursor in seen:parent.pop(key,None);break
            seen.add(cursor)
    matching={key for key,source in indexed.items() if not search or search in ' '.join(
        str(source.get(f,'')) for f in ('title','project_name','cwd','id')).casefold()}
    visible=set(matching)
    # Admit ancestor member lists before the existing rollup allocates them.
    for key in indexed:
        engine.snapshot._check();cursor=key;budget.add(key,96)
        while cursor in parent:
            cursor=parent[cursor];budget.add((),16)
            if key in matching:visible.add(cursor)
    lineage=[{**source,'parent_thread_id':parent[key][1] if key in parent else None} for key,source in indexed.items()]
    rollups=session_costs(lineage,{key:aggregate.own for key,aggregate in own.items()})
    children=defaultdict(list);project_sessions=defaultdict(list);items={}
    for key,source in indexed.items():
        if key not in visible:continue
        pid=project_identity(source);group=rollups[key]
        children[parent.get(key)].append(key);project_sessions[pid].append(key)
        measured=[own[member].history() for member in group['members'] if member in own and own[member].count]
        tokens={name:sum(r[name] for r in measured) if measured and all(r[name] is not None for r in measured) else None for name in ('input','output')}
        items[key]=budget.add(dict({name:value for name,value in group.items() if name!='members'},
            known=group['priced'],home=key[0],sid=key[1],key=node_key('session',*key),
            title=source.get('title') or '제목 없음',project=projects[pid],project_id=pid,source=source.get('source',''),
            requests=sum(request_counts.get(member,0) for member in group['members']),**tokens,ts=group['latest_ts'],context=key not in matching))
    project_rows=[]
    for pid in projects:
        keys=project_sessions[pid]
        project_rows.append(budget.add(dict(projects_total.get(pid,Totals()).history(),
            key=node_key('project',pid),project_id=pid,title=projects[pid],
            roots=sum(key not in parent for key in keys),children=sum(key in parent for key in keys),
            requests=sum(request_counts.get(key,0) for key in keys if key in matching))))
    return dict(projects=project_rows,items=items,parent=parent,children=children,rollups=rollups,summary=global_total.history())


def project(engine,q):
    cached=getattr(engine,'_prepared_history_projection',None)
    if cached is not None:
        if cached.key==projection_key(engine,q) and cached.token==engine.token and q.get('now',q['end'])<cached.deadline:
            try:return cached.project(engine,q)
            except BaseException:
                close_for_engine(engine);raise
        close_for_engine(engine)
    with ExitStack() as cleanup:
        return _project(engine,q,cleanup)


def projection_key(engine,q):
    stable={key:value for key,value in q.items() if key not in ('now','table_windows') and
            (q.get('period')=='custom' or key not in ('start','end'))}
    stable['explorer']={key:value for key,value in q.get('explorer',{}).items() if key!='call_identity'}
    return engine.query_key(stable)


def close_for_engine(engine):
    """The engine calls this before generation adoption and during close."""
    cached=engine.__dict__.pop('_prepared_history_projection',None)
    if cached is not None:cached.index.close()


class HistoryProjection:
    """One scope index plus bounded compact metadata, never retained call bodies."""
    def __init__(self,engine,q,result,parents,event_rows,index):
        from .analysis_worker import expiry
        self.query=dict(q)
        stable={k:v for k,v in q.items() if k!='now' and (q.get('period')=='custom' or k not in ('start','end'))}
        self.page_key=engine.query_key(stable)
        self.key=projection_key(engine,q);self.token=engine.token;self.deadline=expiry(engine,q)
        self.index=index;self.parents=parents
        self.events=[dict(home=row['home'],sid=row['sid'],key=row['key']) for row in event_rows]
        self.head=dict(result,analysis={**result['analysis'],'responses':[]},
            explorer={key:value for key,value in result['explorer'].items()
                      if key not in ('records','parents','detail','detail_in_scope','event_rows')})

    def weight(self):
        return retained_bytes((self.key,self.head,self.parents,self.events,self.query,self.page_key),CACHE_BYTES)

    def unaffected(self,engine,pending,removed):
        # A foreign population can depend on owners outside this query's scope.
        selection=self.query.get('explorer') or {}
        if (selection.get('context') or {}).get('population'):
            return False
        detail=selection.get('call_identity')
        if detail and tuple(detail[:2]) in pending.keys()|removed:
            return False
        for owner in pending.keys()|removed:
            previous=engine.sessions.get(owner)
            replacement=pending.get(owner)
            current=replacement[0] if replacement is not None and not replacement[1] else None
            if any(state is not None and source_matches(state['prepared'],owner,self.query)
                   for state in (previous,current)):
                return False
        return True

    def adopt(self,engine):
        self.token=engine.token
        if '_prepared_token' in self.query:
            self.query=dict(self.query,_prepared_token=(engine.token.instance,engine.token.generation))
        self.key=projection_key(engine,self.query)
        stable={k:v for k,v in self.query.items() if k!='now' and
                (self.query.get('period')=='custom' or k not in ('start','end'))}
        self.page_key=engine.query_key(stable)
        query={k:v for k,v in self.query.items() if k not in
               ('explorer','table_windows','presentation','compare_model','result_view')}
        query.update(include_whole_history=False,_session_costs=False)
        self.head=dict(self.head,key=engine.query_key(query),revision=engine.revision)

    def project(self,engine,q):
        self.index.bind(engine.snapshot)
        selection=q.get('explorer',{});ranges=q.get('table_windows',{});request=ranges.get('records',{})
        anchor=self.index.anchor(request.get('anchor'))
        start,descriptors=self.index.window(request,anchor)
        budget=Budget(SELECTED_BYTES)
        detail=selected_detail(engine,selection,budget)
        records=hydrate_records(engine,descriptors,budget)
        event_rows=hydrate_records(engine,self.events,budget)
        return dict(self.head,analysis={**self.head['analysis'],'responses':event_rows},
            explorer={**self.head['explorer'],'records':dict(start=start,total=self.index.count,rows=records),
                'parents':window(self.parents,ranges.get('parents',{})),'detail':detail,
                'detail_in_scope':bool(detail and self.index.contains(identity(detail))),'event_rows':event_rows})


def _project(engine,q,cleanup):
    query={key:value for key,value in q.items() if key not in ('explorer','table_windows','presentation','compare_model','result_view')}
    query.update(include_whole_history=False,_session_costs=False)
    selection=q.get('explorer',{});scope=tuple(selection['session']) if selection.get('session') else None
    turn=selection.get('turn');view=selection.get('view','requests')
    if view=='requests' and not scope:view='sessions'
    search=selection.get('search','').casefold().strip();sort=selection.get('sort','ts_desc')
    context=selection.get('context') or {};filters=[*selection.get('filters',[]),*context.get('filters',[])]
    population=context_filter(engine,context.get('population'))
    wanted={tuple(key) for key in context['records']} if context.get('records') is not None and not context.get('population') else None
    budget=Budget();budget.add(wanted);sessions=[];selected_states=[]
    global_calls=Totals();global_units=Totals();global_history=Totals()
    owner_totals={};project_totals={};request_counts={};combos=Counter();response_sessions=set()
    missing_turns=turn_count=excluded=0;request_view=view=='requests' and scope is not None
    selected_total=Totals()
    selected_budget=Budget(SELECTED_BYTES)
    detail=selected_detail(engine,selection,selected_budget)
    detail_present=False
    call_index=cleanup.enter_context(SelectionIndex(engine.snapshot)) if view=='calls' else None
    call_window_state=(_CallWindow(engine,selected_total,detail,selection,
        q.get('table_windows',{}).get('records',{}),selected_budget,call_index)
        if call_index is not None else None)
    unlinked_index=cleanup.enter_context(SelectionIndex(engine.snapshot)) if request_view else None
    selected_requests={};selected_headers={};selected_base={};selected_visible={};request_order=[]
    metadata=None;event=None;phases={};event_keys=set();event_descriptors=[]
    global_unit_calls=query.get('unit','response') in ('response','call')
    for owner,state in engine._selected(query):
        engine.snapshot._check()
        source=engine.query_source(owner,state,{**query,'explorer':selection})
        source={k:v for k,v in source.items() if k not in ('history','requests','groups','_prepared')}
        sessions.append(budget.add(source));selected_states.append((owner,state,source))
        if owner==scope:
            metadata=source
            event=next((item for item in (source.get('cache_health') or {}).get('events',[])
                if str(item['id'])==str(selection.get('event'))),None) if selection.get('event') else None
            phases={str(key):name for field,name in (('baseline_keys','기준'),('occurrence_keys','발생'),('recovery_keys','회복'))
                for key in (event or {}).get(field,[])};event_keys=set(phases)
        history_match=not search or search in ' '.join(str(source.get(f,'')) for f in ('title','project_name','cwd','id')).casefold()
        call_match=not search or search in ' '.join(str(source.get(f,'')) for f in ('title','project_name','agent_nickname','cwd','id')).casefold()
        local_budget=Budget(SELECTED_BYTES);counts={};visible_turns=set();aggregates={}
        for row in rows(engine,owner,query):
            if not dimensions(row,query):continue
            if owner==scope and str(identity(row)[2]) in event_keys:
                event_descriptors.append(selected_budget.add(dict(home=owner[0],sid=owner[1],key=row['key']),16))
            global_calls.add(row);response_sessions.add((row['home'],row['sid']))
            combo=(row.get('model') or '미확인',row.get('effort') or '미확인',display_tier(row))
            if combo not in combos:budget.add(combo,128)
            combos[combo]+=1;request=row.get('turn')
            if request:
                if request not in counts:
                    local_budget.add(request,192);counts[request]=0
                    if not global_unit_calls or request_view and owner==scope:
                        aggregates[request]=Totals();local_budget.add(vars(aggregates[request]))
                counts[request]+=1
                if request in aggregates:aggregates[request].add(row)
            else:missing_turns+=1
            accepted=population(row,owner,source) and (wanted is None or identity(row) in wanted)
            accepted=accepted and (not filters or any(selected_match(row,key) for key in filters))
            if (scope and view not in ('calls','requests') and (view=='sessions' or owner==scope)
                    and accepted and call_match
                    and (not turn or (not request if turn=='__unlinked__' else request==turn))
                    and (not selection.get('event') or str(identity(row)[2]) in phases)):
                selected_total.add(row)
            if (call_window_state is not None and (not scope or owner==scope) and accepted and call_match
                    and (not turn or (not request if turn=='__unlinked__' else request==turn))
                    and (not selection.get('event') or str(identity(row)[2]) in phases)):
                call_window_state.add(dict(row,_incident_phase=phases[str(identity(row)[2])]) if selection.get('event') else row)
            if accepted and history_match:
                if owner not in owner_totals:owner_totals[owner]=Totals();budget.add(vars(owner_totals[owner]))
                owner_totals[owner].add(row);global_history.add(row);visible_turns.add(request)
                pid=project_identity(source)
                if pid not in project_totals:project_totals[pid]=Totals();budget.add(vars(project_totals[pid]))
                project_totals[pid].add(row)
            if request_view and owner==scope and accepted and call_match and (
                not turn or (not request if turn=='__unlinked__' else request==turn)) and (
                not selection.get('event') or str(identity(row)[2]) in phases):
                if request not in selected_visible:selected_visible[request]=Totals();budget.add((request,vars(selected_visible[request])))
                selected_visible[request].add(row)
                selected_total.add(row)
                detail_present|=detail is not None and identity(row)==identity(detail)
                if request is None or request=='':
                    unlinked_index.add(False,0 if request is None else 1,
                        {name:row[name] for name in (*METRICS,*COST_KEYS,'ts') if name in row})
        boundary={}
        if not any(query.get(name) for name in ('model','effort','service_tier')):
            for field in ('started_at','ended_at'):
                for record in engine._pages(engine.snapshot.boundaries,owner,field,query['start'],query['end']):
                    if record['key'] not in boundary:local_budget.add(record,96)
                    boundary[record['key']]=record
        keys=dict.fromkeys(counts)
        keys.update((record['key'],None) for record in sorted(boundary.values(),key=lambda item:item['ordinal']))
        owner_request_count=0
        for batch in batches(keys):
            records={row['key']:row['metadata'] for row in engine._pages(engine.snapshot.requests,owner,batch)}
            headers={row['key']:row for row in engine._pages(engine.snapshot.request_headers,owner,batch,with_first=request_view and owner==scope)}
            if len(headers)!=len(batch):raise NotReady('History touched request lacks its committed header')
            for request in batch:
                header=headers[request];record=records.get(request,{});count=counts.get(request,0)
                exclusions=reasons(record,record.get('state') or header.get('turn_state','미확인'),source,count,header,query)
                turn_count+=1;excluded+=bool(exclusions)
                owner_request_count+=request in visible_turns or (not filters and not count)
                aggregate=aggregates.get(request,Totals())
                if not global_unit_calls and not exclusions:global_units.add(request_row(owner,request,aggregate,header,record,source,query))
                if request_view and owner==scope:
                    selected_requests[request]=budget.add(request_row(owner,request,aggregate,header,record,source,query))
                    selected_headers[request]=(header['display_ordinal'] or None,header['effective_at'])
                    selected_base[request]=count;request_order.append(request)
        request_counts[owner]=owner_request_count
    tree=hierarchy(engine,sessions,search,owner_totals,project_totals,global_history,request_counts,budget)
    totals=global_calls if global_unit_calls else global_units
    unclassified=[source['unclassified'] for source in sessions if source.get('unclassified')]
    analysis=dict(totals=totals.values(),basis=[],priced_count=totals.known['cost'],unpriced_count=totals.count-totals.known['cost'],
        missing_turn_responses=missing_turns,excluded_turns=excluded,turn_count=turn_count,unclassified=summarize(unclassified),
        unclassified_sessions=len(unclassified),response_count=global_calls.count,session_count=len(response_sessions))
    choices=dict(models=sorted({key[0] for key in combos}),efforts=sorted({key[1] for key in combos},key=effort_key),
        modes=sorted({key[2] for key in combos}),combinations=[dict(model=m,effort=e,service_tier=t,count=n) for (m,e,t),n in sorted(combos.items())])
    navigation=sorted(tree['projects'],key=lambda row:row['title'].casefold())
    project_id=project_identity(metadata) if metadata else selection.get('project','')
    if not project_id and view=='sessions' and navigation:project_id=navigation[0]['project_id']
    available=set();event_rows=[]

    def selected_rows(*,outside=False):
        for owner,state,source in selected_states:
            if scope and view!='sessions' and owner!=scope:continue
            match=not search or search in ' '.join(str(source.get(f,'')) for f in ('title','project_name','agent_nickname','cwd','id')).casefold()
            # Outside expansion intentionally ignores the original filters/search.
            if not match and not outside:continue
            values=(engine._pages(engine.snapshot.projected_calls,owner,FIELDS,history=True,turn=turn)
                    if outside else rows(engine,owner,query))
            for raw in values:
                row=compact_record(raw);request=row.get('turn')
                accepted=(match and query['start']<=row['ts']<query['end'] and dimensions(row,query)
                    and population(row,owner,source) and (wanted is None or identity(row) in wanted)
                    and (not filters or any(selected_match(row,key) for key in filters))
                    and (not turn or (not request if turn=='__unlinked__' else request==turn))
                    and (not selection.get('event') or str(identity(row)[2]) in phases))
                if outside:
                    if not accepted:yield dict(row,_outside=True)
                elif accepted:yield dict(row,_incident_phase=phases[str(identity(row)[2])]) if selection.get('event') else row

    request_ordinal=engine.request_positions(scope,[turn]).get(turn,(None,None))[0] if scope and turn else None
    timeline=None
    if view=='requests':
        records=[]
        call_match=metadata is not None and (not search or search in ' '.join(str(metadata.get(f,''))
            for f in ('title','project_name','agent_nickname','cwd','id')).casefold())
        for request in sorted(request_order,key=lambda key:selected_requests[key]['ts'],reverse=True):
            aggregate=selected_visible.get(request,Totals())
            if not aggregate.count and (selected_base[request] or filters or not call_match):continue
            row=dict(selected_requests[request])
            if aggregate.count!=selected_base[request]:
                values=aggregate.values(strict=True)
                row.update({name:values[name] for name in (*METRICS,*COST_KEYS)},responses=aggregate.count,
                    duration=None,complete=False,state='조건 일부',exclusions=list(dict.fromkeys([*row['exclusions'],'조건 일부'])),rate=aggregate.rate(True))
            row['call_mean']=aggregate.mean();ordinal,effective=selected_headers[request]
            row.update(ordinal=ordinal,ts=effective if effective is not None else float('inf'))
            records.append(selected_budget.add(row,16))
        unlinked=Totals()
        for row in unlinked_index.rows():unlinked.add(row)
        if unlinked.count:
            records.append(selected_budget.add(dict(unlinked.values(strict=True),home=scope[0],sid=scope[1],turn='__unlinked__',
                state=f'요청 미연결 {unlinked.count:,}호출',complete=False,duration=None,responses=unlinked.count,
                total_responses=engine.unlinked_total(scope),call_mean=unlinked.mean(),ts=unlinked.ts,ordinal=None),16))
        available=set(available_fields(records));records=ordered(records,sort)
        if selection.get('timeline') and scope:timeline=project_timeline(records,kind=view)
        records=window(records,q.get('table_windows',{}).get('records',{}))
    elif view=='calls':
        if selection.get('outside') and turn and scope and metadata is not None:
            for row in selected_rows(outside=True):call_window_state.add(row)
        records,timeline,available,detail_present=call_window_state.finish()
    else:
        if view=='projects':records=ordered(tree['projects'],sort)
        elif view=='sessions':records=session_tree(tree,project_id,set(selection.get('expanded',[])),
            lambda rows:ordered(rows,sort),bool(search),set(selection.get('search_collapsed',[])))
        elif view=='children':records=ordered([tree['items'][key] for key in tree['children'].get(scope,[])],sort)
        else:raise ValueError('unsupported History view')
        available=set(available_fields(records));records=window(records,q.get('table_windows',{}).get('records',{}))
    if event_descriptors:
        event_rows=hydrate_records(engine,event_descriptors,selected_budget)
    summary=selected_total.history() if scope else next((row for row in tree['projects'] if row['project_id']==project_id),{}) if project_id else tree['summary']
    path=[]
    if project_id:path.append(dict(title=next((row['title'] for row in tree['projects'] if row['project_id']==project_id),project_id),project_id=project_id))
    cursor=scope;chain=[]
    while cursor in tree['items']:
        item=tree['items'][cursor];chain.append({key:item[key] for key in ('title','home','sid','project_id')});cursor=tree['parent'].get(cursor)
    path.extend(reversed(chain))
    explorer=dict(path=path,view=view,session=scope,turn=turn,request_ordinal=request_ordinal,metadata=metadata,
        group={key:value for key,value in tree['rollups'].get(scope,{}).items() if key!='members'},project=project_id,
        summary=summary,child_count=len(tree['children'].get(scope,[])),records=records,
        parents=window(navigation,q.get('table_windows',{}).get('parents',{})),parent_kind='projects',partial_parents=False,
        available=sorted(available),detail=detail,timeline=timeline,detail_in_scope=detail_present,event=event,event_rows=event_rows)
    result=dict(key=engine.query_key(query),revision=engine.revision,filter_choices=choices,lookup={},
        analysis={**analysis,'sessions':[metadata] if metadata else [],'responses':event_rows},explorer=explorer)
    if call_index is not None:
        cached=HistoryProjection(engine,q,result,navigation,event_rows,call_index)
        if cached.weight()<=CACHE_BYTES:
            engine._prepared_history_projection=cached
            call_index.retained=True
    return result


class SelectionIndex:
    """A disposable on-disk scalar index, separate from the read-only source.

    SQLite's empty filename creates a private temporary file deleted on close.
    The maintained ordering index avoids external sort runs. Both table and
    index share the hard page limit; its cache is capped at 4 MiB.
    """
    def __init__(self,snapshot):
        self.snapshot=snapshot;self.db=sqlite3.connect('',isolation_level=None)
        self.count=self.bytes=0;self.retained=False;self.closed=False
        try:
            self.db.execute('PRAGMA temp_store=FILE')
            self.db.execute('PRAGMA cache_size=-4096')
            self.db.execute('PRAGMA temp.cache_size=-4096')
            self.db.execute('PRAGMA mmap_size=0')
            self.db.execute('PRAGMA journal_mode=OFF')
            self.db.execute('PRAGMA synchronous=OFF')
            page=self.db.execute('PRAGMA page_size').fetchone()[0]
            self.db.execute(f'PRAGMA max_page_count={SORT_BYTES//page}')
            self.db.create_collation('history_number',self.compare)
            self.db.execute('CREATE TABLE selected(ordinal INTEGER PRIMARY KEY,missing INTEGER NOT NULL,value TEXT COLLATE history_number NOT NULL,payload BLOB NOT NULL,member BLOB,anchor BLOB)')
            self.db.execute('CREATE INDEX selected_order ON selected(missing,value,ordinal)')
            self.db.execute('CREATE INDEX selected_member ON selected(member)')
            self.db.execute('CREATE INDEX selected_anchor ON selected(anchor)')
            self.bind(snapshot)
        except BaseException:
            self.db.close();raise

    @staticmethod
    def compare(left,right):
        # Numeric JSON retains integers beyond SQLite's signed integer domain.
        a,b=json.loads(left),json.loads(right)
        return (a>b)-(a<b)

    def execute(self,sql,args=()):
        self.snapshot._check()
        try:return self.db.execute(sql,args)
        except sqlite3.OperationalError as error:
            if 'full' in str(error):raise CapacityExceeded('History temporary order index exceeds 64 MiB') from error
            if 'interrupted' in str(error):raise ReadCancelled('History temporary index cancelled or expired') from error
            raise

    def bind(self,snapshot):
        self.snapshot=snapshot
        self.db.set_progress_handler(lambda:int(snapshot.closed or snapshot._expired()),1000)

    def add(self,missing,value,payload,member=None,anchor=None):
        encoded=pack(payload,self.snapshot.limits)
        membership=pack(member,self.snapshot.limits) if member is not None else None
        anchor=pack(anchor,self.snapshot.limits) if anchor is not None else None
        order=json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':'))
        size=len(encoded)+len(membership or b'')+len(anchor or b'')+len(order.encode('utf-8'))+96
        if self.bytes+size>SORT_BYTES:raise CapacityExceeded('History temporary identity input exceeds 64 MiB')
        self.execute('INSERT INTO selected VALUES(?,?,?,?,?,?)',(self.count,int(missing),order,encoded,membership,anchor))
        self.count+=1;self.bytes+=size
        return self.count-1

    def anchor(self,value):
        if value is None:return None
        row=self.execute('SELECT ordinal FROM selected WHERE anchor=? ORDER BY ordinal LIMIT 1',
                         (pack(tuple(value),self.snapshot.limits),)).fetchone()
        return row[0] if row else None

    def contains(self,value):
        return self.execute('SELECT 1 FROM selected WHERE member=? LIMIT 1',
                            (pack(value,self.snapshot.limits),)).fetchone() is not None

    def window(self,request,anchor):
        size=max(64,min(512,int(request.get('size',128))))
        start=max(0,min(int(request.get('start',0)),max(0,self.count-1)))
        if anchor is not None:
            at=self.execute('SELECT missing,value,ordinal FROM selected WHERE ordinal=?',(anchor,)).fetchone()
            rank=self.execute('SELECT count(*) FROM selected WHERE (missing,value,ordinal)<(?,?,?)',at).fetchone()[0]
            start=max(0,rank-int(request.get('anchor_offset',0)))
        cursor=self.execute('SELECT payload FROM selected ORDER BY missing,value,ordinal LIMIT ? OFFSET ?',(size,start))
        try:return start,[unpack(row[0],self.snapshot.limits) for row in cursor]
        finally:cursor.close()

    def rows(self):
        cursor=self.execute('SELECT payload FROM selected ORDER BY missing,value,ordinal')
        try:
            while True:
                self.snapshot._check();batch=cursor.fetchmany(128)
                if not batch:return
                for row in batch:yield unpack(row[0],self.snapshot.limits)
        finally:cursor.close()

    def __enter__(self):return self

    def __exit__(self,*exc):
        if not self.retained:self.close()

    def close(self):
        if not self.closed:
            self.db.close();self.closed=True


class _CallWindow:
    """Select during the aggregate pass; hydrate only the visible viewport."""
    def __init__(self,engine,total,detail,selection,request,budget,index):
        self.engine,self.total,self.detail=engine,total,detail
        self.selection,self.request,self.budget,self.index=selection,request,budget,index
        sort=selection.get('sort','ts_desc')
        self.field='cost' if sort.startswith('cost') else 'ts'
        self.descending=sort.endswith('desc')
        self.available=set();self.anchor=None;self.detail_present=False;self.points=[]

    def add(self,row):
        if not row.get('_outside'):self.total.add(row)
        fields=set(row['_available_fields']) if '_available_fields' in row else set(available_fields([row]))
        fields.update(name for name in ('_outside','_incident_phase') if row.get(name))
        for name in fields-self.available:self.budget.add(name,64);self.available.add(name)
        self.detail_present|=self.detail is not None and identity(row)==identity(self.detail)
        descriptor=dict(home=row['home'],sid=row['sid'],key=row['key'])
        for name in ('_outside','_incident_phase'):
            if name in row:descriptor[name]=row[name]
        value=row.get(self.field);order=-value if self.descending and isinstance(value,(int,float)) else value
        ordinal=self.index.add(value is None,0 if value is None else order,descriptor,identity(row),row_key(row))
        if self.request.get('anchor') and list(row_key(row))==self.request['anchor']:self.anchor=ordinal
        if self.selection.get('timeline') and self.selection.get('session'):
            self.points.append(self.budget.add(project_timeline([row])['rows'][0],16))

    def finish(self):
        count=self.index.count;start,selected=self.index.window(self.request,self.anchor)
        result=hydrate_records(self.engine,selected,self.budget)
        timeline=None
        if self.selection.get('timeline') and self.selection.get('session'):
            self.points.sort(key=lambda row:(row['ordinal'] is None,row['ordinal'] or 0,row['ts'] is None,row['ts'] or 0))
            timeline=dict(rows=self.points,total=count,omitted_timestamps=sum(row['ts'] is None for row in self.points))
        return dict(start=start,total=count,rows=result),timeline,self.available,self.detail_present


def selected_detail(engine,selection,budget):
    if not selection.get('call_identity'):return None
    detail=engine.record(*selection['call_identity'])
    if detail is not None:
        compact=compact_record(detail)
        budget.hydrated={identity(compact):budget.add(compact,96)}
    return detail


def hydrate_records(engine,selected,budget):
    if not hasattr(budget,'hydrated'):budget.hydrated={}
    hydrated=budget.hydrated
    byowner=defaultdict(list)
    for descriptor in selected:
        key=(descriptor['home'],descriptor['sid'],descriptor['key'])
        retained=getattr(engine.snapshot,'history_records',None)
        if key not in hydrated and retained is not None:
            row=retained.get(key)
            if row is not None:hydrated[key]=budget.add(row,96)
        if key not in hydrated:
            byowner[(descriptor['home'],descriptor['sid'])].append(descriptor['key'])
    for owner,keys in byowner.items():
        for batch in batches(keys):
            for row in engine._pages(engine.snapshot.records,owner,batch):
                row=compact_record(row);budget.add(row,96);hydrated[(*owner,row['key'])]=row
    result=[]
    for descriptor in selected:
        row=hydrated.get((descriptor['home'],descriptor['sid'],descriptor['key']))
        if row is None:raise NotReady('History selected record disappeared inside its pinned snapshot')
        result.append(dict(row,**{k:v for k,v in descriptor.items() if k.startswith('_')}))
    return result
