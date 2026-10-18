"""Filter-independent, call-scoped overlay data from priced analysis records."""
from copy import deepcopy
from heapq import nlargest

from .analytics import stats, observation_flags, output_speed, output_speed_summary, finite
from .pricing import request_tier, sum_cost
from .core import token_number, transport_label, token_parts
from .cache_misses import classify
from .cache_health import CacheHealth
from .session_costs import session_costs
from .speed_health import SpeedHealth
from .analysis_records import public_record
from .prepared_sums import _Sum

RECENT_CALLS = 20


TOKEN_LABELS = {'cached': '캐시 읽기', 'uncached': '일반 입력', 'written': '캐시 쓰기',
                'reasoning': '추론', 'output': '추론 외', 'input_unknown': '입력 미분류',
                'output_unknown': '출력 미분류', 'unknown': '미분류'}
TOKEN_ORDER = ('cached', 'uncached', 'written', 'reasoning', 'output', 'unknown')
COMPARISON_ORDER = ('uncached', 'written', 'reasoning', 'output')


def observed_mode(row):
    return request_tier(row)


def assumed_cost(row):
    return bool(row.get('price_assumed'))


def canonical_model(row):
    return ((row.get('analysis_model') or row.get('requested_model') or row.get('model') or '미확인')
            if not row.get('model_conflict') else '미확인')


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
        cache_input=cache_input,cache_read=cache_read,
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


def new_financial_totals():
    """Persist only financial facts, including resumable headline summation."""
    return dict(count=0, cost_sum=(0,0.0,0), priced=0, component_priced=0, assumed=0,
        speed=dict(output=0,seconds=0.,n=0,N=0),
        token=dict(counts=dict.fromkeys(TOKEN_LABELS,0), known=dict.fromkeys(TOKEN_ORDER,False),
            inp_total=0, out_total=0, partial=False, input_known=False, output_known=False,
            written_known=True, cache_input=0, cache_read=0, cache_valid=0),
        costs={side:dict.fromkeys((key for key,_ in fields),0.) for side,fields in COST_PARTS})


def add_financial_total(total,row):
    total['count']+=1
    speed=total['speed'];speed['N']+=1
    if output_speed(row) is not None:
        speed['output']+=row['output'];speed['seconds']+=row['completion_latency_ms']/1000;speed['n']+=1
    cost=row.get('cost')
    if finite(cost):
        total['priced']+=1
        accumulator=_Sum()
        accumulator.total,accumulator.correction,accumulator.phase=total['cost_sum']
        accumulator.add(cost)
        total['cost_sum']=(accumulator.total,accumulator.correction,accumulator.phase)
    if cost is not None:
        total['component_priced']+=1
        for side,fields in COST_PARTS:
            counts=total['costs'][side]
            if side=='output' and (row.get('output_conflict') or
                    any(row.get(key) is None for key in ('cost_reasoning','cost_non_reasoning'))):
                counts['output_unknown']+=row.get('cost_output') or 0
            else:
                for key,field in fields:counts[key]+=row.get(field) or 0
    total['assumed']+=assumed_cost(row)
    token=total['token'];parts=token_parts(row)
    for side,mapping in (('input',{'ordinary':'uncached','read':'cached','write':'written','unclassified':'input_unknown'}),
                         ('output',{'ordinary':'output','reasoning':'reasoning','unclassified':'output_unknown'})):
        for key,destination in mapping.items():token['counts'][destination]+=parts[side][key]
    inp,cached,written,out,reasoning=(token_number(row.get(key)) for key in
                                    ('input','cached','written','output','reasoning'))
    if inp is not None:token['inp_total']+=inp
    if out is not None:token['out_total']+=out
    token['input_known']|=inp is not None;token['output_known']|=out is not None
    if inp is not None and cached is not None and cached<=inp:
        token['cache_input']+=inp;token['cache_read']+=cached;token['cache_valid']+=1
    token['partial']|=inp is None or out is None
    token['written_known']&=written is not None
    valid_input=(inp is not None and not row.get('input_conflict') and
        (cached is None or cached<=inp) and (written is None or written<=inp) and
        (cached is None or written is None or cached+written<=inp))
    valid_output=out is not None and reasoning is not None and reasoning<=out and not row.get('output_conflict')
    token['known']['cached']|=valid_input and cached is not None
    token['known']['written']|=valid_input and written is not None
    token['known']['uncached']|=valid_input and cached is not None and written is not None
    token['known']['output']|=valid_output;token['known']['reasoning']|=valid_output


