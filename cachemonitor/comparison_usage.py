from array import array
from copy import deepcopy
from typing import NamedTuple
from itertools import islice
from struct import calcsize
import sys
from hashlib import sha256
import json
import re
from math import isfinite

from .analytics import INPUT_BANDS, CACHE_BANDS, condition_match, output_speed, sorted_quantile
from .category_usage import category, display_rows
from .prepared_category import BASE_FIELDS, SPEED_FIELDS
from .prepared_store import CapacityExceeded, ReadCancelled, _size, pack
from .prepared_sums import _Sum
from .pricing import display_tier

DIMENSIONS=('all','project','source','home','input_band','cache_band')
METRICS=('calls','cost','cost_per_call','latency','tps','input_per_call','output_per_call','reasoning')
MEAN_FIELDS=dict(cost_per_call='cost',latency='latency',tps='tps',input_per_call='input',output_per_call='output',reasoning='reasoning')
MEANS=frozenset(MEAN_FIELDS)
FIELDS=tuple(dict.fromkeys((*BASE_FIELDS,*SPEED_FIELDS,'cost','input','cached','rate')))
LIMIT=32*1024*1024
_FAMILIES={name:i for i,name in enumerate(('astra','sol','terra','luna','daybreak',''))}
_EFFORTS={name:i for i,name in enumerate(('ultra','max','xhigh','high','medium','low'))}
_MODE_MARKS={'Standard':'','Fast':' ⚡'}


def dependency_stamp(dependencies):
    from .prepared_ingest import fact_dependencies
    return sha256(pack(fact_dependencies(dependencies))).hexdigest()


def band(row, dimension):
    if dimension=='input_band':
        return next((label for lo,hi,label in INPUT_BANDS
                     if condition_match(row,{'input_band':(lo,hi)},'response')),'미확인')
    return next((label for value,label in CACHE_BANDS
                 if condition_match(row,{'cache_band':value},'response')),'미확인')


def matches(row, scope):
    return all(not scope.get(name) or band(row,name)==scope[name]
               for name in ('input_band','cache_band'))


class Totals:
    __slots__=('count','sums','known','_fixed_bytes')
    fields=('cost','latency','tps','input','output','reasoning')

    def __init__(self):
        self.count=0
        self.sums={key:_Sum() for key in self.fields}
        self.known={key:0 for key in self.fields}
        self._fixed_bytes=(sys.getsizeof(self)+sys.getsizeof(self.known)+sys.getsizeof(self.sums)
                           +sys.getsizeof(0)+sum(_size(key) for key in self.known)
                           +sum(_size(key)+sys.getsizeof(value)+sys.getsizeof(value.phase)
                                +sys.getsizeof(value.correction) for key,value in self.sums.items()))

    def add(self,row):
        self.count+=1
        speed=output_speed(row)
        values=dict(cost=row.get('cost'),input=row.get('input'),output=row.get('output'),reasoning=row.get('reasoning'),
                    tps=speed,latency=row.get('completion_latency_ms',0)/1000 if speed is not None else None)
        for key,value in values.items():
            if type(value) in (int,float) and isfinite(value) and value>=0:
                self.sums[key].add(value);self.known[key]+=1
            else:values[key]=None
        return values

    def merge(self,other):
        self.count+=other.count
        for key in self.fields:
            self.sums[key].add(other.sums[key].value());self.known[key]+=other.known[key]

    def retained_bytes(self):
        return (self._fixed_bytes+sys.getsizeof(self.count)
                +sum(sys.getsizeof(value) for value in self.known.values())
                +sum(sys.getsizeof(value.total) for value in self.sums.values()))

    def metrics(self,budget=None):
        scratch=sys.getsizeof([])+2*len(METRICS)*calcsize('P')
        if budget:budget.reserve(scratch+_tuple_bytes(len(METRICS)))
        result=[]
        for metric,field in [('calls',None),('cost','cost'),('cost_per_call','cost'),('latency','latency'),
                             ('tps','tps'),('input_per_call','input'),('output_per_call','output'),('reasoning','reasoning')]:
            if field:
                known=self.known[field];value=self.sums[field].value() if known else None
            else:known=value=self.count
            if metric in MEANS and value is not None:value/=known
            if budget:
                budget.reserve(_tuple_bytes(4)+_size(value)+_size(known)+_size(self.count)+_size(known!=self.count))
            result.append(_Metric(value,known,self.count,known!=self.count))
        metrics=tuple(result)
        if budget:budget.release(scratch)
        return metrics

    def result(self):
        return {key:value._asdict() for key,value in zip(METRICS,self.metrics())}


