"""Filter-independent, call-scoped overlay data from priced analysis records."""
from copy import deepcopy
from heapq import nlargest

from .analytics import stats, observation_flags, output_speed, output_speed_summary
from .pricing import request_tier, sum_cost
from .core import token_number, transport_label, token_parts
from .cache_misses import classify
from .cache_health import CacheHealth
from .session_costs import session_costs
from .speed_health import SpeedHealth
from .analysis_records import public_record


TOKEN_LABELS = {'cached': '캐시 읽기', 'uncached': '일반 입력', 'written': '캐시 쓰기',
                'reasoning': '추론', 'output': '추론 외', 'input_unknown': '입력 미분류',
                'output_unknown': '출력 미분류', 'unknown': '미분류'}
TOKEN_ORDER = ('cached', 'uncached', 'written', 'reasoning', 'output', 'unknown')
COMPARISON_ORDER = ('uncached', 'written', 'reasoning', 'output')


def observed_mode(row):
    return request_tier(row)


def assumed_cost(row):
    return bool(row.get('price_assumed'))


def token_composition(session,*,_partials=None):
    """Shared disjoint token rules; unattached cumulative gaps stay out."""
    rows=list(session.get('history',()))
    counts=dict.fromkeys(TOKEN_LABELS,0);known=dict.fromkeys(TOKEN_ORDER,False)
    inp_total=out_total=0;partial=False
    cache_input=cache_read=cache_valid=0
    input_known=output_known=False;written_known=bool(rows)
    for row in rows:
        parts=token_parts(row);inp,out=parts['input'],parts['output']
        for section,mapping in ((inp,{'ordinary':'uncached','read':'cached','write':'written','unclassified':'input_unknown'}),
                                (out,{'ordinary':'output','reasoning':'reasoning','unclassified':'output_unknown'})):
            for key,dest in mapping.items():counts[dest]+=section[key]
        i,r=token_number(row.get('input')),token_number(row.get('cached'))
        if i is not None:inp_total+=i
        if token_number(row.get('output')) is not None:out_total+=row['output']
        input_known |= i is not None
        output_known |= token_number(row.get('output')) is not None
        if i is not None and r is not None and r<=i:
            cache_input+=i;cache_read+=r;cache_valid+=1
        partial |= i is None or token_number(row.get('output')) is None
        w,o,q=(token_number(row.get(key)) for key in ('written','output','reasoning'))
        written_known &= w is not None
        input_valid=i is not None and not row.get('input_conflict') and (r is None or r<=i) and (w is None or w<=i) and (r is None or w is None or r+w<=i)
        output_valid=o is not None and q is not None and q<=o and not row.get('output_conflict')
        known['cached'] |= input_valid and r is not None
        known['written'] |= input_valid and w is not None
        known['uncached'] |= input_valid and r is not None and w is not None
        known['output'] |= output_valid;known['reasoning'] |= output_valid
    if _partials is not None:_partials.update(cache_input=cache_input,cache_read=cache_read)
    return _token_view(counts,known,inp_total,out_total,partial,input_known,output_known,
                       cache_input,cache_read,cache_valid,len(rows),written_known)


def _token_view(counts,known,inp_total,out_total,partial,input_known,output_known,
                cache_input,cache_read,cache_valid,cache_target,written_known):
    total=inp_total+out_total
    def group(keys,denominator):
        return [dict(key=key,label=TOKEN_LABELS[key],tokens=counts[key],known=known.get(key,True),share=counts[key]/denominator if denominator else 0)
                for key in keys if counts[key] or key not in ('input_unknown','output_unknown')]
    input_parts=group(('cached','written','uncached','input_unknown'),inp_total) if input_known else []
    output_parts=group(('reasoning','output','output_unknown'),out_total) if output_known else []
    unknown=counts['input_unknown']+counts['output_unknown'];known['unknown']=bool(unknown)
    displayed={**counts,'unknown':unknown};maximum=max((displayed[k] for k in COMPARISON_ORDER),default=0)
    bars=[dict(key=k,label=TOKEN_LABELS[k],tokens=displayed[k],known=known[k],share=displayed[k]/total if total else 0,
               bar_share=displayed[k]/maximum if maximum else 0) for k in (*COMPARISON_ORDER,'unknown') if k!='unknown' or unknown]
    return dict(total=total,partial=partial,counts=counts,known=bool(input_parts or output_parts),category_known=known,
        input_parts=input_parts,output_parts=output_parts,input_total=inp_total,output_total=out_total,
        cached=counts['cached'],cached_share=counts['cached']/inp_total if inp_total else 0,
        cache_hit_rate=100*cache_read/cache_input if cache_input else None,
        cache_hit_partial=cache_valid<cache_target,cache_valid=cache_valid,cache_target=cache_target,
        written_known=written_known,
        unknown=unknown,non_cache_total=total-counts['cached'],bar_max=maximum,bars=bars,
        parts=[dict(key=k,label=TOKEN_LABELS[k],tokens=displayed[k],share=displayed[k]/total) for k in TOKEN_ORDER if displayed[k] and total])


