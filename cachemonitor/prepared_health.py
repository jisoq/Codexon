"""Small overlay heads and exact, demand-read health evidence sections.

Normal publication contains counts, the current incident's scalar facts and a
versioned reference. Event arrays live in bounded owner sections. Selected owner
detail may use at most the snapshot's object budget; capacity failures propagate
instead of truncating evidence. The store owns staging and persistent budgets.
"""
from dataclasses import replace
from hashlib import sha256

from .prepared_store import (Limits, CapacityExceeded, NotReady, RebuildRequired,
                             StaleGeneration, pack, unpack, _size)


FORMAT = ('owner-health-sections', 1)
FIELDS = ('cache_misses', 'cache_health', 'cache_degradation')
CHUNK_BYTES = 30 * 1024
DETAIL_BYTES = 32 * 1024 * 1024
DETAIL_LIMITS = replace(Limits(), packet_bytes=DETAIL_BYTES, packet_nodes=1_000_000)


def canonical_health_event(event):
    """Remove only summarize_session's three derived call-evidence projections."""
    return {key: value for key, value in event.items()
            if key not in ('baseline_calls', 'comparison_calls', 'recovery_calls')}


def _nodes(value):
    if type(value) is dict:
        return 1+sum(_nodes(key)+_nodes(item) for key,item in value.items())
    if type(value) in (tuple,list):
        return 1+sum(_nodes(item) for item in value)
    return 1


def _head(value):
    """Retain event scalars and fixed classifier segment, never memberships."""
    return {key: item for key, item in value.items()
            if not isinstance(item, (list, dict))
            and (not isinstance(item, tuple) or key == 'segment')}


def compact_owner_health(source):
    """Project raw or summarized owner health without walking event histories."""
    result = {}
    for field in FIELDS:
        if field not in source:
            continue
        value = source[field]
        if value is None:
            result[field] = None
            continue
        compact = {key: item for key, item in value.items()
                   if key not in ('events', 'incident')}
        if 'incident' in value:
            compact['incident'] = _head(value['incident']) if value['incident'] else value['incident']
        result[field] = compact
    return result


