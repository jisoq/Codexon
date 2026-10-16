"""Bounded scalar category aggregation over ordered prepared projections.

No call dictionaries survive their projection page. Aggregate states and the
compact result each have the snapshot's delivered-object ceiling; a scalar page
has its separate existing ceiling. The caller retains the 16 MiB cache policy.
"""
import json
import sys

from .analytics import output_speed
from .category_usage import category
from .pricing import display_tier
from .prepared_store import CapacityExceeded, RebuildRequired, _size
from .prepared_sums import _Sum


BASE_FIELDS = ('home', 'sid', 'project', 'project_name', 'source', 'model', 'effort',
               'service_tier', 'mode_conflict', 'service_tier_source', 'display_service_tier')
SPEED_FIELDS = ('output', 'reasoning', 'completion_latency_ms', 'response_status',
                'timing_valid', 'observation_missing', 'model_conflict',
                'output_conflict', 'transport_source', 'tps')


class _Summary:
    __slots__ = ('metric', 'count', 'known', 'total')

    def __init__(self, metric):
        self.metric=metric
        self.count=self.known=0
        self.total=_Sum()

    def add(self, value):
        self.count+=1
        if value is not None:
            self.known+=1
            self.total.add(value)

    def result(self):
        if self.metric=='calls':
            return dict(value=self.count,known=self.count,total=self.count,partial=False)
        value=self.total.value() if self.known or not self.count else None
        if self.metric=='tps':
            value=self.total.value()/self.known if self.known else None
        return dict(value=value,known=self.known,total=self.count,partial=self.known!=self.count)


class _Budget:
    """Charge new aggregate entries before admission, never the growing history."""
    def __init__(self, maximum):
        self.maximum=maximum
        self.used=0

    def add(self, *values):
        # A fresh group owns two dictionaries, a summary, a numeric accumulator,
        # and scalar counters. This also covers sparse dictionary/set capacity.
        self.used+=1024+sum(_size(value) for value in values)
        if self.used>self.maximum:
            raise CapacityExceeded('category aggregate state exceeds the delivered object budget')


class _Group:
    __slots__ = ('label', 'filters', 'summary', 'children', 'groups')

    def __init__(self, label, filters, metric):
        self.label=label
        self.filters=filters
        self.summary=_Summary(metric)
        self.children={}
        self.groups={}

    def add(self, cross, value, metric, budget):
        self.summary.add(value)
        if cross not in self.children:
            budget.add(cross, _Summary(metric))
            self.children[cross]=_Summary(metric)
        self.children[cross].add(value)


def _components(group, visible, metric):
    components=[dict(key=key,**part.result()) for key,part in group.children.items()]
    components.sort(key=lambda value:-(value['value'] or 0))
    known=[value for value in components if value['key']!='미확인']
    small=[value for value in known if value['key'] not in visible]
    shown=sorted((value for value in known if value['key'] in visible),
                 key=lambda value:visible.index(value['key']))
    shown.extend(value for value in components if value['key']=='미확인')
    if small and metric!='tps':
        shown.append(dict(key='기타',value=sum(value['value'] for value in small if value['value'] is not None)
                          if any(value['value'] is not None for value in small) else None,
                          known=sum(value['known'] for value in small),total=sum(value['total'] for value in small),
                          partial=any(value['partial'] for value in small),names=[value['key'] for value in small[:8]]))
    return shown


def _result(key, group, visible, metric, *, parent='', group_value=None):
    return dict(key=key,label=group.label,filters=group.filters,group=parent,group_value=group_value,
                components=_components(group,visible,metric),**group.summary.result())


def row_matches(row, scope):
    """The same scalar filters as the existing prepared range reader."""
    model,effort,mode=(scope.get(key,'') for key in ('model','effort','service_tier'))
    return not ((model and (row.get('model') or '미확인')!=model) or
                (effort and (row.get('effort') or '미확인')!=effort) or
                (mode and display_tier(row)!=mode))


