"""Pair completed local requests with observed allowance, without rewriting it."""
from bisect import bisect_left, bisect_right
import math


def merged_ranges(ranges):
    result=[]
    for lo,hi in sorted(ranges):
        if result and lo<=result[-1][1]:result[-1][1]=max(result[-1][1],hi)
        else:result.append([lo,hi])
    return result


def local_request_intervals(cycle, requests, clip):
    """Use one pre-request observation and up to two post-completion observations.

    The extra post-completion sample accommodates the normal delayed quota
    publication. It is bounded by the existing continuity check, never an idle
    session flag or an unbounded wait. A priced request establishes a usable
    cost subtotal and observation range even when another request is unresolved.
    Unpriced requests cannot erase that evidence or contribute invented costs.
    """
    points=cycle.get('endpoints',[]);times=[p[0] for p in points]
    if len(times)<2:return [],dict(idle_delta=0,pending_delta=0,unmatched_calls=0,pending_windows=0)

    def bounds(start,end,pending=False):
        if start is None:start=end
        if end is None:end=times[-1]
        if not all(type(v) in (float,int) and math.isfinite(v) for v in (start,end)) or end<start:return None
        if end<times[0] or start>times[-1]:return None
        lo=bisect_left(times,start)-1
        hi=bisect_left(times,end)
        if not pending and (lo<0 or hi>=len(times)):return None
        lo=max(0,lo);hi=min(len(times)-1,hi+1)
        return (lo,hi) if hi>lo else None

    rows=cycle.get('cost_rows',[])
    from .pricing import ALIASES
    known_ids={r.get('uid') for r in rows if r.get('uid') and (r.get('cost') is not None or
               ALIASES.get(r['model'],r['model']) in cycle['separate_models'])}
    blocked=[];candidates=[];all_ranges=[]
    eligible=[r for r in rows if ALIASES.get(r['model'],r['model']) not in cycle['separate_models']]
    for index,row in enumerate(eligible):
        start=row.get('local_request_start',row.get('request_start'));end=row['ts']
        span=bounds(start,end)
        if span is not None:
            all_ranges.append(span)
            if row['cost'] is not None:candidates.append((span,index))
            else:blocked.append(span)
        else:
            span=bounds(start,end,True)
            if span is not None:blocked.append(span);all_ranges.append(span)
    for request in requests:
        if request['uid'] in known_ids:continue
        # Exact completed/ongoing request evidence only; task lifecycle flags
        # cannot keep an unrelated future interval open.
        span=bounds(request.get('start'),request.get('end'),True)
        if span is not None:blocked.append(span);all_ranges.append(span)
    blocked=merged_ranges(blocked);ends=[r[1] for r in blocked]
    accepted=[]
    def overlaps(span):
        position=bisect_right(ends,span[0])
        return position<len(blocked) and blocked[position][0]<span[1]
    for span,index in candidates:
        if overlaps(span):
            first_post=bisect_left(times,eligible[index]['ts'])
            shorter=(span[0],first_post)
            # A newly started request must not withdraw an already completed
            # pair solely because it overlaps the optional extra observation.
            if span[0]<first_post<span[1] and not overlaps(shorter):
                span=shorter
        accepted.append((span,index))
    ranges=merged_ranges([span for span,_ in accepted])
    included={index for _,index in accepted}
    selected=[{**r,'request_start':r.get('local_request_start',r.get('request_start'))}
              for index,r in enumerate(eligible) if index in included]
    output=[]
    source={**cycle,'cost_rows':selected,'pending_usage_times':[],'pending_usage_calls':0,
            'mode_standard_cost':None,'unknown_mode_calls':0}
    for lo,hi in ranges:
        part=clip(source,times[lo],times[hi])
        if part is not None and part['priced_calls']:
            part.update(id=f"{cycle['id']}:local:{times[lo]}",local_attributed=True,
                        provisional=hi==len(times)-1 and bool(cycle.get('provisional')))
            output.append(part)
    delta=lambda spans:sum(max(0,points[hi][1]-points[lo][1]) for lo,hi in spans)
    available=delta(merged_ranges(all_ranges))
    used=delta(ranges)
    return output,dict(idle_delta=max(0,cycle['delta']-available),
                       pending_delta=max(0,available-used),unmatched_calls=len(eligible)-len(selected),pending_windows=len(blocked))
