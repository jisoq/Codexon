"""Incremental projection of JSONL events without retaining message bodies."""
import json
import re
from .native_backend import get_native

SPECIAL = re.compile(br'["\\]')
DELIMITER = re.compile(br'[\s{}\[\],:"]')
USAGE = {key: True for key in ('input_tokens','cached_input_tokens','cache_write_input_tokens',
    'output_tokens','reasoning_output_tokens','total_tokens')}
USAGE.update(input_tokens_details={'cached_tokens':True,'cache_write_tokens':True},
             output_tokens_details={'reasoning_tokens':True})
WINDOW = {key: True for key in ('used_percent','resets_at','window_minutes')}
SCHEMA = {'type':True,'timestamp':True,'payload':{
    **{key:True for key in ('id','cwd','source','type','turn_id','thread_id','response_id','model',
        'effort','reasoning_effort','service_tier','parent_thread_id','agent_path','agent_nickname','spawn_depth')},
    'usage':USAGE,'thread_token_usage':USAGE,
    'collaboration_mode':{'settings':{'reasoning_effort':True}},
    'thread_settings':{'service_tier':True},
    'info':{'total_token_usage':USAGE,'last_token_usage':USAGE},
    'rate_limits':{'limit_id':True,'plan_type':True,'primary':WINDOW,'secondary':WINDOW,
                   'unlimited_windows':True}}}


class EventProjection:
    # Selected scalar fields are metadata, never conversation text. Explicitly
    # fail oversized metadata rather than silently truncating an event.
    MAX_SELECTED = 16 * 1024 * 1024
    MAX_DEPTH = 128

    def __init__(self):
        self.stack=[];self.result=None;self.done=False
        self.string=False;self.escape=False;self.token=None
        self.capture=False;self.key_token=False;self.primitive=False
        self.selected_bytes=0

    def schema(self):
        if not self.stack:
            if self.done:raise ValueError('multiple JSON values')
            return SCHEMA
        frame=self.stack[-1]
        if frame['phase'] not in ('value','first'):raise ValueError('JSON value expected')
        schema=frame['schema']
        if frame['kind']=='[':return schema
        return schema.get(frame['key']) if isinstance(schema,dict) else schema

    def accept(self,value):
        if not self.stack:self.result=value;self.done=True;return
        frame=self.stack[-1]
        if frame['value'] is not None:
            if frame['kind']=='{':
                if frame['key'] is not None and (frame['schema'] is True or frame['key'] in frame['schema']):
                    frame['value'][frame['key']]=value
            elif value in ('weekly','five_hour') and value not in frame['value']:
                frame['value'].append(value)
        frame['phase']='comma'

    def append(self,part):
        if not self.capture:return
        # An unknown long object key cannot match the projection schema.
        if self.key_token and len(self.token)+len(part)>256:
            self.capture=False;self.token=None;return
        if not self.key_token:self.selected_bytes+=len(part)
        if self.selected_bytes>self.MAX_SELECTED:raise ValueError('수집 메타데이터가 16 MiB 상한을 초과했습니다')
        self.token.extend(part)

    def finish_token(self):
        value=json.loads(self.token) if self.capture else None
        if self.key_token:
            frame=self.stack[-1];frame['key']=value;frame['phase']='colon'
        else:self.accept(value)
        self.token=None;self.string=False;self.primitive=False

    def feed(self,data):
        at=0
        while at<len(data):
            if self.string:
                if self.escape:
                    self.append(data[at:at+1]);at+=1;self.escape=False;continue
                match=SPECIAL.search(data,at)
                end=match.start() if match else len(data)
                self.append(data[at:end]);at=end
                if match is None:break
                char=data[at];self.append(data[at:at+1]);at+=1
                if char==92:self.escape=True
                else:self.finish_token()
                continue
            if self.primitive:
                match=DELIMITER.search(data,at)
                end=match.start() if match else len(data)
                self.append(data[at:end]);at=end
                if match is None:break
                self.finish_token();continue
            char=data[at];at+=1
            if char in b' \t\r':continue
            if char in (123,91):
                schema=self.schema();kind=chr(char)
                if len(self.stack)>=self.MAX_DEPTH:raise ValueError('JSON nesting limit')
                self.stack.append(dict(kind=kind,schema=schema,value=({} if kind=='{' else []) if schema else None,
                                       phase='key_first' if kind=='{' else 'first',key=None))
            elif char in (125,93):
                if not self.stack:raise ValueError('unexpected JSON closing delimiter')
                frame=self.stack.pop()
                if frame['kind']!=('{' if char==125 else '[') or frame['phase'] not in ('key_first','first','comma'):
                    raise ValueError('incomplete JSON container')
                self.accept(frame['value'])
            elif char==58:
                if not self.stack or self.stack[-1]['phase']!='colon':raise ValueError('unexpected colon')
                self.stack[-1]['phase']='value'
            elif char==44:
                if not self.stack or self.stack[-1]['phase']!='comma':raise ValueError('unexpected comma')
                self.stack[-1]['phase']='key' if self.stack[-1]['kind']=='{' else 'value'
            else:
                self.key_token=bool(self.stack and self.stack[-1]['kind']=='{' and self.stack[-1]['phase'] in ('key','key_first'))
                if self.key_token and char!=34:raise ValueError('object key expected')
                self.capture=self.key_token or bool(self.schema())
                self.token=bytearray();self.append(bytes((char,)))
                self.string=char==34;self.primitive=not self.string

    def finish(self):
        if self.primitive:self.finish_token()
        if self.string or self.stack or not self.done:raise ValueError('incomplete JSON event')
        return self.result


