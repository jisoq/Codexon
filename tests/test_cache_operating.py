"""Opt-in operation travels through capture, scheduler, transport and usage ledger.

All model responses are supplied by loopback servers. Token counts are fixtures,
not evidence of real cache retention or ChatGPT cost/limit behavior.
"""
import asyncio
import json
import time

import pytest
from aiohttp import web

from cachemonitor.cache_control import Control,control_path
from cachemonitor.cache_execution import Contexts,Executor,Journal,request_once
from cachemonitor.cache_integration import enrich
from cachemonitor.cache_operating import target
from cachemonitor.cache_scheduler import Scheduler
from test_cache_management import body,response
from test_cache_product import profile
from test_model_proxy import server,run_proxy_test

URL='https://chatgpt.com/backend-api/codex/responses'
HEADERS={'ChatGPT-Account-ID':'synthetic-account','Content-Length':'1'}


@pytest.mark.parametrize('kind',['unsupported','schema','nested','text','malformed','truncated','timeout','lost','empty','event','failed_event'])
def test_http_error_evidence_survives_product_executor_and_journal(tmp_path,kind):
    async def scenario():
        path=tmp_path/'c.sqlite';seen=[]
        secret='sk-private-credential';prompt='ORIGINAL PRIVATE CONVERSATION'
        headers={**HEADERS,'Authorization':'Bearer '+secret}
        request={**body(),'input':[{'role':'user','content':prompt}]}
        async def endpoint(req):
            seen.append(await req.json())
            if kind in ('event','failed_event'):
                error=dict(code='invalid_tool_schema',type='invalid_request_error',param='tools',message='Invalid schema '+prompt)
                event=dict(type='error',error=error) if kind=='event' else dict(type='response.failed',response=dict(status='failed',error=error))
                return web.Response(text='data: '+json.dumps(event)+'\n\n',content_type='text/event-stream',headers={'x-request-id':'req-'+kind})
            if kind=='unsupported':
                payload={'error':{'code':'unsupported_parameter','type':'invalid_request_error','param':'max_output_tokens',
                    'detail':'Output limit not supported','message':'Unsupported parameter '+prompt+' '+secret},
                    'request':request,'authorization':'Bearer '+secret}
            elif kind=='schema':
                payload={'detail':[{'code':'invalid_tool_schema','type':'validation_error','param':'tools.0',
                    'message':'Tool schema missing required property','detail':'Missing property'}]}
            elif kind=='nested':
                payload={'metadata':{'private_account_label':{'notes':['UNRELATED PRIVATE RESPONSE DATA',
                    {'code':'invalid_tool_schema','type':'validation_error','param':'tools.0',
                     'message':'Tool schema missing required property','detail':'Missing property'}]}}}
            else:payload=None
            if payload:return web.json_response(payload,status=400,headers={'x-request-id':'req-'+kind})
            if kind=='empty':return web.Response(status=400,headers={'x-request-id':'req-empty'})
            text=('Unsupported request '+prompt+' Authorization=Bearer '+secret if kind=='text' else
                  '{"error":{"code":"bad_schema","message":"'+prompt+' '+secret+'"' if kind=='malformed' else
                  'Rejected '+prompt+' '+secret+' '+'x'*20000 if kind=='truncated' else
                  '{"error":{"code":"partial_error","message":"'+prompt+'"')
            reply=web.StreamResponse(status=400,headers={'x-request-id':'req-'+kind})
            await reply.prepare(req);await reply.write(text.encode())
            if kind=='timeout':await asyncio.sleep(.3)
            if kind=='lost':req.transport.close()
            return reply
        app=web.Application();app.router.add_post('/responses',endpoint)
        async with server(app) as upstream:
            async def send(url,h,value,**options):return await request_once(upstream+'/responses',h,value,**options)
            journal=Journal(path);contexts=Contexts();contexts.completed(request,response())
            executor=Executor(journal,contexts,send=send);operation=allow(journal)
            opts=dict(anchor=time.monotonic(),deadline=time.monotonic(),latency_bound=.1 if kind=='timeout' else 2,operation=operation)
            start=time.monotonic()
            assert await executor.run('home','s','original',URL,headers,**opts)=='failed'
            if kind=='timeout':assert time.monotonic()-start<.25
            assert await executor.run('home','s','original',URL,headers,**opts)=='duplicate'
            journal.close()
        journal=Journal(path);rows=journal.rows();assert len(rows)==len(seen)==1
        row=rows[0];evidence=row['transport']
        assert evidence['http_status']==(200 if kind in ('event','failed_event') else 400) and evidence['rejected']
        assert evidence['request_ids']['x-request-id']=='req-'+kind
        assert row['state']=='failed' and row['input'] is None and row['cost'] is None and not row['usage_known']
        assert journal.operations.stats(journal.operations.grants()[0])['calls']==1
        assert journal.operations.grants()[0]['stopped']=='usage_unresolved'
        if kind in ('unsupported','schema','nested'):
            fields={f['field']:f['value'] for f in evidence['fields']}
            assert fields['code']==('unsupported_parameter' if kind=='unsupported' else 'invalid_tool_schema')
            assert all(k in fields for k in ('code','type','param','detail','message'))
            assert evidence['body_complete'] and not evidence['missing_fields']
        if kind=='malformed':assert evidence['body_format']=='invalid_json' and evidence['body_complete']
        if kind=='text':assert evidence['body_format']=='text' and 'Unsupported request' in evidence['body']
        if kind=='truncated':assert evidence['body_truncated'] and not evidence['body_complete']
        if kind in ('timeout','lost'):assert evidence['read_error'] and evidence['body_bytes']>0 and not evidence['body_complete']
        if kind=='empty':assert evidence['body_format']=='empty' and len(evidence['missing_fields'])==5
        assert len(evidence['body'])<=2048
        stored='\n'.join(journal.db.iterdump())
        assert prompt not in stored and secret not in stored and 'synthetic-account' not in stored
        assert 'UNRELATED PRIVATE RESPONSE DATA' not in stored and 'private_account_label' not in stored
        journal.close()
    run_proxy_test(scenario())