def add_model_total(models,row):
    model=canonical_model(row)
    if model not in models:models[model]=new_financial_totals()
    add_financial_total(models[model],row)


def financial_cost(total):
    accumulator=_Sum()
    accumulator.total,accumulator.correction,accumulator.phase=total['cost_sum']
    return accumulator.value()


def model_breakdown(models):
    result=[]
    for model,total in models.items():
        token=total['token'];calls=total['count']
        composition=_token_view(deepcopy(token['counts']),deepcopy(token['known']),
            token['inp_total'],token['out_total'],token['partial'],token['input_known'],token['output_known'],
            token['cache_input'],token['cache_read'],token['cache_valid'],calls,token['written_known'] and bool(calls))
        result.append(dict(model=model,cost=financial_cost(total) if total['priced'] else None,
            calls=calls,priced=total['priced'],assumed=total['assumed'],token_composition=composition,
            cost_composition=_cost_view(total['costs'],total['component_priced'],calls),
            output_speed_summary=merge_speeds([total['speed']])))
    return sorted(result,key=lambda value:(-(value['cost'] or 0),value['model']))


def summarize_models(rows):
    models={}
    for row in rows:add_model_total(models,row)
    return model_breakdown(models)


def _merge_financial_compositions(summaries,partials):
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
    return dict(token_composition=composition,cost_composition=cost)


def merge_model_breakdowns(summaries):
    groups={}
    for summary in summaries:
        for model in summary['model_breakdown']:
            groups.setdefault(model['model'],[]).append(model)
    result=[]
    for model,values in groups.items():
        priced=sum(value['priced'] for value in values)
        accumulator=_Sum()
        for value in values:
            if value['cost'] is not None:accumulator.add(value['cost'])
        result.append(dict(model=model,cost=accumulator.value() if priced else None,
            calls=sum(value['calls'] for value in values),priced=priced,
            assumed=sum(value['assumed'] for value in values),
            output_speed_summary=merge_speeds([value['output_speed_summary'] for value in values]),
            **_merge_financial_compositions(values,[value['token_composition'] for value in values])))
    return sorted(result,key=lambda value:(-(value['cost'] or 0),value['model']))


SUBAGENT_CALL_FIELDS=('model','effort','response_model','model_state','model_mismatch','latest_ts','output_speed_summary')


def _subagent_finance(summaries,cost):
    """Project descendant owners once, without retaining calls or health evidence."""
    agents=[{key:summary[key] for key in (
        'home','id','title','cost','calls','priced','missing','coverage_gap','assumed',
        'model_breakdown','token_composition','cost_composition')} for summary in summaries]
    for agent,summary in zip(agents,summaries):
        agent.update({field:summary.get(field) for field in SUBAGENT_CALL_FIELDS})
        if not agent['calls']:agent['cost']=0.
    agents.sort(key=lambda value:(value['cost'] is None,-(value['cost'] or 0),value['home'],value['id']))
    calls=sum(value['calls'] for value in agents)
    priced=sum(value['priced'] for value in agents)
    return dict(cost=cost,calls=calls,priced=priced,missing=calls-priced,
        coverage_gap=any(value['coverage_gap'] for value in agents),
        assumed=sum(value['assumed'] for value in agents),
        models=merge_model_breakdowns(summaries),agents=agents)


def main_usage(summary):
    """The selected owner's summary, independent of descendant rollups."""
    return summary.get('main_usage',summary)


