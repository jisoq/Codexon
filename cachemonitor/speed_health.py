"""Conservative, call-scoped speed alerts from completed historical evidence.

No wall-clock polling, guessed throughput, or persistent writes. Replay in time
order keeps peer baselines causal and makes corrected records deterministic.
"""
from collections import defaultdict, deque, namedtuple
from statistics import median

from .analytics import INPUT_BANDS, call_identity, output_speed, output_speed_summary
from .core import token_number
from .pricing import request_tier

MIN_BASELINE = 8
MAX_BASELINE = 30
MAX_AGE = 7 * 86400
SLOW_CALLS = 3
RECOVERY_CALLS = 2
# Provisional 64 MiB metadata allowance. Tracing current metadata measured
# 24.80 MiB at 43,915 calls / 463 sessions; an earlier all-eligible fixture
# measured 29.35 MiB at 45,370 calls / 698 sessions. The 80,000-call cap projects
# about 46-52 MiB for those topologies. Unusual scope counts can cost more.
MAX_TRACKED_CALLS = 80000
MAX_TRACKED_SCOPES = 4096


def scope(row):
    """Only compare observed conditions and similar input/output/cache sizes."""
    speed = output_speed(row)
    inp, out, cached = (token_number(row.get(k)) for k in ('input', 'output', 'cached'))
    model = row.get('requested_model') or row.get('analysis_model') or row.get('model')
    effort, tier, transport = row.get('effort'), request_tier(row), row.get('transport')
    if (speed is None or inp is None or not inp or out is None or out < 128
            or cached is None or cached > inp
            or row.get('transport_source') != 'response_id'
            or any(row.get(k) for k in ('input_conflict', 'mode_conflict', 'cache_policy_conflict'))
            or any(v in (None, '', '미확인', '확인 불가') for v in (model, effort, tier, transport))):
        return None
    input_band = next(i for i, (low, high, _) in enumerate(INPUT_BANDS) if low <= inp < high)
    output_band = 0 if out < 512 else 1 if out < 2048 else 2 if out < 8192 else 3
    return (model, effort, tier, transport, input_band, output_band, cached / inp >= .75)


def baseline(rows):
    values = [output_speed(r) for r in rows]
    center = median(values)
    # Require both a material relative drop and departure from normal variation.
    deviation = median(abs(v - center) for v in values)
    average = output_speed_summary(rows)['value']
    return dict(baseline_speed=average,
                threshold=min(average * .5, center - 3 * 1.4826 * deviation),
                baseline_count=len(rows),
                baseline_keys=[r['key'] for r in rows])


def classify(sessions):
    sessions = list(sessions)
    timelines = sorted(((r['ts'], s['home'], s['id'], r)
                        for s in sessions for r in s.get('history', ())), key=lambda x: x[:3])
    pools = defaultdict(lambda: deque(maxlen=MAX_BASELINE * 4))
    states = {}
    result = {(s['home'], s['id']): dict(active=False) for s in sessions}
    seen = set()
    for ts, home, sid, row in timelines:
        # Ignore duplicate evidence before touching the session's current state.
        key = call_identity(home, sid, row.get('key'))
        if key in seen:
            continue
        seen.add(key)
        identity = (home, sid)
        condition = scope(row)
        state = states.setdefault(identity, {})
        if condition != state.get('scope') or ts - state.get('last_ts', ts) > MAX_AGE:
            state.clear()
            state['scope'] = condition
        state['last_ts'] = ts
        result[identity] = dict(active=False)
        if condition is None:
            state.clear()
            continue
        pool = pools[(home, condition)]
        while pool and ts - pool[0][1]['ts'] > MAX_AGE:
            pool.popleft()
        base = state.get('baseline')
        if base is None:
            own = [r for owner, r in pool if owner == sid and r['ts'] < ts][-MAX_BASELINE:]
            peers = [r for owner, r in pool if owner != sid and r['ts'] < ts][-MAX_BASELINE:]
            reference = own if len(own) >= MIN_BASELINE else peers
            if len(reference) >= MIN_BASELINE:
                base = baseline(reference)
                base['baseline_source'] = 'session' if reference is own else 'peers'
        speed = output_speed(row)
        slow = base is not None and 0 <= speed <= base['threshold'] and base['threshold'] > 0
        if state.get('active'):
            recovered = speed >= base['baseline_speed'] * .8
            state['recovery'] = state.get('recovery', []) + [row] if recovered else []
            state['recent'].append(row)
            state['recent'] = state['recent'][-SLOW_CALLS:]
            if len(state['recovery']) >= RECOVERY_CALLS:
                # Recovered samples seed the next baseline; slow samples never do.
                for recovered_row in state['recovery']:
                    pool.append((sid, recovered_row))
                state = states[identity] = dict(scope=condition, last_ts=ts)
                continue
        elif slow:
            state['baseline'] = base
            state.setdefault('recent', []).append(row)
            if len(state['recent']) >= SLOW_CALLS:
                state['active'] = True
                state['incident_id'] = state['recent'][0]['key']
        else:
            state.pop('baseline', None)
            state.pop('recent', None)
            pool.append((sid, row))
        if state.get('active'):
            recent = state['recent']
            average = output_speed_summary(recent)['value']
            result[identity] = dict(active=True, **base,
                                    recent_speed=average, recent_count=len(recent),
                                    drop_percent=max(0, (1 - average / base['baseline_speed']) * 100),
                                    recent_keys=[r['key'] for r in recent],
                                    incident_id=state['incident_id'], call_id=row['key'],
                                    model=condition[0], effort=condition[1], tier=condition[2])
    return result


