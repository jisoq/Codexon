"""Worker-owned dashboard projections. Historical records never cross the view wire."""
from collections import Counter

from .analytics import cache_rows
from .pricing import request_tier


def compact(value):
    if isinstance(value,list):return [compact(row) for row in value]
    if not isinstance(value,dict):return value
    return {key:compact(item) for key,item in value.items() if key not in ('records','rows')}


def project(engine,q):
    query={k:v for k,v in q.items() if k not in ('presentation','compare_model','result_view')}
    query['include_whole_history']=False
    result=engine.query(query);a=result['analysis'];responses=a['responses'];turns=a['turns']
    view=dict(key=result['key'],revision=result['revision'],filter_choices=result['filter_choices'],
        analysis={k:v for k,v in a.items() if k not in ('sessions','responses','turns','units','groups')},lookup={})
    view['analysis'].update(response_count=len(responses),session_count=len({(r['home'],r['sid']) for r in responses}))
    if q['page']==0:
        overview=result['overview']
        view['overview']=compact({k:v for k,v in overview.items() if k not in ('models','sessions','attention')})
        totals={'calls':len(responses)}
        for key in ('input','output'):
            known=[r[key] for r in responses if r.get(key) is not None]
            totals[key]=sum(known) if len(known)==len(responses) else None
            totals[key+'_known']=len(known)
        view['overview']['usage_totals']=totals
        view['overview']['attention']={k:{'count':len(v)} for k,v in overview['attention'].items()}
        completed=[t for t in turns if t['complete']];priced=[r for r in responses if r.get('cost') is not None]
        selections=[priced,priced,[t for t in completed if t['cost'] is not None],cache_rows(responses),[r for r in responses if r.get('output_speed') is not None]]
        summaries=[]
        values=[overview['total'],overview['call_stats']['mean'],overview['turn_stats']['mean'],overview['summary']['cache']['value'],overview['summary']['output_speed']['value']]
        for i,rows in enumerate(selections):
            summaries.append(dict(value=values[i],known=len(rows),N=len(turns) if i==2 else len(responses),
                request_exclusions=dict(Counter(reason for t in turns if not t['complete'] for reason in t.get('exclusions',[]) or [t.get('state','기록 불완전')])) if i==2 else {}))
        view['summaries']=summaries
    else:
        comparison={k:v for k,v in result['comparison'].items() if k not in ('scatter','repricing')}
        if q.get('result_view')!='scatter':
            comparison={**comparison,'groups':[{k:v for k,v in g.items() if k!='scatter'} for g in comparison.get('groups',[])]}
        view['comparison']=compact(comparison)
        unknown=[r for r in responses if r.get('model')==q.get('compare_model') and request_tier(r)=='미확인']
        view['unknown_population']=dict(count=len(unknown))
    return view


def refresh_bounds(value,q):
    if q.get("category_usage"):return value
    """Refresh partial bucket labels without rebuilding an unchanged population."""
    if q['page']!=0 or q.get('period')=='custom':return value
    from datetime import datetime
    overview=value['overview'];timeline=overview.get('timeline',[])
    if overview.get('end')==datetime.fromtimestamp(q['end']).isoformat():return value
    if not timeline:return value
    timeline=list(timeline)
    timeline[-1]={**timeline[-1],'end_ts':q['end'],'end':datetime.fromtimestamp(q['end']).isoformat()}
    if q.get('start'):
        timeline[0]={**timeline[0],'start_ts':q['start'],'start':datetime.fromtimestamp(q['start']).isoformat()}
    return {**value,'overview':{**overview,'timeline':timeline,'end':datetime.fromtimestamp(q['end']).isoformat()}}
