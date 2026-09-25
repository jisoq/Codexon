"""Loop-owned admission and safe retirement, independent of observation storage."""
import asyncio
import re
import time
from collections import Counter, OrderedDict, defaultdict, deque
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ConnectionPolicy:
    budget: int = 100
    idle_limit: int = 32
    idle_seconds: float = 300
    lifetime_seconds: float = 1800
    pressure_start: int = 90
    pressure_target: int = 80
    check_seconds: float = 1
    close_seconds: float = 5
    capacity_seconds: float = 5
    waiters: int = 16


POLICY = ConnectionPolicy()
SERVER_NOTIFICATIONS = ('codex.rate_limits', 'codex.response.metadata', 'responsesapi.websocket_timing')


class LocalCapacity(Exception):
    pass


class Activity:
    def __init__(self, clock=time.monotonic, known=True):
        self.clock = clock
        self.connected_at = None
        self.connecting = False
        self.idle_since = None
        self.pending = defaultdict(deque)
        self.active = {}
        self.completed = OrderedDict()
        self.cancelling = set()
        self.forwarding = 0
        self.unknown = not known
        self.unknown_events = set()
        self.closing = False

    @property
    def busy(self):
        return self.unknown or self.forwarding or self.active or any(self.pending.values())

    @property
    def state(self):
        if self.closing: return 'closing'
        if self.connected_at is None: return 'connecting' if self.connecting else 'reserved'
        if self.unknown: return 'unknown'
        return 'responding' if self.busy else 'idle'

    def connected(self):
        self.connected_at = self.clock()
        self.settle()

    def unclassified(self, typ, outbound):
        self.unknown = True
        # Only a bounded protocol identifier is diagnostic; never retain bodies.
        name = typ if isinstance(typ, str) and re.fullmatch(r'[a-z][a-z0-9_.]{0,63}', typ) else 'invalid_event'
        if len(self.unknown_events) < 8:
            self.unknown_events.add(('client:' if outbound else 'upstream:') + name)

    def settle(self):
        if self.busy: self.idle_since = None
        elif self.idle_since is None: self.idle_since = self.clock()

    def begin(self, obj, outbound):
        if self.closing: return False
        self.forwarding += 1
        typ = obj.get('type', '')
        lane = obj.get('stream_id', '')
        if outbound:
            if typ in ('response.create', 'response.steer'):
                self.pending[lane].append(obj.get('event_id', ''))
                self.idle_since = None
            elif typ == 'response.cancel':self.cancelling.add(lane)
            elif typ not in ('response.cancel', 'ping', 'pong', 'ack', 'session.update'):
                self.unclassified(typ, True)
        return True

    def delivered(self, obj, outbound):
        # Only retire after the client received the last terminal frame.
        if not outbound:
            typ = obj.get('type', '')
            response = obj.get('response') or {}
            rid = response.get('id') or obj.get('response_id')
            lane = obj.get('stream_id', '')
            terminal = typ in ('response.completed', 'response.failed', 'response.incomplete', 'response.cancelled')
            if rid and rid not in self.completed:
                if rid not in self.active:
                    if self.pending[lane]: self.pending[lane].popleft()
                    self.active[rid] = lane
                if terminal:
                    ended_lane=self.active.pop(rid, None)
                    self.cancelling.discard(ended_lane)
                    self.completed[rid] = True
                    if len(self.completed) > 2048: self.completed.popitem(last=False)
            elif typ == 'error':
                if lane in self.cancelling:pass
                elif len(self.pending[lane]) == 1 and lane not in self.active.values():
                    self.pending[lane].popleft()
                elif self.pending[lane] or self.active: self.unclassified(typ, False)
            elif not isinstance(typ,str) or not typ or not (typ.startswith(('response.', 'session.', 'rate_limits.', 'usage.', 'status.'))
                                  or typ in ('ping', 'pong', 'ack', 'usage', 'status', *SERVER_NOTIFICATIONS)):
                self.unclassified(typ, False)
        self.forwarding -= 1
        self.settle()


class WebSocketBudget:
    def __init__(self, policy=POLICY, clock=time.monotonic):
        self.policy, self.clock = policy, clock
        self.entries = {}
        self.waiting = 0
        self.changed = asyncio.Event()
        self.retirements = Counter()
        self.tasks = set()
        self.draining = False

    async def reserve(self, key, activity):
        self.reap(incoming=1)
        deadline = self.clock() + self.policy.capacity_seconds
        if self.waiting >= self.policy.waiters: raise LocalCapacity()
        self.waiting += 1
        try:
            while len(self.entries) >= self.policy.budget:
                if self.draining: raise LocalCapacity()
                self.changed.clear()
                remaining = deadline - self.clock()
                if remaining <= 0: raise LocalCapacity()
                try: await asyncio.wait_for(self.changed.wait(), remaining)
                except asyncio.TimeoutError: raise LocalCapacity() from None
                self.reap(incoming=1)
            if self.draining: raise LocalCapacity()
            self.entries[key] = (activity, None)
            self.reap()
        finally: self.waiting -= 1

    def connected(self, key, close):
        activity, _ = self.entries[key]
        self.entries[key] = (activity, close)
        activity.connected()
        # Give the admitted peer its first read opportunity. Reclaim older idle
        # sockets here; this one joins the regular one-second scan immediately.
        self.reap(exclude=None if self.draining else key)

    def release(self, key):
        self.entries.pop(key, None)
        self.changed.set()

    def reap(self, incoming=0, exclude=None):
        now = self.clock()
        idle = sorted(((key, a, close) for key, (a, close) in self.entries.items()
                       if a.state == 'idle' and close), key=lambda row: row[1].idle_since)
        closing = sum(a.closing for a, _ in self.entries.values())
        pressure = len(self.entries) + incoming >= self.policy.pressure_start
        remaining = len(idle)
        for key, activity, close in idle:
            if key==exclude:continue
            reason = ('update' if self.draining else
                      'lifetime' if now - activity.connected_at >= self.policy.lifetime_seconds else
                      'idle_expired' if now - activity.idle_since >= self.policy.idle_seconds else
                      'idle_limit' if remaining > self.policy.idle_limit else
                      'capacity' if pressure and len(self.entries) + incoming - closing > self.policy.pressure_target else '')
            if not reason: continue
            # Atomic with begin(): no new request crosses this boundary.
            activity.closing = True
            remaining -= 1
            closing += 1
            self.retirements[reason] += 1
            task = asyncio.create_task(close(reason))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def status(self):
        counts = Counter(a.state for a, _ in self.entries.values())
        return dict(policy=asdict(self.policy), occupied=len(self.entries), capacity_waiting=self.waiting,
                    states={name:counts[name] for name in ('reserved','connecting','responding','idle','closing','unknown')},
                    pending_requests=sum(len(q) for a,_ in self.entries.values() for q in a.pending.values()),
                    active_responses=sum(len(a.active) for a,_ in self.entries.values()),retired=dict(self.retirements),
                    unknown_events=dict(Counter(event for a,_ in self.entries.values() for event in a.unknown_events)))

    async def close(self):
        if self.tasks: await asyncio.gather(*self.tasks, return_exceptions=True)
