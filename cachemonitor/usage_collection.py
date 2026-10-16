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


class CollectionPending(RuntimeError):
    """An intentional handoff or an owner awaiting identification, not a fault."""


def publish_collector_fault(path,scope,instance,error,*,facts=None):
    from .app_errors import exception_facts
    from .observer_control import atomic_write
    value=dict(scope=sha256(scope.encode('utf-8')).hexdigest(),instance=str(instance or '')[:64],at=time.time(),
        facts=dict(runtime_failed=True,**exception_facts(error)) | (facts or {}))
    try:atomic_write(path,json.dumps(value,ensure_ascii=False).encode('utf-8'))
    except OSError:pass


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


class CollectionChanges:
    """Owner-scoped producer tokens, used only inside the collector process.

    Session tokens cover retained fields outside LiveCollection.volatile;
    activity tokens cover the entire record. Producers replace tokens for
    corrections as well as appends. An activity revision also covers ordering
    and removal. Callers without those guarantees retain full validation.
    """
    __slots__=('owner','sessions','activities','activity_revision')

    def __init__(self,owner,sessions,activities,*,activity_revision=None):
        self.owner=owner;self.sessions=sessions;self.activities=activities
        self.activity_revision=activity_revision


class LiveCollection:
    """Retain one snapshot; send changed rows without retaining a replay queue."""
    volatile={'remaining','totals','groups','collection_complete','status','usage_revision','request_revision'}
    counters={'done','files','bytes_read','version','last_usage_success','usage_complete_at'}

    def __init__(self,path,scope,*,recovery=None):
        self.path=str(path);self.scope=scope;self.lock=threading.Lock()
        self.leases={};self.current=None
        self.recovery=recovery
        self.changed=threading.Condition(self.lock);self.closed=False;self.waiters=set()

    def prepare(self,snapshot,epoch,sequence,*,changes=None):
        previous=self.current
        same_epoch=previous is not None and previous['publication'][0]==epoch
        trusted=isinstance(changes,CollectionChanges) and changes.owner is not None
        same_owner=same_epoch and trusted and previous.get('change_owner') is changes.owner
        prior_sessions=previous.get('session_tokens',{}) if same_owner else {}
        prior_activities=previous.get('activity_tokens',{}) if same_owner else {}
        session_tokens={};activity_tokens={}
        sessions={};activities={};manifest=[];activity_manifest=[]
        prior_entries=previous['entries'] if same_epoch else {}
        entries={};state_revisions={};meaningful_changed=not same_epoch
        def row(value,old,token,prior,key):
            if token is not None and old is not None and prior.get(key)==token:
                return old
            fingerprint=publication_fingerprint(value)
            if old is not None and old[1]==fingerprint:return old
            payload=zlib.compress(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(),1)
            return sequence,fingerprint,payload,sha256(payload).hexdigest()
        from .collection_replay import EncodedSession
        for session in snapshot['sessions']:
            key=(session['home'],session['id'])
            token=changes.sessions.get(key) if trusted else None
            if token is not None:session_tokens[key]=token
            old_body=previous['sessions'].get(key) if same_epoch else None
            if isinstance(session,EncodedSession):
                saved=(old_body if old_body is not None and old_body[3]==session.digest else
                       (sequence,session.fingerprint,session.payload,session.digest))
            else:
                heavy={key:value for key,value in session.items() if key not in self.volatile}
                saved=row(heavy,old_body,token,prior_sessions,key)
            sessions[key]=saved
            state={field:value for field,value in session.items() if field in self.volatile}
            old=prior_entries.get(key)
            if old is not None and old['revision']==saved[0] and old['state']==state:
                entry=old;state_revisions[key]=previous['state_revisions'][key]
            else:
                if (old is None or old['revision']!=saved[0] or
                        {k:v for k,v in old['state'].items() if k!='remaining'}!=
                        {k:v for k,v in state.items() if k!='remaining'}):meaningful_changed=True
                # Source-owned nested values can change after this publication.
                # Freeze only changed envelopes; unchanged entries are immutable.
                entry=dict(home=key[0],sid=key[1],revision=saved[0],body_digest=saved[3],
                           state=json.loads(json.dumps(state,ensure_ascii=False)))
                state_revisions[key]=sequence
            entries[key]=entry;manifest.append(entry)
        activities_present='request_activity' in snapshot and not snapshot.get('activity_stream')
        activity_revision=changes.activity_revision if trusted else None
        same_activity=(same_owner and activity_revision is not None and
                       previous.get('activity_revision')==activity_revision and
                       previous.get('activities_present')==activities_present)
        if same_activity:
            activities=previous['activities'];activity_tokens=prior_activities
            activity_manifest=previous['envelope']['activities'] if activities_present else []
        else:
            for record in snapshot.get('request_activity',[]) if activities_present else ():
                key=(record['home'],record['attempt'])
                token=changes.activities.get(key) if trusted else None
                if token is not None:activity_tokens[key]=token
                saved=row(record,previous['activities'].get(key) if same_epoch else None,token,prior_activities,key)
                activities[key]=saved
                activity_manifest.append(dict(home=key[0],attempt=key[1],revision=saved[0]))
        meaningful_index={key:value for key,value in snapshot.get('index',{}).items() if key not in self.counters}
        membership_changed=(not same_epoch or list(sessions)!=list(previous['sessions']) or
                            list(activities)!=list(previous['activities']) or
                            activities_present!=previous['activities_present'])
        membership_revision=sequence if membership_changed else previous['membership_revision']
        meaningful_revision=sequence if meaningful_changed or membership_changed else previous['meaningful_revision']
        header={key:value for key,value in snapshot.items() if key not in ('sessions','request_activity')}
        stable={key:value for key,value in header.items() if key not in ('ts','last_usage_collection_success','index')}
        old_manifest=previous['envelope']['activities'] if same_epoch else None
        if same_epoch and (activity_manifest is old_manifest or activity_manifest==old_manifest):
            activity_digest=previous['activity_digest']
        else:activity_digest=publication_fingerprint(activity_manifest)
        durable=publication_fingerprint((epoch,stable,meaningful_index,meaningful_revision,activity_digest,
                                         activities_present))
        envelope=dict(header=json.loads(json.dumps(header,ensure_ascii=False)),sessions=manifest,
                      activities=activity_manifest if activities_present else None)
        return dict(sessions=sessions,activities=activities,envelope=envelope,header=envelope['header'],
                    entries=entries,state_revisions=state_revisions,membership_revision=membership_revision,
                    meaningful_revision=meaningful_revision,
                    durable=durable,publication=(epoch,sequence),
                    change_owner=changes.owner if trusted else None,
                    session_tokens=session_tokens,activity_tokens=activity_tokens,
                    activity_revision=activity_revision,activity_digest=activity_digest,
                    activities_present=activities_present)

    def publish(self,candidate):
        with self.changed:
            self.current=candidate;self.changed.notify_all()

    def close(self):
        with self.changed:
            self.closed=True;self.recovery=None;self.changed.notify_all()

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
                if value.get('close'):
                    self.leases.pop(client,None);self.changed.notify_all()
                else:self.leases[client]=(homes,time.time())
            current=self.current
            recovery=self.recovery
            if value.get('watch')==1:
                from .collection_transport import NOTIFICATION_WAIT
                if not isinstance(client,str) or not client or client in self.waiters or len(self.waiters)>=4:
                    return dict(error='waiter_capacity'),b''
                self.waiters.add(client)
                try:
                    known=value.get('publication')
                    self.changed.wait_for(lambda:self.closed or client not in self.leases or
                        self.current is not None and list(self.current['publication'])!=known,NOTIFICATION_WAIT)
                    if self.closed or client not in self.leases:return dict(error='closed'),b''
                    return dict(watch=1,publication=self.current['publication'] if self.current else None),b''
                finally:self.waiters.discard(client)
        if value.get('close') or value.get('lease_only'):return dict(ready=current is not None),b''
        if current is None:return dict(ready=False),b''
        if value.get('header_only'):return dict(ready=True,header=current['header']),b''
        # Older clients can still use the unchanged durable SQLite schema.
        protocol=value.get('protocol')
        if protocol not in (3,4,5):return dict(ready=False),b''
        if current['header'].get('activity_stream') and not value.get('activity_stream'):
            return dict(ready=False),b''
        known=value.get('publication')
        checkpoint=value.get('checkpoint') or known
        recovered=(recovery[1] if protocol==5 and recovery is not None and
                   isinstance(checkpoint,(list,tuple)) and tuple(checkpoint)==recovery[0] else {})
        exclusions=value.get('checkpoint_exclusions',[])
        if (not isinstance(exclusions,list) or len(exclusions)>4096 or
                any(not isinstance(key,(list,tuple)) or len(key)!=2 or
                    not all(isinstance(part,str) for part in key) for key in exclusions)):
            recovered={};exclusions=[]
        exclusions={tuple(key) for key in exclusions}
        since=(known[1] if isinstance(known,(list,tuple)) and len(known)==2 and
               known[0]==current['publication'][0] and type(known[1]) is int and
               0<known[1]<=current['publication'][1] else -1)
        chunks=[]
        full=protocol==3 or since<current['membership_revision']
        def manifest(entries,rows,id_key):
            result=[]
            for entry in entries:
                key=(entry['home'],entry[id_key])
                changed=current['state_revisions'][key] if id_key=='sid' else entry['revision']
                if not full and changed<=since:continue
                payload=rows[key][2] if entry['revision']>since else b''
                if id_key=='sid' and key not in exclusions and key in recovered and recovered[key]==entry.get('body_digest'):payload=b''
                result.append({**entry,'size':len(payload)})
                if payload:chunks.append(payload)
            return result
        sessions=manifest(current['envelope']['sessions'],current['sessions'],'sid')
        activities=current['envelope']['activities']
        if activities is not None:activities=manifest(activities,current['activities'],'attempt')
        return dict(ready=True,protocol=protocol,publication=current['publication'],header=current['header'],
                    sessions=sessions,activities=activities,
                    **(dict(checkpoint_filter=True) if protocol==5 else {}),
                    **(dict(full=full) if protocol in (4,5) else {})),b''.join(chunks)


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
        self.db.execute('CREATE INDEX IF NOT EXISTS activity_revision ON activity(revision)')
        self.published={};self.session_cache={};self.published_activity={};self.activity_cache={};self.activity_identity=None
        self.client=uuid.uuid4().hex;self.cached=None;self.identity=None
        self.live=None;self.live_server=None
        self.live_cached=None;self.live_publication=None;self.last_read_live=False
        self.live_session_cache={};self.live_activity_cache={}
        self.retain_pending=False;self.pending_checkpoint=None
        self.checkpoint_exclusions=set()

    def companion(self,suffix):
        return self.path.with_name(self.path.name+'.codexon-'+suffix.lstrip('.'))

    def scope_key(self,evidence):
        return os.path.normcase(str(Path(evidence or self.default_evidence).resolve()))

    def _recovery_catalog(self):
        budget=4*1024*1024
        try:
            saved=self.db.execute('''SELECT scope,epoch,sequence,CASE WHEN length(payload)<=? THEN payload END
                FROM snapshot WHERE id=1''',(budget,)).fetchone()
            if not saved or saved[0]!=self.scope or saved[3] is None:return None
            decoder=zlib.decompressobj();decoded=decoder.decompress(saved[3],budget+1)
            if len(decoded)>budget or not decoder.eof:return None
            header=json.loads(decoded)
            if not isinstance(header,dict) or not isinstance(header.get('session_manifest'),list):return None
            bodies={};weight=0
            # One small immutable checkpoint catalog, not a payload replay queue.
            # Entries beyond the budget are transmitted normally.
            for entry in header.get('session_manifest',[]):
                if not isinstance(entry,dict):continue
                digest=entry.get('body_digest')
                if not isinstance(digest,str) or len(digest)!=64:continue
                key=(entry.get('home'),entry.get('sid'))
                if not all(isinstance(part,str) for part in key):continue
                weight+=320+sum(len(part.encode()) for part in (*key,digest))
                if weight>budget:break
                bodies[key]=digest
            return ((saved[1],saved[2]),bodies) if bodies else None
        except (sqlite3.Error,ValueError,TypeError,RecursionError,zlib.error):return None

    def start_live(self):
        from .collection_transport import LiveServer
        self.live=LiveCollection(self.path,self.scope,recovery=self._recovery_catalog())
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

    def publish(self,snapshot,epoch,sequence,*,changes=None):
        if snapshot.get('activity_stream'):
            snapshot={**snapshot,'activity_highwater':getattr(self,'activity_row_revision',0)}
        candidate=self.live.prepare(snapshot,epoch,sequence,changes=changes) if self.live else None
        if candidate is not None and self.live.current is not None and candidate['durable']==self.live.current['durable']:
            self.live.publish(candidate)
            return
        volatile=LiveCollection.volatile
        previous=self.published if getattr(self,'publish_epoch',None)==epoch else {}
        current={};manifest=[];writes=[]
        from .collection_replay import EncodedSession
        for session in snapshot['sessions']:
            key=(session['home'],session['id'])
            prepared=candidate['sessions'][key] if candidate is not None else None
            if prepared is None and isinstance(session,EncodedSession):
                prepared=(sequence,session.fingerprint,session.payload,session.digest)
            heavy={k:v for k,v in session.items() if k not in volatile} if prepared is None else None
            fingerprint=prepared[1] if prepared is not None else publication_fingerprint(heavy)
            old=previous.get(key)
            if old is not None and fingerprint==old[1]:
                revision=old[0];current[key]=old
                body_digest=old[2]
            else:
                # Reused checkpoint revisions can be ahead of this epoch.
                # Every correction must advance the stored body's identity.
                revision=max(sequence,old[0]+1) if old is not None else sequence
                payload=(prepared[2] if prepared is not None else
                         zlib.compress(json.dumps(heavy,ensure_ascii=False,separators=(',',':')).encode('utf-8'),1))
                body_digest=prepared[3] if prepared is not None else sha256(payload).hexdigest()
                writes.append((*key,revision,payload))
                current[key]=(revision,fingerprint,body_digest)
            manifest.append(dict(home=key[0],sid=key[1],revision=revision,body_digest=body_digest,
                state={k:v for k,v in session.items() if k in volatile}))
        header={k:v for k,v in snapshot.items() if k not in ('sessions','request_activity')}
        header.update(collection_schema=2,session_manifest=manifest)
        old_header=getattr(self,'published_header',None) if previous is self.published else None
        with self.db:
            if not self.db.in_transaction:self.db.execute('BEGIN IMMEDIATE')
            # Only the complete catalog proves which derived bodies disappeared.
            # Keep unloaded checkpoint bodies during restart, and compare their
            # actual committed bytes before reusing them under the writer lock.
            retained=(set(self.db.execute('SELECT home,sid FROM sessions')) if previous is not self.published
                      else getattr(self,'retained_session_keys',set()))
            if writes:
                entries={(entry['home'],entry['sid']):entry for entry in manifest}
                changed=[]
                for home,sid,revision,payload in writes:
                    key=(home,sid)
                    saved=(self.db.execute('SELECT revision FROM sessions WHERE home=? AND sid=? AND payload=?',
                        (home,sid,payload)).fetchone() if key not in previous else None)
                    if saved is None or type(saved[0]) is not int:changed.append((home,sid,revision,payload))
                    else:
                        current[key]=(saved[0],current[key][1],current[key][2])
                        entries[key]['revision']=saved[0]
                writes=changed
            if snapshot.get('activity_stream'):
                prior_source=getattr(self,'published_activity_source',None)
                prior_limit=getattr(self,'published_activity_limit',0)
                if prior_source is None:
                    stored=self.db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
                    prior=json.loads(zlib.decompress(stored[0])) if stored else {}
                    prior_source=prior.get('activity_source');prior_limit=prior.get('activity_source_limit',0)
                activity_row_revision=self.db.execute('SELECT coalesce(max(revision),0) FROM activity').fetchone()[0]
                if prior_source!=snapshot.get('activity_source') or snapshot.get('activity_source_limit',prior_limit)<prior_limit:
                    self.db.execute('DELETE FROM activity')
                for record in snapshot.get('request_activity',[]):
                    payload=zlib.compress(json.dumps(record,ensure_ascii=False,separators=(',',':')).encode(),1)
                    old=self.db.execute('SELECT payload FROM activity WHERE home=? AND attempt=?',
                        (record['home'],record['attempt'])).fetchone()
                    if old is not None and old[0]==payload:continue
                    activity_row_revision+=1
                    self.db.execute('INSERT OR REPLACE INTO activity VALUES(?,?,?,?,?)',
                        (record['home'],record['attempt'],activity_row_revision,activity_row_revision,payload))
                activities={};activity_revision=activity_row_revision
                header['activity_highwater']=activity_row_revision
                # Older analyzers can still reconstruct a complete activity
                # snapshot from the durable schema during an upgrade handoff.
                header['activity_revision']=activity_row_revision
                if candidate is not None:candidate['header']['activity_highwater']=activity_row_revision
            else:
                activities,activity_revision,activity_row_revision=self._publish_activity(
                    snapshot.get('request_activity',[]),sequence,previous is not self.published,
                    candidate['activities'] if candidate is not None else None)
                if 'request_activity' in snapshot:header['activity_revision']=activity_revision
            encoded_header=SnapshotHeader(header,old_header)
            self.db.executemany('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)',writes)
            removed=previous.keys()-current.keys()
            if snapshot.get('index',{}).get('loading'):
                retained=retained-current.keys()
            else:
                removed|=retained-current.keys()
                retained=set()
            self.db.executemany('DELETE FROM sessions WHERE home=? AND sid=?',removed)
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
        self.retained_session_keys=retained
        self.published_header=encoded_header;self.publish_sequence=sequence
        self.published_activity=activities;self.activity_revision=activity_revision
        self.activity_row_revision=activity_row_revision
        if snapshot.get('activity_stream'):
            self.published_activity_source=snapshot.get('activity_source')
            self.published_activity_limit=snapshot.get('activity_source_limit',0)
        if candidate is not None:
            self.live.publish(candidate)

    def read_header(self):
        return self.read(header_only=True)

    def activity_page(self,result,epoch):
        if not result.get('activity_stream'):return result
        result={key:value for key,value in result.items() if key!='activity_revision'}
        epoch=f"{self.snapshot_path}:{self.scope}:{result.get('activity_source')}"
        highwater=result.get('activity_highwater',0)
        acknowledged=getattr(self,'activity_ack',None)
        after=acknowledged[1] if acknowledged and acknowledged[0]==epoch else 0
        if after>highwater:after=0
        with closing(sqlite3.connect(self.snapshot_path.as_uri()+'?mode=ro',uri=True)) as db:
            rows=db.execute('SELECT revision,payload FROM activity WHERE revision>? AND revision<=? '
                            'ORDER BY revision LIMIT 512',(after,highwater)).fetchall()
        end=rows[-1][0] if len(rows)==512 else highwater
        more=end<highwater
        return {**result,'request_activity':[json.loads(zlib.decompress(row[1])) for row in rows],
            'activity_cursor':(epoch,end),'activity_more':more,
            **({'usage_collection_complete':False,'index':{**result['index'],'loading':True,'usage_complete':False}}
               if more else {})}

    def acknowledge_activity(self,snapshot):
        cursor=snapshot.get('activity_cursor')
        if cursor:self.activity_ack=tuple(cursor)

    def _cached_body(self,key,digest):
        if not digest:return None
        for cache,index in ((getattr(self,'live_session_cache',{}),3),(self.session_cache,2)):
            old=cache.get(key)
            if old is not None and len(old)>index and old[index]==digest:return old[1]
        return None

    def _decode_body(self,key,payload,digest):
        if digest is not None:
            if not isinstance(digest,str) or sha256(payload).hexdigest()!=digest:
                raise CollectionScopeError('수집 결과의 본문 검증값이 일치하지 않습니다.')
            cached=self._cached_body(key,digest)
            if cached is not None:return cached,digest,{}
        value=json.loads(zlib.decompress(payload))
        # Legacy schema-2 bodies stored producer tokens inside the body. Move
        # them to the envelope while deriving the same stable identity once.
        revisions={field:value.pop(field) for field in ('usage_revision','request_revision') if field in value}
        if digest is None:
            encoded=zlib.compress(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode(),1)
            digest=sha256(encoded).hexdigest()
        elif revisions:
            raise CollectionScopeError('수집 결과의 본문 형식이 일치하지 않습니다.')
        return value,digest,revisions

    def _retain_pending(self,result,current,*,live):
        if not getattr(self,'retain_pending',False):return current
        if not result.get('index',{}).get('loading'):
            if live and any(row[2].get('_collection_pending') for row in current.values()):
                # The delta may borrow the current map. Do not mutate it until
                # all body lengths and activity records have been validated.
                return {key:row for key,row in current.items() if not row[2].get('_collection_pending')}
            return current
        previous=self.live_cached if self.live_cached is not None else self.cached
        if previous is None:return current
        from_live=self.live_cached is not None
        cache=self.live_session_cache if from_live else self.session_cache
        homes=set(result.get('homes',())) & set(self.homes)
        for source in previous.get('sessions',()):
            key=source['home'],source['id']
            if key in current or key[0] not in homes:continue
            old=cache.get(key)
            if old is None:continue
            # A partial catalog cannot prove deletion. Keep the same decoded
            # body until validation finishes, and never present it as confirmed.
            # None prevents an old epoch's numeric revision from matching a
            # different body in the new epoch; the content digest remains valid.
            pending={**source,'collection_complete':False,'_collection_pending':True}
            digest=old[3] if from_live else old[2]
            current[key]=((None,old[1],pending,digest) if live else (None,old[1],digest,{}))
            if not live:result['sessions'].append(pending)
        return current

    def _read_live(self,metadata,body):
        same_epoch=self.live_publication and self.live_publication[0]==metadata['publication'][0]
        delta=metadata.get('protocol') in (4,5) and not metadata['full']
        if delta and not same_epoch:raise CollectionScopeError('수집 결과의 이전 publication이 없습니다.')
        offset=0
        def rows(entries,previous,id_key):
            nonlocal offset
            current=(dict(previous) if entries else previous) if delta else {}
            for entry in entries:
                key=(entry['home'],entry[id_key]);revision=entry['revision'];size=entry['size']
                if type(size) is not int or size<0 or offset+size>len(body):
                    raise CollectionScopeError('수집 결과의 본문 길이가 일치하지 않습니다.')
                old=previous.get(key)
                digest=entry.get('body_digest');revisions={}
                if size:
                    payload=memoryview(body)[offset:offset+size];offset+=size
                    if id_key=='sid':value,digest,revisions=self._decode_body(key,payload,digest)
                    else:value=json.loads(zlib.decompress(payload))
                elif old is not None and old[0]==revision:
                    value=old[1]
                    if id_key=='sid':
                        if digest is not None and digest!=old[3]:
                            raise CollectionScopeError('수집 결과의 본문 revision이 일치하지 않습니다.')
                        if digest is None:
                            revisions={field:old[2][field] for field in ('usage_revision','request_revision') if field in old[2]}
                            digest=old[3]
                elif id_key=='sid' and metadata.get('protocol')==5 and digest:
                    value=self._cached_body(key,digest)
                    if value is None:raise CollectionScopeError('수집 결과의 이전 본문이 없습니다.')
                else:raise CollectionScopeError('수집 결과의 본문 revision이 일치하지 않습니다.')
                current[key]=((revision,value,{**value,**revisions,**entry['state']},digest) if id_key=='sid' else (revision,value))
            return current
        sessions=rows(metadata['sessions'],self.live_session_cache if same_epoch else {},'sid')
        result=dict(metadata['header'])
        sessions=self._retain_pending(result,sessions,live=True)
        result['sessions']=[row[2] for row in sessions.values()]
        activities={}
        if metadata['activities'] is not None:
            activities=rows(metadata['activities'],self.live_activity_cache if same_epoch else {},'attempt')
            result['request_activity']=[row[1] for row in activities.values()]
        if offset!=len(body):raise CollectionScopeError('수집 결과의 본문 길이가 일치하지 않습니다.')
        return result,sessions,activities

    def _checkpoint_after_live(self,result,sessions,metadata):
        checkpoint=self.identity or self.pending_checkpoint
        exclusions=set() if self.identity is not None else self.checkpoint_exclusions
        if checkpoint and any(s.get('_collection_pending') for s in result['sessions']):
            previous=self.live_session_cache or self.session_cache
            digest_index=3 if self.live_session_cache else 2
            exclusions=exclusions | {key for key,row in previous.items()
                if key not in sessions or row[digest_index]!=sessions[key][3]}
            # A changed body no longer proves ownership of its checkpoint bytes.
            # Older collectors lack selective proof withdrawal, so stop using
            # the checkpoint with them instead of suppressing a required body.
            if (len(exclusions)>4096 or sum(64+4*sum(map(len,key)) for key in exclusions)>4*1024*1024
                    or exclusions and not metadata.get('checkpoint_filter')):
                checkpoint=None
        else:checkpoint=None
        self.pending_checkpoint=checkpoint
        self.checkpoint_exclusions=exclusions if checkpoint else set()

    def read(self,*,shared=False,header_only=False):
        # __new__ read-only diagnostic channels intentionally remain SQLite-only.
        self.last_read_live=False
        if hasattr(self,'client'):
            # A durable checkpoint proves body ownership, not a live delta base.
            response=self.live_request(header_only=header_only,protocol=5,publication=self.live_publication,
                                       checkpoint=self.identity or self.pending_checkpoint,
                                       checkpoint_exclusions=list(self.checkpoint_exclusions),activity_stream=True)
            if response is not None and response[0].get('ready') is False:
                response=self.live_request(header_only=header_only,protocol=4,publication=self.live_publication,activity_stream=True)
            if response is not None and response[0].get('ready') is False:
                # A retiring collector can still publish fresh protocol-3 data
                # while its replacement waits for the source ownership lock.
                response=self.live_request(header_only=header_only,protocol=3,publication=self.live_publication,activity_stream=True)
            if response is not None:
                metadata,body=response
                if metadata.get('error'):
                    raise CollectionScopeError('사용량 색인에 다른 관측 DB가 연결되어 있습니다. 별도 색인 경로를 사용하세요.')
                if metadata.get('ready') and (header_only or metadata.get('protocol') in (3,4,5)):
                    if header_only:result=metadata['header']
                    else:result,sessions,activities=self._read_live(metadata,body)
                    source=result.get('index',{}).get('path')
                    if not source or os.path.normcase(str(Path(source).resolve()))!=os.path.normcase(str(self.path)):
                        raise CollectionScopeError('수집 결과의 사용량 색인 경로가 일치하지 않습니다.')
                    self.last_read_live=True
                    if not header_only:
                        result=self.activity_page(result,metadata['publication'][0])
                        self._checkpoint_after_live(result,sessions,metadata)
                        self.live_session_cache=sessions;self.live_activity_cache=activities
                        self.live_publication=tuple(metadata['publication']);self.live_cached=result
                        # Recovery and live delivery are alternate owners of one
                        # snapshot, not independent retained history populations.
                        self.session_cache={};self.activity_cache={};self.activity_identity=None
                        self.cached=None;self.identity=None
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
            publication=getattr(self,'live_publication',None)
            if (getattr(self,'live_cached',None) is not None and publication and
                    publication[0]==identity[0] and publication[1]>=identity[1]):
                # A disconnected collector cannot make an older checkpoint
                # newer. Avoid decoding that checkpoint merely to discard it.
                return self.recovered_snapshot(None,identity,shared=shared,header_only=header_only)
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
                    if old is not None and old[0]==revision:heavy,digest,revisions=old[1:]
                    else:
                        stored=db.execute('SELECT revision,payload FROM sessions WHERE home=? AND sid=?',key).fetchone()
                        if stored is None or stored[0]!=revision:raise CollectionScopeError('수집 결과의 세션 revision이 일치하지 않습니다.')
                        heavy,digest,revisions=self._decode_body(key,stored[1],entry.get('body_digest'))
                    current[key]=(revision,heavy,digest,revisions);sessions.append({**heavy,**revisions,**entry['state']})
                result={k:v for k,v in header.items() if k not in ('collection_schema','session_manifest')}
                result['sessions']=sessions
                current=self._retain_pending(result,current,live=False)
                self.session_cache=current
                if 'activity_revision' in header and not header.get('activity_stream'):
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
            self.cached={**header,'sessions':result['sessions']};self.identity=identity
        # Shared records are read-only; each poll owns its envelope and list.
        result=self.activity_page(result,identity[0])
        self.pending_checkpoint=identity if any(s.get('_collection_pending') for s in result['sessions']) else None
        self.checkpoint_exclusions=set()
        self.live_cached=None;self.live_publication=None
        self.live_session_cache={};self.live_activity_cache={}
        return self.recovered_snapshot(result,identity,shared=shared,header_only=False)

    def recovered_snapshot(self,result,identity,*,shared,header_only):
        cached=getattr(self,'live_cached',None)
        publication=getattr(self,'live_publication',None)
        # Wall clocks can move backwards. Only publication order within one
        # collector epoch can prove that an in-memory observation is newer.
        if cached is not None and (result is None or publication and publication[0]==identity[0] and publication[1]>identity[1]):
            result=cached
            if not header_only and publication:result=self.activity_page(result,publication[0])
        if result is None:return None
        if header_only:return {key:value for key,value in result.items() if key not in ('sessions','request_activity')}
        return {**result,'sessions':list(result['sessions'])} if shared else deepcopy(result)

    def close(self):
        if self.live_server:
            self.live.close()
            self.live_server.close();self.live_server=None;self.live=None
        elif self.last_read_live:self.live_request(close=True)
        if self.db:
            try:self.db.execute('DELETE FROM consumers WHERE id=?',(self.client,));self.db.commit()
            finally:self.db.close();self.db=None
        self.session_cache={};self.activity_cache={};self.activity_identity=None
        self.live_session_cache={};self.live_activity_cache={}
        self.cached=None;self.identity=None;self.live_cached=None;self.live_publication=None
        self.pending_checkpoint=None
        self.checkpoint_exclusions=set()
        self.retained_session_keys=set()


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
        self.channel.retain_pending=True
        self.path=self.channel.path
        self.waiter=None
        self.notification_epoch=None

    def notification_connection(self):
        if not self.channel.last_read_live:return None
        epoch=self.channel.live_publication[0]
        if self.waiter is not None and self.notification_epoch!=epoch:self.stop_notifications()
        if self.waiter is None:
            from .collection_transport import PublicationWaiter
            self.waiter=PublicationWaiter(self.channel.companion('.collection-live.json'),
                dict(path=str(self.path),scope=self.channel.scope,client=self.channel.client,homes=self.channel.homes))
            self.notification_epoch=epoch
        self.waiter.arm(list(self.channel.live_publication))
        return self.waiter.receiver

    def consume_notification(self):
        notice=self.waiter.consume()
        if notice is None:return None
        observed=tuple(notice.get('publication') or ())
        current=self.channel.live_publication
        if len(observed)!=2 or type(observed[1]) is not int:return None
        return current is None or observed[0]!=current[0] or observed[1]>current[1]

    def stop_notifications(self):
        if self.waiter is not None:
            self.waiter.close();self.waiter=None
            self.notification_epoch=None
            self.channel.live_request(close=True)

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
        # Freshness belongs to this read, not the shared source snapshot or its
        # error list. A late read must not contaminate a later cache recovery.
        snapshot={**snapshot}
        snapshot.pop('collection_stale',None)
        if now-snapshot['ts']>30:
            snapshot['collection_stale']=True
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
        self.stop_notifications()
        self.channel.close()


