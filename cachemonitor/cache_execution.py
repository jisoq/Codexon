"""Independent cache requests. Never invoke turn/start or mutate a Codex home.

The owner runs on the relay event loop. ingress() must precede any await on the
user path. A sent request is never retried, including after loss of its response.
"""
import asyncio
import copy
import hashlib
import json
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import contextmanager,ExitStack
from pathlib import Path

import aiohttp

from .core import usage_values
from .pricing import token_cost
from .cache_audit import TTL_SECONDS


class RequestFailure(ConnectionError):
    def __init__(self,transport):
        super().__init__(transport['failure'])
        self.transport=transport


def error_evidence(raw,headers,body,*,http_status=None,response_headers=None,read_error=None,truncated=False):
    """Bounded error evidence only. Request values are used in memory to redact echoes.

    JSON bodies retain diagnostic fields, not arbitrary server/request payloads.
    Missing fields and incomplete reads are independent of unknown token usage.
    """
    fields=('code','type','param','detail','message')
    private=[]
    def strings(value):
        if isinstance(value,str):yield value
        elif isinstance(value,dict):
            for v in value.values():yield from strings(v)
        elif isinstance(value,list):
            for v in value:yield from strings(v)
    private.extend(str(v) for k,v in headers.items() if k.lower() not in ('content-type','accept','connection','content-length'))
    for k in ('input','instructions','tools','prompt_cache_key','previous_response_id'):
        private.extend(strings(body.get(k)))
    # Split long prose as well as matching complete values: an error may echo
    # only one line/token, or end midway through a credential at the read limit.
    secrets=set()
    for value in private:
        secrets.add(value)
        secrets.update(re.findall(r'[\w+/=@.:-]{4,}',value))
    secrets.discard('')
    def clean(value,*,identifier=False):
        value=str(value)
        for secret in private:
            if not secret or len(secret)>len(value):continue
            if len(secret)<4:
                value=re.sub(r'(?<!\w)'+re.escape(secret)+r'(?!\w)','[redacted]',value)
            else:
                value=value.replace(secret,'[redacted]')
                value=value.replace(json.dumps(secret)[1:-1],'[redacted]')
        if not (identifier and re.fullmatch(r'[A-Za-z0-9_.\[\]-]{1,128}',value)):
            value=re.sub(r'[\w+/=@.:-]{4,}',lambda m:'[redacted]' if m[0] in secrets else m[0],value)
            tail=re.search(r'[\w+/=@.:-]{4,}$',value)
            if tail and any(s.startswith(tail[0]) for s in secrets):value=value[:tail.start()]+'[redacted]'
        value=re.sub(r'(?i)\bBearer\s+[^\s,;"\']+','Bearer [redacted]',value)
        value=re.sub(r'\b(?:sk-[\w-]+|eyJ[\w.-]+)','[redacted]',value)
        value=re.sub(r'(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|cookie|password)\s*[=:]\s*[^\r\n,;}]+',r'\1=[redacted]',value)
        # Unparseable request-shaped payloads cannot safely be retained as text.
        value=re.sub(r'(?is)["\']?(?:input|instructions|messages|tools|content|prompt)["\']?\s*:\s*.*','[request payload omitted]',value)
        return value
    text=raw.decode('utf-8',errors='replace')
    result=dict(http_status=http_status,request_ids={},read_error=read_error,
                body_truncated=truncated,body_complete=not truncated and read_error is None,
                body_bytes=len(raw),decode_replaced='\ufffd' in text)
    for k,v in (response_headers or {}).items():
        if k.lower() in ('x-request-id','request-id','openai-request-id','x-amzn-requestid','cf-ray'):
            result['request_ids'][k.lower()]=clean(v)[:256]
    diagnostics=[];omitted=False
    def project(value,depth=0):
        nonlocal omitted
        if depth>12:omitted=True;return None
        if isinstance(value,dict):
            out={}
            for k,v in value.items():
                if k in fields and (v is None or isinstance(v,(str,int,float,bool))):
                    sanitized=clean(v,identifier=k in ('code','type','param')) if v is not None else None
                    if sanitized and len(sanitized)>1024:omitted=True
                    out[k]=sanitized[:1024] if sanitized is not None else None
                    diagnostics.append((k,out[k]))
                elif k in ('request_id','requestId') and isinstance(v,str):
                    out[k]=clean(v)[:256];result['request_ids'].setdefault(k,out[k])
                elif k.lower() in ('input','instructions','tools','messages','headers','authorization','request','body','output','content'):
                    omitted=True
                elif isinstance(v,(dict,list)):
                    child=project(v,depth+1)
                    if child:out[clean(k)[:64]]=child
                else:omitted=True
            return out
        if isinstance(value,list):
            if len(value)>32:omitted=True
            return [x for v in value[:32] if (x:=project(v,depth+1))]
        if isinstance(value,str):return clean(value)[:2048]
        return None
    try:
        parsed=json.loads(text)
        projected=project(parsed)
        preview=json.dumps(projected,ensure_ascii=False) if projected is not None else ''
        result['body_format']='json'
    except (ValueError,RecursionError):
        preview=clean(text)
        result['body_format']='invalid_json' if text.lstrip().startswith(('{','[')) else 'text' if text else 'empty'
    result.update(body=preview[:2048],body_omitted=omitted,preview_truncated=len(preview)>2048,
                  fields=[dict(field=k,value=v) for k,v in diagnostics][:32],
                  fields_truncated=len(diagnostics)>32,
                  missing_fields=[k for k in fields if not any(n==k for n,v in diagnostics)])
    return result


