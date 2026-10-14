"""Reversible user exclusions over observed weekly quota boundaries."""
import math


def active_exclusions(report, account=None):
    account = report.get('account', '') if account is None else account
    return [r for r in report.get('exclusions', [])
            if r.get('restored_at') is None and r.get('account', '') == account
            and (not report.get('home') or r.get('home', report['home']) == report['home'])]


def exclusion_ranges(records):
    """Merge overlaps before subtracting, so shared edges count only once."""
    merged = []
    for row in sorted(records, key=lambda r: (r['start'], r['end'])):
        lo, hi = row['start'], row['end']
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(hi, merged[-1][1])
        else:
            merged.append([lo, hi])
    return merged


def retained_intervals(cycles, report, clip):
    """Subtract edges, keeping both surviving endpoint runs disconnected."""
    by_account = {}
    for cycle in cycles:
        account = cycle.get('account', '')
        if account not in by_account:
            by_account[account] = exclusion_ranges([*active_exclusions(report, account),
                *(r for r in report.get('cycle_clears',[]) if r['account']==account)])
        ranges = by_account[account]
        if not any(lo < cycle['end'] and hi > cycle['start'] for lo, hi in ranges):
            yield cycle
            continue
        points = cycle.get('endpoints', [])
        if not points:
            continue
        runs = [[points[0]]]
        for previous, point in zip(points, points[1:]):
            if any(lo < point[0] and hi > previous[0] for lo, hi in ranges):
                runs.append([point])
            else:
                runs[-1].append(point)
        for run in runs:
            if len(run) < 2:
                continue
            part = clip(cycle, run[0][0], run[-1][0])
            if part is not None:
                yield {**part, 'id': f"{cycle['id']}:kept:{run[0][0]}",
                       'user_exclusions_applied': True}


def validate_exclusion(report, start, end, reason=''):
    """Validate bounds against current evidence without rebuilding accounting."""
    from .quota_view import _history_rows

    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end) or start >= end):
        raise ValueError('종료 시각을 시작보다 뒤로 선택하세요.')
    if not isinstance(reason, str) or len(reason.strip()) > 200:
        raise ValueError('제외 사유는 200자 이내로 입력하세요.')
    account = report.get('account') or ''
    if not account:
        raise ValueError('계정을 확인한 뒤 구간을 제외하세요.')
    period=bounds=None;has_start=has_end=False
    for row in _history_rows(report,'weekly',labels=False):
        if row.get('account') != account:
            continue
        if bounds is None or row.get('reset_kind'):
            if has_start and has_end:
                period=bounds;break
            bounds=[row['at'],row['at']];has_start=has_end=False
        bounds[1]=row['at']
        has_start|=row['at']==start;has_end|=row['at']==end
    if period is None and has_start and has_end:period=bounds
    if period is None:
        raise ValueError('한 주기 안에서 기록된 시작과 종료 시각을 선택하세요.')
    existing = active_exclusions(report)
    if any(lo <= start and hi >= end for lo, hi in exclusion_ranges(existing)):
        raise ValueError('이미 제외한 구간입니다.')
    record = dict(home=report.get('home', ''), account=account, start=start, end=end,
                  reason=reason.strip().replace(chr(183), ''),
                  period_start=period[0], period_end=period[-1])
    return record


def preview_exclusion(report, start, end, reason=''):
    """Validate an observed range and calculate its effect without a write."""
    from .quota_cycles import quota_statistics

    record = validate_exclusion(report, start, end, reason)
    revised = {**report, 'exclusions': [*report.get('exclusions', []), record]}
    def summaries(source):
        prepared=source.get('view') if source is report else None
        if prepared:
            period=next(p for p in prepared['periods'] if p['start']==record['period_start'])
            return prepared['lifetime'],dict(cost=period['cost'],delta=period['delta'])
        return quota_statistics(source),quota_statistics(source,record['period_start'],record['period_end'])
    before,period_before=summaries(report)
    after,period_after=summaries(revised)
    fields = ('cost', 'delta', 'per_percent', 'calls', 'observed_calls', 'observed_priced_calls')
    removed = {key: before[key] - after[key]
               if before[key] is not None and after[key] is not None else None
               for key in ('cost', 'delta', 'calls')}
    return {**record, 'before': {key: before[key] for key in fields},
            'after': {key: after[key] for key in fields}, 'removed': removed,
            'period_before': period_before, 'period_after': period_after}
