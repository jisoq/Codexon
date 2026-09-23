"""Incremental local index. Source databases and rollouts are always read-only."""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
from itertools import islice
from bisect import bisect_right
from collections import defaultdict
from collections import deque
from pathlib import Path

from .core import LINEAGE_FIELDS, TRANSPORT_FIELDS, Monitor, Session, readonly, stamp, session_lineage
from .codex_names import CodexNames, task_select
from .quota import clean_limits
from .model_evidence import EvidenceReader, default_path as evidence_path, FIELDS as MODEL_FIELDS


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
    if kind == 'turn_context':
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
        elif typ in ('task_started', 'task_complete', 'task_completed', 'turn_aborted'):
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
        self.homes = [Path(h).resolve() for h in homes]
        self.path = Path(path) if path else Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'CacheMonitor' / 'usage-index.sqlite'
        if any(self.path.resolve().is_relative_to(h) for h in self.homes):
            raise ValueError('앱 색인은 Codex 원본 폴더 밖에 저장해야 합니다')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        tables = {r[0] for r in self.db.execute("select name from sqlite_master where type='table'")}
        if tables - {'files','events','metadata'}:
            self.db.close()
            raise ValueError('다른 데이터베이스를 앱 색인으로 사용할 수 없습니다')
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, home TEXT, tid TEXT,
                offset INTEGER, size INTEGER, mtime INTEGER, inode TEXT);
            CREATE TABLE IF NOT EXISTS events(path TEXT, pos INTEGER, home TEXT, tid TEXT,
                ts REAL, data TEXT, PRIMARY KEY(path,pos));
            CREATE INDEX IF NOT EXISTS events_session ON events(home,tid,ts);
            CREATE TABLE IF NOT EXISTS metadata(home TEXT, tid TEXT, data TEXT, PRIMARY KEY(home,tid));
        ''')
        # Re-read source incrementally once to recover settings snapshots omitted
        # by the old sanitizer. Keep existing events while this backfill proceeds.
        self.tier_backfill=self.db.execute('pragma user_version').fetchone()[0]<2
        if self.tier_backfill:
            self.db.execute('update files set offset=0')
            self.db.execute('pragma user_version=2')
            self.db.commit()
        self.monitor = Monitor(self.homes)
        self.metadata = {(r[0],r[1]):json.loads(r[2]) for r in self.db.execute('select home,tid,data from metadata') if r[0] in {str(h) for h in self.homes}}
        self.files = []
        self.queue = deque()
        self.last_discovery = 0
        self.initial = True
        self.bytes_read = 0
        self.errors = []
        self.loaded = set()
        self.version = 0
        self.done_files = 0
        self.turn_states = {}
        self.event_tails = {}
        self.new_events = defaultdict(list)
        self.rebuild_required = set()
        self.source_hashes = {}
        self.view_signatures = {}
        self.history_signatures = {}
        self.session_revisions = {}
        self.model_evidence = EvidenceReader(model_evidence_path if model_evidence_path is not None else
                                            self.path.with_name('model-evidence.sqlite') if path is not None else evidence_path())
        self.quota_by_home = {}
        self.quota_seeded = False
        self.last_usage_success = None
        self.names = {}
        self.lineage = {key:{field:meta[field] for field in LINEAGE_FIELDS if field in meta}
                        for key,meta in self.metadata.items() if meta.get('parent_thread_id')}

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
                            paths[str(Path(row['rollout_path']).resolve()).removeprefix('\\\\?\\')] = (home, row['id'])
            except (OSError, sqlite3.Error) as exc:
                self.errors.append(f'{home.name} / 세션 목록: {exc}')
            for folder in ('sessions', 'archived_sessions'):
                for path in (home / folder).rglob('*.jsonl'):
                    resolved = str(path.resolve()).removeprefix('\\\\?\\')
                    if resolved not in paths:
                        # A filename is not proof of ownership; session_meta supplies the ID.
                        paths[resolved] = (home, None)
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

    def scan_file(self, path, home, tid):
        p = Path(path)
        try:
            st = p.stat()
        except FileNotFoundError:
            # Historical paths can move to archived_sessions; discovery covers that location.
            return None
        old = self.db.execute('select tid,offset,size,mtime,inode from files where path=?', (path,)).fetchone()
        if old and old[1] == st.st_size and old[2] == st.st_size and old[3] == st.st_mtime_ns:
            return (str(home), old[0]) if self.initial else None
        offset = old[1] if old else 0
        if old:
            tid = old[0]
            if str(st.st_ino) != old[4] or st.st_size < offset or (st.st_size == offset and st.st_mtime_ns != old[3]):
                self.db.execute('delete from events where path=?', (path,))
                self.rebuild_required.add((str(home),tid))
                offset = 0
        with p.open('rb') as f:
            digest=hashlib.sha256()
            if offset:
                # An increased length alone does not prove append-only: editors can
                # correct old bytes and append in the same write. Verify the consumed
                # prefix before reusing parsed state. Only digests stay in memory.
                remaining=offset
                while remaining:
                    block=f.read(min(1024*1024,remaining))
                    if not block:break
                    digest.update(block);remaining-=len(block);self.bytes_read+=len(block)
                if remaining or self.source_hashes.get(path)!=(offset,digest.hexdigest()):
                    self.db.execute('delete from events where path=?',(path,))
                    self.rebuild_required.add((str(home),tid));offset=0;digest=hashlib.sha256()
                f.seek(0)
            if offset == 0:
                head = f.readline()
                try:
                    meta = json.loads(head)
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
                except (ValueError, TypeError, KeyError):
                    pass
            if not tid:
                return None
            f.seek(offset)
            consumed = 0
            # Bound work per file so a large active session cannot block other files.
            while consumed < 16 * 1024 * 1024:
                pos = f.tell()
                line = f.readline()
                if not line or not line.endswith(b'\n'):
                    f.seek(pos)
                    break
                consumed += len(line)
                digest.update(line)
                if any(t in line[:250] for t in (b'"turn_context"', b'"token_usage_record"', b'"event_msg"')):
                    try:
                        clean = sanitized(json.loads(line))
                        if clean:
                            # Source event identity survives an archive-directory move;
                            # equal times/token values never collapse distinct events.
                            clean['event_id']=f'{p.name}:{pos}'
                            self.new_events[(str(home),tid)].append(((stamp(clean['timestamp']),path,pos),clean))
                            self.db.execute('insert or replace into events values(?,?,?,?,?,?)',
                                (path, pos, str(home), tid, stamp(clean['timestamp']), json.dumps(clean, ensure_ascii=False)))
                            self.observe_quota(home,clean['payload'].get('rate_limits'),stamp(clean['timestamp']))
                    except (ValueError, TypeError, AttributeError):
                        self.errors.append(f'일부 기록 형식 확인 필요: {tid[:8]}')
            offset = f.tell()
            self.bytes_read += consumed
            self.source_hashes[path]=(offset,digest.hexdigest())
        self.db.execute('insert or replace into files values(?,?,?,?,?,?,?)',
                        (path, str(home), tid, offset, st.st_size, st.st_mtime_ns, str(st.st_ino)))
        if offset < st.st_size and consumed >= 16 * 1024 * 1024:
            self.queue.append((path, home, tid))
        return str(home), tid

    def rebuild(self, key, now):
        additions=sorted(self.new_events.pop(key,[]),key=lambda item:item[0])
        previous=self.monitor.sessions.get(key)
        append=bool(previous and key in self.loaded and key not in self.rebuild_required
                    and all(order>self.event_tails.get(key,(float('inf'),'',0)) for order,_ in additions)
                    and (previous.modern or not any(e['type']=='token_usage_record' for _,e in additions)))
        if append:
            events=[e for _,e in additions]
            s=previous
            if additions:self.event_tails[key]=additions[-1][0]
        else:
            stored=self.db.execute('select data,ts,path,pos from events where home=? and tid=? order by ts,path,pos',key).fetchall()
            events=[json.loads(row[0]) for row in stored]
            for event,row in zip(events,stored):
                event.setdefault('event_id',f'{Path(row[2]).name}:{row[3]}')
            self.event_tails[key]=tuple(stored[-1][1:]) if stored else (0,'',0)
            s=Session(key[1],key[0])
        self.rebuild_required.discard(key)
        meta = self.metadata.get(key, {})
        s.metadata_seen = bool(meta)
        s.title = meta.get('display_title') or meta.get('name') or meta.get('title') or f'작업 {key[1][:8]}'
        s.cwd = (meta.get('cwd') or '').removeprefix('\\\\?\\')
        if not append:s.model = meta.get('model') or ''
        s.provider = meta.get('model_provider') or ''
        for field in LINEAGE_FIELDS:
            if field in meta:setattr(s,field,meta[field])
        # Modern and legacy events may coexist: never count the legacy mirror too.
        s.modern = s.modern or any(e['type'] == 'token_usage_record' and e['payload'].get('thread_id', s.id) == s.id for e in events)
        previous_count=len(s.requests)
        for e in events:
            s.consume(e, now)
        contexts = defaultdict(list)
        for e in events:
            if e['type'] == 'turn_context' and e['payload'].get('turn_id'):
                contexts[e['payload']['turn_id']].append((stamp(e['timestamp']), e['payload']))
        for request in islice(s.requests,previous_count if append else 0,None):
            candidates = contexts.get(request.turn, [])
            position = bisect_right([c[0] for c in candidates], request.ts)-1
            if position >= 0 and (request.effort == '미확인' or not request.model):
                context = candidates[position][1]
                request.effort = context.get('effort') or request.effort
                request.model = context.get('model') or request.model
                request.configured_model = context.get('model') or request.configured_model
        states = dict(self.turn_states.get(key,{})) if append else {}
        for e in events:
            p = e['payload']
            if e['type'] == 'event_msg' and p.get('turn_id'):
                typ = p.get('type')
                if typ in ('task_started', 'task_complete', 'task_completed', 'turn_aborted'):
                    states[p['turn_id']] = {'task_started': '진행', 'turn_aborted': '중단'}.get(typ, '완료')
        self.turn_states[key] = states
        previous = self.monitor.sessions.get(key)
        if previous:
            s.transports = previous.transports
        self.monitor.sessions[key] = s
        self.loaded.add(key)

    def poll(self, now=None):
        now = now or time.time()
        self.errors = []
        if not self.queue and (not self.files or now - self.last_discovery >= 10):
            self.discover(now)
            if not self.quota_seeded:
                self.seed_quota()
                self.quota_seeded=True
        elif not self.queue:
            self.queue = deque(self.files)
            self.done_files = 0
        changed = set()
        deadline = time.monotonic() + .4
        while self.queue and time.monotonic() < deadline:
            args = self.queue.popleft()
            try:
                key = self.scan_file(*args)
                if key:
                    changed.add(key)
            except (OSError, sqlite3.Error) as exc:
                self.errors.append(f'기록 읽기: {Path(args[0]).name}: {exc}')
            self.done_files += 1
        self.db.commit()
        for key in changed:
            self.rebuild(key, now)
        if not self.queue:
            self.initial = False
        usage_errors=list(self.errors)
        usage_complete=not self.queue and not usage_errors
        if usage_complete:self.last_usage_success=now
        for home in self.homes:
            try:
                self.monitor.read_logs(home, now)
            except (OSError, sqlite3.Error) as exc:
                self.errors.append(f'{home.name} / 연결 기록: {exc}')
        self.model_evidence.poll()
        if self.model_evidence.error:
            self.errors.append(self.model_evidence.error)
        views = []
        for key, s in self.monitor.sessions.items():
            s.trim(now)
            if s.excluded_title:
                continue
            if not s.requests and not s.reported_usage and not s.transports and not s.request_state:
                continue
            view = s.view(now,use_cache=True)
            view['history'] = self.model_evidence.enrich(s.home, view['history'],s.id)
            meta = self.metadata.get(key, {})
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
            view['collection_complete'] = usage_complete and not s.pending
            old_history=self.history_signatures.get(key)
            if old_history is not None and old_history[0] is view['history']:
                history_signature=old_history[1]
            else:
                fields=('key','ts','turn','model','effort','input','cached','written','output','reasoning','total','service_tier') + TRANSPORT_FIELDS + MODEL_FIELDS
                history_signature=tuple(tuple(r.get(field) for field in fields) for r in view['history'])
                self.history_signatures[key]=(view['history'],history_signature)
            signature=(history_signature,view['title'],view['cwd'],view['archived'],view['source'],
                       view.get('project'),view.get('project_name'),
                       tuple(view.get(field) for field in LINEAGE_FIELDS),
                       tuple(view.get('request_state',{}).items()),
                       tuple(sorted(view['turn_states'].items())),tuple(sorted((view.get('unclassified') or {}).items())),
                       tuple((g['start'],g['end'],g['tokens']) for g in view.get('coverage_gaps',[])))
            if self.view_signatures.get(key)!=signature:
                self.view_signatures[key]=signature
                self.session_revisions[key]=self.session_revisions.get(key,0)+1
            view['usage_revision']=self.session_revisions[key]
            views.append(view)
        self.version += bool(changed)
        return {'ts': now, 'sessions': sorted(views, key=lambda s: s['activity'], reverse=True),
                'request_activity': list(self.model_evidence.activity_records.values()),
                'usage_collection_complete':usage_complete,'last_usage_collection_success':self.last_usage_success,
                'usage_errors':usage_errors,
                'homes': [str(h) for h in self.homes], 'errors': list(dict.fromkeys(self.errors)),
                'unassigned': list(self.monitor.unassigned),
                'excluded_title_sessions': sum(s.excluded_title for s in self.monitor.sessions.values()),
                'quota':self.quota_by_home.get(str(self.homes[0])) if self.homes else None,
                'index': {'loading': bool(self.queue), 'done': min(self.done_files, len(self.files)),
                          'usage_complete':usage_complete,'last_usage_success':self.last_usage_success,
                          'files': len(self.files), 'bytes_read': self.bytes_read, 'version': self.version,
                          'path': str(self.path)}}

    def close(self):
        self.model_evidence.close()
        self.db.close()