class _OwnerCacheUnavailable(CapacityExceeded):
    pass


class _Budget:
    def __init__(self,maximum,*,exhausted=CapacityExceeded):
        self.maximum=maximum;self.used=0;self.peak=0
        self.exhausted=exhausted

    def reserve(self,size):
        if self.used+size>self.maximum:
            raise self.exhausted('comparison aggregates exceed cache budget')
        self.used+=size;self.peak=max(self.peak,self.used)

    def release(self,size):
        self.used-=size


class Box(NamedTuple):
    p10: int | float
    q1: int | float
    median: int | float
    q3: int | float
    p90: int | float

    def result(self):
        return dict(kind='box',p10=self.p10,q1=self.q1,median=self.median,q3=self.q3,p90=self.p90)


class _Observations:
    __slots__=('floats','unsigned','large','order','counts','count')
    lanes=('floats','unsigned','large')

    def __init__(self):
        self.floats=array('d');self.unsigned=array('Q');self.large=[];self.order=None
        self.counts=[0,0,0];self.count=0

    def retained_bytes(self):
        return (sys.getsizeof(self)+sum(sys.getsizeof(getattr(self,key)) for key in (*self.lanes,'order'))
                +_size(self.counts)+sys.getsizeof(self.count)
                +sum(sys.getsizeof(self.large[i]) for i in range(self.counts[2])))

    def _buffer_bytes(self,name,capacity):
        if name=='floats':return sys.getsizeof(array('d'))+capacity*array('d').itemsize
        if name=='unsigned':return sys.getsizeof(array('Q'))+capacity*array('Q').itemsize
        if name=='large':return sys.getsizeof([])+capacity*calcsize('P')
        return sys.getsizeof(bytearray())+capacity+bool(capacity)

    def _grow(self,name,required,budget):
        old=getattr(self,name)
        if old is not None and required<=len(old):return
        capacity=_capacity(required)
        budget.reserve(self._buffer_bytes(name,capacity))
        if isinstance(old,array):
            new=array(old.typecode,[0])*capacity
            memoryview(new)[:len(old)]=memoryview(old)
        elif name=='large':
            new=[None]*capacity
            for i in range(self.counts[2]):new[i]=old[i]
        else:
            new=bytearray(capacity)
            if old is not None:memoryview(new)[:len(old)]=memoryview(old)
        setattr(self,name,new);budget.release(sys.getsizeof(old))

    def add(self,value,budget):
        lane=0 if type(value) is float else 1 if value<2**64 else 2
        if self.order is None and self.count and not self.counts[lane]:
            self._grow('order',self.count+1,budget)
            prior=next(i for i,count in enumerate(self.counts) if count)
            for i in range(self.count):self.order[i]=prior
        elif self.order is not None:self._grow('order',self.count+1,budget)
        self._grow(self.lanes[lane],self.counts[lane]+1,budget)
        if lane==2:budget.reserve(sys.getsizeof(value))
        getattr(self,self.lanes[lane])[self.counts[lane]]=value
        if self.order is not None:self.order[self.count]=lane
        self.counts[lane]+=1;self.count+=1

    def clone_bytes(self):
        return (sys.getsizeof(self)+_size(self.counts)+sys.getsizeof(self.count)
                +sum(self._buffer_bytes(name,_capacity(count)) for name,count in zip(self.lanes,self.counts))
                +(self._buffer_bytes('order',_capacity(self.count)) if self.order is not None else sys.getsizeof(None))
                +sum(sys.getsizeof(self.large[i]) for i in range(self.counts[2])))

    def clone(self):
        result=object.__new__(_Observations)
        for lane,name in enumerate(self.lanes):
            count=self.counts[lane];source=getattr(self,name);capacity=_capacity(count)
            if isinstance(source,array):
                target=array(source.typecode,[0])*capacity
                memoryview(target)[:count]=memoryview(source)[:count]
            else:
                target=[None]*capacity
                for i in range(count):target[i]=source[i]
            setattr(result,name,target)
        result.order=None
        if self.order is not None:
            result.order=bytearray(_capacity(self.count))
            memoryview(result.order)[:self.count]=memoryview(self.order)[:self.count]
        result.counts=self.counts[:];result.count=self.count
        return result

    def compact(self,budget):
        for name,count in (*zip(self.lanes,self.counts),('order',self.count)):
            old=getattr(self,name)
            if old is None or len(old)==count:continue
            try:budget.reserve(self._buffer_bytes(name,count))
            except CapacityExceeded:continue
            new=old[:count]
            setattr(self,name,new);budget.release(sys.getsizeof(old))

    def extend_values(self,values):
        if sum(count>0 for count in self.counts)==1:
            lane=next(i for i,count in enumerate(self.counts) if count)
            source=getattr(self,self.lanes[lane])
            if isinstance(source,array):values.extend(memoryview(source)[:self.counts[lane]])
            else:values.extend(source if len(source)==self.counts[lane] else islice(source,self.counts[lane]))
            return
        positions=[0,0,0]
        for i in range(self.count):
            lane=self.order[i]
            values.append(getattr(self,self.lanes[lane])[positions[lane]])
            positions[lane]+=1


