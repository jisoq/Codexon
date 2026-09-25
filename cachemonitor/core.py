from __future__ import annotations

import re
import sqlite3
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

WINDOW = 30 * 60
SESSION_WINDOW = 24 * 60 * 60
TRANSPORT_FIELDS = ("transport", "transport_ts", "transport_evidence", "transport_endpoint", "transport_turn", "transport_source")
METRICS = ("input", "cached", "uncached", "written", "ordinary_input", "output", "reasoning", "non_reasoning", "total")
LINEAGE_FIELDS = ('parent_thread_id', 'agent_path', 'agent_nickname', 'spawn_depth')


def session_lineage(payload, thread_id):
    """Only a matching rollout header can establish parent/child ownership."""
    if not isinstance(payload, dict) or payload.get('id') != thread_id:
        return {}
    source = payload.get('source')
    subagent = source.get('subagent') if isinstance(source, dict) else None
    spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
    spawn = spawn if isinstance(spawn, dict) else {}
    direct, nested = payload.get('parent_thread_id'), spawn.get('parent_thread_id')
    if direct and nested and direct != nested:
        return {}
    parent = direct or nested
    if not isinstance(parent, str) or parent == thread_id or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', parent):
        return {}
    result = {'parent_thread_id':parent}
    path = spawn.get('agent_path')
    if isinstance(path, str) and re.fullmatch(r'/[A-Za-z0-9_/-]{1,256}', path):
        result['agent_path'] = path
    nickname = payload.get('agent_nickname') or spawn.get('agent_nickname')
    if isinstance(nickname, str) and 0 < len(nickname) <= 128 and '\n' not in nickname and '\r' not in nickname:
        result['agent_nickname'] = nickname
    depth = spawn.get('depth')
    if type(depth) is int and depth > 0:
        result['spawn_depth'] = depth
    return result


def transport_label(row):
    kind = row.get('transport')
    source = row.get('transport_source', 'log_time' if kind in ('WebSocket', 'HTTP/SSE') else 'unknown')
    if source == 'conflict': return '미확인 · 관측 충돌'
    if kind not in ('WebSocket', 'HTTP/SSE'): return '미확인'
    return kind + ('' if source == 'response_id' else ' · 추정')


def token_number(value):
    return value if type(value) is int and value >= 0 else None


def day_start(now):
    return datetime.fromtimestamp(now, timezone.utc).astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def usage_values(usage):
    inp = token_number(usage.get("input_tokens"))
    output = token_number(usage.get("output_tokens"))
    cached = token_number(usage.get("cached_input_tokens", (usage.get("input_tokens_details") or {}).get("cached_tokens")))
    reasoning = token_number(usage.get("reasoning_output_tokens", (usage.get("output_tokens_details") or {}).get("reasoning_tokens")))
    written = token_number(usage.get("cache_write_input_tokens", (usage.get("input_tokens_details") or {}).get("cache_write_tokens")))
    reported = token_number(usage.get("total_tokens"))
    input_conflict = inp is not None and ((cached is not None and cached > inp) or
        (written is not None and written > inp) or
        (cached is not None and written is not None and cached + written > inp))
    output_conflict = output is not None and reasoning is not None and reasoning > output
    if cached is not None and inp is not None and cached > inp:
        cached = None
    if written is not None and inp is not None and written > inp:
        written = None
    if output_conflict:
        reasoning = None
    return {"input": inp, "cached": cached, "written": written, "output": output, "reasoning": reasoning,
            "uncached": inp - cached if inp is not None and cached is not None else None,
            "ordinary_input": inp - cached - written if all(v is not None for v in (inp,cached,written)) and not input_conflict else None,
            "non_reasoning": output - reasoning if output is not None and reasoning is not None else None,
            "total": inp + output if inp is not None and output is not None else reported,
            "reported_total": reported, "total_discrepancy": reported is not None and inp is not None and output is not None and reported != inp + output,
            "input_conflict": input_conflict, "output_conflict": output_conflict}


def token_parts(row):
    """Lossless disjoint composition on each independently confirmed denominator."""
    i,r,w,o,q=(token_number(row.get(k)) for k in ('input','cached','written','output','reasoning'))
    inp={'ordinary':0,'read':0,'write':0,'unclassified':0,'total':i}
    out={'ordinary':0,'reasoning':0,'unclassified':0,'total':o}
    if i is not None:
        conflict=row.get('input_conflict') or (r is not None and r>i) or (w is not None and w>i) or (r is not None and w is not None and r+w>i)
        if conflict:inp['unclassified']=i
        else:
            inp['read']=r or 0;inp['write']=w or 0
            if r is not None and w is not None:inp['ordinary']=i-r-w
            else:inp['unclassified']=i-inp['read']-inp['write']
    if o is not None:
        if q is None or q>o or row.get('output_conflict'):out['unclassified']=o
        else:out['ordinary']=o-q;out['reasoning']=q
    return {'input':inp,'output':out}


