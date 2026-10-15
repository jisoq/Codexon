"""Non-temporal projections of the engine's owned calls. No source collection."""
from collections import defaultdict
import json
from math import fsum
from .analytics import output_speed
from .pricing import request_tier
from .translation_catalog import CATALOG

DIMENSIONS = ('project', 'model', 'source')
BREAKDOWNS = ('service_tier', 'effort')
METRICS = ('calls', 'cost', 'input', 'cached', 'output', 'reasoning', 'tps')


def category(row, dimension):
    value = request_tier(row) if dimension == 'service_tier' else row.get(dimension)
    return '미확인' if not value or dimension == 'source' and value == 'unknown' else value


def summary(rows, metric):
    if metric == 'calls':
        return dict(value=len(rows), known=len(rows), total=len(rows), partial=False)
    values = [output_speed(r) if metric == 'tps' else r.get(metric) for r in rows]
    known = [v for v in values if v is not None]
    value = sum(known) if known or not rows else None
    if metric == 'tps':value = sum(known) / len(known) if known else None
    return dict(value=value, known=len(known), total=len(rows), partial=len(known) != len(rows))


def call_totals(engine,q):
    """One compact range aggregate per owner; never retain a second call population."""
    fields=METRICS[1:-1]
    counts=0;sessions=0;parts=[]
    filters=tuple(q.get(k,'') for k in ('model','effort','service_tier'))
    for state,lo,hi in engine.call_ranges(q):
        signature=(state.get('content_revision',state['revision']),lo,hi,filters)
        cached=state.get('call_totals')
        if cached and cached[0]==signature:
            part=cached[1]
        else:
            values=[0]*len(fields);known=[0]*len(fields);count=0
            for row in engine.range_calls(state,lo,hi,q):
                count+=1
                for i,field in enumerate(fields):
                    value=row.get(field)
                    if value is not None:values[i]+=value;known[i]+=1
            part=(count,tuple(values),tuple(known))
            state['call_totals']=(signature,part)
        counts+=part[0];sessions+=bool(part[0]);parts.append(part)
    totals={'calls':dict(value=counts,known=counts,total=counts,partial=False)}
    for i,field in enumerate(fields):
        known=sum(part[2][i] for part in parts)
        values=(part[1][i] for part in parts)
        value=(fsum(values) if field=='cost' else sum(values)) if known or not counts else None
        totals[field]=dict(value=value,known=known,total=counts,partial=known!=counts)
    return dict(key=engine.query_key(q),revision=engine.revision,lookup={},
                analysis=dict(response_count=counts,session_count=sessions),usage=dict(totals=totals))


