import asyncio
import gzip
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientSession, web
from multidict import CIMultiDict
from yarl import URL

from cachemonitor.model_evidence import EvidenceStore, EvidenceReader
from cachemonitor.model_proxy import create_app, proxy_loop


def run_proxy_test(coro):
    with asyncio.Runner(loop_factory=proxy_loop) as runner:return runner.run(coro)


def test_managed_shutdown_finishes_inflight_websocket_without_replay(tmp_path):
    async def run():
        token='own-worker';control=tmp_path/('proxy-control-'+token+'.json')
        control.write_text(json.dumps({'id':token,'action':'run'}))
        stop=asyncio.Event();finish=asyncio.Event();received=[]
        async def upstream(request):
            ws=web.WebSocketResponse();await ws.prepare(request)
            message=await ws.receive_json();received.append(message)
            await ws.send_json({'type':'response.created','response':{'id':'one','model':'m'}})
            await finish.wait()
            await ws.send_json({'type':'response.completed','response':{'id':'one','model':'m','output':'KEEP_FINAL'}})
            async for _ in ws:pass
            return ws
        app=web.Application();app.router.add_get('/responses',upstream)
        store=EvidenceStore(tmp_path/'e.sqlite')
        try:
            async with server(app) as url, server(create_app(store,tmp_path/'home',url,
                    control_file=control,control_id=token,stop_event=stop)) as proxy, ClientSession() as client:
                async with client.ws_connect(proxy+'/responses') as ws:
                    await ws.send_json({'type':'response.create','model':'m','input':'keep'})
                    assert (await ws.receive_json())['type']=='response.created'
                    control.write_text(json.dumps({'id':token,'action':'drain'}))
                    await asyncio.sleep(.3)
                    health=await (await client.get(proxy+'/health')).json()
                    assert health['draining'] and health['active_connections']==1
                    assert not stop.is_set() and not ws.closed
                    finish.set()
                    assert (await ws.receive_json())['response']['output']=='KEEP_FINAL'
                    await asyncio.wait_for(ws.receive(),3)
                await asyncio.wait_for(stop.wait(),3)
                assert len(received)==1
        finally:store.close()
    run_proxy_test(run())


def test_managed_shutdown_keeps_inflight_http_tool_bytes(tmp_path):
    async def run():
        token='own-worker';control=tmp_path/('proxy-control-'+token+'.json')
        stop=asyncio.Event();finish=asyncio.Event();received=[]
        async def upstream(request):
            received.append(await request.read())
            response=web.StreamResponse();await response.prepare(request)
            await response.write(b'first');await finish.wait()
            await response.write(b'last');await response.write_eof();return response
        app=web.Application();app.router.add_post('/tool',upstream)
        store=EvidenceStore(tmp_path/'e.sqlite')
        try:
            async with server(app) as url, server(create_app(store,tmp_path/'home',url,
                    control_file=control,control_id=token,stop_event=stop)) as proxy, ClientSession() as client:
                async with client.post(proxy+'/tool',data=b'original') as response:
                    assert await response.content.readexactly(5)==b'first'
                    control.write_text(json.dumps({'id':token,'action':'drain'}))
                    await asyncio.sleep(.3)
                    assert not stop.is_set()
                    rejected=await client.post(proxy+'/tool',data=b'do-not-forward')
                    assert rejected.status==503
                    finish.set();assert await response.read()==b'last'
                await asyncio.wait_for(stop.wait(),3)
                assert received==[b'original']
        finally:store.close()
    run_proxy_test(run())


@asynccontextmanager
async def server(app,ssl_context=None):
    runner=web.AppRunner(app,access_log=None)
    await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=ssl_context)
    await site.start()
    scheme='https' if ssl_context is not None else 'http'
    try:yield f'{scheme}://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    finally:await runner.cleanup()