@pytest.mark.parametrize('state',['observed','reserved','completed','stopped'])
def test_cli_uses_panel_forecasts_after_status_changes_and_rejects_stale_context(tmp_path,monkeypatch,capsys,state):
    from tools.cache_runtime import main
    home=str((tmp_path/'home').resolve());path=tmp_path/'c.sqlite'
    control=Control(path);now=time.time()
    row=dict(profile(),key='current',ts=now)
    control.profile(home,'s',row)
    forecast=dict(snapshot='current',observed_at=now,cost_valid_until=now+100,model=row['model'],effort=row['effort'],
                  service_tier='default',maintenance_expected=.1,maintenance_adverse=1,output_high=64,basis='natural_output_proxy')
    control.forecast(home,'s',forecast)
    control.status(home,'s',dict(state=state,maintenance_expected=99,maintenance_adverse=999,cost_valid_until=now+100))
    def invoke(action,*extra):
        monkeypatch.setattr('sys.argv',['cache_runtime.py',action,'--database',str(path),'--home',home,*extra])
        main();output=capsys.readouterr().out
        if action=='authorize-diagnostic':output=output[output.index('\n')+1:]
        return json.loads(output)
    assert invoke('status')['current_scenarios']==control.forecasts(home,time.time())==[forecast]
    args=('--authorization','test-only','--account-hash','a'*64,'--model',row['model'],'--effort',row['effort'],'--cost-stop','1')
    result=invoke('authorize-diagnostic',*args)
    assert result['grants'][0]['expected']==.1 and result['grants'][0]['max_calls']==2
    assert not result['usage']
    # An existing persistent forecast must never fall back to a status amount.
    for stale in ('expired','new_context'):
        control.forecast(home,'s',dict(forecast,cost_valid_until=now-1) if stale=='expired' else forecast)
        if stale=='new_context':control.profile(home,'s',dict(row,key='next',ts=now+1))
        assert invoke('status')['current_scenarios']==control.forecasts(home,time.time())==[]
        fresh_args=('--authorization','unused-'+stale,*args[2:])
        with pytest.raises(ValueError,match='fresh_pricing_required'):invoke('authorize-diagnostic',*fresh_args)
        assert control.get('authorization:unused-'+stale) is None
    control.close()


