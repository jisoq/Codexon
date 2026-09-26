"""Installed Codex against loopback only; no account or production home."""
import asyncio
import json
import os
import time
import uuid

import pytest
from aiohttp import web

from cachemonitor.cache_capture import RelayCapture
from cachemonitor.cache_execution import Contexts,Executor,Journal
from cachemonitor.model_evidence import EvidenceStore
from cachemonitor.model_proxy import create_app
from cachemonitor.quota_live import locate_codex
from cachemonitor.cache_control import Control
from cachemonitor.cache_hooks import configure
from cachemonitor.core import usage_values
from test_model_proxy import server


@pytest.mark.parametrize('websocket,maintenance_websocket,native_reconnect',[(False,False,False),(True,True,False),(True,False,False),(True,False,True)])
def test_installed_codex_independent_request(tmp_path,websocket,maintenance_websocket,native_reconnect):
    try:executable=locate_codex()
    except RuntimeError:pytest.skip('Installed Codex runtime unavailable; mock unit tests are separate')
    async def scenario():
        seen=[];wire=[];snapshots=[];notices=[];futures={};sequence=0;clock_offset=[0.]
        journal=Journal(tmp_path/'jobs.sqlite');store=EvidenceStore(tmp_path/'e.sqlite')
        contexts=Contexts();executor=Executor(journal,contexts)
        def events(body):
            rid='resp_'+uuid.uuid4().hex;mid='msg_'+uuid.uuid4().hex
            item=dict(type='message',id=mid,role='assistant',status='completed',content=[dict(type='output_text',text='OK',annotations=[])])
            response=dict(id=rid,object='response',created_at=int(time.time()),status='completed',model=body['model'],output=[item],
                usage=dict(input_tokens=5000,input_tokens_details=dict(cached_tokens=4096,cache_write_tokens=0),output_tokens=2,output_tokens_details=dict(reasoning_tokens=0),total_tokens=5002))
            return [dict(type='codex.response.metadata',headers={'x-models-etag':'synthetic'}),
                    dict(type='codex.rate_limits',rate_limits={'primary':{'used_percent':1,'window_minutes':300,'reset_after_seconds':60}}),
                    dict(type='responsesapi.websocket_timing'),
                    dict(type='response.created',response={**response,'status':'in_progress','output':[]}),
                    dict(type='response.output_item.added',output_index=0,item={**item,'status':'in_progress','content':[]}),
                    dict(type='response.output_text.delta',item_id=mid,output_index=0,content_index=0,delta='OK'),
                    dict(type='response.output_item.done',output_index=0,item=item),dict(type='response.completed',response=response)]
        async def endpoint(request):
            if request.headers.get('Upgrade','').lower()=='websocket':
                ws=web.WebSocketResponse();await ws.prepare(request)
                async for message in ws:
                    if message.type!=web.WSMsgType.TEXT:continue
                    body=json.loads(message.data);seen.append(body);wire.append('WebSocket')
                    for event in events(body):await ws.send_json(event)
                return ws
            body=await request.json();seen.append(body);wire.append('HTTP')
            return web.Response(text=''.join('data: '+json.dumps(e)+'\n\n' for e in events(body)),content_type='text/event-stream')
        app=web.Application();app.router.add_route('*','/responses',endpoint)
        home=tmp_path/'home';home.mkdir()
        control=Control(tmp_path/'control.sqlite');control.set('guard',True)
        configure(home,tmp_path/'control.sqlite',True)
        async with server(app) as upstream,server(create_app(store,home,upstream,cache_capture=RelayCapture(executor,lambda *a:snapshots.append(a)),clock=lambda:time.monotonic()+clock_offset[0])) as proxy:
            config={'model_provider':'isolated','model':'gpt-6-luna','model_providers.isolated.name':'Loopback only',
                    'model_providers.isolated.base_url':proxy,'model_providers.isolated.wire_api':'responses',
                    'model_providers.isolated.needs_openai_auth':False,'model_providers.isolated.supports_websockets':websocket,
                    'model_providers.isolated.request_max_retries':0,'model_providers.isolated.stream_max_retries':0,'analytics.enabled':False}
            command=[executable,'app-server']
            if native_reconnect:
                # Exercise the installed client's normal reconnect policy.
                # Disabling every retry explicitly forces fallback on closure.
                config.pop('model_providers.isolated.request_max_retries')
                config.pop('model_providers.isolated.stream_max_retries')
            for key,value in config.items():command+=['-c',key+'='+json.dumps(value)]
            process=await asyncio.create_subprocess_exec(*command,cwd=tmp_path,env={**os.environ,'CODEX_HOME':str(home)},
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,creationflags=getattr(__import__('subprocess'),'CREATE_NO_WINDOW',0))
            async def read():
                while line:=await process.stdout.readline():
                    value=json.loads(line)
                    if value.get('id') in futures:futures.pop(value['id']).set_result(value)
                    else:notices.append(value)
            reader=asyncio.create_task(read())
            async def rpc(method,params):
                nonlocal sequence
                sequence+=1;future=asyncio.get_running_loop().create_future();futures[sequence]=future
                process.stdin.write((json.dumps(dict(id=sequence,method=method,params=params))+'\n').encode());await process.stdin.drain()
                value=await asyncio.wait_for(future,20)
                assert 'error' not in value,value
                return value.get('result',{})
            async def turn(tid,text,model='gpt-6-luna'):
                value=await rpc('turn/start',dict(threadId=tid,model=model,effort='low',input=[dict(type='text',text=text,text_elements=[])]))
                turn_id=value['turn']['id']
                for _ in range(200):
                    if any(n.get('method')=='turn/completed' and n.get('params',{}).get('turn',{}).get('id')==turn_id for n in notices):return
                    await asyncio.sleep(.05)
                # Keep the deadline; distinguish client/hook startup from relay failure.
                methods=[n.get('method') for n in notices[-12:]]
                raise TimeoutError(f'isolated turn: process={process.returncode}, wire={wire}, notices={methods}')
            try:
                await rpc('initialize',dict(clientInfo=dict(name='cache_integration_test',version='1'),capabilities={'experimentalApi':True}))
                process.stdin.write(b'{"method":"initialized"}\n');await process.stdin.drain()
                listing=await rpc('hooks/list',{'cwds':[str(tmp_path)]})
                trusted={h['key']:{'enabled':True,'trusted_hash':h['currentHash']} for row in listing['data'] for h in row['hooks']}
                assert trusted
                (home/'config.toml').write_text(''.join('[hooks.state.'+json.dumps(k)+']\nenabled=true\ntrusted_hash='+json.dumps(v['trusted_hash'])+'\n' for k,v in trusted.items()),encoding='utf-8')
                thread=await rpc('thread/start',dict(model='gpt-6-luna',modelProvider='isolated',cwd=str(tmp_path),sandbox='read-only',approvalPolicy='never',ephemeral=False,baseInstructions='Reply OK. No tools.',config={'hooks.state':trusted}))
                tid=thread['thread']['id'];await turn(tid,'Remember SYNTHETIC_ORIGINAL. Reply OK.')
                if native_reconnect:
                    clock_offset[0]=301
                    await asyncio.sleep(1.2)
                    from aiohttp import ClientSession
                    async with ClientSession() as health_client:
                        health=await (await health_client.get(proxy+'/health')).json()
                    assert health['websocket_connections']['retired']['idle_expired']>=1
                    assert health['active_connections']==0
                    before_count=len(seen)
                    await turn(tid,'SYNTHETIC_AFTER_IDLE. Reply OK.')
                    # The installed client must rebuild context on a new socket;
                    # a mock that accepts a stale response id would hide data loss.
                    new_requests=seen[before_count:]
                    assert len(new_requests)==1,new_requests
                    assert not new_requests[0].get('previous_response_id')
                    assert 'SYNTHETIC_ORIGINAL' in json.dumps(new_requests[0])
                    assert 'SYNTHETIC_AFTER_IDLE' in json.dumps(new_requests[0])
                    assert set(wire)=={'WebSocket'}
                    await turn(tid,'SYNTHETIC_THIRD. Reply OK.')
                    assert set(wire)=={'WebSocket'} and len(seen)==before_count+2
                    return
                if websocket and not maintenance_websocket:
                    await turn(tid,'SYNTHETIC_DELTA. Reply OK.')
                before=await rpc('thread/read',dict(threadId=tid,includeTurns=True))
                snapshot=snapshots[-1];rid=snapshot[1]['id']
                original_context=contexts.responses[rid][0]
                result=await executor.run(str(home),tid,rid,upstream+'/responses',snapshot[4],anchor=time.monotonic()-1700,deadline=time.monotonic(),latency_bound=20,websocket=maintenance_websocket)
                assert result=='completed'
                assert wire[-1]==('WebSocket' if maintenance_websocket else 'HTTP')
                assert seen[-1]['input'][:len(original_context['input'])]==original_context['input']
                if websocket and not maintenance_websocket:
                    assert len(wire[:-1])>=2 and set(wire[:-1])=={'WebSocket'}
                    assert 'SYNTHETIC_ORIGINAL' in json.dumps(seen[-1]) and 'SYNTHETIC_DELTA' in json.dumps(seen[-1])
                    assert 'previous_response_id' not in seen[-1] and 'type' not in seen[-1]
                after=await rpc('thread/read',dict(threadId=tid,includeTurns=True))
                assert before['thread']['turns']==after['thread']['turns']
                assert ('tools' in seen[-1])==('tools' in snapshot[0])
                assert seen[-1].get('tools')==snapshot[0].get('tools') and seen[-1]['tool_choice']=='none'
                await turn(tid,'Next original request.')
                assert 'Reply exactly ACK' not in json.dumps(seen[-1])
                if seen[-1].get('previous_response_id'):assert seen[-1]['previous_response_id']==rid
                assert journal.rows()[0]['cached']==4096
                # Product hook executable, installed Codex, same live invocation.
                # Approval releases it once; cancellation produces no wire request.
                control.profile(str(home),tid,dict(key='natural',ts=time.time(),model='gpt-6-luna',effort='low',
                    service_tier='Standard',**usage_values(snapshot[1]['usage'])))
                for choice in ('approve','cancel'):
                    before_count=len(seen);control.set('ui_heartbeat',time.time())
                    pending=asyncio.create_task(turn(tid,'SYNTHETIC_GUARDED','gpt-6-sol'))
                    for _ in range(200):
                        control.set('ui_heartbeat',time.time());tickets=control.requests()
                        if tickets:break
                        await asyncio.sleep(.05)
                    assert tickets,notices[-8:]
                    assert len(seen)==before_count
                    assert control.resolve(tickets[0],choice)
                    await pending
                    assert len(seen)==before_count+(choice=='approve')
                    assert 'Codexon: request not approved' not in json.dumps(seen[-1])
            finally:
                process.terminate();await process.wait();reader.cancel();await asyncio.gather(reader,return_exceptions=True)
        journal.close();store.close();control.close()
    asyncio.run(scenario())
