"""Model/effort comparisons from bounded scalar pages and per-owner aggregates.

The reader actor owns one scope cache. No call bodies or record keys survive a
page. An admitted writer append certificate permits only an ordinal-tail read;
corrections and missing certificates rebuild just the affected owner.
"""
from copy import deepcopy
from hashlib import sha256
import json
from math import isfinite

from .analytics import INPUT_BANDS, CACHE_BANDS, condition_match, output_speed
from .category_usage import category, display_rows
from .prepared_category import BASE_FIELDS, SPEED_FIELDS, _Budget
from .prepared_store import CapacityExceeded, _size, pack
from .prepared_sums import _Sum
from .pricing import display_tier

DIMENSIONS=('all','project','source','service_tier','home','input_band','cache_band')
METRICS=('calls','cost','cost_per_call','latency','tps','input_per_call','output_per_call','reasoning')
MEANS=frozenset(('cost_per_call','latency','tps','input_per_call','output_per_call'))
FIELDS=tuple(dict.fromkeys((*BASE_FIELDS,*SPEED_FIELDS,'cost','input','cached','rate')))
LIMIT=16*1024*1024


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
    __slots__=('count','sums','known')
    fields=('cost','latency','tps','input','output','reasoning')

    def __init__(self):
        self.count=0
        self.sums={key:_Sum() for key in self.fields}
        self.known={key:0 for key in self.fields}

    def add(self,row):
        self.count+=1
        speed=output_speed(row)
        values=dict(cost=row.get('cost'),input=row.get('input'),output=row.get('output'),reasoning=row.get('reasoning'),
                    tps=speed,latency=row.get('completion_latency_ms',0)/1000 if speed is not None else None)
        for key,value in values.items():
            if type(value) in (int,float) and isfinite(value) and value>=0:
                self.sums[key].add(value);self.known[key]+=1

    def merge(self,other):
        self.count+=other.count
        for key in self.fields:
            self.sums[key].add(other.sums[key].value());self.known[key]+=other.known[key]

    def result(self):
        result={'calls':dict(value=self.count,known=self.count,total=self.count,partial=False)}
        for metric,field in [('cost','cost'),('cost_per_call','cost'),('latency','latency'),
                             ('tps','tps'),('input_per_call','input'),('output_per_call','output'),('reasoning','reasoning')]:
            known=self.known[field]
            value=self.sums[field].value() if known else None
            if metric in MEANS and value is not None:value/=known
            result[metric]=dict(value=value,known=known,total=self.count,partial=known!=self.count)
        return result


def group_key(row,dimension):
    parent=('전체' if dimension=='all' else display_tier(row) if dimension=='service_tier'
            else band(row,dimension) if dimension in ('input_band','cache_band') else category(row,dimension))
    return parent,category(row,'model'),category(row,'effort')


def add_row(groups,row,dimension,budget):
    key=group_key(row,dimension)
    if key not in groups:
        label=(row.get('project_name') or key[0]) if dimension=='project' else key[0]
        budget.add(key,label)
        # Five compensated sums plus their counters are charged before admission.
        budget.add(key)
        groups[key]=(label,Totals())
    groups[key][1].add(row)


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
                groups=deepcopy(old['groups']) if ordinal is not None else {}
                local=_Budget(budget.maximum)
                for key,(label,_) in groups.items():local.add(key,label);local.add(key)
                after=None
                while True:
                    page=snapshot.projected_calls(owner,FIELDS,start=lo,end=hi,after=after,
                                                  filters=scope,ordinal_start=ordinal)
                    for row in page.rows:
                        processed+=1
                        if matches(row,scope):add_row(groups,row,dimension,local)
                    complete=page.complete;after=page.after
                    del page
                    if progress:progress(dict(stage='aggregate',processed=processed,unit='calls'))
                    if complete:break
                entry=dict(version=version,stamp=stamp,groups=groups,weight=local.used,context=context)
        if entry is old or (old and entry.get('groups') is old['groups']):
            reused+=sum(value[1].count for value in entry['groups'].values())
        budget.used+=entry['weight']+512+_size(owner)+_size(context)
        if budget.used>budget.maximum:
            raise CapacityExceeded('comparison owner aggregates exceed cache budget')
        entries[owner]=entry
    engine.read_metrics['category_projection_rows']=engine.read_metrics.get('category_projection_rows',0)+processed
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
    rows=[dict(row,**row['metrics'][metric],group_value=values[row['group']]) for row in data['rows']]
    return dict(data,rows=rows,metric=metric,summary=data['metrics'][metric],
                maximum=max([row['value'] or 0 for row in rows]+[0]))


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
    budget=_Budget(min(LIMIT,engine.reader.limits.page_objects_bytes) if hasattr(engine,'reader') else LIMIT)
    if hasattr(engine,'snapshot'):
        with engine.read_scope():
            entries=_owner_data(engine,scope,dimension,previous['entries'] if reusable else {},budget)
    else:
        entries={}
        for index,(state,lo,hi) in enumerate(engine.call_ranges(scope)):
            groups={}
            for row in engine.range_calls(state,lo,hi,scope):
                if matches(row,scope):add_row(groups,row,dimension,budget)
            entries[index]=dict(groups=groups)
    groups={};total=Totals();sessions=0;result_budget=_Budget(budget.maximum)
    for entry in entries.values():
        if entry['groups']:sessions+=1
        for key,(label,value) in entry['groups'].items():
            if key not in groups:
                result_budget.add(key,label);result_budget.add(key)
                groups[key]=(label,Totals())
            groups[key][1].merge(value);total.merge(value)
    rows=[]
    for (parent,model,effort),(label,values) in groups.items():
        filters=dict(model=model,effort=effort)
        if dimension!='all':filters[dimension]=parent
        key=json.dumps((parent,model,effort),ensure_ascii=False,separators=(',',':'))
        rows.append(dict(key=key,label=f'{model} / {effort}',group=parent,group_label=label,filters=filters,
                         metrics=values.result(),components=[]))
    data=dict(rows=rows,metrics=total.result(),dimension=dimension,cross='model',comparison=True,
              detail=None,breakdowns=['effort'],population=dict(query=scope,path=['analysis','responses']))
    data=choose_metric(data,q['usage'].get('metric','calls'))
    data['rows']=display_rows(data['rows'],q['usage'],True);data['count']=len(data['rows'])
    if _size(data)>budget.maximum:raise CapacityExceeded('comparison result exceeds delivered object budget')
    expires=expiry(engine,q)
    if hasattr(engine,'snapshot'):
        engine._comparison_cache=dict(signature=signature,entries=entries,expires=expires,end=q['end'])
    return dict(key=engine.query_key(scope),revision=engine.revision,lookup={},valid_until=expires,
                analysis=dict(response_count=total.count,session_count=sessions),usage=data)
