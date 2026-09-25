"""Fixed-upstream loopback relay with passive Responses observation."""
from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
import os
import sqlite3
import hashlib
import ssl

import httpx
from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, WSServerHandshakeError, web
from multidict import CIMultiDict
from yarl import URL
from .model_evidence import EvidenceStore, default_path, identifier, home_key
from .version import PROXY_VERSION
from .proxy_observation import DecodedObservation, SelectedJSON, ResponseMetadata, message_metadata
from .proxy_http import HTTPRelayPool
from .observer_state import read_json
from .evidence_writer import EvidenceWriter

CLIENT = web.AppKey('client', ClientSession)
HTTP_CLIENT = web.AppKey('http_client', HTTPRelayPool)
RELAY_HEADERS = web.RequestKey('relay_header_names', set)
HOP = {'host','connection','upgrade','keep-alive','proxy-authenticate','proxy-authorization',
       'te','trailer','transfer-encoding'}


class ClientDisconnected(ConnectionError):
    pass


def proxy_loop():
    # This process only needs socket I/O. On Windows, the Proactor transport's
    # shutdown() can raise WinError 10022 before detaching a closed socket,
    # leaving server shutdown waiting forever. Use the standard socket loop.
    return asyncio.SelectorEventLoop() if os.name=='nt' else asyncio.new_event_loop()


def headers_without_hop(headers, websocket=False):
    excluded = HOP | {s.strip().lower() for value in headers.getall('Connection',[])
                      for s in value.split(',')}
    return CIMultiDict((k,v) for k,v in headers.items() if k.lower() not in excluded
                      and not (websocket and (k.lower().startswith('sec-websocket-')
                                              or k.lower()=='content-length')))


class Tracker:
    def __init__(self, store, home, transport, health):
        self.store, self.home, self.transport, self.health = store,home,transport,health
        self.pending = defaultdict(deque)
        self.active = {}
        self.completed = OrderedDict()
        self.timings = {}

    def save(self, record):
        if not self.health.get('observation_enabled', True):return
        try:
            accepted=self.store.write(self.home, transport=self.transport, **record)
            if accepted is False:record['observation_missing']=True
            self.health['storage_failure_streak'] = 0
        except (sqlite3.Error, OSError):
            # Observation failure must not trigger model-request retries.
            self.health['storage_errors'] += 1
            self.health['storage_failure_streak'] = self.health.get('storage_failure_streak', 0)+1

    def request(self, obj):
        lane = identifier(obj.get('stream_id'))
        record = dict(attempt=str(uuid.uuid4()),ts=time.time(), requested_model=identifier(obj.get('model')),
                      requested_service_tier=identifier(obj.get('service_tier')),
                      wire_service_tier_present='service_tier' in obj,
                      response_id='', response_model='', response_service_tier='',status='pending', conflict=False)
        record['request_observed_at']=time.time()
        record['cache_policy']=identifier(obj.get('prompt_cache_retention'))
        self.timings[record['attempt']]=time.monotonic()
        self.pending[lane].append(record)
        self.save(record)
        self.health['requests'] += 1

    def response(self, obj):
        typ = identifier(obj.get('type'))
        response = obj.get('response')
        if not isinstance(response,dict):
            response = obj if obj.get('object')=='response' else {}
        rid = identifier(response.get('id'))
        lane = identifier(obj.get('stream_id'))
        if not rid:
            if typ=='error' and self.pending[lane]:
                record=self.pending[lane].popleft()
                record['status']='failed'; self.save(record)
                self.timings.pop(record['attempt'],None)
            return
        if rid in self.completed:
            # Replayed terminal events must never consume a queued request.
            record=self.completed[rid]
            model=identifier(response.get('model'))
            if model and record['response_model'] and model!=record['response_model']:
                record['conflict']=True
                self.save(record)
            return
        if rid not in self.active:
            if self.pending[lane]:
                self.active[rid]=self.pending[lane].popleft()
            else:
                # No exact request association: record response-only evidence, never guess.
                self.active[rid]=dict(attempt=str(uuid.uuid4()),ts=time.time(),requested_model='',
                                      requested_service_tier='',response_service_tier='',
                                      response_id='',response_model='',status='pending',conflict=False)
        record=self.active[rid]
        record['response_id']=rid
        if response.get('service_tier'):
            record['response_service_tier']=identifier(response['service_tier'])
        model=identifier(response.get('model'))
        if model:
            if record['response_model'] and record['response_model']!=model:
                record['conflict']=True
            record['response_model']=model
        state = typ.removeprefix('response.') if typ else response.get('status','')
        began=self.timings.get(record['attempt'])
        # response.in_progress is a generation signal, not a first output token.
        if state=='in_progress' and began is not None and record.get('generation_latency_ms') is None:
            record.update(generation_observed_at=time.time(),generation_latency_ms=(time.monotonic()-began)*1000)
        if state=='completed' and began is not None:
            record.update(completed_observed_at=time.time(),completion_latency_ms=(time.monotonic()-began)*1000)
        if state in ('created','completed','failed','incomplete'):
            if state in ('failed','incomplete'):
                record['completed_observed_at']=time.time()
            record['status']=state
            self.save(record)
        if state in ('completed','failed','incomplete'):
            self.health['internal_failure_streak']=0
            self.active.pop(rid,None)
            self.timings.pop(record['attempt'],None)
            self.completed[rid]=record
            if len(self.completed)>2048:
                self.completed.popitem(last=False)
            self.health['responses'] += 1

    def finish(self, status='disconnected'):
        for record in list(self.active.values())+[r for q in self.pending.values() for r in q]:
            record['completed_observed_at']=time.time()
            record['status']=status
            self.save(record)
        self.active.clear();self.pending.clear()
        self.timings.clear()


