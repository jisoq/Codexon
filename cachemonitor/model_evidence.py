"""Sanitized wire evidence. Never infer a wire model from a turn setting."""
from __future__ import annotations

import os
import re
import sqlite3
import math
from collections import Counter
from pathlib import Path

FIELDS = ('requested_model', 'response_model', 'model_match', 'model_evidence', 'response_status',
          'requested_service_tier','response_service_tier','response_tier_confirmed','mode_evidence','service_tier_source',
          'model_alert_confirmed','model_observation_ts')
TIMING_FIELDS = ('observation_missing','request_observed_at','generation_observed_at','completed_observed_at',
                 'generation_latency_ms','completion_latency_ms')
FIELDS += TIMING_FIELDS + ('cache_policy','cache_policy_conflict')
FIELDS += ('model_conflict','mode_conflict','timing_valid','analysis_model','model_source','configured_model')
FIELDS += ('request_mode_source','request_mode_action','configured_service_tier','wire_service_tier_present','wire_observed')
UNKNOWN = '확인 불가'


def default_path():
    # MSIX can redirect LocalAppData differently for Codex children and ordinary
    # desktop launches. The user-profile directory is shared by both contexts.
    return Path.home() / '.cachemonitor' / 'model-observer' / 'model-evidence.sqlite'


def identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,256}', value) else ''


def home_key(home):
    return os.path.normcase(str(Path(home).resolve()))


def compare(requested, responded, conflict=False):
    if conflict or not requested or not responded:
        return UNKNOWN
    return '일치' if requested == responded else '불일치'


def summary(rows):
    rows = list(rows)
    counts = Counter(r.get('model_match', UNKNOWN) for r in rows)
    known = counts['일치'] + counts['불일치']
    return (f"모델 검증 {known:,}/{len(rows):,}건 · 일치 {counts['일치']:,} · "
            f"불일치 {counts['불일치']:,} · 확인 불가 {len(rows)-known:,}")