class _Event:
    __slots__ = ('session', 'uid', 'order', 'fingerprint', 'condition', 'row')

    def __init__(self, session, uid, order, fingerprint, condition, row):
        self.session, self.uid, self.order = session, uid, order
        self.fingerprint, self.condition, self.row = fingerprint, condition, row

    def get(self, key, default=None):
        return self.row.get(key, default)

    def __getitem__(self, key):
        return self.row[key]


_Step = namedtuple('_Step', 'event reset')


class _ScopeReplay:
    """One peer pool; retain only its bounded baseline and current session state."""
    def __init__(self, condition):
        self.condition = condition
        self.pool = deque(maxlen=MAX_BASELINE * 4)
        self.states = {}
        self.results = {}
        self.last_order = None

    def advance(self, steps):
        for event, reset in steps:
            ts, home, sid, _ = event.order
            row = event
            identity = event.session
            state = self.states.setdefault(identity, {})
            if reset:
                state.clear()
            self.results[identity] = dict(active=False)
            while self.pool and ts - self.pool[0][1]['ts'] > MAX_AGE:
                self.pool.popleft()
            base = state.get('baseline')
            if base is None:
                own = [r for owner, r in self.pool if owner == sid and r['ts'] < ts][-MAX_BASELINE:]
                peers = [r for owner, r in self.pool if owner != sid and r['ts'] < ts][-MAX_BASELINE:]
                reference = own if len(own) >= MIN_BASELINE else peers
                if len(reference) >= MIN_BASELINE:
                    base = baseline(reference)
                    base['baseline_source'] = 'session' if reference is own else 'peers'
            speed = output_speed(row)
            slow = base is not None and 0 <= speed <= base['threshold'] and base['threshold'] > 0
            if state.get('active'):
                recovered = speed >= base['baseline_speed'] * .8
                state['recovery'] = state.get('recovery', []) + [row] if recovered else []
                state['recent'].append(row)
                state['recent'] = state['recent'][-SLOW_CALLS:]
                if len(state['recovery']) >= RECOVERY_CALLS:
                    for recovered_row in state['recovery']:
                        self.pool.append((sid, recovered_row))
                    self.states[identity] = {}
                    self.last_order = event.order
                    continue
            elif slow:
                state['baseline'] = base
                state.setdefault('recent', []).append(row)
                if len(state['recent']) >= SLOW_CALLS:
                    state['active'] = True
                    state['incident_id'] = state['recent'][0]['key']
            else:
                state.pop('baseline', None)
                state.pop('recent', None)
                self.pool.append((sid, row))
            if state.get('active'):
                recent = state['recent']
                average = output_speed_summary(recent)['value']
                self.results[identity] = dict(active=True, **base,
                    recent_speed=average, recent_count=len(recent),
                    drop_percent=max(0, (1 - average / base['baseline_speed']) * 100),
                    recent_keys=[r['key'] for r in recent],
                    incident_id=state['incident_id'], call_id=row['key'],
                    model=self.condition[0], effort=self.condition[1],
                    tier=self.condition[2])
            self.last_order = event.order


