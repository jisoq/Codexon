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


def decide(gaps, *, latency_bound, scheduler_slack, max_calls):
    """Bootstrap from natural warm/cold calls and a bounded output allowance.

    Caller supplies conservative same-model cost bounds using pricing.token_cost.
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

    An execution cap covers output AND reasoning. Unknown writes stay unknown.
    Read reuse is a scenario assumption; actual zero-hit work is charged in full.
    """
    if type(output_cap) is not int or output_cap<=0:return None
    if not all(previous.get(k)==current.get(k) and previous.get(k) is not None
               for k in ('model','effort','service_tier')):return None
    if previous.get('compaction_epoch')!=current.get('compaction_epoch'):return None
    keys=('input','cached','output')
    if any(type(r.get(k)) is not int or r[k]<0 for r in (previous,current) for k in keys):return None
    if any(r['cached']>r['input'] or r.get('input_conflict') for r in (previous,current)):return None
    if current['input']<previous['input'] or previous['cached']<=0:return None
    lost=max(0,min(previous['cached'],current['input'])-current['cached'])
    # Cost of replacement input minus reading it, at the SAME target model rates.
    # Unknown write counts are not observations of writes. Bound that missing
    # composition using BOTH endpoints; never replace the source usage.
    cold_values=[token_cost({**current,'input':lost,'cached':0,'written':w,'output':0,'reasoning':0})['cost'] for w in (0,lost)]
    cold=min(cold_values) if all(v is not None for v in cold_values) else None
    warm=token_cost({**current,'input':lost,'cached':lost,'written':0,'output':0,'reasoning':0})['cost']
    maintenance_values=[token_cost({**previous,'input':previous['input']+input_extra,
        'cached':previous['cached'],'written':w,'output':output_cap,'reasoning':None})['cost']
        for w in (0,previous['input']+input_extra-previous['cached'])]
    maintenance=max(maintenance_values) if all(v is not None for v in maintenance_values) else None
    if any(x is None for x in (cold,warm,maintenance)):return None
    return dict(benefit_lower=max(0,cold-warm),maintenance_upper=maintenance,
                bound_kind='comparable_call_scenario',output_cap=output_cap,
                reuse_assumption=previous['cached'],write_composition='bounded_not_observed')