def test_diagnostic_astra_uses_product_executor_without_return_history(tmp_path):
    async def scenario():
        index=tmp_path/'index.sqlite';seen=[]
        async def endpoint(req):
            seen.append(await req.json())
            value=response();value['id']='diagnostic-result'
            value['usage'].update(input_tokens=100000,input_tokens_details={'cached_tokens':100000,'cache_write_tokens':0})
            return web.Response(text='data: '+json.dumps(dict(type='response.completed',response=value))+'\n\n',content_type='text/event-stream')
        app=web.Application();app.router.add_post('/responses',endpoint)
        async with server(app) as upstream:
            async def send(url,headers,value,**options):
                assert url==URL and not options['websocket']
                return await request_once(upstream+'/responses',headers,value,**options)
            scheduler=Scheduler('home',control_path(index),send=send,continuous_capture=True)
            request={**body(),'model':'gpt-6-astra','reasoning':{'effort':'high'}}
            request.pop('service_tier')  # Installed Codex omits Standard on the wire.
            assert target('home',request,URL,HEADERS,False) is None
            assert target('home',{**request,'service_tier':'priority'},URL,HEADERS,False,observed_tier='default') is None
            original=response();original['usage'].update(input_tokens=100000,input_tokens_details={'cached_tokens':99000,'cache_write_tokens':0})
            history=[dict(profile(),model='gpt-6-astra',effort='high',key=original['id'],ts=time.time(),turn='natural')]
            enrich([dict(home='home',id='session',history=history)],index,time.time())
            scheduler.executor.contexts.completed(request,original)
            scheduler.snapshot(request,original,time.monotonic(),URL,HEADERS,False)
            estimate=scheduler.policy('session',scheduler.snapshots['session'],observe=True)
            assert estimate['policy_state']!='eligible'
            proposal=scheduler.journal.operations.propose(target('home',request,URL,HEADERS,False,observed_tier='default'),
                estimate['maintenance_expected'],estimate['maintenance_adverse'],estimate['output_high'],estimate['basis'],
                cost_stop=.464502,purpose='diagnostic',dynamic_estimate=True)
            key=scheduler.journal.operations.consent(proposal['id'])
            scheduler.control.set('diagnostic_request',key)
            scheduler.control.activity('home',dict(session_id='session',turn_id='t',hook_event_name='UserPromptSubmit'))
            await scheduler.tick();assert not seen and scheduler.control.get('diagnostic_request')==key
            scheduler.control.activity('home',dict(session_id='session',turn_id='t',hook_event_name='Stop'))
            await scheduler.tick();await scheduler.jobs['diagnostic:'+key]
            await scheduler.tick()
            assert len(seen)==1 and not scheduler.control.get('diagnostic_request')
            assert seen[0]['tools']==request['tools'] and seen[0]['reasoning']==request['reasoning']
            assert seen[0]['tool_choice']=='none' and 'max_output_tokens' not in seen[0]
            assert 'service_tier' not in seen[0]  # Keep the original wire representation.
            rows=scheduler.journal.rows();assert rows[0]['purpose']=='diagnostic' and rows[0]['cost']>0
            await scheduler.close()
        views=[dict(home='home',id='session',history=history)]
        summary=enrich(views,index,time.time())
        assert summary['diagnostic_calls']==summary['calls']==summary['priced']==1
        assert not summary['effects'] and views[-1]['purpose']=='diagnostic'
        from cachemonitor.analysis_engine import AnalysisEngine
        from cachemonitor.quota_cycles import QuotaLedger
        from cachemonitor.overlay_data import OverlaySummaries
        engine=AnalysisEngine();engine.ingest(views[-1:])
        assert next(iter(engine.sessions.values()))['prepared']['history'][0]['cost']==rows[0]['cost']
        assert summary['known_cost']==rows[0]['cost']
        ledger=QuotaLedger(tmp_path/'quota.sqlite')
        snapshot=dict(homes=['home'],sessions=views[-1:],ts=time.time(),usage_collection_complete=True,
            request_activity=summary['request_activity'],index=dict(loading=False,usage_complete=True,last_usage_success=time.time()))
        ledger.sync(engine,snapshot);ledger.sync(engine,snapshot)
        assert ledger.db.execute('SELECT COUNT(*),SUM(cost) FROM calls').fetchone()==pytest.approx((1,rows[0]['cost']))
        ledger.close()
        from cachemonitor.core import Session
        parent=Session('session','home',title='Natural work')
        parent.add_usage(time.time()-1,'original',original['usage'],'gpt-6-astra','natural','high','default')
        engine.ingest([parent.view(time.time()),views[-1]])
        overlay=next(s for s in OverlaySummaries().collect(engine) if s['id']=='session')
        assert overlay['calls']==2 and overlay['maintenance_calls']==0
    run_proxy_test(scenario())


