"""Conservative, call-scoped speed alerts from completed historical evidence.

No wall-clock polling, guessed throughput, or persistent writes. Replay in time
order keeps peer baselines causal and makes corrected records deterministic.
"""
from collections import defaultdict, deque, namedtuple
from statistics import median
from dataclasses import dataclass
from heapq import merge
from math import isfinite
from sys import getsizeof
from weakref import ref

from .analytics import INPUT_BANDS, call_identity, output_speed, output_speed_summary
from .core import token_number
from .pricing import request_tier

MIN_BASELINE = 8
MAX_BASELINE = 30
MAX_AGE = 7 * 86400
SLOW_CALLS = 3
RECOVERY_CALLS = 2
# Bound retained classifier metadata independently of total prepared history.
MAX_TRACKED_SCOPES = 4096
MAX_RETAINED_SAMPLES = 80000
MAX_METADATA_BYTES = 64 * 1024 * 1024
# Unusual identities use the exact compatibility path, outside this allowance.
MAX_TEXT_CHARS = 32768


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


class _Unsupported(Exception):
    pass


class _Capacity(Exception):
    pass


@dataclass(slots=True)
class _Proof:
    revision: object
    length: int
    # Condition -> (row count, largest key size). No per-call membership index.
    buckets: dict
    condition: object
    timestamp: object


class _Sample(dict):
    """Only the scalar fields consumed by the established scope reducer."""
    __slots__ = ('session', 'order', 'condition')

    def __init__(self, owner, ordinal, row, condition):
        super().__init__(key=row['key'], ts=row['ts'], output=row['output'],
                         completion_latency_ms=row['completion_latency_ms'],
                         response_status='completed')
        self.session = owner
        self.order = (row['ts'], *owner, ordinal)
        self.condition = condition


def _text(value):
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
        raise _Unsupported('non-string or oversized prepared identity')
    return getsizeof(value)


def _condition_bytes(condition):
    return getsizeof(condition) + sum(_text(v) for v in condition[:4]) + 96


def _proof_bytes(owner, proof):
    return (1024 + sum(_text(v) for v in owner)
            + sum(256 + _condition_bytes(c) for c in proof.buckets))


@dataclass(slots=True)
class _OwnerInput:
    revision: object
    length: int
    append_from: object


class _EngineInput:
    def __init__(self, engine):
        self.binding=engine
        self.owners={owner:_OwnerInput(state.get('content_revision',state['revision']),
            len(state['prepared'].get('history',())),state.get('speed_append_from'))
            for owner,state in engine.sessions.items()}

    def rows(self,owner,start):
        history=self.binding.sessions[owner]['prepared'].get('history',())
        for ordinal in range(start,len(history)):
            yield ordinal,history[ordinal]

    def ordered(self,starts):
        def rows(owner,start):
            for ordinal,row in self.rows(owner,start):yield owner,ordinal,row
        return merge(*(rows(owner,start) for owner,start in starts.items()),
                     key=lambda item:(item[2]['ts'],*item[0],item[1]))

    def full_classify(self):
        return classify(state['prepared'] for state in self.binding.sessions.values())


