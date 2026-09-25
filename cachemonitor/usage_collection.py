"""Read-only collection clients and an independent background collector service."""
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
import zlib

from .observer_state import ProcessLock
from .version import VERSION


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
        self.scope=str(Path(evidence).resolve()) if evidence else ''
        self.snapshot_path=self.path.with_suffix('.collection.sqlite')
        self.snapshot_path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.snapshot_path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS snapshot (id INTEGER PRIMARY KEY, scope TEXT, epoch TEXT, sequence INTEGER, observed REAL, payload BLOB)')
        self.db.execute('CREATE TABLE IF NOT EXISTS consumers (id TEXT PRIMARY KEY, scope TEXT, homes TEXT, observed REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS control (instance TEXT PRIMARY KEY, action TEXT)')
        self.client=uuid.uuid4().hex;self.cached=None;self.identity=None

    def subscribe(self,now):
        self.db.execute('INSERT OR REPLACE INTO consumers VALUES(?,?,?,?)',
            (self.client,self.scope,json.dumps(self.homes),now));self.db.commit()

    def requested_homes(self,now):
        homes=set(self.homes)
        for row in self.db.execute('SELECT homes FROM consumers WHERE scope=? AND observed>=?',(self.scope,now-60)):
            homes.update(json.loads(row[0]))
        return sorted(homes)

    def publish(self,snapshot,epoch,sequence):
        payload=zlib.compress(json.dumps(snapshot,ensure_ascii=False,separators=(',',':')).encode('utf-8'),1)
        self.db.execute('INSERT OR REPLACE INTO snapshot VALUES(1,?,?,?,?,?)',
            (self.scope,epoch,sequence,snapshot['ts'],payload));self.db.commit()

    def read(self):
        with closing(sqlite3.connect(self.snapshot_path.as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('BEGIN')
            row=db.execute('SELECT scope,epoch,sequence FROM snapshot WHERE id=1').fetchone()
            if not row or row[0]!=self.scope:return None
            identity=(row[1],row[2])
            if identity!=self.identity:
                payload=db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0]
                self.cached=json.loads(zlib.decompress(payload));self.identity=identity
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
    def __init__(self,homes,path=None,model_evidence_path=None,*,worker=False,autostart=True):
        self.channel=CollectionChannel(homes,path,model_evidence_path)
        self.path=self.channel.path;self.next_start=0;self.autostart=autostart
        self.worker_marker=None
        if worker:
            self.worker_marker=ProcessLock(self.path.with_suffix('.worker-consumer.lock'))
            self.worker_marker.__enter__()

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
        if snapshot and now-snapshot['ts']<=30:return
        self.next_start=now+30
        from .observer_task import ObserverTask
        # Task Scheduler owns lifetime/restart outside the GUI's process tree.
        # Only the dedicated executable acquires the source collector lock.
        ObserverTask(str(self.path),role='UsageCollector').start(self.command(),autostart=False)

    def poll(self,now=None):
        now=now or time.time();self.channel.subscribe(now)
        snapshot=self.channel.read()
        start_error=False
        try:self.ensure_service(now,snapshot)
        except (OSError,RuntimeError):start_error=True
        if snapshot is None:snapshot=empty_snapshot(self.channel.homes,self.path,now)
        elif now-snapshot['ts']>30:
            snapshot['errors']=list(dict.fromkeys([*snapshot.get('errors',[]),'수집 결과 갱신 지연']))
            snapshot['usage_collection_complete']=False
            snapshot['index']={**snapshot['index'],'usage_complete':False}
        if start_error:snapshot['errors']=[*snapshot.get('errors',[]),'백그라운드 수집기 시작 지연']
        homes=set(self.channel.homes)
        if set(snapshot['homes'])!=homes:
            missing=not homes.issubset(snapshot['homes'])
            snapshot={**snapshot,'homes':self.channel.homes,
                'sessions':[s for s in snapshot['sessions'] if s['home'] in homes],
                'request_activity':[r for r in snapshot.get('request_activity',[]) if r.get('home') in homes]}
            if missing:
                snapshot['index']={**snapshot['index'],'loading':True,'usage_complete':False}
                snapshot['usage_collection_complete']=False
            # The GUI registers all monitored homes. Cache consumers need only
            # status; never expose aggregates from other homes.
            snapshot['cache_management']={};snapshot['quota']=None
        return snapshot

    def close(self):
        self.channel.close()
        if self.worker_marker:self.worker_marker.__exit__(None,None,None);self.worker_marker=None


class CollectorService:
    """Construct only in the dedicated --usage-collector background process."""
    def __init__(self,homes,path=None,evidence=None):
        self.channel=CollectionChannel(homes,path,evidence)
        self.lock=ProcessLock(self.channel.path.with_suffix('.collector.lock'))
        try:self.lock.__enter__()
        except BaseException:self.channel.close();raise
        self.index=None;self.epoch=uuid.uuid4().hex;self.instance=uuid.uuid4().hex;self.sequence=0;self.last=None

    def stopping(self):
        return self.channel.db.execute("SELECT 1 FROM control WHERE instance=? AND action='stop'",(self.instance,)).fetchone() is not None

    def poll(self,now=None):
        from .index import UsageIndex
        now=now or time.time();homes=self.channel.requested_homes(now)
        # Preserve active connections during upgrade from a collecting worker.
        # Only this service adapts its sanitized index; clients never scan it.
        legacy=bool(self.channel.index_path is not None
            and locked(self.channel.path.with_name('cache-worker.lock'))
            and not locked(self.channel.path.with_suffix('.worker-consumer.lock')))
        if legacy and not self.channel.path.exists():
            snapshot=empty_snapshot(homes,self.channel.path,now)
        else:
            if self.index and (homes!=[str(h) for h in self.index.homes] or self.index.read_only!=legacy):
                self.index.close();self.index=None;self.epoch=uuid.uuid4().hex
            if self.index is None:self.index=UsageIndex(homes,self.channel.index_path,self.channel.evidence,read_only=legacy)
            snapshot=self.index.poll(now)
        snapshot={**snapshot,'sessions':[dict(s,usage_revision=f'{self.epoch}:{s.get("usage_revision")}') for s in snapshot['sessions']],
            'collection':dict(pid=os.getpid(),instance=self.instance,version=VERSION,mode='indexed_compatibility' if legacy else 'dedicated')}
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
    args=parser.parse_args()
    try:service=CollectorService(args.codex_home,None if args.default_index else args.index_path,args.evidence_path)
    except RuntimeError:return 0
    try:
        while not service.stopping():
            snapshot=service.poll()
            time.sleep(.1 if snapshot['index']['loading'] else 2)
    finally:service.close()


def isolated_collector(homes,path,evidence=None):
    """QA owns a dedicated child service; never register a persistent test task."""
    import subprocess
    client=CollectionClient(homes,path,evidence,autostart=False)
    previous=client.channel.read()
    previous_instance=(previous or {}).get('collection',{}).get('instance')
    instance=None
    child=subprocess.Popen(client.command(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
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
            if candidate and candidate!=previous_instance:
                instance=candidate;return cleanup
            if child.poll() is not None:raise RuntimeError('격리 수집기를 시작하지 못했습니다')
            time.sleep(.05)
        raise RuntimeError('격리 수집기 준비 시간 초과')
    except BaseException:cleanup();raise
    finally:client.close()