def create_app(store, home, upstream='https://chatgpt.com/backend-api/codex', *, diagnostics=None,ssl_context=None,
               control_file=None, control_id='', stop_event=None, managed=None, cache_capture=None):
    base=URL(upstream)
    if (base.scheme not in ('http','https') or not base.host or base.user is not None
            or base.query_string or base.fragment):
        raise ValueError('Upstream must be an absolute HTTP(S) base URL without credentials, query or fragment')
    upstream=upstream.rstrip('/')
    health={'requests':0,'responses':0,'storage_errors':0,'relay_errors':0,'forwarded_response_frames':0,
            'client_disconnects':0,'observation_skips':0,'storage_failure_streak':0,'internal_failure_streak':0,
            'observation_enabled':True,'draining':False,'control_id':control_id,'cache_management':cache_capture is not None,
            'cache_observation_only':bool(cache_capture and cache_capture.executor.observation_only),
            'cache_analysis_revision':getattr(cache_capture,'analysis_revision',None),
            'forwarded_http_requests':0,'forwarded_http_responses':0,
            'service':'cachemonitor-model-observer',
            'version':PROXY_VERSION,'instance':uuid.uuid4().hex,'pid':os.getpid(),
            'identity':hashlib.sha256((home_key(home)+'|'+str(store.path.resolve())).encode()).hexdigest()}
    if managed:health['lifecycle']='managed'
    app=web.Application(handler_args={'auto_decompress':False,'handler_cancellation':True})
    active_relays={}
    sockets={}
    recent_relays=deque(maxlen=32)

    async def control_lifecycle(app):
        async def watch():
            last_tick=0
            while True:
                if managed and time.monotonic()-last_tick>=1:
                    await asyncio.to_thread(managed.tick,health)
                    last_tick=time.monotonic()
                command=read_json(control_file) if control_file else {}
                if command.get('id')==control_id and command.get('action')=='drain':
                    health['draining']=True;health['observation_enabled']=False
                if health['draining']:
                    for relay_id,(ws,tracker,known) in list(sockets.items()):
                        diagnostic=active_relays.get(relay_id,{})
                        if (known and not diagnostic.get('idle_unknown') and not diagnostic.get('forwarding')
                                and not tracker.active and not any(tracker.pending.values())):
                            await ws.close(code=1001, message=b'Observer disabled')
                    if not active_relays and stop_event is not None:stop_event.set()
                await asyncio.sleep(.2)
        task=asyncio.create_task(watch())
        try:yield
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    if control_file:app.cleanup_ctx.append(control_lifecycle)

    async def preserve_response_headers(request,response):
        supplied=request.get(RELAY_HEADERS)
        if supplied is not None:
            for name in ('Content-Type','Date','Server'):
                if name.lower() not in supplied:response.headers.pop(name,None)
    app.on_response_prepare.append(preserve_response_headers)

    async def lifecycle(app):
        async with ClientSession(timeout=ClientTimeout(total=None,connect=30,sock_read=600),
                                 auto_decompress=False,trust_env=False,cookie_jar=DummyCookieJar(),
                                 skip_auto_headers={'Accept','Accept-Encoding','User-Agent','Content-Type'}) as client:
            # Keep the OS trust store used by the previous transport. HTTP/2 is
            # negotiated with the server; application requests are never retried.
            pool=HTTPRelayPool(ssl_context)
            app[CLIENT]=client;app[HTTP_CLIENT]=pool
            try:yield
            finally:await pool.close()
    app.cleanup_ctx.append(lifecycle)

    async def handle(request):
        # Never accept browser-origin traffic or act as an arbitrary forward proxy.
        if request.headers.get('Origin'):
            raise web.HTTPForbidden(text='Browser requests are not supported')
        if request.path=='/health' and request.method=='GET':
            if hasattr(store,'status'):health.update(store.status())
            return web.json_response({'status':'degraded' if health['storage_failure_streak'] else 'ok',**health,
                'active_connections':len(active_relays),
                'active_relays':list(active_relays.values())[-32:],'recent_relays':list(recent_relays)})
        if health['draining']:
            return web.json_response({'error':{'code':'observer_disabled','message':'Observer disabled; restart Codex to use direct connection'}},status=503)
        if request.rel_url.is_absolute() or not request.raw_path.startswith('/'):
            raise web.HTTPBadRequest(text='Only origin-form request targets are supported')
        # Append the untouched request target to the fixed base. URL joining would
        # drop its path prefix; decoded paths/query mappings would change signed URLs.
        url=URL(upstream+request.raw_path,encoded=True)
        is_ws=request.headers.get('Upgrade','').lower()=='websocket'
        observe=request.path=='/responses'
        tracker=Tracker(store,home,'WebSocket' if is_ws else 'HTTP/SSE',health)
        headers=headers_without_hop(request.headers,websocket=is_ws)
        client=app[CLIENT]
        downstream=None
        started=time.monotonic()
        diagnostic={'transport':tracker.transport,'responses_endpoint':observe,'method':request.method,
                    'started_at':time.time(),
                    'client_http':f'{request.version.major}.{request.version.minor}',
                    'request_encoding':identifier(request.headers.get('Content-Encoding','')),
                    'request_bytes':0,'response_bytes':0,'request_frames':0,'response_frames':0}
        relay_id=uuid.uuid4().hex
        active_relays[relay_id]=diagnostic
        phase='upstream_connect'
        response=None
        request_observer=None
        response_observer=None
        connection=None
        lease=None
        cache_record=None
        ws_capture=None
        if cache_capture is not None and observe and not is_ws:
            # Invalidate at ingress, before connection establishment or upload.
            cache_record=cache_capture.begin(headers,url,time.monotonic())
            if request.headers.get('Content-Encoding','identity') not in ('','identity'):
                cache_record['request'].failed=True
        try:
            if is_ws:
                protocols=tuple(p.strip() for p in request.headers.get('Sec-WebSocket-Protocol','').split(',') if p.strip())
                async with client.ws_connect(url,headers=headers,
                                             protocols=protocols,max_msg_size=128*1024*1024) as upstream_ws:
                    diagnostic['upstream_status']=101
                    diagnostic['upstream_http']=f'{upstream_ws._response.version.major}.{upstream_ws._response.version.minor}'
                    downstream=web.WebSocketResponse(protocols=([upstream_ws.protocol] if upstream_ws.protocol else ()),
                                                     max_msg_size=128*1024*1024,compress=False)
                    relay_headers=headers_without_hop(upstream_ws._response.headers,websocket=True)
                    request[RELAY_HEADERS]={name.lower() for name in relay_headers}
                    downstream.headers.extend(relay_headers)
                    await downstream.prepare(request)
                    sockets[relay_id]=(downstream,tracker,observe)

                    if cache_capture is not None:
                        from .cache_capture import WebSocketCapture
                        ws_capture=WebSocketCapture(cache_capture,tracker,headers,url)

                    async def relay(source,destination,outbound):
                        nonlocal phase,cache_record
                        async for msg in source:
                            if msg.type in (WSMsgType.TEXT,WSMsgType.BINARY):
                                diagnostic['forwarding']=diagnostic.get('forwarding',0)+1
                                phase='upstream_write' if outbound else 'downstream_write'
                                diagnostic['request_frames' if outbound else 'response_frames']+=1
                                diagnostic['last_client_frame_at' if outbound else 'last_upstream_frame_at']=time.time()
                                diagnostic['request_bytes' if outbound else 'response_bytes']+=len(msg.data.encode('utf-8') if isinstance(msg.data,str) else msg.data)
                                if observe:
                                    obj=message_metadata(msg.data)
                                    diagnostic['last_client_event' if outbound else 'last_upstream_event']=identifier(obj.get('type'))
                                    if outbound:
                                        if obj.get('type')=='response.create': tracker.request(obj)
                                        elif obj.get('type') not in ('response.cancel',):diagnostic['idle_unknown']=True
                                    if ws_capture is not None:
                                        raw=msg.data.encode() if isinstance(msg.data,str) else msg.data
                                        try:
                                            if outbound:ws_capture.outgoing(obj,raw)
                                            else:ws_capture.incoming(obj,raw)
                                        except Exception:ws_capture.disable()
                                    if not outbound:tracker.response(obj)
                                try:
                                    if msg.type==WSMsgType.TEXT: await destination.send_str(msg.data)
                                    else: await destination.send_bytes(msg.data)
                                except ConnectionResetError as exc:
                                    if not outbound:raise ClientDisconnected() from exc
                                    raise
                                if not outbound: health['forwarded_response_frames']+=1
                                diagnostic['forwarding']-=1
                            elif msg.type==WSMsgType.ERROR:
                                diagnostic['websocket_error_side']='client' if outbound else 'upstream'
                                diagnostic['websocket_error_type']=type(source.exception()).__name__
                                diagnostic['websocket_error_code']=getattr(source.exception(),'code',None)
                                if outbound:raise ClientDisconnected()
                                raise ConnectionError('WebSocket relay failed')
                        return source.close_code or 1000

                    tasks=[asyncio.create_task(relay(downstream,upstream_ws,True)),
                           asyncio.create_task(relay(upstream_ws,downstream,False))]
                    try:
                        done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                        for task in done: task.result()
                        # Stop the idle opposite reader before close(). aiohttp otherwise
                        # wakes that reader with a synthetic CLOSING message; on Windows this
                        # can close the transport before buffered final frames reach the peer.
                        for task in pending: task.cancel()
                        await asyncio.gather(*pending,return_exceptions=True)
                        code=(upstream_ws.close_code if tasks[1] in done else downstream.close_code) or 1000
                        health['last_close_side']='upstream' if tasks[1] in done else 'client'
                        diagnostic['close_side']=health['last_close_side']
                        diagnostic['close_code']=code
                        if code in (1005,1006,1015): code=1011
                        await downstream.close(code=code)
                        await upstream_ws.close(code=code)
                        phase='complete'
                        health['internal_failure_streak']=0
                    finally:
                        for task in tasks: task.cancel()
                        await asyncio.gather(*tasks,return_exceptions=True)
                    return downstream
            capture=observe and request.method=='POST'
            if capture:
                request_observer=DecodedObservation(SelectedJSON(),request.headers.get('Content-Encoding',''))

            async def upload():
                nonlocal phase
                try:
                    async for chunk in request.content.iter_chunked(65536):
                        phase='request_read';diagnostic['request_bytes']+=len(chunk)
                        if cache_record is not None:cache_record['request'].feed(chunk)
                        if request_observer is not None:request_observer.feed(chunk)
                        phase='upstream_write'
                        yield chunk
                finally:
                    if request_observer is not None:
                        fields=request_observer.finish()
                        diagnostic['request_observation']=request_observer.error or request_observer.consumer.error or 'parsed'
                        tracker.request(fields)
            body=upload() if request.can_read_body else None
            if capture and body is None:tracker.request({})
            health['forwarded_http_requests']+=1
            phase='upstream_headers'
            # Construct Request directly: no client-default headers or cookie merge.
            outgoing=httpx.Request(request.method,str(url),
                headers=[(k.encode('ascii'),v.encode('utf-8','surrogateescape')) for k,v in headers.items()],content=body)
            connection=app[HTTP_CLIENT].lease()
            lease=await connection.__aenter__()
            response=await lease.client.send(outgoing,stream=True,follow_redirects=False)
            diagnostic.update(upstream_status=response.status_code,upstream_http=response.http_version,
                              response_encoding=identifier(response.headers.get('Content-Encoding','')),
                              response_type=identifier(response.headers.get('Content-Type','').split(';')[0]))
            health['forwarded_http_responses']+=1
            relay_headers=headers_without_hop(CIMultiDict((k.decode('ascii'),v.decode('utf-8','surrogateescape'))
                                                         for k,v in response.headers.raw))
            request[RELAY_HEADERS]={name.lower() for name in relay_headers}
            downstream=web.StreamResponse(status=response.status_code,reason=response.reason_phrase,headers=relay_headers)
            phase='downstream_headers'
            await downstream.prepare(request)
            if capture:
                response_observer=DecodedObservation(ResponseMetadata(tracker.response),response.headers.get('Content-Encoding',''))
            phase='upstream_read'
            # Do not specify chunk_size: HTTPX otherwise coalesces small SSE frames
            # until that many bytes arrive, delaying or deadlocking a live stream.
            async for chunk in response.aiter_raw():
                diagnostic['response_bytes']+=len(chunk)
                if cache_record is not None:
                    if response.headers.get('Content-Encoding','identity') not in ('','identity'):
                        cache_record['response'].failed=True
                    cache_record['response'].feed(chunk)
                if response_observer is not None:response_observer.feed(chunk)
                phase='downstream_write'
                await downstream.write(chunk)
                phase='upstream_read'
            lease.complete=True
            if response_observer is not None:
                response_observer.finish()
                diagnostic['response_observation']=response_observer.error or response_observer.consumer.error or 'parsed'
            tracker.finish('http_error' if response.status_code>=400 else 'unparsed')
            phase='downstream_eof'
            await downstream.write_eof()
            phase='complete'
            health['internal_failure_streak']=0
            return downstream
        except asyncio.CancelledError:
            if request.transport is None or request.transport.is_closing():
                health['client_disconnects']+=1
                diagnostic['outcome']='client_disconnected'
            else:diagnostic['outcome']='cancelled'
            raise
        except (ClientDisconnected,ConnectionResetError) as exc:
            if isinstance(exc,ClientDisconnected) or phase.startswith('downstream') or request.transport is None or request.transport.is_closing():
                health['client_disconnects']+=1
                diagnostic.update(outcome='client_disconnected',error_type=type(exc).__name__)
            else:
                health['relay_errors']+=1
                health['last_error_type']=type(exc).__name__
                diagnostic.update(outcome='exception',error_type=type(exc).__name__)
            if downstream is not None:
                if not isinstance(downstream,web.WebSocketResponse):
                    downstream.force_close()
                    if request.transport is not None:request.transport.close()
                return downstream
            return web.Response(status=502)
        except WSServerHandshakeError as exc:
            diagnostic.update(outcome='handshake_rejected',upstream_status=exc.status)
            health['relay_errors']+=1
            retry={k:v for k,v in (exc.headers or {}).items() if k.lower() in ('retry-after','www-authenticate')}
            return web.json_response({'error':{'message':'Upstream WebSocket handshake rejected'}},
                                     status=exc.status,headers=retry)
        except Exception as exc:
            diagnostic.update(outcome='exception',error_type=type(exc).__name__)
            health['relay_errors']+=1
            health['last_error_type']=type(exc).__name__
            if not isinstance(exc,(httpx.HTTPError,OSError,asyncio.TimeoutError)):
                health['internal_failure_streak']+=1
            # Never emit upstream URLs, headers or body content in logs/errors.
            if downstream is not None and downstream.prepared:
                if isinstance(downstream,web.WebSocketResponse): await downstream.close(code=1011)
                else:
                    downstream.force_close()
                    if request.transport is not None:request.transport.close()
                return downstream
            return web.json_response({'error':{'message':'Local relay connection failed'}},status=502)
        finally:
            if ws_capture is not None:ws_capture.close()
            if cache_record is not None:
                cache_capture.finish(cache_record,websocket=is_ws)
            try:
                if response is not None:await response.aclose()
            finally:
                if lease is not None:await connection.__aexit__(None,None,None)
            if response_observer is not None:
                response_observer.finish()
                diagnostic['response_observation']=response_observer.error or response_observer.consumer.error or 'parsed'
            if any(diagnostic.get(name) not in (None,'parsed') for name in ('request_observation','response_observation')):
                health['observation_skips']+=1
            tracker.finish()
            diagnostic.update(phase=phase,elapsed_ms=round((time.monotonic()-started)*1000,3),finished_at=time.time())
            diagnostic.setdefault('outcome','finished')
            active_relays.pop(relay_id,None)
            sockets.pop(relay_id,None)
            recent_relays.append(dict(diagnostic))
            if diagnostics is not None:
                try:diagnostics(diagnostic)
                except Exception:pass  # Optional diagnostics must never change transport behavior.

    app.router.add_route('*','/{tail:.*}',handle)
    return app


