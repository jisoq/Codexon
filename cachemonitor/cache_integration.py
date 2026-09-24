"""Feed maintenance into the existing analytical and quota pipeline exactly once."""
from .cache_control import Control,control_path
from .cache_execution import Journal
from .core import Session
from .cache_policy import cost_bounds
from .cache_audit import observations
from .pricing import token_cost
import json


def enrich(sessions,index_path,now):
    path=control_path(index_path)
    control=Control(path)
    journal=None
    try:
        for session in sessions:
            for row in session.get('history',[])[-1:]:
                control.profile(session['home'],session['id'],row)
            inputs=list(control.db.execute("SELECT turn,at FROM cache_inputs WHERE home=? AND sid=? AND kind='UserPromptSubmit' AND at>=? ORDER BY at",
                                           (session['home'],session['id'],now-60*86400)))
            turns={};firsts={}
            for row in session.get('history',[]):
                if row.get('purpose')!='maintenance':
                    turns[row.get('turn')]=row;firsts.setdefault(row.get('turn'),row)
            for i,(turn,at) in enumerate(inputs):
                previous=turns.get(turn)
                if previous is None:continue
                returned=i+1<len(inputs)
                current=firsts.get(inputs[i+1][0]) if returned else previous
                if current is None:continue
                until=inputs[i+1][1] if returned else now
                bounds=cost_bounds(previous,current,previous.get('output'))
                gap=dict(at=previous['ts'],seconds=max(0,until-previous['ts']),returned=returned,
                         benefit_lower=bounds['benefit_lower'] if bounds else None,
                         maintenance_upper=bounds['maintenance_upper'] if bounds else None,
                         origin='submission',settled=returned)
                control.db.execute('INSERT OR REPLACE INTO cache_gaps VALUES(?,?,?,?)',
                                   (session['home'],session['id'],turn,json.dumps(gap)))
        journal=Journal(path)
        groups={}
        for row in journal.rows():
            key=(row['home'],row['sid'])
            if key not in groups:
                groups[key]=Session('maintenance:'+row['sid'],row['home'],title='캐시 유지',parent_thread_id=row['sid'])
            session=groups[key]
            session.add_usage(row['ts'],row['key'],dict(input_tokens=row['input'],cached_input_tokens=row['cached'],
                cache_write_input_tokens=row['written'],output_tokens=row['output'],reasoning_output_tokens=row['reasoning']),
                row['model'],turn=row['key'],effort=row['effort'],service_tier=row['service_tier'])
            request=session.requests[-1]
            request.purpose='maintenance';request.request_start=row['request_start']
            session.turn_records[row['key']]=dict(started_at=row['request_start'],ended_at=row['request_end'],
                state={'completed':'완료','sent':'진행','unknown':'미확인'}.get(row['state'],'중단'))
        for session in groups.values():
            view=session.view(now)
            view.update(source='maintenance',collection_complete=True,archived=False,purpose='maintenance')
            sessions.append(view)
        maintenance=journal.rows()
        audit=[];effects=[];compactions=[];delegated=[]
        for session in sessions:
            if session.get('purpose')=='maintenance':continue
            history=session.get('history',[])
            if session.get('parent_thread_id'):delegated.extend(history)
            for previous,current in zip(history,history[1:]):
                if previous.get('compaction_epoch')!=current.get('compaction_epoch'):
                    compactions.append(dict(before=previous.get('input'),after=current.get('input'),quality_measured=False))
            for item in observations(history[-101:])[-100:]:
                audit.append({**item,'title':session.get('title') or session['id'],'sid':session['id'],'home':session['home']})
            own=[r for r in maintenance if r['home']==session['home'] and r['sid']==session['id']]
            for snapshot in dict.fromkeys(r['snapshot'] for r in own):
                jobs=[r for r in own if r['snapshot']==snapshot]
                original=next((r for r in history if r['key']==snapshot),None)
                if original is None:continue
                follow=next((r for r in history if r['ts']>original['ts'] and r.get('turn')!=original.get('turn')),None)
                effects.append(dict(sid=session['id'],snapshot=snapshot,maintenance_calls=len(jobs),
                    maintenance_cost=sum(r['cost'] for r in jobs if r['cost'] is not None),
                    missing_cost=sum(r['cost'] is None for r in jobs),
                    maintained_input_lower=jobs[-1]['scope_read_lower'],
                    user_response=follow['key'] if follow else None,user_read=follow.get('cached') if follow else None,
                    user_input=follow.get('input') if follow else None,causal_saving=None))
        audit.sort(key=lambda r:r['ts'])
        return dict(calls=len(maintenance),known_cost=sum(r['cost'] for r in maintenance if r['cost'] is not None),
                    priced=sum(r['cost'] is not None for r in maintenance),
                    unknown=sum(not r['usage_known'] for r in maintenance),audit=audit[-100:],
                    shortfalls=sum(bool(r['reuse_shortfall_scenario']) for r in audit[-100:]),effects=effects[-100:],
                    compactions=compactions[-100:],delegation=dict(calls=len(delegated),
                        known_cost=sum(token_cost(r)['cost'] or 0 for r in delegated),
                        priced=sum(token_cost(r)['cost'] is not None for r in delegated)),
                    request_activity=[dict(home=r['home'],attempt=r['key'],response_id=r['key'],
                        request_observed_at=r['request_start'],ts=r['ts'],
                        status='completed' if r['state']=='completed' else 'created',purpose='maintenance') for r in maintenance])
    finally:
        if journal:journal.close()
        control.close()
