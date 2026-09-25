"""Read-only collection clients and an independent background collector service."""
from contextlib import closing
from copy import deepcopy
import json
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


class CollectionScopeError(RuntimeError):
    pass


def index_location(path=None):
    return (Path(path) if path else Path(os.environ.get('LOCALAPPDATA',Path.home()))/'CacheMonitor'/'usage-index.sqlite').resolve()


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
        if tables-{'snapshot','consumers','control'}:
            self.db.close()
            raise ValueError('다른 데이터베이스를 수집 통신용으로 사용할 수 없습니다')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, scope TEXT, epoch TEXT, sequence INTEGER, observed REAL, payload BLOB)')
        self.db.execute('CREATE TABLE IF NOT EXISTS consumers (id TEXT PRIMARY KEY, scope TEXT, homes TEXT, observed REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS control (instance TEXT PRIMARY KEY, action TEXT)')
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
        payload=zlib.compress(json.dumps(snapshot,ensure_ascii=False,separators=(',',':')).encode('utf-8'),1)
        self.db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?,?,?,?,?)',
            (self.scope,epoch,sequence,snapshot['ts'],payload));self.db.commit()

    def read(self):
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
                self.cached=json.loads(zlib.decompress(payload));self.identity=identity
            source=self.cached.get('index',{}).get('path')
            if not source or os.path.normcase(str(Path(source).resolve()))!=os.path.normcase(str(self.path)):
                raise CollectionScopeError('수집 결과의 사용량 색인 경로가 일치하지 않습니다.')
        return deepcopy(self.cached)

    def close(self):
        if self.db:
            try:self.db.execute('DELETE FROM consumers WHERE id=?',(self.client,));self.db.commit()
            finally:self.db.close();self.db=None


def empty_snapshot(homes,path,now):
    return dict(ts=now,homes=homes,sessions=[],errors=[],unassigned=[],
        usage_collection_complete=False,last_usage_collection_success=None,usage_errors=[],
        index=dict(loading=True,done=0,files=0,bytes_read=0,version=0,path=str(path)))