def project(engine, q):
    # Display choices never invalidate the population or its aggregation.
    scope = {k:v for k,v in q.items() if k not in ('usage', 'presentation')}
    if q['page']==0:return call_totals(engine,scope)
    options = q.get('usage', {})
    dimension = options.get('dimension', 'project')
    if dimension not in DIMENSIONS:dimension = 'project'
    metric = options.get('metric', 'calls')
    if metric not in METRICS:metric = 'calls'
    breakdowns = tuple(key for key in BREAKDOWNS if key in options.get('breakdowns', []) and dimension == 'model')
    selected = options.get('selected', [])
    selection = dict(zip(selected[::2], selected[1::2])) if len(selected)%2 == 0 else {}
    selection = {key:value for key,value in selection.items() if key in (*DIMENSIONS,*BREAKDOWNS)}
    stable={k:v for k,v in scope.items() if k not in ('now','loading') and (q.get('period')=='custom' or k not in ('start','end'))}
    signature=(engine.query_key(stable,calls_only=True),dimension,metric,tuple(selection.items()),breakdowns)
    cached=getattr(engine,'_category_projection',None)
    if cached and cached[0]==signature and q.get('now',q['end'])<cached[2]:
        data=cached[1]
        common=dict(data['common'],key=engine.query_key(scope),revision=engine.revision)
    else:
        rows=[r for state,lo,hi in engine.call_ranges(scope) for r in engine.range_calls(state,lo,hi,scope)]
        common = dict(key=engine.query_key(scope), revision=engine.revision, lookup={},
                      analysis=dict(response_count=len(rows), session_count=len({(r['home'],r['sid']) for r in rows})))
        cross = 'source' if dimension == 'model' else 'model'
        buckets = defaultdict(list)
        for r in rows:buckets[category(r,dimension)].append(r)
        weights=defaultdict(float)
        for row in rows:
            key=category(row,cross)
            if key!='미확인':weights[key]+=1 if metric=='calls' else (row.get(metric) or 0)
        visible_keys=sorted(weights,key=lambda key:-weights[key])[:4]
        categories = []
        def aggregate(key, label, members, filters, group='', group_value=None):
            children = defaultdict(list)
            for r in members:children[category(r,cross)].append(r)
            components = [dict(key=k, **summary(rs,metric)) for k,rs in children.items()]
            components.sort(key=lambda c:-(c['value'] or 0))
            known = [c for c in components if c['key'] != '미확인']
            # Keep unknown distinct; retain names of folded categories for hover.
            small = [c for c in known if c['key'] not in visible_keys]
            shown = sorted((c for c in known if c['key'] in visible_keys),key=lambda c:visible_keys.index(c['key'])) + [c for c in components if c['key'] == '미확인']
            if small and metric != 'tps':
                shown.append(dict(key='기타', value=sum(c['value'] for c in small if c['value'] is not None) if any(c['value'] is not None for c in small) else None,
                                  known=sum(c['known'] for c in small), total=sum(c['total'] for c in small),
                                  partial=any(c['partial'] for c in small), names=[c['key'] for c in small[:8]]))
            return dict(key=key,label=label,filters=filters,group=group,group_value=group_value,
                        components=shown,**summary(members,metric))
        for key,members in buckets.items():
            if breakdowns:
                combinations=defaultdict(list)
                for row in members:combinations[tuple(category(row,field) for field in breakdowns)].append(row)
                group_value=summary(members,metric)['value']
                for values,items in combinations.items():
                    filters=dict(zip(('model',*breakdowns),(key,*values)))
                    identifier=json.dumps(list(filters.items()),ensure_ascii=False,separators=(',',':'))
                    categories.append(aggregate(identifier,' / '.join(values),items,filters,key,group_value))
            else:
                categories.append(aggregate(key,members[0].get('project_name',key) if dimension=='project' else key,members,{dimension:key}))
        detail = None
        if selection:
            first=last=None
            # Only the selected item's history is inspected; no per-category history copies.
            for state in engine.sessions.values():
                for r in state['prepared']['history']:
                    if all(category(r,field)==value for field,value in selection.items()):
                        ts=r['ts'];first=ts if first is None else min(first,ts);last=ts if last is None else max(last,ts)
            sessions={(r['home'],r['sid']) for r in rows}
            sources={source:len({(r['home'],r['sid']) for r in rows if category(r,'source')==source}) for source in ('user','subagent','미확인')}
            field=next(iter(selection))
            detail=dict(key=selection[field],dimension=field,selection=selection,first=first,last=last,sessions=len(sessions),sources=sources,
                        projects=len({r['project'] for r in rows if category(r,'project')!='미확인'}),
                        unknown_projects=sum(category(r,'project')=='미확인' for r in rows))
        data=dict(common=common,categories=categories,summary=summary(rows,metric),dimension=dimension,metric=metric,cross=cross,detail=detail,breakdowns=list(breakdowns))
        from .analysis_worker import expiry
        from .analysis_engine import retained_bytes
        cached=(signature,data,expiry(engine,q))
        engine._category_projection=cached if retained_bytes(cached,16*1024*1024)<=16*1024*1024 else None
    categories=data['categories']
    search=options.get('search','').casefold()
    names={'user':'유저 에이전트','subagent':'하위 에이전트','unknown':'미확인'}
    visible=[r for r in categories if any(search in str(text).casefold() for text in (r['label'],r['group'],names.get(r['label'],r['label']),CATALOG.get(names.get(r['label'],r['label']),r['label']),*[CATALOG.get(v,v) for v in r['filters'].values()]))]
    order=options.get('sort','desc')
    def rank(value):return (value is None,(value or 0)*(1 if order=='asc' else -1))
    if breakdowns:
        visible.sort(key=lambda r:(r['group'].casefold(),r['label'].casefold()) if order=='name' else (rank(r['group_value']),r['group'],rank(r['value']),r['label']))
    else:
        visible.sort(key=lambda r:str(r['label']).casefold() if order=='name' else rank(r['value']))
    size=max(2,min(12,int(options.get('size',8))))
    # A model heading takes 28 pixels in addition to each 54-pixel data row.
    # Pack a bounded page and repeat the heading when a model continues on it.
    offsets=[0];height=0;previous=None
    for index,row in enumerate(visible):
        extra=28 if breakdowns and row['group']!=previous else 0
        if height and height+54+extra>size*54:
            offsets.append(index);height=0;extra=28 if breakdowns else 0
        height+=54+extra;previous=row['group']
    page=max(0,min(int(options.get('page',0)),len(offsets)-1))
    start=offsets[page];end=offsets[page+1] if page+1<len(offsets) else len(visible)
    # Only one page and at most six composition segments per item cross the worker wire.
    population=dict(query=scope,path=['analysis','responses'])
    return dict(common,usage={**{k:v for k,v in data.items() if k not in ('categories','common')},'rows':visible[start:end],
                              'count':len(visible),'page':page,'size':size,'maximum':max([r['value'] or 0 for r in categories]+[0]),
                              'page_count':len(offsets),'start_index':start,'end_index':end,
                              'population':population})