def test_dynamic_estimate_and_combined_cost_gate_are_checked_again_before_upload(tmp_path):
    journal=Journal(tmp_path/'c.sqlite');scope=target('home',body(),URL,HEADERS,False)
    proposal=journal.operations.propose(scope,.1,1,64,'natural_output_proxy',cost_stop=.464502,purpose='diagnostic',dynamic_estimate=True)
    key=journal.operations.consent(proposal['id'])
    operation=dict(id=key,scope=scope,purpose='diagnostic',expected=.3,adverse=3,output_high=64)
    assert journal.operations.check(operation) is None  # New expected cost is not frozen C.
    reserved=journal.reserve('home','s',0,body(),'original',operation=operation)
    assert not journal.permit_operation(reserved,dict(operation,expected=.5))
    assert not journal.rows() and journal.operations.stats(journal.operations.grants()[0])['calls']==0
    journal.finish(reserved,'cancelled')
    reserved=journal.reserve('home','s',0,body(),'second',operation=operation)
    assert journal.permit_operation(reserved,operation)
    value=response();value['usage']['output_tokens']=4
    journal.finish(reserved,'completed',value,scope_read_lower=80)
    grant=journal.operations.grants()[0];observed=journal.operations.stats(grant)['observed']
    assert observed>0 and not grant['stopped']
    assert journal.operations.check(dict(operation,expected=.464502-observed+.000001))=='projected_cost_stop'
    assert journal.operations.check(dict(operation,scope={**scope,'effort':'high'}))=='scope_mismatch'
    journal.close()


def test_observation_only_forwards_user_but_never_executes_or_guards(tmp_path):
    from aiohttp import ClientSession
    from cachemonitor.model_proxy import create_app
    from cachemonitor.model_evidence import EvidenceStore
    from cachemonitor.cache_control import hook_decision
    async def scenario():
        seen=[]
        async def endpoint(req):
            seen.append(await req.json())
            return web.Response(text='data: '+json.dumps(dict(type='response.completed',response=response()))+'\n\n',content_type='text/event-stream')
        async def forbidden(*a,**kw):raise AssertionError('maintenance transport must never run')
        path=tmp_path/'cache-control.sqlite'
        scheduler=Scheduler('home',path,observation_only=True,send=forbidden)
        scheduler.control.set('automatic',True);scheduler.control.set('guard',True)
        scheduler.control.set('ui_heartbeat',time.time());allow(scheduler.journal)
        app=web.Application();app.router.add_post('/responses',endpoint)
        store=EvidenceStore(tmp_path/'e.sqlite')
        async with server(app) as upstream,server(create_app(store,'home',upstream,cache_capture=scheduler.capture)) as proxy:
            async with ClientSession() as client:
                async with client.post(proxy+'/responses',json=body()) as reply:assert reply.status==200
                async with client.get(proxy+'/health') as reply:
                    health=await reply.json()
                    assert health['cache_observation_only'] and health['cache_analysis_revision']==2
            assert scheduler.snapshots
            scheduler.policy=lambda *a:dict(state='eligible',calls=2,interval=0,latency_bound=30,output_cap=64)
            await scheduler.tick()
            assert not scheduler.jobs
            assert await scheduler.executor.run('home','s','original',upstream,{},anchor=0,deadline=0,latency_bound=30)=='observation_only'
            scheduler.control.profile('home','s',dict(profile(),written=0))
            assert hook_decision(path,'home',dict(hook_event_name='UserPromptSubmit',session_id='s',turn_id='t',model='gpt-6-sol'),observe_only=True)=={}
            assert not scheduler.control.requests()
            assert len(seen)==1 and not scheduler.journal.rows()
        await scheduler.close();store.close()
    run_proxy_test(scenario())


def allow(journal,expected=.1):
    scope=target('home',body(),URL,HEADERS,False)
    proposal=journal.operations.propose(scope,expected,expected*10,64,'natural_output_proxy')
    key=journal.operations.consent(proposal['id'])
    return dict(id=key,scope=scope,expected=expected,adverse=expected*10,output_high=64)


def seed_history(index,sid='session'):
    now=time.time();control=Control(control_path(index));history=[]
    for n in range(6):
        at=now-12000+n*2100
        control.db.execute('INSERT INTO cache_inputs VALUES(?,?,?,?,?,?)',
                           ('home',sid,str(n),at,'gpt-6-luna','UserPromptSubmit'))
        history.extend([dict(profile(),ts=at+1,key='cold'+str(n),turn=str(n),cached=0),
                        dict(profile(),ts=at+2,key='warm'+str(n),turn=str(n))])
    enrich([dict(home='home',id=sid,history=history)],index,now)
    control.close()