def project_data(engine, scope, dimension, metric, breakdowns, selection):
    """Build the unchanged business projector's data without retaining records."""
    if sys.implementation.name!='cpython' or sys.version_info[:2]!=(3,12):
        raise RebuildRequired('category arithmetic requires the pinned CPython 3.12 runtime')
    snapshot=engine.snapshot
    if snapshot is None:
        raise RuntimeError('category projection requires a pinned read scope')
    fields=tuple(dict.fromkeys((*BASE_FIELDS,*(SPEED_FIELDS if metric=='tps' else () if metric=='calls' else (metric,)))))
    cross='source' if dimension=='model' else 'model'
    totals=_Summary(metric)
    buckets={}
    weights={}
    sessions=set()
    source_sessions={key:set() for key in ('user','subagent','미확인')}
    projects=set()
    unknown_projects=0
    budget=_Budget(snapshot.limits.page_objects_bytes)
    progress=getattr(engine,'progress',None)
    if progress:progress('aggregate')
    for state,lo,hi in engine.call_ranges(scope):
        after=None
        while True:
            page=snapshot.projected_calls(state['owner'],fields,start=lo,end=hi,after=after)
            for row in page.rows:
                engine.read_metrics['category_projection_rows']=engine.read_metrics.get('category_projection_rows',0)+1
                if not row_matches(row,scope):
                    continue
                identity=(row['home'],row['sid'])
                if identity not in sessions:
                    budget.add(identity)
                    sessions.add(identity)
                value=1 if metric=='calls' else output_speed(row) if metric=='tps' else row.get(metric)
                totals.add(value)
                key=category(row,dimension)
                child=category(row,cross)
                if child!='미확인':
                    if child not in weights:
                        budget.add(child,0.0)
                        weights[child]=0.0
                    weights[child]+=1 if metric=='calls' else (row.get(metric) or 0)
                if key not in buckets:
                    label=row.get('project_name',key) if dimension=='project' else key
                    filters={dimension:key}
                    budget.add(key,label,filters)
                    buckets[key]=_Group(label,filters,metric)
                group=buckets[key]
                if breakdowns:
                    group.summary.add(value)
                    combination=tuple(category(row,field) for field in breakdowns)
                    if combination not in group.groups:
                        filters=dict(zip(('model',*breakdowns),(key,*combination)))
                        label=' / '.join(combination)
                        budget.add(combination,label,filters)
                        group.groups[combination]=_Group(label,filters,metric)
                    group=group.groups[combination]
                group.add(child,value,metric,budget)
                if selection:
                    observed_source=category(row,'source')
                    if observed_source in source_sessions and identity not in source_sessions[observed_source]:
                        budget.add(observed_source,identity)
                        source_sessions[observed_source].add(identity)
                    if category(row,'project')=='미확인':
                        unknown_projects+=1
                    elif row['project'] not in projects:
                        budget.add(row['project'])
                        projects.add(row['project'])
            complete=page.complete
            after=page.after
            del page
            if complete:
                break
    visible=sorted(weights,key=lambda key:-weights[key])[:4]
    categories=[]
    result_bytes=0
    def append(value):
        nonlocal result_bytes
        result_bytes+=_size(value)
        if result_bytes>snapshot.limits.page_objects_bytes:
            raise CapacityExceeded('category rows exceed the delivered object budget')
        categories.append(value)
    for key,group in buckets.items():
        if breakdowns:
            group_value=group.summary.result()['value']
            for child in group.groups.values():
                identifier=json.dumps(list(child.filters.items()),ensure_ascii=False,separators=(',',':'))
                append(_result(identifier,child,visible,metric,parent=key,group_value=group_value))
        else:
            append(_result(key,group,visible,metric))
    detail=None
    if selection:
        first,last=engine.category_bounds(selection)
        field=next(iter(selection))
        detail=dict(key=selection[field],dimension=field,selection=selection,first=first,last=last,
                    sessions=len(sessions),sources={key:len(value) for key,value in source_sessions.items()},
                    projects=len(projects),unknown_projects=unknown_projects)
    common=dict(key=engine.query_key(scope),revision=engine.revision,lookup={},
                analysis=dict(response_count=totals.count,session_count=len(sessions)))
    result=dict(common=common,categories=categories,summary=totals.result(),dimension=dimension,
                metric=metric,cross=cross,detail=detail,breakdowns=list(breakdowns))
    if _size(result)>snapshot.limits.page_objects_bytes:
        raise CapacityExceeded('category projection exceeds the delivered object budget')
    engine.read_metrics['category_aggregate_budget_bytes']=budget.used
    return result
