"""Managed proxy lifecycle owns this scheduler; no independent polling collector."""
import asyncio
import json
import time
from urllib.parse import urlsplit

from .cache_capture import RelayCapture
from .cache_control import Control
from .cache_execution import Contexts,Executor,Journal
from .cache_policy import Gap,decide,cost_bounds
from .core import usage_values


class Scheduler:
    def __init__(self,home,path,*,send=None,clock=time.monotonic):
        self.home=str(home);self.control=Control(path);self.journal=Journal(path)
        self.executor=Executor(self.journal,Contexts(),**({'send':send} if send else {}))
        self.capture=RelayCapture(self.executor,self.snapshot,lambda:self.control.get('automatic',False))
        self.clock=clock;self.snapshots={};self.jobs={};self.stopped=set();self.evaluations={}
        self.revision=self.control.revision(self.home);self.last_tick=clock();self.closed=False
        self.control.db.execute('DELETE FROM cache_status WHERE home=?',(self.home,))

    def snapshot(self,request,response,anchor,url,headers,websocket,generation=None):
        if not self.control.get('automatic',False):return
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
        for job in self.jobs.values():job.cancel()
        self.snapshots.clear()

    def policy(self,sid,snapshot):
        # API documentation does not establish this field for ChatGPT's backend.
        # A mock or explicitly verified provider can advertise the same contract.
        endpoint=urlsplit(snapshot['url'])
        supported=endpoint.hostname=='api.openai.com' or self.control.get('bounded_provider:'+endpoint.netloc,False)
        if not supported:return dict(state='waiting',reason='output_bound_not_verified')
        rows=[Gap(**json.loads(r[0])) for r in self.control.db.execute('SELECT data FROM cache_gaps WHERE home=? AND sid=?',(self.home,sid))]
        rows=[g for g in rows if g.at>=time.time()-60*86400]
        row=snapshot['row'];profile=self.control.latest(self.home,sid)
        if profile and profile['key']==row['key'] and not row.get('service_tier'):
            row={**row,'service_tier':profile.get('service_tier')};snapshot['row']=row
        output_cap=row.get('output')
        if type(output_cap) is not int or output_cap<=0:return dict(state='waiting',reason='output_budget_unobserved')
        # UTF-8 serialized suffix size is a conservative token-count scenario,
        # not a tokenizer measurement. The actual input cost is reconciled later.
        maintenance=self.executor.contexts.maintenance(snapshot['response']['id'])
        original=self.executor.contexts.responses[snapshot['response']['id']][0]
        extra=len(json.dumps(maintenance['input'][len(original['input']):],ensure_ascii=False).encode())
        bounds=cost_bounds(row,{**row,'cached':0},output_cap,extra)
        if not bounds:return dict(state='waiting',reason='input_or_price_unobserved')
        # Reprice every historical maintenance against this session's size/budget.
        rows=[Gap(g.at,g.seconds,g.returned,min(g.benefit_lower,bounds['benefit_lower']) if g.benefit_lower is not None else None,
                  bounds['maintenance_upper'],g.origin,g.settled) for g in rows]
        caps=[int(g.benefit_lower/bounds['maintenance_upper']) for g in rows if type(g.benefit_lower) in (int,float) and bounds['maintenance_upper']>0]
        decision=decide(rows,latency_bound=30,scheduler_slack=1,max_calls=max(1,min(100,max(caps,default=1))))
        return {**decision,**bounds,'latency_bound':30}

    async def maintain(self,sid,snapshot,decision,revision):
        rid=snapshot['response']['id'];anchor=snapshot['anchor']
        for round_number in range(decision['calls']):
            result=await self.executor.run(self.home,sid,rid,snapshot['url'],snapshot['headers'],anchor=anchor,
                deadline=anchor+decision['interval'],latency_bound=decision['latency_bound'],websocket=snapshot['websocket'],
                round_number=round_number,max_output_tokens=decision['output_cap'],
                expected_generation=snapshot['generation'],
                valid=lambda:self.control.get('automatic',False) and self.control.revision(self.home)==revision)
            self.control.status(self.home,sid,dict(state=result,round=round_number+1,measured_saving=None))
            renewal=self.executor.renewals.get((self.home,sid,rid))
            if result!='completed' or not renewal:break
            # A smaller maintained portion cannot inherit the full benefit estimate.
            if renewal['read_lower']<(snapshot['row'].get('cached') or 0):break
            observed=next(r for r in self.journal.rows() if r['home']==self.home and r['sid']==sid and r['snapshot']==rid and r['round']==round_number)
            if (observed['cost'] is None or observed['cost']>decision['maintenance_upper'] or
                    observed.get('output') is None or observed['output']>decision['output_cap']):break
            anchor=renewal['anchor']
        self.stopped.add((sid,rid))

    async def tick(self):
        now=self.clock();revision=self.control.revision(self.home)
        if not self.control.get('automatic',False):
            self.invalidate();self.executor.contexts.clear();self.last_tick=now;self.revision=revision
            return
        if now-self.last_tick>5 or revision!=self.revision:
            # Cancel old schedules, but a new Stop may release the latest captured request.
            for task in self.jobs.values():task.cancel()
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
        for task in self.jobs.values():task.cancel()
        await asyncio.gather(*self.jobs.values(),return_exceptions=True)
        self.control.close();self.journal.close()