def terminal_response(response,headers,body,http_status,response_headers):
    transport=error_evidence(b'',headers,body,http_status=http_status,response_headers=response_headers)
    if response.get('error') or response.get('status')=='failed':
        raw=json.dumps({'error':response.get('error')}).encode()
        transport=error_evidence(raw[:16384],headers,body,http_status=http_status,
            response_headers=response_headers,truncated=len(raw)>16384)
        transport.update(failure='maintenance_rejected',rejected=True)
    transport['response_status']=response.get('status')
    return dict(response,_transport=transport)


async def http_failure(response,headers,body):
    raw=bytearray();limit=16384;read_error=None
    try:
        while len(raw)<=limit:
            chunk=await response.content.read(min(4096,limit+1-len(raw)))
            if not chunk:break
            raw.extend(chunk)
    except (asyncio.TimeoutError,asyncio.CancelledError):read_error='timeout'
    except Exception as exc:read_error=type(exc).__name__
    evidence=error_evidence(bytes(raw[:limit]),headers,body,http_status=response.status,
        response_headers=response.headers,read_error=read_error,truncated=len(raw)>limit)
    raise RequestFailure(dict(evidence,failure='maintenance_http_'+str(response.status),rejected=True))


@contextmanager
def execution_owner(path,*,proxy_owned=False):
    """One lifetime owner per journal, including transitions from older workers.

    Both paths take cache-worker.lock. Taking the legacy supervisor lock first
    also prevents a new standalone worker recovering an older managed relay's
    sent requests. A managed relay already holds that outer lifetime lock.
    """
    from .observer_state import ProcessLock
    directory=Path(path).resolve().parent
    with ExitStack() as locks:
        if not proxy_owned:locks.enter_context(ProcessLock(directory/'proxy-supervisor.lock'))
        locks.enter_context(ProcessLock(directory/'cache-worker.lock'))
        yield


class Contexts:
    def __init__(self, limit=32*1024*1024):
        self.limit=limit
        self.responses=OrderedDict()
        self.usage={}
        self.size=0

    def full(self, body):
        body=copy.deepcopy(body)
        previous=body.pop('previous_response_id',None)
        if previous:
            prior=self.responses.get(previous)
            if prior is None:
                raise ValueError('incomplete_response_chain')
            request,output,_=prior
            if any(body.get(k)!=request.get(k) for k in ('model','instructions','tools','reasoning','prompt_cache_key')):
                raise ValueError('changed_response_chain')
            body['input']=copy.deepcopy(request['input'])+copy.deepcopy(output)+body.get('input',[])
        if not isinstance(body.get('input'),list) or not body.get('model'):
            raise ValueError('incomplete_context')
        return body

    def completed(self, request, response):
        if response.get('status')!='completed' or not response.get('id') or not isinstance(response.get('output'),list):
            raise ValueError('incomplete_response')
        full=self.full(request)
        output=copy.deepcopy(response['output'])
        size=len(json.dumps([full,output]).encode())
        if size>self.limit:
            raise ValueError('context_limit')
        rid=response['id']
        if rid in self.responses:
            self.size-=self.responses.pop(rid)[2]
        while self.responses and self.size+size>self.limit:
            old,item=self.responses.popitem(last=False)
            self.size-=item[2];self.usage.pop(old,None)
        self.responses[rid]=(full,output,size)
        self.usage[rid]=usage_values(response.get('usage') or {})
        self.size+=size

    def maintenance(self, rid):
        request,output,_=self.responses[rid]
        body=copy.deepcopy(request)
        body['input']+=copy.deepcopy(output)
        body['input'].append({'role':'user','content':[{'type':'input_text','text':'Reply exactly ACK. Do not call tools.'}]})
        # Definitions, order, schemas, instructions and reasoning stay identical.
        body['tool_choice']='none'
        body.pop('generate',None)
        return body

    def clear(self):
        self.responses.clear()
        self.usage.clear()
        self.size=0