@pytest.mark.parametrize('source_websocket',[False,True])
def test_natural_policy_consent_capture_wire_and_final_accounting(tmp_path,source_websocket):
    async def scenario():
        index=tmp_path/'index.sqlite';seed_history(index);seen=[]
        async def endpoint(req):
            value=await req.json();seen.append(value)
            result=response();result['id']='maintained'
            result['usage'].update(input_tokens=100010,input_tokens_details={'cached_tokens':99010,'cache_write_tokens':0})
            return web.Response(text='data: '+json.dumps(dict(type='response.completed',response=result))+'\n\n',content_type='text/event-stream')
        app=web.Application();app.router.add_post('/responses',endpoint)
        async with server(app) as upstream:
            async def send(url,headers,value,**options):
                assert url==URL
                assert options['websocket'] is False
                return await request_once(upstream+'/responses',headers,value,**options)
            scheduler=Scheduler('home',control_path(index),send=send);scheduler.control.set('automatic',True)
            request=dict(body(),max_output_tokens=64);original=dict(response(),id='warm5')
            original['usage'].update(input_tokens=100000,input_tokens_details={'cached_tokens':99000,'cache_write_tokens':0})
            captured=scheduler.capture.begin(HEADERS,URL,time.monotonic())
            captured['request'].feed(json.dumps(request).encode())
            captured['response'].feed(b'data: '+json.dumps(dict(type='response.completed',response=original)).encode()+b'\n\n')
            scheduler.capture.finish(captured,websocket=source_websocket)
            snapshot=scheduler.snapshots['session']
            decision=scheduler.policy('session',snapshot)
            assert decision['state']=='disabled' and decision['reason']=='operating_consent_required'
            assert decision['natural_samples']==12 and decision['maintenance_samples']==0
            assert decision['maintenance_adverse']>decision['maintenance_expected']>0
            assert 'output_cap' not in decision and 'maintenance_upper' not in decision
            assert scheduler.control.get('bounded_provider:chatgpt.com') is None
            # Longer-return history can justify 3 calls but not this 2-call pilot.
            saved=list(scheduler.control.db.execute('SELECT turn,data FROM cache_gaps'))
            for turn,data in saved:
                gap=json.loads(data)
                if gap['returned']:gap['seconds']=5800
                scheduler.control.db.execute('UPDATE cache_gaps SET data=? WHERE turn=?',(json.dumps(gap),turn))
            assert scheduler.policy('session',snapshot)['reason']=='no_positive_forward_estimate'
            for turn,data in saved:scheduler.control.db.execute('UPDATE cache_gaps SET data=? WHERE turn=?',(data,turn))
            scheduler.journal.operations.consent(decision['proposal'])
            real_policy=scheduler.policy
            scheduler.policy=lambda sid,snap:dict(real_policy(sid,snap),interval=0)
            scheduler.control.activity('home',dict(session_id='session',turn_id='done',hook_event_name='Stop'))
            await scheduler.tick();await scheduler.jobs['session']
            assert len(seen)==1
            assert 'max_output_tokens' not in seen[0] and seen[0]['tool_choice']=='none'
            assert all(seen[0][k]==request[k] for k in ('model','reasoning','tools','instructions','service_tier'))
            assert request['max_output_tokens']==64
            assert scheduler.executor.contexts.responses['warm5'][0]==request
            rows=scheduler.journal.rows();assert len(rows)==1 and rows[0]['usage_known'] and rows[0]['cost']>0
            grants=scheduler.journal.operations.grants();assert scheduler.journal.operations.stats(grants[0])['calls']==1
            await scheduler.close()
        views=[];summary=enrich(views,index,time.time())
        assert summary['calls']==summary['priced']==1 and summary['known_cost']==rows[0]['cost']
        assert len(views)==1 and views[0]['purpose']=='maintenance'
        views=[];assert enrich(views,index,time.time())['known_cost']==summary['known_cost']
    run_proxy_test(scenario())


@pytest.mark.parametrize('observation_only,automatic',[(True,False),(False,False),(False,True)])
def test_expired_inactive_contexts_are_removed_in_every_worker_mode(tmp_path,observation_only,automatic):
    async def scenario():
        scheduler=Scheduler('home',tmp_path/'c.sqlite',observation_only=observation_only,continuous_capture=True)
        scheduler.control.set('automatic',automatic)
        request=body();original=response();sid=request['prompt_cache_key']
        scheduler.executor.contexts.completed(request,original)
        scheduler.snapshot(request,original,time.monotonic()-1801,URL,{'Authorization':'synthetic-secret'},False)
        scheduler.control.status('home',sid,dict(state='waiting'))
        scheduler.evaluations[sid]=(original['id'],time.monotonic())
        try:
            await scheduler.tick()
            assert not scheduler.snapshots and not scheduler.evaluations
            assert not scheduler.executor.contexts.responses and not scheduler.executor.contexts.usage
            assert scheduler.executor.contexts.size==0 and not scheduler.jobs
            assert not scheduler.control.db.execute('SELECT 1 FROM cache_status').fetchone()
            assert scheduler.control.get('worker_snapshots')==0
        finally:await scheduler.close()
    run_proxy_test(scenario())


