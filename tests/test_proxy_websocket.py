import asyncio
from aiohttp import ClientSession, TCPConnector, web
from cachemonitor.model_evidence import EvidenceStore
from cachemonitor.model_proxy import create_app
from test_model_proxy import server, run_proxy_test


def test_upstream_cleanup_failure_does_not_leave_phantom_connections(tmp_path,monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import cachemonitor.model_proxy as module
    class Response:
        status_code=200;reason_phrase='OK';http_version='HTTP/1.1'
        headers=SimpleNamespace(get=lambda *a:'',raw=[])
        async def aiter_raw(self):yield b'complete'
        async def aclose(self):raise RuntimeError('synthetic close failure')
    class Pool:
        def __init__(self,*a):pass
        @asynccontextmanager
        async def lease(self):
            async def send(*a,**kw):return Response()
            yield SimpleNamespace(client=SimpleNamespace(send=send),complete=False)
            raise RuntimeError('synthetic release failure')
        async def close(self):pass
    monkeypatch.setattr(module,'HTTPRelayPool',Pool)
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite')
        try:
            async with server(create_app(store,tmp_path,'http://127.0.0.1:1')) as proxy,ClientSession() as client:
                assert await (await client.get(proxy+'/models')).read()==b'complete'
                health=await (await client.get(proxy+'/health')).json()
                assert health['active_connections']==0
                assert health['recent_relays'][-1]['cleanup_error_type']=='RuntimeError'
        finally:store.close()
    run_proxy_test(run())


def test_native_ping_pong_close_and_no_unsolicited_requests(tmp_path):
    async def run():
        upstream_ready=asyncio.Event();sockets=[]
        async def upstream(request):
            ws=web.WebSocketResponse(autoping=False);await ws.prepare(request)
            sockets.append(ws);upstream_ready.set()
            await ws.ping(b'server-ping')
            pong=await ws.receive()
            assert pong.type==web.WSMsgType.PONG and pong.data==b'server-ping'
            ping=await ws.receive()
            assert ping.type==web.WSMsgType.PING and ping.data==b'client-ping'
            await ws.pong(ping.data)
            await ws.close(code=1001,message=b'Native lifetime ended')
            return ws
        app=web.Application();app.router.add_get('/responses',upstream)
        store=EvidenceStore(tmp_path/'e.sqlite')
        try:
            async with server(app) as url,server(create_app(store,tmp_path,url)) as proxy,ClientSession() as client:
                async with client.ws_connect(proxy+'/responses',autoping=False) as ws:
                    msg=await asyncio.wait_for(ws.receive(),3)
                    assert msg.type==web.WSMsgType.PING and msg.data==b'server-ping'
                    await ws.pong(msg.data);await ws.ping(b'client-ping')
                    msg=await ws.receive();assert msg.type==web.WSMsgType.PONG and msg.data==b'client-ping'
                    assert (await ws.receive()).type==web.WSMsgType.CLOSE
                    assert ws.close_code==1001
                for _ in range(50):
                    health=await (await client.get(proxy+'/health')).json()
                    if health['active_connections']==0:break
                    await asyncio.sleep(.01)
                assert health['active_connections']==0 and health['requests']==0
                assert health['websocket_policy']['budget']==100
        finally:store.close()
    run_proxy_test(run())


def test_policy_clock_and_request_accounting():
    from cachemonitor.proxy_websocket import Activity,WebSocketBudget,POLICY
    async def run():
        now=[0.];closed=[]
        budget=WebSocketBudget(clock=lambda:now[0])
        async def add(key):
            a=Activity(lambda:now[0]);await budget.reserve(key,a)
            async def close(reason):closed.append((key,reason))
            budget.connected(key,close);return a
        a=await add('idle')
        now[0]=299;budget.reap();assert not a.closing
        # Control notifications must not extend idle time.
        for event in ('rate_limits.updated', 'codex.rate_limits', 'codex.response.metadata', 'responsesapi.websocket_timing'):
            msg={'type':event};a.begin(msg,False);a.delivered(msg,False)
        assert not a.unknown and a.idle_since==0
        now[0]=300;budget.reap();assert a.closing
        await budget.close();assert closed==[('idle','idle_expired')]
        assert not a.begin({'type':'response.create'},True)
        assert budget.status()['occupied']==1  # close selection is not release
        budget.release('idle');budget.release('idle');assert not budget.entries
        a=await add('lanes')
        for lane in ('a','b','b'):
            msg={'type':'response.create','stream_id':lane};a.begin(msg,True);a.delivered(msg,True)
        now[0]+=3600;budget.reap();assert not a.closing
        cancel={'type':'response.cancel','stream_id':'a'};a.begin(cancel,True);a.delivered(cancel,True)
        error={'type':'error','stream_id':'a'};a.begin(error,False);a.delivered(error,False)
        assert len(a.pending['a'])==1
        assert a.busy
        for rid,lane in [('1','a'),('2','b'),('2','b')]:
            msg={'type':'response.completed','stream_id':lane,'response':{'id':rid}}
            a.begin(msg,False);assert a.busy;a.delivered(msg,False)
        assert a.busy  # replay terminal must not consume queued lane b
        msg={'type':'response.completed','stream_id':'b','response':{'id':'3'}}
        a.begin(msg,False);a.delivered(msg,False);budget.reap()
        assert a.closing;await budget.close();assert closed[-1]==('lanes','lifetime')
        a=await add('unknown')
        msg={'type':'future.request'};a.begin(msg,True);a.delivered(msg,True)
        now[0]+=3600;budget.reap()
        assert a.unknown and not a.closing
        assert budget.status()['unknown_events']=={'client:future.request':1}
        assert vars(POLICY)==dict(budget=100,idle_limit=32,idle_seconds=300,lifetime_seconds=1800,
            pressure_start=90,pressure_target=80,check_seconds=1,close_seconds=5,capacity_seconds=5,waiters=16)
    run_proxy_test(run())


def test_idle_limit_pressure_and_all_busy_capacity():
    from dataclasses import replace
    from cachemonitor.proxy_websocket import Activity,WebSocketBudget,POLICY,LocalCapacity
    async def run():
        budget=WebSocketBudget(replace(POLICY,capacity_seconds=.03));closed=[]
        async def add(key,busy=False):
            a=Activity();await budget.reserve(key,a)
            if busy:
                msg={'type':'response.create'};a.begin(msg,True);a.delivered(msg,True)
            async def close(reason):closed.append((key,reason))
            budget.connected(key,close);return a
        for i in range(33):await add(i)
        await budget.close();assert closed==[(0,'idle_limit')]
        budget.release(0)
        # Fill to 90, preserving busy work while collecting 10 oldest idle slots.
        for i in range(33,91):await add(i,True)
        await budget.close()
        assert len(closed)==11
        assert all(reason=='capacity' for _,reason in closed[1:])
        for key,_ in closed[1:]:budget.release(key)
        assert len(budget.entries)==80
        for key in list(budget.entries):budget.release(key)
        for i in range(100):await add(i,True)
        try:await budget.reserve(101,Activity());assert False,'capacity must be bounded'
        except LocalCapacity:pass
        assert len(budget.entries)==100 and budget.waiting==0
        assert all(not a.closing for a,_ in budget.entries.values())
    run_proxy_test(run())


def test_accumulated_connections_retire_and_reconnect_without_pool_wait(tmp_path):
    async def run():
        async def upstream(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            async for msg in ws:
                await ws.send_json({'type':'response.completed','response':{'id':'answer'}})
            return ws
        app=web.Application();app.router.add_get('/responses',upstream)
        store=EvidenceStore(tmp_path/'e.sqlite');sockets=[]
        try:
            async with server(app) as url,server(create_app(store,tmp_path,url)) as proxy,ClientSession(connector=TCPConnector(limit=100)) as client:
                try:
                    for _ in range(140):
                        ws=await asyncio.wait_for(client.ws_connect(proxy+'/responses'),2)
                        sockets.append(ws)
                        # aiohttp clients, like native clients, must read CLOSE to acknowledge it.
                        if len(sockets)>32:await sockets[-33].receive()
                    await sockets[-1].send_json({'type':'response.create'})
                    assert (await sockets[-1].receive_json())['type']=='response.completed'
                    health=await (await client.get(proxy+'/health')).json()
                    assert health['websocket_connections']['occupied']<=33
                    assert health['websocket_connections']['retired']['idle_limit']>=108
                    assert health['relay_errors']==0
                    assert health['selector_sockets']<512
                finally:
                    await asyncio.gather(*(ws.close() for ws in sockets))
                for _ in range(100):
                    health=await (await client.get(proxy+'/health')).json()
                    if health['active_connections']==0:break
                    await asyncio.sleep(.01)
                assert health['active_connections']==0
                assert health['websocket_connections']['occupied']==0
        finally:store.close()
    run_proxy_test(run())


def test_100_busy_connections_have_bounded_local_failure_and_http_is_independent(tmp_path):
    import time
    from aiohttp import WSServerHandshakeError
    async def run():
        async def upstream(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            async for message in ws:
                await ws.send_json({'type':'response.created','response':{'id':'pending'}})
            return ws
        app=web.Application();app.router.add_get('/responses',upstream)
        async def models(request):return web.Response(text='http-still-works')
        app.router.add_get('/models',models)
        store=EvidenceStore(tmp_path/'e.sqlite');sockets=[]
        try:
            async with server(app) as url,server(create_app(store,tmp_path,url)) as proxy,ClientSession(connector=TCPConnector(limit=0)) as client:
                try:
                    for _ in range(100):
                        ws=await client.ws_connect(proxy+'/responses');sockets.append(ws)
                        await ws.send_json({'type':'response.create'})
                        assert (await ws.receive_json())['type']=='response.created'
                    started=time.monotonic()
                    try:await client.ws_connect(proxy+'/responses');assert False
                    except WSServerHandshakeError as error:assert error.status==503
                    assert 4.5<=time.monotonic()-started<8
                    assert await (await client.get(proxy+'/models')).text()=='http-still-works'
                    health=await (await client.get(proxy+'/health')).json()
                    assert health['websocket_states']['responding']==100
                    assert health['websocket_connections']['occupied']==100
                    assert health['websocket_connections']['capacity_waiting']==0
                    assert health['relay_errors']==0 and health['selector_sockets']<512
                finally:await asyncio.gather(*(ws.close() for ws in sockets))
        finally:store.close()
    run_proxy_test(run())
