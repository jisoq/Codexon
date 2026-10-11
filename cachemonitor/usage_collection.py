"""Read-only collection clients and an independent background collector service."""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import marshal
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zlib

from .observer_state import ProcessLock
from .version import VERSION
from .usage_paths import index_location


class CollectionScopeError(RuntimeError):
    pass


def publication_fingerprint(value):
    # An in-process comparison only. The IPC payload remains JSON. Check all
    # fields so in-place corrections cannot hide behind an unchanged revision.
    # Reference-sharing flags in newer marshal formats change when an equal
    # snapshot is copied. Version 2 compares content without those flags.
    try:encoded=marshal.dumps(value,2)
    except ValueError:
        # JSON also accepts dict/list subclasses that marshal does not accept.
        encoded=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return sha256(encoded).digest()


class SnapshotHeader:
    """Keep zlib/JSON compatibility while isolating collection freshness."""
    def __init__(self,header,previous=None):
        freshness={key:header[key] for key in ('ts','last_usage_collection_success','index') if key in header}
        stable={key:value for key,value in header.items() if key not in freshness}
        self.fingerprint=publication_fingerprint(stable)
        if previous is not None and previous.fingerprint==self.fingerprint:
            self.prefix=previous.prefix;self.compressor=previous.compressor
        else:
            encoded=json.dumps(stable,ensure_ascii=False,separators=(',',':')).encode('utf-8')
            self.compressor=zlib.compressobj(1)
            # The flush ends the stable deflate block. A copied compressor keeps
            # the checksum valid when the final JSON collection state changes.
            self.prefix=self.compressor.compress(encoded[:-1]+b',')+self.compressor.flush(zlib.Z_FULL_FLUSH)
        compressor=self.compressor.copy()
        encoded=json.dumps(freshness,ensure_ascii=False,separators=(',',':')).encode('utf-8')[1:]
        tail=compressor.compress(encoded)+compressor.flush()
        # Standard zlib readers ignore trailing padding. Keeping this capacity
        # fixed lets SQLite replace only the tail's pages as timestamps grow.
        self.tail=tail.ljust(1024,b'\0')
        self.payload=self.prefix+self.tail