def _capacity(count):
    return 1<<max(3,(count-1).bit_length()) if count else 0


def _tuple_bytes(length):
    return sys.getsizeof(())+length*calcsize('P')


class _Metric(NamedTuple):
    value: int | float | None
    known: int
    total: int
    partial: bool


class _ComparisonKey(NamedTuple):
    parent: str
    model: str
    effort: str
    service_tier: str


class _Row(NamedTuple):
    key: _ComparisonKey
    label: str
    metrics: tuple[_Metric,...]
    boxes: tuple[Box | None,...]


class _Fold(NamedTuple):
    rows: tuple[_Row,...]
    metrics: tuple[_Metric,...]
    response_count: int
    session_count: int


def _describe(parts,known,budget):
    if known!=sum(part.count for part in parts):
        raise ValueError('comparison observation count differs from coverage')
    if not known:return None
    scratch=(sys.getsizeof([])+known*calcsize('P')*2
             +sum(part.counts[0] for part in parts)*sys.getsizeof(0.0)
             +sum(part.counts[1] for part in parts)*sys.getsizeof(2**64-1)+8192)
    budget.reserve(scratch)
    try:
        values=[]
        for part in parts:part.extend_values(values)
        values.sort()
        size=_tuple_bytes(5)+5*max(sys.getsizeof(int(values[-1])),sys.getsizeof(0.0))
        budget.reserve(size)
        box=Box(*(sorted_quantile(values,p) for p in (.1,.25,.5,.75,.9)))
        budget.release(size-_size(box))
        return box
    finally:
        budget.release(scratch)


class GroupAggregate:
    __slots__=('totals','observations')

    def __init__(self):
        self.totals=Totals();self.observations={key:_Observations() for key in MEAN_FIELDS}


    def retained_bytes(self):
        return (sys.getsizeof(self)+self.totals.retained_bytes()+sys.getsizeof(self.observations)
                +sum(_size(key)+value.retained_bytes() for key,value in self.observations.items()))

    def add(self,row,budget):
        before=self.totals.retained_bytes();accepted=self.totals.add(row)
        change=self.totals.retained_bytes()-before
        if change>0:budget.reserve(change)
        else:budget.release(-change)
        self.observe(accepted,budget)

    def observe(self,accepted,budget):
        for metric,field in MEAN_FIELDS.items():
            if accepted[field] is not None:self.observations[metric].add(accepted[field],budget)

    def clone_bytes(self):
        return (sys.getsizeof(self)+self.totals.retained_bytes()+sys.getsizeof(self.observations)
                +sum(_size(key)+value.clone_bytes() for key,value in self.observations.items()))

    def compact(self,budget):
        for values in self.observations.values():values.compact(budget)

    def clone(self):
        result=object.__new__(GroupAggregate);result.totals=deepcopy(self.totals)
        result.observations={key:value.clone() for key,value in self.observations.items()}
        return result

    def distributions(self,budget):
        result={}
        for metric,field in MEAN_FIELDS.items():
            box=_describe((self.observations[metric],),self.totals.known[field],budget)
            result[metric]=box.result() if box else None
        return result


