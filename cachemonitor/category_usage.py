"""Non-temporal projections of the engine's owned calls. No source collection."""
from collections import defaultdict
from .analytics import output_speed
from .pricing import request_tier
from .translation_catalog import CATALOG

DIMENSIONS = ('project', 'model', 'source', 'effort', 'service_tier')
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


def project(engine, q):
    # Display choices never invalidate the population or its aggregation.
    scope = {k:v for k,v in q.items() if k not in ('usage', 'presentation')}
    options = q.get('usage', {})
    signature=(engine.query_key(scope),options.get('dimension','project'),options.get('metric','calls'),tuple(options.get('selected',[])))
    cached=getattr(engine,'_category_projection',None)
    if q['page']==1 and cached and cached[0]==signature:
        common=cached[1]['common'];rows=None;result={'key':signature[0]}
    else:
        result = engine.query(scope)
        rows = result['analysis']['responses']
        common = dict(key=result['key'], revision=engine.revision, lookup={},
                      analysis=dict(response_count=len(rows), session_count=len({(r['home'],r['sid']) for r in rows})))
    if q['page'] == 0:
        return dict(common, usage=dict(totals={k:summary(rows,k) for k in METRICS if k != 'tps'}))
    options = q.get('usage', {})
    dimension = options.get('dimension', 'project')
    if dimension not in DIMENSIONS:dimension = 'project'
    metric = options.get('metric', 'calls')
    if metric not in METRICS:metric = 'calls'
    cross = 'source' if dimension == 'model' else 'model'
    signature = (result['key'], dimension, metric, tuple(options.get('selected', [])))
    cached = getattr(engine, '_category_projection', None)
    if cached and cached[0] == signature:
        data = cached[1]
    else:
        buckets = defaultdict(list)
        for r in rows:buckets[category(r,dimension)].append(r)
        weights=defaultdict(float)
        for row in rows:
            key=category(row,cross)
            if key!='미확인':weights[key]+=1 if metric=='calls' else (row.get(metric) or 0)
        visible_keys=sorted(weights,key=lambda key:-weights[key])[:4]
        categories = []
        for key, members in buckets.items():
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
            categories.append(dict(key=key, label=members[0].get('project_name',key) if dimension=='project' else key,
                                   components=shown, **summary(members, metric)))
        selected = options.get('selected', [])
        detail = None
        if len(selected) == 2 and selected[0] in DIMENSIONS:
            first=last=None
            # Only the selected item's history is inspected; no per-category history copies.
            for state in engine.sessions.values():
                for r in state['prepared']['history']:
                    if category(r,selected[0]) == selected[1]:
                        ts=r['ts'];first=ts if first is None else min(first,ts);last=ts if last is None else max(last,ts)
            sessions={(r['home'],r['sid']) for r in rows}
            sources={source:len({(r['home'],r['sid']) for r in rows if category(r,'source')==source}) for source in ('user','subagent','미확인')}
            detail=dict(key=selected[1], dimension=selected[0], first=first,last=last,sessions=len(sessions),sources=sources,
                        projects=len({r['project'] for r in rows if category(r,'project')!='미확인'}),
                        unknown_projects=sum(category(r,'project')=='미확인' for r in rows))
        data=dict(common=common,categories=categories,summary=summary(rows,metric),dimension=dimension,metric=metric,cross=cross,detail=detail)
        engine._category_projection=(signature,data) if len(categories)<=160000 else None
    categories=data['categories']
    search=options.get('search','').casefold()
    names={'user':'유저 에이전트','subagent':'하위 에이전트','unknown':'미확인'}
    visible=[r for r in categories if any(search in str(text).casefold() for text in (r['label'],r['key'],names.get(r['label'],r['label']),CATALOG.get(names.get(r['label'],r['label']),r['label'])))]
    order=options.get('sort','desc')
    visible.sort(key=lambda r:str(r['label']).casefold() if order=='name' else (r['value'] is None,(r['value'] or 0)*(1 if order=='asc' else -1)))
    size=max(2,min(12,int(options.get('size',8))));page=max(0,min(int(options.get('page',0)),max(0,(len(visible)-1)//size)))
    # Only one page and at most six composition segments per item cross the worker wire.
    population=dict(query=scope,path=['analysis','responses'])
    return dict(common,usage={**{k:v for k,v in data.items() if k not in ('categories','common')},'rows':visible[page*size:(page+1)*size],
                              'count':len(visible),'page':page,'size':size,'maximum':max([r['value'] or 0 for r in categories]+[0]),
                              'population':population})