def summarize(rows):
    rows = list(rows)
    result = {"count": len(rows), "missing": {}}
    for key in METRICS:
        known = [r[key] for r in rows if token_number(r.get(key)) is not None]
        result[key] = sum(known) if known or not rows else None
        result["missing"][key] = len(rows) - len(known)
    known = [r for r in rows if token_number(r.get("cached")) is not None and token_number(r.get("input")) is not None and r['cached']<=r['input']]
    inputs = sum(r["input"] for r in known)
    result["rate"] = 100 * sum(r["cached"] for r in known) / inputs if inputs else None
    return result


def group_usage(rows):
    groups = {}
    for row in rows:
        groups.setdefault((row.get("model") or "미확인", row.get("effort") or "미확인"), []).append(row)
    return sorted(({"model": model, "effort": effort, **summarize(items)} for (model, effort), items in groups.items()),
                  key=lambda r: r["total"] or 0, reverse=True)
def combined_usage_groups(sessions, scope):
    buckets = {}
    for session in sessions:
        for group in session["groups"][scope]:
            buckets.setdefault((group["model"], group["effort"]), []).append(group)
    result = []
    for (model, effort), groups in buckets.items():
        merged = summarize(groups)
        merged["count"] = sum(g["count"] for g in groups) if all(g["count"] is not None for g in groups) else None
        merged["missing"] = {key: sum(g.get("missing", {}).get(key, 0) for g in groups) for key in METRICS}
        result.append({"model": model, "effort": effort, **merged})
    return sorted(result, key=lambda g: g["total"] or 0, reverse=True)


TITLE_HANDLER = "codex_core::session::handlers"
TITLE_PROMPT = "You are a helpful assistant. You will be presented with a user prompt, and your job is to provide a short title for a task that will be created from that prompt."


def is_title_request(row):
    if row["target"] != TITLE_HANDLER or not row["thread_id"]:
        return False
    body = row["feedback_log_body"] or ""
    prefix = f"session_loop{{thread_id={row['thread_id']}}}: Submission sub=Submission {{"
    return (body.startswith(prefix)
            and 'op: TurnInput { request: TurnInputRequest { input: UserInput { content: [Text { text: "' + TITLE_PROMPT in body
            and '"required": Array [String("title"), String("description")]' in body)


TARGETS = (
    "feedback_tags", "codex_core::client", "codex_core::responses_retry",
    "codex_api::endpoint::responses_websocket",
)
UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


def stamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0.0


