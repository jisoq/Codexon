"""Source cache worker control. Never submits a model request itself."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cachemonitor.cache_control import Control
from cachemonitor.cache_execution import Journal


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','authorize-diagnostic','queue-diagnostic','end'))
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--home',type=Path,required=True)
    parser.add_argument('--account-hash')
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('--cost-stop',type=float)
    parser.add_argument('--grant')
    parser.add_argument('--authorization',help='Explicit one-time authorization identifier; cannot be reused')
    args=parser.parse_args();home=str(args.home.resolve())
    control=Control(args.database);journal=Journal(args.database)
    try:
        if args.action=='authorize-diagnostic':
            if not args.authorization or not args.account_hash or len(args.account_hash)!=64 or not args.model or not args.effort or args.cost_stop is None:
                parser.error('Explicit authorization, full account hash, model, effort and cost stop required')
            if control.get('authorization:'+args.authorization):raise ValueError('authorization_already_used')
            states=control.forecasts(home,time.time())
            states=[s for s in states if s.get('model')==args.model and s.get('effort')==args.effort and
                    s.get('service_tier') in ('default','Standard') and s.get('maintenance_expected')]
            if not states:raise ValueError('fresh_pricing_required')
            estimate=max(states,key=lambda s:s.get('observed_at',0))
            scope=dict(home=home,account=args.account_hash,model=args.model,effort=args.effort,tier='default',transport='http',
                       endpoint='https://chatgpt.com/backend-api/codex/responses')
            proposal=journal.operations.propose(scope,estimate['maintenance_expected'],estimate['maintenance_adverse'],
                estimate['output_high'],estimate['basis'],cost_stop=args.cost_stop,purpose='diagnostic',dynamic_estimate=True)
            # Consume the authorization identifier before granting. A failure is
            # conservative and cannot accidentally create another time window.
            control.set('authorization:'+args.authorization,dict(started=time.time()))
            key=journal.operations.consent(proposal['id'])
            control.set('authorization:'+args.authorization,dict(grant=key))
            print(json.dumps(dict(grant=key)))
        elif args.action in ('queue-diagnostic','end'):
            grant=next((g for g in journal.operations.grants(home) if g['id']==args.grant),None)
            if not grant:raise ValueError('grant_not_found')
            if args.action=='end':
                journal.operations.stop(grant['id'],'verification_complete')
                control.set('diagnostic_request',None)
                previous=control.get('diagnostic_result',{})
                control.set('diagnostic_result',dict(state='stopped',reason='verification_complete',last_result=previous))
            else:
                if grant.get('purpose')!='diagnostic' or grant['stopped'] or grant['expires']<=time.time():raise ValueError('grant_inactive')
                if control.get('diagnostic_request'):raise ValueError('diagnostic_already_queued')
                control.set('diagnostic_request',grant['id'])
        states=control.forecasts(home,time.time())
        print(json.dumps(dict(home=home,worker_heartbeat=control.get('worker_heartbeat'),
            worker_snapshots=control.get('worker_snapshots',0),
            collector_error=control.get('collector_error'),worker_error=control.get('worker_error'),
            hooks=[dict(kind=k,count=n,last_at=t) for k,n,t in control.db.execute(
                'SELECT kind,COUNT(*),MAX(at) FROM cache_inputs WHERE home=? GROUP BY kind',(home,))],
            profiles=control.db.execute('SELECT COUNT(*) FROM cache_profiles WHERE home=?',(home,)).fetchone()[0],
            current_scenarios=states,
            diagnostic=control.get('diagnostic_result'),pending=control.get('diagnostic_request'),
            grants=[dict(g,stats=journal.operations.stats(g)) for g in journal.operations.grants(home)],
            usage=journal.rows()),ensure_ascii=False,indent=2))
    finally:control.close();journal.close()


if __name__=='__main__':main()
