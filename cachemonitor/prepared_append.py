"""Certified owner tails in the existing prepared facts and owner checkpoint.

Admission is read-only. A rejected transition leaves the complete-owner path
responsible for preparation. An admitted plan is published by the caller's
ordinary Stage; no source or canonical history is retained in this checkpoint.
"""
from collections import Counter, deque
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite

from .analysis_records import compact_record
from .analytics import finite, observation_flags, output_speed, prepare_row, effort_key
from .cache_health import CacheHealth, segment
from .core import token_number, token_parts
from .overlay_data import (COST_PARTS, TOKEN_LABELS, TOKEN_ORDER, _cost_view,
                           _token_view, assumed_cost, call_summary,
                           _summary_view, _request_view)
from .prepared_health import FORMAT as HEALTH_FORMAT, FIELDS as HEALTH_FIELDS, _head
from .prepared_store import (CapacityExceeded, RebuildRequired, OWNER_FIELDS,
                             FORBIDDEN_OWNER, pack, _size)
from .prepared_summary import summary_for_storage, recent_references
from .prepared_sums import _Sum


FORMAT = ('prepared-owner-append', 1)
EVENT_FORMAT = ('append-health-event', 1)
TAIL_ROWS = 128
CHECKPOINT_BYTES = 64 * 1024
PLAN_BYTES = 16 * 1024 * 1024
HEALTH_FIELDS_ROW = ('key', 'ts', 'input', 'cached', 'model', 'effort',
    'service_tier', 'service_tier_source', 'mode_conflict', 'cache_policy',
    'compaction_epoch', 'observation_missing', 'cache_policy_conflict',
    'model_conflict', 'input_conflict')
HOME_FIELDS = ('cost', 'input', 'cached', 'output', 'reasoning')


class AppendUnavailable(Exception):
    """A read-only admission refusal requiring ordinary complete preparation."""


def _require(value, reason):
    if not value:
        raise AppendUnavailable(reason)


def _pages(method, *args, **kwargs):
    after = None
    while True:
        page = method(*args, after=after, **kwargs)
        yield from page.rows
        if page.complete:
            return
        after = page.after


def _sum_state(value):
    return value.total, value.correction, value.phase


def _sum_load(value):
    result = _Sum()
    result.total, result.correction, result.phase = value
    return result


def _add_sum(values, name, value):
    total = _sum_load(values[name])
    total.add(value)
    values[name] = _sum_state(total)


def _value(values, name):
    return _sum_load(values[name]).value()


def _new_totals():
    return dict(count=0, sums={key:_sum_state(_Sum()) for key in
                ('cost', 'maintenance', 'speed_output', 'speed_seconds')},
        priced=0, component_priced=0, maintenance_calls=0, maintenance_missing=0,
        speed_n=0, assumed=0, mismatch=0, observation_missing=0, observation_conflict=0,
        home_values=[0]*len(HOME_FIELDS), home_missing=[0]*len(HOME_FIELDS),
        minimum=None, maximum=None,
        token=dict(counts=dict.fromkeys(TOKEN_LABELS,0), known=dict.fromkeys(TOKEN_ORDER,False),
            inp_total=0, out_total=0, partial=False, input_known=False, output_known=False,
            written_known=True, cache_input=0, cache_read=0, cache_valid=0),
        costs={side:dict.fromkeys((key for key,_ in fields),0.) for side,fields in COST_PARTS})


def _add_totals(total, row):
    total['count'] += 1
    total['minimum'] = row['ts'] if total['minimum'] is None else min(total['minimum'],row['ts'])
    total['maximum'] = row['ts']
    for i, field in enumerate(HOME_FIELDS):
        value = row.get(field)
        if value is None:total['home_missing'][i] += 1
        else:total['home_values'][i] += value
    cost = row.get('cost')
    if finite(cost):
        total['priced'] += 1
        _add_sum(total['sums'],'cost',cost)
    if cost is not None:
        total['component_priced'] += 1
        for side, fields in COST_PARTS:
            counts = total['costs'][side]
            if side == 'output' and (row.get('output_conflict') or
                    any(row.get(key) is None for key in ('cost_reasoning','cost_non_reasoning'))):
                counts['output_unknown'] += row.get('cost_output') or 0
            else:
                for key, field in fields:counts[key] += row.get(field) or 0
    if row.get('purpose') == 'maintenance':
        total['maintenance_calls'] += 1
        if cost is None:total['maintenance_missing'] += 1
        else:_add_sum(total['sums'],'maintenance',cost)
    if output_speed(row) is not None:
        total['speed_n'] += 1
        _add_sum(total['sums'],'speed_output',row['output'])
        _add_sum(total['sums'],'speed_seconds',row['completion_latency_ms']/1000)
    total['assumed'] += assumed_cost(row)
    total['mismatch'] += bool(row.get('model_alert_confirmed'))
    flags = observation_flags(row)
    total['observation_missing'] += flags['missing']
    total['observation_conflict'] += flags['conflict']
    token = total['token']
    parts = token_parts(row)
    for side, mapping in (('input',{'ordinary':'uncached','read':'cached','write':'written','unclassified':'input_unknown'}),
                          ('output',{'ordinary':'output','reasoning':'reasoning','unclassified':'output_unknown'})):
        for key,destination in mapping.items():token['counts'][destination] += parts[side][key]
    inp,cached,written,out,reasoning = (token_number(row.get(key)) for key in
                                       ('input','cached','written','output','reasoning'))
    if inp is not None:token['inp_total'] += inp
    if out is not None:token['out_total'] += out
    token['input_known'] |= inp is not None
    token['output_known'] |= out is not None
    if inp is not None and cached is not None and cached <= inp:
        token['cache_input'] += inp
        token['cache_read'] += cached
        token['cache_valid'] += 1
    token['partial'] |= inp is None or out is None
    token['written_known'] &= written is not None
    valid_input = (inp is not None and not row.get('input_conflict') and
        (cached is None or cached <= inp) and (written is None or written <= inp) and
        (cached is None or written is None or cached+written <= inp))
    valid_output = out is not None and reasoning is not None and reasoning <= out and not row.get('output_conflict')
    token['known']['cached'] |= valid_input and cached is not None
    token['known']['written'] |= valid_input and written is not None
    token['known']['uncached'] |= valid_input and cached is not None and written is not None
    token['known']['output'] |= valid_output
    token['known']['reasoning'] |= valid_output