class SpeedHealth:
    """Advance appended evidence and replay only corrected peer scopes.

    Current input owns the index lifetime: one fingerprint/event per input row,
    no copied histories, replay checkpoints, queued revisions or durable state.
    Each scope keeps at most 120 baseline rows and each session keeps one live
    streak. Removed input releases its index entries and empty scopes. Above
    80,000 input rows or 4,096 peer scopes, release this index and use the
    complete classifier. Limits bound counts; the byte allowance is provisional.
    """
    def __init__(self):
        self.signature = None
        self.results = {}
        self.sessions = {}
        self.calls = {}
        self.chosen = {}
        self.scope_counts = {}
        self.by_session = {}
        self.members = {}
        self.session_members = {}
        self.latest = {}
        self.replays = {}
        self.metrics = {}

    @staticmethod
    def _same(left, right):
        return (left.event.order == right.event.order and left.reset == right.reset
                and left.event.fingerprint == right.event.fingerprint)

    def _fallback(self, engine, signature, row_count):
        self.__init__()
        self.results = classify(state['prepared'] for state in engine.sessions.values())
        self.signature = signature
        self.metrics = dict(scanned_rows=row_count, processed_rows=row_count,
            replayed_scopes=0, appended_rows=0, retained_rows=0, baseline_rows=0,
            live_states=0, capacity_fallback=True)
        return self.results

    def collect(self, engine):
        # Display/catalog order does not change the classifier's time-ordered
        # evidence. Partial source restoration can reorder unchanged owners.
        signature = frozenset((key, state.get('content_revision',state['revision'])) for key, state in engine.sessions.items())
        self.metrics = dict(scanned_rows=0, processed_rows=0, replayed_scopes=0, appended_rows=0,
            **{key: self.metrics[key] for key in ('retained_rows', 'baseline_rows', 'live_states', 'capacity_fallback')
               if key in self.metrics})
        if signature == self.signature:
            return self.results
        row_count = sum(len(state['prepared'].get('history', ())) for state in engine.sessions.values())
        if row_count > MAX_TRACKED_CALLS:
            return self._fallback(engine, signature, row_count)
        touched = set()
        conditions = {bucket[1]: bucket[1] for bucket in self.members}
        for key in self.sessions.keys() - engine.sessions.keys():
            _, entries = self.sessions.pop(key)
            for event in entries:
                self.calls[event.uid].remove(event)
                touched.add(event.uid)
        for key, state in engine.sessions.items():
            revision=state.get('content_revision',state['revision'])
            previous = self.sessions.get(key)
            if previous and previous[0] == revision:
                continue
            old = previous[1] if previous else ()
            entries = []
            for ordinal, row in enumerate(state['prepared'].get('history', ())):
                self.metrics['scanned_rows'] += 1
                condition = scope(row)
                if condition is not None:
                    condition = conditions.setdefault(condition, condition)
                # These are the classifier's observable inputs, including the
                # weighted speed's raw numerator and denominator. Invalid calls
                # still keep identity/time so they reset streaks and deduplicate.
                fingerprint = (row.get('key'), row['ts'], condition,
                    output_speed(row), row.get('output'), row.get('completion_latency_ms'))
                if ordinal < len(old) and old[ordinal].fingerprint == fingerprint:
                    event = old[ordinal]
                    # Pools borrow this event too. Refresh the authoritative
                    # reference without keeping a prior pricing/metadata row.
                    event.row = row
                else:
                    event = _Event(key, call_identity(*key, row.get('key')),
                        (row['ts'], *key, ordinal), fingerprint, condition, row)
                entries.append(event)
            for event in old:
                self.calls[event.uid].remove(event)
                touched.add(event.uid)
            for event in entries:
                self.calls.setdefault(event.uid, []).append(event)
                touched.add(event.uid)
            self.sessions[key] = (revision, entries)
        affected = set()
        changes = []
        for uid in touched:
            candidates = self.calls[uid]
            old = self.chosen.get(uid)
            new = min(candidates, key=lambda event: event.order) if candidates else None
            if old is new:
                continue
            changes.append((uid, old, new))
        # Remove departing owners first so replacing conditions at capacity
        # does not temporarily count both the old and new scope populations.
        for uid, old, new in changes:
            if old:
                self.by_session[old.session].pop(uid)
                affected.add(old.session)
                if old.condition is not None:
                    bucket = (old.session[0], old.condition)
                    self.scope_counts[bucket] -= 1
                    if not self.scope_counts[bucket]:
                        self.scope_counts.pop(bucket)
        for uid, old, new in changes:
            if new:
                self.chosen[uid] = new
                self.by_session.setdefault(new.session, {})[uid] = new
                affected.add(new.session)
                if new.condition is not None:
                    bucket = (new.session[0], new.condition)
                    self.scope_counts[bucket] = self.scope_counts.get(bucket, 0) + 1
                    if len(self.scope_counts) > MAX_TRACKED_SCOPES:
                        # No new peer-pool objects or replay work were created.
                        return self._fallback(engine, signature, row_count)
            else:
                self.chosen.pop(uid, None)
                self.calls.pop(uid)
        pending = {}
        for key in affected:
            events = sorted(self.by_session.get(key, {}).values(), key=lambda event: event.order)
            grouped = defaultdict(list)
            prior = None
            for event in events:
                if event.condition is not None:
                    reset = prior is None or prior.condition != event.condition or event.order[0] - prior.order[0] > MAX_AGE
                    grouped[(key[0], event.condition)].append(_Step(event, reset))
                prior = event
            old_groups = self.session_members.get(key, {})
            for bucket in old_groups.keys() | grouped.keys():
                old, new = old_groups.get(bucket, ()), grouped.get(bucket, ())
                prefix = len(old) <= len(new) and all(self._same(a, b) for a, b in zip(old, new))
                if prefix and len(old) == len(new):
                    grouped[bucket] = old
                    continue
                _, added = pending.setdefault(bucket, [False, []])
                if prefix:
                    added.extend(new[len(old):])
                else:
                    pending[bucket][0] = True
                members = self.members.setdefault(bucket, {})
                if new:
                    members[key] = new
                else:
                    members.pop(key, None)
            self.session_members[key] = grouped
            previous_bucket = self.latest.get(key)
            bucket = (key[0], events[-1].condition) if events and events[-1].condition is not None else None
            self.latest[key] = bucket
            if previous_bucket != bucket and previous_bucket in self.replays:
                self.replays[previous_bucket].states.pop(key, None)
                self.replays[previous_bucket].results.pop(key, None)
        for bucket, (rebuild, added) in pending.items():
            members = self.members[bucket]
            if not members:
                self.members.pop(bucket)
                self.replays.pop(bucket, None)
                continue
            replay = self.replays.get(bucket)
            steps = sorted(added, key=lambda step: step.event.order)
            if (rebuild or replay is None or steps and replay.last_order is not None
                    and steps[0].event.order <= replay.last_order):
                steps = sorted((step for records in members.values() for step in records), key=lambda step: step.event.order)
                replay = _ScopeReplay(bucket[1])
                self.replays[bucket] = replay
                self.metrics['replayed_scopes'] += 1
            else:
                self.metrics['appended_rows'] += len(steps)
            replay.advance(steps)
            self.metrics['processed_rows'] += len(steps)
            for key in tuple(replay.states):
                if self.latest.get(key) != bucket:
                    replay.states.pop(key)
                    replay.results.pop(key, None)
        for key in self.by_session.keys() - engine.sessions.keys():
            self.by_session.pop(key)
            self.session_members.pop(key, None)
            self.latest.pop(key, None)
        self.results = {key: self.replays[bucket].results.get(key, dict(active=False)) if bucket else dict(active=False)
                        for key in engine.sessions for bucket in (self.latest.get(key),)}
        self.signature = signature
        self.metrics.update(retained_rows=sum(len(entries) for _, entries in self.sessions.values()),
                            baseline_rows=sum(len(replay.pool) for replay in self.replays.values()),
                            live_states=sum(len(replay.states) for replay in self.replays.values()),
                            capacity_fallback=False)
        return self.results
