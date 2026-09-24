"""Cross-component contracts: policy bootstrap, execution, final usage and hooks."""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from cachemonitor.cache_control import Control,control_path,hook_decision
from cachemonitor.cache_execution import Journal
from cachemonitor.cache_integration import enrich
from cachemonitor.cache_policy import cost_bounds
from cachemonitor.cache_scheduler import Scheduler
from cachemonitor.core import Session
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.quota_cycles import QuotaLedger
from test_cache_management import body,response


def profile():
    return dict(key='original',ts=time.time(),model='gpt-6-luna',effort='low',service_tier='Standard',
                input=100000,cached=99000,written=None,output=4,reasoning=2)


def test_durable_hook_exact_choice_timeout_disconnect(tmp_path):
    path=tmp_path/'c.sqlite';control=Control(path);control.set('guard',True)
    control.profile('home','s',dict(profile(),written=0))
    event=dict(hook_event_name='UserPromptSubmit',session_id='s',turn_id='t',model='gpt-6-sol',prompt='not persisted')
    with ThreadPoolExecutor(1) as pool:
        for choice in ('approve','cancel','dismiss','timeout','unavailable'):
            event={**event,'turn_id':choice}
            control.set('ui_heartbeat',time.time())
            future=pool.submit(hook_decision,path,'home',event,.3 if choice=='timeout' else 5)
            limit=time.monotonic()+3
            while not (tickets:=control.requests()) and time.monotonic()<limit:time.sleep(.01)
            assert tickets
            ticket=tickets[0]
            assert not control.resolve(dict(ticket,model='wrong'),'approve')
            if choice=='unavailable':control.set('ui_heartbeat',0)
            elif choice!='timeout':assert control.resolve(ticket,choice)
            result=future.result(timeout=3)
            assert bool(result)==(choice in ('cancel','dismiss','timeout'))
            if result:assert result['continue'] is False
            assert not control.resolve(ticket,'approve')
            assert 'additionalContext' not in result and 'systemMessage' not in result
            control.set('ui_heartbeat',time.time())
            assert hook_decision(path,'home',event)['continue'] is False
    assert 'not persisted' not in path.read_bytes().decode(errors='ignore')
    control.close()


def test_master_disabled_hook_observes_without_confirmation(tmp_path):
    path=tmp_path/'control.sqlite';control=Control(path)
    control.profile('home','s',dict(profile(),written=0));control.set('guard',True);control.set('enabled',False)
    control.set('ui_heartbeat',time.time())
    event=dict(hook_event_name='UserPromptSubmit',session_id='s',turn_id='off',model='gpt-6-sol')
    assert control.guard_needed('home',event)
    assert hook_decision(path,'home',event)=={}
    assert control.db.execute('SELECT COUNT(*) FROM cache_inputs').fetchone()[0]==1
    assert not control.requests()
    control.close()


def test_worker_recovers_when_error_reporting_also_hits_a_database_lock(tmp_path,monkeypatch):
    import sqlite3
    scheduler=Scheduler('home',tmp_path/'control.sqlite',continuous_capture=True)
    original=scheduler.control.set;attempts=[]
    def write(key,value):
        attempts.append(key)
        if len(attempts)<=2:raise sqlite3.OperationalError('database is locked')
        original(key,value)
        if key=='worker_error' and value is False:scheduler.closed=True
    monkeypatch.setattr(scheduler.control,'set',write)
    async def run():
        await asyncio.wait_for(scheduler.serve(),2)
        assert scheduler.control.get('worker_heartbeat')
        await scheduler.close()
    asyncio.run(run())
    assert attempts[:2]==['worker_heartbeat','worker_error']


