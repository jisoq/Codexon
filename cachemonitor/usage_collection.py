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
    try:encoded=marshal.dumps(value,4)
    except ValueError:
        # JSON also accepts dict/list subclasses that marshal does not accept.
        encoded=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return sha256(encoded).digest()


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

    def companion(self,suffix):
        return self.path.with_name(self.path.name+'.codexon-'+suffix.lstrip('.'))

    def scope_key(self,evidence):
        return os.path.normcase(str(Path(evidence or self.default_evidence).resolve()))

    def subscribe(self,now):
        self.db.execute('INSERT OR REPLACE INTO consumers VALUES(?,?,?,?)',
            (self.client,self.scope,json.dumps(self.homes),now));self.db.commit()

    def requested_homes(self,now):
        homes=list(self.homes)
        for scope,value in self.db.execute('SELECT scope,homes FROM consumers WHERE observed>=? ORDER BY observed,id',(now-60,)):
            if scope==self.scope:homes.extend(home for home in json.loads(value) if home not in homes)
        return homes

    def publish(self,snapshot,epoch,sequence):
        volatile={'remaining','totals','groups','collection_complete','status'}
        previous=self.published if getattr(self,'publish_epoch',None)==epoch else {}
        current={};manifest=[];writes=[]
        for session in snapshot['sessions']:
            key=(session['home'],session['id'])
            heavy={k:v for k,v in session.items() if k not in volatile}
            fingerprint=publication_fingerprint(heavy)
            old=previous.get(key)
            if old is not None and fingerprint==old[1]:
                revision=old[0];current[key]=old
            else:
                revision=sequence
                encoded=json.dumps(heavy,ensure_ascii=False,separators=(',',':')).encode('utf-8')
                writes.append((*key,revision,zlib.compress(encoded,1)))
                current[key]=(revision,fingerprint)
            manifest.append(dict(home=key[0],sid=key[1],revision=revision,
                state={k:v for k,v in session.items() if k in volatile}))
        old_activity=self.published_activity if previous is self.published else {}
        activities={};activity_writes=[]
        for position,record in enumerate(snapshot.get('request_activity',[])):
            key=(record['home'],record['attempt']);old=old_activity.get(key)
            fingerprint=publication_fingerprint(record)
            if old and old[0]==position and old[1]==fingerprint:activities[key]=old
            else:
                activities[key]=(position,fingerprint)
                activity_writes.append((*key,sequence,position,zlib.compress(json.dumps(record,ensure_ascii=False,separators=(',',':')).encode(),1)))
        removed_activity=old_activity.keys()-activities.keys()
        activity_revision=sequence if activity_writes or removed_activity or previous is not self.published else getattr(self,'activity_revision',sequence)
        header={k:v for k,v in snapshot.items() if k not in ('sessions','request_activity')}
        if 'request_activity' in snapshot:header['activity_revision']=activity_revision
        header.update(collection_schema=2,session_manifest=manifest)
        payload=zlib.compress(json.dumps(header,ensure_ascii=False,separators=(',',':')).encode('utf-8'),1)
        with self.db:
            if previous is not self.published:
                self.db.execute('DELETE FROM sessions');self.db.execute('DELETE FROM activity')
            self.db.executemany('INSERT OR REPLACE INTO activity VALUES(?,?,?,?,?)',activity_writes)
            self.db.executemany('DELETE FROM activity WHERE home=? AND attempt=?',removed_activity)
            self.db.executemany('INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)',writes)
            self.db.executemany('DELETE FROM sessions WHERE home=? AND sid=?',self.published.keys()-current.keys())
            self.db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?,?,?,?,?)',
                (self.scope,epoch,sequence,snapshot['ts'],payload))
        self.published=current;self.publish_epoch=epoch
        self.published_activity=activities;self.activity_revision=activity_revision

    def read_header(self):
        return self.read(header_only=True)

    def read(self,*,shared=False,header_only=False):
        with closing(sqlite3.connect(self.snapshot_path.as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('BEGIN')
            row=db.execute('SELECT scope,epoch,sequence FROM snapshot WHERE id=1').fetchone()
            if not row:return None
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
                return {k:v for k,v in header.items() if k not in ('sessions','session_manifest','collection_schema')}
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
        return {**result,'sessions':list(result['sessions'])} if shared else deepcopy(result)

    def close(self):
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
        try:snapshot=self.channel.read(shared=True)
        except CollectionScopeError as error:
            snapshot=empty_snapshot(self.channel.homes,self.path,now)
            snapshot['errors']=[str(error)];snapshot['usage_errors']=[str(error)]
            snapshot['index']['loading']=False
            return snapshot
        self.channel.subscribe(now)
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
            # The GUI registers all monitored homes. Cache consumers need only
            # status; never expose aggregates from other homes.
            snapshot['cache_management']={}
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
            time.sleep(.1 if snapshot['index']['loading'] else 2)
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
