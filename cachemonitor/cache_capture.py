"""Bounded optional wire copy. Capture failure must not change forwarded bytes."""
import json
import time
from collections import defaultdict


class Capture:
    def __init__(self,limit=32*1024*1024):
        self.limit=limit
        self.data=bytearray()
        self.failed=False

    def feed(self,data):
        if self.failed:return
        if len(self.data)+len(data)>self.limit:
            self.failed=True;self.data.clear();return
        self.data.extend(data)

    def json(self):
        if self.failed:return None
        try:
            value=json.loads(self.data)
            return value if isinstance(value,dict) else None
        except (ValueError,UnicodeError):return None

    def response(self):
        value=self.json()
        if value and value.get('object')=='response':return value
        if self.failed:return None
        terminal=None
        output=[]
        data=[]
        for line in bytes(self.data).replace(b'\r\n',b'\n').split(b'\n'):
            if line.startswith(b'data:'):data.append(line[5:].lstrip())
            elif not line and data:
                try:value=json.loads(b'\n'.join(data))
                except (ValueError,UnicodeError):return None
                data=[]
                if value.get('type')=='response.output_item.done':output.append(value['item'])
                if value.get('type')=='response.completed':terminal=value.get('response')
        if terminal and 'output' not in terminal:terminal['output']=output
        return terminal


class RelayCapture:
    """Optional create_app adapter; callbacks run on the forwarding event loop.

    A policy owner can schedule Executor.run only from completed snapshots. It
    receives the request-start monotonic anchor, never the completion timestamp.
    """
    def __init__(self,executor,on_snapshot=lambda *args:None,enabled=lambda:True):
        self.executor=executor
        self.on_snapshot=on_snapshot
        self.enabled=enabled

    def begin(self,headers,url,clock):
        generation=self.executor.ingress()
        record=dict(headers=dict(headers),url=str(url),anchor=clock,generation=generation,request=Capture(),response=Capture())
        try:enabled=self.enabled()
        except Exception:enabled=False
        if not enabled:
            record['request'].failed=True;record['response'].failed=True
        return record

    def finish(self,record,*,websocket=False):
        try:
            request=record['request'].json();response=record['response'].response()
            if request and response:
                self.executor.contexts.completed(request,response)
                self.on_snapshot(request,response,record['anchor'],record['url'],record['headers'],websocket,record['generation'])
        except Exception:
            pass
        finally:self.executor.leave()


class WebSocketCapture:
    """Bind at response.created using the relay's pending attempt identity.

    Ambiguous starts and cancellation disable capture for this connection only.
    Wire forwarding and the metadata tracker remain independent.
    """
    def __init__(self,adapter,tracker,headers,url):
        self.adapter,self.tracker,self.headers,self.url=adapter,tracker,headers,url
        self.pending={};self.active={};self.retired=set();self.disabled=False;self.blocked=False

    def disable(self):
        if not self.disabled:
            # Correlation is lost: conservatively hold an idle veto until this
            # user connection closes, while forwarding all its traffic unchanged.
            self.adapter.executor.ingress();self.blocked=True
        for record in [*self.pending.values(),*self.active.values()]:
            record['request'].failed=True
            self.adapter.finish(record,websocket=True)
        self.pending.clear();self.active.clear();self.disabled=True

    def close(self):
        self.disable()
        if self.blocked:self.adapter.executor.leave();self.blocked=False

    def outgoing(self,obj,raw):
        if self.disabled:return
        if obj.get('type')=='response.cancel':self.disable();return
        if obj.get('type')!='response.create':return
        lane=str(obj.get('stream_id') or '')
        queue=self.tracker.pending[lane]
        if len(queue)!=1:self.disable();return
        record=self.adapter.begin(self.headers,self.url,time.monotonic())
        record['request'].feed(raw)
        self.pending[queue[-1]['attempt']]=record

    def incoming(self,obj,raw):
        if self.disabled:return
        response=obj.get('response') or {};rid=response.get('id') or obj.get('response_id')
        typ=obj.get('type')
        if typ=='error':self.disable();return
        if rid in self.retired:return
        if typ=='response.created':
            lane=str(obj.get('stream_id') or '')
            queue=self.tracker.pending[lane]
            if not rid or rid in self.active or len(queue)!=1:self.disable();return
            record=self.pending.pop(queue[0]['attempt'],None)
            if record is None:self.disable();return
            self.active[rid]=record
        if not rid and len(self.active)==1:rid=next(iter(self.active))
        if rid not in self.active:
            # Unbound output cannot be used to reconstruct any response.
            if typ in ('response.output_item.done','response.completed'):self.disable()
            return
        record=self.active[rid]
        record['response'].feed(b'data: '+raw+b'\n\n')
        if typ in ('response.completed','response.failed','response.incomplete'):
            self.active.pop(rid);self.retired.add(rid)
            self.adapter.finish(record,websocket=True)
            if len(self.retired)>2048:self.disable()
