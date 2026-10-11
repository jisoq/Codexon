"""Compact request or call measurements for the selected history scope."""
from datetime import datetime
from math import isfinite

from .analytics import cache_rows


def finite_number(value, *, nonnegative=False):
    if type(value) not in (int, float):
        return None
    try:
        return value if isfinite(value) and (not nonnegative or value >= 0) else None
    except OverflowError:
        return None


def recorded_time(value):
    stamp = finite_number(value)
    # Missing collector timestamps are represented as zero.
    if stamp is None or stamp <= 0:
        return None
    try:
        datetime.fromtimestamp(stamp)
    except (ValueError, OSError, OverflowError):
        return None
    return stamp


def call_duration(row):
    elapsed = finite_number(row.get('completion_latency_ms'), nonnegative=True)
    valid = (row.get('timing_valid', row.get('response_status') == 'completed')
             and not row.get('observation_missing') and not row.get('model_conflict'))
    return elapsed / 1000 if valid and elapsed is not None else None


def project_timeline(rows, kind='calls'):
    """Keep the full table population, including untimed entries for the ordinal axis."""
    if kind not in ('calls', 'requests'):
        raise ValueError(f'Unsupported timeline kind: {kind}')
    projected = []
    total = 0
    omitted = 0
    for row in rows:
        total += 1
        stamp = recorded_time(row.get('ts'))
        omitted += stamp is None
        values = {key: finite_number(row.get(key), nonnegative=True)
                  for key in ('cost', 'input', 'output', 'output_speed')}
        if kind == 'requests':
            key = row.get('turn') or '__unlinked__'
            ordinal = row.get('ordinal')
            calls = row.get('responses', 0)
            total_calls = row.get('total_responses', calls)
            complete = bool(row.get('complete'))
            state = row.get('state') or ''
            duration = finite_number(row.get('duration'), nonnegative=True) if complete else None
            ratio = finite_number(row.get('rate'), nonnegative=True)
            if not values['input'] or ratio is not None and ratio > 100:
                ratio = None
            values['output_speed'] = None
        else:
            key = row.get('call_id') or row.get('key') or row.get('response_id')
            ordinal = row.get('session_ordinal')
            calls = total_calls = 1
            complete = row.get('response_status') == 'completed'
            state = row.get('response_status') or ''
            duration = call_duration(row)
            ratio = row['cached'] / row['input'] * 100 if values['input'] and cache_rows((row,)) else None
        projected.append(dict(
            home=row.get('home'), sid=row.get('sid'),
            key=key, turn=row.get('turn'), ordinal=ordinal, kind=kind,
            ts=stamp, model=row.get('model'), cache_ratio=ratio, duration=duration,
            calls=calls, total_calls=total_calls, complete=complete, state=state,
            partial=not complete or calls < total_calls, **values))
    projected.sort(key=lambda row: (row['ordinal'] is None, row['ordinal'] or 0,
                                   row['ts'] is None, row['ts'] or 0))
    return dict(rows=projected, total=total, omitted_timestamps=omitted)