def test_natural_history_to_policy_no_maintenance_prerequisite(tmp_path):
    now=time.time();path=tmp_path/'index.sqlite';control=Control(control_path(path))
    session=Session('s','home')
    # Two independent cold returns, followed by warm calls. No maintenance data.
    for i in range(6):
        start=now-12000+i*2100;turn=str(i)
        control.db.execute('INSERT INTO cache_inputs VALUES(?,?,?,?,?,?)',('home','s',turn,start,'gpt-6-luna','UserPromptSubmit'))
        session.add_usage(start+1,'cold'+turn,dict(input_tokens=100000,cached_input_tokens=0,output_tokens=4),
                          'gpt-6-luna',turn,'low','Standard')
        session.add_usage(start+2,'warm'+turn,dict(input_tokens=100000,cached_input_tokens=99000,output_tokens=4),
                          'gpt-6-luna',turn,'low','Standard')
    views=[session.view(now)]
    enrich(views,path,now)
    scheduler=Scheduler('home',control_path(path));scheduler.control.set('automatic',True)
    row=dict(profile(),key='warm5');request={**body(),'prompt_cache_key':'s'};result=dict(response(),id='warm5')
    result['usage'].update(input_tokens=100000,input_tokens_details={'cached_tokens':99000})
    scheduler.executor.contexts.completed(request,result)
    scheduler.snapshot(request,result,time.monotonic(),'https://api.openai.com/v1/responses',{},False)
    scheduler.snapshots['s']['row']=row
    decision=scheduler.policy('s',scheduler.snapshots['s'])
    # Natural cold returns must not be replaced with their later warm calls.
    assert decision['state']=='eligible',decision
    assert not scheduler.journal.rows()
    assert decision['output_cap']==row['output']
    assert decision['write_composition']=='bounded_not_observed' and row['written'] is None
    scheduler.snapshots['s']['url']='https://chatgpt.com/backend-api/codex/responses'
    scheduler.snapshots['s']['headers']={'ChatGPT-Account-ID':'synthetic'}
    scheduler.control.set('bounded_provider:chatgpt.com',True)  # Cannot override the handed-off rejection.
    blocked=scheduler.policy('s',scheduler.snapshots['s'])
    assert blocked['state']=='disabled' and blocked['history_can_unlock'] is False
    assert blocked['calls']==0 and blocked['reason']=='operating_consent_required' and not blocked['server_output_cap']
    asyncio.run(scheduler.close());control.close()


@pytest.mark.parametrize('change',[None,'model','effort','service_tier','input','compaction_epoch'])
def test_current_cohort_recovers_after_changes_but_keeps_unknown_and_no_return(tmp_path,change):
    from cachemonitor.cache_policy import Gap,decide
    path=tmp_path/'index.sqlite';control=Control(control_path(path));rows=[]
    start=time.time()-18000
    def append(i):
        row=dict(profile(),ts=start+i*2100,key='cold'+str(i),turn=str(i),cached=0,compaction_epoch=0)
        if i>=2 and change:
            row[change]={'model':'gpt-6-sol','effort':'high','service_tier':'Fast','input':90000,'compaction_epoch':1}[change]
        row['cached']=0
        warm=dict(row,ts=row['ts']+1,key='warm'+str(i),cached=row['input']-1000)
        rows.extend((row,warm))
        control.activity('home',dict(session_id='s',turn_id=str(i),hook_event_name='UserPromptSubmit',model=row['model']))
        control.db.execute('UPDATE cache_inputs SET at=? WHERE turn=?',(row['ts']-1,str(i)))
    def evaluate(now):
        enrich([dict(id='s',home='home',history=rows)],path,now)
        scheduler=Scheduler('home',control_path(path));scheduler.control.set('automatic',True)
        row=rows[-1];request=dict(body(),model=row['model'],reasoning={'effort':row['effort']},service_tier=row['service_tier'],prompt_cache_key='s')
        result=dict(response(),id=row['key']);scheduler.executor.contexts.completed(request,result)
        scheduler.snapshot(request,result,time.monotonic(),'https://api.openai.com/v1/responses',{},False)
        scheduler.snapshots['s']['row']=row
        decision=scheduler.policy('s',scheduler.snapshots['s']);asyncio.run(scheduler.close())
        return decision
    for i in range(3):append(i)
    first=evaluate(rows[-1]['ts']+10)
    if change:assert first['reason']=='need_independent_natural_return_history'
    for i in range(3,8):append(i)
    assert evaluate(rows[-1]['ts']+10)['state']=='eligible'
    gaps=[Gap(**json.loads(r[0])) for r in control.db.execute('SELECT data FROM cache_gaps ORDER BY turn')]
    if change:
        assert gaps[1].benefit_lower==0 and gaps[1].maintenance_upper>0
        assert gaps[1].scope!=gaps[-1].scope
    # No read is an observed zero-benefit interval, not missing data.
    rows[7]['cached']=0
    assert evaluate(rows[-1]['ts']+10)['reason']!='natural_cost_bounds_unobserved'
    zero=json.loads(control.db.execute("SELECT data FROM cache_gaps WHERE turn='3'").fetchone()[0])
    assert zero['benefit_lower']==0 and zero['maintenance_upper']>0
    # Missing return usage within this regime still vetoes a profit claim.
    rows[10]['output']=None
    assert evaluate(rows[-1]['ts']+10)['reason']=='natural_cost_bounds_unobserved'
    rows[10]['output']=4
    old_scope=json.loads(control.db.execute("SELECT data FROM cache_gaps WHERE turn='5'").fetchone()[0])['scope']
    saved=dict(rows[10]);rows[10].update(input=1,input_conflict=True)
    assert evaluate(rows[-1]['ts']+10)['reason']=='natural_cost_bounds_unobserved'
    assert json.loads(control.db.execute("SELECT data FROM cache_gaps WHERE turn='5'").fetchone()[0])['scope']==old_scope
    rows[10]=saved
    # Censored no-return cost survives selection, including unknown cost.
    evaluate(rows[-1]['ts']+20000)
    gap=Gap(**json.loads(control.db.execute("SELECT data FROM cache_gaps WHERE turn='7'").fetchone()[0]))
    assert not gap.returned and not gap.settled and gap.seconds==20000 and gap.maintenance_upper>0
    assert decide([Gap(1,2100,True,1,.1,'submission'),gap],latency_bound=30,scheduler_slack=1,max_calls=1)['state']=='off'
    rows[-1]['output']=None
    assert evaluate(rows[-1]['ts']+20000)['reason'] in ('output_budget_unobserved','natural_cost_bounds_unobserved')
    # A submission with no token record must not vanish from the cohort.
    rows[-1]['output']=4
    control.db.execute('INSERT INTO cache_inputs VALUES(?,?,?,?,?,?)',('home','s','missing',rows[-1]['ts']+10,'gpt-6-luna','UserPromptSubmit'))
    assert evaluate(rows[-1]['ts']+20000)['reason']=='natural_cost_bounds_unobserved'
    control.close()


