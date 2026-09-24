"""Five-minute model shares of the recorded increases on the quota cost curve."""
from bisect import bisect_left, bisect_right
from collections import defaultdict
import math
from .pricing import ALIASES

BUCKET_SECONDS=300

def prepare_model_share(series, intervals):
    rows=series['rows'];times=series['times']
    if not rows:return dict(bins={},spans=[],models=[])
    additions=defaultdict(lambda:defaultdict(float));unknown=set();names=set()
    for interval in intervals:
        separate=set(interval.get('separate_models',[]))
        for event in interval.get('cost_rows',[]):
            name=event['model']
            if ALIASES.get(name,name) in separate or not interval['start']<event['ts']<=interval['end']:continue
            index=bisect_left(times,event['ts'])
            if index>=len(rows) or rows[index]['at']>interval['end']:continue
            cost=event.get('cost');names.add(name)
            if cost is None or not math.isfinite(cost) or cost<0:unknown.add(index)
            else:additions[index][name]+=cost
        for at in interval.get('pending_usage_times',[]):
            index=bisect_left(times,at)
            if index<len(rows):unknown.add(index)
    bins={};spans=[]
    for index in range(1,len(rows)):
        previous,row=rows[index-1:index+1]
        if not row['connect'] or row.get('reset_kind') or row.get('cycle_start')!=previous.get('cycle_start'):continue
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
        values=additions[index]
        verified=(before is not None and after is not None and
                  math.isclose(after-before,sum(values.values()),rel_tol=0,abs_tol=1e-7))
        item['unknown']|=not verified or index in unknown
        if not verified or index in unknown:
            for covered in range(first,last+1):
                if covered in bins:bins[covered]['unknown']=True
        for name,cost in values.items():item['costs'][name]=item['costs'].get(name,0)+cost
    for bucket,item in bins.items():
        item['start']=bucket*BUCKET_SECONDS;item['end']=(bucket+1)*BUCKET_SECONDS
        item['total']=sum(item['costs'].values())
        item['state']='unknown' if item['unknown'] else 'cost' if item['total']>0 else 'zero'
        item['shares']={name:cost/item['total']*100 for name,cost in item['costs'].items() if cost>0} if item['state']=='cost' else {}
    return dict(bins=bins,spans=spans,models=sorted(names))

def share_at(series, at):
    return series.get('model_share',{}).get('bins',{}).get(math.floor(at/BUCKET_SECONDS))