def test_websocket_interleaved_lanes_preserves_frames_and_secrets(tmp_path):
    async def run():
        home=tmp_path/'home';path=tmp_path/'e.sqlite';store=EvidenceStore(path)
        received=[]
        async def upstream(request):
            assert request.headers['Authorization']=='Bearer PRIVATE'
            ws=web.WebSocketResponse();await ws.prepare(request)
            received.extend([await ws.receive_str(),await ws.receive_str()])
            for lane,model in [('b','different'),('a','model-a')]:
                for typ in ('created','completed'):
                    await ws.send_json({'type':'response.'+typ,'stream_id':lane,
                                        'response':{'id':'r-'+lane,'model':model,'output':[{'text':'PRIVATE_OUTPUT'}]}})
            await ws.close();return ws
        upstream_app=web.Application();upstream_app.router.add_get('/responses',upstream)
        try:
            async with server(upstream_app) as url, server(create_app(store,home,url)) as proxy, ClientSession() as client:
                messages=[json.dumps({'type':'response.create','stream_id':x,'model':'model-'+x,'input':'PRIVATE_PROMPT'}) for x in ('a','b')]
                async with client.ws_connect(proxy+'/responses',headers={'Authorization':'Bearer PRIVATE'}) as ws:
                    for message in messages:await ws.send_str(message)
                    replies=[]
                    try:
                        for _ in range(4): replies.append(await ws.receive_json())
                    except Exception as exc:
                        details=await (await client.get(proxy+'/health')).json()
                        raise AssertionError(f'WebSocket relay diagnostics: {details}; frames={len(replies)}; close={ws.close_code}; error={ws.exception()!r}') from exc
                assert received==messages
                assert replies[0]['response']['output'][0]['text']=='PRIVATE_OUTPUT'
                reader=EvidenceReader(path);reader.poll()
                rows=reader.enrich(home,[{'key':'r-a'},{'key':'r-b'}]);reader.close()
                assert [r['model_match'] for r in rows]==['일치','불일치']
                assert (await (await client.get(proxy+'/health')).json())['requests']==2
                assert (await client.get(proxy+'/models',headers={'Origin':'https://example.com'})).status==403
                assert (await client.get(proxy+'/unrelated')).status==404
            persisted=json.dumps(store.db.execute('select * from model_observations').fetchall())
            assert 'PRIVATE' not in persisted
        finally:store.close()
    run_proxy_test(run())


def test_http_sse_chunking_json_errors_and_unparsed_body(tmp_path):
    async def run():
        home=tmp_path/'home';path=tmp_path/'e.sqlite';store=EvidenceStore(path)
        payload=b'data: '+json.dumps({'type':'response.completed','response':{'id':'sse','model':'m'}}).encode()+b'\r\n\r\n'
        async def upstream(request):
            data=await request.json()
            if data.get('case')=='error':return web.json_response({'error':{'message':'secret'}},status=429,headers={'Retry-After':'7'})
            if data.get('case')=='json':return web.json_response({'object':'response','id':'json','model':'m','status':'completed'})
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await response.prepare(request)
            for i in range(0,len(payload),3):await response.write(payload[i:i+3])
            await response.write_eof();return response
        app=web.Application();app.router.add_post('/responses',upstream)
        try:
            async with server(app) as url, server(create_app(store,home,url)) as proxy, ClientSession() as client:
                for case in ('sse','json','error'):
                    async with client.post(proxy+'/responses',json={'model':'m','case':case}) as r:
                        data=await r.read()
                        if case=='sse':assert data==payload
                        if case=='error':assert r.status==429 and r.headers['Retry-After']=='7'
            reader=EvidenceReader(path);reader.poll()
            assert all(r['model_match']=='일치' for r in reader.enrich(home,[{'key':'sse'},{'key':'json'}]))
            reader.close()
            assert store.db.execute("select count(*) from model_observations where status='http_error'").fetchone()[0]==1
        finally:store.close()
    run_proxy_test(run())