def _new_request(turn):
    return dict(turn=turn,count=0,priced=0,assumed=0,cost=_sum_state(_Sum()),latest=None,latest_ts=None)


def _add_request(value,row):
    value['count'] += 1
    if row.get('cost') is not None:
        value['priced'] += 1
        total = _sum_load(value['cost']);total.add(row['cost']);value['cost'] = _sum_state(total)
    value['assumed'] += assumed_cost(row)
    if value['latest'] is None or row['ts']>value['latest_ts']:
        value['latest'],value['latest_ts'] = row['key'],row['ts']


def _eligible(row):
    return row.get('purpose') not in ('maintenance','diagnostic')


def _miss(row):
    return _eligible(row) and type(row.get('input')) is int and row['input'] > 0 and type(row.get('cached')) is int and row['cached'] == 0


def _add_miss(state,row):
    if not _eligible(row):return None
    if not state['eligible']:state['first_key'] = row.get('key')
    state['eligible'] += 1
    state['last_key'] = row.get('key')
    if not _miss(row):return None
    event = dict(key=row.get('key'),ts=row['ts'],input=row.get('input'))
    state['count'] += 1;state['input'] += event['input'];state['last_miss'] = row.get('key')
    return event


def _miss_head(state):
    return dict(count=state['count'],input=state['input'],input_missing=0,
                first_key=state['first_key'],current=bool(state['eligible'] and state['count'] and state['last_key']==state['last_miss']))


class _Keys:
    def __init__(self,count=0):self.count=count;self.added=[]
    def __len__(self):return self.count
    def append(self,key):self.count+=1;self.added.append(key)


def _health_row(row):
    return {key:row[key] for key in HEALTH_FIELDS_ROW if key in row} | {'_ordinal':row['session_ordinal']}


class _Health:
    """Reuse CacheHealth.consume without retaining prior rows or event arrays."""
    def __init__(self,saved=None):
        self.reducer=CacheHealth();self.recent=deque(maxlen=5)
        self.changed={};self.members={};self.annotations={};self.incident_positions={}
        if saved is None:return
        r=self.reducer
        for key in ('scope','count','recovery','candidate','missing','state','reason','last'):
            setattr(r,key,deepcopy(saved[key]))
        r.baseline=deque(deepcopy(saved['baseline']),maxlen=5)
        r.candidate_rows=deepcopy(saved['candidate_rows']);r.recovery_rows=deepcopy(saved['recovery_rows'])
        r.incident=deepcopy(saved['incident'])
        if r.incident is not None:r.incident['occurrence_keys']=_Keys(saved['occurrences'])
        self.incident_positions=dict(saved['incident_positions'])
        self.recent.extend(deepcopy(saved['recent']))

    def add(self,row,*,capture=True):
        if not _eligible(row):return
        r=self.reducer;value=_health_row(row)
        previous=r.incident
        r.consume(value);self.recent.append(value)
        event=r.incident
        if event is not None:
            if not isinstance(event['occurrence_keys'],_Keys):
                keys=event['occurrence_keys'];event['occurrence_keys']=_Keys()
                for key in keys:event['occurrence_keys'].append(key)
            keys=event['occurrence_keys']
            rows=[value,*r.baseline,*r.candidate_rows,*r.recovery_rows,*self.recent]
            positions={item['key']:item['_ordinal'] for item in rows}
            if event is not previous:
                self.incident_positions={key:positions[key] for key in event['baseline_keys']}
            positions.update(self.incident_positions)
            if capture:
                for key in keys.added:
                    self.members.setdefault(event['id'],[]).append((key,positions[key]))
                    self.annotations[key]=event['id']
                # Recovery updates the active event even when it adds no occurrence.
                if keys.added or r.recovery_rows or event is not previous:
                    self.changed[event['id']]=dict(event=event,ordinal=r.count-1,
                        baseline_ordinals=[(key,positions[key]) for key in event['baseline_keys']],
                        recovery_ordinals=[(key,positions[key]) for key in event['recovery_keys']])
            keys.added.clear()
        r.events.clear()

    def save(self):
        r=self.reducer
        incident=({key:deepcopy(value) for key,value in r.incident.items() if key!='occurrence_keys'}
                  if r.incident is not None else None)
        return dict(**{key:deepcopy(getattr(r,key)) for key in
                    ('scope','count','recovery','candidate','missing','state','reason','last')},
            baseline=list(r.baseline),candidate_rows=r.candidate_rows,recovery_rows=r.recovery_rows,
            incident=incident,occurrences=len(r.incident['occurrence_keys']) if r.incident else 0,
            incident_positions=tuple(self.incident_positions.items()),recent=list(self.recent))

    def head(self):
        r=self.reducer;recent=[]
        for row in reversed(self.recent):
            if segment(row)!=r.scope:break
            recent.append(row)
        valid=[row for row in recent if r.valid(row)];total=sum(row['input'] for row in valid)
        incident=_event_fixed(r.incident) if r.incident else None
        return dict(state=r.state,reason=r.reason,incident=_head(incident) if incident else None,
            incidents=r.count,recent_rate=sum(row['cached'] for row in valid)/total*100 if total else None,
            valid=len(valid),sample=len(recent),partial=len(valid)!=len(recent),provisional=True)