def _new_group(groups,key,label,budget):
    value=GroupAggregate()
    budget.reserve(256+_size(key)+_size(label)+value.retained_bytes())
    groups[key]=(label,value)
    return value


def mark_bound(rows,metric):
    maximum=max([max(row['value'] or 0,(row.get('plot') or {}).get('p90',0)) for row in rows]+[0])
    return maximum*1.05 if metric in MEANS and maximum else 1 if metric in MEANS else max(1,maximum*1.05)


def group_key(row,dimension):
    parent=('전체' if dimension=='all' else band(row,dimension)
            if dimension in ('input_band','cache_band') else category(row,dimension))
    return _ComparisonKey(parent,category(row,'model'),category(row,'effort'),display_tier(row))


def add_row(groups,row,dimension,budget,previous=None):
    key=group_key(row,dimension)
    if key not in groups:
        label=(row.get('project_name') or key[0]) if dimension=='project' else key[0]
        _new_group(groups,key,label,budget)
    elif previous is not None and groups[key] is previous.get(key):
        label,value=groups[key]
        budget.release(value.retained_bytes())
        budget.reserve(value.clone_bytes())
        groups[key]=(label,value.clone())
    groups[key][1].add(row,budget)


def _owner_data(engine,scope,dimension,cache,budget):
    """Reuse untouched owners; replace affected entries atomically after reading."""
    snapshot=engine.snapshot
    entries={};processed=reused=0
    progress=getattr(engine,'progress',None)
    if progress:progress('aggregate')
    for state,lo,hi in engine.call_ranges(scope):
        owner=state['owner'];old=cache.get(owner)
        version=state['revision'];stamp=None;ordinal=None
        context=tuple((name,state['call_owner'].get(name)) for name in BASE_FIELDS)
        overhead=512+_size(owner)+_size(context)
        if old and old.get('context')!=context:old=None
        if old and old['version']==version:
            entry=old
        else:
            metadata=snapshot.owner(*owner)
            stamp=dependency_stamp(metadata['dependencies'])
            if old and old['stamp']==stamp:
                entry=dict(old,version=version)
            else:
                if old:
                    checkpoint=snapshot.derived(*owner,'checkpoint') or {}
                    proof=checkpoint.get('comparison_append') or {}
                    if (proof.get('format')==1 and proof.get('prior')==old['stamp']
                            and proof.get('target')==stamp and type(proof.get('ordinal')) is int):
                        ordinal=proof['ordinal']+1
                local=_Budget(budget.maximum-budget.used-overhead,exhausted=budget.exhausted)
                if ordinal is not None:
                    local.reserve(old['weight'])
                    groups=old['groups'].copy()
                else:groups={}
                after=None
                while True:
                    page=snapshot.projected_calls(owner,FIELDS,start=lo,end=hi,after=after,
                                                  filters=scope,ordinal_start=ordinal)
                    engine.read_metrics['category_projection_rows']=engine.read_metrics.get('category_projection_rows',0)+len(page.rows)
                    for row in page.rows:
                        processed+=1
                        if matches(row,scope):add_row(groups,row,dimension,local,old['groups'] if ordinal is not None else None)
                    complete=page.complete;after=page.after
                    del page
                    if progress:progress(dict(stage='aggregate',processed=processed,unit='calls'))
                    if complete:break
                for key,(_,value) in groups.items():
                    if ordinal is None or groups[key] is not old['groups'].get(key):value.compact(local)
                entry=dict(version=version,stamp=stamp,groups=groups,weight=local.used,context=context)
        if entry is old or (old and entry.get('groups') is old['groups']):
            reused+=sum(value[1].totals.count for value in entry['groups'].values())
        budget.reserve(entry['weight']+overhead)
        entries[owner]=entry
    engine.read_metrics['comparison_reused_calls']=reused
    engine.read_metrics['comparison_cache_bytes']=budget.used
    return entries


