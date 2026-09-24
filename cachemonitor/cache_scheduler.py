"""Managed proxy lifecycle owns this scheduler; no independent polling collector."""
import asyncio
import json
import sqlite3
import time
from urllib.parse import urlsplit

from .cache_capture import RelayCapture
from .cache_control import Control
from .cache_execution import Contexts,Executor,Journal
from .cache_policy import Gap,decide,cost_bounds,operating_scenarios,executable_rounds
from .cache_operating import target,maintenance_route
from .core import usage_values


class Scheduler:
    def __init__(self,home,path,*,send=None,clock=time.monotonic,observation_only=False,continuous_capture=False):
        self.observation_only=observation_only
        self.continuous_capture=continuous_capture
        self.home=str(home);self.control=Control(path);self.journal=Journal(path)
        self.executor=Executor(self.journal,Contexts(),observation_only=observation_only,**({'send':send} if send else {}))
        self.capture=RelayCapture(self.executor,self.snapshot,lambda:self.continuous_capture or self.observation_only or self.control.enabled('automatic'))
        self.capture.analysis_revision=2
        self.clock=clock;self.snapshots={};self.jobs={};self.stopped=set();self.evaluations={}
        self.revision=self.control.revision(self.home);self.last_tick=clock();self.closed=False
        self.control.db.execute('DELETE FROM cache_status WHERE home=?',(self.home,))

    def snapshot(self,request,response,anchor,url,headers,websocket,generation=None):
        if not self.continuous_capture and not self.observation_only and not self.control.enabled('automatic'):return
        sid=request.get('prompt_cache_key')
        if not isinstance(sid,str) or not sid:return
        header_sid=next((v for k,v in headers.items() if k.lower()=='session_id'),sid)
        if header_sid!=sid:return
        row={**usage_values(response.get('usage') or {}), 'model':request.get('model'),
             'effort':(request.get('reasoning') or {}).get('effort'),'service_tier':request.get('service_tier'),
             'ts':time.time(),'request_start':time.time()-(time.monotonic()-anchor),'key':response['id']}
        self.snapshots[sid]=dict(request=request,response=response,anchor=anchor,url=url,headers=headers,websocket=websocket,row=row,
                                generation=self.executor.generation if generation is None else generation,revision=self.control.revision(self.home))
        # A new context must never borrow the previous context's estimate, even
        # while the usage collector is still catching up with this response.
        self.control.forecast(self.home,sid,dict(snapshot=response['id'],observed_at=row['ts']))
        # Contexts owns the bounded copy. Drop evicted snapshots and credentials.
        self.snapshots={s:v for s,v in self.snapshots.items() if v['response']['id'] in self.executor.contexts.responses}

    def invalidate(self):
        self.executor.ingress();self.executor.leave()
        self.cancel_schedules()
        self.snapshots.clear()

    def cancel_schedules(self):
        for job in self.jobs.values():
            if not job.done() and not job.cancelling():job.cancel()

    def policy(self,sid,snapshot,*,observe=False):
        observing=self.observation_only or observe
        # API documentation does not establish this field for ChatGPT's backend.
        # A mock or explicitly verified provider can advertise the same contract.
        endpoint=urlsplit(snapshot['url'])
        supported=endpoint.hostname=='api.openai.com' or (endpoint.hostname!='chatgpt.com' and self.control.get('bounded_provider:'+endpoint.netloc,False))
        execution_url,execution_websocket=maintenance_route(snapshot['url'],snapshot['websocket'])
        rows=[Gap(**json.loads(r[0])) for r in self.control.db.execute('SELECT data FROM cache_gaps WHERE home=? AND sid=?',(self.home,sid))]
        rows=[g for g in rows if g.at>=time.time()-60*86400]
        row=snapshot['row'];profile=self.control.latest(self.home,sid)
        if not profile or profile['key']!=row['key'] or not profile.get('policy_scope'):
            return dict(state='waiting',reason='history_scope_unobserved')
        row={**row,'service_tier':row.get('service_tier') or profile.get('service_tier'),
             'compaction_epoch':profile.get('compaction_epoch')};snapshot['row']=row
        scope=target(self.home,snapshot['request'],execution_url,snapshot['headers'],execution_websocket,
                     observed_tier=row.get('service_tier')) if not supported else None
        # Do not mix old model/effort/context regimes with the current context.
        # Unclassified legacy records inside this regime remain unknown, not free.
        rows=[g for g in rows if g.scope==profile['policy_scope'] or
              (g.scope is None and g.at>=profile['policy_scope_start'])]
        output_cap=row.get('output')
        if type(output_cap) is not int or output_cap<=0:return dict(state='waiting',reason='output_budget_unobserved')
        # UTF-8 serialized suffix size is a conservative token-count scenario,
        # not a tokenizer measurement. The actual input cost is reconciled later.
        maintenance=self.executor.contexts.maintenance(snapshot['response']['id'])
        original=self.executor.contexts.responses[snapshot['response']['id']][0]
        extra=len(json.dumps(maintenance['input'][len(original['input']):],ensure_ascii=False).encode())
        bounds=cost_bounds(row,{**row,'cached':0},output_cap,extra)
        if not bounds:return dict(state='waiting',reason='input_or_price_unobserved')
        operation=None;scenarios=None
        if not supported or observing:
            grants={g['id'] for g in self.journal.operations.grants() if g['scope']==scope}
            observed=[r for r in self.journal.rows() if r['operation'] in grants and r['purpose']=='maintenance']
            scenarios=operating_scenarios(row,profile,extra,observed)
            if not scenarios:
                grant=self.journal.operations.permission(scope)
                return dict(state='stopped' if grant and grant['stopped'] else 'waiting',
                    reason=grant['stopped'] if grant and grant['stopped'] else 'operating_cost_unobserved',calls=0,server_output_cap=False)
            # Feed the existing chronological comparison with an estimate, not
            # an invented output/cost guarantee. Preserve unknown historical costs.
            bounds={**scenarios,'maintenance_upper':scenarios['maintenance_expected']}
        forecast=scenarios or operating_scenarios(row,profile,extra,[])
        if forecast:
            self.control.forecast(self.home,sid,{**forecast,
                'snapshot':snapshot['response']['id'],'observed_at':row['ts'],
                'model':row['model'],'effort':row['effort'],'service_tier':row['service_tier'],
                'source_transport':'WebSocket' if snapshot['websocket'] else 'HTTP',
                'maintenance_transport':'WebSocket' if execution_websocket else 'HTTP',
                'operating_scope_available':bool(scope),'cost_stop_scenario':2*forecast['maintenance_expected'],
                'cost_valid_until':time.time()+1800-(time.monotonic()-snapshot['anchor']),
                'worker_revision':3 if self.continuous_capture else 2})
        if not observing and not supported and not scope:return dict(state='disabled',reason='operating_scope_unavailable',calls=0,
            history_can_unlock=False,server_output_cap=False)
        # Reprice every historical maintenance against this session's size/budget.
        rows=[Gap(g.at,g.seconds,g.returned,min(g.benefit_lower,bounds['benefit_lower']) if g.benefit_lower is not None else None,
                  max(g.maintenance_upper,bounds['maintenance_upper']) if g.maintenance_upper is not None else None,
                  g.origin,g.settled,g.scope,g.comparison) for g in rows]
        caps=[int(g.benefit_lower/bounds['maintenance_upper']) for g in rows if type(g.benefit_lower) in (int,float) and bounds['maintenance_upper']>0]
        max_calls=max(1,min(100,max(caps,default=1)))
        if observing:
            # Cost visibility does not depend on a grant, return history or an
            # executable schedule. Never change grants while observing.
            decision=decide(rows,latency_bound=30,scheduler_slack=1,max_calls=min(2,max_calls))
            return {**decision,**scenarios,'policy_state':decision['state'],'policy_calls':decision['calls'],
                'state':'observing','calls':0,'execution_disabled':True,
                'source_transport':'WebSocket' if snapshot['websocket'] else 'HTTP',
                'maintenance_transport':'WebSocket' if execution_websocket else 'HTTP',
                'model':row['model'],'effort':row['effort'],'service_tier':row['service_tier'],
                'operating_scope_available':bool(scope),'cost_stop_scenario':2*scenarios['maintenance_expected'],
                'cost_valid_until':row['request_start']+1800,'transport_reuse_verified':False}
        if scenarios:
            # Optimize INSIDE the consent allowance, never truncate a profitable
            # long-horizon policy to a shorter, potentially loss-making pilot.
            grant=self.journal.operations.permission(scope) if scope else None
            remaining=self.journal.operations.stats(grant)['remaining'] if grant and not grant['stopped'] else 2
            if not remaining:
                self.journal.operations.stop(grant['id'],'call_limit')
                return dict(state='stopped',reason='call_limit',calls=0,server_output_cap=False)
            max_calls=min(max_calls,2,remaining)
            expires_in=grant['expires']-time.time() if grant and not grant['stopped'] else 3600
            max_calls=executable_rounds(snapshot['anchor'],time.monotonic(),expires_in,max_calls)
            if not max_calls:
                return dict(state='off',reason='no_executable_rounds',calls=0,server_output_cap=False)
        decision=decide(rows,latency_bound=30,scheduler_slack=1,max_calls=max_calls)
        if scenarios:
            result={**decision,**scenarios,'latency_bound':30}
            if decision['state']!='eligible':return result
            proposal=self.journal.operations.propose(scope,scenarios['maintenance_expected'],
                scenarios['maintenance_adverse'],scenarios['output_high'],scenarios['basis'])
            grant=self.journal.operations.permission(scope)
            if not grant:return {**result,'state':'disabled','reason':'operating_consent_required','calls':0,
                'history_can_unlock':False,'proposal':proposal['id']}
            operation=dict(id=grant['id'],scope=scope,expected=scenarios['maintenance_expected'],
                           adverse=scenarios['maintenance_adverse'],output_high=scenarios['output_high'])
            reason=self.journal.operations.check(operation)
            if reason:return {**result,'state':'stopped' if reason!='operation_busy' else 'waiting','reason':reason,'calls':0}
            return {**result,'operation':operation,'execution_url':execution_url,'execution_websocket':execution_websocket}
        return {**decision,**bounds,'latency_bound':30}

    def diagnostic_decision(self,sid,snapshot,grant):
        """Fresh pricing and transport permission, independent of return economics."""
        decision=self.policy(sid,snapshot,observe=True)
        url,websocket=maintenance_route(snapshot['url'],snapshot['websocket'])
        scope=target(self.home,snapshot['request'],url,snapshot['headers'],websocket,observed_tier=snapshot['row'].get('service_tier'))
        if scope!=grant['scope']:return dict(state='disabled',reason='scope_mismatch')
        if decision.get('maintenance_expected') is None:return decision
        operation=dict(id=grant['id'],scope=scope,purpose='diagnostic',expected=decision['maintenance_expected'],
            adverse=decision['maintenance_adverse'],output_high=decision['output_high'])
        reason=self.journal.operations.check(operation)
        return dict(decision,state='disabled' if reason else 'eligible',reason=reason or 'diagnostic_ready',
                    operation=operation,execution_url=url,execution_websocket=websocket)

    def idle(self,sid,snapshot):
        if self.executor.busy or snapshot['generation']!=self.executor.generation:return False
        latest=self.control.db.execute('SELECT kind,at FROM cache_inputs WHERE home=? AND sid=? ORDER BY rowid DESC LIMIT 1',
                                       (self.home,sid)).fetchone()
        return bool(latest and latest[0]=='Stop' and latest[1]>=snapshot['row']['request_start'])

    async def diagnostic_tick(self):
        key=self.control.get('diagnostic_request')
        if not key or self.observation_only or not self.control.get('enabled',True):return
        grant=next((g for g in self.journal.operations.grants(self.home) if g['id']==key),None)
        if not grant or grant.get('purpose')!='diagnostic':
            self.control.set('diagnostic_request',None);return
        if grant['stopped'] or grant['expires']<=time.time():
            self.journal.operations.stop(key,'permission_expired');self.control.set('diagnostic_request',None);return
        self.control.set('diagnostic_result',dict(state='collecting',reason='fresh_context_required'))
        for sid,snapshot in sorted(self.snapshots.items(),key=lambda item:item[1]['row']['ts'],reverse=True):
            if time.monotonic()-snapshot['anchor']+30>=1800:continue
            decision=self.diagnostic_decision(sid,snapshot,grant)
            self.control.set('diagnostic_result',{k:decision.get(k) for k in ('state','reason','maintenance_expected','maintenance_adverse')})
            if decision.get('state')!='eligible':continue
            if not self.idle(sid,snapshot):
                self.control.set('diagnostic_result',dict(state='waiting',reason='user_active'));continue
            # Claim once. A crash, ambiguous send, or failure cannot regenerate it.
            self.control.set('diagnostic_request',None)
            async def run(sid=sid,snapshot=snapshot,decision=decision):
                revision=self.control.revision(self.home)
                def valid():return self.control.get('enabled',True) and self.idle(sid,snapshot) and self.control.revision(self.home)==revision
                result=await self.executor.run(self.home,sid,snapshot['response']['id'],decision['execution_url'],snapshot['headers'],
                    anchor=snapshot['anchor'],deadline=time.monotonic(),latency_bound=30,websocket=False,
                    operation=decision['operation'],observed_tier=snapshot['row'].get('service_tier'),
                    expected_generation=snapshot['generation'],valid=valid)
                self.control.set('diagnostic_result',dict(state=result,reason='diagnostic_'+result,operation=key))
            self.jobs['diagnostic:'+key]=asyncio.create_task(run())
            break

    async def maintain(self,sid,snapshot,decision,revision):
        rid=snapshot['response']['id'];anchor=snapshot['anchor']
        for round_number in range(decision['calls']):
            if (self.closed or not self.control.enabled('automatic') or
                    self.executor.generation!=snapshot['generation'] or self.control.revision(self.home)!=revision):break
            def valid():
                if not self.control.enabled('automatic') or self.control.revision(self.home)!=revision:return False
                if decision.get('operation'):
                    grant=self.journal.operations.permission(decision['operation']['scope'])
                    if not grant or grant['id']!=decision['operation']['id']:return False
                    # A delayed first start may invalidate the remaining profitable
                    # plan even while this individual request is still permitted.
                    needed=decision['calls']-round_number
                    if executable_rounds(anchor,time.monotonic(),grant['expires']-time.time(),needed,
                                         latency_bound=decision['latency_bound'])<needed:return False
                return True
            result=await self.executor.run(self.home,sid,rid,decision.get('execution_url',snapshot['url']),snapshot['headers'],anchor=anchor,
                deadline=anchor+decision['interval'],latency_bound=decision['latency_bound'],websocket=decision.get('execution_websocket',snapshot['websocket']),
                round_number=round_number,max_output_tokens=decision.get('output_cap'),operation=decision.get('operation'),
                observed_tier=snapshot['row'].get('service_tier'),
                expected_generation=snapshot['generation'],
                valid=valid)
            self.control.status(self.home,sid,dict(state=result,round=round_number+1,measured_saving=None))
            if result=='operation_deferred':
                self.evaluations.pop(sid,None);return
            renewal=self.executor.renewals.get((self.home,sid,rid))
            if result!='completed' or not renewal:break
            # A smaller maintained portion cannot inherit the full benefit estimate.
            if renewal['read_lower']<(snapshot['row'].get('cached') or 0):
                if decision.get('operation'):self.journal.operations.stop(decision['operation']['id'],'partial_reuse')
                break
            observed=next(r for r in self.journal.rows() if r['home']==self.home and r['sid']==sid and r['snapshot']==rid and r['round']==round_number)
            if decision.get('operation'):
                if self.journal.operations.check(decision['operation']):break
            elif (observed['cost'] is None or observed['cost']>decision['maintenance_upper'] or
                    observed.get('output') is None or observed['output']>decision['output_cap']):break
            anchor=renewal['anchor']
        self.stopped.add((sid,rid))

    async def tick(self):
        if self.closed:return
        if self.continuous_capture:
            self.control.set('worker_heartbeat',time.time())
            self.control.set('worker_snapshots',len(self.snapshots))
            await self.diagnostic_tick()
        if self.observation_only or (self.continuous_capture and not self.control.enabled('automatic')):
            for sid,snapshot in list(self.snapshots.items()):
                if time.monotonic()-snapshot['anchor']>=1800:
                    self.snapshots.pop(sid,None)
                    rid=snapshot['response']['id'];item=self.executor.contexts.responses.pop(rid,None)
                    if item:self.executor.contexts.size-=item[2]
                    self.executor.contexts.usage.pop(rid,None)
                    self.control.db.execute('DELETE FROM cache_status WHERE home=? AND sid=?',(self.home,sid))
                    continue
                previous=self.evaluations.get(sid)
                if previous and previous[0]==snapshot['response']['id'] and time.monotonic()-previous[1]<5:continue
                self.evaluations[sid]=(snapshot['response']['id'],time.monotonic())
                decision=self.policy(sid,snapshot,observe=True) if self.continuous_capture else self.policy(sid,snapshot)
                self.control.status(self.home,sid,{**decision,'observation_only':self.observation_only,'passive_analysis':True,
                    'worker_revision':3 if self.continuous_capture else 2,
                    'snapshot':snapshot['response']['id'],'observed_at':snapshot['row']['ts']})
            return
        now=self.clock();revision=self.control.revision(self.home)
        if not self.control.enabled('automatic'):
            self.invalidate();self.executor.contexts.clear();self.last_tick=now;self.revision=revision
            return
        if now-self.last_tick>5 or revision!=self.revision:
            # Cancel old schedules, but a new Stop may release the latest captured request.
            self.cancel_schedules()
            self.snapshots={s:v for s,v in self.snapshots.items() if v['revision']==revision}
            if now-self.last_tick>5:
                self.executor.ingress();self.executor.leave();self.snapshots.clear()
            self.revision=revision
        self.last_tick=now
        for sid,snapshot in list(self.snapshots.items()):
            active=self.jobs.get(sid)
            if active and not active.done():continue
            if (sid,snapshot['response']['id']) in self.stopped:continue
            latest=self.control.db.execute('SELECT kind,at FROM cache_inputs WHERE home=? AND sid=? ORDER BY rowid DESC LIMIT 1',(self.home,sid)).fetchone()
            if not latest or latest[0]!='Stop' or latest[1]<snapshot['row']['request_start']:continue
            if not self.control.enabled('automatic'):continue
            previous=self.evaluations.get(sid)
            if previous and previous[0]==snapshot['response']['id'] and now-previous[1]<60:continue
            self.evaluations[sid]=(snapshot['response']['id'],now)
            decision=self.policy(sid,snapshot)
            self.control.status(self.home,sid,{**decision,'snapshot':snapshot['response']['id'],'observed_at':snapshot['row']['ts']})
            if decision['state']=='eligible':self.jobs[sid]=asyncio.create_task(self.maintain(sid,snapshot,decision,revision))

    async def serve(self):
        while not self.closed:
            try:
                await self.tick()
                if self.continuous_capture:self.control.set('worker_error',False)
            except Exception:
                self.invalidate()
                if self.continuous_capture:
                    try:self.control.set('worker_error',True)
                    except (sqlite3.Error,OSError):pass
            await asyncio.sleep(.2)

    async def close(self):
        self.closed=True;self.executor.close()
        self.cancel_schedules()
        await asyncio.gather(*self.jobs.values(),return_exceptions=True)
        self.control.close();self.journal.close()