def _event_fixed(event):
    return {key:deepcopy(value) for key,value in event.items()
            if key not in ('occurrence_keys','baseline_calls','comparison_calls','recovery_calls')}


def _member_section(event_id):
    return (EVENT_FORMAT,'occurrences',str(event_id))


def _descriptor(event,baseline,recovery,*,evidence=None):
    fields=('baseline_calls','comparison_calls','recovery_calls')
    return dict(append_event=EVENT_FORMAT,event=_event_fixed(event),
        occurrence_count=len(event['occurrence_keys']),occurrence_section=_member_section(event['id']),
        baseline_ordinals=baseline,recovery_ordinals=recovery,
        evidence_fields=tuple(name for name in fields if evidence is True or evidence is None and name in event))


def capture_health(tracker,records):
    """Capture the existing full classifier once; no second classification pass."""
    incident=_event_fixed(tracker.incident) if tracker.incident is not None else None
    value={key:deepcopy(getattr(tracker,key)) for key in
           ('scope','count','recovery','candidate','missing','state','reason')}
    value.update(last=_health_row(tracker.last) if tracker.last is not None else None,
        baseline=[_health_row(row) for row in tracker.baseline],
        candidate_rows=[_health_row(row) for row in tracker.candidate_rows],
        recovery_rows=[_health_row(row) for row in tracker.recovery_rows],
        recent=[_health_row(row) for row in tracker.rows[-5:]],incident=incident,
        occurrences=len(tracker.incident['occurrence_keys']) if tracker.incident is not None else 0,
        incident_positions=tuple((key,records[key][1]['session_ordinal'])
            for key in tracker.incident['baseline_keys']) if tracker.incident is not None else ())
    return value


def reduce_full(source):
    """One scalar pass feeds both the full prepared summary and append seed."""
    total=_new_totals();request=_new_request((source.get('request_state') or {}).get('turn'))
    misses=dict(count=0,input=0,eligible=0,first_key=None,last_key=None,last_miss=None)
    latest={};mismatch=None
    for row in source['history']:
        _add_totals(total,row);_add_miss(misses,row)
        if request['turn'] and row.get('turn')==request['turn']:
            _add_request(request,row)
            if request['latest']==row['key']:latest=row
        if row.get('model_alert_confirmed'):mismatch=row['key']
    return dict(total=total,request=request,misses=misses,request_latest=latest,latest_mismatch=mismatch)


def summary_facts(state,health_count):
    total=state['total'];request=state['request'];tokens=total['token'];calls=total['count'];priced=total['priced']
    cost=_value(total['sums'],'cost') if priced or not calls else None
    seconds=_value(total['sums'],'speed_seconds');speed_output=_value(total['sums'],'speed_output')
    composition=_token_view(deepcopy(tokens['counts']),deepcopy(tokens['known']),
        tokens['inp_total'],tokens['out_total'],tokens['partial'],tokens['input_known'],tokens['output_known'],
        tokens['cache_input'],tokens['cache_read'],tokens['cache_valid'],calls,tokens['written_known'] and bool(calls))
    return dict(cost=dict(sum=cost,mean=cost/priced if priced else None,n=priced,missing=calls-priced),
        calls=calls,tokens=composition,assumed=total['assumed'],
        request=dict(calls=request['count'],cost=_sum_load(request['cost']).value() if request['priced'] else None,
            priced=request['priced'],assumed=request['assumed'],partial=request['count']>request['priced']),
        request_latest=state['request_latest'],mismatch_count=total['mismatch'],mismatch_key=state['latest_mismatch'],
        observation_missing=total['observation_missing'],observation_conflict=total['observation_conflict'],
        degradation_count=health_count,
        speed=dict(value=speed_output/seconds if seconds else None,output=speed_output,seconds=seconds,
            n=total['speed_n'],N=calls,missing=calls-total['speed_n']),
        cost_composition=_cost_view(total['costs'],total['component_priced'],calls),
        partials=dict(cache_input=tokens['cache_input'],cache_read=tokens['cache_read'],
            maintenance_calls=total['maintenance_calls'],maintenance_missing=total['maintenance_missing'],
            maintenance_cost=_value(total['sums'],'maintenance') if total['maintenance_calls']>total['maintenance_missing'] else None))


def home_totals(owner,total):
    from .prepared_home import FORMAT as HOME_FORMAT
    return dict(format=HOME_FORMAT,owner=tuple(owner),fields=HOME_FIELDS,count=total['count'],
        minimum=total['minimum'],maximum=total['maximum'],values=tuple(total['home_values']),missing=tuple(total['home_missing']))


