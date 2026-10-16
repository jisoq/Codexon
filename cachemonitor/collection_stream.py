"""Stream one committed collector owner into unpublished prepared staging."""
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import json
import re
import sqlite3
import time
import zlib

from ijson.backends import yajl2_c as json_stream
from ijson.common import JSONError, ObjectBuilder

from .usage_collection import CollectionCatalogUnavailable, CollectionScopeError, OWNER_FIELDS


CHUNK_BYTES=64*1024
TOKEN_BYTES=4*1024*1024
RECORD_BYTES=32*1024*1024
MAX_DEPTH=64
LEASE_SECONDS=30.
WAL_GROWTH_BYTES=64*1024*1024
WAL_BYTES=256*1024*1024
_STRING_SPECIAL=re.compile(rb'["\\]')
_DELIMITER=re.compile(rb'[ \t\r\n,{}\[\]:"]')
_TOKEN_START=re.compile(rb'[^ \t\r\n,{}\[\]:]')
_NONFINITE={b'NaN':float('nan'),b'Infinity':float('inf'),b'-Infinity':float('-inf')}
MATERIAL_BYTES=2*1024*1024
# JSON trees are admitted at 96 bytes per input byte, including decoder memo
# storage. Six more bytes cover raw storage and the temporary Unicode input.
# Chunk/decompressor scratch is reserved separately within the same allowance.
_MATERIAL_EXPANSION=102
_MATERIAL_SCRATCH=8*CHUNK_BYTES
_MATERIAL_MARKER=re.compile(rb'["{}\[\]]|(?<![^ \t\r\n,{}\[\]:"])[^ \t\r\n,{}\[\]:"]{8193}')


def _material_admitted(raw,check):
    """Conservative admission only; the JSON decoder still validates grammar."""
    if raw.startswith(b'\xef\xbb\xbf'):return False
    stack=[];position=0;next_check=0;length=len(raw)
    while True:
        found=_MATERIAL_MARKER.search(raw,position)
        if found is None:break
        start=found.start();byte=raw[start]
        if start>=next_check:check();next_check=start+CHUNK_BYTES
        if byte==34:
            end=raw.find(b'"',start+1)
            while end>=0:
                back=end-1
                while back>start and raw[back]==92:back-=1
                if (end-1-back)%2==0:break
                end=raw.find(b'"',end+1)
            if end<0:return False
            if end-start+1>TOKEN_BYTES:raise CollectionCatalogUnavailable('json_token_budget')
            # YAJL's handling of escaped surrogate code points remains owned
            # by the original parser. Ordinary UTF-8 strings use this path.
            if raw.find(b'\\u',start,end)>=0:return False
            position=end+1
            after=position
            while after<length and raw[after] in b' \t\r\n':after+=1
            if after<length and raw[after]==58 and stack and stack[-1][2] is not None:
                key=json.loads(raw[start:position]);seen=stack[-1][2]
                if key in seen:
                    raise ValueError('source repeats a top-level field' if len(stack)==1 else 'source repeats a map member')
                seen.add(key)
            continue
        if byte in (123,91):
            stack.append((byte,start,set() if byte==123 and len(stack)<2 else None))
            if len(stack)>MAX_DEPTH:raise CollectionCatalogUnavailable('json_depth_budget')
        elif byte in (125,93):
            if not stack or stack[-1][0]!=(123 if byte==125 else 91):return False
            if len(stack)==3 and (start-stack[-1][1]+1)*96>RECORD_BYTES:return False
            stack.pop()
        else:raise CollectionCatalogUnavailable('json_token_budget')
        position=found.end()
    check()
    return not stack


class _JSONLexical:
    """Bound parser token storage and preserve Python JSON's nonfinite values."""
    def __init__(self):
        self.mode=None;self.escaped=False;self.size=0;self.literal=bytearray()
        self.token=0;self.replacements={};self.depth=0

    def _admit(self,count):
        self.size+=count
        limit=8192 if self.mode=='literal' else TOKEN_BYTES
        if self.size>limit:raise CollectionCatalogUnavailable('json_token_budget')

    def _finish_literal(self):
        value=bytes(self.literal);self.literal.clear();self.mode=None
        if value in _NONFINITE:
            self.replacements[self.token]=_NONFINITE[value]
            return b'null'
        return value

    def feed(self,data,*,final=False):
        parts=[];position=0;length=len(data)
        while position<length:
            if self.mode=='string':
                if self.escaped:
                    self._admit(1);parts.append(data[position:position+1]);position+=1
                    self.escaped=False;continue
                found=_STRING_SPECIAL.search(data,position)
                end=found.end() if found else length
                self._admit(end-position);parts.append(data[position:end]);position=end
                if found:
                    if found.group()==b'\\':self.escaped=True
                    else:self.mode=None
                continue
            if self.mode=='literal':
                found=_DELIMITER.search(data,position);end=found.start() if found else length
                self._admit(end-position);self.literal.extend(data[position:end]);position=end
                if found:parts.append(self._finish_literal())
                continue
            found=_TOKEN_START.search(data,position);end=found.start() if found else length
            separators=data[position:end]
            for byte in separators:
                if byte in (ord('{'),ord('[')):self.depth+=1
                elif byte in (ord('}'),ord(']')):self.depth-=1
                if self.depth>MAX_DEPTH:raise CollectionCatalogUnavailable('json_depth_budget')
            parts.append(separators);position=end
            if not found:break
            self.token+=1;self.size=0
            if data[position]==ord('"'):
                self.mode='string';self._admit(1);parts.append(b'"');position+=1
            else:self.mode='literal'
        if final and self.mode=='literal':parts.append(self._finish_literal())
        return b''.join(parts)