def choose_metric(data,metric):
    """Select a chart metric from delivered aggregates, without a reader query."""
    metric=metric if metric in METRICS else 'calls'
    group_totals={}
    for row in data['rows']:
        group_totals.setdefault(row['group'],[]).append(row['metrics'][metric])
    def group_value(values):
        known=sum(value['known'] for value in values)
        total=sum((value['value'] or 0)*(value['known'] if metric in MEANS else 1) for value in values)
        return total/known if metric in MEANS and known else total
    values={key:group_value(items) for key,items in group_totals.items()}
    rows=[dict(row,**row['metrics'][metric],group_value=values[row['group']],
               plot=row['distributions'].get(metric)) for row in data['rows']]
    return dict(data,rows=rows,metric=metric,summary=data['metrics'][metric],
                maximum=max([row['value'] or 0 for row in rows]+[0]),markMaximum=mark_bound(rows,metric))


def _same_population(previous,entries):
    if len(previous)!=len(entries):return False
    return all(old_owner==owner and old.get('context')==entry.get('context')
               and old['groups'] is entry['groups']
               for (old_owner,old),(owner,entry) in zip(previous.items(),entries.items()))


def _check_cancel(cancel):
    if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
        raise ReadCancelled('comparison cancellation reached')


class _FoldGroup:
    __slots__=('label','totals','parts','reusable')

    def __init__(self,label,reusable):
        self.label=label
        self.totals=Totals()
        self.parts=[]
        self.reusable=reusable


def _fold(entries,budget,previous=None,cancel=None):
    groups={};total=Totals();sessions=0
    budget.reserve(sys.getsizeof(groups)+total.retained_bytes())
    prior=previous['entries'] if previous and previous.get('fold') else None
    same_owners=prior is not None and len(prior)==len(entries)
    if same_owners:
        for old,owner in zip(prior,entries):
            _check_cancel(cancel)
            if old!=owner:
                same_owners=False;break
    for owner,entry in entries.items():
        _check_cancel(cancel)
        if entry['groups']:sessions+=1
        old=prior[owner] if same_owners else None
        for key,(label,value) in entry['groups'].items():
            if key not in groups:
                group=_FoldGroup(label,same_owners)
                budget.reserve(256+_size(key)+_size(label)+group.totals.retained_bytes())
                groups[key]=group
            group=groups[key]
            if group.reusable:
                old_group=old['groups'].get(key)
                if old_group is None or old_group[1] is not value:group.reusable=False
            before=group.totals.retained_bytes()+total.retained_bytes()
            group.totals.merge(value.totals);total.merge(value.totals)
            budget.reserve(group.totals.retained_bytes()+total.retained_bytes()-before+4*calcsize('P'))
            group.parts.append(value)
    if same_owners:
        for owner,old in prior.items():
            _check_cancel(cancel)
            current=entries[owner]
            if old['groups'] is current['groups']:continue
            for key in old['groups']:
                if key not in current['groups'] and key in groups:groups[key].reusable=False
        for row in previous['fold'].rows:
            _check_cancel(cancel)
            group=groups.get(row.key)
            if group is not None and group.reusable and group.label==row.label:group.parts=row
    return _finish_fold(((key,(group.label,group.totals,group.parts)) for key,group in groups.items()),
                        len(groups),total,sessions,budget,cancel=cancel)


def _finish_fold(groups,count,total,sessions,budget,cancel=None):
    scratch=sys.getsizeof([])+count*calcsize('P')
    budget.reserve(scratch+_tuple_bytes(count))
    rows=[None]*count
    for i,(key,(label,totals,parts)) in enumerate(groups):
        _check_cancel(cancel)
        if isinstance(parts,_Row):
            budget.reserve(_size(parts));rows[i]=parts
            continue
        budget.reserve(_tuple_bytes(4)+_size(key)+_size(label)+_tuple_bytes(len(MEAN_FIELDS)))
        metrics=totals.metrics(budget)
        box_scratch=sys.getsizeof([])+2*len(MEAN_FIELDS)*calcsize('P')
        part_scratch=_tuple_bytes(max(8,2*len(parts)))
        budget.reserve(box_scratch+part_scratch)
        boxes=[]
        for metric,field in MEAN_FIELDS.items():
            _check_cancel(cancel)
            box=_describe(tuple(part.observations[metric] for part in parts),totals.known[field],budget)
            if box is None:budget.reserve(_size(None))
            boxes.append(box)
        rows[i]=_Row(key,label,metrics,tuple(boxes))
        budget.release(box_scratch+part_scratch)
    metrics=total.metrics(budget)
    budget.reserve(_tuple_bytes(4)+_size(total.count)+_size(sessions))
    fold=_Fold(tuple(rows),metrics,total.count,sessions)
    size=_size(fold)
    if size>budget.used:budget.reserve(size-budget.used)
    else:budget.release(budget.used-size)
    return fold