@pytest.mark.parametrize('websocket',[False,True])
def test_invalid_event_type_does_not_break_passive_relay(tmp_path,websocket):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite')
        invalid=b'{"type":true,"response":{"id":"r","model":"m"}}'
        completed=b'{"type":"response.completed","response":{"id":"r","model":"m"}}'
        payload=b'data: '+invalid+b'\n\ndata: '+completed+b'\n\n'
        async def upstream(request):
            if websocket:
                ws=web.WebSocketResponse();await ws.prepare(request)
                await ws.receive()
                await ws.send_str(invalid.decode());await ws.send_str(completed.decode())
                await ws.close();return ws
            await request.read()
            return web.Response(body=payload,content_type='text/event-stream')
        app=web.Application();app.router.add_route('*','/responses',upstream)
        try:
            async with server(app) as url,server(create_app(store,tmp_path,url)) as proxy,ClientSession() as client:
                if websocket:
                    async with client.ws_connect(proxy+'/responses') as ws:
                        await ws.send_json({'type':'response.create','model':'m'})
                        assert await ws.receive_str()==invalid.decode()
                        assert await ws.receive_str()==completed.decode()
                else:
                    async with client.post(proxy+'/responses',json={'model':'m'}) as response:
                        assert await response.read()==payload
                health=await (await client.get(proxy+'/health')).json()
                assert health['relay_errors']==0
                assert store.db.execute("select response_model from model_observations where status='completed'").fetchone()==('m',)
        finally:store.close()
    run_proxy_test(run())