class LiveCollection:
    """Retain one snapshot; send changed rows without retaining a replay queue."""
    volatile={'remaining','totals','groups','collection_complete','status'}
    counters={'done','files','bytes_read','version','last_usage_success','usage_complete_at'}

    def __init__(self,path,scope):
        self.path=str(path);self.scope=scope;self.lock=threading.Lock()
        self.leases={};self.current=None

    def prepare(self,snapshot,epoch,sequence):
        previous=self.current
        same_epoch=previous is not None and previous['publication'][0]==epoch
        sessions={};activities={};manifest=[];activity_manifest=[]
        def row(value,old):
            fingerprint=publication_fingerprint(value)
            if old is not None and old[1]==fingerprint:return old
            payload=zlib.compress(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(),1)
            return sequence,fingerprint,payload
        for session in snapshot['sessions']:
            key=(session['home'],session['id'])
            heavy={key:value for key,value in session.items() if key not in self.volatile}
            saved=row(heavy,previous['sessions'].get(key) if same_epoch else None)
            sessions[key]=saved
            manifest.append(dict(home=key[0],sid=key[1],revision=saved[0],
                                 state={key:value for key,value in session.items() if key in self.volatile}))
        for record in snapshot.get('request_activity',[]):
            key=(record['home'],record['attempt'])
            saved=row(record,previous['activities'].get(key) if same_epoch else None)
            activities[key]=saved
            activity_manifest.append(dict(home=key[0],attempt=key[1],revision=saved[0]))
        meaningful_index={key:value for key,value in snapshot.get('index',{}).items() if key not in self.counters}
        meaningful_manifest=[{**entry,'state':{key:value for key,value in entry['state'].items() if key!='remaining'}}
                             for entry in manifest]
        header={key:value for key,value in snapshot.items() if key not in ('sessions','request_activity')}
        stable={key:value for key,value in header.items() if key not in ('ts','last_usage_collection_success','index')}
        durable=publication_fingerprint((epoch,stable,meaningful_index,meaningful_manifest,activity_manifest,
                                         'request_activity' in snapshot))
        # Serialize mutable envelopes before publication so a later source poll
        # cannot change an in-flight response or the durable comparison key.
        envelope=json.loads(json.dumps(dict(header=header,sessions=manifest,
            activities=activity_manifest if 'request_activity' in snapshot else None),ensure_ascii=False))
        return dict(sessions=sessions,activities=activities,envelope=envelope,header=envelope['header'],
                    durable=durable,publication=(epoch,sequence))

    def publish(self,candidate):
        with self.lock:self.current=candidate

    def requested_homes(self,now):
        with self.lock:
            self.leases={key:value for key,value in self.leases.items() if value[1]>=now-60}
            return list(dict.fromkeys(home for homes,_ in self.leases.values() for home in homes))

    def request(self,value):
        if value.get('scope')!=self.scope or not isinstance(value.get('path'),str) or os.path.normcase(value['path'])!=os.path.normcase(self.path):
            return dict(error='scope'),b''
        with self.lock:
            client=value.get('client')
            if isinstance(client,str) and client:
                homes=value.get('homes',[])
                if not isinstance(homes,list) or not all(isinstance(home,str) for home in homes):
                    return dict(error='homes'),b''
                if value.get('close'):self.leases.pop(client,None)
                else:self.leases[client]=(homes,time.time())
            current=self.current
        if value.get('close') or value.get('lease_only'):return dict(ready=current is not None),b''
        if current is None:return dict(ready=False),b''
        if value.get('header_only'):return dict(ready=True,header=current['header']),b''
        # Older clients can still use the unchanged durable SQLite schema.
        if value.get('protocol')!=3:return dict(ready=False),b''
        known=value.get('publication')
        since=(known[1] if isinstance(known,(list,tuple)) and len(known)==2 and
               known[0]==current['publication'][0] and type(known[1]) is int and
               0<known[1]<=current['publication'][1] else -1)
        chunks=[]
        def manifest(entries,rows,id_key):
            result=[]
            for entry in entries:
                payload=rows[(entry['home'],entry[id_key])][2] if entry['revision']>since else b''
                result.append({**entry,'size':len(payload)})
                if payload:chunks.append(payload)
            return result
        sessions=manifest(current['envelope']['sessions'],current['sessions'],'sid')
        activities=current['envelope']['activities']
        if activities is not None:activities=manifest(activities,current['activities'],'attempt')
        return dict(ready=True,protocol=3,publication=current['publication'],header=current['header'],
                    sessions=sessions,activities=activities),b''.join(chunks)


def locked(path):
    probe=ProcessLock(path)
    try:probe.__enter__()
    except RuntimeError:return True
    else:probe.__exit__(None,None,None);return False