@pytest.mark.parametrize('mode',['disable','return_disable','close','lost','timeout'])
def test_sent_usage_drains_through_repeated_invalidation_and_shutdown(tmp_path,mode):
    from aiohttp import web,ClientSession
    from test_model_proxy import server,run_proxy_test
    async def scenario():
        entered=asyncio.Event();release=asyncio.Event();seen=[]
        async def endpoint(req):
            value=await req.json();seen.append(value)
            if 'max_output_tokens' in value:
                entered.set();await release.wait()
                if mode=='lost':req.transport.close();return web.Response()
                if mode=='timeout':await asyncio.sleep(.3)
            return web.Response(text='data: '+json.dumps(dict(type='response.completed',response=response()))+'\n\n',content_type='text/event-stream')
        app=web.Application();app.router.add_post('/responses',endpoint)
        path=control_path(tmp_path/'index.sqlite');scheduler=Scheduler('home',path);scheduler.control.set('automatic',True)
        async with server(app) as upstream:
            scheduler.executor.contexts.completed(body(),response())
            scheduler.snapshot(body(),response(),time.monotonic(),upstream+'/responses',{},False)
            scheduler.control.activity('home',dict(session_id='session',turn_id='t',hook_event_name='Stop'))
            scheduler.policy=lambda *a:dict(state='eligible',calls=2,interval=0,latency_bound=.2 if mode=='timeout' else 2,output_cap=4,maintenance_upper=1)
            await scheduler.tick();await asyncio.wait_for(entered.wait(),2)
            job=scheduler.jobs['session'];closing=None
            if mode=='close':closing=asyncio.create_task(scheduler.close());await asyncio.sleep(0)
            else:
                if mode=='return_disable':
                    scheduler.executor.ingress();scheduler.executor.leave()
                    scheduler.control.activity('home',dict(session_id='session',turn_id='return',hook_event_name='UserPromptSubmit'))
                    # A natural request finishes while independent maintenance is held.
                    async with ClientSession() as client:
                        async with client.post(upstream+'/responses',json=body()) as reply:assert reply.status==200
                scheduler.control.set('automatic',False)
            for _ in range(4):
                await scheduler.tick();job.cancel();await asyncio.sleep(.01)
            assert not job.done()
            release.set();await asyncio.wait_for(job,2)
            if closing:await closing
            else:await scheduler.close()
            journal=Journal(path);rows=journal.rows();assert len(rows)==1
            missing=mode in ('lost','timeout')
            assert rows[0]['state']==('unknown' if missing else 'completed')
            assert rows[0]['usage_known']==(not missing)
            assert rows[0]['scope_read_lower']==(None if missing else 80)
            assert sum('max_output_tokens' in b for b in seen)==1
            journal.close()
            views=[];summary=enrich(views,tmp_path/'index.sqlite',time.time())
            assert summary['calls']==1 and summary['priced']==(not missing)
    run_proxy_test(scenario())