COST_PARTS=(('input',(('cached','cost_cached'),('written','cost_written'),('uncached','cost_uncached'),('input_unknown','cost_unclassified'))),
            ('output',(('reasoning','cost_reasoning'),('output','cost_non_reasoning'),('output_unknown',None))))


def cost_composition(rows):
    """Disjoint amounts over exactly the headline's priced population."""
    priced=[r for r in rows if r.get('cost') is not None]
    sections={}
    for section,keys in COST_PARTS:
        counts=dict.fromkeys((k for k,_ in keys),0.)
        for row in priced:
            if section=='output' and (row.get('output_conflict') or any(row.get(k) is None for k in ('cost_reasoning','cost_non_reasoning'))):
                counts['output_unknown']+=row.get('cost_output') or 0
            else:
                for key,field in keys:counts[key]+=row.get(field) or 0
        sections[section]=counts
    return _cost_view(sections,len(priced),len(rows))


def _cost_view(sections,priced,calls):
    result=dict(priced=priced,calls=calls,partial=priced<calls,known=bool(priced))
    for section,counts in sections.items():
        total=sum(counts.values()) if priced else None
        parts=[dict(key=k,label=TOKEN_LABELS[k],tokens=v,known=bool(priced),share=v/total if total else 0)
               for k,v in counts.items() if v or not k.endswith('unknown')]
        result.update({section+'_total':total,section+'_parts':parts})
    return result


def _merge_compositions(summaries,partials):
    tokens=[summary['token_composition'] for summary in summaries]
    counts={key:sum(value['counts'][key] for value in tokens) for key in TOKEN_LABELS}
    known={key:any(value['category_known'][key] for value in tokens) for key in TOKEN_ORDER}
    calls=sum(summary['calls'] for summary in summaries)
    composition=_token_view(counts,known,sum(value['input_total'] for value in tokens),
        sum(value['output_total'] for value in tokens),any(value['partial'] for value in tokens),
        any(value['input_parts'] for value in tokens),any(value['output_parts'] for value in tokens),
        sum(value['cache_input'] for value in partials),sum(value['cache_read'] for value in partials),
        sum(value['cache_valid'] for value in tokens),sum(value['cache_target'] for value in tokens),
        bool(calls) and all(not summary['calls'] or value['written_known'] for summary,value in zip(summaries,tokens)))
    costs=[summary['cost_composition'] for summary in summaries]
    sections={section:dict.fromkeys((key for key,_ in fields),0.) for section,fields in COST_PARTS}
    for cost in costs:
        for section,counts in sections.items():
            for part in cost[section+'_parts']:counts[part['key']]+=part['tokens']
    cost=_cost_view(sections,sum(value['priced'] for value in costs),sum(value['calls'] for value in costs))
    speeds=[summary['output_speed_summary'] for summary in summaries]
    output=sum(value['output'] for value in speeds);seconds=sum(value['seconds'] for value in speeds)
    count=sum(value['n'] for value in speeds);target=sum(value['N'] for value in speeds)
    speed=dict(value=output/seconds if seconds else None,output=output,seconds=seconds,n=count,N=target,missing=target-count)
    maintenance_calls=sum(value['maintenance_calls'] for value in partials)
    maintenance_missing=sum(value['maintenance_missing'] for value in partials)
    maintenance_cost=(sum(value['maintenance_cost'] for value in partials if value['maintenance_cost'] is not None)
                      if maintenance_calls>maintenance_missing else None)
    return dict(token_composition=composition,cost_composition=cost,output_speed_summary=speed,
        maintenance_calls=maintenance_calls,maintenance_missing=maintenance_missing,maintenance_cost=maintenance_cost)


