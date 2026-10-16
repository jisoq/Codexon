"""Incremental local index. Source databases and rollouts are always read-only."""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import stat
import time
from itertools import islice
from bisect import bisect_right
from collections import defaultdict, OrderedDict
from collections import deque
from pathlib import Path

from .core import LINEAGE_FIELDS, SessionRegistry, Session, readonly, stamp, session_lineage
from .analysis_records import ROW_SIGNATURE_FIELDS
from .codex_names import CodexNames, task_select
from .quota import clean_limits
from .model_evidence import EvidenceReader, default_path as evidence_path, home_key
from .usage_paths import index_location
from .native_backend import get_native
from .event_store import EventStore, index_tables_valid, pack_event, unpack_event


FILE_READ_BUDGET = 16 * 1024 * 1024
MAX_SOURCE_WORK = 32
PREFIX_PRIORITY_BUDGET = 64 * 1024 * 1024
REBUILD_BATCH = 256
DISCOVERY_PATH_BUDGET = 8 * 1024 * 1024


def _resolved_catalog_paths(rows):
    """Resolve shared parents once per discovery, with bounded temporary state.

    Only enumerated regular files use the shared result. Missing names, aliases
    and reparse points keep the ordinary per-path resolver in the caller.
    This map never replaces scan_file's source identity or prefix validation.
    """
    groups={};budget=DISCOVERY_PATH_BUDGET
    for row in rows:
        raw=row[0]
        if not isinstance(raw,str) or not raw:continue
        # Allow for Python containers, directory keys and resolved path strings.
        weight=512+4*len(raw)
        if weight>budget:break
        budget-=weight
        parent,name=os.path.split(raw)
        groups.setdefault(parent,{})[name]=raw
    resolved={}
    for parent,wanted in groups.items():
        if len(wanted)<8:continue
        try:
            directory=Path(parent).resolve()
            with os.scandir(directory) as entries:
                for entry in islice(entries,max(256,4*len(wanted))):
                    raw=wanted.get(entry.name)
                    if raw is None or not entry.is_file(follow_symlinks=False):continue
                    if os.name=='nt' and entry.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:continue
                    resolved[raw]=str(directory/entry.name).removeprefix('\\\\?\\')
        except (OSError,RuntimeError):pass
    return resolved


def _event_lines(source, digest, limit=16 * 1024 * 1024, state=None):
    from .event_stream import EventLines
    state={} if state is None else state
    reader=state.get('reader')
    if reader is None:
        reader=state['reader']=EventLines(source.tell(),digest)
    before=reader.bytes_read
    try:
        yield from reader.read(source,limit)
    finally:
        state['digest']=reader.committed_digest.copy()
        state['read_bytes']=state.get('read_bytes',0)+reader.bytes_read-before


def usage_only(value):
    if not isinstance(value, dict):
        return {}
    keys = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
            'output_tokens', 'reasoning_output_tokens', 'total_tokens')
    result={k: value[k] for k in keys if type(value.get(k)) is int}
    for outer,allowed in (('input_tokens_details',('cached_tokens','cache_write_tokens')),
                          ('output_tokens_details',('reasoning_tokens',))):
        details=value.get(outer)
        if isinstance(details,dict):result[outer]={k:details[k] for k in allowed if type(details.get(k)) is int}
    return result


def sanitized(event):
    """Never persist messages, instructions, tool arguments or credentials."""
    kind, p = event.get('type'), event.get('payload')
    if not isinstance(p, dict):
        return None
    if kind=='compacted':
        clean={}
    elif kind == 'turn_context':
        settings = (p.get('collaboration_mode') or {}).get('settings') or {}
        clean = {k: p.get(k) for k in ('turn_id', 'model')}
        clean['effort'] = p.get('effort') or p.get('reasoning_effort') or settings.get('reasoning_effort')
        if 'service_tier' in p:
            clean['service_tier']=p['service_tier'] if isinstance(p['service_tier'],str) or p['service_tier'] is None else '미확인'
    elif kind == 'token_usage_record':
        clean = {k: p.get(k) for k in ('thread_id', 'turn_id', 'response_id', 'model') if p.get(k)}
        clean['usage'] = usage_only(p.get('usage'))
        if isinstance(p.get('service_tier'),str):
            clean['service_tier']=p['service_tier']
        if isinstance(p.get('thread_token_usage'), dict):
            clean['thread_token_usage'] = usage_only(p['thread_token_usage'])
    elif kind == 'event_msg':
        typ = p.get('type')
        if typ == 'token_count':
            info = p.get('info') or {}
            clean = {'type': typ, 'info': {k: usage_only(info.get(k)) for k in ('total_token_usage', 'last_token_usage')}}
            limits=clean_limits(p.get('rate_limits'))
            if limits is not None: clean['rate_limits']=limits
        elif typ in ('task_started', 'task_complete', 'task_completed', 'turn_aborted','context_compacted'):
            clean = {'type': typ, 'turn_id': p.get('turn_id')}
        elif typ=='thread_settings_applied':
            settings=p.get('thread_settings')
            if not isinstance(settings,dict) or 'service_tier' not in settings:return None
            tier=settings['service_tier']
            clean={'type':typ,'thread_id':p.get('thread_id'),'thread_settings':{
                'service_tier':tier if isinstance(tier,str) else None}}
        else:
            return None
    else:
        return None
    return {'type': kind, 'timestamp': event.get('timestamp'), 'payload': clean}


