"""Conservative, call-scoped speed alerts from completed historical evidence.

No wall-clock polling, guessed throughput, or persistent writes. Replay in time
order keeps peer baselines causal and makes corrected records deterministic.
"""
from collections import defaultdict, deque
from statistics import median

from .analytics import INPUT_BANDS, call_identity, output_speed, output_speed_summary
from .core import token_number
from .pricing import request_tier

MIN_BASELINE = 8
MAX_BASELINE = 30
MAX_AGE = 7 * 86400
SLOW_CALLS = 3
RECOVERY_CALLS = 2


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


class SpeedHealth:
    """Recompute only on analysis revisions, never on hover or repaint."""
    def __init__(self):
        self.signature = None
        self.results = {}

    def collect(self, engine):
        signature = tuple((key, state['revision']) for key, state in engine.sessions.items())
        if signature != self.signature:
            self.results = classify(state['prepared'] for state in engine.sessions.values())
            self.signature = signature
        return self.results
