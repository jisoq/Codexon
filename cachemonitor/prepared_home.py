"""Exact Home totals from published owner facts and bounded scalar range scans."""
from math import fsum, isfinite

from .analysis_engine import BoundedCache, retained_bytes
from .prepared_category import row_matches
from .prepared_store import CapacityExceeded, RebuildRequired
from .prepared_summary import _validate as validate_summary

FORMAT = ('home-owner-totals', 1)
FIELDS = ('cost', 'input', 'cached', 'output', 'reasoning')
CACHE_BYTES = 16 * 1024 * 1024


def build(owner, rows):
    """Called once while canonical prepared rows already belong to the writer.

    Home's per-owner arithmetic is deliberately +=, including cost. Its final
    cross-owner cost uses fsum. Overlay headline/valid-cache partials use other
    populations and arithmetic and cannot substitute for this small proof.
    """
    values = [0] * len(FIELDS)
    known = [0] * len(FIELDS)
    count = 0
    minimum = maximum = None
    for row in rows:
        count += 1
        stamp = row['ts']
        minimum = stamp if minimum is None else min(minimum, stamp)
        maximum = stamp if maximum is None else max(maximum, stamp)
        for index, field in enumerate(FIELDS):
            value = row.get(field)
            if value is not None:
                values[index] += value
                known[index] += 1
    return dict(format=FORMAT, owner=tuple(owner), fields=FIELDS, count=count,
        minimum=minimum, maximum=maximum, values=tuple(values),
        missing=tuple(count-number for number in known))


def proof(packet, owner):
    validate_summary(packet, owner)
    value = packet.get('summary', {}).get('home_totals')
    if not isinstance(value, dict) or value.get('format') != FORMAT or value.get('owner') != owner:
        raise RebuildRequired('prepared owner lacks the versioned Home totals proof')
    if value.get('fields') != FIELDS:
        raise RebuildRequired('prepared Home metric order is incompatible')
    count = value.get('count')
    values, missing = value.get('values'), value.get('missing')
    if (type(count) is not int or count < 0 or packet['own'].get('calls') != count or
            type(values) is not tuple or len(values) != len(FIELDS) or
            type(missing) is not tuple or len(missing) != len(FIELDS) or
            any(type(item) not in (int, float) for item in values) or
            any(type(item) is not int or not 0 <= item <= count for item in missing)):
        raise RebuildRequired('prepared Home totals have invalid counts or values')
    edges = (value.get('minimum'), value.get('maximum'))
    if (not count and edges != (None, None) or count and
            (any(type(item) not in (int,float) or not isfinite(item) for item in edges) or edges[0] > edges[1])):
        raise RebuildRequired('prepared Home totals have invalid canonical bounds')
    return value


def part(value):
    return value['count'], value['values'], tuple(value['count']-missing for missing in value['missing'])


def cache_for(engine):
    cache = getattr(engine, '_prepared_home_parts', None)
    if cache is None:
        cache = BoundedCache(entries=65536, rows=CACHE_BYTES)
        engine._prepared_home_parts = cache
    return cache


def project(engine, query):
    snapshot = engine.snapshot
    if snapshot is None:
        raise RuntimeError('prepared Home totals require a pinned read scope')
    cache = cache_for(engine)
    filters = tuple(query.get(name, '') for name in ('model', 'effort', 'service_tier'))
    fields = list(FIELDS)
    if filters[0]:fields.append('model')
    if filters[1]:fields.append('effort')
    if filters[2]:fields.extend(('service_tier', 'display_service_tier', 'mode_conflict', 'service_tier_source'))
    fields = tuple(fields)
    counts = sessions = admitted = 0
    parts = []
    work = dict(summary_reads=0, scalar_rows=0, persisted_owners=0, cached_ranges=0)
    for owner, state in engine._selected(query):
        snapshot._check()
        key = (owner, state.get('content_revision', state['revision']))
        cached = cache.get(key)
        if cached is None:
            value = proof(snapshot.derived(*owner, 'summary'), owner)
            work['summary_reads'] += 1
            cached = dict(proof=value, range=None, part=None)
        else:
            value = cached['proof']
        minimum, maximum, count = value['minimum'], value['maximum'], value['count']
        full = not count or query['start'] <= minimum and maximum < query['end']
        if full and not any(filters):
            result = part(value)
            work['persisted_owners'] += 1
        else:
            if not count or query['end'] <= minimum or query['start'] > maximum:
                edges = (None, None)
            elif full:
                edges = (minimum, maximum)
            else:
                edges = snapshot.call_bounds(owner,
                    start=query['start'] if isfinite(query['start']) else None,
                    end=query['end'] if isfinite(query['end']) else None)
            signature = (edges, filters)
            if cached['range'] == signature:
                result = cached['part']
                work['cached_ranges'] += 1
            else:
                if edges[0] is None:
                    result = (0, (0,) * len(FIELDS), (0,) * len(FIELDS))
                else:
                    values = [0] * len(FIELDS)
                    known = [0] * len(FIELDS)
                    selected = 0
                    for row in engine._pages(snapshot.projected_calls, owner, fields,
                            start=query['start'] if isfinite(query['start']) else None,
                            end=query['end'] if isfinite(query['end']) else None):
                        work['scalar_rows'] += 1
                        if not row_matches(row, query):continue
                        selected += 1
                        for index, field in enumerate(FIELDS):
                            metric = row.get(field)
                            if metric is not None:
                                values[index] += metric
                                known[index] += 1
                    result = selected, tuple(values), tuple(known)
                cached = dict(cached, range=signature, part=result)
        cache.put(key, cached, retained_bytes((key,cached), CACHE_BYTES) + 128)
        counts += result[0]
        sessions += bool(result[0])
        admitted += retained_bytes(result, snapshot.limits.page_objects_bytes) + 16
        if admitted > snapshot.limits.page_objects_bytes:
            raise CapacityExceeded('Home owner reductions exceed the query object budget')
        parts.append(result)
    totals = {'calls':dict(value=counts, known=counts, total=counts, partial=False)}
    for index, field in enumerate(FIELDS):
        known = sum(value[2][index] for value in parts)
        values = (value[1][index] for value in parts)
        value = (fsum(values) if field == 'cost' else sum(values)) if known or not counts else None
        totals[field] = dict(value=value, known=known, total=counts, partial=known != counts)
    engine.home_work = work
    return dict(key=engine.query_key(query), revision=engine.revision, lookup={},
        analysis=dict(response_count=counts, session_count=sessions), usage=dict(totals=totals))