def test_observation_prices_current_model_without_execution_scope_or_return_history(tmp_path):
    async def scenario():
        index=tmp_path/'index.sqlite'
        scheduler=Scheduler('home',control_path(index),observation_only=True)
        request={**body(),'model':'gpt-6-astra','reasoning':{'effort':'high'},'type':'response.create'}
        original=response()
        original['usage'].update(input_tokens=100000,input_tokens_details={'cached_tokens':99000,'cache_write_tokens':0})
        history=[dict(profile(),model='gpt-6-astra',effort='high',key=original['id'],ts=time.time(),turn='natural')]
        enrich([dict(home='home',id='session',history=history)],index,time.time())
        scheduler.executor.contexts.completed(request,original)
        scheduler.snapshot(request,original,time.monotonic(),URL,HEADERS,True)
        await scheduler.tick()
        decision=scheduler.policy('session',scheduler.snapshots['session'])
        assert decision['state']=='observing' and decision['policy_state']!='eligible'
        assert decision['source_transport']=='WebSocket' and decision['maintenance_transport']=='HTTP'
        assert decision['maintenance_adverse']>decision['maintenance_expected']>0
        assert decision['model']=='gpt-6-astra' and decision['effort']=='high'
        assert decision['operating_scope_available'] and decision['execution_disabled']
        assert not decision['transport_reuse_verified']
        assert not scheduler.journal.operations.proposals('home') and not scheduler.jobs
        assert not scheduler.journal.operations.grants() and not scheduler.journal.rows()
        # Observation does not broaden which model may actually execute.
        scheduler.observation_only=False
        assert scheduler.policy('session',scheduler.snapshots['session'])['state']!='eligible'
        assert not scheduler.journal.operations.grants()
        scheduler.observation_only=True
        scheduler.snapshots['session']['anchor']-=1800
        await scheduler.tick()
        assert not scheduler.control.db.execute('SELECT 1 FROM cache_status').fetchone()
        await scheduler.close()
    run_proxy_test(scenario())


def test_policy_prices_only_rounds_fitting_consent_time_for_each_session(tmp_path):
    from cachemonitor.cache_policy import executable_rounds
    async def scenario():
        index=tmp_path/'index.sqlite';seed_history(index);seed_history(index,'later-session')
        scheduler=Scheduler('home',control_path(index));scheduler.control.set('automatic',True)
        operation=allow(scheduler.journal)
        for sid in ('session','later-session'):
            request=dict(body(),prompt_cache_key=sid);result=dict(response(),id='warm5')
            result['usage'].update(input_tokens=100000,input_tokens_details={'cached_tokens':99000,'cache_write_tokens':0})
            scheduler.executor.contexts.completed(request,result)
            scheduler.snapshot(request,result,time.monotonic(),URL,HEADERS,False)
        # Every returned interval needs TWO maintenance rounds to cover it.
        for sid,turn,data in scheduler.control.db.execute('SELECT sid,turn,data FROM cache_gaps').fetchall():
            gap=json.loads(data)
            if gap['returned']:gap['seconds']=4000
            scheduler.control.db.execute('UPDATE cache_gaps SET data=? WHERE sid=? AND turn=?',(json.dumps(gap),sid,turn))
        for remaining,state,calls in ((3600,'eligible',2),(2400,'off',0),(1700,'off',0)):
            scheduler.journal.db.execute('UPDATE cache_operating_grants SET expires=?',(time.time()+remaining,))
            for sid in ('session','later-session'):
                decision=scheduler.policy(sid,scheduler.snapshots[sid])
                assert (decision['state'],decision['calls'])==(state,calls),decision
                if remaining==2400:assert decision['reason']=='no_positive_forward_estimate'
                if remaining==1700:assert decision['reason']=='no_executable_rounds'
        # Tick actually schedules nothing, and expiry is never extended.
        expiry=scheduler.journal.operations.grants()[0]['expires']
        for sid in scheduler.snapshots:scheduler.control.activity('home',dict(session_id=sid,turn_id='stop',hook_event_name='Stop'))
        await scheduler.tick()
        assert not scheduler.jobs and not scheduler.journal.rows()
        assert scheduler.journal.operations.grants()[0]['expires']==expiry
        # Revalidate the whole remaining plan before the first send, including
        # a decision made before a scheduling delay reduced the available time.
        scheduler.journal.db.execute('UPDATE cache_operating_grants SET expires=?',(time.time()+2400,))
        await scheduler.maintain('later-session',scheduler.snapshots['later-session'],
            dict(calls=2,interval=0,latency_bound=30,operation=operation),scheduler.control.revision('home'))
        assert not scheduler.journal.rows()
        # A delayed start and execution margin are included, not completion time.
        assert executable_rounds(0,1200,2400,2)==2
        assert executable_rounds(1200,1200,2400,2)==1
        assert executable_rounds(0,1769,30,2)==0
        assert executable_rounds(0,1769.5,1800,2)==1
        assert executable_rounds(0,1800,3600,2)==0
        await scheduler.close()
    run_proxy_test(scenario())


