"""Incremental, UI-independent analytical state owned by one worker process."""
from bisect import bisect_left
from collections import OrderedDict, defaultdict
from datetime import datetime
from .analytics import analyze, comparison_view, overview_view, stats, effort_key, prepare_row, project_matches, observed_choices, call_identity, annotate_incidents, session_summary
from .cache_misses import classify
from .cache_health import CacheHealth
from .core import TRANSPORT_FIELDS, summarize, METRICS
from .model_evidence import FIELDS as MODEL_FIELDS
from .pricing import token_cost, sum_cost, RATES, FAST_RATES, ALIASES, VERIFIED, PRICE_POLICY, request_tier, display_tier, unknown_mode_calls


class BoundedCache:
    def __init__(self,entries=8,rows=160000):
        self.data=OrderedDict()
        self.entries,self.rows=entries,rows
        self.weight=0
    def get(self,key):
        if key not in self.data: return None
        self.data.move_to_end(key)
        return self.data[key][0]
    def put(self,key,value,weight=1):
        if key in self.data: self.weight-=self.data.pop(key)[1]
        if weight>self.rows: return
        self.data[key]=(value,weight)
        self.weight+=weight
        while len(self.data)>self.entries or self.weight>self.rows:
            _,(_,old)=self.data.popitem(last=False)
            self.weight-=old
    def clear(self):
        self.data.clear(); self.weight=0


def row_signature(row):
    return tuple(row.get(k) for k in ('key','ts','turn','model','effort','input','cached','written','output','reasoning','total','service_tier',
        'reported_total','total_discrepancy','input_conflict','output_conflict','purpose','compaction_epoch') + TRANSPORT_FIELDS + MODEL_FIELDS)