def seed(material,dependencies,*,raw_count,mode_evidence=None):
    """Add a bounded checkpoint during complete preparation, or decline reuse."""
    source=material['state']['prepared'];rows=source['history'];owner=(source['home'],source['id'])
    if (material['internal_review'] or source.get('parent_thread_id') or not rows or
            len(rows)!=raw_count or any(row['session_ordinal']!=i for i,row in enumerate(rows,1))):
        return False
    raw=material['state']['source']['history']
    if any(a.get('key')!=b['key'] or a['ts']!=b['ts'] for a,b in zip(raw,rows)):
        return False
    from .collection_append import context_digest
    context=context_digest(material['state']['source'])
    if context is None:return False
    if any(not row.get('key') for row in rows):return False
    reduced=material['append_state']
    total=reduced['total'];request=reduced['request'];misses=reduced['misses']
    from .prepared_ingest import request_headers
    last_order=None;headers=0
    for turn,header in request_headers(source):
        if header['display_ordinal']:
            headers+=1;last_order=(header['effective_at'] if header['effective_at'] is not None else float('inf'),turn)
    checkpoint=dict(format=FORMAT,owner=owner,source_digest=dependencies['source_digest'],
        candidate_source_digest=dependencies.get('candidate_source_digest'),
        dependencies={key:dependencies[key] for key in ('pricing','classifier','preparation')},
        mode_evidence=mode_evidence,raw_count=raw_count,frontier=(rows[-1]['ts'],rows[-1]['key']),
        context_digest=context,
        total=total,misses=misses,health=material['state']['health_checkpoint'],request=request,
        request_count=len(source.get('turn_records',{})),header_count=headers,last_header_order=last_order)
    try:
        encoded=pack(checkpoint,maximum=CHECKPOINT_BYTES)
    except (CapacityExceeded,ValueError,TypeError):
        return False
    material['append_checkpoint']=checkpoint
    material['append_checkpoint_bytes']=len(encoded)
    material['append_sections']=seed_sections(material)
    return True


def seed_sections(material):
    """Replace full health blobs with lossless membership sections on full ingest."""
    own=material['summary']['own'];records=material['state']['records']
    def positions(keys):return [(key,records[key][1]['session_ordinal']) for key in keys]
    for ordinal,event in enumerate(own['cache_health']['events']):
        descriptor=_descriptor(event,positions(event['baseline_keys']),positions(event['recovery_keys']))
        for index,key in enumerate(event['occurrence_keys']):
            yield (_member_section(event['id']),index,index,None,(key,records[key][1]['session_ordinal']))
        yield ((HEALTH_FORMAT,'event-id'),str(event['id']),ordinal,event.get('ts'),descriptor)
    for field in HEALTH_FIELDS:
        value=own[field];manifest=dict(value={key:item for key,item in value.items() if key not in ('events','incident')})
        manifest['events']=dict(kind='tuple' if type(value['events']) is tuple else 'list',count=len(value['events']))
        for ordinal,event in enumerate(value['events']):
            descriptor=(dict(inline=event) if field=='cache_misses' else
                        _descriptor(event,positions(event['baseline_keys']),positions(event['recovery_keys'])))
            yield ((HEALTH_FORMAT,field,'events'),ordinal,ordinal,event.get('ts'),dict(descriptor,head=_head(event)))
        if 'incident' in value:
            event=value['incident']
            manifest['incident']=(_descriptor(event,positions(event['baseline_keys']),positions(event['recovery_keys'])) if event else None)
        yield (HEALTH_FORMAT,field,0,None,manifest)


def prepare_full(material,dependencies,*,mode_evidence=None):
    """Small stage_owner hook; cold preparation already owns these canonical rows."""
    from .prepared_health import compact_owner_health,split_summary
    if not seed(material,dependencies,raw_count=material['raw_count'],mode_evidence=mode_evidence):
        summary,sections=split_summary(material['summary'])
        return summary_for_storage(summary),sections,None
    own=material['summary']['own']
    compact=dict(own,**compact_owner_health(own),
        _health_ref=dict(format=HEALTH_FORMAT,owner=(own['home'],own['id'])))
    summary=dict(material['summary'],own=compact)
    return summary_for_storage(summary),material['append_sections'],material['append_checkpoint']


def _evidence(row,ordinal):
    inp,cached=token_number(row.get('input')),token_number(row.get('cached'))
    return dict(key=row['key'],ordinal=ordinal,ts=row.get('ts'),input=inp,cached=cached,
                cache_rate=cached/inp*100 if inp and cached is not None and cached<=inp else None)


def read_event(snapshot,owner,descriptor):
    """Only selected health detail reconstructs growing membership arrays."""
    if descriptor.get('append_event')!=EVENT_FORMAT:raise RebuildRequired('unknown append health descriptor')
    result=deepcopy(descriptor['event']);result['occurrence_keys']=[]
    fields=descriptor['evidence_fields']
    if 'comparison_calls' in fields:result['comparison_calls']=[]
    size=_size(result);count=descriptor['occurrence_count']
    if type(count) is not int or count<0:raise RebuildRequired('invalid health occurrence count')
    def evidence(pairs):
        if not pairs:return []
        keys=[key for key,_ in pairs]
        found={row['key']:row for row in _pages(snapshot.records,owner,keys)}
        if len(found)!=len(set(keys)):raise RebuildRequired('health member call is missing')
        return [_evidence(found[key],ordinal) for key,ordinal in pairs]
    group=[];seen=0
    for item in _pages(snapshot.section,owner,descriptor['occurrence_section']):
        if item['ordinal']!=seen or seen>=count:raise RebuildRequired('health membership ordering differs')
        key,ordinal=item['value'];seen+=1;group.append((key,ordinal))
        size+=_size(item['value'])+128
        if size>snapshot.limits.page_objects_bytes:raise CapacityExceeded('selected health membership exceeds object budget')
        if len(group)==128:
            values=evidence(group) if 'comparison_calls' in fields else [];size+=_size(values)
            if size>snapshot.limits.page_objects_bytes:raise CapacityExceeded('selected health evidence exceeds object budget')
            result['occurrence_keys'].extend(key for key,_ in group)
            if 'comparison_calls' in fields:result['comparison_calls'].extend(values)
            group=[]
    if seen!=count:raise RebuildRequired('health membership is incomplete')
    if group:
        values=evidence(group) if 'comparison_calls' in fields else [];size+=_size(values)
        result['occurrence_keys'].extend(key for key,_ in group)
        if 'comparison_calls' in fields:result['comparison_calls'].extend(values)
    for name,positions in (('baseline_calls','baseline_ordinals'),('recovery_calls','recovery_ordinals')):
        if name in fields:result[name]=evidence(descriptor[positions])
    if size+_size(result.get('baseline_calls',()))+_size(result.get('recovery_calls',()))>snapshot.limits.page_objects_bytes:
        raise CapacityExceeded('selected health evidence exceeds object budget')
    return result