class EventLines:
    CHUNK = 16 * 1024 * 1024
    MARKERS = (b'"turn_context"',b'"token_usage_record"',b'"event_msg"',b'"compacted"',b'"session_meta"')

    def __init__(self,offset,digest):
        self.read_offset=offset;self.committed=offset;self.line_start=offset
        self.digest=digest.copy();self.committed_digest=digest.copy()
        self.prefix=bytearray();self.selected=None;self.projection=None;self.error=None
        self.header_only=False
        self.bytes_read=0
        self.event_end=None

    def feed(self,part,complete):
        if self.selected is None:
            count=min(250-len(self.prefix),len(part))
            self.prefix.extend(part[:count]);part=part[count:]
            if len(self.prefix)==250 or complete:
                selected=any(marker in self.prefix for marker in self.MARKERS)
                self.header_only=self.line_start==0 and not selected
                self.selected=selected or self.header_only
                if self.selected:
                    self.projection=EventProjection();self.projection.feed(bytes(self.prefix))
                self.prefix.clear()
        if self.projection is not None:self.projection.feed(part)

    def read(self,source,limit):
        source.seek(self.read_offset)
        left=limit
        native=get_native()
        try:
            while left:
                block=source.read(min(self.CHUNK,left))
                if not block:break
                self.bytes_read+=len(block);left-=len(block)
                while block:
                    if native is not None and self.selected is None and not self.prefix:
                        consumed,selected=native.scan_event_lines(block)
                        if consumed:
                            start=self.read_offset
                            if start==0 and (not selected or selected[0][0]!=0):
                                end=block.find(b'\n')+1
                                try:header=json.loads(block[:end])
                                except (ValueError,UnicodeError):header=None
                                if isinstance(header,dict) and header.get('type')=='session_meta':selected.insert(0,(0,end))
                            self.digest.update(memoryview(block)[:consumed]);self.read_offset+=consumed
                            self.committed=self.line_start=self.read_offset;self.committed_digest=self.digest.copy()
                            for begin,end in selected:
                                self.event_end=start+end
                                yield start+begin,block[begin:end]
                            block=block[consumed:]
                            continue
                    end=block.find(b"\n")+1
                    if not end:end=len(block)
                    part,block=block[:end],block[end:]
                    # JSONL separators are LF; CR alone is ordinary whitespace.
                    complete=part.endswith(b'\n')
                    direct=complete and self.selected is None and not self.prefix
                    self.digest.update(part);self.read_offset+=len(part)
                    try:
                        if not direct:self.feed(part[:-1] if complete else part,complete)
                    except (ValueError,TypeError,UnicodeError) as error:self.error=error;self.projection=None
                    if complete:
                        event=part if direct and any(marker in part[:250] for marker in self.MARKERS) else None
                        if direct and self.line_start==0 and event is None:
                            try:header=json.loads(part)
                            except (ValueError,UnicodeError):header=None
                            if isinstance(header,dict) and header.get('type')=='session_meta':event=part
                        try:
                            if self.error:raise self.error
                            if self.projection is not None:event=self.projection.finish()
                        except (ValueError,TypeError,UnicodeError) as error:event=error
                        if self.header_only and (not isinstance(event,dict) or event.get('type')!='session_meta'):event=None
                        position=self.line_start
                        self.committed=self.read_offset;self.committed_digest=self.digest.copy()
                        self.line_start=self.read_offset;self.prefix.clear();self.selected=None;self.projection=None;self.error=None;self.header_only=False
                        if event is not None:
                            if isinstance(event,Exception):event=b'{'
                            elif not isinstance(event,bytes):event=json.dumps(event,ensure_ascii=False).encode('utf-8')
                            self.event_end=self.committed
                            yield position,event
        finally:source.seek(self.committed)