def _collect_uncached(engine,scope,dimension,budget):
    groups={};total=Totals();sessions=processed=0
    budget.reserve(sys.getsizeof(groups)+total.retained_bytes())
    progress=getattr(engine,'progress',None)
    if progress:progress('aggregate')
    for state,lo,hi in engine.call_ranges(scope):
        owner_totals={};owner_bytes=sys.getsizeof(owner_totals)
        budget.reserve(owner_bytes)
        after=None
        while True:
            page=engine.snapshot.projected_calls(state['owner'],FIELDS,start=lo,end=hi,after=after,filters=scope)
            engine.read_metrics['category_projection_rows']=engine.read_metrics.get('category_projection_rows',0)+len(page.rows)
            for row in page.rows:
                processed+=1
                if not matches(row,scope):continue
                key=group_key(row,dimension)
                if key not in groups:
                    label=(row.get('project_name') or key[0]) if dimension=='project' else key[0]
                    _new_group(groups,key,label,budget)
                if key not in owner_totals:
                    owner_total=Totals()
                    weight=256+_size(key)+owner_total.retained_bytes()
                    budget.reserve(weight);owner_bytes+=weight
                    owner_totals[key]=owner_total
                owner_total=owner_totals[key]
                before=owner_total.retained_bytes();accepted=owner_total.add(row)
                change=owner_total.retained_bytes()-before
                if change>0:budget.reserve(change)
                else:budget.release(-change)
                owner_bytes+=change
                groups[key][1].observe(accepted,budget)
            complete=page.complete;after=page.after
            del page
            if progress:progress(dict(stage='aggregate',processed=processed,unit='calls'))
            if complete:break
        if owner_totals:
            sessions+=1
            for key,owner_total in owner_totals.items():
                value=groups[key][1]
                before=value.totals.retained_bytes()+total.retained_bytes()
                value.totals.merge(owner_total);total.merge(owner_total)
                change=value.totals.retained_bytes()+total.retained_bytes()-before
                if change>0:budget.reserve(change)
                else:budget.release(-change)
            del key,owner_total
        del owner_totals
        budget.release(owner_bytes)
    engine.read_metrics['comparison_reused_calls']=0
    budget.reserve(_tuple_bytes(2)+_tuple_bytes(3)+_tuple_bytes(1))
    return groups,total,sessions


def _name_key(key):
    model=key.model.casefold();effort=key.effort.casefold()
    numbered=re.fullmatch(r'gpt-(\d+(?:\.\d+)*)(?:-(.+))?',model)
    parts=[int(part) for part in numbered[1].split('.')] if numbered else []
    while parts and parts[-1]==0:parts.pop()
    version=tuple(-part for part in parts)+(1,) if numbered else ()
    suffix=(numbered[2] or '') if numbered else model.removeprefix('gpt-')
    family=suffix.split('-',1)[0]
    return (numbered is None,version,_FAMILIES.get(family,len(_FAMILIES)),
            family if family not in _FAMILIES else '',_EFFORTS.get(effort,len(_EFFORTS)),
            effort,model,key.service_tier.casefold(),key)


