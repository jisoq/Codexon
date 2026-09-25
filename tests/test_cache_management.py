import asyncio
import copy
import json
import time

from cachemonitor.cache_audit import observations
from cachemonitor.cache_execution import Contexts, Executor, Journal, request_once
from cachemonitor.cache_policy import Gap, decide
from cachemonitor.cache_health import CacheHealth


def body():
    return dict(model='gpt-6-luna',input=[{'role':'user','content':'ORIGINAL'}],
                instructions='stable',tools=[{'type':'function','name':'f','parameters':{'type':'object'}}],
                reasoning={'effort':'low'},service_tier='default',prompt_cache_key='session')


def response():
    return dict(id='original',status='completed',output=[{'type':'message','role':'assistant','content':[]}],
                usage={'input_tokens':100,'input_tokens_details':{'cached_tokens':80,'cache_write_tokens':10},
                       'output_tokens':4,'output_tokens_details':{'reasoning_tokens':2}})


def test_context_chain_and_tool_definitions_unchanged():
    contexts=Contexts(); source=body(); before=copy.deepcopy(source)
    contexts.completed(source,response())
    delta={**source,'previous_response_id':'original','input':[{'role':'user','content':'NEXT'}]}
    full=contexts.full(delta)
    assert full['input']==source['input']+response()['output']+delta['input']
    maintenance=contexts.maintenance('original')
    assert maintenance['tools']==source['tools'] and maintenance['tool_choice']=='none'
    assert maintenance['reasoning']==source['reasoning'] and source==before
    assert 'ACK' not in json.dumps(contexts.full(delta))


def test_policy_bootstrap_holdout_and_no_return_cost():
    kwargs=dict(latency_bound=30,scheduler_slack=2,max_calls=3)
    favorable=[Gap(i,1900,True,1,.1,'human') for i in range(6)]
    assert decide(favorable,**kwargs)['state']=='eligible'
    assert decide([*favorable[:3],*[Gap(i,9000,False,0,.1,'human') for i in range(3,6)]],**kwargs)['state']=='off'
    assert decide([Gap(i,1900,True,1,.1) for i in range(6)],**kwargs)['state']=='waiting'
    assert decide([Gap(i,1900,True,.01,1,'human') for i in range(6)],**kwargs)['state']=='off'


def test_executor_user_priority_dedup_usage_and_restart(tmp_path):
    async def scenario():
        journal=Journal(tmp_path/'cache.sqlite');contexts=Contexts();contexts.completed(body(),response())
        entered=asyncio.Event();release=asyncio.Event();sent=[]
        async def send(url,headers,value,**kwargs):
            sent.append(value);entered.set();await release.wait();return response()
        runner=Executor(journal,contexts,send)
        options=dict(anchor=time.monotonic()-1700,deadline=time.monotonic(),latency_bound=30)
        task=asyncio.create_task(runner.run('home','s','original','http://unused',{},**options))
        await entered.wait()
        assert await runner.run('home','s','original','http://unused',{},**options)=='duplicate'
        runner.ingress();assert not task.done();runner.leave()
        release.set();assert await task=='completed'
        assert await runner.run('home','s','original','http://unused',{},**options)=='duplicate'
        rows=journal.rows();assert len(rows)==1 and rows[0]['purpose']=='maintenance'
        assert rows[0]['input']==100 and rows[0]['cost'] is not None
        assert rows[0]['reasoning']==2
        runner.close()
        assert await runner.run('home','s','original','http://unused',{},**options)=='invalidated'
        journal.close()
    asyncio.run(scenario())


def test_real_http_and_websocket_side_transport(tmp_path):
    from aiohttp import web
    from test_model_proxy import server,run_proxy_test
    async def scenario():
        received=[]
        async def endpoint(request):
            if request.headers.get('Upgrade','').lower()=='websocket':
                ws=web.WebSocketResponse();await ws.prepare(request)
                message=await ws.receive_json();received.append(message)
                await ws.send_json({'type':'response.completed','response':response()})
                await ws.close();return ws
            received.append(await request.json())
            return web.Response(text='data: '+json.dumps({'type':'response.completed','response':response()})+'\n\n',content_type='text/event-stream')
        app=web.Application();app.router.add_route('*','/responses',endpoint)
        contexts=Contexts();contexts.completed(body(),response())
        async with server(app) as url:
            for ws in (False,True):
                result=await request_once(url+'/responses',{},contexts.maintenance('original'),websocket=ws)
                assert result['usage']['input_tokens']==100
        assert len(received)==2
        assert all(r['tools']==body()['tools'] and r['tool_choice']=='none' for r in received)
    run_proxy_test(scenario())