class AnalysisEngine:
    def __init__(self):
        self.sessions=OrderedDict()
        self.parts=BoundedCache(2048,160000)
        self.results=BoundedCache(6,160000)
        self.statistics=BoundedCache(2048,160000)
        self.summaries=BoundedCache(64,160000)
        self.revision=0
        self.generation=0
        self.pricing_signature=None
        self.timestamps=[]
        self.metrics={'priced_calls':0,'session_rebuilds':0,'part_builds':0,'view_builds':0,'cache_hits':0}

    def ingest(self,sessions):
        price_signature=(VERIFIED,PRICE_POLICY,tuple(RATES.items()),tuple(FAST_RATES.items()),tuple(ALIASES.items()))
        # A collector revision covers corrections as well as append. Unversioned
        # callers retain the full fingerprint path.
        fast=tuple(((s['home'],s['id']),s.get('usage_revision'),
                    tuple(s.get(k) for k in ('title','cwd','project','project_name','source','archived','collection_complete','pending','parent_thread_id','agent_path','agent_nickname','spawn_depth')),
                    repr(s.get('request_state',{})),repr(s.get('turn_records',{})),
                    repr(s.get('coverage_gaps',[])),repr(s.get('turn_states',{})),repr(s.get('unclassified')))
                   for s in sessions) if all(s.get('usage_revision') is not None for s in sessions) else None
        if fast is not None and fast==getattr(self,'_ingest_key',None) and price_signature==self.pricing_signature:
            for s in sessions:self.sessions[(s['home'],s['id'])]['source']=s
            return False
        self._ingest_key=fast
        owners={}
        for source in sorted(sessions,key=lambda s:(s['home'],s['id'])):
            for row in source['history']:
                owners.setdefault(call_identity(source['home'],source['id'],row.get('key')),(source['home'],source['id']))
        price_signature=(VERIFIED,PRICE_POLICY,tuple(RATES.items()),tuple(FAST_RATES.items()),tuple(ALIASES.items()))
        reprice=price_signature!=self.pricing_signature
        self.pricing_signature=price_signature
        changed=False
        updated=OrderedDict()
        for source in sessions:
            key=(source['home'],source['id'])
            old=self.sessions.get(key)
            revision=source.get('usage_revision')
            owned_history=[r for r in source['history'] if owners[call_identity(source['home'],source['id'],r.get('key'))]==key]
            metadata=tuple(source.get(k) for k in ('title','cwd','project','project_name','source','archived','collection_complete','pending','parent_thread_id','agent_path','agent_nickname','spawn_depth'))+(tuple(source.get('request_state',{}).items()),
                tuple(r.get('key') for r in owned_history),
                repr(source.get('turn_records',{})),repr(source.get('coverage_gaps',[])))
            fingerprint=(metadata,revision if revision is not None else tuple(row_signature(r) for r in source['history']),
                         tuple(sorted(source.get('turn_states',{}).items())),tuple(sorted((source.get('unclassified') or {}).items())))
            if old and old['fingerprint']==fingerprint and not reprice:
                # Diagnostics change independently of usage/metadata caches.
                old['source']=source
                updated[key]=old
                continue
            changed=True
            self.generation+=1
            previous=old['records'] if old and not reprice else {}
            misses=classify(owned_history)
            miss_keys={e['key'] for e in misses['events']}
            records={}
            history=[]
            for i,r in enumerate(sorted(owned_history,key=lambda row:row['ts'])):
                identity=r.get('key') or (r.get('turn'),r['ts'],i)
                if identity in records:continue
                signature=row_signature(r)
                state=source.get('turn_states',{}).get(r.get('turn'),'미확인')
                cached=previous.get(identity)
                is_miss=r.get('key') in miss_keys
                context=(signature,source['title'],source.get('cwd'),source.get('project'),source.get('project_name'),source.get('source'),state,is_miss,i)
                if cached and cached[0]==context:
                    row=cached[1]
                else:
                    price_keys=('model','requested_model','model_conflict','mode_conflict','input','cached','written','output','reasoning','service_tier','input_conflict')
                    if cached and all(cached[1].get(k)==r.get(k) for k in price_keys):
                        price={k:v for k,v in cached[1].items() if k.startswith(('cost','price_')) or k=='long_context'}
                    else:
                        price=None
                        self.metrics['priced_calls']+=1
                    row=prepare_row(r,source,price)
                    row.update(cache_miss=is_miss,session_ordinal=i+1)
                records[identity]=(context,row)
                history.append(row)
            health=CacheHealth().update(history)
            history=annotate_incidents(history,health)
            for row in history:
                identity=row['key']
                records[identity]=(records[identity][0],row)
            whole_turns=defaultdict(list)
            for row in history:
                if row.get('turn'):whole_turns[(source['home'],source['id'],row['turn'])].append(row)
            prepared={**source,'history':history,'_prepared':True,'cache_misses':misses,'cache_health':health}
            boundaries=sorted(value for turn in source.get('turn_records',{}).values()
                              for value in (turn.get('started_at'),turn.get('ended_at')) if value is not None)
            updated[key]={'source':source,'prepared':prepared,'records':records,'fingerprint':fingerprint,
                          'revision':self.generation,'timestamps':sorted(r['ts'] for r in history),'boundaries':boundaries,'whole_turns':dict(whole_turns)}
            self.metrics['session_rebuilds']+=1
        if set(updated)!=set(self.sessions): changed=True
        self.sessions=updated
        if changed:
            self.revision+=1
            self.timestamps=sorted(r['ts'] for s in updated.values() for r in s['prepared']['history'])
            # Old result objects may remain on screen, but cannot answer a newer revision.
            self.results.clear()
        return changed

    def record(self,home,sid,response_id):
        state=self.sessions.get((home,sid))
        found=state['records'].get(response_id) if state else None
        return dict(found[1]) if found else None

    def stat(self,rows,key,method='mean'):
        signature=(key,method,tuple(map(id,rows)))
        cached=self.statistics.get(signature)
        if cached is not None: return cached[1]
        value=stats(rows,key,method)
        self.statistics.put(signature,(tuple(rows),value),len(rows)+1)
        return value

    def summary(self,rows):
        key=tuple(map(id,rows));cached=self.summaries.get(key)
        if cached is not None:return cached[1]
        value=session_summary(rows);self.summaries.put(key,(tuple(rows),value),len(rows)+1)
        return value

    def query_key(self,q):
        def freeze(value):
            if isinstance(value,dict):return tuple((k,freeze(v)) for k,v in sorted(value.items()))
            if isinstance(value,(list,tuple)):return tuple(map(freeze,value))
            return value
        # Request boundaries and partial buckets can change without call membership.
        return (self.revision,q['page'],freeze(q))

    def query(self,q):
        key=self.query_key(q)
        cached=self.results.get(key)
        if cached is not None:
            self.metrics['cache_hits']+=1
            return cached
        self.metrics['view_builds']+=1
        start,end=q['start'],q['end']
        selected=[]; responses=[]; turns=[]; unclassified=[]
        bysession={}; byturn={}; session_turns={}; session_totals={};whole_turns=defaultdict(list);whole_sessions={}
        model=q.get('model','') if q['page']!=1 else ''
        effort=q.get('effort','') if q['page']!=1 else ''
        mode=q.get('service_tier','') if q['page']!=1 else ''
        for session_key,state in self.sessions.items():
            s=state['prepared']
            if not q.get('archived',True) and s.get('archived'): continue
            if q.get('source') and s.get('source','unknown')!=q['source']: continue
            if q.get('home') and s['home']!=q['home']:continue
            if q.get('sid') and s['id']!=q['sid']:continue
            if q.get('project') and not project_matches(s,q['project']):continue
            selected.append({k:v for k,v in s.items() if k not in ('history','requests','groups','_prepared')})
            if q.get('include_whole_history',True):whole_turns.update(state['whole_turns'])
            if s.get('unclassified'): unclassified.append(s['unclassified'])
            interval=(bisect_left(state['timestamps'],start),bisect_left(state['timestamps'],end))
            boundaries=state['boundaries']
            request_interval=(bisect_left(boundaries,start),bisect_left(boundaries,end))
            part_key=(session_key,state['revision'],interval,request_interval,model,effort,mode)
            part=self.parts.get(part_key)
            if part is None:
                part=analyze([s],start,end,model,with_comparisons=False,service_tier=mode,effort=effort)
                part['_sorted_responses']=sorted(part['responses'],key=lambda r:r['ts'])
                part['_sorted_turns']=sorted(part['turns'],key=lambda t:t['ts'],reverse=True)
                lookup=defaultdict(list)
                for row in part['_sorted_responses']:
                    if row.get('turn'): lookup[(row['home'],row['sid'],row['turn'])].append(row)
                part['_turn_lookup']=dict(lookup)
                self.parts.put(part_key,part,len(s['history'])+1)
                self.metrics['part_builds']+=1
            responses.extend(part['responses']); turns.extend(part['turns'])
            if part['responses']:
                bysession[session_key]=part['_sorted_responses']
                session_totals[session_key]=part['totals']
            byturn.update(part['_turn_lookup'])
            session_turns[session_key]=part['_sorted_turns']
        unit=q.get('unit','response') if q['page']!=0 else 'response'
        units=responses if unit in ('response','call') else [t for t in turns if t['complete']]
        basis=[]
        if q['page']==1:
            groups=defaultdict(list)
            for t in turns:
                if t['complete'] and t['cost'] is not None: groups[(t['model'],t['effort'],display_tier(t))].append(t)
            for (m,e,tier),ts in groups.items():
                rs=[r for t in ts for r in t['calls']]
                basis.append({'model':m,'effort':e,'service_tier':tier,'turns':len(ts),'calls':len(rs),'total_cost':sum(t['cost'] for t in ts),
                              'unknown_mode_calls':sum(unknown_mode_calls(r) for r in rs),
                              'call_cost':self.stat(rs,'cost',q.get('method','mean'))['value'],
                              'turn_cost':self.stat(ts,'cost',q.get('method','mean'))['value']})
            basis.sort(key=lambda r:(r['model'],effort_key(r['effort']),r['service_tier']))
        a={'sessions':selected,'responses':responses,'turns':turns,'units':units,'groups':[],
           'totals':{**summarize(units),**sum_cost(units)},'basis':basis,
           'priced_count':sum(r['cost'] is not None for r in units),'unpriced_count':sum(r['cost'] is None for r in units),
           'missing_turn_responses':sum(not r.get('turn') for r in responses),'excluded_turns':sum(not t['complete'] for t in turns),
           'turn_count':len(turns),'unclassified':summarize(unclassified),'unclassified_sessions':len(unclassified)}
        result={'key':key,'analysis':a,'lookup':{'sessions':dict(bysession),'turns':dict(byturn),'session_turns':dict(session_turns),
                'whole_turns':dict(whole_turns)},
                'revision':self.revision}
        result['filter_choices']=observed_choices(responses)
        if q['page']==0: result['overview']=overview_view(a,start,end,q.get('metric','cost'),q.get('granularity','auto'),q.get('basis','total'),q.get('group_by','project'),self.stat,self.summary)
        if q['page']==1: result['comparison']=comparison_view(a,q.get('metric','cost'),q.get('band'),self.stat,q.get('targets'),q.get('baseline','A'),
            unit,q.get('method','mean'),q.get('conditions'),q.get('matrix_by','input'),q.get('comparison_type',''))
        self.results.put(key,result,len(responses)+len(turns)+1)
        return result