class CollectionClient:
    """GUI/cache clients never instantiate a source index or acquire its lock."""
    def __init__(self,homes,path=None,model_evidence_path=None,*,autostart=True):
        self.channel=CollectionChannel(homes,path,model_evidence_path)
        self.path=self.channel.path;self.next_start=0;self.autostart=autostart

    def command(self):
        parts=[sys.executable]
        if not getattr(sys,'frozen',False):parts.append(str(Path(__file__).resolve().parents[1]/'run.py'))
        parts+=['--usage-collector','--index-path',str(self.path)]
        if self.channel.index_path is None:parts.append('--default-index')
        for home in self.channel.homes:parts+=['--codex-home',home]
        if self.channel.evidence:parts+=['--evidence-path',str(self.channel.evidence)]
        return parts

    def ensure_service(self,now,snapshot):
        if not self.autostart or now<self.next_start:return
        collection=(snapshot or {}).get('collection',{})
        version=collection.get('version')
        newer=bool(version and tuple(int(p) for p in version.split('.') if p.isdigit())>
                   tuple(int(p) for p in VERSION.split('.')))
        different_binary=bool(getattr(sys,'frozen',False) and
            collection.get('executable')!=str(Path(sys.executable).resolve()))
        if different_binary and version==VERSION:
            from .installation import installed
            active=installed().get('AppPath')
            if active and Path(active).resolve()!=Path(sys.executable).resolve():different_binary=False
        replace=bool(collection and not newer and (version!=VERSION or different_binary))
        if newer:return
        if snapshot and now-snapshot['ts']<=30 and not replace:return
        self.next_start=now+30
        from .observer_task import ObserverTask
        # Task Scheduler owns lifetime/restart outside the GUI's process tree.
        # Only the dedicated executable acquires the source collector lock.
        task=ObserverTask(str(self.path),role='UsageCollector')
        if replace:
            # Register the replacement before requesting a cooperative stop.
            # Never terminate a GUI, cache worker, or active proxy connection.
            with ProcessLock(self.channel.companion('.collector-update.lock')):
                current=self.channel.read()
                if not current or current.get('collection',{}).get('instance')!=collection['instance']:return
                task.configure(self.command(),autostart=False)
                self.channel.db.execute('INSERT OR REPLACE INTO control VALUES(?,?)',(collection['instance'],'stop'))
                self.channel.db.commit()
                deadline=time.monotonic()+3
                while locked(self.channel.companion('.collector.lock')):
                    if time.monotonic()>=deadline:raise RuntimeError('백그라운드 수집기 교체 대기')
                    time.sleep(.05)
        task.start(self.command(),autostart=False)

    def poll(self,now=None):
        now=now or time.time()
        try:snapshot=self.channel.read()
        except CollectionScopeError as error:
            snapshot=empty_snapshot(self.channel.homes,self.path,now)
            snapshot['errors']=[str(error)];snapshot['index']['loading']=False
            return snapshot
        self.channel.subscribe(now)
        start_error=False
        try:self.ensure_service(now,snapshot)
        except (OSError,RuntimeError,subprocess.SubprocessError):start_error=True
        if snapshot is None:snapshot=empty_snapshot(self.channel.homes,self.path,now)
        elif now-snapshot['ts']>30:
            snapshot['errors']=list(dict.fromkeys([*snapshot.get('errors',[]),'수집 결과 갱신 지연']))
            snapshot['usage_collection_complete']=False
            snapshot['index']={**snapshot['index'],'usage_complete':False}
        if start_error:snapshot['errors']=[*snapshot.get('errors',[]),'백그라운드 수집기 시작 지연']
        homes=set(self.channel.homes)
        primary_matches=bool(snapshot['homes'] and self.channel.homes and snapshot['homes'][0]==self.channel.homes[0])
        if not primary_matches:snapshot['quota']=None
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
            snapshot['cache_management']={};snapshot['quota']=None
        snapshot['homes']=self.channel.homes
        return snapshot

    def close(self):
        self.channel.close()


class CollectorService:
    """Construct only in the dedicated --usage-collector background process."""
    def __init__(self,homes,path=None,evidence=None):
        self.channel=CollectionChannel(homes,path,evidence)
        self.lock=ProcessLock(self.channel.companion('.collector.lock'))
        try:self.lock.__enter__()
        except BaseException:self.channel.close();raise
        self.index=None;self.epoch=uuid.uuid4().hex;self.instance=uuid.uuid4().hex;self.sequence=0;self.last=None

    def stopping(self):
        return self.channel.db.execute("SELECT 1 FROM control WHERE instance=? AND action='stop'",(self.instance,)).fetchone() is not None

    def poll(self,now=None):
        from .index import UsageIndex
        now=now or time.time();homes=self.channel.requested_homes(now)
        if self.index and homes!=[str(h) for h in self.index.homes]:
            self.index.close();self.index=None;self.epoch=uuid.uuid4().hex
        if self.index is None:self.index=UsageIndex(homes,self.channel.index_path,self.channel.evidence)
        snapshot=self.index.poll(now)
        snapshot={**snapshot,'sessions':[dict(s,usage_revision=f'{self.epoch}:{s.get("usage_revision")}') for s in snapshot['sessions']],
            'collection':dict(pid=os.getpid(),instance=self.instance,version=VERSION,
                executable=str(Path(sys.executable).resolve()))}
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
    client=CollectionClient(homes,path,evidence,autostart=False)
    try:previous=client.channel.read()
    except BaseException:client.close();raise
    if reuse and previous and locked(client.channel.companion('.collector.lock')):
        client.close();return lambda:None
    instance=uuid.uuid4().hex
    child=subprocess.Popen(client.command()+['--instance',instance],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
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