def test_scheduler_rounds_ingress_suspend_and_usage_pipeline(tmp_path):
    async def scenario():
        path=tmp_path/'index.sqlite';captured=[]
        async def send(url,headers,value,**options):
            assert options['permit']()
            captured.append(value)
            return {**response(),'id':'maintenance-'+str(len(captured))}
        scheduler=Scheduler('home',control_path(path),send=send)
        scheduler.control.set('automatic',True)
        scheduler.executor.contexts.completed(body(),response())
        scheduler.snapshot(body(),response(),time.monotonic(),'http://mock/responses',{'Content-Length':'9999'},False)
        snap=scheduler.snapshots['session']
        scheduler.control.activity('home',dict(session_id='session',turn_id='t',hook_event_name='Stop'))
        # Fast test clock interval; the real policy's TTL timing is tested separately.
        decision=dict(state='eligible',calls=2,interval=0,latency_bound=30,output_cap=4,maintenance_upper=1)
        scheduler.policy=lambda *a:decision
        await scheduler.tick();await scheduler.jobs['session']
        assert len(captured)==2 and len(scheduler.journal.rows())==2
        assert all(v['max_output_tokens']==4 and v['tool_choice']=='none' for v in captured)
        assert captured[0]['input']==captured[1]['input']
        # New snapshot, scheduled then user ingress before body delivery.
        original={**response(),'id':'new'};scheduler.executor.contexts.completed(body(),original)
        scheduler.snapshot(body(),original,time.monotonic(),'http://mock/responses',{},False)
        scheduler.control.activity('home',dict(session_id='session',turn_id='new',hook_event_name='Stop'))
        await scheduler.tick()
        scheduler.capture.executor.ingress();scheduler.capture.executor.leave()
        scheduler.control.activity('home',dict(session_id='session',turn_id='return',hook_event_name='UserPromptSubmit'))
        await scheduler.tick();await asyncio.gather(*scheduler.jobs.values(),return_exceptions=True)
        assert len(captured)==2
        scheduler.last_tick-=10;await scheduler.tick();assert not scheduler.snapshots
        user=Session('session','home',title='Original')
        user.add_usage(time.time()-100,'original',response()['usage'],'gpt-6-luna','t','low','default')
        views=[user.view(time.time())];data=enrich(views,path,time.time())
        assert data['calls']==2 and data['priced']==2 and data['unknown']==0
        engine=AnalysisEngine();engine.ingest(views)
        rows=[r for s in engine.sessions.values() for r in s['prepared']['history']]
        assert len(rows)==3 and sum(r.get('purpose')=='maintenance' for r in rows)==2
        summaries=OverlaySummaries().collect(engine)
        parent=next(s for s in summaries if s['id']=='session')
        assert parent['calls']==3 and parent['maintenance_calls']==2
        ledger=QuotaLedger(tmp_path/'ledger.sqlite')
        snapshot=dict(homes=['home'],sessions=views,ts=time.time(),usage_collection_complete=True,
                      request_activity=data['request_activity'],index=dict(loading=False,usage_complete=True,last_usage_success=time.time()))
        ledger.sync(engine,snapshot);ledger.sync(engine,snapshot)
        assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0]==3
        assert ledger.db.execute('SELECT sum(cost) FROM calls').fetchone()[0]==pytest.approx(sum(r['cost'] for r in rows))
        user.add_usage(time.time()+1,'return',response()['usage'],'gpt-6-luna','return','low','default')
        effects=enrich([user.view(time.time()+2)],path,time.time()+2)['effects']
        assert effects[0]['user_response']=='return' and effects[0]['user_read']==80
        assert effects[0]['causal_saving'] is None
        ledger.close();await scheduler.close()
    asyncio.run(scenario())


