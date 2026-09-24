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


def test_natural_policy_consent_capture_wire_and_final_accounting(tmp_path):
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
                return await request_once(upstream+'/responses',headers,value,**options)
            scheduler=Scheduler('home',control_path(index),send=send);scheduler.control.set('automatic',True)
            request=dict(body(),max_output_tokens=64);original=dict(response(),id='warm5')
            original['usage'].update(input_tokens=100000,input_tokens_details={'cached_tokens':99000,'cache_write_tokens':0})
            captured=scheduler.capture.begin(HEADERS,URL,time.monotonic())
            captured['request'].feed(json.dumps(request).encode())
            captured['response'].feed(b'data: '+json.dumps(dict(type='response.completed',response=original)).encode()+b'\n\n')
            scheduler.capture.finish(captured)
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