@contextmanager
def readonly(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


@dataclass
class Transport:
    ts: float
    kind: str
    endpoint: str = ""
    evidence: str = ""
    process: str = ""
    turn: str = ""
    model: str = ""


def parse_transport(row):
    """Only accept transport-owned records, never conversation/tool text."""
    if row["target"] not in TARGETS:
        return None
    body = row["feedback_log_body"] or ""
    # The final tracing message is distinct from its enclosing span fields.
    span, _, message = body.rpartition("}: ")
    if not span:
        message = body
    tid = row["thread_id"]
    if not tid:
        m = re.search(r'(?:thread_id|thread\.id)="?(' + UUID + r')', span)
        tid = m.group(1) if m else None
    kind = None
    evidence = "요청 경로 기록"
    if row["target"] == "codex_core::client" and message.strip() == "falling back to HTTP":
        kind, evidence = "HTTP/SSE", "HTTP 전환 기록"
    elif row["target"] == "feedback_tags" and 'auth_header_attached=' in message:
        if 'transport="responses_websocket"' in span:
            kind = "WebSocket"
        elif 'transport="responses_http"' in span:
            kind = "HTTP/SSE"
    elif row["target"] == "codex_api::endpoint::responses_websocket" and message.startswith("connecting to websocket:"):
        kind, evidence = "WS 연결 시도", "성공 여부 미확인"
    if not kind:
        return None
    endpoint = ""
    m = re.search(r'api\.path="([^"\s]+)"', span)
    if m:
        endpoint = m.group(1)
    if message.startswith("connecting to websocket:"):
        url = urlsplit(message.split("connecting to websocket:", 1)[1].strip())
        # Strip credentials, queries and fragments before retaining any URL.
        if url.hostname:
            endpoint = f"{url.scheme}://{url.hostname}{url.path}"
    turn = re.search(r'(?:turn_id|turn\.id)="?(' + UUID + r')', span)
    model = re.search(r'\bmodel="?([^"\s}]+)', span)
    return tid, Transport(float(row["ts"]) + (row["ts_nanos"] or 0) / 1e9,
                          kind, endpoint, evidence, row["process_uuid"] or "",
                          turn.group(1) if turn else "", model.group(1) if model else "")


@dataclass
class Request:
    ts: float
    key: str
    input: int | None
    cached: int | None
    written: int | None
    output: int | None
    model: str
    turn: str = ""
    reasoning: int | None = None
    total: int | None = None
    effort: str = "미확인"
    service_tier: str = "미확인"
    service_tier_source: str = "record"
    # Historical clear snapshots explicitly mean Standard; absent snapshots do not.
    configured_model: str = ""
    model_source: str = "settings"
    reported_total: int | None = None
    total_discrepancy: bool = False
    input_conflict: bool = False
    output_conflict: bool = False
    configured_service_tier: str | None = None
    request_mode_source: str = "unknown"
    request_mode_action: str = "unknown"
    compaction_epoch: int = 0

    def values(self):
        return {**vars(self), "call_id":self.key, "uncached": self.input - self.cached if self.input is not None and self.cached is not None else None,
                "ordinary_input": self.input-self.cached-self.written if all(v is not None for v in (self.input,self.cached,self.written)) and not self.input_conflict else None,
                "non_reasoning": self.output - self.reasoning if self.output is not None and self.reasoning is not None else None,
                "rate": self.rate}

    @property
    def rate(self):
        return 100 * self.cached / self.input if self.input and self.cached is not None else None


@dataclass
class Session:
    id: str
    home: str
    title: str = ""
    model: str = ""
    provider: str = ""
    cwd: str = ""
    modern: bool = False
    total_key: tuple = ()
    requests: deque = field(default_factory=deque)
    transports: deque = field(default_factory=deque)
    keys: set = field(default_factory=set)
    running: bool = False
    state_ts: float = 0
    activity: float = 0
    pending: bool = False
    contexts: dict = field(default_factory=dict)
    current_turn: str = ""
    request_state: dict = field(default_factory=dict)
    turn_records: dict = field(default_factory=dict)
    configured_model: str = ""
    current_effort: str = "미확인"
    current_service_tier: str = "미확인"
    tier_contexts: dict = field(default_factory=dict)
    tier_evidence: dict = field(default_factory=dict)
    current_tier_evidence: dict = field(default_factory=dict)
    reported_usage: dict = field(default_factory=dict)
    reported_ts: float = 0
    metadata_seen: bool = False
    title_request: bool = False
    coverage_total: int = 0
    coverage_last: tuple | None = None
    coverage_gaps: list = field(default_factory=list)
    coverage_initial: int = 0
    parent_thread_id: str = ""
    agent_path: str = ""
    agent_nickname: str = ""
    spawn_depth: int | None = None
    compaction_epoch: int = 0

    @property
    def excluded_title(self):
        return self.title_request and not self.metadata_seen and not self.requests and self.model == "gpt-5.6-luna"

    def add_usage(self, ts, key, usage, model, turn="", effort="미확인", service_tier="미확인",service_tier_source="record",configured_model="",model_source="settings",mode_metadata=None):
        if key in self.keys:
            return
        values = usage_values(usage)
        inp, cache, written = values["input"], values["cached"], values["written"]
        self.keys.add(key)
        self.requests.append(Request(ts, key, inp, cache, written,
                                      values["output"], model, turn, values["reasoning"], values["total"], effort, service_tier,service_tier_source,
                                      configured_model or model,model_source,values['reported_total'],values['total_discrepancy'],values['input_conflict'],values['output_conflict']))
        request=self.requests[-1]
        request.compaction_epoch=self.compaction_epoch
        for field_name,value in (mode_metadata or {}).items():
            if field_name in ('configured_service_tier','request_mode_source','request_mode_action'):setattr(request,field_name,value)
        self.coverage_total += values['total'] or 0
        self.activity = max(self.activity, ts)

    def consume(self, event, now):
        previous_report = (self.reported_ts, self.reported_usage.get('total'))
        ts = stamp(event.get("timestamp"))
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return
        kind = event.get("type")
        if kind=='compacted' or kind=='event_msg' and payload.get('type')=='context_compacted':
            self.compaction_epoch+=1
            return
        if kind == 'session_meta':
            for key, value in session_lineage(payload, self.id).items():
                setattr(self, key, value)
        elif kind == "turn_context":
            self.configured_model = payload.get("model") or ""
            self.model = self.configured_model or self.model
            self.current_turn = payload.get("turn_id") or self.current_turn
            effort = payload.get("effort") or payload.get("reasoning_effort") or (payload.get("collaboration_mode") or {}).get("settings", {}).get("reasoning_effort")
            self.current_effort = effort if isinstance(effort, str) else "미확인"
            self.contexts[self.current_turn] = (self.configured_model, self.current_effort)
            self.tier_contexts[self.current_turn] = ('Standard' if payload.get('service_tier') is None else payload['service_tier']) if 'service_tier' in payload else self.current_service_tier
            self.tier_evidence[self.current_turn] = (dict(configured_service_tier=payload['service_tier'],request_mode_action='clear' if payload['service_tier'] is None else 'set',request_mode_source='turn_context')
                if 'service_tier' in payload else dict(self.current_tier_evidence))
        elif kind == "token_usage_record":
            if payload.get("thread_id", self.id) != self.id:
                return  # Forked history must not become this session's usage.
            if not self.modern and any(str(r.key).startswith('legacy:') for r in self.requests):
                self.requests=deque(r for r in self.requests if not str(r.key).startswith('legacy:'))
                self.keys={r.key for r in self.requests}
                self.coverage_total=sum(r.total or 0 for r in self.requests)
            self.modern = True
            turn = payload.get("turn_id", "")
            model, effort = self.contexts.get(turn, ("", "미확인"))
            self.add_usage(ts, payload.get("response_id") or f"record:{event.get('event_id') or event.get('ordinal') or len(self.keys)}",
                           payload.get("usage") or {}, model, turn, effort,
                           payload.get('service_tier',self.tier_contexts.get(turn,'미확인')),
                           'record' if 'service_tier' in payload else 'settings',model,
                           mode_metadata=self.tier_evidence.get(turn,{}))
            if ts >= self.reported_ts and isinstance(payload.get("thread_token_usage"), dict):
                self.reported_usage = usage_values(payload["thread_token_usage"])
                self.reported_ts = ts
        elif kind == "event_msg":
            event_type = payload.get("type")
            if event_type=='thread_settings_applied' and payload.get('thread_id')==self.id:
                # This is a historical, successfully applied settings snapshot,
                # not today's config. It affects subsequent turn contexts only.
                settings=payload.get('thread_settings') or {}
                if 'service_tier' in settings:
                    value=settings['service_tier']
                    self.current_service_tier='Standard' if value is None else value if isinstance(value,str) and value else '미확인'
                    self.current_tier_evidence=dict(configured_service_tier=value,request_mode_action='clear' if value is None else 'set',request_mode_source='applied_settings')
            if event_type in ("task_started", "task_complete", "task_completed", "turn_aborted") and ts >= self.state_ts:
                self.state_ts = ts
                self.running = event_type == "task_started"
                self.activity = max(self.activity, ts)
                turn_id=payload.get('turn_id')
                if turn_id:
                    previous=self.turn_records.get(turn_id,{})
                    self.request_state={'turn':turn_id,'state':{'task_started':'진행','turn_aborted':'중단'}.get(event_type,'완료'),
                                        'started_at':ts if self.running else previous.get('started_at'),
                                        'ended_at':None if self.running else ts,'observed_at':ts}
                    self.turn_records[turn_id]=dict(self.request_state)
                else:self.request_state={}
                if self.running:
                    self.current_turn = payload.get("turn_id", "")
                    self.current_effort = "미확인"
            if event_type == "token_count" and not self.modern:
                info = payload.get("info") or {}
                total = info.get("total_token_usage") or {}
                fingerprint = tuple(sorted(total.items()))
                if fingerprint and fingerprint != self.total_key:
                    self.total_key = fingerprint
                    self.add_usage(ts, f"legacy:{event.get('event_id') or event.get('ordinal') or len(self.keys)}", info.get("last_token_usage") or {}, self.configured_model,
                                   self.current_turn, self.current_effort,
                                   self.tier_contexts.get(self.current_turn,'미확인'),'settings')
                    if ts >= self.reported_ts:
                        self.reported_usage, self.reported_ts = usage_values(total), ts
        current_report = (self.reported_ts, self.reported_usage.get('total'))
        if current_report != previous_report and current_report[1] is not None:
            residual = current_report[1] - self.coverage_total
            if self.coverage_last is None:
                self.coverage_initial = max(0, residual)
            elif residual > self.coverage_last[1]:
                self.coverage_gaps.append({'start':self.coverage_last[0], 'end':self.reported_ts,
                                           'tokens':residual-self.coverage_last[1]})
            self.coverage_last = (self.reported_ts, residual)

    def trim(self, now):
        # Keep one earlier observation for attribution to the first retained request.
        while len(self.transports) > 1 and self.transports[1].ts < now - WINDOW:
            self.transports.popleft()

    def transport_details(self, request):
        observation = max((t for t in self.transports
                           if t.ts <= request.ts and
                           (not request.turn or not t.turn or request.turn == t.turn)),
                          key=lambda t: t.ts, default=None)
        return {
            "transport": observation.kind if observation and observation.kind in ("WebSocket", "HTTP/SSE") else "미확인",
            "transport_source": "log_time" if observation and observation.kind in ("WebSocket", "HTTP/SSE") else "unknown",
            "transport_ts": observation.ts if observation else None,
            "transport_evidence": observation.evidence if observation else "대응하는 연결 기록 없음",
            "transport_endpoint": observation.endpoint if observation else "",
            "transport_turn": observation.turn if observation else "",
        }

    def transport_at(self, request):
        return self.transport_details(request)["transport"]

    def view(self, now, use_cache=False):
        signature=(len(self.requests),tuple(vars(self.requests[-1]).items()) if self.requests else (),
                   tuple(sorted(self.reported_usage.items())),self.running,self.activity,self.pending,
                   self.title,self.cwd,self.model,self.provider,tuple(self.request_state.items()),
                   tuple(getattr(self,key) for key in LINEAGE_FIELDS),
                   tuple(tuple(vars(t).items()) for t in self.transports)) if use_cache else None
        old=getattr(self,'_cached_view',None)
        same=use_cache and old is not None and signature==getattr(self,'_view_signature',None)
        if same and self._view_time <= now < self._view_until:
            result=dict(old)
            result['remaining']=max(0,WINDOW-(now-self._last_recent_ts)) if self._last_recent_ts is not None else None
            if self.running:
                result['status']='진행 기록' if now-self.activity<WINDOW else '진행 기록 · 갱신 지연'
            return result
        history = old['history'] if same else [x.values() for x in self.requests]
        requests = [x for x in self.requests if x.ts >= now - WINDOW]
        last = requests[-1] if requests else None
        connection_history = old['history'] if same else [{**x.values(), **self.transport_details(x)} for x in self.requests]
        matched = {(r["transport_ts"], r["transport"], r["transport_turn"]) for r in connection_history}
        latest = self.transports[-1] if self.transports else None
        known = [x for x in requests if x.input is not None and x.cached is not None]
        total = sum(x.input for x in known)
        cached = sum(x.cached for x in known)
        remaining = max(0, WINDOW - (now - last.ts)) if last else None
        from .cache_misses import classify
        cache_misses=old['cache_misses'] if same else classify(history)
        warning = ""
        if latest and latest.kind == "HTTP/SSE" and any(t.evidence == "HTTP 전환 기록" for t in self.transports if t.process == latest.process):
            warning = "HTTP 전환" + (" · 캐시 미적중" if warning else "")
        if self.running:
            status = "진행 기록" if now - self.activity < WINDOW else "진행 기록 · 갱신 지연"
        else:
            status = "관찰 중"
        today = day_start(now)
        subsets = {"session": history, "today": [r for r in history if r["ts"] >= today],
                   "30m": [r for r in history if r["ts"] >= now - WINDOW]}
        totals = {key: old['totals'][key] if same and key=='session' else summarize(rows) for key, rows in subsets.items()}
        groups = {key: old['groups'][key] if same and key=='session' else group_usage(rows) for key, rows in subsets.items()}
        unclassified = None
        observed_total = totals["session"]["total"] or 0
        if self.reported_usage.get("total", 0) is not None and self.reported_usage.get("total", 0) > observed_total:
            unclassified = {key: self.reported_usage[key] - totals["session"][key]
                            if self.reported_usage.get(key) is not None and totals["session"][key] is not None
                            and self.reported_usage[key] >= totals["session"][key] else None for key in METRICS}
        result = {
            "id": self.id, "home": self.home, "title": self.title or f"세션 {self.id[:8]}",
            "model": self.model, "provider": self.provider, "cwd": self.cwd,
            **{key:getattr(self,key) for key in LINEAGE_FIELDS},
            "status": status, "running": self.running, "activity": self.activity,
            "request_state":dict(self.request_state),
            "turn_records":{k:dict(v) for k,v in self.turn_records.items()},
            "transport": latest.kind if latest else "미확인",
            "transport_ts": latest.ts if latest else None,
            "endpoint": latest.endpoint if latest else "",
            "transport_evidence": latest.evidence if latest else "연결 기록 없음",
            "rate": last.rate if last else None, "weighted_rate": 100 * cached / total if total else None,
            "input": last.input if last else None, "cached": last.cached if last else None,
            "remaining": remaining, "warning": warning, "pending": self.pending,
            "cache_misses": cache_misses,
            "requests": [{**vars(x), "rate": x.rate, **self.transport_details(x)} for x in requests],
            "history": connection_history,
            "unmatched_transports": [vars(t) for t in self.transports if (t.ts, t.kind, t.turn) not in matched],
            "totals": totals, "groups": groups, "unclassified": unclassified,
            "coverage_gaps": list(self.coverage_gaps), "coverage_initial":self.coverage_initial,
            "coverage_available":self.coverage_last is not None,
            "transports": [vars(x) for x in self.transports],
        }
        if use_cache:
            # Time-window expiry changes recent counters, not historical usage identity.
            if same: result['history']=old['history']
            tomorrow=datetime.fromtimestamp(now).date().toordinal()+1
            try:
                next_day=datetime.fromordinal(tomorrow).timestamp()
            except OSError:  # Windows CRT cannot localize some synthetic dates near the epoch.
                next_day=day_start(now)+86400
            expiries=[r.ts+WINDOW+0.000001 for r in requests if r.ts+WINDOW>=now]
            self._view_until=min(expiries+[next_day])
            self._last_recent_ts=last.ts if last else None
            self._view_time=now
            self._view_signature=signature
            self._cached_view=result
        return result


class SessionRegistry:
    """Sessions and transport observations shared by the usage index."""
    def __init__(self):
        self.sessions = {}
        self.cursors = {}
        self.title_requests = {}
        self.unassigned = deque()

    def session(self, home, tid):
        key = (str(home), tid)
        if key not in self.sessions:
            self.sessions[key] = Session(tid, str(home), title_request=key in self.title_requests)
        return self.sessions[key]

    def read_logs(self, home, now):
        with readonly(home / "logs_2.sqlite") as conn:
            high = conn.execute("SELECT max(id) FROM logs").fetchone()[0] or 0
            cursor = self.cursors.get(str(home))
            if cursor is None or high < cursor:
                # Indexed timestamp lookup, then incremental primary-key reads.
                cursor = conn.execute("SELECT min(id) FROM logs WHERE ts>=?", (now - SESSION_WINDOW,)).fetchone()[0]
                cursor = cursor - 1 if cursor else high
            placeholders = ",".join("?" for _ in TARGETS)
            rows = conn.execute(
                f"SELECT id,ts,ts_nanos,target,thread_id,process_uuid,feedback_log_body FROM logs "
                f"WHERE id>? AND id<=? AND ((target IN ({placeholders}) AND ts>=?) "
                "OR (target=? AND instr(feedback_log_body,?)>0)) ORDER BY id",
                (cursor, high, *TARGETS, now - WINDOW, TITLE_HANDLER, TITLE_PROMPT))
            for row in rows:
                if is_title_request(row):
                    key = (str(home), row["thread_id"])
                    self.title_requests[key] = row["ts"]
                    if key in self.sessions:
                        self.sessions[key].title_request = True
                    continue
                parsed = parse_transport(row)
                if not parsed:
                    continue
                tid, transport = parsed
                if tid:
                    s = self.session(home, tid)
                    if transport.model:
                        s.model = transport.model
                    if transport.endpoint.startswith("/") and s.transports:
                        previous = s.transports[-1].endpoint
                        if "://" in previous and urlsplit(previous).path == transport.endpoint:
                            transport.endpoint = previous
                    s.transports.append(transport)
                    s.activity = max(s.activity, transport.ts)
                else:
                    self.unassigned.append({"home": str(home), **vars(transport)})
            self.cursors[str(home)] = high