class UsageIndex:
    def __init__(self, homes, path=None, model_evidence_path=None):
        self.read_only=False
        self.homes = [Path(h).resolve() for h in homes]
        self.path = index_location(path)
        if any(self.path.resolve().is_relative_to(h) for h in self.homes):
            raise ValueError('앱 색인은 Codex 원본 폴더 밖에 저장해야 합니다')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        tables = {r[0] for r in self.db.execute("select name from sqlite_master where type='table'")}
        if not index_tables_valid(tables):
            self.db.close()
            raise ValueError('다른 데이터베이스를 앱 색인으로 사용할 수 없습니다')
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, home TEXT, tid TEXT,
                offset INTEGER, size INTEGER, mtime INTEGER, inode TEXT);
            CREATE TABLE IF NOT EXISTS metadata(home TEXT, tid TEXT, data TEXT, PRIMARY KEY(home,tid));
        ''')
        if 'events' in tables and [r[2] for r in self.db.execute('PRAGMA index_info(events_session)')]!=['home','tid','ts','path','pos']:
            self.db.execute('DROP INDEX IF EXISTS events_session')
            self.db.execute('CREATE INDEX events_session ON events(home,tid,ts,path,pos)')
            self.db.commit()
        self.db.execute('CREATE TABLE IF NOT EXISTS usage_archive(home TEXT,response_id TEXT,data TEXT,PRIMARY KEY(home,response_id))')
        # Recover settings snapshots and top-level compactions omitted by older
        # scanners. Keep existing events while this incremental backfill proceeds.
        self.tier_backfill=self.db.execute('pragma user_version').fetchone()[0]<4
        if self.tier_backfill:
            self.db.execute('update files set offset=0')
            self.db.execute('pragma user_version=4')
            self.db.commit()
        try:
            self.event_store=EventStore(self.db,self.path)
            self.event_store.migrate(time.monotonic()+.025)
            self._initialize_runtime(model_evidence_path,custom_index=path is not None)
        except BaseException:
            self.db.close();raise

    @classmethod
    def for_replay(cls, path):
        """Read either event format for diagnostics without migrating evidence."""
        self=cls.__new__(cls)
        self.path=Path(path).resolve();self.homes=[];self.read_only=True;self.tier_backfill=False
        self.db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
        try:
            tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not index_tables_valid(tables):raise ValueError('사용량 색인의 형식을 확인하지 못했습니다')
            self.event_store=EventStore(self.db,self.path,read_only=True)
            self._initialize_runtime(None,custom_index=True,all_metadata=True)
            return self
        except BaseException:
            self.db.close();raise

    def _initialize_runtime(self, model_evidence_path, *, custom_index, all_metadata=False):
        self.monitor = SessionRegistry()
        self.replay = None
        selected_homes={str(h) for h in self.homes}
        self.metadata = {(r[0],r[1]):json.loads(r[2]) for r in self.db.execute('select home,tid,data from metadata') if all_metadata or r[0] in selected_homes}
        self.files = []
        self.queue = deque()
        self.last_discovery = 0
        self.source_catalog = None
        self.initial = True
        # Retained sanitized events outlive source files. Restore their owners
        # once, after current sources have completed their ordinary validation.
        homes_key=tuple(dict.fromkeys(str(home) for home in self.homes))
        self.cached_restore=deque(self.event_store.owners(homes_key)) if homes_key else deque()
        self.bytes_read = 0
        self.errors = []
        self.collection_fault = {}
        self.loaded = set()
        self.version = 0
        self.done_files = 0
        self.turn_states = {}
        self.event_tails = {}
        self.new_events = defaultdict(list)
        self.rebuild_required = set()
        self.rebuild_jobs = {}
        self.pending_rebuilds = deque()
        self.rebuild_deadline = None
        self.source_hashes = {}
        # Only unfinished files own live hash state; overflow waits in the file queue.
        self.source_work = {}
        self.partial_work = OrderedDict()
        self.prefix_priority = 0
        self.view_signatures = {}
        self.history_signatures = {}
        self.session_revisions = {}
        self.request_revisions = {}
        self.publication_rows = {}
        self.publication_revision = 0
        self.publication_owner = object()
        self.collection_changes = None
        self.activity_publication = None
        self.activity_rows = []
        self.activity_tokens = {}
        self.model_evidence = EvidenceReader(model_evidence_path if model_evidence_path is not None else
                                            self.path.with_name('model-evidence.sqlite') if custom_index else evidence_path())
        self.quota_by_home = {}
        self.quota_seeded = False
        self.last_usage_success = None
        self.names = {}
        self.lineage = {key:{field:meta[field] for field in LINEAGE_FIELDS if field in meta}
                        for key,meta in self.metadata.items() if meta.get('parent_thread_id')}
        from .usage_archive import ArchiveSessions
        self.archive = (ArchiveSessions(self.db) if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='usage_archive' AND type='table'").fetchone() else None)

    def observe_quota(self,home,limits,observed):
        if limits is None or not observed: return
        key=str(home)
        old=self.quota_by_home.get(key)
        if old is None or observed>=old['observed_at']:
            self.quota_by_home[key]={**limits,'observed_at':observed,'home':key}

    def seed_quota(self):
        # Existing indexes predate quota storage. Read bounded tails once, never reindex conversation bodies.
        for home in self.homes:
            candidates=[]
            for path,file_home,_ in self.files:
                if Path(file_home)!=home: continue
                try: candidates.append((Path(path).stat().st_mtime,path))
                except OSError: continue
            for _,path in sorted(candidates,reverse=True)[:8]:
                try:
                    with open(path,'rb') as source:
                        source.seek(0,2); size=source.tell(); offset=max(0,size-1024*1024)
                        source.seek(offset)
                        if offset: source.readline()
                        lines=source.read().splitlines()
                    for line in lines:
                        if b'"rate_limits"' not in line: continue
                        try:
                            event=json.loads(line)
                            payload=event.get('payload') or {}
                            if event.get('type')!='event_msg' or payload.get('type')!='token_count': continue
                            self.observe_quota(home,clean_limits(payload.get('rate_limits')),stamp(event.get('timestamp')))
                        except (ValueError,TypeError,AttributeError): continue
                except (OSError,ValueError,TypeError,AttributeError): continue

    def discover(self, now):
        paths = {}
        for home in self.homes:
            try:
                with readonly(home / 'state_5.sqlite') as db:
                    cols = {r[1] for r in db.execute('pragma table_info(threads)')}
                    names = CodexNames(home, db)
                    self.names[str(home)] = names
                    catalog=db.execute('SELECT rollout_path FROM threads')
                    try:resolved=_resolved_catalog_paths(catalog)
                    finally:catalog.close()
                    for row in db.execute('select ' + task_select(cols) + ' from threads'):
                        meta = dict(row)
                        key = (str(home), row['id'])
                        # Read only the immutable header for lineage. Existing
                        # event offsets and historical mode values are untouched.
                        if key not in self.lineage and row['rollout_path']:
                            try:
                                with Path(row['rollout_path']).open('rb') as source:
                                    header=json.loads(source.readline(2*1024*1024))
                                if header.get('type')=='session_meta':
                                    self.lineage[key]=session_lineage(header.get('payload'),row['id'])
                            except (OSError,ValueError,TypeError):
                                pass
                        meta.update(self.lineage.get(key,{}))
                        if meta.get('parent_thread_id'):meta['thread_source']='subagent'
                        meta.update(names.resolve(meta))
                        if self.metadata.get(key)!=meta:
                            self.metadata[key] = meta
                            self.db.execute('insert or replace into metadata values(?,?,?)',(*key,json.dumps(meta,ensure_ascii=False)))
                        if row['rollout_path']:
                            source=resolved.get(row['rollout_path'])
                            if source is None:source=str(Path(row['rollout_path']).resolve()).removeprefix('\\\\?\\')
                            paths[source] = (home, row['id'])
            except (OSError, sqlite3.Error) as exc:
                self.errors.append(f'{home.name} / 세션 목록: {exc}')
                self.collection_fault['read_failed']=True
            for folder in ('sessions', 'archived_sessions'):
                for path in (home / folder).rglob('*.jsonl'):
                    # Catalog entries were already resolved in this discovery.
                    # An exact match needs no second filesystem resolution.
                    if str(path).removeprefix('\\\\?\\') in paths:continue
                    resolved = str(path.resolve()).removeprefix('\\\\?\\')
                    if resolved not in paths:
                        # A filename is not proof of ownership; session_meta supplies the ID.
                        paths[resolved] = (home, None)
        # Replacing the discovery queue also releases owners that can no longer run.
        self.source_work={path:state for path,state in self.source_work.items() if path in paths}
        self.files = [(path, *value) for path, value in sorted(paths.items())]
        self.queue = deque(self.files)
        if self.tier_backfill:
            def modified(item):
                try:return Path(item[0]).stat().st_mtime_ns
                except OSError:return 0
            self.queue=deque(sorted(self.files,key=modified,reverse=True))
            self.tier_backfill=False
        self.done_files = 0
        self.last_discovery = now

    def validate_cached_prefix(self, source, offset, path, home, tid, state, budget):
        """Check one prefix slice; None means more validation remains."""
        start=state['offset']
        source.seek(start)
        rows=self.db.execute('select pos,data from events where path=? and pos>=? and pos<? order by pos',(path,start,offset))
        expected=next(rows,None)
        try:
            before=state.get('read_bytes',0)
            read_start=state['reader'].read_offset if 'reader' in state else start
            for pos,line in _event_lines(source,state['digest'],min(budget,offset-read_start),state):
                try:
                    event=json.loads(line)
                    if event.get('type')=='session_meta':
                        payload=event.get('payload') or {}
                        if (payload.get('id') or tid)!=tid:return False
                        if session_lineage(payload,tid)!=self.lineage.get((str(home),tid),{}):return False
                    clean=sanitized(event)
                    if clean is None:continue
                    # Let the ordinary scanner retain malformed-source warnings.
                    stamp(clean['timestamp'])
                    clean['event_id']=f'{Path(path).name}:{pos}'
                except (ValueError,TypeError,AttributeError):
                    return False
                if expected is None or expected[0]!=pos:return False
                try:stored=json.loads(unpack_event(expected[1]))
                except (ValueError,TypeError):return False
                if not isinstance(stored,dict):return False
                stored.setdefault('event_id',clean['event_id'])
                if stored!=clean:return False
                expected=next(rows,None)
            if expected is not None and expected[0]<source.tell():return False
            if state.get('reader') and state['reader'].read_offset>=offset and source.tell()!=offset:return False
            return True if source.tell()==offset and expected is None else None
        finally:
            self.bytes_read+=state.get('read_bytes',0)-before
            state['offset']=source.tell()
            rows.close()

    def scan_file(self, path, home, tid):
        p = Path(path)
        try:
            st = p.stat()
        except FileNotFoundError:
            # Historical paths can move to archived_sessions; discovery covers that location.
            self.source_work.pop(path,None)
            return None
        old = self.db.execute('select tid,offset,size,mtime,inode from files where path=?', (path,)).fetchone()
        if (old and path not in self.source_work and old[1] == st.st_size and old[2] == st.st_size
                and old[3] == st.st_mtime_ns and old[4] == str(st.st_ino)):
            self.source_work.pop(path,None)
            key=(str(home),old[0])
            return key if self.initial and key not in self.loaded and not (self.replay and key in self.replay.pending) else None
        offset = old[1] if old else 0
        if old:
            tid = old[0]
            # A pending verification has a durable sentinel, while its live
            # owner still knows which source signature its current pass saw.
            mtime=(self.source_work[path]['signature'][1]
                   if old[3]==-1 and path in self.source_work else old[3])
            if str(st.st_ino) != old[4] or st.st_size < offset or (st.st_size == offset and st.st_mtime_ns != mtime):
                self.db.execute('delete from events where path=?', (path,))
                self.rebuild_required.add((str(home),tid))
                offset = 0
                self.source_work.pop(path,None)
        signature=(st.st_size,st.st_mtime_ns,str(st.st_ino))
        partial=self.partial_work.get(path)
        if partial is not None and partial['signature']==signature:return None
        if partial is not None:self.partial_work.pop(path,None)
        state=self.source_work.get(path)
        restarted = state is not None and state['signature'] != signature
        if restarted:
            prior=state['signature']
            if signature[2]==prior[2] and signature[0]>prior[0]:
                # Growth cannot prove append-only. Finish this fixed pass, but
                # require another pass before reporting collection complete.
                state['signature']=signature;state['recheck']=True
            else:
                self.source_work.pop(path,None);state=None
        if state is None:
            if len(self.source_work)>=MAX_SOURCE_WORK:
                self.queue.append((path,home,tid))
                return None
            state=dict(signature=signature,offset=0,target=offset,digest=hashlib.sha256(),
                       verified=not offset,known=self.source_hashes.get(path),scan_target=st.st_size)
            self.source_work[path]=state
        with p.open('rb') as f:
            budget=FILE_READ_BUDGET
            # Finish small known prefixes promptly after growth. Larger ones
            # retain their fixed target and progress across bounded slices.
            # Extra verification is at most 64 MiB per poll; buffers stay 1 MiB.
            inline_prefix = (restarted and not state['verified'] and state['known'] is not None
                             and state['target'] <= self.prefix_priority)
            validation_budget = self.prefix_priority if inline_prefix else budget
            if not state['verified']:
                # An increased length alone does not prove append-only: editors can
                # correct old bytes and append in the same write. Verify the consumed
                # prefix before reusing parsed state. A restart recovers its digest
                # by comparing sanitized records, without rewriting cached history.
                known=state['known'];before=self.bytes_read
                if known is None:
                    unchanged=self.validate_cached_prefix(f,state['target'],path,home,tid,state,validation_budget)
                else:
                    f.seek(state['offset'])
                    remaining=min(validation_budget,state['target']-state['offset'])
                    while remaining:
                        block=f.read(min(1024*1024,remaining))
                        if not block:break
                        state['digest'].update(block);remaining-=len(block);self.bytes_read+=len(block)
                    state['offset']=f.tell()
                    if state['offset']==state['target']:
                        unchanged=known==(state['target'],state['digest'].hexdigest())
                    else:
                        unchanged=False if remaining else None
                if inline_prefix:
                    self.prefix_priority -= self.bytes_read-before
                else:
                    budget-=self.bytes_read-before
                if unchanged is None:
                    self.queue_prefix(path,home,tid,self.bytes_read-before)
                    return None
                if not unchanged:
                    self.db.execute('delete from events where path=?',(path,))
                    self.rebuild_required.add((str(home),tid));state.update(offset=0,digest=hashlib.sha256())
                    state.pop('reader',None)
                resume=state.pop('resume_reader',None)
                if unchanged and resume is not None:
                    # The pass also verified the unfinished line's bytes. Keep
                    # its bounded parser, including progress beyond the last LF.
                    state.update(offset=resume.committed,digest=resume.committed_digest.copy(),reader=resume)
                state['verified']=True
                if budget<=0:
                    self.queue_prefix(path,home,tid,self.bytes_read-before)
                    return None
            offset=state['offset'];digest=state['digest']
            f.seek(offset)
            # Bound work per file so a large active session cannot block other files.
            start_offset = offset
            before_read=state.get('read_bytes',0)
            for pos, line in _event_lines(f, digest,budget,state):
                try:
                    meta=json.loads(line)
                    if meta.get('type') == 'session_meta':
                        payload = meta['payload']
                        tid = payload.get('id') or tid
                        lineage=session_lineage(payload,tid)
                        if tid and lineage:
                            self.lineage[(str(home),tid)]=lineage
                        if tid and (str(home), tid) not in self.metadata:
                            source = payload.get('source')
                            self.metadata[(str(home), tid)] = {'id': tid, 'cwd': payload.get('cwd', ''),
                                'archived': 'archived_sessions' in p.parts,
                                'thread_source': 'subagent' if isinstance(source, dict) and 'subagent' in source else 'user' if source in ('cli', 'vscode', 'exec') else 'unknown'}
                            self.metadata[(str(home), tid)].update(lineage)
                            names = self.names.get(str(home))
                            if names:
                                self.metadata[(str(home), tid)].update(names.resolve(self.metadata[(str(home), tid)]))
                            self.db.execute('insert or replace into metadata values(?,?,?)',
                                (str(home),tid,json.dumps(self.metadata[(str(home),tid)],ensure_ascii=False)))
                    clean = sanitized(meta) if tid else None
                    if clean:
                        encoded=json.dumps(clean,ensure_ascii=False)
                        # Source event identity survives an archive-directory move;
                        # persisted path/pos reconstruct it without duplicating it.
                        clean['event_id']=f'{p.name}:{pos}'
                        self.new_events[(str(home),tid)].append(((stamp(clean['timestamp']),path,pos),clean))
                        self.db.execute('insert or replace into events values(?,?,?,?,?,?)',
                            (path, pos, str(home), tid, stamp(clean['timestamp']), pack_event(encoded)))
                        self.observe_quota(home,clean['payload'].get('rate_limits'),stamp(clean['timestamp']))
                except (ValueError, TypeError, AttributeError):
                    self.errors.append(f'일부 기록 형식 확인 필요: {str(tid or p.name)[:8]}')
                    self.collection_fault['read_failed']=True
            offset = f.tell()
            consumed = state.get('read_bytes',0)-before_read
            self.bytes_read += consumed
            digest=state['digest']
            self.source_hashes[path]=(offset,digest.hexdigest())
            state['offset']=offset
        # Keep the recheck obligation across a crash after tentative rows are
        # committed. Only a clean pass may persist the real source signature.
        mtime=-1 if state.get('recheck') else st.st_mtime_ns
        self.db.execute('insert or replace into files values(?,?,?,?,?,?,?)',
                        (path, str(home), tid, offset, st.st_size, mtime, str(st.st_ino)))
        reader=state.get('reader')
        read_offset=reader.read_offset if reader is not None else offset
        if state.get('recheck') and (read_offset>=state['scan_target'] or consumed<budget):
            # Publish tentative progress, then validate it without chasing a
            # moving EOF. A rewrite behind this pass is caught by the next one;
            # the queued obligation keeps usage_complete false until then.
            partial=reader if reader is not None and read_offset>offset else None
            self.source_work[path]=dict(signature=signature,offset=0,target=read_offset,
                digest=hashlib.sha256(),verified=not read_offset,
                known=(read_offset,(reader.digest if partial else digest).hexdigest()),
                scan_target=st.st_size,**({'resume_reader':partial} if partial else {}))
            self.queue.append((path,home,tid))
        elif offset < st.st_size and consumed >= budget:
            self.queue.append((path, home, tid))
        else:
            self.source_work.pop(path,None)
            if offset<st.st_size:
                # A dormant tail needs only its signature. A source change
                # requires prefix validation anyway; release its parser now.
                self.partial_work[path]={'signature':signature}
                while len(self.partial_work)>MAX_SOURCE_WORK:self.partial_work.popitem(last=False)
        return (str(home), tid) if tid else None

    def queue_prefix(self,path,home,tid,consumed):
        # Finish ordinary large prefixes before a growing file changes again.
        # Priority is limited per poll; remaining files still share the scan's
        # existing deadline. Changed prefixes still require a clean pass.
        args=(path,home,tid)
        if 0 < consumed <= self.prefix_priority:
            self.prefix_priority -= consumed
            self.queue.appendleft(args)
        else:
            self.prefix_priority = 0
            self.queue.append(args)

    def rebuild(self, key, now):
        job=self.rebuild_jobs.get(key)
        if job is None:
            job=self.rebuild_jobs[key]=self.rebuild_steps(key,now)
        while True:
            try:next(job)
            except StopIteration:
                self.rebuild_jobs.pop(key,None)
                return
            if self.rebuild_deadline is not None and time.monotonic()>=self.rebuild_deadline:return

    def rebuild_steps(self, key, now):
        additions=sorted(self.new_events.pop(key,[]),key=lambda item:item[0])
        previous=self.monitor.sessions.get(key)
        append=bool(previous and key in self.loaded and key not in self.rebuild_required
                    and not hasattr(previous,'_replay_body')
                    and all(order>self.event_tails.get(key,(float('inf'),'',0)) for order,_ in additions)
                    and (previous.modern or not any(e['type']=='token_usage_record' for _,e in additions)))
        if append:
            events=[e for _,e in additions]
            s=previous
            if additions:self.event_tails[key]=additions[-1][0]
        else:
            s=Session(key[1],key[0])
            upper=self.event_store.upper()
            def stored_events():
                cursor=self.db.execute('select data,ts,path,pos from '+self.event_store.replay_table+' where home=? and tid=? '
                    'and rowid<=? order by ts,path,pos',(*key,upper))
                last_path=None
                last_name=''
                try:
                    for row in cursor:
                        event=json.loads(unpack_event(row[0]))
                        if 'event_id' not in event:
                            if row[2]!=last_path:
                                last_path=row[2]
                                last_name=Path(last_path).name
                            event['event_id']=f'{last_name}:{row[3]}'
                        yield tuple(row[1:]),event
                finally:cursor.close()
        replay_source=self.event_store.owner_replay_token(*key)
        self.rebuild_required.discard(key)
        meta = self.metadata.get(key, {})
        s.metadata_seen = bool(meta)
        s.title = meta.get('display_title') or meta.get('name') or meta.get('title') or f'작업 {key[1][:8]}'
        s.cwd = (meta.get('cwd') or '').removeprefix('\\\\?\\')
        if not append:s.model = meta.get('model') or ''
        s.provider = meta.get('model_provider') or ''
        for field in LINEAGE_FIELDS:
            if field in meta:setattr(s,field,meta[field])
        # Inspect in bounded batches before replay so legacy mirrors cannot
        # change coverage accounting before a later modern event is discovered.
        contexts = defaultdict(list)
        preflight=enumerate(((None,e) for e in events) if append else stored_events(),1)
        for number,(_,e) in preflight:
            s.modern = s.modern or (e['type']=='token_usage_record' and e['payload'].get('thread_id',s.id)==s.id)
            if e['type']=='turn_context' and e['payload'].get('turn_id'):
                contexts[e['payload']['turn_id']].append((stamp(e['timestamp']),e['payload']))
            if number%REBUILD_BATCH==0 or self.rebuild_deadline is not None and time.monotonic()>=self.rebuild_deadline:yield
        previous_count=len(s.requests)
        states = dict(self.turn_states.get(key,{})) if append else {}
        tail=self.event_tails.get(key,(0,'',0)) if append else (0,'',0)
        replay=enumerate(((None,e) for e in events) if append else stored_events(),1)
        for number,(order,e) in replay:
            s.consume(e, now)
            self.observe_quota(key[0],e['payload'].get('rate_limits'),stamp(e.get('timestamp')))
            if order is not None:tail=order
            p=e['payload']
            if e['type']=='event_msg' and p.get('turn_id'):
                typ=p.get('type')
                if typ in ('task_started','task_complete','task_completed','turn_aborted'):
                    states[p['turn_id']]={'task_started':'진행','turn_aborted':'중단'}.get(typ,'완료')
            if number%REBUILD_BATCH==0 or self.rebuild_deadline is not None and time.monotonic()>=self.rebuild_deadline:yield
        for number,request in enumerate(islice(s.requests,previous_count if append else 0,None),1):
            candidates = contexts.get(request.turn, [])
            position = bisect_right([c[0] for c in candidates], request.ts)-1
            if position >= 0 and (request.effort == '미확인' or not request.model):
                context = candidates[position][1]
                request.effort = context.get('effort') or request.effort
                request.model = context.get('model') or request.model
                request.configured_model = context.get('model') or request.configured_model
            if number%REBUILD_BATCH==0 or self.rebuild_deadline is not None and time.monotonic()>=self.rebuild_deadline:yield
        self.event_tails[key]=tail
        if not append and key in self.rebuild_required:return
        self.turn_states[key] = states
        previous = self.monitor.sessions.get(key)
        if previous:
            s.transports = previous.transports
        s._replay_source=replay_source
        if self.replay is not None:self.replay.rebuilt(key,previous)
        self.monitor.sessions[key] = s
        self.loaded.add(key)

    def catalog_signature(self):
        # Three scalar reads per home on an idle-source poll. A new thread or
        # newly assigned rollout path wakes discovery; ordinary token updates
        # do not repeat the directory scan. The ten-second reconciliation stays.
        result=[]
        for home in self.homes:
            try:
                with readonly(home/'state_5.sqlite') as db:
                    count=db.execute('SELECT count(*) FROM threads').fetchone()[0]
                    latest=db.execute('SELECT max(rowid) FROM threads').fetchone()[0]
                    missing=db.execute("SELECT count(*) FROM threads WHERE rollout_path IS NULL OR rollout_path='' ").fetchone()[0]
                    result.append((str(home),count,latest,missing))
            except (OSError,sqlite3.Error):
                result.append((str(home),None))
        return tuple(result)

    def poll(self, now=None):
        if self.read_only:raise ValueError('읽기 전용 색인에서는 원본 수집을 실행할 수 없습니다')
        now = now or time.time()
        self.errors = []
        self.collection_fault = {}
        if self.replay is not None:
            self.model_evidence.poll()
            self.replay.begin(now)
        catalog=self.catalog_signature() if not self.queue else self.source_catalog
        if not self.queue and (not self.files or now - self.last_discovery >= 10 or catalog!=self.source_catalog):
            self.discover(now)
            self.source_catalog=catalog
            if not self.quota_seeded:
                self.seed_quota()
                self.quota_seeded=True
        elif not self.queue and not self.cached_restore:
            self.queue = deque(self.files)
            self.done_files = 0
        changed = set()
        self.prefix_priority = PREFIX_PRIORITY_BUDGET
        deadline = time.monotonic() + .4
        self.rebuild_deadline=deadline
        while self.pending_rebuilds and time.monotonic()<deadline:
            key=self.pending_rebuilds[0]
            self.rebuild(key,now)
            if key in self.rebuild_jobs:break
            self.pending_rebuilds.popleft()
            if self.new_events.get(key) or key in self.rebuild_required:self.pending_rebuilds.append(key)
        if not self.initial and self.cached_restore:
            # Periodic discovery still runs, but cannot consume every startup
            # recovery budget. Reserve half for retained owners, half for scans.
            self.restore_cached(now,min(deadline,time.monotonic()+.2))
        while self.queue and time.monotonic() < deadline:
            args = self.queue.popleft()
            try:
                key = self.scan_file(*args)
                if key:
                    if self.replay is not None:self.replay.pending.discard(key)
                    changed.add(key)
            except (OSError, sqlite3.Error) as exc:
                self.source_work.pop(args[0],None)
                self.errors.append(f'기록 읽기: {Path(args[0]).name}: {exc}')
                self.collection_fault['read_failed']=True
            self.done_files += 1
        try:self.db.commit()
        except sqlite3.Error:
            self.collection_fault['save_failed']=True
            raise
        for key in changed:
            if key not in self.pending_rebuilds:self.pending_rebuilds.append(key)
        while self.pending_rebuilds and time.monotonic()<deadline:
            key=self.pending_rebuilds[0]
            self.rebuild(key,now)
            if key in self.rebuild_jobs:break
            self.pending_rebuilds.popleft()
            if self.new_events.get(key) or key in self.rebuild_required:self.pending_rebuilds.append(key)
        if not self.queue:
            self.initial = False
            self.restore_cached(now,deadline)
        if not self.rebuild_jobs and time.monotonic()<deadline:
            self.event_store.migrate(deadline)
        usage_errors=list(self.errors)
        usage_complete=not self.queue and not self.cached_restore and not self.pending_rebuilds and not self.rebuild_jobs and not usage_errors
        if self.replay is not None and self.replay.restore_job is not None:usage_complete=False
        self.rebuild_deadline=None
        return self.snapshot(now,changed,usage_complete,usage_errors)

    def restore_cached(self,now,deadline):
        while self.cached_restore and time.monotonic()<deadline:
            key=self.cached_restore[0]
            if self.replay is not None and key in self.replay.pending:break
            if self.pending_rebuilds:break
            if key not in self.loaded:self.rebuild(key,now)
            if key in self.rebuild_jobs:break
            self.cached_restore.popleft()

    def ready_sessions(self,usage_complete):
        # A queued source can still establish new ancestry. Once every source
        # is checked, unrelated retained owners need not hold ready sessions.
        if self.queue or self.errors or self.model_evidence.catching_up:return set()
        if usage_complete:
            return {key for key,s in self.monitor.sessions.items() if not s.pending}
        pending=({key for key in self.cached_restore if key not in self.loaded}
                 |set(self.pending_rebuilds)|set(self.rebuild_jobs)|set(self.rebuild_required)
                 |{key for key,events in self.new_events.items() if events})
        blocked=set(pending);unknown_homes=set()
        for key in pending:
            seen=set()
            while key not in seen:
                seen.add(key)
                meta=self.metadata.get(key)
                session=self.monitor.sessions.get(key)
                lineage=self.lineage.get(key)
                if meta is None and session is None and lineage is None:
                    unknown_homes.add(key[0]);break
                parent=(lineage or {}).get('parent_thread_id') or (meta or {}).get('parent_thread_id')
                if not parent and session is not None:parent=session.parent_thread_id
                if not parent:
                    if (meta or {}).get('thread_source')=='subagent':unknown_homes.add(key[0])
                    break
                key=(key[0],parent);blocked.add(key)
        return {key for key,s in self.monitor.sessions.items()
                if key in self.loaded and key not in blocked and key[0] not in unknown_homes and not s.pending}

    def snapshot(self,now,changed,usage_complete,usage_errors):
        self.monitor.trim_unassigned(now)
        for home in self.homes:
            try:
                self.monitor.read_logs(home, now)
            except (OSError, sqlite3.Error) as exc:
                self.errors.append(f'{home.name} / 연결 기록: {exc}')
                self.collection_fault['read_failed']=True
        if self.replay is None:self.model_evidence.poll()
        if self.model_evidence.catching_up:usage_complete=False
        if self.model_evidence.error:
            self.errors.append(self.model_evidence.error)
        ready=self.ready_sessions(usage_complete)
        views = []
        # Resolve each home once per publication, not once per retained session.
        # The next publication resolves again so changed links are still observed.
        evidence_homes = {home:home_key(home) for home in dict.fromkeys(home for home,_ in self.monitor.sessions)}
        for key, s in self.monitor.sessions.items():
            meta = self.metadata.get(key, {})
            if hasattr(s,'_replay_body'):
                views.append(self.replay.view(key,s,key in ready,now))
                continue
            s.metadata_seen = bool(meta)
            s.trim(now)
            if s.excluded_title:
                continue
            if not s.requests and not s.reported_usage and not s.transports and not s.request_state:
                continue
            view = dict(s.view(now,use_cache=True))
            view['history'] = self.model_evidence.enrich_owned(evidence_homes[s.home],view['history'],s,
                **({'capture_proof':True} if self.replay is not None else {}))
            if not meta.get('project') or not meta.get('project_name'):
                names=self.names.get(str(s.home))
                if names:
                    # Log-only sessions and retained metadata for removed app
                    # tasks still use the same groups as current app tasks.
                    # This changes presentation only, never event history.
                    unresolved={**meta,'id':s.id,'cwd':meta.get('cwd') or view.get('cwd','')}
                    meta={**meta,**names.resolve(unresolved)}
            view['title'] = meta.get('display_title') or meta.get('name') or meta.get('title') or s.title
            view.update({k: meta[k] for k in ('project', 'project_id', 'project_name', 'project_source', *LINEAGE_FIELDS) if k in meta})
            view['archived'] = bool(meta.get('archived'))
            view['source'] = meta.get('thread_source') or 'unknown'
            view['cwd'] = (meta.get('cwd') or view['cwd']).removeprefix('\\\\?\\')
            view['turn_states'] = self.turn_states.get(key, {})
            view['collection_complete'] = key in ready
            old_history=self.history_signatures.get(key)
            if old_history is not None and old_history[0] is view['history']:
                history_signature=old_history[1]
            else:
                history_signature=tuple(tuple(r.get(field) for field in ROW_SIGNATURE_FIELDS) for r in view['history'])
                self.history_signatures[key]=(view['history'],history_signature)
            request_values=tuple(view.get(field) for field in
                ('request_state','turn_records','turn_states','coverage_gaps','unclassified'))
            prior=self.request_revisions.get(key)
            request_revision=(prior[1] if prior and prior[0] is not None and all(a is b or a==b for a,b in zip(prior[0],request_values))
                              else (prior[1]+1 if prior else 1))
            self.request_revisions[key]=(request_values,request_revision)
            view['request_revision']=request_revision
            signature=(history_signature,view['title'],view['cwd'],view['archived'],view['source'],
                       view.get('project'),view.get('project_name'),
                       tuple(view.get(field) for field in LINEAGE_FIELDS),
                       request_revision)
            if self.view_signatures.get(key)!=signature:
                self.view_signatures[key]=signature
                self.session_revisions[key]=self.session_revisions.get(key,0)+1
            view['usage_revision']=self.session_revisions[key]
            views.append(view)
        self.version += bool(changed)
        if self.pending_rebuilds:usage_complete=False
        if usage_complete:self.last_usage_success=now
        if self.archive is not None:views.extend(self.archive.views(now,{str(h) for h in self.homes}))
        activity_revision = (self.model_evidence.generation,self.model_evidence.revision)
        if activity_revision != self.activity_publication:
            self.activity_rows = list(self.model_evidence.changed_activity.values())
            self.activity_tokens = {(row['home'],row['attempt']):(self.model_evidence.generation,row['seq'])
                                   for row in self.activity_rows}
            self.activity_publication = activity_revision
        snapshot = {'ts': now, 'sessions': sorted(views, key=lambda s: s['activity'], reverse=True),
                'request_activity': list(self.model_evidence.changed_activity.values()),
                'activity_stream':True,
                'activity_generation':self.model_evidence.generation,
                'activity_source':repr((self.model_evidence.identity,self.model_evidence.generation,self.model_evidence.source_marker)),
                'activity_source_limit':self.model_evidence.highwater,
                'activity_source_offset':self.model_evidence.offset,
                'usage_collection_complete':usage_complete,'last_usage_collection_success':self.last_usage_success,
                'usage_errors':usage_errors,
                'collection_fault':dict(self.collection_fault),
                'observation_read_failed':bool(self.model_evidence.error),
                'homes': [str(h) for h in self.homes], 'errors': list(dict.fromkeys(self.errors)),
                'unassigned': list(self.monitor.unassigned),
                'unassigned_dropped': self.monitor.unassigned_dropped,
                'excluded_title_sessions': sum(s.excluded_title for s in self.monitor.sessions.values() if not hasattr(s,'_replay_body')),
                'quota_by_home':dict(self.quota_by_home),
                'index': {'loading': bool(self.queue or self.cached_restore or self.pending_rebuilds or self.rebuild_jobs or self.model_evidence.catching_up), 'done': min(self.done_files, len(self.files)),
                          'usage_complete':usage_complete,'last_usage_success':self.last_usage_success,
                          'files': len(self.files), 'bytes_read': self.bytes_read, 'version': self.version,
                          'path': str(self.path)}}
        self.track_publication(snapshot)
        return snapshot

    def track_publication(self, snapshot):
        from .usage_collection import CollectionChanges, LiveCollection, publication_fingerprint
        from .collection_replay import EncodedSession
        # Session.view owns its cached nested values. It replaces that cache on
        # changes and window expiry; EvidenceReader likewise replaces joined rows.
        # Retain references, not bare ids, so object-id reuse cannot hide a change.
        bases = {key:getattr(session,'_cached_view',None)
                 for key,session in self.monitor.sessions.items()}
        bases.update({(session.home,session.id):getattr(session,'_cached_view',None)
                      for _,session,_ in self.archive.groups})
        current = {}
        tokens = {}
        for view in snapshot['sessions']:
            key = (view['home'],view['id'])
            if isinstance(view,EncodedSession):
                tokens[key]=view.digest
                continue
            base = bases.get(key)
            if base is None:continue
            history = view['history']
            overrides = {field:value for field,value in view.items()
                         if field != 'history' and field not in LiveCollection.volatile
                         and (field not in base or value is not base[field])}
            previous = self.publication_rows.get(key)
            unchanged=(previous is not None and previous[4].keys()==overrides.keys() and
                all(value is previous[4][field] or isinstance(value,(str,int,float,bool,type(None))) and
                    value==previous[4][field] for field,value in overrides.items()))
            fingerprint=previous[2] if unchanged else publication_fingerprint(overrides)
            if (previous is not None and previous[0] is base and
                    previous[1] is history and previous[2] == fingerprint):
                token = previous[3]
            else:
                self.publication_revision += 1
                token = self.publication_revision
            current[key] = (base,history,fingerprint,token,overrides)
            tokens[key] = token
        self.publication_rows = current
        self.collection_changes = CollectionChanges(self.publication_owner,tokens,self.activity_tokens,
                                                    activity_revision=self.activity_publication)

    def close(self):
        if self.replay is not None:self.replay.close();self.replay=None
        for job in self.rebuild_jobs.values():job.close()
        self.rebuild_jobs.clear();self.pending_rebuilds.clear()
        self.cached_restore.clear()
        self.publication_rows.clear()
        self.collection_changes = None
        self.activity_rows = []
        self.activity_tokens = {}
        self.source_work.clear()
        self.partial_work.clear()
        if self.archive is not None:self.archive.clear()
        self.model_evidence.close()
        self.db.close()