@pytest.mark.parametrize('second_session',[False,True])
def test_shared_call_limit_serialization_and_same_context_rounds(tmp_path,second_session):
    async def scenario():
        journal=Journal(tmp_path/'c.sqlite');operation=allow(journal);contexts=Contexts();contexts.completed(body(),response())
        entered=asyncio.Event();release=asyncio.Event();seen=[]
        async def endpoint(req):
            seen.append(await req.json());entered.set();await release.wait()
            value=dict(response(),id='maintenance-'+str(len(seen)))
            return web.Response(text='data: '+json.dumps(dict(type='response.completed',response=value))+'\n\n',content_type='text/event-stream')
        app=web.Application();app.router.add_post('/responses',endpoint)
        async with server(app) as upstream:
            async def send(url,headers,value,**options):return await request_once(upstream+'/responses',headers,value,**options)
            executor=Executor(journal,contexts,send)
            other_journal=Journal(tmp_path/'c.sqlite');other_executor=Executor(other_journal,contexts,send)
            options=dict(anchor=time.monotonic(),deadline=time.monotonic(),latency_bound=2,operation=operation)
            first=asyncio.create_task(executor.run('home','one','original',URL,HEADERS,**options))
            await asyncio.wait_for(entered.wait(),2)
            assert await other_executor.run('home','two','original',URL,HEADERS,**options)=='operation_deferred'
            assert len(seen)==1 and journal.operations.grants()[0]['stopped'] is None
            release.set();assert await first=='completed'
            # Normal next maintenance round is distinct from a duplicate/retry.
            runner=other_executor if second_session else executor
            assert await runner.run('home','two' if second_session else 'one','original',URL,HEADERS,
                                      round_number=0 if second_session else 1,**options)=='completed'
            assert await executor.run('home','third','original',URL,HEADERS,**options)=='operation_deferred'
            assert len(seen)==2
            assert journal.operations.stats(journal.operations.grants()[0])['remaining']==0
            assert journal.operations.grants()[0]['stopped']=='call_limit'
            other_journal.close()
        journal.close();journal=Journal(tmp_path/'c.sqlite');journal.recover_exclusive()
        assert journal.operations.grants()[0]['stopped']=='call_limit' and len(journal.rows())==2
        journal.close()
    run_proxy_test(scenario())