def test_websocket_handshake_error_preserves_status_and_retry_after(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite')
        async def reject(request):return web.Response(status=429,headers={'Retry-After':'10'},text='private upstream details')
        app=web.Application();app.router.add_get('/responses',reject)
        try:
            async with server(app) as url, server(create_app(store,tmp_path,url)) as proxy, ClientSession() as client:
                async with client.get(proxy+'/responses',headers={'Upgrade':'websocket'}) as response:
                    assert response.status==429 and response.headers['Retry-After']=='10'
                    assert 'private' not in await response.text()
        finally:store.close()
    run_proxy_test(run())


@pytest.mark.parametrize('method', ['GET','POST','PUT','PATCH','DELETE','OPTIONS'])
def test_arbitrary_endpoints_preserve_raw_target_body_and_headers(tmp_path,method):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite')
        # Deliberately not a known API route: future tools must work without code changes.
        target='/new-tool/asset%2Fpart/%E9%9B%AA?key=a%2Bb&key=a+b&key=%2f&empty='
        payload=b'\x00\xffopaque multipart or binary upload\r\n'
        seen=[]
        async def upstream(request):
            seen.append((request.method,request.raw_path,await request.read(),request.headers.copy()))
            return web.Response(status=207,body=payload,headers=CIMultiDict([
                ('Content-Type','application/octet-stream'),('X-Repeated','first'),('X-Repeated','second'),
                ('Connection','X-Private'),('Connection','X-Also-Private'),
                ('X-Private','hop'),('X-Also-Private','hop')]))
        app=web.Application();app.router.add_route('*','/{tail:.*}',upstream)
        try:
            async with server(app) as url, server(create_app(store,tmp_path,url+'/base/v9/')) as proxy, ClientSession() as client:
                headers=CIMultiDict([('Authorization','Bearer PRIVATE'),('Content-Type','multipart/form-data; boundary=opaque'),
                                     ('X-Repeated','one'),('X-Repeated','two'),('Connection','X-Hop'),
                                     ('Connection','X-Other-Hop'),('X-Hop','remove'),('X-Other-Hop','remove')])
                async with client.request(method,URL(proxy+target,encoded=True),data=payload,headers=headers) as r:
                    assert r.status==207 and await r.read()==payload
                    assert r.headers.getall('X-Repeated')==['first','second']
                    assert 'X-Private' not in r.headers and 'X-Also-Private' not in r.headers
                health=await (await client.get(proxy+'/health')).json()
                assert health['requests']==health['responses']==0
                assert health['forwarded_http_requests']==health['forwarded_http_responses']==1
            sent_method,sent_target,sent_body,sent_headers=seen[0]
            assert (sent_method,sent_target,sent_body)==(method,'/base/v9'+target,payload)
            assert sent_headers['Authorization']=='Bearer PRIVATE'
            assert sent_headers.getall('X-Repeated')==['one','two']
            assert 'X-Hop' not in sent_headers and 'X-Other-Hop' not in sent_headers
            assert store.db.execute('select count(*) from model_observations').fetchone()[0]==0
        finally:store.close()
    run_proxy_test(run())


def test_streams_large_upload_before_client_finishes(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');first_chunk=asyncio.Event()
        count=0
        async def upstream(request):
            nonlocal count
            async for chunk in request.content.iter_chunked(65536):
                count+=len(chunk);first_chunk.set()
            return web.Response(body=str(count).encode())
        async def upload():
            yield b'x'*65536
            # A buffering proxy deadlocks here. Also exceed its former 128 MiB cap.
            await asyncio.wait_for(first_chunk.wait(),5)
            for _ in range(2048):yield b'x'*65536
        app=web.Application();app.router.add_post('/upload',upstream)
        # Production upstreams use TLS. Plaintext loopback uploads on Windows
        # can be rewritten by third-party HTTP filters (e.g. AdGuard).
        from test_proxy_http2 import tls_contexts
        server_tls,client_tls=tls_contexts(tmp_path)
        server_tls.set_alpn_protocols(['http/1.1'])
        try:
            async with server(app,server_tls) as url, server(create_app(store,tmp_path,url,ssl_context=client_tls)) as proxy, ClientSession() as client:
                async with client.post(proxy+'/upload',data=upload()) as r:
                    body=await r.text()
                    assert r.status==200,(r.status,body[:400])
                    assert body==str(2049*65536)
        finally:store.close()
    run_proxy_test(run())


def test_opaque_compression_cookies_redirects_and_upstream_errors(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');seen=[]
        payload=gzip.compress(b'compressed bytes\x00\xff')
        async def upstream(request):
            seen.append((request.raw_path,await request.read(),request.headers.copy()))
            if request.path=='/start':
                return web.Response(status=307,body=b'redirect body',headers=CIMultiDict([
                    ('Location','/finish'),('Set-Cookie','one=secret; Path=/'),('Set-Cookie','two=secret; Path=/')]))
            if request.path=='/missing':
                return web.Response(status=404,body=b'actual upstream error',headers={'X-Upstream':'yes'})
            return web.Response(body=payload,headers={'Content-Encoding':'gzip'})
        app=web.Application(handler_args={'auto_decompress':False});app.router.add_route('*','/{tail:.*}',upstream)
        try:
            async with server(app) as url, server(create_app(store,tmp_path,url.replace('127.0.0.1','localhost'))) as proxy:
                # Separate callers must not share upstream cookies via the relay.
                async with ClientSession(auto_decompress=False) as client:
                    async with client.post(proxy+'/start',data=b'',allow_redirects=False) as r:
                        assert r.status==307 and await r.read()==b'redirect body'
                        assert r.headers['Location']=='/finish' and len(r.headers.getall('Set-Cookie'))==2
                async with ClientSession(auto_decompress=False) as client:
                    async with client.post(proxy+'/compressed',data=payload,headers={'Content-Encoding':'gzip','Accept-Encoding':'gzip'}) as r:
                        assert r.status==200 and await r.read()==payload and r.headers['Content-Encoding']=='gzip'
                    async with client.get(proxy+'/missing') as r:
                        assert r.status==404 and await r.read()==b'actual upstream error' and r.headers['X-Upstream']=='yes'
                assert [x[0] for x in seen]==['/start','/compressed','/missing']
                assert seen[1][1]==payload and seen[1][2]['Accept-Encoding']=='gzip'
                assert all('Cookie' not in x[2] for x in seen)
        finally:store.close()
    run_proxy_test(run())


def test_head_and_bodyless_response_semantics(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite')
        async def upstream(request):
            if request.method=='HEAD':return web.Response(body=b'representation',headers={'ETag':'"one"'})
            return web.Response(status=204,headers={'ETag':'"two"'})
        app=web.Application();app.router.add_route('*','/asset',upstream)
        try:
            async with server(app) as url, server(create_app(store,tmp_path,url)) as proxy, ClientSession() as client:
                async with client.head(proxy+'/asset') as r:
                    assert r.status==200 and r.headers['Content-Length']==str(len(b'representation'))
                    assert await r.read()==b'' and r.headers['ETag']=='"one"'
                async with client.delete(proxy+'/asset') as r:
                    assert r.status==204 and await r.read()==b'' and r.headers['ETag']=='"two"'
        finally:store.close()
    run_proxy_test(run())


def test_non_response_websocket_is_opaque_and_preserves_target(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');seen=[]
        async def upstream(request):
            seen.append(request.raw_path)
            ws=web.WebSocketResponse(protocols=['opaque'])
            ws.headers.add('X-Tool-Session','first');ws.headers.add('X-Tool-Session','second')
            await ws.prepare(request)
            await ws.send_str(await ws.receive_str())
            await ws.send_bytes(await ws.receive_bytes())
            await ws.close();return ws
        app=web.Application();app.router.add_get('/base/future/socket',upstream)
        try:
            async with server(app) as url, server(create_app(store,tmp_path,url+'/base')) as proxy, ClientSession() as client:
                async with client.ws_connect(URL(proxy+'/future/socket?x=%2f&x=a+b',encoded=True),protocols=['opaque']) as ws:
                    assert ws.protocol=='opaque'
                    assert ws._response.headers.getall('X-Tool-Session')==['first','second']
                    data=json.dumps({'type':'response.create','model':'do-not-observe'})
                    await ws.send_str(data);assert await ws.receive_str()==data
                    await ws.send_bytes(b'\x00\xff');assert await ws.receive_bytes()==b'\x00\xff'
                assert seen==['/base/future/socket?x=%2f&x=a+b']
                health=await (await client.get(proxy+'/health')).json()
                assert health['requests']==health['responses']==0
                assert store.db.execute('select count(*) from model_observations').fetchone()[0]==0
        finally:store.close()
    run_proxy_test(run())


def test_request_cannot_change_upstream_authority(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');seen=[]
        async def upstream(request):
            seen.append(request.raw_path);return web.Response(text='fixed upstream')
        app=web.Application();app.router.add_route('*','/{tail:.*}',upstream)
        try:
            async with server(app) as url, server(create_app(store,tmp_path,url+'/base')) as proxy, ClientSession() as client:
                async with client.get(URL(proxy+'//attacker.invalid/path',encoded=True)) as r:
                    assert r.status==200 and await r.text()=='fixed upstream'
                assert seen==['/base//attacker.invalid/path']
                async with client.get(proxy+'/anywhere',headers={'Origin':'https://example.com'}) as r:
                    assert r.status==403
                assert len(seen)==1
        finally:store.close()
    run_proxy_test(run())


def test_disconnect_while_waiting_for_upstream_cancels_work(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');started=asyncio.Event();cancelled=asyncio.Event();release=asyncio.Event()
        async def upstream(request):
            await request.read();started.set()
            try:
                await release.wait()
                return web.Response()
            finally:cancelled.set()
        app=web.Application(handler_args={'handler_cancellation':True});app.router.add_post('/responses',upstream)
        try:
            async with server(app) as url, server(create_app(store,tmp_path,url)) as proxy:
                reader,writer=await asyncio.open_connection('127.0.0.1',int(proxy.rsplit(':',1)[1]))
                writer.write(b'POST /responses HTTP/1.1\r\nHost: localhost\r\nContent-Length: 13\r\n\r\n{"model":"m"}')
                await writer.drain();await asyncio.wait_for(started.wait(),2)
                writer.close();await writer.wait_closed()
                try:await asyncio.wait_for(cancelled.wait(),.5)
                finally:release.set()
        finally:store.close()
    run_proxy_test(run())


def test_no_default_content_type_is_added_and_sse_still_observed(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite')
        payload=b'data: {"type":"response.completed","response":{"id":"bare","model":"m"}}\n\n'
        async def upstream(reader,writer):
            headers=await reader.readuntil(b'\r\n\r\n')
            length=next(int(line.split(b':',1)[1]) for line in headers.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            await reader.readexactly(length)
            writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
            await writer.drain();writer.close();await writer.wait_closed()
        listener=await asyncio.start_server(upstream,'127.0.0.1',0)
        try:
            url=f'http://127.0.0.1:{listener.sockets[0].getsockname()[1]}'
            async with server(create_app(store,tmp_path,url)) as proxy,ClientSession() as client:
                async with client.post(proxy+'/responses',json={'model':'m'}) as response:
                    assert await response.read()==payload
                    assert not {'Content-Type','Date','Server'}&response.headers.keys()
                assert store.db.execute("select response_model from model_observations where status='completed'").fetchone()==('m',)
        finally:listener.close();await listener.wait_closed();store.close()
    run_proxy_test(run())


def test_upstream_failure_is_not_replayed(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');calls=[];traces=[]
        async def upstream(reader,writer):
            calls.append(await reader.readuntil(b'\r\n\r\n'))
            writer.transport.abort()
        listener=await asyncio.start_server(upstream,'127.0.0.1',0)
        try:
            url=f'http://127.0.0.1:{listener.sockets[0].getsockname()[1]}'
            async with server(create_app(store,tmp_path,url,diagnostics=traces.append)) as proxy,ClientSession() as client:
                async with client.post(proxy+'/responses',json={'model':'m'}) as response:
                    assert response.status==502;await response.read()
                assert len(calls)==1
                health=await (await client.get(proxy+'/health')).json()
                assert health['relay_errors']==1 and health['client_disconnects']==0
        finally:listener.close();await listener.wait_closed();store.close()
    run_proxy_test(run())


def test_truncated_upstream_is_not_reported_as_success(tmp_path):
    async def run():
        from aiohttp import ClientPayloadError
        store=EvidenceStore(tmp_path/'e.sqlite')
        calls=[]
        async def upstream(reader,writer):
            calls.append(await reader.readuntil(b'\r\n\r\n'))
            writer.write(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\noops\r\n')
            await writer.drain()
            await asyncio.sleep(.02)
            writer.close();await writer.wait_closed()
        listener=await asyncio.start_server(upstream,'127.0.0.1',0)
        try:
            url=f'http://127.0.0.1:{listener.sockets[0].getsockname()[1]}'
            async with server(create_app(store,tmp_path,url)) as proxy,ClientSession() as client:
                with pytest.raises(ClientPayloadError):
                    async with client.get(proxy+'/truncated') as response:await response.read()
                assert len(calls)==1
                assert (await (await client.get(proxy+'/health')).json())['relay_errors']==1
        finally:listener.close();await listener.wait_closed();store.close()
    run_proxy_test(run())


def test_websocket_compaction_shaped_large_request_and_idle_gap(tmp_path):
    async def run():
        store=EvidenceStore(tmp_path/'e.sqlite');sent=[]
        request_text=json.dumps({'type':'response.create','input':'x'*18000000,'model':'m','service_tier':'priority'})
        completed=json.dumps({'type':'response.completed','response':{'id':'compact-test','model':'m','output':[{'type':'compaction','encrypted_content':'opaque'}]}})
        async def upstream(request):
            ws=web.WebSocketResponse(max_msg_size=32*1024*1024)
            await ws.prepare(request)
            message=await ws.receive_str();sent.append(message)
            await ws.send_str('{"type":"response.created","response":{"id":"compact-test","model":"m"}}')
            await asyncio.sleep(.25)
            await ws.ping(b'keepalive')
            await asyncio.sleep(.25)
            await ws.send_str(completed)
            await ws.close(code=1000)
            return ws
        app=web.Application();app.router.add_get('/responses',upstream)
        try:
            async with server(app) as url,server(create_app(store,tmp_path,url)) as proxy,ClientSession() as client:
                async with client.ws_connect(proxy+'/responses',max_msg_size=32*1024*1024) as ws:
                    await ws.send_str(request_text)
                    assert (await ws.receive_json())['type']=='response.created'
                    assert await ws.receive_str()==completed
                    await ws.receive()
                    assert ws.close_code==1000
                assert sent==[request_text]
                await asyncio.sleep(.05)
                health=await (await client.get(proxy+'/health')).json()
                assert health['relay_errors']==0
                trace=health['recent_relays'][-1]
                assert trace['close_side']=='upstream' and trace['close_code']==1000
                assert trace['request_frames']==1 and trace['response_frames']==2
                assert trace['last_upstream_event']=='response.completed'
                assert 'encrypted_content' not in json.dumps(health)
                assert store.db.execute("select requested_service_tier from model_observations where status='completed'").fetchone()==('priority',)
        finally:store.close()
    run_proxy_test(run())