class SpeedHealth:
    """One proof per canonical owner and bounded state per peer condition.

    The engine certifies reuse of an owner's prepared prefix. Ordered appends
    advance their scopes. Corrections replay current owners
    that intersect the affected scopes. Unchanged owner histories are not read
    during ordinary append. No history-sized calls, chosen or fingerprint map
    survives collect().
    """
    def __init__(self):
        self.signature = None
        self.engine_ref = None
        self.proofs = {}
        self.replays = {}
        self.results = {}
        self.metrics = {}
        self.retention = {}
        self.checkpoint_token = None
        self.checkpoint_from_token = None
        self._changes()

    def _changes(self):
        # The completed cohort alone is retained. Its predecessor token makes
        # skipping an unpublished delta detectable by the section writer.
        self.changed_owners = set()
        self.changed_scopes = set()
        self.removed_owners = set()
        self.removed_scopes = set()

    def _metrics(self):
        self.metrics = dict(scanned_rows=0, proof_rows=0, replay_rows=0,
            append_scan_rows=0, processed_rows=0, replayed_scopes=0,
            appended_rows=0, invalid_rows=0, retained_rows=0, baseline_rows=0,
            live_states=0, proof_owners=len(self.proofs), proof_memberships=0,
            estimated_metadata_bytes=0, capacity_fallback=False,
            fallback_reason=None)
        self.metrics.update(self.retention)

    def _fallback(self, source, signature, reason):
        # Exact compatibility path. Its transient full-classifier allocation
        # is the existing contract; it retains no incremental source index.
        self.proofs.clear()
        self.replays.clear()
        self.retention.clear()
        self.results = source.full_classify()
        count = sum(state.length for state in source.owners.values())
        self.metrics.update(scanned_rows=self.metrics['scanned_rows'] + count,
            processed_rows=count, retained_rows=0, baseline_rows=0,
            live_states=0, proof_owners=0, proof_memberships=0,
            estimated_metadata_bytes=0, capacity_fallback=True,
            fallback_reason=reason)
        self.signature = signature
        return self.results

    def _proof(self, owner, state, old, source):
        prefix = (old is not None
            and state.append_from == (old.revision, old.length)
            and state.length >= old.length)
        start = old.length if prefix else 0
        buckets, tail_min = (dict(old.buckets) if prefix else {}), {}
        previous_ts = old.timestamp if prefix else None
        condition = old.condition if prefix else None
        for ordinal, row in source.rows(owner,start):
            self.metrics['proof_rows'] += 1
            self.metrics['scanned_rows'] += 1
            key, timestamp = row.get('key'), row['ts']
            key_bytes = _text(key)
            if (not key or type(timestamp) not in (int, float)
                    or not isfinite(timestamp)
                    or previous_ts is not None and timestamp < previous_ts):
                raise _Unsupported('noncanonical prepared order or identity')
            previous_ts = timestamp
            condition = scope(row)
            if condition is not None:
                _condition_bytes(condition)
                count, largest = buckets.get(condition, (0, 0))
                buckets[condition] = (count + 1, max(largest, key_bytes))
                if len(buckets) > MAX_TRACKED_SCOPES:
                    raise _Capacity('scope count')
                tail_min.setdefault((owner[0], condition),
                                    (timestamp, *owner, ordinal))
        proof = _Proof(state.revision, state.length, buckets, condition, previous_ts)
        return proof, prefix, tail_min

    def _admit(self, proofs):
        """Conservative bytes and sample slots, before any reducer mutation.

        Shared strings and aliased samples are deliberately counted repeatedly.
        Frozen baseline IDs, recent/recovery lists, result dictionaries, owner
        proofs and temporary per-scope owner state are included. The estimate
        is a provisional allowance, not measured private process memory.
        """
        size = 1024 + sum(_proof_bytes(o, p) for o, p in proofs.items())
        pools = {}
        samples = pairs = 0
        for owner, proof in proofs.items():
            owner_bytes = sum(_text(v) for v in owner) + 56
            for condition, (count, key_bytes) in proof.buckets.items():
                pairs += 1
                # Five rows cover recent three plus two recovery references.
                current = min(5, count)
                sample_bytes = 512 + owner_bytes + key_bytes
                samples += current
                size += (2048 + 2 * MAX_BASELINE * key_bytes
                         + current * sample_bytes)
                bucket = (owner[0], condition)
                n, largest = pools.get(bucket, (0, 0))
                pools[bucket] = (n + count, max(largest, sample_bytes))
                if (pairs > MAX_RETAINED_SAMPLES
                        or len(pools) > MAX_TRACKED_SCOPES):
                    raise _Capacity('owner/scope membership or scope count')
        for count, sample_bytes in pools.values():
            retained = min(MAX_BASELINE * 4, count)
            samples += retained
            size += 2048 + retained * sample_bytes
        # Cohort change/removal sets retain identities only, never calls.
        size += 256 * (len(proofs) + len(self.proofs) + len(pools) + len(self.replays))
        if samples > MAX_RETAINED_SAMPLES or size > MAX_METADATA_BYTES:
            raise _Capacity('retained sample or metadata allowance')
        self.metrics.update(proof_owners=len(proofs), proof_memberships=pairs,
                            estimated_metadata_bytes=size)
        return pools

    def _steps(self, source, ranges, selected, metric):
        prior={owner:previous for owner,(_,previous) in ranges.items()}
        for owner,ordinal,row in source.ordered({owner:start for owner,(start,_) in ranges.items()}):
            self.metrics[metric] += 1
            self.metrics['scanned_rows'] += 1
            condition, timestamp = scope(row), row['ts']
            prior_condition, prior_ts = prior[owner]
            reset = (condition != prior_condition or prior_ts is None
                     or timestamp - prior_ts > MAX_AGE)
            prior[owner] = condition, timestamp
            if condition is None:
                self.metrics['invalid_rows'] += 1
            elif (owner[0], condition) in selected:
                yield _Step(_Sample(owner, ordinal, row, condition), reset)

    def _advance(self, steps, appended):
        for step in steps:
            event = step.event
            bucket=(event.session[0], event.condition)
            self.replays[bucket].advance((step,))
            self.changed_owners.add(event.session)
            self.changed_scopes.add(bucket)
            self.metrics['processed_rows'] += 1
            self.metrics['appended_rows'] += int(appended)

    def _finish(self, proofs):
        self.results = {}
        for bucket, replay in self.replays.items():
            for owner in tuple(replay.states):
                proof = proofs.get(owner)
                if proof is None or (owner[0], proof.condition) != bucket:
                    replay.states.pop(owner, None)
                    replay.results.pop(owner, None)
        for owner, proof in proofs.items():
            replay = self.replays.get((owner[0], proof.condition))
            self.results[owner] = (replay.results.get(owner, dict(active=False))
                if replay is not None else dict(active=False))
        self.metrics.update(
            baseline_rows=sum(len(r.pool) for r in self.replays.values()),
            live_states=sum(len(r.states) for r in self.replays.values()),
            retained_rows=sum(len(r.pool) + sum(len(s.get('recent', ()))
                + len(s.get('recovery', ())) for s in r.states.values())
                for r in self.replays.values()))
        self.retention = {key: self.metrics[key] for key in (
            'baseline_rows', 'live_states', 'retained_rows', 'proof_owners',
            'proof_memberships', 'estimated_metadata_bytes')}

    def collect(self, engine):
        # Only the actual canonical engine owns this preparation contract.
        # Similarly shaped fixtures retain the exact compatibility path.
        from .analysis_engine import AnalysisEngine
        source=_EngineInput(engine)
        signature=frozenset((owner,state.revision) for owner,state in source.owners.items())
        if not isinstance(engine, AnalysisEngine):
            self._metrics()
            self.engine_ref = None
            return self._fallback(source, signature, 'unproven input owner')
        return self._collect(source)

    def collect_prepared(self, source):
        from .prepared_speed import PreparedSpeedInput
        if not isinstance(source,PreparedSpeedInput):
            raise TypeError('prepared speed input requires the actual writer binding')
        try:
            with source.read_scope():
                same_binding=self.engine_ref is not None and self.engine_ref() is source.binding
                previous=self.checkpoint_token if same_binding else None
                signature=frozenset((owner,state.revision) for owner,state in source.owners.items())
                if previous == source.token and signature != self.signature:
                    raise ValueError('prepared metadata changed within a published token')
                if previous != source.token:
                    self._changes()
                result=self._collect(source)
                if previous != source.token:
                    self.checkpoint_from_token=previous
                    self.checkpoint_token=source.token
                return result
        except BaseException:
            # A cancelled database stream may have partly advanced a peer
            # reducer. Retry from a durable checkpoint or replay, never from
            # a partially consumed tail.
            self.__init__()
            raise

    def _collect(self, source):
        signature=frozenset((owner,state.revision) for owner,state in source.owners.items())
        if self.engine_ref is None or self.engine_ref() is not source.binding:
            self.__init__()
            self.engine_ref = ref(source.binding)
        fallback = self.metrics.get('capacity_fallback',False), self.metrics.get('fallback_reason')
        self._metrics()
        if signature == self.signature:
            self.metrics.update(capacity_fallback=fallback[0],fallback_reason=fallback[1])
            return self.results
        self._changes()
        proofs = {}
        tails = {}
        dirty = set()
        try:
            proof_size = 0
            for owner, state in source.owners.items():
                old = self.proofs.get(owner)
                revision = state.revision
                if old is not None and old.revision == revision:
                    proof = old
                else:
                    self.changed_owners.add(owner)
                    proof, prefix, minima = self._proof(owner, state, old, source)
                    if old is not None and not prefix:
                        dirty.update((owner[0], c) for c in old.buckets)
                        dirty.update((owner[0], c) for c in proof.buckets)
                    else:
                        start = old.length if old is not None else 0
                        if proof.length > start:
                            tails[owner] = (start,
                                (old.condition, old.timestamp) if old else (None, None))
                        for bucket, order in minima.items():
                            replay = self.replays.get(bucket)
                            if (replay is not None and replay.last_order is not None
                                    and order <= replay.last_order):
                                dirty.add(bucket)
                proofs[owner] = proof
                proof_size += _proof_bytes(owner, proof)
                if proof_size > MAX_METADATA_BYTES:
                    raise _Capacity('owner proof allowance')
            self.removed_owners = self.proofs.keys() - proofs.keys()
            for owner in self.removed_owners:
                dirty.update((owner[0], c) for c in self.proofs[owner].buckets)
            pools = self._admit(proofs)
        except (_Capacity, _Unsupported) as exc:
            return self._fallback(source, signature, str(exc))
        self.removed_scopes = self.replays.keys() - pools.keys()
        for bucket in self.removed_scopes:
            self.replays.pop(bucket)
        dirty.intersection_update(pools)
        for bucket in pools:
            if bucket in dirty or bucket not in self.replays:
                self.changed_scopes.add(bucket)
                self.replays[bucket] = _ScopeReplay(bucket[1])
        self.metrics['replayed_scopes'] = len(dirty)
        if dirty:
            ranges={owner:(0,(None,None)) for owner,proof in proofs.items()
                    if any((owner[0],c) in dirty for c in proof.buckets)}
            self._advance(self._steps(source,ranges,dirty,'replay_rows'),appended=False)
        append_scopes = pools.keys() - dirty
        self._advance(self._steps(source,tails,append_scopes,'append_scan_rows'),appended=True)
        self.proofs = proofs
        self._finish(proofs)
        self.signature = signature
        return self.results
