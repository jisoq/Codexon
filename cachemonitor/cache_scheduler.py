"""Managed proxy lifecycle owns this scheduler; no independent polling collector."""
import asyncio
import json
import time
from urllib.parse import urlsplit

from .cache_capture import RelayCapture
from .cache_control import Control
from .cache_execution import Contexts,Executor,Journal
from .cache_policy import Gap,decide,cost_bounds,operating_scenarios,executable_rounds
from .cache_operating import target
from .core import usage_values


class Scheduler:
    def __init__(self,home,path,*,send=None,clock=time.monotonic,observation_only=False):
        self.observation_only=observation_only
        self.home=str(home);self.control=Control(path);self.journal=Journal(path)
        self.executor=Executor(self.journal,Contexts(),observation_only=observation_only,**({'send':send} if send else {}))
        self.capture=RelayCapture(self.executor,self.snapshot,lambda:self.observation_only or self.control.get('automatic',False))
        self.clock=clock;self.snapshots={};self.jobs={};self.stopped=set();self.evaluations={}
        self.revision=self.control.revision(self.home);self.last_tick=clock();self.closed=False
        self.control.db.execute('DELETE FROM cache_status WHERE home=?',(self.home,))

    def snapshot(self,request,response,anchor,url,headers,websocket,generation=None):
        if not self.observation_only and not self.control.get('automatic',False):return
        sid=request.get('prompt_cache_key')
        if not isinstance(sid,str) or not sid:return
        header_sid=next((v for k,v in headers.items() if k.lower()=='session_id'),sid)
        if header_sid!=sid:return
        row={**usage_values(response.get('usage') or {}), 'model':request.get('model'),
             'effort':(request.get('reasoning') or {}).get('effort'),'service_tier':request.get('service_tier'),
             'ts':time.time(),'request_start':time.time()-(time.monotonic()-anchor),'key':response['id']}
        self.snapshots[sid]=dict(request=request,response=response,anchor=anchor,url=url,headers=headers,websocket=websocket,row=row,
                                generation=self.executor.generation if generation is None else generation,revision=self.control.revision(self.home))
        # Contexts owns the bounded copy. Drop evicted snapshots and credentials.
        self.snapshots={s:v for s,v in self.snapshots.items() if v['response']['id'] in self.executor.contexts.responses}

    def invalidate(self):
        self.executor.ingress();self.executor.leave()
        self.cancel_schedules()
        self.snapshots.clear()

    def cancel_schedules(self):
        for job in self.jobs.values():
            if not job.done() and not job.cancelling():job.cancel()

    def policy(self,sid,snapshot):
        # API documentation does not establish this field for ChatGPT's backend.
        # A mock or explicitly verified provider can advertise the same contract.
        endpoint=urlsplit(snapshot['url'])
        supported=endpoint.hostname=='api.openai.com' or (endpoint.hostname!='chatgpt.com' and self.control.get('bounded_provider:'+endpoint.netloc,False))
        scope=target(self.home,snapshot['request'],snapshot['url'],snapshot['headers'],snapshot['websocket']) if not supported else None
        if not supported and not scope:return dict(state='disabled',reason='operating_scope_unavailable',calls=0,
            history_can_unlock=False,server_output_cap=False)
        rows=[Gap(**json.loads(r[0])) for r in self.control.db.execute('SELECT data FROM cache_gaps WHERE home=? AND sid=?',(self.home,sid))]
        rows=[g for g in rows if g.at>=time.time()-60*86400]
        row=snapshot['row'];profile=self.control.latest(self.home,sid)
        if not profile or profile['key']!=row['key'] or not profile.get('policy_scope'):
            return dict(state='waiting',reason='history_scope_unobserved')
        row={**row,'service_tier':row.get('service_tier') or profile.get('service_tier'),
             'compaction_epoch':profile.get('compaction_epoch')};snapshot['row']=row
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
        if not supported:
            grants={g['id'] for g in self.journal.operations.grants() if g['scope']==scope}
            observed=[r for r in self.journal.rows() if r['operation'] in grants]
            scenarios=operating_scenarios(row,profile,extra,observed)
            if not scenarios:
                grant=self.journal.operations.permission(scope)
                return dict(state='stopped' if grant and grant['stopped'] else 'waiting',
                    reason=grant['stopped'] if grant and grant['stopped'] else 'operating_cost_unobserved',calls=0,server_output_cap=False)
            # Feed the existing chronological comparison with an estimate, not
            # an invented output/cost guarantee. Preserve unknown historical costs.
            bounds={**scenarios,'maintenance_upper':scenarios['maintenance_expected']}
        # Reprice every historical maintenance against this session's size/budget.
        rows=[Gap(g.at,g.seconds,g.returned,min(g.benefit_lower,bounds['benefit_lower']) if g.benefit_lower is not None else None,
                  max(g.maintenance_upper,bounds['maintenance_upper']) if g.maintenance_upper is not None else None,
                  g.origin,g.settled,g.scope,g.comparison) for g in rows]
        caps=[int(g.benefit_lower/bounds['maintenance_upper']) for g in rows if type(g.benefit_lower) in (int,float) and bounds['maintenance_upper']>0]
        max_calls=max(1,min(100,max(caps,default=1)))
        if scenarios:
            # Optimize INSIDE the consent allowance, never truncate a profitable
            # long-horizon policy to a shorter, potentially loss-making pilot.
            grant=self.journal.operations.permission(scope)
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
            if self.observation_only:return {**result,'policy_state':decision['state'],'state':'observing','execution_disabled':True}
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
            return {**result,'operation':operation}
        return {**decision,**bounds,'latency_bound':30}

    async def maintain(self,sid,snapshot,decision,revision):
        rid=snapshot['response']['id'];anchor=snapshot['anchor']
        for round_number in range(decision['calls']):
            if (self.closed or not self.control.get('automatic',False) or
                    self.executor.generation!=snapshot['generation'] or self.control.revision(self.home)!=revision):break
            def valid():
                if not self.control.get('automatic',False) or self.control.revision(self.home)!=revision:return False
                if decision.get('operation'):
                    grant=self.journal.operations.permission(decision['operation']['scope'])
                    if not grant or grant['id']!=decision['operation']['id']:return False
                    # A delayed first start may invalidate the remaining profitable
                    # plan even while this individual request is still permitted.
                    needed=decision['calls']-round_number
                    if executable_rounds(anchor,time.monotonic(),grant['expires']-time.time(),needed,
                                         latency_bound=decision['latency_bound'])<needed:return False
                return True
            result=await self.executor.run(self.home,sid,rid,snapshot['url'],snapshot['headers'],anchor=anchor,
                deadline=anchor+decision['interval'],latency_bound=decision['latency_bound'],websocket=snapshot['websocket'],
                round_number=round_number,max_output_tokens=decision.get('output_cap'),operation=decision.get('operation'),
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
        if self.observation_only:
            for sid,snapshot in list(self.snapshots.items()):
                if time.monotonic()-snapshot['anchor']>=1800:
                    self.snapshots.pop(sid,None)
                    rid=snapshot['response']['id'];item=self.executor.contexts.responses.pop(rid,None)
                    if item:self.executor.contexts.size-=item[2]
                    self.executor.contexts.usage.pop(rid,None)
                    continue
                previous=self.evaluations.get(sid)
                if previous and previous[0]==snapshot['response']['id'] and time.monotonic()-previous[1]<5:continue
                self.evaluations[sid]=(snapshot['response']['id'],time.monotonic())
                self.control.status(self.home,sid,{**self.policy(sid,snapshot),'observation_only':True,
                    'snapshot':snapshot['response']['id'],'observed_at':snapshot['row']['ts']})
            return
        now=self.clock();revision=self.control.revision(self.home)
        if not self.control.get('automatic',False):
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
            if not self.control.get('automatic',False):continue
            previous=self.evaluations.get(sid)
            if previous and previous[0]==snapshot['response']['id'] and now-previous[1]<60:continue
            self.evaluations[sid]=(snapshot['response']['id'],now)
            decision=self.policy(sid,snapshot);self.control.status(self.home,sid,decision)
            if decision['state']=='eligible':self.jobs[sid]=asyncio.create_task(self.maintain(sid,snapshot,decision,revision))

    async def serve(self):
        while not self.closed:
            try:await self.tick()
            except Exception:
                self.invalidate()
            await asyncio.sleep(.2)

    async def close(self):
        self.closed=True;self.executor.close()
        self.cancel_schedules()
        await asyncio.gather(*self.jobs.values(),return_exceptions=True)
        self.control.close();self.journal.close()