def _headers(snapshot,owner,checkpoint,transition):
    """Patch touched whole-request facts without renumbering prior requests."""
    requests=transition['requests']['upserts'];states=dict(transition['turns']['upserts'])
    turns=set(states)|{turn for turn,_,_ in requests}|{r['turn'] for r in transition['tail'] if r.get('turn')}
    current=(transition['metadata'].get('request_state') or {}).get('turn')
    if current:turns.add(current)
    _require(len(turns)<=TAIL_ROWS,'too many changed request identities')
    before={value['key']:value for value in _pages(snapshot.request_headers,owner,tuple(turns),with_first=True)} if turns else {}
    boundaries={}
    request_count=checkpoint['request_count']
    for turn,ordinal,value in requests:
        old=snapshot.request(owner,turn)
        if old is None:
            _require(ordinal==request_count,'request insertion changes prior map ordering')
            request_count+=1
        else:
            _require(old['ordinal']==ordinal,'request ordinal changed')
        boundaries[turn]=value
    result={}
    for turn in turns:
        if turn in before:
            item=dict(before[turn]);item.pop('key')
            first=item.pop('first_call')
            item['first_key']=first['key'] if first is not None else None
            item['dimensions']=dict(item['dimensions'])
        else:
            item=dict(display_ordinal=0,effective_at=None,call_count=0,minimum=None,maximum=None,
                first_key=None,dimensions=dict.fromkeys(('model','effort','service_tier'),'혼합'),request_present=False)
        if turn in boundaries:item['request_present']=True
        if turn in states:item['turn_state']=states[turn]
        result[turn]=item
    source_states={turn:value['turn_state'] for turn,value in result.items() if 'turn_state' in value}
    return result,before,boundaries,source_states,request_count


def _finish_headers(snapshot,owner,checkpoint,headers,before,boundaries,rows):
    for row in rows:
        turn=row.get('turn')
        if not turn:continue
        item=headers[turn]
        if not item['call_count']:
            item.update(minimum=row['ts'],maximum=row['ts'],first_key=row['key'],dimensions={})
        item['call_count']+=1;item['maximum']=row['ts']
        for field in ('model','effort','service_tier'):
            value=row.get(field) or '미확인'
            if item['dimensions'].setdefault(field,value)!=value:item['dimensions'][field]='혼합'
    pending=[]
    for turn,item in headers.items():
        old=before.get(turn)
        boundary=boundaries.get(turn)
        if boundary is None and item['request_present']:
            stored=snapshot.request(owner,turn)
            _require(stored is not None,'request header has no boundary')
            boundary=stored['metadata']
        started=(boundary or {}).get('started_at')
        effective=started if started is not None else item['minimum']
        displayed=bool(item['call_count'] or item['request_present'])
        if old is not None and old['display_ordinal']:
            _require(effective==old['effective_at'],'request correction changes display ordering')
        elif displayed:
            pending.append((effective if effective is not None else float('inf'),turn))
        item['effective_at']=effective
    last=checkpoint['last_header_order'];count=checkpoint['header_count']
    for position in sorted(pending):
        _require(last is None or position>tuple(last),'new request precedes an existing displayed request')
        count+=1;headers[position[1]]['display_ordinal']=count;last=position
    return count,last


def _choices(previous,rows):
    from .analysis_delivery import SnapshotPublisher
    new=SnapshotPublisher._choices_for_rows(rows)
    counts=Counter()
    for choices in (previous,new):
        for item in choices['combinations']:
            counts[item['model'],item['effort'],item['service_tier']]+=item['count']
    return dict(models=sorted({k[0] for k in counts}),
        efforts=sorted({k[1] for k in counts},key=effort_key),modes=sorted({k[2] for k in counts}),
        combinations=[dict(model=m,effort=e,service_tier=t,count=n) for (m,e,t),n in sorted(counts.items())],
        **{name:sorted(set(previous[name])|set(new[name])) for name in ('transports','transport_sources','cache_policies')})


def _compact_summary(source,previous,total,misses,health,request,recent,current_latest,latest_mismatch):
    facts=summary_facts(dict(total=total,request=request,request_latest=current_latest,
                             latest_mismatch=latest_mismatch),health.reducer.count)
    rendered=[call_summary(row,row['session_ordinal'],miss=row['cache_miss'],
                          degradation=row.get('cache_degradation',bool(row.get('cache_incident_id')))) for row in recent]
    request_view=_request_view(source.get('request_state') or {},facts['request'],current_latest,
                              bool(source.get('coverage_gaps')))
    own=_summary_view(source,recent[-1],rendered,facts,request=request_view,
                      misses=_miss_head(misses),health=health.head())
    own['cache_degradation'].pop('events')
    own['_health_ref']=dict(format=HEALTH_FORMAT,owner=(source['home'],source['id']))
    summary=dict(previous['summary'],own=own,partials=facts['partials'],
        coverage_bounds=(total['minimum'],total['maximum']),home_totals=home_totals((source['home'],source['id']),total))
    return summary_for_storage(summary)