def test_proxy_capture_preserves_user_payload_and_anchor(tmp_path):
    from aiohttp import web,ClientSession
    from cachemonitor.cache_capture import RelayCapture
    from cachemonitor.model_proxy import create_app
    from cachemonitor.model_evidence import EvidenceStore
    from test_model_proxy import server,run_proxy_test
    async def scenario():
        journal=Journal(tmp_path/'jobs.sqlite');store=EvidenceStore(tmp_path/'evidence.sqlite')
        snapshots=[];contexts=Contexts();runner=Executor(journal,contexts)
        adapter=RelayCapture(runner,lambda *args:snapshots.append(args))
        seen=[]
        async def endpoint(request):
            seen.append(await request.json())
            return web.Response(text='data: '+json.dumps({'type':'response.completed','response':response()})+'\n\n',content_type='text/event-stream')
        app=web.Application();app.router.add_post('/responses',endpoint)
        try:
            async with server(app) as url,server(create_app(store,tmp_path,url,cache_capture=adapter)) as proxy,ClientSession() as client:
                before=time.monotonic()
                async with client.post(proxy+'/responses',json=body()) as reply:
                    assert reply.status==200;await reply.read()
                await asyncio.sleep(.05)
                snapshot=snapshots[-1]
                for round_number in (0,1):
                    assert await runner.run('h','s','original',snapshot[3],snapshot[4],anchor=snapshot[2],
                        deadline=time.monotonic(),latency_bound=30,round_number=round_number)=='completed'
                assert await runner.run('h','s','original',snapshot[3],snapshot[4],anchor=snapshot[2],
                    deadline=time.monotonic(),latency_bound=30,round_number=1)=='duplicate'
            assert seen[0]==body() and len(seen)==3 and len(snapshots)==1 and runner.busy==0
            assert all(b['input'][:1]==body()['input'] and b['tool_choice']=='none' for b in seen[1:])
            assert len(journal.rows())==2
            assert before<=snapshots[0][2]<=time.monotonic()
            assert contexts.maintenance('original')['tools']==body()['tools']
        finally:journal.close();store.close()
    run_proxy_test(scenario())


def test_websocket_overlap_cancel_and_disconnect_never_mispair(tmp_path):
    from aiohttp import web,ClientSession
    from cachemonitor.cache_capture import RelayCapture
    from cachemonitor.model_proxy import create_app
    from cachemonitor.model_evidence import EvidenceStore
    from test_model_proxy import server,run_proxy_test
    async def scenario(mode):
        journal=Journal(tmp_path/(mode+'jobs.sqlite'));store=EvidenceStore(tmp_path/(mode+'wire.sqlite'))
        snapshots=[];runner=Executor(journal,Contexts());received=[]
        async def endpoint(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            a=await ws.receive_json();received.append(a)
            await ws.send_json(dict(type='response.created',response=dict(id='A')))
            b=await ws.receive_json();received.append(b)
            if mode=='close':await ws.close();return ws
            await ws.send_json(dict(type='response.completed',response={**response(),'id':'A'}))
            if mode=='overlap':
                await ws.send_json(dict(type='response.created',response=dict(id='B')))
                await ws.send_json(dict(type='response.completed',response={**response(),'id':'B'}))
            await ws.close();return ws
        app=web.Application();app.router.add_get('/responses',endpoint)
        try:
            async with server(app) as upstream,server(create_app(store,tmp_path,upstream,
                    cache_capture=RelayCapture(runner,lambda *args:snapshots.append(args)))) as proxy,ClientSession() as client:
                async with client.ws_connect(proxy+'/responses') as ws:
                    await ws.send_json({**body(),'type':'response.create','input':[{'role':'user','content':'A'}]})
                    await ws.receive_json()
                    await ws.send_json({'type':'response.cancel','response_id':'A'} if mode=='cancel' else
                        {**body(),'type':'response.create','input':[{'role':'user','content':'B'}]})
                    async for _ in ws:pass
                await asyncio.sleep(.02)
            assert len(received)==2 and runner.busy==0
            if mode=='overlap':
                assert [(s[0]['input'][0]['content'],s[1]['id']) for s in snapshots]==[('A','A'),('B','B')]
            else:assert snapshots==[]
        finally:journal.close();store.close()
    for mode in ('overlap','cancel','close'):run_proxy_test(scenario(mode))


def test_response_loss_is_unknown_and_durable(tmp_path):
    async def scenario():
        journal=Journal(tmp_path/'cache.sqlite');contexts=Contexts();contexts.completed(body(),response())
        async def lost(*args,**kwargs):raise ConnectionError()
        runner=Executor(journal,contexts,lost)
        assert await runner.run('h','s','original','http://unused',{},anchor=time.monotonic()-1700,
                                deadline=time.monotonic(),latency_bound=30)=='unknown'
        assert journal.rows()[0]['cost'] is None and journal.rows()[0]['input'] is None
        journal.close();journal=Journal(tmp_path/'cache.sqlite')
        journal.recover_exclusive();assert journal.rows()[0]['state']=='unknown';journal.close()
    asyncio.run(scenario())


def test_audit_does_not_invent_write_or_cause():
    rows=[dict(key=str(i),ts=i,input=1000,cached=900,model='gpt-6-luna',effort='low',service_tier='Standard') for i in range(4)]
    rows.append(dict(rows[-1],key='grown',ts=5,input=9000))
    assert CacheHealth().update(rows)['incident'] is None
    last=dict(rows[-1],key='changed',ts=2000,model='gpt-6-sol',effort='high',cached=0)
    audit=observations(rows+[last])[-1]
    assert set(audit['changes'])=={'model_changed','effort_changed','idle_over_design_lifetime'}
    assert audit['written'] is None and audit['avoidable_cost'] is None


def test_compaction_and_context_shrink_reset_baseline():
    rows=[dict(key=str(i),ts=i,input=1000,cached=900,model='m',effort='low',service_tier='Standard',compaction_epoch=0) for i in range(5)]
    compacted=[dict(rows[-1],key=str(i),ts=i,input=1000,cached=0,compaction_epoch=1) for i in (5,6)]
    assert CacheHealth().update(rows+compacted)['incident'] is None
    shrunk=[dict(rows[-1],key=str(i),ts=i,input=100,cached=0) for i in (5,6)]
    assert CacheHealth().update(rows+shrunk)['incident'] is None


def test_censored_intervals_do_not_supply_fake_benefit():
    natural=[Gap(i,1900,True,1,.1,'human') for i in range(3)]
    censored=[Gap(i,9000,True,100,.1,'human',False) for i in range(3,6)]
    assert decide(natural+censored,latency_bound=30,scheduler_slack=2,max_calls=3)['state']=='off'