class Journal:
    def __init__(self,path,*,timeout=5):
        from .cache_db import connect
        self.db=connect(path,timeout)
        from .cache_operating import Operations
        self.operations=Operations(self)

    def reserve(self,home,sid,generation,body,snapshot,round_number=0,operation=None):
        if type(round_number) is not int or round_number<0:raise ValueError('invalid_round')
        key=hashlib.sha256(json.dumps([home,sid,snapshot,round_number]).encode()).hexdigest()
        if round_number:
            prior=self.db.execute('SELECT state,scope_read_lower,generation FROM cache_jobs WHERE home=? AND sid=? AND snapshot=? AND round=?',
                                  (home,sid,snapshot,round_number-1)).fetchone()
            if not prior or prior[0]!='completed' or not prior[1] or prior[2]!=generation:return None
        try:
            if operation:
                self.db.execute('BEGIN IMMEDIATE')
                reason=self.operations.check(operation)
                if reason:
                    self.db.execute('COMMIT');return None
            self.db.execute('INSERT INTO cache_jobs(id,home,sid,generation,state,model,effort,tier,snapshot,round) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key,home,sid,generation,'reserved',body['model'],
                 (body.get('reasoning') or {}).get('effort'),body.get('service_tier') or (operation['scope']['tier'] if operation else None),snapshot,round_number))
            if operation:
                self.db.execute('UPDATE cache_jobs SET purpose=? WHERE id=?',(operation.get('purpose','maintenance'),key))
                self.db.execute('UPDATE cache_jobs SET operation=?,expected_cost=?,adverse_cost=?,output_high=?,read_required=? WHERE id=?',
                    (operation['id'],operation['expected'],operation['adverse'],operation['output_high'],operation.get('read_required',0),key))
                self.db.execute('COMMIT')
        except sqlite3.IntegrityError:
            if operation and self.db.in_transaction:self.db.execute('ROLLBACK')
            return None
        except BaseException:
            if operation and self.db.in_transaction:self.db.execute('ROLLBACK')
            raise
        return key

    def permit_operation(self,key,operation):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            reason=self.operations.check(operation,exclude=key)
            allowed=not reason and self.sent(key)
            self.db.execute('COMMIT');return allowed
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def sent(self,key):
        return self.db.execute("UPDATE cache_jobs SET state='sent',started=? WHERE id=? AND state='reserved'",
                               (time.time(),key)).rowcount==1

    def has_round(self,home,sid,snapshot,round_number):
        key=hashlib.sha256(json.dumps([home,sid,snapshot,round_number]).encode()).hexdigest()
        legacy=hashlib.sha256(json.dumps([home,sid,snapshot]).encode()).hexdigest()
        return self.db.execute('SELECT 1 FROM cache_jobs WHERE id IN (?,?)',(key,legacy)).fetchone() is not None

    def finish(self,key,state,response=None,*,anchor=None,scope_read_lower=None,transport=None):
        response=response or {}
        usage=response.get('usage')
        # Store token allowlist and already sanitized transport evidence only.
        clean=usage_values(usage) if isinstance(usage,dict) else None
        # Completion releases the shared reservation and records any stop in ONE
        # transaction, so another connection cannot start before reconciliation.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('UPDATE cache_jobs SET state=?,ended=?,response_id=?,usage=?,anchor=?,scope_read_lower=?,transport=? WHERE id=?',
                (state,time.time(),response.get('id'),json.dumps(clean) if clean is not None else None,anchor,scope_read_lower,
                 json.dumps(transport) if transport is not None else None,key))
            self.operations.reconcile(key)
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise

    def recover_exclusive(self):
        """Only the lifetime execution_owner may call this at startup."""
        self.db.execute("UPDATE cache_jobs SET state='unknown' WHERE state='sent'")
        self.db.execute("UPDATE cache_jobs SET state='cancelled' WHERE state='reserved'")
        for key, in self.db.execute("SELECT id FROM cache_jobs WHERE state='unknown' AND operation IS NOT NULL").fetchall():
            self.operations.reconcile(key)

    def rows(self):
        rows=[]
        for key,home,sid,state,start,end,rid,model,effort,tier,usage,snapshot,round_number,anchor,scope,operation,purpose,transport in self.db.execute(
                'SELECT id,home,sid,state,started,ended,response_id,model,effort,tier,usage,snapshot,round,anchor,scope_read_lower,operation,purpose,transport FROM cache_jobs WHERE started IS NOT NULL ORDER BY started,id'):
            row=dict(key=rid or 'maintenance:'+key,job_id=key,operation=operation,home=home,sid=sid,purpose=purpose or 'maintenance',
                     ts=end or start,request_start=start,request_end=end,model=model,effort=effort,
                     service_tier=tier or '미확인',state=state,usage_known=usage is not None,
                     snapshot=snapshot,round=round_number,anchor=anchor,scope_read_lower=scope,
                     transport=json.loads(transport) if transport else None)
            row.update(json.loads(usage) if usage else usage_values({}))
            row['usage_known']=all(type(row.get(k)) is int and row[k]>=0 for k in ('input','cached','output'))
            row.update(token_cost(row))
            rows.append(row)
        return rows

    def close(self):
        self.db.close()