def split_summary(summary):
    """Return a small own summary and an iterator for Stage.sections(reconcile=True).

    Each event is stored once per owner by its exact encoded content. The same
    enriched event in health and degradation therefore shares its storage.
    Large events are split into 30 KiB chunks, below the ordinary packet limit.
    """
    own = summary['own']
    owner = (own['home'], own['id'])
    compact = dict(own, **compact_owner_health(own),
                   _health_ref=dict(format=FORMAT, owner=owner))

    def sections():
        seen = set()

        def blob(value):
            encoded = pack(value, DETAIL_LIMITS)
            if len(encoded) <= 2048:
                return dict(inline=value)
            digest = sha256(encoded).hexdigest()
            descriptor = dict(digest=digest, bytes=len(encoded),
                              chunks=(len(encoded)+CHUNK_BYTES-1)//CHUNK_BYTES)
            if digest not in seen:
                seen.add(digest)
                for offset in range(0, len(encoded), CHUNK_BYTES):
                    index = offset//CHUNK_BYTES
                    yield ((FORMAT, 'blob', digest), index, index, None,
                           encoded[offset:offset+CHUNK_BYTES])
            return descriptor

        for field in FIELDS:
            if field not in own:
                continue
            value = own[field]
            if value is None:
                yield (FORMAT, field, 0, None, dict(value=None))
                continue
            envelope = {key: item for key, item in value.items()
                        if key not in ('events', 'incident')}
            manifest = dict(value=envelope)
            if 'events' in value:
                events = value['events']
                manifest['events'] = dict(kind='tuple' if type(events) is tuple else 'list',
                                          count=len(events))
                for ordinal, event in enumerate(events):
                    descriptor = yield from blob(event)
                    index = dict(descriptor, head=_head(event))
                    yield ((FORMAT, field, 'events'), ordinal, ordinal, event.get('ts'), index)
                    if field == 'cache_health' and event.get('id') is not None:
                        yield ((FORMAT, 'event-id'), str(event['id']), ordinal, event.get('ts'), descriptor)
            if 'incident' in value:
                manifest['incident'] = ((yield from blob(value['incident']))
                                        if value['incident'] is not None else None)
            yield (FORMAT, field, 0, None, manifest)

    return dict(summary, own=compact), sections()


def _rows(snapshot, owner, section, *, since=None):
    after = None
    while True:
        page = snapshot.section(owner, section, after=after, since=since)
        yield from page.rows
        if page.complete:
            return
        after = page.after


def _read_blob(snapshot, owner, descriptor):
    if 'append_event' in descriptor:
        from .prepared_append import read_event
        return read_event(snapshot,owner,descriptor)
    if 'inline' in descriptor:
        return descriptor['inline']
    expected = descriptor['bytes']
    if type(expected) is not int or not 0 <= expected <= DETAIL_BYTES:
        raise RebuildRequired('invalid health detail byte count')
    chunks = []
    size = 0
    for ordinal, row in enumerate(_rows(snapshot, owner, (FORMAT, 'blob', descriptor['digest']))):
        value = row['value']
        if row['ordinal'] != ordinal or type(value) is not bytes or len(value) > CHUNK_BYTES:
            raise RebuildRequired('invalid health detail chunk')
        size += len(value)
        if size > expected:
            raise RebuildRequired('health detail exceeds its declared byte count')
        chunks.append(value)
    if len(chunks) != descriptor['chunks'] or size != expected:
        raise NotReady('health detail chunks are incomplete')
    encoded = b''.join(chunks)
    if sha256(encoded).hexdigest() != descriptor['digest']:
        raise RebuildRequired('health detail checksum differs')
    return unpack(encoded, DETAIL_LIMITS)


def _budget(snapshot, values):
    if _size(values) > snapshot.limits.page_objects_bytes:
        raise CapacityExceeded('selected health detail exceeds the delivered object budget')


def hydrate_health(snapshot, owner, event_id=None):
    """Read exact selected owner evidence or one event from the pinned generation."""
    owner = tuple(owner)
    if event_id is not None:
        row = snapshot.section_item(owner, (FORMAT, 'event-id'), str(event_id))
        if row is None:
            return None
        result = _read_blob(snapshot, owner, row['value'])
        _budget(snapshot, result)
        return result
    packet = snapshot.derived(*owner, 'summary')
    if packet is None:
        raise NotReady('the selected owner has no published health summary')
    result = {}
    for field in FIELDS:
        if field not in packet['own']:
            continue
        row = snapshot.section_item(owner, FORMAT, field)
        if row is None:
            raise NotReady('a declared health evidence section is missing')
        manifest = row['value']
        result[field] = value = manifest['value']
        if value is None:
            continue
        if 'events' in manifest:
            events = []
            event_size = 0
            for ordinal, item in enumerate(_rows(snapshot, owner, (FORMAT, field, 'events'))):
                if item['ordinal'] != ordinal:
                    raise RebuildRequired('health event ordering is incomplete')
                event = _read_blob(snapshot, owner, item['value'])
                event_size += _size(event)
                if event_size > snapshot.limits.page_objects_bytes:
                    raise CapacityExceeded('selected health events exceed the delivered object budget')
                events.append(event)
            if len(events) != manifest['events']['count']:
                raise NotReady('health event inventory is incomplete')
            value['events'] = tuple(events) if manifest['events']['kind'] == 'tuple' else events
        if 'incident' in manifest:
            value['incident'] = (_read_blob(snapshot, owner, manifest['incident'])
                                 if manifest['incident'] is not None else None)
        _budget(snapshot, result)
    return result


def notification_health(snapshot, owner, *, since):
    """Return current incident plus only zero-read facts beyond a consumer floor."""
    packet = snapshot.derived(*owner, 'summary')
    if packet is None:
        raise NotReady('the owner has no published health head')
    head = compact_owner_health(packet['own'])
    misses = dict(head.get('cache_misses') or {})
    events = []
    event_size = 0
    for row in _rows(snapshot, owner, (FORMAT, 'cache_misses', 'events'), since=since):
        event = _read_blob(snapshot, owner, row['value'])
        if event['ts'] > since:
            event_size += _size(event)
            if event_size > snapshot.limits.page_objects_bytes:
                raise CapacityExceeded('fresh cache misses exceed the delivered object budget')
            events.append(event)
    misses['events'] = events
    return dict(home=owner[0], id=owner[1], title=packet['own']['title'],
                cache_misses=misses, cache_health=head.get('cache_health'))


def notification_health_pages(snapshot, owner, *, since):
    """Yield packet-sized ordered misses, applying the current incident only last.

    ConfirmedNotifications updates its seen-floor before deciding on the current
    incident. Earlier batches therefore omit cache_health entirely.
    """
    packet = snapshot.derived(*owner, 'summary')
    if packet is None:
        raise NotReady('the owner has no published health head')
    head = compact_owner_health(packet['own'])
    base = dict(home=owner[0], id=owner[1], title=packet['own']['title'],
                cache_misses=dict(head.get('cache_misses') or {}, events=[]),
                cache_health=head.get('cache_health'))
    base_bytes = len(pack(base, snapshot.limits))
    base_nodes = _nodes(base)
    batch = []
    size = base_bytes
    nodes = base_nodes

    def payload(last):
        value = dict(base, cache_misses=dict(base['cache_misses'], events=batch))
        if not last:
            value.pop('cache_health')
        pack(value, snapshot.limits)
        return value

    for row in _rows(snapshot, owner, (FORMAT, 'cache_misses', 'events'), since=since):
        event = _read_blob(snapshot, owner, row['value'])
        if event['ts'] <= since:
            continue
        amount = len(pack(event, snapshot.limits))+1
        event_nodes = _nodes(event)
        if batch and (size+amount > snapshot.limits.packet_bytes or
                      nodes+event_nodes > snapshot.limits.packet_nodes):
            yield payload(False)
            batch = []
            size = base_bytes
            nodes = base_nodes
        if size+amount > snapshot.limits.packet_bytes or nodes+event_nodes > snapshot.limits.packet_nodes:
            raise CapacityExceeded('one fresh cache miss exceeds the notification packet budget')
        batch.append(event)
        size += amount
        nodes += event_nodes
    yield payload(True)


def check_reference(snapshot, summary):
    ref = summary.get('_health_ref')
    owner = (summary.get('home'), summary.get('id'))
    if not isinstance(ref, dict) or ref.get('format') != FORMAT or ref.get('owner') != owner:
        raise RebuildRequired('unsupported health detail reference')
    token = summary.get('_prepared_token')
    current = snapshot.publication_token
    if token is not None and tuple(token) != (current.instance, current.generation):
        raise StaleGeneration('health detail no longer matches the displayed generation')
    return owner
