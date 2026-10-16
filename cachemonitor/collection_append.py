"""Live, bounded append proofs from the source index's validated mutation path.

This does not replace Session.view, evidence enrichment, or whole-body encoding.
It prevents a selected consumer from reconstructing an already-proven prefix.
No proof survives a producer restart or an unavailable published predecessor.
"""
from __future__ import annotations

import hashlib
import json
import math


FORMAT = 1
TAIL_ROWS = 128
RETAINED_BYTES = 16 * 1024 * 1024
RESPONSE_BYTES = 4 * 1024 * 1024
METADATA_BYTES = 64 * 1024
_MUTABLE = frozenset(('history', 'requests', 'groups', 'totals', 'remaining',
    'collection_complete', 'status', 'usage_revision', 'request_revision',
    'activity', 'running', 'request_state', 'turn_records', 'turn_states',
    'rate', 'weighted_rate', 'input', 'cached', 'cache_misses', 'cache_health',
    'cache_degradation', '_prepared'))
_METADATA_EXCLUDE = frozenset(('history', 'requests', 'groups', 'turn_records',
    'turn_states', 'cache_misses', 'cache_health', 'cache_degradation', '_prepared'))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def context_digest(source):
    """Stable source context; writer separately verifies its own dependencies."""
    from .usage_collection import _catalog_weight
    if source.get('parent_thread_id'):
        return None  # Parent-mode inheritance has no source append proof yet.
    value = {key: item for key, item in source.items() if key not in _MUTABLE}
    try:
        _catalog_weight(value, METADATA_BYTES)
        return digest(value)
    except (TypeError, ValueError):
        return None


class AppendBudget:
    """One allowance shared by pending and published immutable tail packets."""
    def __init__(self, limit=RETAINED_BYTES):
        self.limit, self.used = limit, 0

    def reserve(self, value):
        from .usage_collection import _catalog_weight
        try:charge = _catalog_weight(value, RESPONSE_BYTES) + 4096
        except ValueError:return None
        if self.used + charge > self.limit:return None
        self.used += charge
        return AppendReservation(self, charge)

    def packet(self, producer, key, prior_token, token, value):
        from .usage_collection import _catalog_weight
        try:
            _catalog_weight(value, RESPONSE_BYTES)
            payload = encoded(value)
        except (TypeError, ValueError):
            return None
        # Reserve room for the selected endpoint's envelope. The conservative
        # retained charge includes both producer and live-catalog bindings.
        charge = len(payload) + 4096 + 4 * sum(map(len, key))
        if len(payload) > RESPONSE_BYTES - METADATA_BYTES or self.used + charge > self.limit:
            return None
        self.used += charge
        return AppendPacket(self, charge, producer, key, prior_token, token, payload)


class AppendPacket:
    __slots__ = ('budget', 'charge', 'producer', 'key', 'prior_token', 'token', 'payload', 'digest')

    def __init__(self, budget, charge, producer, key, prior_token, token, payload):
        self.budget, self.charge = budget, charge
        self.producer, self.key = producer, key
        self.prior_token, self.token = prior_token, token
        self.payload = payload
        self.digest = hashlib.sha256(payload).hexdigest()

    def __del__(self):
        self.budget.used -= self.charge


class AppendReservation:
    __slots__ = ('budget', 'charge')

    def __init__(self, budget, charge):
        self.budget, self.charge = budget, charge

    def __del__(self):
        self.budget.used -= self.charge


def begin(index, key, session, append, additions):
    """Capture the last published predecessor before Session.consume mutates it."""
    prior = index.publication_rows.get(key)
    reader = index.model_evidence
    if (not append or not additions or not session.modern or prior is None
            or prior[0] is not getattr(session, '_cached_view', None)
            or len(prior[1]) != len(session.requests) or not prior[1]
            or reader.error or reader.catching_up):
        return None
    joined = getattr(session, '_evidence_join', None)
    if joined is None or joined[0] is not reader or joined[4] is not prior[1]:
        return None
    keys, turns = set(), set()
    for _, event in additions:
        payload = event.get('payload') or {}
        turn = payload.get('turn_id')
        if turn is not None:
            if not isinstance(turn, str):return None
            turns.add(turn)
        if event.get('type') == 'token_usage_record':
            name = payload.get('response_id')
            if (payload.get('thread_id', session.id) != session.id or not isinstance(name, str)
                    or not name or name in session.keys or name in keys):
                return None
            keys.add(name)
        if len(keys) > TAIL_ROWS or len(turns) > TAIL_ROWS:
            return None
    if not keys:
        return None
    old = {**prior[0], **prior[4]}
    context = context_digest(old)
    last = prior[1][-1]
    ts = last.get('ts')
    if (context is None or type(ts) not in (int, float) or not math.isfinite(ts)
            or not isinstance(last.get('key'), str)):
        return None
    maps = {field:{name:old[field][name] for name in turns if name in old[field]}
            for field in ('turn_records', 'turn_states') if isinstance(old.get(field), dict)}
    proof = dict(prior_token=prior[3], prior_raw_count=len(prior[1]),
        prior_frontier=[ts, last['key']], context_digest=context,
        evidence=(reader.identity, reader.generation, joined[2], joined[3]),
        source_token=getattr(session, '_replay_source', None), keys=tuple(keys), turns=tuple(turns),
        request_maps=maps)
    reservation = index.append_budget.reserve(proof)
    if reservation is None:return None
    return dict(proof, session=session, reader=reader, reservation=reservation)