def _recent_members(members,updated):
    # Preserve stable tie ordering while retaining at most twelve candidates.
    rows=((member,row) for member in members for row in updated[member][1]['recent'])
    def order(item):
        ordinal,(_,row)=item
        return row['ts'],str(row.get('home')),str(row.get('sid')),str(row['id']),ordinal
    selected=nlargest(12,enumerate(rows),key=order)
    return [dict(row,source_title=updated[member][1]['title'])
            for _,(member,row) in sorted(selected,key=order)]


class OverlayCacheHealth(CacheHealth):
    """Compatibility name for the canonical full-session classifier."""


def _call_id(row):
    # Every collected response normally has a source key. Older records without
    # one still have a stable observed timestamp/turn identity, never a plot slot.
    return row.get('call_id') or row.get('key') or row.get('response_id') or repr((
        row.get('home'), row.get('sid'), row.get('ts'), row.get('turn'),
        row.get('request_observed_at')))


def call_summary(row, ordinal, *, miss=False, degradation=False):
    """Keep absent observations absent, and all headline fields on one call."""
    result = public_record(row)
    parts = token_parts(row)
    inp, cached, written, out, reasoning = (token_number(row.get(key))
        for key in ('input', 'cached', 'written', 'output', 'reasoning'))
    valid_input = inp is not None and cached is not None and cached <= inp
    rate = cached/inp*100 if valid_input and inp else None
    source = row.get('transport_source', 'log_time' if row.get('transport') in ('WebSocket', 'HTTP/SSE') else 'unknown')
    transport_state = ('관측 충돌' if source == 'conflict' else
                       '확정' if source == 'response_id' and row.get('transport') in ('WebSocket', 'HTTP/SSE') else
                       '추정' if row.get('transport') in ('WebSocket', 'HTTP/SSE') else '미확인')
    model_evidence = row.get('model_evidence') or ''
    model_mismatch = bool(row.get('model_alert_confirmed') and not row.get('observation_missing') and '충돌' not in model_evidence)
    model_state = ('관측 누락' if row.get('observation_missing') else
                   '관측 충돌' if '충돌' in model_evidence else
                   '모델명 불일치' if model_mismatch else '일치' if row.get('model_match')=='일치' else '미확인')
    warnings = []
    if model_mismatch:
        warnings.append('모델명 불일치')
    if miss:
        warnings.append('캐시 읽기 0')
    if degradation:
        warnings.append('캐시 저하 의심')
    if row.get('observation_missing'):
        warnings.append('모델 관측 누락')
    elif model_state == '관측 충돌':
        warnings.append('모델 관측 충돌')
    if source == 'conflict':
        warnings.append('연결 관측 충돌')
    if row.get('cache_policy_conflict'):
        warnings.append('캐시 정책 관측 충돌')
    result.update(id=_call_id(row), ordinal=ordinal,output_speed=output_speed(row),
        model=(row.get('analysis_model') or row.get('requested_model') or row.get('model') or '미확인') if not row.get('model_conflict') else '미확인',
        configured_model=row.get('configured_model') or row.get('model'), model_setting=row.get('model_source')=='settings' or not bool(row.get('requested_model')) and bool(row.get('model')),
        response_model=row.get('response_model') or None,
        model_mismatch=model_mismatch,
        model_state=model_state, effort=row.get('effort') or '미확인', mode=request_tier(row),
        response_mode=request_tier({'service_tier':row.get('response_service_tier')}),
        transport_kind=row.get('transport'), transport=transport_label(row), transport_state=transport_state,
        rate=rate, cache_rate=rate, uncached=inp-cached-written if valid_input and written is not None and cached+written<=inp else None,
        non_reasoning=out-reasoning if out is not None and reasoning is not None and reasoning<=out else None,
        input_unknown=parts['input']['unclassified'], output_unknown=parts['output']['unclassified'],
        unknown=parts['input']['unclassified']+parts['output']['unclassified'],
        cost_assumed=assumed_cost(row), warnings=warnings, cache_miss=miss,
        cache_degradation=degradation, cache_warning=miss or degradation)
    return result


