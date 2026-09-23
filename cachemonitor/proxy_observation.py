"""Read only Responses metadata from streaming JSON/SSE, without changing wire bytes."""
from __future__ import annotations

import zlib
from ijson.backends import yajl2_c as json_stream
from ijson.common import JSONError
import zstandard


MAX_OBSERVATION = 128 * 1024 * 1024
CHUNK = 65536
FIELDS = {'model','stream_id','type','object','id','status','generate','stream','prompt_cache_retention',
          'service_tier','response.id','response.model','response.status','response.service_tier'}


class SelectedJSON:
    """SAX parsing keeps selected scalar fields, never whole inputs or outputs."""
    def __init__(self):
        self.fields={}
        self.error=None
        self.closed=False
        self.size=0
        self.target=self._collect();next(self.target)
        self.parser=json_stream.parse_coro(self.target)

    def _collect(self):
        while True:
            prefix,event,value=yield
            if prefix in FIELDS and event in ('string','boolean'):
                if isinstance(value,str) and len(value)>256:continue
                if prefix.startswith('response.'):
                    self.fields.setdefault('response',{})[prefix.split('.')[1]]=value
                else:self.fields[prefix]=value

    def feed(self,data):
        if not data or self.error or self.closed:return
        self.size+=len(data)
        if self.size>MAX_OBSERVATION:
            self.error='observation_limit';return
        try:self.parser.send(data)
        except (JSONError,ValueError,UnicodeError):self.error='invalid_json'

    def finish(self):
        if not self.closed:
            self.closed=True
            try:self.parser.close()
            except (JSONError,ValueError,UnicodeError):self.error=self.error or 'invalid_json'
            finally:self.target.close()
        return {} if self.error else self.fields


def message_metadata(data):
    parser=SelectedJSON()
    for offset in range(0,len(data),CHUNK):
        piece=data[offset:offset+CHUNK]
        parser.feed(piece.encode('utf-8') if isinstance(piece,str) else piece)
    return parser.finish()


class EventStream:
    """SSE framing with bounded line buffering, including split CR/LF boundaries."""
    def __init__(self,callback):
        self.callback=callback
        self.json=SelectedJSON()
        self.prefix=bytearray()
        self.field=None
        self.first_value=False
        self.line_nonempty=False
        self.had_data=False
        self.skip_lf=False

    def _piece(self,piece):
        if not piece:return
        self.line_nonempty=True
        if self.field is None:
            while piece and self.field is None:
                byte,piece=piece[0],piece[1:]
                if byte==58:
                    self.field='data' if self.prefix==b'data' else 'ignore'
                    self.first_value=True
                elif len(self.prefix)<5:self.prefix.append(byte)
                else:self.field='ignore'
        if self.field=='data':
            self.had_data=True
            if self.first_value and piece:
                if piece.startswith(b' '):piece=piece[1:]
                self.first_value=False
            self.json.feed(piece)

    def _newline(self):
        if not self.line_nonempty:
            fields=self.json.finish()
            if self.had_data and fields:self.callback(fields)
            self.json=SelectedJSON();self.had_data=False
        elif self.field=='data':self.json.feed(b'\n')
        self.prefix.clear();self.field=None;self.line_nonempty=False;self.first_value=False

    def feed(self,data):
        offset=0
        if self.skip_lf and data.startswith(b'\n'):offset=1
        self.skip_lf=False
        while offset<len(data):
            cr=data.find(b'\r',offset);lf=data.find(b'\n',offset)
            ends=[n for n in (cr,lf) if n>=0]
            if not ends:self._piece(data[offset:]);break
            end=min(ends);self._piece(data[offset:end]);self._newline()
            offset=end+1
            if data[end]==13:
                if offset==len(data):self.skip_lf=True
                elif data[offset]==10:offset+=1

    def finish(self):
        # An unterminated event is not evidence of a completed response.
        self.json.finish()


class ResponseMetadata:
    """Select JSON or SSE from the payload, not an unreliable Content-Type."""
    def __init__(self,callback):
        self.callback=callback
        self.prefix=bytearray()
        self.parser=None
        self.error=None
        self.kind=None

    def feed(self,data):
        if self.error:return
        if self.parser is not None:
            self.parser.feed(data);return
        self.prefix.extend(data)
        probe=bytes(self.prefix).lstrip(b' \t\r\n')
        if probe.startswith(b'\xef\xbb\xbf'):probe=probe[3:]
        if probe.startswith(b'{'):
            self.kind='json';self.parser=SelectedJSON()
        elif probe.startswith((b'data:',b'event:',b':',b'id:',b'retry:')):
            self.kind='sse';self.parser=EventStream(self.callback)
        elif len(self.prefix)>16:
            self.error='unknown_format';self.prefix.clear();return
        else:return
        self.parser.feed(probe);self.prefix.clear()

    def finish(self):
        if self.parser is None:return
        result=self.parser.finish()
        if self.kind=='json':
            self.error=self.parser.error
            if result:self.callback(result)


class DecodedObservation:
    """Decode only the observation copy, with bounded decompressor output chunks."""
    def __init__(self,consumer,encoding=''):
        self.consumer=consumer
        self.encoding=encoding.strip().lower()
        self.error=None
        self.size=0
        self.decoder=None
        self.closed=False
        self.result=None
        if self.encoding in ('','identity'):pass
        elif self.encoding in ('gzip','deflate'):
            self.decoder=zlib.decompressobj(31 if self.encoding=='gzip' else 15)
        elif self.encoding=='zstd':
            self.decoder=zstandard.ZstdDecompressor().stream_writer(self,write_size=CHUNK,closefd=False)
        else:self.error='unsupported_encoding'

    def write(self,data):
        self.size+=len(data)
        if self.size>MAX_OBSERVATION:raise ValueError('observation_limit')
        self.consumer.feed(data)
        return len(data)

    def feed(self,data):
        if self.error:return
        try:
            if self.decoder is None:self.write(data)
            elif self.encoding=='zstd':self.decoder.write(data)
            else:
                while data:
                    self.write(self.decoder.decompress(data,CHUNK))
                    data=self.decoder.unconsumed_tail
        except (ValueError,zlib.error,zstandard.ZstdError):
            self.error='observation_limit' if self.size>MAX_OBSERVATION else 'decode_error'

    def finish(self):
        if self.closed:return self.result
        self.closed=True
        if self.encoding=='zstd' and self.decoder is not None:
            try:self.decoder.close()
            except (ValueError,zstandard.ZstdError):self.error=self.error or 'decode_error'
        if self.encoding in ('gzip','deflate') and self.decoder is not None and not self.decoder.eof:
            self.error=self.error or 'truncated_encoding'
        result=self.consumer.finish()
        self.result={} if self.error else result
        return self.result
