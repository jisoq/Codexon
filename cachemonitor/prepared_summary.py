"""Persist own overlay summaries without repeated latest-call projections.

Only the most recent twelve references are retained. Historical health evidence
is preserved verbatim and remains subject to the ordinary packet budget. Its
event/membership storage is a separate concern; this module never truncates it.
"""
from .analysis_records import compact_record
from .overlay_data import call_summary
from .prepared_store import CapacityExceeded, NotReady, RebuildRequired, _size


FORMAT = ('owner-summary-refs', 2)
MAX_RECENT = 12


def summary_for_storage(summary):
    """Replace recent/latest objects with exact call references, without mutation."""
    own = summary['own']
    owner = (own['home'], own['id'])
    recent = own['recent']
    if len(recent) > MAX_RECENT:
        raise ValueError('only an own summary with at most twelve recent calls is admitted')
    latest = recent[-1] if recent else {}
    if own['latest'] is not latest and own['latest'] != latest:
        raise ValueError('own latest call differs from the final recent call')
    refs = []
    for row in recent:
        if (row['home'], row['sid']) != owner:
            raise ValueError('persist an own summary before descendant rollup')
        refs.append((row['key'], row['ordinal'], row['ts'], row['id'],
                     row['cache_miss'], row['cache_degradation']))
    return dict(format=FORMAT, owner=owner,
                summary={key: value for key, value in summary.items() if key != 'own'},
                own={key: value for key, value in own.items() if key not in ('recent', 'latest')},
                recent=tuple(refs))


def _validate(packet, owner):
    if not isinstance(packet, dict) or packet.get('format') != FORMAT or packet.get('owner') != tuple(owner):
        raise RebuildRequired('unsupported own-summary references or owner identity')
    if not isinstance(packet.get('recent'), tuple) or len(packet['recent']) > MAX_RECENT:
        raise RebuildRequired('invalid own-summary recent-reference count')
    for ref in packet['recent']:
        if (not isinstance(ref, tuple) or len(ref) != 6 or type(ref[1]) is not int or ref[1] < 1
                or type(ref[4]) is not bool or type(ref[5]) is not bool):
            raise RebuildRequired('invalid own-summary call reference')
    if (packet.get('own', {}).get('home'), packet.get('own', {}).get('id')) != tuple(owner):
        raise RebuildRequired('own-summary metadata belongs to another owner')
    if not isinstance(packet['own'].get('model_breakdown'),list):
        raise RebuildRequired('own-summary model accounting is missing')


def recent_references(packet):
    """Expose ordering fields so descendant top-12 selection needs no call reads.

    The caller selects references first, then hydrates only the chosen twelve.
    A rolled-up row adds source_title from that member's stored own title after
    hydration, exactly as OverlaySummaries does today.
    """
    owner = packet.get('owner')
    if not isinstance(owner, tuple) or len(owner) != 2:
        raise RebuildRequired('own-summary owner reference is missing')
    _validate(packet, owner)
    return tuple(dict(home=owner[0], sid=owner[1], key=key, ordinal=ordinal, ts=ts,
                      id=display_id, cache_miss=miss, cache_degradation=degradation)
                 for key, ordinal, ts, display_id, miss, degradation in packet['recent'])


def hydrate_references(snapshot, refs):
    """Hydrate up to twelve selected references in the caller's pinned snapshot."""
    selected = []
    for ref in refs:
        if len(selected) >= MAX_RECENT:
            raise CapacityExceeded('select the twelve recent references before hydration')
        selected.append(ref)
    rows, size = [], 0
    for ref in selected:
        snapshot._check()
        row = snapshot.record(ref['home'], ref['sid'], ref['key'])
        if row is None:
            raise NotReady('a prepared summary references an absent call')
        if type(row.get('ts')) is not type(ref['ts']) or row['ts'] != ref['ts']:
            raise RebuildRequired('a prepared summary references different call facts')
        value = call_summary(compact_record(row), ref['ordinal'],
                             miss=ref['cache_miss'], degradation=ref['cache_degradation'])
        if type(value['id']) is not type(ref['id']) or value['id'] != ref['id']:
            raise RebuildRequired('a prepared summary references a different display identity')
        size += _size(value)
        if size > snapshot.limits.page_objects_bytes:
            raise CapacityExceeded('recent call summaries exceed the delivered object budget')
        rows.append(value)
    return rows


def hydrate_summary(snapshot, home, sid):
    """Load summary and calls from one visible generation, never current fallback."""
    packet = snapshot.derived(home, sid, 'summary')
    if packet is None:
        raise NotReady('the owner has no published summary')
    _validate(packet, (home, sid))
    recent = hydrate_references(snapshot, recent_references(packet))
    own = dict(packet['own'], recent=recent, latest=recent[-1] if recent else {})
    result = dict(packet['summary'], own=own)
    if _size(result) > snapshot.limits.page_objects_bytes:
        raise CapacityExceeded('own summary exceeds the delivered object budget')
    return result