def _deliver(fold,scope,dimension,options,cancel=None):
    rows=[]
    name_order={row.key:i for i,row in enumerate(sorted(fold.rows,key=lambda row:_name_key(row.key)))}
    for row in fold.rows:
        _check_cancel(cancel)
        parent,model,effort,service_tier=row.key
        filters=dict(model=model,effort=effort,service_tier=service_tier)
        if dimension!='all':filters[dimension]=parent
        key=json.dumps(row.key,ensure_ascii=False,separators=(',',':'))
        rows.append(dict(key=key,label=f'{model} / {effort}'+_MODE_MARKS.get(service_tier,f' / {service_tier}'),group=parent,group_label=row.label,
                         filters=filters,name_order=name_order[row.key],
                         metrics={key:value._asdict() for key,value in zip(METRICS,row.metrics)},
                         distributions={key:box.result() if box else None for key,box in zip(MEAN_FIELDS,row.boxes)},
                         components=[]))
    data=dict(rows=rows,metrics={key:value._asdict() for key,value in zip(METRICS,fold.metrics)},
              dimension=dimension,cross='model',comparison=True,detail=None,breakdowns=['effort','service_tier'])
    data=choose_metric(data,options.get('metric','calls'))
    data['rows']=display_rows(data['rows'],options,True);data['count']=len(data['rows'])
    return data


def project(engine,q):
    from .analysis_worker import expiry
    scope={k:v for k,v in q.items() if k not in ('usage','presentation')}
    if scope.get('source')=='미확인':scope['source']='unknown'
    dimension=q['usage'].get('dimension','project')
    if dimension not in DIMENSIONS:dimension='all'
    stable={k:v for k,v in scope.items() if k not in ('now','end','loading','_prepared_token')}
    if q.get('period')=='custom':stable['end']=scope['end']
    signature=(engine.query_key(stable)[1:],dimension)
    previous=getattr(engine,'_comparison_cache',None)
    now=q.get('now',q['end'])
    reusable=(previous and previous['signature']==signature and now<previous['expires']
              and q['end']>=previous['end'])
    maximum=min(LIMIT,engine.reader.limits.page_objects_bytes) if hasattr(engine,'reader') else LIMIT
    budget=_Budget(maximum,exhausted=_OwnerCacheUnavailable if hasattr(engine,'snapshot') else CapacityExceeded)
    result_budget=_Budget(maximum);fallback=False
    cancel=getattr(engine,'cancel',None)
    if hasattr(engine,'snapshot'):
        with engine.read_scope():
            expires=expiry(engine,q)
            try:entries=_owner_data(engine,scope,dimension,previous['entries'] if reusable else {},budget)
            except _OwnerCacheUnavailable:entries=None
            if entries is None:
                fallback=True
                groups,total,sessions=_collect_uncached(engine,scope,dimension,result_budget)
                entries={}
    else:
        entries={}
        for index,(state,lo,hi) in enumerate(engine.call_ranges(scope)):
            groups={}
            for row in engine.range_calls(state,lo,hi,scope):
                if matches(row,scope):add_row(groups,row,dimension,budget)
            for _,value in groups.values():value.compact(budget)
            entries[index]=dict(groups=groups)
        expires=expiry(engine,q)
    _check_cancel(cancel)
    if fallback:
        fold=_finish_fold(((key,(label,value.totals,(value,))) for key,(label,value) in groups.items()),
                          len(groups),total,sessions,result_budget,cancel=cancel)
    else:
        fold=(previous.get('fold') if reusable and _same_population(previous['entries'],entries) else None)
        if fold is None:fold=_fold(entries,result_budget,previous if reusable else None,cancel)
        else:result_budget.reserve(_size(fold))
    data=_deliver(fold,scope,dimension,q['usage'],cancel)
    result_size=_size(data)
    if result_size>maximum:raise CapacityExceeded('comparison result exceeds delivered object budget')
    _check_cancel(cancel)
    if hasattr(engine,'snapshot'):
        cache=dict(signature=signature,entries=entries,expires=expires,end=q['end'])
        if not fallback:
            fold_weight=64+_size('fold')+_size(fold)
            if budget.used+fold_weight<=maximum:
                budget.reserve(fold_weight);cache['fold']=fold
        engine._comparison_cache=cache
    if hasattr(engine,'read_metrics'):
        engine.read_metrics['comparison_cache_bytes']=0 if fallback else budget.used
        engine.read_metrics['comparison_working_bytes']=result_budget.used
        engine.read_metrics['comparison_working_peak_bytes']=result_budget.peak
        engine.read_metrics['comparison_result_bytes']=result_size
    return dict(key=engine.query_key(scope),revision=engine.revision,lookup={},valid_until=expires,
                analysis=dict(response_count=fold.response_count,session_count=fold.session_count),usage=data)
