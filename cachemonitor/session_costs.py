"""Display-only cost rollups for confirmed local task ancestry."""

from collections import defaultdict


def own_costs(rows):
    """Summarize already deduplicated, priced calls by their owning session."""
    totals = defaultdict(lambda: {'cost': 0.0, 'calls': 0, 'priced': 0,
                                  'cache_input': 0, 'cache_read': 0,
                                  'latest_ts': None, 'gap': False})
    for row in rows:
        value = totals[(row['home'], row['sid'])]
        value['calls'] += 1
        if row.get('cost') is not None:
            value['cost'] += row['cost']
            value['priced'] += 1
        inp, cached = row.get('input'), row.get('cached')
        if type(inp) is int and type(cached) is int and inp > 0 and 0 <= cached <= inp:
            value['cache_input'] += inp
            value['cache_read'] += cached
        stamp = row.get('ts')
        if stamp is not None:
            value['latest_ts'] = max(value['latest_ts'] or stamp, stamp)
    return dict(totals)


def session_costs(sessions, own):
    """Return own and descendant costs without changing global call totals.

    Only parent_thread_id from collected session metadata is accepted. Both ends
    must exist in the selected scope and belong to the same Codex home.
    """
    indexed = {(session['home'], session['id']): session for session in sessions}
    parents = {}
    for key, session in indexed.items():
        parent_id = session.get('parent_thread_id')
        parent = (key[0], parent_id)
        if isinstance(parent_id, str) and parent != key and parent in indexed:
            parents[key] = parent

    def ancestors(key):
        seen = {key}
        result = []
        while key in parents:
            key = parents[key]
            if key in seen:
                return []  # Corrupt cyclic lineage cannot establish ownership.
            seen.add(key)
            result.append(key)
        return result

    result = {}
    for key in indexed:
        source = own.get(key) or {}
        calls = source.get('calls', 0)
        priced = source.get('priced', 0)
        cost = source.get('cost') or 0.0
        result[key] = {'own_cost': cost if priced else 0.0 if not calls else None,
                       'own_calls': calls, 'own_priced': priced,
                       'child_cost_sum': 0.0, 'child_calls': 0,
                       'child_priced': 0, 'descendants': 0,
                       'members': [key],
                       'cache_input': source.get('cache_input', 0),
                       'cache_read': source.get('cache_read', 0),
                       'latest_ts': source.get('latest_ts'),
                       'gap': bool(source.get('gap'))}

    for key in indexed:
        source = own.get(key) or {}
        for parent in ancestors(key):
            target = result[parent]
            target['descendants'] += 1
            target['members'].append(key)
            target['child_cost_sum'] += source.get('cost') or 0.0
            target['child_calls'] += source.get('calls', 0)
            target['child_priced'] += source.get('priced', 0)
            target['cache_input'] += source.get('cache_input', 0)
            target['cache_read'] += source.get('cache_read', 0)
            stamp = source.get('latest_ts')
            if stamp is not None:
                target['latest_ts'] = max(target['latest_ts'] or stamp, stamp)
            target['gap'] |= bool(source.get('gap'))

    for value in result.values():
        calls = value['own_calls'] + value['child_calls']
        priced = value['own_priced'] + value['child_priced']
        value['cost'] = ((value['own_cost'] or 0.0) + value['child_cost_sum']) if priced else None
        value['child_cost'] = (value['child_cost_sum'] if value['child_priced'] else
                               0.0 if not value['child_calls'] else None)
        value['calls'] = calls
        value['priced'] = priced
        value['partial'] = priced < calls or value['gap']
        value['cache_ratio'] = (100 * value['cache_read'] / value['cache_input']
                                if value['cache_input'] else None)
        del value['child_cost_sum']
    return result