@pytest.mark.parametrize('outcome',['overshoot','cost_excess','output_excess','partial','missing','missing_write','429','timeout','return_disable','close','revoke','incomplete'])
def test_durable_stops_drain_and_unknown_usage(tmp_path,outcome):
    async def scenario():
        index=tmp_path/'index.sqlite';entered=asyncio.Event();release=asyncio.Event();seen=[]
        async def endpoint(req):
            seen.append(await req.json());entered.set();await release.wait()
            if outcome=='missing':req.transport.close();return web.Response()
            if outcome=='429':return web.Response(status=429)
            if outcome=='timeout':await asyncio.sleep(.1)
            value=dict(response(),id='maintenance')
            if outcome=='overshoot':value['usage']['output_tokens']=1000000
            if outcome=='cost_excess':value['usage']['output_tokens']=300000
            if outcome=='output_excess':value['usage']['output_tokens']=65
            if outcome=='partial':value['usage']['input_tokens_details']['cached_tokens']=79
            if outcome=='missing_write':value['usage']['input_tokens_details'].pop('cache_write_tokens')
            if outcome=='incomplete':value['status']='incomplete'
            return web.Response(text='data: '+json.dumps(dict(type='response.incomplete' if outcome=='incomplete' else 'response.completed',response=value))+'\n\n',content_type='text/event-stream')
        app=web.Application();app.router.add_post('/responses',endpoint)
        async with server(app) as upstream:
            async def send(url,headers,value,**options):return await request_once(upstream+'/responses',headers,value,**options)
            scheduler=Scheduler('home',control_path(index),send=send);scheduler.control.set('automatic',True)
            operation=allow(scheduler.journal)
            scheduler.executor.contexts.completed(body(),response())
            scheduler.snapshot(body(),response(),time.monotonic(),URL,HEADERS,False)
            scheduler.control.activity('home',dict(session_id='session',turn_id='t',hook_event_name='Stop'))
            scheduler.policy=lambda *a:dict(state='eligible',calls=2,interval=0,latency_bound=.05 if outcome=='timeout' else 2,operation=operation)
            await scheduler.tick();await asyncio.wait_for(entered.wait(),2)
            task=scheduler.jobs['session'];closing=None
            if outcome=='close':closing=asyncio.create_task(scheduler.close())
            elif outcome=='return_disable':
                scheduler.executor.ingress();scheduler.executor.leave();scheduler.control.set('automatic',False)
                for _ in range(3):await scheduler.tick();task.cancel();await asyncio.sleep(0)
            elif outcome=='revoke':scheduler.journal.operations.stop(operation['id'],'revoked')
            release.set();await task
            if closing:await closing
            else:await scheduler.close()
        journal=Journal(control_path(index));journal.recover_exclusive()
        rows=journal.rows();grant=journal.operations.grants()[0]
        assert len(seen)==len(rows)==1
        unknown=outcome in ('missing','missing_write','429','timeout')
        assert (rows[0]['cost'] is None)==unknown
        if outcome=='overshoot':
            assert rows[0]['cost']>grant['cost_stop'] and grant['stopped']=='observed_cost_stop'
        elif outcome=='incomplete':assert grant['stopped']=='request_failed' and rows[0]['cost']>0
        elif outcome in ('cost_excess','output_excess','partial'):
            assert grant['stopped']==dict(cost_excess='cost_above_estimate',output_excess='output_above_observed',partial='partial_reuse')[outcome]
        elif unknown:
            assert grant['stopped']=='usage_unresolved'
            assert journal.operations.stats(grant)['observed'] is None
            proposal=journal.operations.propose(operation['scope'],.1,1,64,'natural_output_proxy')
            with pytest.raises(ValueError,match='usage_unresolved'):journal.operations.consent(proposal['id'])
        # Reopening settings never clears failures, costs, or the consumed call.
        control=Control(control_path(index));control.set('automatic',False);control.set('automatic',True);control.close()
        assert journal.operations.stats(grant)['calls']==1
        journal.close();views=[];summary=enrich(views,index,time.time())
        assert summary['calls']==1 and summary['priced']==(not unknown)
    run_proxy_test(scenario())


def test_prebody_expiry_scope_mismatch_and_crash_recovery(tmp_path):
    async def scenario():
        journal=Journal(tmp_path/'c.sqlite');operation=allow(journal);contexts=Contexts();contexts.completed(body(),response())
        async def send(url,headers,value,**options):
            journal.db.execute('UPDATE cache_operating_grants SET expires=0')
            assert not options['permit']()
            raise ConnectionError('not transmitted')
        executor=Executor(journal,contexts,send)
        options=dict(anchor=time.monotonic(),deadline=time.monotonic(),latency_bound=2,operation=operation)
        assert await executor.run('wrong-home','s','original',URL,HEADERS,**options)=='scope_mismatch'
        assert await executor.run('home','s','original',URL,HEADERS,**options)=='unknown'
        assert not journal.rows() and journal.operations.grants()[0]['stopped']=='permission_expired'
        operation=allow(journal)
        assert journal.operations.check(dict(operation,expected=.2))=='scope_cost_increased'
        # A crash after the durable body permit leaves a charged, unresolved call.
        operation=allow(journal);key=journal.reserve('home','new',0,body(),'new',operation=operation)
        assert journal.permit_operation(key,operation)
        journal.close();journal=Journal(tmp_path/'c.sqlite');journal.recover_exclusive()
        assert journal.operations.grants()[0]['stopped']=='usage_unresolved'
        assert len(journal.rows())==1 and not journal.rows()[0]['usage_known']
        journal.close()
    run_proxy_test(scenario())
