"""Feed maintenance into the existing analytical and quota pipeline exactly once."""
from .cache_control import Control,control_path
from .cache_execution import Journal
from .core import Session
from .cache_policy import cost_bounds,scoped_history
from .cache_audit import observations
from .pricing import token_cost
import json
import hashlib


def enrich(sessions,index_path,now,homes=None,*,update_profiles=True):
    path=control_path(index_path)
    control=Control(path)
    journal=None
    try:
        for session in (sessions if update_profiles else ()):
            history=scoped_history(session.get('history',[]))
            for row in history[-1:]:
                cohort=[r for r in history if r['policy_scope']==row['policy_scope'] and r['ts']>=now-60*86400]
                outputs=[r['output'] for r in cohort if type(r.get('output')) is int and r['output']>=0]
                row=dict(row,output_samples=len(outputs),output_mean=sum(outputs)/len(outputs) if outputs else None,
                         output_high=max(outputs,default=None),output_missing=len(cohort)-len(outputs))
                control.profile(session['home'],session['id'],row)
            inputs=list(control.db.execute("SELECT turn,at FROM cache_inputs WHERE home=? AND sid=? AND kind='UserPromptSubmit' AND at>=? ORDER BY at",
                                           (session['home'],session['id'],now-60*86400)))
            turns={};firsts={}
            for row in history:
                if row.get('purpose') not in ('maintenance','diagnostic'):
                    turns[row.get('turn')]=row;firsts.setdefault(row.get('turn'),row)
            # These terminal guard choices cannot release a request. A released,
            # unavailable or unresolved ticket is NOT evidence of no transmission.
            # Actual usage takes precedence even if a conflicting ticket exists.
            blocked={r[0] for r in control.db.execute("""SELECT i.turn FROM cache_inputs i
                JOIN cache_tickets t ON t.home=i.home AND t.sid=i.sid AND t.turn=i.turn AND t.model=i.model
                WHERE i.home=? AND i.sid=? AND i.kind='UserPromptSubmit'
                AND t.state IN ('cancel','dismiss','timeout')""",(session['home'],session['id'])) if r[0] not in turns}
            for i,(turn,at) in enumerate(inputs):
                if turn not in blocked:continue
                before=[r for r in history if r['ts']<=at]
                # Preserve the cancellation, without inventing zero token usage or
                # a natural return. The surrounding real request's gap below spans
                # the entire idle period, including time after this cancellation.
                gap=dict(at=at,seconds=max(0,(inputs[i+1][1] if i+1<len(inputs) else now)-at),
                    returned=False,benefit_lower=None,maintenance_upper=None,origin='guard_cancelled',
                    settled=True,scope=before[-1]['policy_scope'] if before else None,
                    comparison='confirmed_not_sent')
                control.db.execute('INSERT OR REPLACE INTO cache_gaps VALUES(?,?,?,?)',
                    (session['home'],session['id'],turn,json.dumps(gap)))
            inputs=[(turn,at) for turn,at in inputs if turn not in blocked]
            for i,(turn,at) in enumerate(inputs):
                previous=turns.get(turn)
                if previous is None:
                    # A submitted turn without usage is not a free/no-work gap.
                    before=[r for r in history if r['ts']<=at]
                    previous=dict(ts=at,policy_scope=before[-1]['policy_scope'] if before else None)
                returned=i+1<len(inputs)
                current=firsts.get(inputs[i+1][0]) if returned else previous
                until=inputs[i+1][1] if returned else now
                bounds=cost_bounds(previous,current or {},previous.get('output'))
                gap=dict(at=previous['ts'],seconds=max(0,until-previous['ts']),returned=returned,
                         benefit_lower=bounds['benefit_lower'] if bounds else None,
                         maintenance_upper=bounds['maintenance_upper'] if bounds else None,
                         origin='submission',settled=returned,scope=previous['policy_scope'],
                         comparison=bounds['bound_kind'] if bounds else 'required_observation_missing')
                control.db.execute('INSERT OR REPLACE INTO cache_gaps VALUES(?,?,?,?)',
                                   (session['home'],session['id'],turn,json.dumps(gap)))
        journal=Journal(path)
        maintenance=[r for r in journal.rows() if homes is None or r['home'] in homes]
        groups={};revisions={}
        for row in maintenance:
            key=(row['home'],row['sid'],row['purpose'])
            revisions.setdefault(key,[]).append(row)
            if key not in groups:
                groups[key]=Session(row['purpose']+':'+row['sid'],row['home'],title='연결 진단' if row['purpose']=='diagnostic' else '캐시 유지',parent_thread_id=row['sid'])
            session=groups[key]
            session.add_usage(row['ts'],row['key'],dict(input_tokens=row['input'],cached_input_tokens=row['cached'],
                cache_write_input_tokens=row['written'],output_tokens=row['output'],reasoning_output_tokens=row['reasoning']),
                row['model'],turn=row['key'],effort=row['effort'],service_tier=row['service_tier'])
            request=session.requests[-1]
            request.purpose=row['purpose'];request.request_start=row['request_start']
            session.turn_records[row['key']]=dict(started_at=row['request_start'],ended_at=row['request_end'],
                state={'completed':'완료','sent':'진행','unknown':'미확인'}.get(row['state'],'중단'))
        for key,session in groups.items():
            purpose=key[2]
            view=session.view(now)
            revision=hashlib.sha256(json.dumps(revisions[key],sort_keys=True,separators=(',',':')).encode()).hexdigest()
            view.update(source='maintenance',collection_complete=True,archived=False,purpose=purpose,usage_revision=revision)
            sessions.append(view)
        audit=[];effects=[];compactions=[];delegated=[]
        for session in sessions:
            if session.get('purpose') in ('maintenance','diagnostic'):continue
            history=session.get('history',[])
            if session.get('parent_thread_id'):delegated.extend(history)
            for previous,current in zip(history,history[1:]):
                if previous.get('compaction_epoch')!=current.get('compaction_epoch'):
                    compactions.append(dict(before=previous.get('input'),after=current.get('input'),quality_measured=False))
            for item in observations(history[-101:])[-100:]:
                audit.append({**item,'title':session.get('title') or session['id'],'sid':session['id'],'home':session['home']})
            own=[r for r in maintenance if r['home']==session['home'] and r['sid']==session['id'] and r['purpose']=='maintenance']
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
        return dict(calls=len(maintenance),diagnostic_calls=sum(r['purpose']=='diagnostic' for r in maintenance),known_cost=sum(r['cost'] for r in maintenance if r['cost'] is not None),
                    priced=sum(r['cost'] is not None for r in maintenance),
                    unknown=sum(not r['usage_known'] for r in maintenance),audit=audit[-100:],
                    shortfalls=sum(bool(r['reuse_shortfall_scenario']) for r in audit[-100:]),effects=effects[-100:],
                    compactions=compactions[-100:],delegation=dict(calls=len(delegated),
                        known_cost=sum(token_cost(r)['cost'] or 0 for r in delegated),
                        priced=sum(token_cost(r)['cost'] is not None for r in delegated)),
                    request_activity=[dict(home=r['home'],attempt='maintenance:'+r['job_id'],response_id=r['key'],
                        request_observed_at=r['request_start'],completed_observed_at=r['request_end'],ts=r['ts'],
                        status='created' if r['state']=='sent' else r['state'],purpose=r['purpose']) for r in maintenance])
    finally:
        if journal:journal.close()
        control.close()