def _sections(snapshot,owner,miss_events,misses,health,latest):
    """Only changed events and new occurrence memberships are staged."""
    output=[]
    for ordinal,event in miss_events:
        output.append(((HEALTH_FORMAT,'cache_misses','events'),ordinal,ordinal,event['ts'],dict(inline=event,head=_head(event))))
    for event_id,change in health.changed.items():
        event=change['event'];descriptor=_descriptor(event,change['baseline_ordinals'],change['recovery_ordinals'],evidence=True)
        members=health.members.get(event_id,())
        first=descriptor['occurrence_count']-len(members)
        output.extend((_member_section(event_id),first+i,first+i,None,pair) for i,pair in enumerate(members))
        for field in ('cache_health','cache_degradation'):
            output.append(((HEALTH_FORMAT,field,'events'),change['ordinal'],change['ordinal'],event['ts'],dict(descriptor,head=_head(_event_fixed(event)))))
        output.append(((HEALTH_FORMAT,'event-id'),str(event_id),change['ordinal'],event['ts'],descriptor))
    incident=health.reducer.incident
    if incident is None:descriptor=None
    elif incident['id'] in health.changed:
        item=health.changed[incident['id']]
        descriptor=_descriptor(incident,item['baseline_ordinals'],item['recovery_ordinals'],evidence=False)
    else:
        found=snapshot.section_item(owner,(HEALTH_FORMAT,'event-id'),str(incident['id']))
        _require(found is not None,'missing active incident section')
        descriptor=dict(found['value'],evidence_fields=())
    heads=dict(cache_misses=_miss_head(misses),cache_health=health.head(),
        cache_degradation=dict(count=health.reducer.count,current=bool(latest['cache_degradation'])))
    for field,value in heads.items():
        old=snapshot.section_item(owner,HEALTH_FORMAT,field)
        _require(old is not None,'missing health manifest')
        manifest=deepcopy(old['value'])
        manifest['value']={k:v for k,v in value.items() if k!='incident'}
        manifest['events']['count']=misses['count'] if field=='cache_misses' else health.reducer.count
        if field=='cache_health':manifest['incident']=descriptor
        output.append((HEALTH_FORMAT,field,0,None,manifest))
    return output,heads


@dataclass
class AppendPlan:
    owner: tuple
    expected: object
    metadata: dict
    call_metadata: dict
    dependencies: dict
    request_maps: tuple
    rows: list
    requests: list
    headers: dict
    state_repairs: dict
    annotations: dict
    sections: list
    summary: dict
    choices: dict
    model_alerts: dict
    checkpoint: dict
    checkpoint_bytes: int
    prior_count: int
    prior_frontier: int
    plan_bytes: int = 0

    def speed_certificate(self,previous_stamp):
        from .prepared_speed_writer import SpeedPrefixCertificate
        _require(previous_stamp.length==self.prior_count and previous_stamp.last_ordinal==self.prior_frontier,
                 'speed predecessor frontier differs')
        return SpeedPrefixCertificate(previous_stamp.revision,self.prior_count,self.prior_frontier)

    def apply(self,stage,source_ordinal,*,cancel=None):
        """Once this begins, failures require aborting the caller's entire Stage."""
        from .prepared_store import StaleGeneration
        if stage.expected!=self.expected:raise StaleGeneration('append plan predecessor was replaced')
        stage.owner(*self.owner,self.metadata,self.dependencies,source_ordinal=source_ordinal,
            call_metadata=self.call_metadata,request_maps=self.request_maps,summary=self.summary,
            choices=self.choices,model_alerts=self.model_alerts,checkpoint=self.checkpoint)
        stage.candidates(self.owner,((row['key'],row['session_ordinal']-1) for row in self.rows))
        # Request-state corrections affect the whole indexed turn. Read only one
        # bounded page under each committed snapshot, then close it before writes.
        for turn,state in self.state_repairs.items():
            after=None
            while True:
                if cancel is not None and cancel():raise InterruptedError('owner append cancelled')
                with stage.store.read_current(cancel=cancel,seconds=60) as view:
                    page=view.calls(self.owner,turn=turn,after=after)
                changed=[]
                for row in page.rows:
                    row=dict(row,state=state)
                    if row['key'] in self.annotations:
                        row.update(cache_incident_id=self.annotations[row['key']],cache_degradation=True)
                    changed.append(compact_record(row))
                stage.calls(self.owner,changed)
                if page.complete:break
                after=page.after
        old_keys=tuple(key for key in self.annotations if key not in {row['key'] for row in self.rows})
        if old_keys:
            with stage.store.read_current(cancel=cancel,seconds=60) as view:
                repaired=list(_pages(view.records,self.owner,old_keys))
            for row in repaired:
                row.update(cache_incident_id=self.annotations[row['key']],cache_degradation=True)
                if row.get('turn') in self.state_repairs:row['state']=self.state_repairs[row['turn']]
            stage.calls(self.owner,(compact_record(row) for row in repaired))
        stage.calls(self.owner,self.rows)
        stage.requests(self.owner,self.requests)
        stage.headers(self.owner,self.headers.items())
        stage.sections(self.owner,self.sections)


