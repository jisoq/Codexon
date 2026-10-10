"""History hierarchy built exclusively from the analysis worker's prepared data."""
import json
from collections import defaultdict
from .analytics import project_identity, project_choices, cache_rows
from .session_costs import own_costs, session_costs


def node_key(kind, *parts):
    return json.dumps([kind,*parts],ensure_ascii=False,separators=(',',':'))


def total(rows):
    priced=[r['cost'] for r in rows if r.get('cost') is not None]
    valid=cache_rows(rows);inputs=sum(r['input'] for r in valid)
    def tokens(key):
        values=[r[key] for r in rows if r.get(key) is not None]
        return sum(values) if len(values)==len(rows) and rows else None
    return dict(cost=sum(priced) if priced else None,partial=len(priced)<len(rows),
        calls=len(rows),known=len(priced),input=tokens('input'),output=tokens('output'),
        cache_ratio=100*sum(r['cached'] for r in valid)/inputs if inputs else None,
        ts=max((r['ts'] for r in rows),default=None))


def prepare(result, rows, search, include_empty=True):
    sessions=result['analysis']['sessions']
    indexed={(s['home'],s['id']):s for s in sessions}
    projects=project_choices(sessions)['project_labels']
    parent={}
    for key,s in indexed.items():
        candidate=(key[0],s.get('parent_thread_id'))
        if candidate in indexed and candidate!=key and project_identity(s)==project_identity(indexed[candidate]):
            parent[key]=candidate
    for key in list(parent):
        seen={key};cursor=key
        while cursor in parent:
            cursor=parent[cursor]
            if cursor in seen:
                parent.pop(key,None);break
            seen.add(cursor)
    matching={k for k,s in indexed.items() if not search or search in ' '.join(str(s.get(f,'')) for f in ('title','project_name','cwd','id')).casefold()}
    visible=set(matching)
    for key in matching:
        cursor=key
        while cursor in parent:
            cursor=parent[cursor];visible.add(cursor)
    selected=[r for r in rows if (r['home'],r['sid']) in matching]
    bysession=defaultdict(list);byproject=defaultdict(list)
    for r in selected:
        key=(r['home'],r['sid']);bysession[key].append(r)
        if key in indexed:byproject[project_identity(indexed[key])].append(r)
    # Restrict ancestry to the same project, matching the displayed hierarchy.
    lineage=[{**s,'parent_thread_id':parent[k][1] if k in parent else None} for k,s in indexed.items()]
    rollups=session_costs(lineage,own_costs(selected))
    children=defaultdict(list);project_sessions=defaultdict(list);items={}
    turns=result['lookup']['session_turns']
    request_counts={}
    for k in indexed:
        visible_turns={r.get('turn') for r in bysession[k]}
        request_counts[k]=sum(t.get('turn') in visible_turns or (include_empty and not t.get('calls')) for t in turns.get(k,[]))
    own_summaries={k:total(bysession[k]) for k in indexed}
    for k,s in indexed.items():
        if k not in visible:continue
        pid=project_identity(s);g=rollups[k]
        children[parent.get(k)].append(k);project_sessions[pid].append(k)
        members=g['members'];own=bysession[k]
        requests=sum(request_counts[m] for m in members)
        measured=[own_summaries[m] for m in members if own_summaries[m]['calls']]
        tokens={name:sum(r[name] for r in measured) if measured and all(r[name] is not None for r in measured) else None for name in ('input','output')}
        items[k]=dict({name:value for name,value in g.items() if name!='members'},**{'known':g['priced']},home=k[0],sid=k[1],key=node_key('session',*k),
            title=s.get('title') or '제목 없음',project=projects[pid],project_id=pid,
            source=s.get('source',''),requests=requests,input=tokens['input'],output=tokens['output'],
            ts=g['latest_ts'],context=k not in matching)
    project_rows=[]
    for pid in projects:
        keys=project_sessions[pid]
        project_rows.append(dict(total(byproject[pid]),key=node_key('project',pid),project_id=pid,title=projects[pid],
            roots=sum(k not in parent for k in keys),children=sum(k in parent for k in keys),
            requests=sum(request_counts[k] for k in keys if k in matching)))
    return dict(projects=project_rows,items=items,parent=parent,children=children,rollups=rollups,
        selected=selected,summary=total(selected))


def session_tree(data, project, expanded, order, searching=False, collapsed=()):
    """Flatten expanded sibling groups without losing their ancestry or sort order."""
    result=[]
    def siblings(keys):
        return order([data['items'][key] for key in keys])
    def append_session(row,depth=0,guides=(),last=True):
        key=(row['home'],row['sid'])
        row=data['items'][key];kids=data['children'].get(key,[])
        opened=row['key'] not in collapsed if searching else row['key'] in expanded
        result.append(dict(row,depth=depth,guides=list(guides),last=last,expandable=bool(kids),expanded=opened))
        if opened:
            children=siblings(kids)
            for i,child in enumerate(children):
                append_session(child,depth+1,(*guides,not last) if depth else (),i==len(children)-1)
    roots=[key for key,row in data['items'].items() if row['project_id']==project and key not in data['parent']]
    for row in siblings(roots):append_session(row)
    return result