class CollectorService:
    """Construct only in the dedicated --usage-collector background process."""
    def __init__(self,homes,path=None,evidence=None):
        self.channel=CollectionChannel(homes,path,evidence)
        legacy_lock=self.channel.path.with_suffix('.collector.lock')
        if legacy_lock.exists() and locked(legacy_lock):
            self.channel.close()
            raise CollectionPending('이전 수집기의 기록 저장과 종료 대기')
        self.lock=ProcessLock(self.channel.companion('.collector.lock'))
        try:self.lock.__enter__()
        except RuntimeError as error:
            self.channel.close();raise CollectionPending('다른 수집기의 실행과 종료 대기') from error
        except BaseException:self.channel.close();raise
        try:
            from .collection_lifecycle import migrate_default_index
            migrate_default_index(self.channel)
        except BaseException:
            self.lock.__exit__(None,None,None);self.channel.close();raise
        self.index=None;self.epoch=uuid.uuid4().hex;self.instance=uuid.uuid4().hex;self.sequence=0;self.last=None
        self.replay_snapshot=None
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

    def poll(self,now=None,*,detailed=True):
        from .index import UsageIndex
        started=time.monotonic()
        # The next source poll supersedes unfinished admission candidates.
        # Release its view before rebuilding any changed owner.
        self.replay_snapshot=None
        if self.index and self.index.replay:self.index.replay.capture_snapshot=None
        now=now or time.time();homes=self.channel.requested_homes(now)
        if self.index and homes!=[str(h) for h in self.index.homes]:
            self.index.close();self.index=None;self.epoch=uuid.uuid4().hex
        if self.index is None:
            self.index=UsageIndex(homes,self.channel.index_path,self.channel.evidence)
            self.index.db.commit()  # Finish schema and TEMP revision setup before replay proofs.
            from .collection_replay import CollectionReplay
            self.index.replay=CollectionReplay.open(self.channel,self.index)
        snapshot=self.index.poll(now)
        source_snapshot=snapshot
        collected=time.monotonic()
        changes=getattr(self.index,'collection_changes',None)
        from .collection_replay import EncodedSession
        snapshot={**snapshot,'sessions':[self.with_epoch(s) for s in snapshot['sessions']],
            'collection':dict(pid=os.getpid(),instance=self.instance,version=VERSION,
                executable=self.executable)}
        self.sequence+=1;self.channel.publish(snapshot,self.epoch,self.sequence,changes=changes)
        if self.index.replay is not None and self.index.replay.capture(source_snapshot,now):
            self.replay_snapshot=source_snapshot
        published_sessions=snapshot['sessions']
        self.last=snapshot
        self.compact_last()
        from .quota_diagnostics import record_slow_cycle
        self.last_slow_report=record_slow_cycle(self.channel.path.with_suffix('.collector.sqlite'),
            dict(source_collection=collected-started,publication=time.monotonic()-collected),
            0,getattr(self,'last_slow_report',float('-inf')))
        if detailed:
            # Diagnostics can explicitly request ordinary public dictionaries.
            # The service loop and self.last retain only encoded closed bodies.
            return {**snapshot,'sessions':[dict(json.loads(zlib.decompress(s.payload)),**s)
                if isinstance(s,EncodedSession) else s for s in published_sessions]}
        return snapshot

    def with_epoch(self,session):
        from .collection_replay import EncodedSession
        state=dict(usage_revision=f'{self.epoch}:{session.get("usage_revision")}')
        if session.get('request_revision') is not None:state['request_revision']=f'{self.epoch}:{session["request_revision"]}'
        return session.with_state(**state) if isinstance(session,EncodedSession) else dict(session,**state)

    def compact_last(self):
        from .collection_replay import EncodedSession
        self.last['sessions']=[self.with_epoch(self.index.monitor.sessions[(s['home'],s['id'])]._replay_body)
            if not isinstance(s,EncodedSession) and hasattr(self.index.monitor.sessions.get((s['home'],s['id'])),'_replay_body')
            else s for s in self.last['sessions']]

    def maintain(self):
        """Admit committed bodies between collection deadlines, without a new poll."""
        if self.replay_snapshot is None:return False
        pending=self.index.replay.capture(self.replay_snapshot,self.replay_snapshot['ts'])
        self.compact_last()
        if not pending:self.replay_snapshot=None
        return pending

    def close(self):
        try:
            self.replay_snapshot=None;self.last=None
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
    fault_path=args.index_path.with_name(args.index_path.name+'.codexon-collector-error.json')
    from .model_evidence import default_path
    evidence=args.evidence_path or (default_path() if args.default_index else args.index_path.with_name('model-evidence.sqlite'))
    scope=os.path.normcase(str(evidence.resolve()))
    try:service=CollectorService(args.codex_home,None if args.default_index else args.index_path,args.evidence_path)
    except CollectionPending:return 0
    except Exception as error:
        publish_collector_fault(fault_path,scope,args.instance,error,facts=dict(start_failed=True))
        return 1
    if args.instance:service.instance=args.instance
    first=True
    try:
        while not service.stopping():
            snapshot=service.poll(detailed=False)
            if first:
                # One bounded record is replaced only on failure or a new healthy
                # service instance. Normal polling adds no marker writes.
                from .observer_control import atomic_write
                try:atomic_write(fault_path,json.dumps(dict(scope=sha256(service.channel.scope.encode('utf-8')).hexdigest(),
                    instance=service.instance,at=snapshot['ts'],facts={})).encode('utf-8'))
                except OSError:pass
                first=False
            deadline=time.monotonic()+(.1 if snapshot['index']['loading'] else 3)
            while time.monotonic()<deadline:
                if not service.maintain():
                    time.sleep(max(0,deadline-time.monotonic()))
                    break
    except Exception as error:
        facts=dict(getattr(service.index,'collection_fault',{}) or {})
        publish_collector_fault(fault_path,service.channel.scope,service.instance,error,facts=facts)
        raise
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
