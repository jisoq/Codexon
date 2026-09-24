"""Small chronological policy replay. All money is API equivalent, never quota."""
from dataclasses import dataclass
from math import ceil, isfinite
from .cache_audit import TTL_SECONDS
from .pricing import token_cost


@dataclass(frozen=True)
class Gap:
    at: float
    seconds: float
    returned: bool
    benefit_lower: float
    maintenance_upper: float
    origin: str = 'unknown'
    settled: bool = True
    scope: str | None = None
    comparison: str = 'legacy_unclassified'


def changed_conditions(previous,current):
    """Only observed changes establish a new cohort; missing data never does."""
    known=lambda v:v not in (None,'','미확인')
    changed=[k for k in ('model','effort','service_tier','compaction_epoch')
             if known(previous.get(k)) and known(current.get(k)) and previous[k]!=current[k]
             and (k!='service_tier' or not any(r.get('mode_conflict') for r in (previous,current)))]
    if all(type(r.get('input')) is int and not r.get('input_conflict') for r in (previous,current)) and current['input']<previous['input']:
        changed.append('context_shrink')
    return changed


def scoped_history(history):
    """Continuous context regime, including unknown/unprofitable calls inside it."""
    result=[];known={};scope=None;start=None
    for row in sorted(history,key=lambda r:r['ts']):
        if row.get('purpose')=='maintenance':continue
        if scope is None or changed_conditions(known,row):
            start=0 if scope is None else row['ts']
            scope=row['key'];known={}
        for key in ('model','effort','service_tier','compaction_epoch','input'):
            if key=='input' and row.get('input_conflict'):continue
            if key=='service_tier' and row.get('mode_conflict'):continue
            if row.get(key) not in (None,'','미확인'):known[key]=row[key]
        result.append(dict(row,policy_scope=scope,policy_scope_start=start))
    return result


def decide(gaps, *, latency_bound, scheduler_slack, max_calls):
    """Bootstrap from natural warm/cold calls and supplied cost scenarios.

    Legacy maintenance_upper is a scenario cost, not a server total-cost cap.
    Caller supplies same-model scenarios using pricing.token_cost.
    No prior maintenance sample is necessary. Missing natural bounds defer, and
    nonpositive holdout value selects off. Unknown-origin gaps never become humans.
    """
    if (not all(type(x) in (int, float) and isfinite(x) and x >= 0
                for x in (latency_bound, scheduler_slack)) or
            type(max_calls) is not int or not 0 < max_calls <= 100):
        return dict(state='waiting', reason='missing_execution_bounds', calls=0)
    interval = TTL_SECONDS - latency_bound - scheduler_slack
    if interval <= 0:
        return dict(state='off', reason='latency_exceeds_lifetime', calls=0)
    if any(g.origin in ('human','submission') and any(type(v) not in (int,float) or not isfinite(v) or v<0
           for v in (g.seconds,g.benefit_lower,g.maintenance_upper)) for g in gaps):
        return dict(state='waiting',reason='natural_cost_bounds_unobserved',calls=0)
    rows = sorted((g for g in gaps if g.origin in ('human','submission') and
                   all(type(v) in (int,float) and isfinite(v) and v >= 0
                       for v in (g.seconds,g.benefit_lower,g.maintenance_upper))), key=lambda g:g.at)
    if len({g.at for g in rows}) < 2:
        return dict(state='waiting', reason='need_independent_natural_return_history', calls=0)
    split = len(rows)//2
    train, check = rows[:split], rows[split:]

    def value(part, cap):
        total = 0.
        for g in part:
            n = min(cap, max(0, ceil(g.seconds/interval)-1))
            covered = g.settled and g.returned and g.seconds >= TTL_SECONDS and g.seconds < n*interval+TTL_SECONDS and n>0
            total += (g.benefit_lower if covered else 0) - n*g.maintenance_upper
        return total

    # Ties prefer no work, then fewer calls. Do not optimize on the holdout.
    cap = max(range(max_calls+1), key=lambda n:(value(train,n),-n))
    net = value(check,cap)
    if cap == 0 or net <= 0:
        return dict(state='off',reason='no_positive_forward_estimate',calls=0,net_scenario=net)
    return dict(state='eligible',reason='natural_history_bounds',calls=cap,interval=interval,
                net_scenario=net,measured_saving=None,basis='api_equivalent')