class _JSONInput:
    def __init__(self,owner):
        self.owner=owner;self.decoder=zlib.decompressobj();self.digest=sha256()
        self.pending=b'';self.output=b'';self.compressed=0;self.decompressed=0;self.complete=False
        self.lexical=_JSONLexical();self.lexical_done=False
        self.plain_prefix=None;self.prefix_offset=0

    def replay_plain(self,prefix):
        self.plain_prefix=prefix;self.prefix_offset=0
        self.decompressed=0;self.complete=False

    def _plain(self):
        self.owner._check()
        if self.plain_prefix is not None:
            end=min(len(self.plain_prefix),self.prefix_offset+CHUNK_BYTES)
            value=bytes(memoryview(self.plain_prefix)[self.prefix_offset:end])
            self.prefix_offset=end
            if end==len(self.plain_prefix):self.plain_prefix=None
            return value
        if self.complete:return b''
        if self.decoder.eof:
            if self.decoder.unused_data or self.compressed!=self.owner.length:
                raise CollectionScopeError('수집 본문의 압축 경계가 일치하지 않습니다.')
            if self.digest.hexdigest()!=self.owner.expected_digest:
                if getattr(self.owner,'forward_commit',False):
                    raise CollectionCatalogUnavailable('stale')
                raise CollectionScopeError('수집 본문의 검증값이 일치하지 않습니다.')
            self.complete=True;return b''
        if not self.pending:
            self.pending=self.owner.blob.read(CHUNK_BYTES)
            if not self.pending:
                value=self.decoder.decompress(b'',CHUNK_BYTES)
                if value:return value
                if self.decoder.eof:return self._plain()
                raise CollectionScopeError('수집 본문의 압축 데이터가 끝나지 않았습니다.')
            self.compressed+=len(self.pending);self.digest.update(self.pending)
        value=self.decoder.decompress(self.pending,CHUNK_BYTES)
        self.pending=self.decoder.unconsumed_tail
        return value

    def read(self,size=CHUNK_BYTES):
        if size==0:return b''
        if size<0 or size>CHUNK_BYTES:raise ValueError('Streaming reads require a bounded chunk')
        self.owner._check()
        while not self.output and not self.lexical_done:
            raw=self._plain()
            self.decompressed+=len(raw)
            self.output=self.lexical.feed(raw,final=self.complete)
            self.lexical_done=self.complete
        result=self.output[:size];self.output=self.output[size:]
        return result


class _SessionJSON:
    def _events(self):
        token=0
        for event,value in json_stream.basic_parse(self.input,buf_size=CHUNK_BYTES,use_float=False):
            if event not in ('start_map','end_map','start_array','end_array'):
                token+=1
                if token in self.input.lexical.replacements:
                    value=self.input.lexical.replacements.pop(token);event='number'
                elif isinstance(value,Decimal):value=float(value)
            yield event,value