async def request_once(url, headers, body, *, websocket=False, timeout=30, permit=lambda:True):
    """Independent transport, no redirects, retries, cookies or tool dispatcher."""
    body=copy.deepcopy(body)
    connection_tokens={x.strip().lower() for k,v in headers.items() if k.lower()=='connection' for x in v.split(',')}
    excluded={'host','content-length','content-type','content-encoding','transfer-encoding','connection','upgrade',
              'keep-alive','proxy-authorization','proxy-authenticate','te','trailer','expect'}|connection_tokens
    headers={k:v for k,v in headers.items() if k.lower() not in excluded and not k.lower().startswith('sec-websocket-')}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout),
                                    cookie_jar=aiohttp.DummyCookieJar(),trust_env=False) as client:
        client._retry_connection=False
        if websocket:
            body.pop('stream',None)
            body['type']='response.create'
            async with client.ws_connect(url,headers=headers,max_msg_size=32*1024*1024) as ws:
                if not permit():
                    raise ConnectionError('maintenance_invalidated_before_body')
                await ws.send_json(body)
                async for msg in ws:
                    if msg.type!=aiohttp.WSMsgType.TEXT:
                        raise ConnectionError('maintenance_disconnected')
                    event=json.loads(msg.data)
                    if event.get('type') in ('response.completed','response.failed','response.incomplete'):
                        return terminal_response(event['response'],headers,body,101,{})
                    if event.get('type')=='error':
                        raise RequestFailure(dict(error_evidence(msg.data.encode()[:16384],headers,body,http_status=101,
                            truncated=len(msg.data.encode())>16384),failure='maintenance_rejected',rejected=True))
        else:
            body.pop('type',None)
            body['stream']=True
            async def upload():
                # Evaluated after connection establishment, immediately before body delivery.
                if not permit():
                    raise ConnectionError('maintenance_invalidated_before_body')
                yield json.dumps(body).encode()
            headers={**headers,'Content-Type':'application/json'}
            async with client.post(url,headers=headers,data=upload(),allow_redirects=False) as response:
                if response.status!=200:
                    await http_failure(response,headers,body)
                data=[]
                async for line in response.content:
                    line=line.rstrip(b'\r\n')
                    if line.startswith(b'data:'):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        event=json.loads(b'\n'.join(data));data=[]
                        if event.get('type') in ('response.completed','response.failed','response.incomplete'):
                            return terminal_response(event['response'],headers,body,response.status,response.headers)
                        if event.get('type')=='error':
                            raw=json.dumps(event).encode()
                            raise RequestFailure(dict(error_evidence(raw[:16384],headers,body,http_status=response.status,
                                response_headers=response.headers,truncated=len(raw)>16384),failure='maintenance_rejected',rejected=True))
    raise ConnectionError('maintenance_missing_terminal')


