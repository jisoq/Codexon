"""Evidence attached to the shared cache classifier, never a second alarm engine."""
from .core import token_number

TTL_SECONDS = 1800


def observations(rows):
    result = []
    previous = None
    for row in sorted(rows, key=lambda r: r['ts']):
        if row.get('purpose') == 'maintenance':
            continue
        changes = []
        estimate = None
        if previous is None:
            changes.append('session_start')
        else:
            for field in ('model', 'effort', 'service_tier', 'cache_policy'):
                if previous.get(field) is not None and row.get(field) is not None and previous[field] != row[field]:
                    changes.append(field + '_changed')
            if row['ts'] - previous['ts'] >= TTL_SECONDS:
                changes.append('idle_over_design_lifetime')
            if row.get('compaction_epoch') != previous.get('compaction_epoch'):
                changes.append('compaction')
            i, c, old = (token_number(v) for v in (row.get('input'), row.get('cached'), previous.get('cached')))
            if not changes and all(v is not None for v in (i, c, old)) and c <= i and not row.get('input_conflict'):
                # A comparable-call scenario, not measured avoidable loss or a write observation.
                estimate = max(0, min(old, i) - c)
        result.append(dict(key=row.get('key'),ts=row['ts'], changes=changes, cause_confirmed=False,
                           read=row.get('cached'), written=row.get('written'),
                           ordinary_input=row.get('ordinary_input'), observed_cost=row.get('cost'),
                           reuse_shortfall_scenario=estimate, avoidable_cost=None))
        previous = row
    return result