def prepare(snapshot,owner,transition,dependencies,*,mode_evidence=None,enrich=None):
    """Read-only admission against one committed predecessor; never publish here.

    The selected producer endpoint must bind the complete old/new source bodies.
    ``enrich`` optionally supplies current owner mode evidence for the bounded tail.
    The caller admits at most one changed owner before its ordinary collision
    election, or must separately reject intersections among several new tails.
    """
    from .collection_append import digest,context_digest,RESPONSE_BYTES,encoded
    _require(snapshot.staged_batch is None,'append requires a committed predecessor')
    owner=tuple(owner);old=snapshot.owner(*owner)
    checkpoint=snapshot.derived(*owner,'checkpoint')
    _require(old is not None and isinstance(checkpoint,dict) and checkpoint.get('format')==FORMAT,'no append checkpoint')
    _require(checkpoint['owner']==owner and transition is not None and transition.get('format')==1
             and tuple(transition.get('owner',()))==owner,'append owner or format differs')
    _require(len(encoded(transition))<=RESPONSE_BYTES and _size(transition)<=PLAN_BYTES,'append packet exceeds budget')
    prior=transition['prior_body_digest'];body=transition['body_digest']
    _require(prior==checkpoint['source_digest']==checkpoint['candidate_source_digest']
             ==old['dependencies']['source_digest']==old['dependencies'].get('candidate_source_digest'),
             'candidate and prepared source predecessor differ')
    _require(body==dependencies['source_digest'] and body!=prior,'new source body differs')
    _require(old['dependencies'].get('prepared') and not old['metadata'].get('_internal_review'),
             'predecessor is not a public prepared owner')
    _require(all(dependencies.get(k)==v==old['dependencies'].get(k) for k,v in checkpoint['dependencies'].items()),
             'pricing or reducer dependency changed')
    _require(mode_evidence==checkpoint['mode_evidence'],'owner mode evidence changed')
    _require(transition['context_digest']==checkpoint['context_digest']==context_digest(transition['metadata']),
             'source envelope changed')
    _require(transition['prior_raw_count']==checkpoint['raw_count'] and
             tuple(transition['prior_frontier'])==tuple(checkpoint['frontier']),'raw prefix frontier differs')
    tail=transition['tail'];count=checkpoint['raw_count']
    _require(0<len(tail)<=TAIL_ROWS and transition['raw_count']==count+len(tail),'raw tail count differs')
    _require(digest(tail)==transition['tail_digest'],'tail integrity differs')
    stamp=checkpoint['frontier'][0];keys=set()
    for row in tail:
        key=row.get('key');ts=row.get('ts')
        _require(isinstance(key,str) and key and key not in keys and type(ts) in (int,float)
                 and isfinite(ts) and ts>stamp,'tail identity or order is uncertain')
        stamp=ts;keys.add(key)
    _require(not next(_pages(snapshot.candidate_matches,owner,tuple(keys)),None),'tail key has a competing candidate')
    for name in ('requests','turns'):
        _require(not transition[name]['removed'] and len(transition[name]['upserts'])<=TAIL_ROWS,'request map deletion requires rebuild')
    request_maps=tuple(transition['request_maps'])
    _require(set(old['request_maps'])<=set(request_maps)<=set(('turn_records','turn_states')),'request map presence was removed')
    headers,before,boundaries,states,request_count=_headers(snapshot,owner,checkpoint,transition)
    source=dict(transition['metadata'],history=deepcopy(tail),turn_states=states,turn_records=boundaries)
    _require(not source.get('parent_thread_id'),'append ancestry is not proven')
    if enrich is not None:source=enrich(source)
    # The callback may enrich tail observations, never replace the source scope.
    _require(len(source['history'])==len(tail) and all(a['key']==b['key'] and a['ts']==b['ts']
             for a,b in zip(source['history'],tail)),'mode enrichment changed the tail population')
    canonical=prepare_row({},source)
    # The store joins raw metadata with canonical overrides. Raw optional None
    # fields stay in that context even when individual calls mark them absent.
    call_metadata={key:source[key] for key in OWNER_FIELDS if key in source}
    call_metadata.update((key,canonical[key]) for key in OWNER_FIELDS if key in canonical)
    joined={**old['metadata'],**old['call_metadata']}
    previous_metadata={key:joined[key] for key in OWNER_FIELDS if key in joined}
    _require(call_metadata.keys()==previous_metadata.keys() and all(pack(value)==pack(previous_metadata[key])
             for key,value in call_metadata.items()),'joined call metadata changed')
    total=deepcopy(checkpoint['total']);misses=deepcopy(checkpoint['misses']);health=_Health(checkpoint['health'])
    rows=[];miss_events=[]
    for index,raw in enumerate(source['history'],count+1):
        event=_add_miss(misses,raw)
        if event:miss_events.append((misses['count']-1,event))
        row=prepare_row(raw,source)
        row.update(session_ordinal=index,cache_miss=event is not None,cache_incident_id=None,cache_degradation=False)
        _add_totals(total,row);health.add(row);rows.append(row)
    for row in rows:
        if row['key'] in health.annotations:row.update(cache_incident_id=health.annotations[row['key']],cache_degradation=True)
    header_count,last_order=_finish_headers(snapshot,owner,checkpoint,headers,before,boundaries,rows)
    packet=snapshot.derived(*owner,'summary')
    refs=recent_references(packet)
    recent_keys=[ref['key'] for ref in refs][-max(0,12-len(rows)):] if len(rows)<12 else []
    found={row['key']:compact_record(row) for row in _pages(snapshot.records,owner,recent_keys)} if recent_keys else {}
    _require(len(found)==len(recent_keys),'recent call references are incomplete')
    recent=[found[key] for key in recent_keys]+rows[-12:]
    for row in recent:
        if row.get('turn') in states:row['state']=states[row['turn']]
        if row['key'] in health.annotations:row.update(cache_incident_id=health.annotations[row['key']],cache_degradation=True)
    turn=(source.get('request_state') or {}).get('turn')
    request=deepcopy(checkpoint['request']) if checkpoint['request']['turn']==turn else _new_request(turn)
    if turn and checkpoint['request']['turn']!=turn and before.get(turn,{}).get('call_count'):
        for row in _pages(snapshot.calls,owner,turn=turn):_add_request(request,row)
    for row in rows:
        if turn and row.get('turn')==turn:_add_request(request,row)
    current_latest=next((row for row in reversed(recent) if row['key']==request['latest']),None)
    if current_latest is None:
        current_latest=snapshot.record(*owner,request['latest']) if request['latest'] is not None else {}
    _require(current_latest is not None,'current request latest call is missing')
    latest_mismatch=packet['own']['model_incident']['key']
    for row in rows:
        if row.get('model_alert_confirmed'):latest_mismatch=row['key']
    summary=_compact_summary(source,packet,total,misses,health,request,recent,current_latest,latest_mismatch)
    sections,heads=_sections(snapshot,owner,miss_events,misses,health,rows[-1])
    excluded=FORBIDDEN_OWNER|{'requests','groups','_prepared'}
    metadata={key:value for key,value in source.items() if key not in excluded}
    metadata.update(cache_misses=heads['cache_misses'],cache_health=heads['cache_health'],
        _internal_review=False,usage_revision=(body,dependencies['mode']),request_revision=body)
    from .analysis_delivery import MODEL_KEYS
    alerts=dict(snapshot.derived(*owner,'model_alerts'))
    for row in rows:
        if row.get('model_alert_confirmed'):alerts[row['key']]=tuple(row.get(k) for k in MODEL_KEYS)
    choices=_choices(snapshot.derived(*owner,'choices'),rows)
    dependency=dict(dependencies,ownership=sha256(pack(('owner-append-ownership',1,
        old['dependencies']['ownership'],tuple((row['session_ordinal']-1,row['key']) for row in rows)))).hexdigest(),
        candidate_source_digest=body,prepared=True,call_count=total['count'],speed_frontier=total['count'],
        coverage_bounds=(total['minimum'],total['maximum']))
    updated=dict(checkpoint,source_digest=body,candidate_source_digest=body,raw_count=count+len(rows),
        frontier=(rows[-1]['ts'],rows[-1]['key']),total=total,misses=misses,health=health.save(),request=request,
        request_count=request_count,header_count=header_count,last_header_order=last_order)
    try:
        checkpoint_bytes=len(pack(updated,snapshot.limits,
            maximum=min(CHECKPOINT_BYTES,snapshot.limits.packet_bytes)))
        for item in (metadata,dependency,summary,alerts,choices):pack(item,snapshot.limits)
        for _,_,_,_,value in sections:pack(value,snapshot.limits)
    except (CapacityExceeded,TypeError,ValueError) as error:
        raise AppendUnavailable('append derived packet exceeds ordinary owner admission') from error
    repairs={turn:state for turn,state in transition['turns']['upserts']
             if before.get(turn,{}).get('call_count') and before[turn].get('turn_state','미확인')!=state}
    plan=AppendPlan(owner,snapshot.token,metadata,call_metadata,dependency,request_maps,
        [compact_record(row) for row in rows],transition['requests']['upserts'],headers,repairs,
        health.annotations,sections,summary,choices,alerts,updated,checkpoint_bytes,count,count)
    plan.plan_bytes=_size(vars(plan))
    _require(plan.plan_bytes<=PLAN_BYTES,'append plan exceeds transient budget')
    return plan