def _request_view(lifecycle,current,current_latest,gap):
    return {**lifecycle,'calls':current['calls'],'cost':current['cost'] if current['calls'] else None,
        'priced':current['priced'],'assumed':current['assumed'],'partial':current['partial'] or gap,
        'model':current_latest.get('requested_model') or current_latest.get('model'),
        'model_setting':not bool(current_latest.get('requested_model')),
        'response_model':current_latest.get('response_model'),
        'model_mismatch':bool(current_latest.get('model_alert_confirmed')),
        'mode':observed_mode(current_latest),'effort':current_latest.get('effort'),
        'usage_at':current_latest.get('ts')}


def _summary_view(session,latest,recent,facts,*,request,misses,health,degradation=()):
    """One presentation finalizer for full and incrementally reduced owner facts."""
    latest_call=recent[-1] if recent else {}
    call=facts['cost'];calls=facts['calls'];tokens=facts['tokens']
    gap=bool(session.get('coverage_gaps'))
    statuses=[]
    if gap:
        statuses.append({'text':'이전 기록 누락','severity':'muted'})
    if not calls:
        statuses.append({'text':'호출 기록 없음','severity':'muted'})
    evidence=[]
    if call['missing'] or gap:
        evidence.append(f"산정 {call['n']:,} / {calls:,}호출")
    assumed=facts['assumed']
    if assumed:
        evidence.append(f'Standard 가정 {assumed:,}호출')
    if gap:
        evidence.append('이전 기록 누락')
    if tokens['cache_hit_partial']:
        evidence.append('확인분 입력 기준')
    return {'id': session['id'], 'home': session['home'],
            'title': ' '.join((session.get('title') or '').split()),
            'model': latest_call.get('model','미확인'), 'model_setting': latest_call.get('model_setting',False),
            'response_model': latest_call.get('response_model'),
            'model_mismatch': latest_call.get('model_mismatch',False),
            'model_state': latest_call.get('model_state','미확인'),
            'effort': latest_call.get('effort','미확인'), 'mode': latest_call.get('mode','미확인'),
            'response_mode':latest_call.get('response_mode','미확인'),
            'transport': latest_call.get('transport','미확인'), 'transport_state':latest_call.get('transport_state','미확인'),
            'cache_rate': latest_call.get('cache_rate'), 'cache_misses':misses,
            'cost': call['sum'] if calls else None, 'mean_cost': call['mean'],
            'latest_cost': latest.get('cost'), 'calls': calls, 'priced': call['n'],
            'missing': call['missing'], 'partial': bool(call['missing']),
            'unattached_total':token_number((session.get('unclassified') or {}).get('total')),
            'assumed': assumed, 'coverage_gap': gap, 'warnings': latest_call.get('warnings',[]),
            'latest_ts': latest.get('ts'), 'latest_key': latest.get('key'),
            'latest':latest_call,'request':request,'recent':recent,'cache_health':health,
            'output_speed_summary':facts['speed'],'cost_composition':facts['cost_composition'],
            'members':[(session['home'],session['id'])],
            'model_mismatch_count':facts['mismatch_count'],
            'observation_missing_count':facts['observation_missing'],
            'observation_conflict_count':facts['observation_conflict'],
            'cache_degradation':{'count':facts['degradation_count'],'events':deepcopy(degradation),
                'current':bool(latest_call.get('cache_degradation'))},
            'statuses':statuses,'session_evidence':evidence,
            'model_incident':{'count':facts['mismatch_count'],'key':facts['mismatch_key']},
            'token_composition': tokens}