def main():
    parser=argparse.ArgumentParser(description='Local model consistency observer (does not change Codex settings)')
    parser.add_argument('--port',type=int,default=8768)
    parser.add_argument('--codex-home',default=os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    parser.add_argument('--evidence-path',type=Path,default=default_path())
    parser.add_argument('--upstream',choices=['chatgpt','openai'],default='chatgpt')
    parser.add_argument('--upstream-url',help='Explicit fixed upstream base URL for controlled deployments or local verification')
    parser.add_argument('--upstream-ca',type=Path,help='CA bundle for that upstream only; does not change system trust')
    parser.add_argument('--control-file',type=Path)
    parser.add_argument('--control-id',default='')
    parser.add_argument('--managed',action='store_true')
    parser.add_argument('--cache-observe-only',action='store_true',help='Capture natural context; disable ALL maintenance execution')
    parser.add_argument('--cache-worker',action='store_true',help='Continuous analysis with explicitly authorized independent execution')
    parser.add_argument('--observation-index',type=Path,help='Existing sanitized usage index copy for observation profiles')
    args=parser.parse_args()
    if args.evidence_path.resolve().is_relative_to(Path(args.codex_home).resolve()):
        parser.error('Evidence must be stored outside the Codex home')
    store=EvidenceWriter(args.evidence_path)
    cache_lock=None
    endpoint={'chatgpt':'https://chatgpt.com/backend-api/codex','openai':'https://api.openai.com/v1'}[args.upstream]
    if args.upstream_url:endpoint=args.upstream_url
    context=ssl.create_default_context(cafile=str(args.upstream_ca)) if args.upstream_ca else None
    try:
        if args.cache_observe_only or args.cache_worker:
            if args.cache_observe_only and args.cache_worker:parser.error('Choose observation only or cache worker')
            if args.managed or args.control_file or not args.observation_index:
                parser.error('Observation requires its own index and unmanaged listener')
            from .cache_scheduler import Scheduler
            from .usage_collection import CollectionClient
            from concurrent.futures import ThreadPoolExecutor
            from .cache_execution import execution_owner
            cache_lock=execution_owner(args.observation_index.with_name('cache-control.sqlite'))
            cache_lock.__enter__()
            scheduler=Scheduler(args.codex_home,args.observation_index.with_name('cache-control.sqlite'),
                observation_only=args.cache_observe_only,continuous_capture=args.cache_worker)
            scheduler.journal.recover_exclusive()
            if args.cache_worker:scheduler.capture.analysis_revision=3
            stop=asyncio.Event();control_id=uuid.uuid4().hex
            app=create_app(store,args.codex_home,endpoint,ssl_context=context,cache_capture=scheduler.capture,
                control_file=args.evidence_path.parent/('proxy-control-'+control_id+'.json'),
                control_id=control_id,stop_event=stop)
            async def observe_context(app):
                pool=ThreadPoolExecutor(max_workers=1);index=None
                def poll():
                    nonlocal index
                    if index is None:index=CollectionClient([args.codex_home],args.observation_index,args.evidence_path)
                    value=index.poll()
                    # Existing index/enrich owns profile generation. Never synthesize hooks.
                    return value['index']['loading']
                async def collect():
                    while True:
                        try:loading=await asyncio.get_running_loop().run_in_executor(pool,poll)
                        except Exception:
                            loading=False
                            try:scheduler.control.set('collector_error',True)
                            except (sqlite3.Error,OSError):pass
                        else:
                            try:scheduler.control.set('collector_error',False)
                            except (sqlite3.Error,OSError):pass
                        await asyncio.sleep(1 if loading else 15)
                tasks=[asyncio.create_task(scheduler.serve()),asyncio.create_task(collect())]
                try:yield
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    def close_index():
                        if index:index.close()
                    await asyncio.get_running_loop().run_in_executor(pool,close_index)
                    pool.shutdown();await scheduler.close()
            app.cleanup_ctx.append(observe_context)
            async def serve_cache():
                runner=web.AppRunner(app,access_log=None)
                await runner.setup()
                try:
                    await web.TCPSite(runner,'127.0.0.1',args.port).start()
                    await stop.wait()
                finally:await runner.cleanup()
            loop=proxy_loop()
            try:loop.run_until_complete(serve_cache())
            finally:loop.close()
        elif args.managed:
            from .observer_control import ObserverManager
            from .managed_proxy import ManagedProxy
            manager=ObserverManager(args.codex_home,args.evidence_path.parent,url=f'http://127.0.0.1:{args.port}')
            loop=proxy_loop()
            try:loop.run_until_complete(ManagedProxy(manager).serve(store,endpoint,context,args.port))
            finally:loop.close()
        elif args.control_file:
            if (args.control_file.resolve().parent != args.evidence_path.resolve().parent
                    or args.control_file.name != 'proxy-control-'+args.control_id+'.json'):
                parser.error('Control file must belong to the observer data directory')
            async def serve():
                stop=asyncio.Event()
                app=create_app(store,args.codex_home,endpoint,ssl_context=context,control_file=args.control_file,
                               control_id=args.control_id,stop_event=stop)
                runner=web.AppRunner(app,access_log=None)
                await runner.setup()
                try:
                    await web.TCPSite(runner,'127.0.0.1',args.port).start()
                    await stop.wait()
                finally:await runner.cleanup()
            loop=proxy_loop()
            try:loop.run_until_complete(serve())
            finally:loop.close()
        else:
            web.run_app(create_app(store,args.codex_home,endpoint,ssl_context=context),host='127.0.0.1',port=args.port,
                        access_log=None,print=None,loop=proxy_loop())
    finally:
        if cache_lock:cache_lock.__exit__(None,None,None)
        if not store.close():
            raise SystemExit('관측 저장 종료 미완료 · 저장 대기 또는 누락 기록을 확인하세요')


if __name__=='__main__':
    main()
