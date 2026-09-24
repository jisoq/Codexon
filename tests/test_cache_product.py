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
    row=profile();request={**body(),'prompt_cache_key':'s'};result=response()
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
    asyncio.run(scheduler.close());control.close()


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


@pytest.mark.parametrize('outcome',['ok','401','429','lost','overcap','zero'])
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
            return web.Response(text='data: '+json.dumps({'type':'response.completed','response':r})+'\n\n',content_type='text/event-stream')
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
        finally:await scheduler.close();store.close()
    run_proxy_test(scenario())