class EvidenceStore:
    def __init__(self, path):
        path = Path(path)
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=.2)
        tables = {r[0] for r in self.db.execute("select name from sqlite_master where type='table'")}
        if tables - {'model_observations', 'observation_gaps', 'sqlite_sequence'}:
            self.db.close()
            raise ValueError('모델 관측 전용 데이터베이스가 아닙니다')
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS model_observations(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, home TEXT NOT NULL,
                attempt TEXT NOT NULL, ts REAL NOT NULL, transport TEXT NOT NULL,
                response_id TEXT NOT NULL, requested_model TEXT NOT NULL,
                response_model TEXT NOT NULL, status TEXT NOT NULL, conflict INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS observation_gaps(start REAL NOT NULL, end REAL NOT NULL);
        ''')
        columns={row[1] for row in self.db.execute('pragma table_info(model_observations)')}
        for column in ('requested_service_tier','response_service_tier','cache_policy'):
            if column not in columns:
                self.db.execute(f"alter table model_observations add column {column} TEXT NOT NULL DEFAULT ''")
        for column in TIMING_FIELDS:
            if column not in columns:
                self.db.execute(f'alter table model_observations add column {column} REAL')
        if 'wire_service_tier_present' not in columns:
            self.db.execute('alter table model_observations add column wire_service_tier_present INTEGER')
        self.db.commit()

    def write(self, home, attempt, ts, transport, response_id='', requested_model='',
              response_model='', status='', conflict=False,requested_service_tier='',response_service_tier='',
              observation_missing=False,request_observed_at=None,generation_observed_at=None,completed_observed_at=None,
              generation_latency_ms=None,completion_latency_ms=None,cache_policy='',wire_service_tier_present=None):
        # An explicit allowlist: no headers, URLs, prompts, outputs or exception text.
        self.db.execute('''INSERT INTO model_observations
            (home,attempt,ts,transport,response_id,requested_model,response_model,status,conflict,
             requested_service_tier,response_service_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (home_key(home), identifier(attempt), float(ts),
                         transport if transport in ('WebSocket', 'HTTP/SSE') else '',
                         identifier(response_id), identifier(requested_model), identifier(response_model),
                         status if status in ('pending', 'created', 'completed', 'failed', 'incomplete',
                                              'disconnected', 'http_error', 'unparsed') else '', int(bool(conflict)),
                         identifier(requested_service_tier), identifier(response_service_tier)))
        values=(int(bool(observation_missing)),request_observed_at,generation_observed_at,completed_observed_at,
                generation_latency_ms,completion_latency_ms)
        values=tuple(v if isinstance(v,(int,float)) and math.isfinite(v) and v>=0 else None for v in values)
        self.db.execute('UPDATE model_observations SET '+','.join(k+'=?' for k in TIMING_FIELDS)+
                        ',cache_policy=? WHERE seq=last_insert_rowid()',(*values,identifier(cache_policy)))
        present=int(wire_service_tier_present) if type(wire_service_tier_present) is bool else None
        self.db.execute('UPDATE model_observations SET wire_service_tier_present=? WHERE seq=last_insert_rowid()',(present,))
        self.db.commit()

    def gap(self,start,end):
        self.db.execute('INSERT INTO observation_gaps VALUES(?,?)',(start,end))
        self.db.commit()

    def close(self):
        self.db.close()


class EvidenceReader:
    """Incremental read-only join, scoped to Codex home and exact response ID."""
    def __init__(self, path):
        self.path = Path(path)
        self.db = None
        self.offset = 0
        self.records = {}
        self.activity_records = {}
        self.revision = 0
        self.error = ''
        self.cache = {}
        self.dependencies = {}
        self.gaps = []

    def poll(self):
        self.error = ''
        if self.db is None:
            if not self.path.exists():
                return
            try:
                self.db = sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.1)
                self.db.row_factory = sqlite3.Row
                self.db.execute('PRAGMA query_only=ON')
            except sqlite3.Error:
                self.error = '모델 관측 기록을 읽을 수 없습니다'
                return
        try:
            if self.db.execute("select 1 from sqlite_master where name='observation_gaps'").fetchone():
                gaps=[tuple(r) for r in self.db.execute('SELECT start,end FROM observation_gaps')]
                if gaps!=self.gaps:
                    self.gaps=gaps; self.cache.clear(); self.revision+=1
            rows = self.db.execute('SELECT * FROM model_observations WHERE seq>? ORDER BY seq LIMIT 5000',
                                   (self.offset,)).fetchall()
        except sqlite3.Error:
            self.error = '모델 관측 기록을 읽을 수 없습니다'
            return
        for row in rows:
            self.offset = row['seq']
            self.activity_records[(row['home'], row['attempt'])] = dict(row)
            if row['response_id']:
                key = (row['home'], row['response_id'])
                self.records.setdefault(key, {})[row['attempt']] = dict(row)
                # Invalidate only joined histories containing this exact response.
                for cache_key in self.dependencies.pop(key,set()):
                    self.cache.pop(cache_key,None)
        if rows:
            self.revision += 1

    def enrich(self, home, rows, session_id=None):
        key = home_key(home)
        cache_key=(key,session_id if session_id is not None else id(rows))
        cached = self.cache.get(cache_key)
        if cached is not None and cached[0] is rows:
            return cached[1]
        result = []
        for row in rows:
            evidence = list(self.records.get((key, row['key']), {}).values())
            missing=any(e.get('observation_missing') or any(a<=e['ts']<=b for a,b in self.gaps) for e in evidence)
            req = {e['requested_model'] for e in evidence if e['requested_model']}
            res = {e['response_model'] for e in evidence if e['response_model']}
            conflict = missing or len(req)>1 or len(res)>1 or any(e['conflict'] for e in evidence)
            paired = any(e['requested_model'] and e['response_model'] for e in evidence)
            requested_tiers={e.get('requested_service_tier') for e in evidence if e.get('requested_service_tier')}
            policies={e.get('cache_policy') or None for e in evidence}
            policy=next(iter(policies)) if len(policies)==1 and not missing else None
            response_tiers={e.get('response_service_tier') for e in evidence if e.get('response_service_tier')}
            mode={}
            # Storage gaps and response-model conflicts describe different
            # observations; they cannot erase an applicable historical setting.
            mode_conflict=len(requested_tiers)>1
            if requested_tiers or mode_conflict:
                tier=next(iter(requested_tiers)) if len(requested_tiers)==1 and not mode_conflict else '미확인'
                mode={'service_tier':tier,'requested_service_tier':tier,'service_tier_source':'wire',
                      'mode_evidence':'요청 등급 관측' if not mode_conflict else '요청 등급 관측 충돌'}
                mode['request_mode_source']='conflict' if mode_conflict else 'wire'
            if len(response_tiers)==1:mode['response_service_tier']=next(iter(response_tiers))
            mode['response_tier_confirmed']=bool(evidence and not conflict and len(response_tiers)==1
                and len(requested_tiers)<=1 and all(e['status']=='completed' and e['requested_model']
                    and e['response_model'] and e.get('response_service_tier') for e in evidence))
            requested = next(iter(req)) if len(req)==1 else ''
            responded = next(iter(res)) if len(res)==1 else ''
            completed_pair = any(e['status']=='completed' and e['requested_model']==requested
                                 and e['response_model']==responded for e in evidence)
            alert_confirmed = bool(requested and responded and requested!=responded
                and not conflict and completed_pair and all(e['status']=='completed' for e in evidence))
            # Transport is observed on the response's wire, independently of model
            # naming/completeness. Never replace conflicting wire evidence with a guess.
            transports = {e['transport'] for e in evidence if e['transport'] in ('WebSocket', 'HTTP/SSE')}
            transport = {}
            if transports:
                exact = len(transports) == 1
                transport = {
                    'transport': next(iter(transports)) if exact else '미확인',
                    'transport_source': 'response_id' if exact else 'conflict',
                    'transport_ts': max(evidence, key=lambda e: e['seq'])['ts'],
                    'transport_evidence': '프록시 관측 · 응답 ID 일치' if exact else '동일 응답 ID의 연결 방식 관측 충돌',
                    'transport_endpoint': '', 'transport_turn': '',
                }
            timing={}
            timing_valid=len(evidence)==1 and not conflict and evidence[0]['status']=='completed'
            if timing_valid:
                timing={field:evidence[0].get(field) for field in TIMING_FIELDS[1:]}
            model_conflict=len(req)>1
            configured=(row.get('configured_model') if 'configured_model' in row else row.get('model')) or ''
            analysis_model='' if model_conflict else requested or configured
            complete=bool(evidence) and all(e['status']=='completed' for e in evidence)
            presence={e.get('wire_service_tier_present') for e in evidence if e.get('wire_service_tier_present') is not None}
            wire_present=bool(next(iter(presence))) if len(presence)==1 else True if requested_tiers and not presence else None
            # Older sanitized records did not keep key presence. Preserve that
            # uncertainty instead of claiming omission from an empty string.
            result.append({**row, **transport, **mode, **timing, 'observation_missing':missing,
                           'wire_observed':bool(evidence),'wire_service_tier_present':wire_present,
                           'model':analysis_model,'analysis_model':analysis_model,'configured_model':configured,
                           'model_source':'conflict' if model_conflict else 'wire' if requested else 'settings' if configured else 'unknown',
                           'model_conflict':model_conflict,'mode_conflict':mode_conflict,'timing_valid':timing_valid,
                           'cache_policy':policy or None,'cache_policy_conflict':len(policies)>1,
                           'requested_model':requested, 'response_model':responded,
                           'model_match':'관측 충돌' if model_conflict or conflict and not missing else '관측 누락' if missing else compare(requested, responded, not complete or not paired),
                           'model_evidence':'저장 관측 누락' if missing else '관측 충돌' if conflict else ('응답 ID 연결' if evidence else '관측 없음'),
                           'response_status':max(evidence,key=lambda e:e['seq'])['status'] if evidence else '',
                           'model_alert_confirmed':alert_confirmed,
                           'model_observation_ts':min(e['ts'] for e in evidence) if evidence else None})
        # Keep only the current snapshot of each session, not every historical list.
        if len(self.cache)>2048:
            self.cache.clear()
            self.dependencies.clear()
        if cached:
            for row in cached[0]:
                dependents=self.dependencies.get((key,row['key']),set())
                dependents.discard(cache_key)
                if not dependents:self.dependencies.pop((key,row['key']),None)
        self.cache[cache_key] = (rows,result)
        for row in rows:
            self.dependencies.setdefault((key,row['key']),set()).add(cache_key)
        return result

    def close(self):
        if self.db is not None:
            self.db.close()
