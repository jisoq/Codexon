"""Filter-independent, call-scoped overlay data from priced analysis records."""
from copy import deepcopy

from .analytics import stats, population, observation_flags
from .pricing import request_tier, sum_cost
from .core import token_number, transport_label, token_parts
from .cache_misses import classify
from .cache_health import CacheHealth
from .session_costs import session_costs


TOKEN_LABELS = {'cached': '캐시 읽기', 'uncached': '일반 입력', 'written': '캐시 쓰기',
                'output': '출력·추론 제외', 'reasoning': '추론', 'input_unknown': '입력 미분류',
                'output_unknown': '출력 미분류', 'unknown': '미분류'}
TOKEN_ORDER = ('cached', 'uncached', 'written', 'output', 'reasoning', 'unknown')
COMPARISON_ORDER = ('uncached', 'written', 'output', 'reasoning')


def observed_mode(row):
    return request_tier(row)


def assumed_cost(row):
    return bool(row.get('price_assumed'))


def token_composition(session):
    """Shared disjoint token rules; unattached cumulative gaps stay out."""
    rows=list(session.get('history',()))
    counts=dict.fromkeys(TOKEN_LABELS,0);known=dict.fromkeys(TOKEN_ORDER,False)
    inp_total=out_total=0;partial=False
    cache=population(rows,'rate')
    for row in rows:
        parts=token_parts(row);inp,out=parts['input'],parts['output']
        for section,mapping in ((inp,{'ordinary':'uncached','read':'cached','write':'written','unclassified':'input_unknown'}),
                                (out,{'ordinary':'output','reasoning':'reasoning','unclassified':'output_unknown'})):
            for key,dest in mapping.items():counts[dest]+=section[key]
        i,r=token_number(row.get('input')),token_number(row.get('cached'))
        if i is not None:inp_total+=i
        if token_number(row.get('output')) is not None:out_total+=row['output']
        partial |= i is None or token_number(row.get('output')) is None
        w,o,q=(token_number(row.get(key)) for key in ('written','output','reasoning'))
        input_valid=i is not None and not row.get('input_conflict') and (r is None or r<=i) and (w is None or w<=i) and (r is None or w is None or r+w<=i)
        output_valid=o is not None and q is not None and q<=o and not row.get('output_conflict')
        known['cached'] |= input_valid and r is not None
        known['written'] |= input_valid and w is not None
        known['uncached'] |= input_valid and r is not None and w is not None
        known['output'] |= output_valid;known['reasoning'] |= output_valid
    total=inp_total+out_total
    def group(keys,denominator):
        return [dict(key=key,label=TOKEN_LABELS[key],tokens=counts[key],known=known.get(key,True),share=counts[key]/denominator if denominator else 0)
                for key in keys if counts[key] or key not in ('input_unknown','output_unknown')]
    input_parts=group(('cached','written','uncached','input_unknown'),inp_total) if any(token_number(r.get('input')) is not None for r in rows) else []
    output_parts=group(('output','reasoning','output_unknown'),out_total) if any(token_number(r.get('output')) is not None for r in rows) else []
    unknown=counts['input_unknown']+counts['output_unknown'];known['unknown']=bool(unknown)
    displayed={**counts,'unknown':unknown};maximum=max((displayed[k] for k in COMPARISON_ORDER),default=0)
    bars=[dict(key=k,label=TOKEN_LABELS[k],tokens=displayed[k],known=known[k],share=displayed[k]/total if total else 0,
               bar_share=displayed[k]/maximum if maximum else 0) for k in (*COMPARISON_ORDER,'unknown') if k!='unknown' or unknown]
    return dict(total=total,partial=partial,counts=counts,known=bool(input_parts or output_parts),category_known=known,
        input_parts=input_parts,output_parts=output_parts,input_total=inp_total,output_total=out_total,
        cached=counts['cached'],cached_share=counts['cached']/inp_total if inp_total else 0,
        cache_hit_rate=cache['value'],cache_hit_partial=bool(cache['missing']),cache_valid=cache['n'],cache_target=cache['N'],
        written_known=bool(rows) and all(token_number(r.get('written')) is not None for r in rows),
        unknown=unknown,non_cache_total=total-counts['cached'],bar_max=maximum,bars=bars,
        parts=[dict(key=k,label=TOKEN_LABELS[k],tokens=v,share=v/total) for k,v in displayed.items() if k in TOKEN_ORDER and v and total])


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
    result = dict(row)
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
    result.update(id=_call_id(row), ordinal=ordinal,
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


def summarize_session(session,health=None):
    rows = sorted(session.get('history', ()), key=lambda row: row['ts'])
    latest = rows[-1] if rows else {}
    call = stats(rows, 'cost')
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
    for ordinal,row in enumerate(rows,1):
        if row.get('key') is None:continue
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
            for ordinal,row in enumerate(rows,1)]
    recent=all_calls[first:]
    latest_call=recent[-1] if recent else {}
    lifecycle=session.get('request_state') or {}
    current=[r for r in rows if lifecycle.get('turn') and r.get('turn')==lifecycle['turn']]
    current_cost=sum_cost(current)
    current_latest=max(current,key=lambda r:r['ts'],default={})
    request={**lifecycle,'calls':len(current),'cost':current_cost['cost'] if current else None,
             'priced':sum(r.get('cost') is not None for r in current),'assumed':sum(assumed_cost(r) for r in current),
             'partial':any(r.get('cost') is None for r in current) or gap,
             'model':current_latest.get('requested_model') or current_latest.get('model'),
             'model_setting':not bool(current_latest.get('requested_model')),
             'response_model':current_latest.get('response_model'),
             'model_mismatch':bool(current_latest.get('model_alert_confirmed')),
             'mode':observed_mode(current_latest),'effort':current_latest.get('effort'),
             'usage_at':current_latest.get('ts')}
    mismatches=[r for r in rows if r.get('model_alert_confirmed')]
    tokens=token_composition(session)
    statuses=[]
    if gap:
        statuses.append({'text':'이전 기록 누락','severity':'muted'})
    if not rows:
        statuses.append({'text':'호출 기록 없음','severity':'muted'})
    evidence=[]
    if call['missing'] or gap:
        evidence.append(f"산정 {call['n']:,} / {len(rows):,}호출")
    assumed=sum(assumed_cost(row) for row in rows)
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
            'cost': call['sum'] if rows else None, 'mean_cost': call['mean'],
            'latest_cost': latest.get('cost'), 'calls': len(rows), 'priced': call['n'],
            'missing': call['missing'], 'partial': bool(call['missing']),
            'unattached_total':token_number((session.get('unclassified') or {}).get('total')),
            'assumed': assumed, 'coverage_gap': gap, 'warnings': latest_call.get('warnings',[]),
            'latest_ts': latest.get('ts'), 'latest_key': latest.get('key'),
            'latest':latest_call,'request':request,'recent':recent,'all_calls':all_calls,'cache_health':health,
            'model_mismatch_count':sum(r['model_mismatch'] for r in all_calls),
            'observation_missing_count':sum(observation_flags(r)['missing'] for r in all_calls),
            'observation_conflict_count':sum(observation_flags(r)['conflict'] for r in all_calls),
            'cache_degradation':{'count':len(degradation),'events':deepcopy(degradation),
                'current':bool(latest_call.get('cache_degradation'))},
            'statuses':statuses,'session_evidence':evidence,
            'model_incident':{'count':len(mismatches),'key':mismatches[-1]['key'] if mismatches else None},
            'token_composition': tokens}


