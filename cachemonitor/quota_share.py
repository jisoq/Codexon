"""Five-minute model shares of the recorded increases on the quota cost curve."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from itertools import pairwise
import math
from .pricing import ALIASES, request_tier

BUCKET_SECONDS=300

def prepare_model_share(series, intervals, *, packed=False):
    rows=series['rows'];times=series['times']
    if not rows:return dict(bins={},spans=[],models=[])
    additions=[];unknown=set();names={}
    for interval in intervals:
        separate=set(interval.get('separate_models',[]))
        for event in interval.get('cost_rows',[]):
            name=event['model']
            if ALIASES.get(name,name) in separate or not interval['start']<event['ts']<=interval['end']:continue
            index=bisect_left(times,event['ts'])
            if index>=len(times) or times[index]>interval['end']:continue
            identity=(name,request_tier(event));name=names.setdefault(identity,identity)
            cost=event.get('cost')
            if name[1] not in ('Standard','Fast') or cost is None or not math.isfinite(cost) or cost<0:unknown.add(index)
            else:additions.append((index,math.floor(event['ts']/BUCKET_SECONDS),name,cost))
        for at in interval.get('pending_usage_times',[]):
            index=bisect_left(times,at)
            if index<len(rows):unknown.add(index)
    # Consume equal-index contributions in their original summation order.
    # Retain one flat entry per event, not three nested maps per request.
    additions.sort(key=lambda event:event[0]);additions.reverse()
    bins={};spans=[]
    for index,(previous,row) in enumerate(pairwise(rows),1):
        if (not row['connect'] or row.get('user_excluded') or row.get('reset_kind')
                or row.get('cycle_start')!=previous.get('cycle_start')):continue
        start,end=previous['at'],row['at']
        first=math.floor(start/BUCKET_SECONDS);last=math.floor(end/BUCKET_SECONDS)
        for bucket in range(first,last+1):
            lo=max(start,bucket*BUCKET_SECONDS);hi=min(end,(bucket+1)*BUCKET_SECONDS)
            if hi>lo:
                if spans and spans[-1][2]==bucket and spans[-1][1]==lo:spans[-1]=(spans[-1][0],hi,bucket)
                else:spans.append((lo,hi,bucket))
                bins.setdefault(bucket,dict(costs={},unknown=False))
        bucket=last
        item=bins.setdefault(bucket,dict(costs={},unknown=False))
        before=previous.get('period_cost',previous.get('cycle_cost'))
        after=row.get('period_cost',row.get('cycle_cost'))
        while additions and additions[-1][0]<index:additions.pop()
        values=defaultdict(lambda:defaultdict(float))
        while additions and additions[-1][0]==index:
            _,event_bucket,name,cost=additions.pop();values[event_bucket][name]+=cost
        verified=(before is not None and after is not None and
                  math.isclose(after-before,sum(sum(costs.values()) for costs in values.values()),rel_tol=0,abs_tol=1e-7))
        item['unknown']|=not verified or index in unknown
        if not verified or index in unknown:
            for covered in range(first,last+1):
                if covered in bins:bins[covered]['unknown']=True
        for event_bucket,costs in values.items():
            item=bins.setdefault(event_bucket,dict(costs={},unknown=False))
            for name,cost in costs.items():item['costs'][name]=item['costs'].get(name,0)+cost
    for bucket,item in bins.items():
        item['start']=bucket*BUCKET_SECONDS;item['end']=(bucket+1)*BUCKET_SECONDS
        item['total']=sum(item['costs'].values())
        item['state']='unknown' if item['unknown'] else 'cost' if item['total']>0 else 'zero'
        item['shares']={name:cost/item['total']*100 for name,cost in item['costs'].items() if cost>0} if item['state']=='cost' else {}
    result=dict(bins=bins,spans=spans,models=sorted(names,key=lambda item:(item[0],{'Standard':0,'Fast':1}.get(item[1],2))))
    if packed:
        import sys
        from .quota_cycles import _PackedRows
        # Cache identical paint only when its exact coalesced representation fits
        # a small budget. Highly variable history keeps the streaming fallback.
        prepared=[];weight=64
        for span in paint_spans(result):
            weight+=sys.getsizeof(span)+sum(sys.getsizeof(value) for value in span)+16
            if len(prepared)>=8192 or weight>2*1024*1024:break
            prepared.append(span)
        else:result['_paint_spans']=prepared
        keys=sorted(bins)
        result.update(bucket_ids=keys,bins=_PackedRows(bins[key] for key in keys),spans=_PackedRows(spans))
    return result


def share_bin(share, bucket):
    bins=share.get('bins',{})
    if isinstance(bins,dict):return bins.get(bucket)
    keys=share['bucket_ids'];index=bisect_left(keys,bucket)
    return bins[index] if index<len(keys) and keys[index]==bucket else None

def share_at(series, at):
    return share_bin(series.get('model_share',{}),math.floor(at/BUCKET_SECONDS))


def paint_spans(share):
    """Merge contiguous identical paint, retaining every five-minute lookup bin."""
    if '_paint_spans' in share:
        yield from share['_paint_spans']
        return
    pending=previous=None
    for start,end,bucket in share['spans']:
        item=share_bin(share,bucket)
        style=(item['state'],item.get('shares') if item['state'] not in ('unknown','zero') else None)
        if pending is not None and pending[1]==start and style==previous:
            pending=(pending[0],end,pending[2])
        else:
            if pending is not None:yield pending
            pending=(start,end,bucket);previous=style
    if pending is not None:yield pending


def share_label(identity):
    return f"{identity[0]} ({identity[1]})"


def share_color_key(identity):
    return f"model-mode:{identity[1]}:{identity[0]}"