def try_apply(cohort,catalog,changed,removed,*,ready,parents):
    """Cohort hook before candidate inventories or complete source retrieval."""
    if len(changed)!=1 or removed:return None
    owner=next(iter(changed))
    if owner in parents:return None
    method=getattr(cohort.client,'selected_owner_append',None)
    if method is None:return None
    from .prepared_ingest import dependencies,mode_dependency,source_identity,ModeEvidenceUnavailable
    import sqlite3
    ledger=cohort.ledger
    mode_evidence=ledger.mode_dependency(*owner) if ledger is not None else None
    def enrich(source):
        if ledger is None:return source
        value=dict(homes=catalog['selected_homes'],sessions=[source])
        try:
            ledger.enrich_modes(value,scan=False)
            return value['sessions'][0]
        except sqlite3.Error as error:
            raise ModeEvidenceUnavailable('optional request-mode evidence failed') from error
        finally:
            ledger._enriched_sessions={}
    try:
        with cohort.store.read_current(cancel=cohort.cancel,seconds=60) as view:
            old=view.owner(*owner)
            if old is None:return None
            checkpoint=view.derived(*owner,'checkpoint')
            if not isinstance(checkpoint,dict) or checkpoint.get('format')!=FORMAT:return None
            entry=catalog['owners'][owner]
            transition=method(owner,old['dependencies']['source_digest'],catalog=catalog)
            if transition is None:return None
            transition=dict(transition,metadata=dict(transition['metadata'],**entry.get('state',{})))
            wanted=dependencies(entry['body_digest'],mode_dependency((owner,),catalog,ledger),
                                old['dependencies']['ownership'],state=entry.get('state',{}))
            plan=prepare(view,owner,transition,wanted,mode_evidence=mode_evidence,enrich=enrich)
    except AppendUnavailable as error:
        cohort.metrics['append_fallbacks']=cohort.metrics.get('append_fallbacks',0)+1
        cohort.metrics['append_last_fallback']=str(error)
        return None
    cohort._check()
    source=source_identity(catalog)
    previous=cohort._speed_inventory() if ready and cohort.speed_writer is not None else None
    stage=cohort.store.begin(source)
    try:
        plan.apply(stage,entry['source_ordinal'],cancel=cohort.cancel)
        if ready and not cohort._complete(catalog,staged=True,parents=parents):
            raise ValueError('append cohort cannot publish incomplete prepared dependencies')
        current=None
        if previous is not None:
            current=dict(previous)
            certificate=plan.speed_certificate(previous[owner]) if owner in previous else None
            current[owner]=cohort._speed_stamp(dict(dependencies=plan.dependencies),certificate)
        token=cohort._publish(stage,source,ready,previous,current)
        cohort.metrics['append_owners']=cohort.metrics.get('append_owners',0)+1
        cohort.metrics['append_rows']=cohort.metrics.get('append_rows',0)+len(plan.rows)
        cohort.metrics['append_checkpoint_bytes']=plan.checkpoint_bytes
        return dict(token=token,changed=(owner,),removed=(),writes=dict(stage.writes))
    except BaseException:
        cohort._abort(stage)
        raise