class SelectedOwnerStream(_SessionJSON):
    """One source snapshot and one single-pass tagged-record iterator.

    Records are (field, kind, ordinal, key, value). Top-level scalars use
    kind='value'; arrays emit start_array/item/end_array; maps emit
    start_map/member/end_map. Member keys are literal strings, including dots.
    The caller must finish reconciliation only after verified EOF and publish
    prepared staging only after this context closes successfully.
    A later commit in the same collector epoch may supply the captured body:
    the full digest proves that its bytes still belong to the captured catalog.
    Catalog pages retain their strict publication pin; epoch changes are stale.
    """
    def __init__(self,channel,key,catalog,*,cancel=None,deadline=None):
        token=channel._catalog_token(catalog)
        if not isinstance(key,(tuple,list)) or len(key)!=2 or not all(isinstance(part,str) for part in key):
            raise CollectionScopeError('수집 목록의 선택 세션이 일치하지 않습니다.')
        self.key=tuple(key);entry=catalog.get('owners',{}).get(self.key)
        if entry is None or self.key[0] not in channel.homes:raise CollectionCatalogUnavailable('missing')
        digest=entry.get('body_digest')
        if not isinstance(digest,str) or len(digest)!=64:raise CollectionScopeError('수집 본문의 검증값이 없습니다.')
        self.path=channel.snapshot_path;self.source_path=str(channel.path);self.scope=channel.scope
        self.selected_homes=tuple(channel.homes);self.identity=tuple(token['catalog_identity'])
        self.expected_digest=digest;self.state=dict(entry.get('state',{}))
        self.owner_metadata=entry.get('owner_metadata');self.cancel=cancel;self.requested_deadline=deadline
        self.db=None;self.blob=None;self.iterator=None;self.verified=False;self.proof=None;self.closed=False
        self.materialization_attempted=False

    def _wal_size(self):
        try:return self.wal_path.stat().st_size
        except FileNotFoundError:return 0

    def _check(self):
        now=time.monotonic()
        if self.cancel is not None and self.cancel():raise CollectionCatalogUnavailable('cancelled')
        if now>=self.deadline:raise CollectionCatalogUnavailable('read_lease_budget')
        if now>=self.next_wal_check:
            size=self._wal_size();self.next_wal_check=now+.25
            if size>WAL_BYTES or size-self.initial_wal>WAL_GROWTH_BYTES:
                raise CollectionCatalogUnavailable('wal_pressure')

    def __enter__(self):
        if self.db is not None or self.closed:raise RuntimeError('Selected owner stream is single use')
        now=time.monotonic();self.deadline=now+LEASE_SECONDS
        if self.requested_deadline is not None:self.deadline=min(self.deadline,self.requested_deadline)
        self.wal_path=Path(str(self.path)+'-wal');self.initial_wal=self._wal_size();self.next_wal_check=now
        try:
            self._check()
            self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=.2)
            self.db.execute('PRAGMA query_only=ON');self.db.execute('PRAGMA cache_size=-2048')
            self.db.execute('PRAGMA mmap_size=0');self.db.execute('BEGIN')
            identity=self.db.execute('SELECT scope,epoch,sequence FROM snapshot WHERE id=1').fetchone()
            if (identity is None or identity[:2]!=(self.scope,self.identity[0]) or
                    type(identity[2]) is not int or identity[2]<self.identity[1]):
                raise CollectionCatalogUnavailable('stale')
            self.forward_commit=identity[2]>self.identity[1]
            row=self.db.execute('SELECT rowid,revision,length(payload) FROM sessions WHERE home=? AND sid=?',self.key).fetchone()
            if row is None:raise CollectionCatalogUnavailable('missing')
            self.rowid,self.revision,self.length=row
            if type(self.length) is not int or self.length<=0:raise CollectionScopeError('수집 본문의 길이가 일치하지 않습니다.')
            self.blob=self.db.blobopen('sessions','payload',self.rowid,readonly=True)
            if len(self.blob)!=self.length:raise CollectionScopeError('수집 본문의 길이가 일치하지 않습니다.')
            self.input=_JSONInput(self)
            return self
        except BaseException:
            self._close();raise

    def _close(self):
        try:
            if self.blob is not None:self.blob.close()
        finally:
            self.blob=None
            try:
                if self.db is not None:self.db.rollback()
            finally:
                if self.db is not None:self.db.close()
                self.db=None;self.closed=True

    def __exit__(self,kind,error,traceback):
        try:
            if self.iterator is not None:self.iterator.close()
        finally:self._close()
        if kind is None and not self.verified:raise CollectionCatalogUnavailable('incomplete_stream')

    def records(self,*,metadata_only=False):
        if self.db is None or self.closed or self.iterator is not None or self.verified:raise RuntimeError('Selected owner stream is single use')
        self.iterator=self._records(metadata_only=metadata_only)
        return self.iterator

    def materialize(self,max_object_bytes):
        """Decode an admitted small owner; retain a prefix for streaming otherwise."""
        if self.db is None or self.closed or self.iterator is not None or self.materialization_attempted:
            raise RuntimeError('Selected owner stream is single use')
        self.materialization_attempted=True
        ceiling=min(MATERIAL_BYTES,max(0,(max_object_bytes-_MATERIAL_SCRATCH)//_MATERIAL_EXPANSION))
        if ceiling<CHUNK_BYTES:return None
        raw=bytearray()
        while not self.input.complete:
            chunk=self.input._plain();self.input.decompressed+=len(chunk);raw.extend(chunk)
            if len(raw)>ceiling:
                self.input.replay_plain(raw)
                return None
        if not _material_admitted(raw,self._check):
            self.input.replay_plain(raw)
            return None
        value=json.loads(raw.decode('utf-8'))
        del raw
        if not isinstance(value,dict):raise CollectionScopeError('수집 본문이 세션 객체가 아닙니다.')
        self._finish(value)
        return value

    @staticmethod
    def _object(first,events,*,retain=True):
        event,value=first
        if event not in ('start_map','start_array'):return value
        builder=ObjectBuilder() if retain else None;depth=0;weight=0
        while True:
            if retain:
                weight+=96+4*len(value) if isinstance(value,str) else 96
                if weight>RECORD_BYTES:raise CollectionCatalogUnavailable('record_object_budget')
                builder.event(event,value)
            if event in ('start_map','start_array'):depth+=1
            elif event in ('end_map','end_array'):depth-=1
            if depth==0:return builder.value if retain else None
            event,value=next(events)

    def _records(self,*,metadata_only):
        events=iter(self._events());identity={}
        if next(events)!=('start_map',None):raise CollectionScopeError('수집 본문이 세션 객체가 아닙니다.')
        while True:
            self._check();event,field=next(events)
            if event=='end_map':break
            if event!='map_key':raise CollectionScopeError('수집 본문의 세션 필드가 일치하지 않습니다.')
            first=next(events);kind=first[0]
            if kind not in ('start_map','start_array'):
                value=first[1]
                if field in ('home','id'):identity[field]=value
                if not metadata_only or field in OWNER_FIELDS:yield field,'value',None,None,value
                continue
            if metadata_only and field in OWNER_FIELDS:raise CollectionCatalogUnavailable('owner_metadata_shape')
            if not metadata_only:yield field,kind,None,None,None
            ordinal=0;ending='end_map' if kind=='start_map' else 'end_array'
            while True:
                self._check();item=next(events)
                if item[0]==ending:break
                key=None
                if kind=='start_map':
                    if item[0]!='map_key':raise CollectionScopeError('수집 본문의 요청 필드가 일치하지 않습니다.')
                    key=item[1];item=next(events)
                value=self._object(item,events,retain=not metadata_only)
                if not metadata_only:yield field,'member' if kind=='start_map' else 'item',ordinal,key,value
                ordinal+=1
            if not metadata_only:yield field,ending,ordinal,None,None
        if next(events,None) is not None:raise CollectionScopeError('수집 본문 뒤에 데이터가 남았습니다.')
        self._finish(identity)

    def _finish(self,identity):
        if not self.input.complete or (identity.get('home'),identity.get('id'))!=self.key:
            raise CollectionScopeError('수집 본문의 세션이 일치하지 않습니다.')
        self._check();self.verified=True
        self.proof=dict(path=self.source_path,scope=self.scope,selected_homes=self.selected_homes,
            catalog_identity=self.identity,owner=self.key,source_digest=self.expected_digest,
            compressed_bytes=self.length,decompressed_bytes=self.input.decompressed)


class _EncodedMetadata(_SessionJSON):
    def __init__(self,payload,digest,max_plain_bytes):
        from io import BytesIO
        self.blob=BytesIO(payload);self.length=len(payload);self.expected_digest=digest
        self.deadline=time.monotonic()+LEASE_SECONDS;self.max_plain_bytes=max_plain_bytes
        self.input=_JSONInput(self)

    def _check(self):
        if time.monotonic()>=self.deadline:raise CollectionCatalogUnavailable('metadata_upgrade_lease')
        if self.input.decompressed>self.max_plain_bytes:
            raise CollectionCatalogUnavailable('metadata_upgrade_body_budget')


def encoded_owner_metadata(payload,key,digest,*,max_plain_bytes):
    """Yield during verified legacy-header recovery; retain only scalar metadata."""
    from .usage_collection import CATALOG_OWNER_BYTES,_catalog_weight
    stream=_EncodedMetadata(payload,digest,max_plain_bytes)
    try:
        events=iter(stream._events())
        if next(events)!=('start_map',None):raise CollectionScopeError('metadata body is not a session object')
        depth=1;field=None;metadata={}
        for number,(event,value) in enumerate(events,1):
            if event=='map_key':
                if depth==1:field=value
            elif event in ('start_map','start_array'):
                if depth==1 and field in OWNER_FIELDS:raise CollectionCatalogUnavailable('owner_metadata_shape')
                depth+=1
            elif event in ('end_map','end_array'):
                depth-=1
                if depth==0:break
            elif depth==1 and field in OWNER_FIELDS:
                metadata[field]=value
                _catalog_weight(metadata,CATALOG_OWNER_BYTES)
            if number%1024==0:
                stream._check()
                yield
        if (depth!=0 or next(events,None) is not None or not stream.input.complete
                or (metadata.get('home'),metadata.get('id'))!=tuple(key)):
            raise CollectionScopeError('metadata body identity or boundary differs')
        stream._check()
        return metadata
    except (StopIteration,JSONError,zlib.error) as error:
        raise CollectionScopeError('metadata body cannot be decoded') from error
    finally:
        stream.blob.close()