def merge_speeds(speeds):
    output=sum(value['output'] for value in speeds);seconds=sum(value['seconds'] for value in speeds)
    count=sum(value['n'] for value in speeds);target=sum(value['N'] for value in speeds)
    return dict(value=output/seconds if seconds else None,output=output,seconds=seconds,n=count,N=target,missing=target-count)


def _merge_compositions(summaries,partials):
    financial=_merge_financial_compositions(summaries,partials)
    speeds=[summary['output_speed_summary'] for summary in summaries]
    output=sum(value['output'] for value in speeds);seconds=sum(value['seconds'] for value in speeds)
    count=sum(value['n'] for value in speeds);target=sum(value['N'] for value in speeds)
    speed=dict(value=output/seconds if seconds else None,output=output,seconds=seconds,n=count,N=target,missing=target-count)
    maintenance_calls=sum(value['maintenance_calls'] for value in partials)
    maintenance_missing=sum(value['maintenance_missing'] for value in partials)
    maintenance_cost=(sum(value['maintenance_cost'] for value in partials if value['maintenance_cost'] is not None)
                      if maintenance_calls>maintenance_missing else None)
    return dict(**financial,output_speed_summary=speed,
        maintenance_calls=maintenance_calls,maintenance_missing=maintenance_missing,maintenance_cost=maintenance_cost)


def _recent_members(members,updated):
    # Preserve stable tie ordering while retaining the bounded Trend window.
    rows=((member,row) for member in members for row in updated[member][1]['recent'])
    def order(item):
        ordinal,(_,row)=item
        return row['ts'],str(row.get('home')),str(row.get('sid')),str(row['id']),ordinal
    selected=nlargest(RECENT_CALLS,enumerate(rows),key=order)
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
        model=canonical_model(row),
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
            'token_composition': tokens,'model_breakdown':facts['model_breakdown']}


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
            for ordinal,row in enumerate(rows[-RECENT_CALLS:],max(1,len(rows)-RECENT_CALLS+1))]
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
            model_breakdown=summarize_models(rows),
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

    def present(self, engine, updated, partials, speeds, *, include_finance=True):
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
            signature=(include_finance,tuple((member,updated[member][0]) for member in group['members']))
            previous=self.rollups.get(key)
            if previous and previous[0]==signature:
                value=previous[1]
            else:
                own_summaries=[updated[member][1] for member in group['members']]
                compositions=_merge_compositions(own_summaries,[partials[member] for member in group['members']])
                value=dict(summary,cost=group['cost'],own_cost=group['own_cost'],
                    child_cost=group['child_cost'],descendants=group['descendants'],
                    own_coverage_gap=summary['coverage_gap'],own_assumed=summary['assumed'],
                    own_calls=summary['calls'],own_priced=summary['priced'],
                    calls=group['calls'],priced=group['priced'],
                    missing=group['calls']-group['priced'],partial=group['partial'],
                    mean_cost=group['cost']/group['priced'] if group['priced'] else None,
                    coverage_gap=group['gap'],
                    assumed=sum(updated[member][1]['assumed'] for member in group['members']))
                # Reuse the owner's bounded compositions; never copy calls or
                # model breakdowns into the main-panel projection.
                value['main_usage']={field:summary[field] for field in (
                    'calls','priced','missing','mean_cost','assumed','coverage_gap',
                    'token_composition','cost_composition','output_speed_summary',
                    'model','effort','model_state','model_mismatch','response_model')}
                value['main_usage'].update({field:partials[key][field] for field in (
                    'maintenance_calls','maintenance_cost','maintenance_missing')})
                value['main_usage']['cost']=group['own_cost']
                if include_finance:
                    value.update(subagent_finance=_subagent_finance(own_summaries[1:],group['child_cost']),
                        own_model_breakdown=summary['model_breakdown'],
                        model_breakdown=merge_model_breakdowns(own_summaries))
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