class Executor:
    def __init__(self,journal,contexts,send=request_once,*,observation_only=False):
        self.journal,self.contexts,self.send=journal,contexts,send
        self.observation_only=observation_only
        self.generation=0
        self.busy=0
        self.closed=False
        self.renewals={}

    def ingress(self):
        self.generation+=1
        self.busy+=1
        # Never cancel a user's transport. An independent sent request drains.
        return self.generation

    def leave(self):
        self.busy=max(0,self.busy-1)

    async def run(self,home,sid,rid,url,headers,*,anchor,deadline,latency_bound,websocket=False,round_number=0,max_output_tokens=None,operation=None,valid=lambda:True,expected_generation=None,observed_tier=None):
        if self.observation_only:return 'observation_only'
        generation=self.generation if expected_generation is None else expected_generation
        if self.closed:return 'invalidated'
        if self.journal.has_round(home,sid,rid,round_number):return 'duplicate'
        renewal=self.renewals.get((home,sid,rid))
        if round_number:
            if not renewal or renewal['round']!=round_number-1 or renewal['generation']!=generation:return 'invalidated'
            anchor=renewal['anchor']
        remaining=deadline-time.monotonic()
        if remaining>0:
            await asyncio.sleep(remaining)
        age=time.monotonic()-anchor
        if (self.closed or self.busy or not valid() or generation!=self.generation or age<0 or
                age+latency_bound>=TTL_SECONDS):
            return 'invalidated'
        body=self.contexts.maintenance(rid)
        if operation:
            from .cache_operating import target
            if target(home,body,url,headers,websocket,observed_tier=observed_tier)!=operation['scope']:return 'scope_mismatch'
            operation=dict(operation,read_required=self.contexts.usage.get(rid,{}).get('cached') or 0)
            # Permission to bear risk is not evidence of a server capability.
            body.pop('max_output_tokens',None)
            if self.journal.operations.check(operation):return 'operation_deferred'
        elif max_output_tokens is not None:
            if type(max_output_tokens) is not int or max_output_tokens<=0:raise ValueError('invalid_output_bound')
            body['max_output_tokens']=max_output_tokens
        key=self.journal.reserve(home,sid,generation,body,rid,round_number,operation)
        if key is None:
            return 'operation_deferred' if operation else 'duplicate'
        # No await between final invalidation check and durable send intent.
        if self.closed or self.busy or generation!=self.generation:
            self.journal.finish(key,'cancelled');return 'invalidated'
        if not operation and not self.journal.sent(key):
            return 'invalidated'
        try:
            sent_anchor=time.monotonic()
            # Retain numeric provenance even if disabling capture clears Contexts.
            original=dict(self.contexts.usage.get(rid,{}))
            def permit():
                if self.observation_only:return False
                if self.closed or self.busy or generation!=self.generation or not valid():return False
                return self.journal.permit_operation(key,operation) if operation else True
            transport=asyncio.create_task(asyncio.wait_for(self.send(url,headers,body,websocket=websocket,timeout=latency_bound,
                permit=permit),latency_bound))
            while True:
                try:
                    response=await asyncio.shield(transport)
                    break
                except asyncio.CancelledError:
                    # Repeated invalidation only cancels scheduling. A sent
                    # transport owns its deadline and must not lose recoverable usage.
                    if transport.cancelled():raise
                    continue
            state=('failed' if (response.get('_transport') or {}).get('rejected') else
                   'completed' if response.get('status')=='completed' and isinstance(response.get('usage'),dict) else 'unknown')
            usage=usage_values(response.get('usage') or {})
            # Inclusion/exclusion lower bound: deduct every token outside original
            # input before attributing any read to that unchanged input. Not full renewal.
            i,c,o=usage.get('input'),usage.get('cached'),original.get('input')
            lower=max(0,c-max(0,i-o)) if all(type(v) is int for v in (i,c,o)) and 0<=c<=i and o<=i else None
            self.journal.finish(key,state,response,anchor=time.time()-(time.monotonic()-sent_anchor),scope_read_lower=lower,
                                transport=response.get('_transport'))
            if state=='completed' and lower and generation==self.generation:
                self.renewals[home,sid,rid]=dict(round=round_number,anchor=sent_anchor,read_lower=lower,generation=generation)
            # Original response chain remains untouched. Unknown or zero overlap cannot renew.
            return state
        except (Exception,asyncio.CancelledError) as exc:
            started=self.journal.db.execute('SELECT started FROM cache_jobs WHERE id=?',(key,)).fetchone()[0]
            evidence=exc.transport if isinstance(exc,RequestFailure) else dict(failure=type(exc).__name__)
            state='failed' if started is not None and evidence.get('rejected') else 'unknown' if started is not None else 'cancelled'
            self.journal.finish(key,state,transport=evidence)
            if operation and started is None and not self.closed and generation==self.generation and valid():
                self.journal.operations.stop(operation['id'],'request_failed')
            return 'failed' if state=='failed' else 'unknown'

    def close(self):
        self.closed=True
        self.generation+=1
        self.contexts.clear()