def test_unknown_usage_survives_index_and_policy(tmp_path):
    path=tmp_path/'index.sqlite';journal=Journal(control_path(path))
    key=journal.reserve('h','s',0,body(),'lost');journal.sent(key);journal.recover_exclusive()
    views=[];summary=enrich(views,path,time.time());journal.close()
    assert summary['unknown']==1 and summary['priced']==0
    assert views[0]['history'][0]['input'] is None
    engine=AnalysisEngine();engine.ingest(views)
    assert next(iter(engine.sessions.values()))['prepared']['history'][0]['cost'] is None
    assert cost_bounds(profile(),dict(profile(),cached=0),4) is not None
    other=[];summary=enrich(other,path,time.time(),{'other-home'})
    assert not other and summary['calls']==0


@pytest.mark.parametrize('outcome',['ok','401','429','lost','overcap','zero','incomplete'])
def test_relay_to_scheduler_to_transport_failure_and_stop_contract(tmp_path,outcome):
    from aiohttp import web,ClientSession
    from cachemonitor.model_proxy import create_app
    from cachemonitor.model_evidence import EvidenceStore
    from test_model_proxy import server,run_proxy_test
    async def scenario():
        requests=[]
        async def endpoint(req):
            value=await req.json();requests.append(value)
            r={**response(),'id':'r'+str(len(requests))}
            if 'max_output_tokens' in value:
                if outcome in ('401','429'):return web.Response(status=int(outcome))
                if outcome=='lost':req.transport.close();return web.Response()
                if outcome=='overcap':r['usage']['output_tokens']=20
                if outcome=='zero':r['usage']['input_tokens_details']['cached_tokens']=0
                if outcome=='incomplete':r.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
            return web.Response(text='data: '+json.dumps({'type':'response.'+r['status'],'response':r})+'\n\n',content_type='text/event-stream')
        scheduler=Scheduler('home',tmp_path/'control.sqlite');scheduler.control.set('automatic',True)
        store=EvidenceStore(tmp_path/'e.sqlite');up=web.Application();up.router.add_post('/responses',endpoint)
        try:
            async with server(up) as upstream,server(create_app(store,tmp_path,upstream,cache_capture=scheduler.capture)) as proxy,ClientSession() as client:
                async with client.post(proxy+'/responses',json=body(),headers={'Authorization':'Bearer synthetic','session_id':'session'}) as reply:
                    assert reply.status==200;await reply.read()
                await asyncio.sleep(.02)
                snap=scheduler.snapshots['session'];assert any(k.lower()=='content-length' for k in snap['headers'])
                scheduler.control.activity('home',dict(session_id='session',turn_id='t',hook_event_name='Stop'))
                scheduler.policy=lambda *a:dict(state='eligible',calls=2,interval=0,latency_bound=2,output_cap=4,maintenance_upper=1)
                await scheduler.tick();await scheduler.jobs['session'];await scheduler.tick()
                assert len(requests)==(3 if outcome=='ok' else 2)
                assert requests[1]['tools']==requests[0]['tools'] and requests[1]['tool_choice']=='none'
                assert requests[1]['reasoning']==requests[0]['reasoning']
                rows=scheduler.journal.rows()
                if outcome in ('401','429','lost'):assert rows[0]['input'] is None and rows[0]['state']=='unknown'
                elif outcome=='overcap':assert rows[0]['output']==20
                elif outcome=='zero':assert rows[0]['scope_read_lower']==0
                elif outcome=='incomplete':assert rows[0]['state']=='unknown' and rows[0]['usage_known'] and rows[0]['cost'] is not None
        finally:await scheduler.close();store.close()
    run_proxy_test(scenario())