class CollectionChannel:
    """IPC contains sanitized results and requested homes, never source offsets."""
    def __init__(self,homes,path=None,evidence=None):
        self.path=index_location(path);self.index_path=path;self.evidence=evidence
        self.homes=list(dict.fromkeys(str(Path(h).resolve()) for h in homes))
        if any(self.path.is_relative_to(Path(h)) for h in self.homes):
            raise ValueError('앱 색인은 Codex 원본 폴더 밖에 저장해야 합니다')
        from .model_evidence import default_path
        self.default_evidence=self.path.with_name('model-evidence.sqlite') if path is not None else default_path()
        self.scope=self.scope_key(evidence)
        self.snapshot_path=self.companion('.collection.sqlite')
        self.snapshot_path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.snapshot_path)
        tables={row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables-{'snapshot','consumers','control','sessions','activity'}:
            self.db.close()
            raise ValueError('다른 데이터베이스를 수집 통신용으로 사용할 수 없습니다')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, scope TEXT, epoch TEXT, sequence INTEGER, observed REAL, payload BLOB)')
        self.db.execute('CREATE TABLE IF NOT EXISTS consumers (id TEXT PRIMARY KEY, scope TEXT, homes TEXT, observed REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS control (instance TEXT PRIMARY KEY, action TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions (home TEXT, sid TEXT, revision INTEGER, payload BLOB, PRIMARY KEY(home,sid))')
        self.db.execute('CREATE TABLE IF NOT EXISTS activity (home TEXT, attempt TEXT, revision INTEGER, position INTEGER, payload BLOB, PRIMARY KEY(home,attempt))')
        self.published={};self.session_cache={};self.published_activity={};self.activity_cache={};self.activity_identity=None
        self.client=uuid.uuid4().hex;self.cached=None;self.identity=None
        self.live=None;self.live_server=None
        self.live_cached=None;self.live_publication=None;self.last_read_live=False
        self.live_session_cache={};self.live_activity_cache={}

    def companion(self,suffix):
        return self.path.with_name(self.path.name+'.codexon-'+suffix.lstrip('.'))

    def scope_key(self,evidence):
        return os.path.normcase(str(Path(evidence or self.default_evidence).resolve()))

    def start_live(self):
        from .collection_transport import LiveServer
        self.live=LiveCollection(self.path,self.scope)
        try:self.live_server=LiveServer(self.companion('.collection-live.json'),self.live.request)
        except BaseException:self.live=None;raise

    def live_request(self,**value):
        from .collection_transport import request
        return request(self.companion('.collection-live.json'),
                       dict(path=str(self.path),scope=self.scope,client=self.client,homes=self.homes,**value))

    def subscribe(self,now):
        if self.last_read_live:return
        response=self.live_request(lease_only=True)
        if response is not None:
            if response[0].get('error'):raise CollectionScopeError('사용량 색인에 다른 관측 DB가 연결되어 있습니다. 별도 색인 경로를 사용하세요.')
            return
        self.db.execute('INSERT OR REPLACE INTO consumers VALUES(?,?,?,?)',
            (self.client,self.scope,json.dumps(self.homes),now));self.db.commit()

    def requested_homes(self,now):
        homes=list(self.homes)
        if self.live:
            homes.extend(home for home in self.live.requested_homes(now) if home not in homes)
        for scope,value in self.db.execute('SELECT scope,homes FROM consumers WHERE observed>=? ORDER BY observed,id',(now-60,)):
            if scope==self.scope:homes.extend(home for home in json.loads(value) if home not in homes)
        return homes

    def _publish_activity(self,records,sequence,restart,prepared=None):
        # Called under publish's write transaction. A new owner must compare
        # committed bytes here, not hydrate a cache before acquiring the lock.
        stored={(home,attempt):(revision,position,payload)
                for home,attempt,revision,position,payload in
                self.db.execute('SELECT home,attempt,revision,position,payload FROM activity')} if restart else {}
        previous={} if restart else self.published_activity
        highwater=max([getattr(self,'activity_row_revision',0),
                       *(row[0] for row in stored.values() if type(row[0]) is int)])
        next_revision=max(sequence,highwater+1)
        current={};writes=[];positions=[]
        for position,record in enumerate(records):
            key=(record['home'],record['attempt'])
            fingerprint=prepared[key][1] if prepared is not None else publication_fingerprint(record)
            old=previous.get(key);saved=stored.get(key);payload=None
            if old is not None and old[1]==fingerprint:
                revision=old[2];old_position=old[0]
            else:
                payload=(prepared[key][2] if prepared is not None else
                         zlib.compress(json.dumps(record,ensure_ascii=False,separators=(',',':')).encode(),1))
                if saved is not None and type(saved[0]) is int and saved[2]==payload:
                    revision,old_position,_=saved
                else:
                    revision=next_revision;old_position=position
                    writes.append((*key,revision,position,payload))
            if old_position!=position:positions.append((position,*key))
            current[key]=(position,fingerprint,revision)
        removed=(stored.keys() if restart else previous.keys())-current.keys()
        self.db.executemany('INSERT OR REPLACE INTO activity VALUES(?,?,?,?,?)',writes)
        self.db.executemany('UPDATE activity SET position=? WHERE home=? AND attempt=?',positions)
        self.db.executemany('DELETE FROM activity WHERE home IS ? AND attempt IS ?',removed)
        changed=bool(writes or positions or removed)
        revision=sequence if changed or restart else getattr(self,'activity_revision',sequence)
        return current,revision,next_revision if writes else highwater

    def publish(self,snapshot,epoch,sequence):
        candidate=self.live.prepare(snapshot,epoch,sequence) if self.live else None
        if candidate is not None and self.live.current is not None and candidate['durable']==self.live.current['durable']:
            self.live.publish(candidate)
            return
        volatile={'remaining','totals','groups','collection_complete','status'}
        previous=self.published if getattr(self,'publish_epoch',None)==epoch else {}
        current={};manifest=[];writes=[]
        for session in snapshot['sessions']:
            key=(session['home'],session['id'])
            heavy={k:v for k,v in session.items() if k not in volatile}
            prepared=candidate['sessions'][key] if candidate is not None else None
            fingerprint=prepared[1] if prepared is not None else publication_fingerprint(heavy)
            old=previous.get(key)
            if old is not None and fingerprint==old[1]:
                revision=old[0];current[key]=old
            else:
                revision=sequence
                payload=(prepared[2] if prepared is not None else
                         zlib.compress(json.dumps(heavy,ensure_ascii=False,separators=(',',':')).encode('utf-8'),1))
                writes.append((*key,revision,payload))
                current[key]=(revision,fingerprint)
            manifest.append(dict(home=key[0],sid=key[1],revision=revision,
                state={k:v for k,v in session.items() if k in volatile}))
        header={k:v for k,v in snapshot.items() if k not in ('sessions','request_activity')}
        header.update(collection_schema=2,session_manifest=manifest)
        old_header=getattr(self,'published_header',None) if previous is self.published else None
        with self.db:
            if not self.db.in_transaction:self.db.execute('BEGIN IMMEDIATE')
            activities,activity_revision,activity_row_revision=self._publish_activity(
                snapshot.get('request_activity',[]),sequence,previous is not self.published,
                candidate['activities'] if candidate is not None else None)
            if 'request_activity' in snapshot:header['activity_revision']=activity_revision
            encoded_header=SnapshotHeader(header,old_header)
            if previous is not self.published:
                self.db.execute('DELETE FROM sessions')
            self.db.executemany('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)',writes)
            self.db.executemany('DELETE FROM sessions WHERE home=? AND sid=?',self.published.keys()-current.keys())
            updated=False
            if (old_header is not None and encoded_header.prefix is old_header.prefix and
                    len(encoded_header.payload)==len(old_header.payload)):
                row=self.db.execute('SELECT scope,epoch,sequence,length(payload) FROM snapshot WHERE id=1').fetchone()
                if row==(self.scope,epoch,self.publish_sequence,len(old_header.payload)):
                    # Dirty the tail first so SQLite reads the overflow payload
                    # through its page cache instead of direct file reads. The
                    # transaction hides this intermediate state from other readers.
                    with self.db.blobopen('snapshot','payload',1) as blob:
                        blob.seek(len(encoded_header.prefix));blob.write(encoded_header.tail)
                    # Verify the entire target payload before advancing freshness.
                    # A damaged prefix still requires a complete publication.
                    updated=self.db.execute('UPDATE snapshot SET sequence=?,observed=? '
                        'WHERE id=1 AND scope=? AND epoch=? AND sequence=? AND payload=?',
                        (sequence,snapshot['ts'],self.scope,epoch,self.publish_sequence,encoded_header.payload)).rowcount==1
            if not updated:
                self.db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?,?,?,?,?)',
                    (self.scope,epoch,sequence,snapshot['ts'],encoded_header.payload))
        self.published=current;self.publish_epoch=epoch
        self.published_header=encoded_header;self.publish_sequence=sequence
        self.published_activity=activities;self.activity_revision=activity_revision
        self.activity_row_revision=activity_row_revision
        if candidate is not None:
            self.live.publish(candidate)

    def read_header(self):
        return self.read(header_only=True)

    def _read_live(self,metadata,body):
        same_epoch=self.live_publication and self.live_publication[0]==metadata['publication'][0]
        offset=0
        def rows(entries,previous,id_key):
            nonlocal offset
            current={};result=[]
            for entry in entries:
                key=(entry['home'],entry[id_key]);revision=entry['revision'];size=entry['size']
                if type(size) is not int or size<0 or offset+size>len(body):
                    raise CollectionScopeError('수집 결과의 본문 길이가 일치하지 않습니다.')
                old=previous.get(key)
                if size:
                    value=json.loads(zlib.decompress(memoryview(body)[offset:offset+size]));offset+=size
                elif old is not None and old[0]==revision:value=old[1]
                else:raise CollectionScopeError('수집 결과의 본문 revision이 일치하지 않습니다.')
                current[key]=(revision,value);result.append(value)
            return current,result
        sessions,heavy=rows(metadata['sessions'],self.live_session_cache if same_epoch else {},'sid')
        result={**metadata['header'],'sessions':[{**session,**entry['state']}
                for session,entry in zip(heavy,metadata['sessions'],strict=True)]}
        activities={}
        if metadata['activities'] is not None:
            activities,result['request_activity']=rows(metadata['activities'],
                self.live_activity_cache if same_epoch else {},'attempt')
        if offset!=len(body):raise CollectionScopeError('수집 결과의 본문 길이가 일치하지 않습니다.')
        return result,sessions,activities

    def read(self,*,shared=False,header_only=False):
        # __new__ read-only diagnostic channels intentionally remain SQLite-only.
        self.last_read_live=False
        if hasattr(self,'client'):
            response=self.live_request(header_only=header_only,protocol=3,publication=self.live_publication)
            if response is not None:
                metadata,body=response
                if metadata.get('error'):
                    raise CollectionScopeError('사용량 색인에 다른 관측 DB가 연결되어 있습니다. 별도 색인 경로를 사용하세요.')
                if metadata.get('ready') and (header_only or metadata.get('protocol')==3):
                    if header_only:result=metadata['header']
                    else:result,sessions,activities=self._read_live(metadata,body)
                    source=result.get('index',{}).get('path')
                    if not source or os.path.normcase(str(Path(source).resolve()))!=os.path.normcase(str(self.path)):
                        raise CollectionScopeError('수집 결과의 사용량 색인 경로가 일치하지 않습니다.')
                    self.last_read_live=True
                    if not header_only:
                        self.live_session_cache=sessions;self.live_activity_cache=activities
                        self.live_publication=tuple(metadata['publication']);self.live_cached=result
                    return result if shared or header_only else deepcopy(result)
        with closing(sqlite3.connect(self.snapshot_path.as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('BEGIN')
            row=db.execute('SELECT scope,epoch,sequence FROM snapshot WHERE id=1').fetchone()
            if not row:
                return self.recovered_snapshot(None,None,shared=shared,header_only=header_only)
            if row[0]!=self.scope:
                if locked(self.companion('.collector.lock')):
                    raise CollectionScopeError('사용량 색인에 다른 관측 DB가 연결되어 있습니다. 별도 색인 경로를 사용하세요.')
                return None
            identity=(row[1],row[2])
            if identity!=self.identity:
                payload=db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0]
                header=json.loads(zlib.decompress(payload))
            else:header=self.cached
            source=header.get('index',{}).get('path')
            if not source or os.path.normcase(str(Path(source).resolve()))!=os.path.normcase(str(self.path)):
                raise CollectionScopeError('수집 결과의 사용량 색인 경로가 일치하지 않습니다.')
            schema=header.get('collection_schema',1)
            if schema not in (1,2):raise CollectionScopeError('지원하지 않는 수집 형식입니다.')
            if header_only:
                return self.recovered_snapshot({k:v for k,v in header.items() if k not in ('sessions','session_manifest','collection_schema')},
                                               identity,shared=shared,header_only=True)
            if schema==2:
                previous=self.session_cache if self.identity and self.identity[0]==row[1] else {}
                current={};sessions=[]
                for entry in header['session_manifest']:
                    key=(entry['home'],entry['sid']);revision=entry['revision']
                    old=previous.get(key)
                    if old is not None and old[0]==revision:heavy=old[1]
                    else:
                        stored=db.execute('SELECT revision,payload FROM sessions WHERE home=? AND sid=?',key).fetchone()
                        if stored is None or stored[0]!=revision:raise CollectionScopeError('수집 결과의 세션 revision이 일치하지 않습니다.')
                        heavy=json.loads(zlib.decompress(stored[1]))
                    current[key]=(revision,heavy);sessions.append({**heavy,**entry['state']})
                result={k:v for k,v in header.items() if k not in ('collection_schema','session_manifest')}
                result['sessions']=sessions
                self.session_cache=current
                if 'activity_revision' in header:
                    activity_identity=(row[1],header['activity_revision'])
                    if activity_identity!=getattr(self,'activity_identity',None):
                        old=self.activity_cache if getattr(self,'activity_identity',None) and self.activity_identity[0]==row[1] else {}
                        activity={}
                        for home,attempt,revision in db.execute('SELECT home,attempt,revision FROM activity ORDER BY position'):
                            key=(home,attempt);prior=old.get(key)
                            if prior and prior[0]==revision:activity[key]=prior
                            else:
                                payload=db.execute('SELECT payload FROM activity WHERE home=? AND attempt=?',key).fetchone()[0]
                                activity[key]=(revision,json.loads(zlib.decompress(payload)))
                        self.activity_cache=activity;self.activity_identity=activity_identity
                    result['request_activity']=[item[1] for item in self.activity_cache.values()]
                    result.pop('activity_revision',None)
            else:
                result=header
            self.cached=header;self.identity=identity
        # Shared records are read-only; each poll owns its envelope and list.
        return self.recovered_snapshot(result,identity,shared=shared,header_only=False)

    def recovered_snapshot(self,result,identity,*,shared,header_only):
        cached=getattr(self,'live_cached',None)
        publication=getattr(self,'live_publication',None)
        # Wall clocks can move backwards. Only publication order within one
        # collector epoch can prove that an in-memory observation is newer.
        if cached is not None and (result is None or publication and publication[0]==identity[0] and publication[1]>identity[1]):result=cached
        if result is None:return None
        if header_only:return {key:value for key,value in result.items() if key not in ('sessions','request_activity')}
        return {**result,'sessions':list(result['sessions'])} if shared else deepcopy(result)

    def close(self):
        if self.live_server:
            self.live_server.close();self.live_server=None;self.live=None
        elif self.last_read_live:self.live_request(close=True)
        if self.db:
            try:self.db.execute('DELETE FROM consumers WHERE id=?',(self.client,));self.db.commit()
            finally:self.db.close();self.db=None


def empty_snapshot(homes,path,now):
    return dict(ts=now,homes=homes,sessions=[],errors=[],unassigned=[],
        usage_collection_complete=False,last_usage_collection_success=None,usage_errors=[],
        index=dict(loading=True,done=0,files=0,bytes_read=0,version=0,path=str(path)))


def collector_command(channel):
    parts=[sys.executable]
    if not getattr(sys,'frozen',False):parts.append(str(Path(__file__).resolve().parents[1]/'run.py'))
    parts+=['--usage-collector','--index-path',str(channel.path)]
    if channel.index_path is None:parts.append('--default-index')
    for home in channel.homes:parts+=['--codex-home',home]
    if channel.evidence:parts+=['--evidence-path',str(channel.evidence)]
    return parts


class CollectionClient:
    """GUI/cache clients never instantiate a source index or acquire its lock."""
    def __init__(self,homes,path=None,model_evidence_path=None):
        self.channel=CollectionChannel(homes,path,model_evidence_path)
        self.path=self.channel.path

    def poll(self,now=None):
        now=now or time.time()
        try:
            snapshot=self.channel.read(shared=True)
            self.channel.subscribe(now)
        except CollectionScopeError as error:
            snapshot=empty_snapshot(self.channel.homes,self.path,now)
            snapshot['errors']=[str(error)];snapshot['usage_errors']=[str(error)]
            snapshot['index']['loading']=False
            return snapshot
        if snapshot is not None and not self.channel.last_read_live:
            snapshot={**snapshot,'collection_recovery':True}
        if snapshot is None:snapshot=empty_snapshot(self.channel.homes,self.path,now)
        elif now-snapshot['ts']>30:
            snapshot['errors']=list(dict.fromkeys([*snapshot.get('errors',[]),'수집 결과 갱신 지연']))
            snapshot['usage_collection_complete']=False
            snapshot['index']={**snapshot['index'],'usage_complete':False}
        homes=set(self.channel.homes)
        snapshot['quota_by_home']={home:quota for home,quota in snapshot.get('quota_by_home',{}).items() if home in homes}
        if set(snapshot['homes'])!=homes:
            missing=not homes.issubset(snapshot['homes'])
            snapshot={**snapshot,'homes':self.channel.homes,
                'sessions':[s for s in snapshot['sessions'] if s['home'] in homes],
                'unassigned':[r for r in snapshot.get('unassigned',[]) if r.get('home') in homes],
                'request_activity':[r for r in snapshot.get('request_activity',[]) if r.get('home') in homes]}
            if missing:
                snapshot['index']={**snapshot['index'],'loading':True,'usage_complete':False}
                snapshot['usage_collection_complete']=False
        snapshot['homes']=self.channel.homes
        return snapshot

    def close(self):
        self.channel.close()


class CollectorService:
    """Construct only in the dedicated --usage-collector background process."""
    def __init__(self,homes,path=None,evidence=None):
        self.channel=CollectionChannel(homes,path,evidence)
        legacy_lock=self.channel.path.with_suffix('.collector.lock')
        if legacy_lock.exists() and locked(legacy_lock):
            self.channel.close()
            raise RuntimeError('이전 수집기의 기록 저장과 종료 대기')
        self.lock=ProcessLock(self.channel.companion('.collector.lock'))
        try:self.lock.__enter__()
        except BaseException:self.channel.close();raise
        try:
            from .collection_lifecycle import migrate_default_index
            migrate_default_index(self.channel)
        except BaseException:
            self.lock.__exit__(None,None,None);self.channel.close();raise
        self.index=None;self.epoch=uuid.uuid4().hex;self.instance=uuid.uuid4().hex;self.sequence=0;self.last=None
        self.executable=str(Path(sys.executable).resolve())
        if os.name=='nt':
            from .proxy_identity import process_identity
            self.executable=process_identity(os.getpid())['executable']
        try:self.channel.start_live()
        except BaseException:
            self.lock.__exit__(None,None,None);self.channel.close();raise

    def stopping(self):
        from .observer_state import read_json
        session=read_json(self.channel.companion('.session.json'))
        return (session.get('scope')==self.channel.scope and session.get('stopped') or
                self.channel.db.execute("SELECT 1 FROM control WHERE instance=? AND action='stop'",(self.instance,)).fetchone() is not None)

    def poll(self,now=None):
        from .index import UsageIndex
        now=now or time.time();homes=self.channel.requested_homes(now)
        if self.index and homes!=[str(h) for h in self.index.homes]:
            self.index.close();self.index=None;self.epoch=uuid.uuid4().hex
        if self.index is None:self.index=UsageIndex(homes,self.channel.index_path,self.channel.evidence)
        snapshot=self.index.poll(now)
        snapshot={**snapshot,'sessions':[dict(s,usage_revision=f'{self.epoch}:{s.get("usage_revision")}') for s in snapshot['sessions']],
            'collection':dict(pid=os.getpid(),instance=self.instance,version=VERSION,
                executable=self.executable)}
        self.sequence+=1;self.channel.publish(snapshot,self.epoch,self.sequence);self.last=snapshot
        return snapshot

    def close(self):
        try:
            if self.index:self.index.close();self.index=None
            self.channel.close()
        finally:self.lock.__exit__(None,None,None)


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--codex-home',action='append',required=True)
    parser.add_argument('--index-path',type=Path,required=True)
    parser.add_argument('--evidence-path',type=Path)
    parser.add_argument('--default-index',action='store_true')
    parser.add_argument('--instance',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.default_index and args.index_path.resolve()!=index_location():
        parser.error('--default-index requires --index-path to match the default usage index')
    try:service=CollectorService(args.codex_home,None if args.default_index else args.index_path,args.evidence_path)
    except RuntimeError:return 0
    if args.instance:service.instance=args.instance
    try:
        while not service.stopping():
            snapshot=service.poll()
            time.sleep(.1 if snapshot['index']['loading'] else 3)
    finally:service.close()


def isolated_collector(homes,path,evidence=None,*,reuse=False):
    """QA owns a dedicated child service; never register a persistent test task."""
    import subprocess
    client=CollectionClient(homes,path,evidence)
    try:previous=client.channel.read()
    except BaseException:client.close();raise
    if reuse and previous and locked(client.channel.companion('.collector.lock')):
        client.close();return lambda:None
    instance=uuid.uuid4().hex
    child=subprocess.Popen(collector_command(client.channel)+['--instance',instance],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    def cleanup():
        if child.poll() is None and instance:
            with closing(sqlite3.connect(client.channel.snapshot_path)) as db:
                db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',(instance,'stop'));db.commit()
        try:child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if os.name=='nt':subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,timeout=10)
            else:child.terminate()
            child.wait(timeout=10)
    try:
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            value=client.channel.read()
            candidate=(value or {}).get('collection',{}).get('instance')
            if candidate==instance:return cleanup
            if child.poll() is not None:
                if reuse and child.returncode==0 and value and locked(client.channel.companion('.collector.lock')):
                    return lambda:None
                raise RuntimeError('격리 수집기를 시작하지 못했습니다')
            time.sleep(.05)
        raise RuntimeError('격리 수집기 준비 시간 초과')
    except BaseException:cleanup();raise
    finally:client.close()