def complete(index, key, origin, source, token):
    """Issue only a contiguous, enriched tail with unchanged prefix context."""
    from .usage_collection import _catalog_weight
    if origin is None or origin['session'] is not index.monitor.sessions.get(key):return None
    session, reader = origin['session'], index.model_evidence
    tail_keys, touched_turns = set(origin['keys']), set(origin['turns'])
    identity, generation, join_epoch, revision = origin['evidence']
    if (reader is not origin['reader'] or reader.identity != identity or reader.generation != generation
            or reader.join_epoch != join_epoch or reader.error or reader.catching_up
            or revision not in (reader.revision, reader.previous_revision)
            or key in index.rebuild_required or key in index.rebuild_jobs):
        return None
    if context_digest(source) != origin['context_digest']:return None
    # No prefix key copy: the Session already owns this membership set. Only the
    # bounded observation batch and bounded certified tail need inspection.
    from .model_evidence import home_key
    changed = reader.changed_responses.get(home_key(key[0]), ())
    if revision != reader.revision and any(name in session.keys and name not in tail_keys for name in changed):return None
    old_source, new_source = origin['source_token'], getattr(session, '_replay_source', None)
    if (not isinstance(old_source, tuple) or not isinstance(new_source, tuple)
            or len(old_source) != 3 or len(new_source) != 3
            or old_source[:2] != new_source[:2] or old_source[2] >= new_source[2]):return None
    history = source.get('history')
    count = origin['prior_raw_count']
    if not isinstance(history, list) or len(history) != count + len(tail_keys):return None
    tail = history[count:]
    if not 0 < len(tail) <= TAIL_ROWS:return None
    previous_ts = origin['prior_frontier'][0]
    actual_keys = set()
    for row in tail:
        name, ts = row.get('key'), row.get('ts')
        if (not isinstance(name, str) or name in actual_keys or type(ts) not in (int, float)
                or not math.isfinite(ts) or ts <= previous_ts):return None
        actual_keys.add(name);previous_ts = ts
    if actual_keys != tail_keys:return None
    metadata = {name:value for name,value in source.items() if name not in _METADATA_EXCLUDE}
    try:
        _catalog_weight(tail, RESPONSE_BYTES)
        _catalog_weight(metadata, METADATA_BYTES)
    except ValueError:return None
    requests, turns = dict(upserts=[], removed=[]), dict(upserts=[], removed=[])
    maps = origin['request_maps']
    for field, target in (('turn_records', requests), ('turn_states', turns)):
        before, after = maps.get(field, {}), source.get(field, {})
        if not isinstance(before, dict) or not isinstance(after, dict):return None
        for ordinal, (name, value) in enumerate(after.items()):
            if name in touched_turns and (name not in before or before[name] != value):
                target['upserts'].append([name, ordinal, value] if field == 'turn_records' else [name, value])
        target['removed'] = [name for name in origin['turns'] if name in before and name not in after]
    value = dict(format=FORMAT, owner=list(key), prior_token=origin['prior_token'], token=token,
        prior_raw_count=count, raw_count=len(history), prior_frontier=origin['prior_frontier'],
        tail=tail, tail_digest=digest(tail), context_digest=origin['context_digest'],
        evidence=dict(prior_revision=revision, revision=reader.revision, join_epoch=join_epoch,
                      generation=generation),
        prior_source_token=old_source, source_token=new_source, metadata=metadata,
        request_maps=[name for name in ('turn_records', 'turn_states') if name in source],
        requests=requests, turns=turns)
    return index.append_budget.packet(index.publication_owner, key, origin['prior_token'], token, value)