def summarize_session(session,health=None,*,_partials=None,_facts=None):
    rows = sorted(session.get('history', ()), key=lambda row: row['ts'])
    latest = rows[-1] if rows else {}
    call = stats(rows, 'cost') if _facts is None else _facts['cost']
    gap = bool(session.get('coverage_gaps'))
    misses=session.get('cache_misses') or classify(rows)
    misses=deepcopy(misses)
    miss_records={(event.get('key'),event['ts']) for event in misses.get('events', ())}
    def is_miss(row):
        return (row.get('key'),row['ts']) in miss_records
    if health is None:
        health=OverlayCacheHealth().update([dict(row,cache_miss=is_miss(row)) for row in rows])
    else:
        health=deepcopy(health)
    incidents=health.get('events', ())
    if not incidents and health.get('incident'):
        incidents=[health['incident']]
    degradation=[event for event in incidents if event.get('state')=='캐시 저하 의심']
    # Keep classifier evidence available after the graph's 24-call window rolls
    # forward. These are observed rows, never reconstructed from session totals.
    evidence_rows={}
    evidence_keys=set()
    for event in degradation:
        evidence_keys.update(event.get('baseline_keys',()))
        evidence_keys.update(event.get('occurrence_keys',event.get('keys',(event.get('first_key'),event.get('latest_key')))))
        evidence_keys.update(event.get('recovery_keys',()))
    if evidence_keys:
        for ordinal,row in enumerate(rows,1):
            if row.get('key') is None or row['key'] not in evidence_keys:continue
            inp,cached=token_number(row.get('input')),token_number(row.get('cached'))
            evidence_rows[row['key']]=dict(key=row['key'],ordinal=ordinal,ts=row.get('ts'),
                input=inp,cached=cached,cache_rate=cached/inp*100 if inp and cached is not None and cached<=inp else None)
    for event in degradation:
        event['baseline_calls']=[deepcopy(evidence_rows[key]) for key in event.get('baseline_keys',()) if key in evidence_rows]
        keys=event.get('occurrence_keys',event.get('keys',(event.get('first_key'),event.get('latest_key'))))
        event['comparison_calls']=[deepcopy(evidence_rows[key]) for key in dict.fromkeys(keys) if key in evidence_rows]
        event['recovery_calls']=[deepcopy(evidence_rows[key]) for key in event.get('recovery_keys',()) if key in evidence_rows]
    degradation_keys={key for event in degradation for key in event.get('occurrence_keys',event.get('keys', (
        event.get('first_key'),event.get('latest_key')))) if key is not None}
    first=max(0,len(rows)-24)
    all_calls=[call_summary(row,ordinal,miss=is_miss(row),
                        degradation=row.get('key') in degradation_keys)
            for ordinal,row in enumerate(rows[-12:],max(1,len(rows)-11))]
    recent=all_calls
    latest_call=recent[-1] if recent else {}
    lifecycle=session.get('request_state') or {}
    if _facts is None:
        current=[r for r in rows if lifecycle.get('turn') and r.get('turn')==lifecycle['turn']]
        current_cost=sum_cost(current)
        current_latest=max(current,key=lambda r:r['ts'],default={})
        mismatches=[r for r in rows if r.get('model_alert_confirmed')]
        tokens=token_composition(session,_partials=_partials)
        if _partials is not None:
            _partials.update(maintenance_calls=0,maintenance_missing=0)
            def maintenance_costs():
                for row in rows:
                    if row.get('purpose')!='maintenance':continue
                    _partials['maintenance_calls']+=1
                    if row.get('cost') is None:_partials['maintenance_missing']+=1
                    else:yield row['cost']
            maintenance_cost=sum(maintenance_costs())
            _partials['maintenance_cost']=(maintenance_cost if _partials['maintenance_calls']>_partials['maintenance_missing'] else None)
        facts=dict(cost=call,calls=len(rows),tokens=tokens,
            request=dict(calls=len(current),cost=current_cost['cost'],
                priced=sum(r.get('cost') is not None for r in current),assumed=sum(assumed_cost(r) for r in current),
                partial=any(r.get('cost') is None for r in current)),request_latest=current_latest,
            assumed=sum(assumed_cost(row) for row in rows),speed=output_speed_summary(rows),cost_composition=cost_composition(rows),
            mismatch_count=len(mismatches),mismatch_key=mismatches[-1]['key'] if mismatches else None,
            observation_missing=sum(observation_flags(r)['missing'] for r in rows),
            observation_conflict=sum(observation_flags(r)['conflict'] for r in rows),degradation_count=len(degradation))
    else:
        facts=_facts
        if _partials is not None:_partials.update(facts['partials'])
    request=_request_view(lifecycle,facts['request'],facts['request_latest'],gap)
    return _summary_view(session,latest,recent,facts,request=request,misses=misses,health=health,degradation=degradation)