def cost_bounds(previous,current,output_cap,input_extra=0):
    """Comparable natural-call scenario, not causal proof of idle expiration.

    output_cap is the legacy name for the output-token scenario (including
    reasoning). Only a verified execution path may enforce it. Unknown writes stay unknown.
    Read reuse is a scenario assumption; actual zero-hit work is charged in full.
    """
    if type(output_cap) is not int or output_cap<=0:return None
    changed=changed_conditions(previous,current)
    if not all(previous.get(k) not in (None,'','미확인') for k in ('model','effort','service_tier')):return None
    # A known transition offers no attributable benefit in the source cohort,
    # but its preceding idle maintenance would still have cost money.
    target=previous if changed else current
    if not changed and not all(previous.get(k)==current.get(k) for k in ('model','effort','service_tier')):return None
    if not changed and previous.get('compaction_epoch')!=current.get('compaction_epoch'):return None
    keys=('input','cached','output')
    if any(type(r.get(k)) is not int or r[k]<0 for r in (previous,target) for k in keys):return None
    if any(r['cached']>r['input'] or r.get('input_conflict') for r in (previous,target)):return None
    lost=0 if changed else max(0,min(previous['cached'],current['input'])-current['cached'])
    # Cost of replacement input minus reading it, at the SAME target model rates.
    # Unknown write counts are not observations of writes. Bound that missing
    # composition using BOTH endpoints; never replace the source usage.
    cold_values=[token_cost({**target,'input':lost,'cached':0,'written':w,'output':0,'reasoning':0})['cost'] for w in (0,lost)]
    cold=min(cold_values) if all(v is not None for v in cold_values) else None
    warm=token_cost({**target,'input':lost,'cached':lost,'written':0,'output':0,'reasoning':0})['cost']
    maintenance_values=[token_cost({**previous,'input':previous['input']+input_extra,
        'cached':previous['cached'],'written':w,'output':output_cap,'reasoning':None})['cost']
        for w in (0,previous['input']+input_extra-previous['cached'])]
    maintenance=max(maintenance_values) if all(v is not None for v in maintenance_values) else None
    if any(x is None for x in (cold,warm,maintenance)):return None
    return dict(benefit_lower=max(0,cold-warm),maintenance_upper=maintenance,
                bound_kind='changed_conditions_no_benefit' if changed else 'comparable_call_scenario',output_cap=output_cap,
                reuse_assumption=previous['cached'],write_composition='bounded_not_observed')


def operating_scenarios(row,profile,input_extra,maintenance):
    """Natural output is a bootstrap proxy, not a promise about an ACK response.

    Missing natural records remain a veto. After maintenance, include every
    matching observation; an unknown cost cannot make the forecast cheaper.
    """
    if profile.get('output_missing') or not profile.get('output_samples'):return None
    if any(not r['usage_known'] or r['cost'] is None for r in maintenance):return None
    mean=profile.get('output_mean');high=profile.get('output_high')
    if type(mean) not in (int,float) or type(high) is not int:return None
    if maintenance:
        # Retain the larger natural baseline while the initial pilot is small.
        mean=max(mean,sum(r['output'] for r in maintenance)/len(maintenance))
        high=max(high,max(r['output'] for r in maintenance))
    expected=cost_bounds(row,{**row,'cached':0},max(1,ceil(mean)),input_extra)
    cold=cost_bounds({**row,'cached':0},{**row,'cached':0},max(1,high),input_extra)
    if not expected or not cold:return None
    return dict(benefit_lower=expected['benefit_lower'],maintenance_expected=expected['maintenance_upper'],
                maintenance_adverse=cold['maintenance_upper'],output_high=max(1,high),
                output_estimate=mean,server_output_cap=False,
                basis='natural_output_and_maintenance' if maintenance else 'natural_output_proxy',
                natural_samples=profile['output_samples'],maintenance_samples=len(maintenance),
                write_composition='scenario_not_observed',reuse_assumption=row['cached'])