class OverlaySummaries:
    def __init__(self):
        self.cache = {}
        self.health = {}
        self.rollups = {}

    def collect(self, engine):
        updated = {}
        for key, state in engine.sessions.items():
            source=state.get('source',state['prepared'])
            coverage_gaps=source.get('coverage_gaps',())
            revision = (state['revision'],bool(coverage_gaps))
            previous = self.cache.get(key)
            summary = previous[1] if previous and previous[0] == revision else summarize_session(
                dict(state['prepared'],coverage_gaps=coverage_gaps),
                self.health.setdefault(key,OverlayCacheHealth()).update(state['prepared']['history']))
            updated[key] = (revision, summary)
        self.cache = updated
        self.health={key:self.health[key] for key in updated}
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
                rows=[row for member in group['members']
                      for row in engine.sessions[member]['prepared']['history']]
                value=dict(summary,cost=group['cost'],own_cost=group['own_cost'],
                    child_cost=group['child_cost'],descendants=group['descendants'],
                    own_calls=summary['calls'],own_priced=summary['priced'],
                    calls=group['calls'],priced=group['priced'],
                    missing=group['calls']-group['priced'],partial=group['partial'],
                    mean_cost=group['cost']/group['priced'] if group['priced'] else None,
                    coverage_gap=group['gap'],token_composition=token_composition({'history':rows}),
                    assumed=sum(updated[member][1]['assumed'] for member in group['members']))
            merged[key]=(signature,value)
            result.append(value)
        self.rollups=merged
        return result