class OverlaySummaries:
    def __init__(self):
        self.cache = {}
        self.partials = {}
        self.rollups = {}
        self.speed_health = SpeedHealth()
        self.presented = {}

    def collect(self, engine):
        speeds = self.speed_health.collect(engine)
        updated = {};partials={}
        for key, state in engine.sessions.items():
            source=state.get('source',state['prepared'])
            coverage_gaps=source.get('coverage_gaps',())
            revision = (state.get('content_revision',state['revision']),bool(coverage_gaps))
            previous = self.cache.get(key)
            if previous and previous[0]==revision:
                summary=previous[1];partials[key]=self.partials[key]
            else:
                parts={}
                summary=summarize_session(dict(state['prepared'],coverage_gaps=coverage_gaps),
                    state['prepared'].get('cache_health'),_partials=parts)
                partials[key]=parts
            updated[key] = (revision, summary)
        return self.present(engine, updated, partials, speeds)

    def present(self, engine, updated, partials, speeds):
        """Present prepared own summaries with the same ancestry and readiness rules."""
        self.cache = updated
        self.partials = partials
        own = {key:dict(cost=summary['cost'], calls=summary['calls'],
                        priced=summary['priced'], gap=summary['coverage_gap'])
               for key,(_,summary) in updated.items()}
        rollups = session_costs((state['prepared'] for state in engine.sessions.values()), own)
        result = []
        merged = {}
        for key,(_,summary) in updated.items():
            group = rollups[key]
            if not group['descendants']:
                result.append(summary)
                continue
            signature=tuple((member,updated[member][0]) for member in group['members'])
            previous=self.rollups.get(key)
            if previous and previous[0]==signature:
                value=previous[1]
            else:
                own_summaries=[updated[member][1] for member in group['members']]
                compositions=_merge_compositions(own_summaries,[partials[member] for member in group['members']])
                value=dict(summary,cost=group['cost'],own_cost=group['own_cost'],
                    child_cost=group['child_cost'],descendants=group['descendants'],
                    own_calls=summary['calls'],own_priced=summary['priced'],
                    calls=group['calls'],priced=group['priced'],
                    missing=group['calls']-group['priced'],partial=group['partial'],
                    mean_cost=group['cost']/group['priced'] if group['priced'] else None,
                    coverage_gap=group['gap'],
                    assumed=sum(updated[member][1]['assumed'] for member in group['members']))
                recent=_recent_members(group['members'],updated)
                value.update(recent=recent,latest=recent[-1] if recent else {},members=group['members'],
                             active_requests=sum(updated[m][1]['request'].get('state')=='진행' for m in group['members']),
                             **compositions)
                if recent:
                    value.update({k:recent[-1].get(k) for k in ('model','model_setting','response_model','model_mismatch','model_state','effort','mode','response_mode','transport','transport_state','cache_rate')})
                    value.update(latest_cost=recent[-1].get('cost'),latest_ts=recent[-1].get('ts'),latest_key=recent[-1].get('key'))
            merged[key]=(signature,value)
            result.append(value)
        self.rollups=merged
        presented={};output=[]
        for summary in result:
            key=(summary['home'],summary['id'])
            speed=speeds.get(key,{'active':False})
            # Readiness changes independently of priced history. Include every
            # displayed descendant without invalidating the expensive summaries.
            flags=[engine.sessions[member].get('source',engine.sessions[member]['prepared']).get('collection_complete')
                   for member in rollups[key]['members']]
            complete=False if False in flags else True if all(flag is True for flag in flags) else None
            previous=self.presented.get(key)
            value=(previous[2] if previous and previous[0] is summary and previous[1]==speed
                   and previous[2].get('collection_complete') is complete
                   else dict(summary,speed_health=speed,collection_complete=complete))
            presented[key]=(summary,speed,value);output.append(value)
        self.presented=presented
        return output
