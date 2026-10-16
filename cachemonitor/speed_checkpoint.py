"""Bounded, optional SpeedHealth checkpoint codec. Storage belongs to its writer.

The binding is a committed prepared-store token, the writer's exact encoded
source identity, preparation format, and durable owner dependency hashes.
No prepared history is iterated by this module, and no Python object is
unpickled. Evidence maps and sampled numeric types survive JSON round trips.
"""
from dataclasses import dataclass
import hashlib
import json
from math import isfinite
import struct
import sys
from weakref import ref

from . import speed_health as speed
from .prepared_speed import PreparedSpeedInput


FORMAT = 2
CLASSIFIER_FORMAT = 'speed-health-1'
CHUNK_BYTES = 64 * 1024
RECORD_BYTES = 8 * 1024 * 1024
CHECKPOINT_BYTES = 64 * 1024 * 1024


class CheckpointRejected(ValueError):
    """The optional checkpoint cannot prove the current prepared generation."""


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    digest: str
    chunks: tuple


@dataclass(frozen=True, slots=True)
class CheckpointDelta:
    """Atomically replace sections at token, after checking the exact base.

    A complete export replaces the whole checkpoint. A delta preserves all
    unlisted sections. The writer enforces CHECKPOINT_BYTES on the merged
    section lengths, and publishes them with the prepared facts in one stage.
    """
    base: tuple | None
    token: tuple
    sections: tuple
    tombstones: tuple
    complete: bool


def _require(condition, message):
    if not condition:
        raise CheckpointRejected(message)


def _text(value, maximum=speed.MAX_TEXT_CHARS, *, empty=False):
    _require(type(value) is str and len(value) <= maximum and (empty or bool(value)),
             'invalid scalar text')
    return value


def _integer(value, maximum=None):
    _require(type(value) is int and value >= 0 and (maximum is None or value <= maximum),
             'invalid nonnegative integer')
    return value


def _number(value):
    _require(type(value) in (int, float) and isfinite(value), 'invalid finite number')
    return value


def _condition(value, *, optional=False):
    if optional and value is None:
        return None
    _require(type(value) is list and len(value) == 7, 'invalid condition')
    for item in value[:4]:
        _text(item)
    _integer(value[4], len(speed.INPUT_BANDS) - 1)
    _integer(value[5], 3)
    _require(type(value[6]) is bool, 'invalid cache band')
    return tuple(value)


def _owner(value):
    _require(type(value) is list and len(value) == 2, 'invalid owner')
    return tuple(_text(part) for part in value)


def _dependencies(value):
    _require(type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),
             'invalid durable owner dependency digest')
    return value


def _exact(left,right):
    if type(left) is not type(right):
        return False
    if isinstance(left,list):
        return len(left)==len(right) and all(_exact(a,b) for a,b in zip(left,right))
    if isinstance(left,dict):
        return left.keys()==right.keys() and all(_exact(left[key],right[key]) for key in left)
    return left==right


def _policy():
    # The classifier version must change when scope/reducer/analytics semantics
    # change. The size stamp also prevents using an old object-size estimate
    # after a Python runtime or memory-policy change.
    return [CLASSIFIER_FORMAT, speed.MIN_BASELINE, speed.MAX_BASELINE, speed.MAX_AGE,
            speed.SLOW_CALLS, speed.RECOVERY_CALLS, speed.MAX_TRACKED_SCOPES,
            speed.MAX_RETAINED_SAMPLES, speed.MAX_METADATA_BYTES, speed.MAX_TEXT_CHARS,
            sys.implementation.name, list(sys.version_info[:2]), struct.calcsize('P')]


def _binding(token, preparation, source_identity):
    _require(type(token) in (tuple, list) and len(token) == 2, 'invalid prepared token')
    _text(token[0], 128)
    _integer(token[1])
    _text(preparation, 256)
    _require(type(source_identity) is bytes and len(source_identity) <= CHUNK_BYTES,
             'source identity must be the writer encoded packet')
    return dict(format=FORMAT, classifier=_policy(), token=list(token),
                owner_binding='prepared-dependencies-pack-sha256-1',
                preparation=preparation, source=hashlib.sha256(source_identity).hexdigest())


def _signature(source):
    _require(isinstance(source, PreparedSpeedInput) and source.token is not None,
             'unproven prepared writer binding')
    return frozenset((owner,state.revision) for owner,state in source.owners.items())


def _section(name, records, budget):
    """Encode one logical section as bounded NDJSON records and byte chunks."""
    encoder = json.JSONEncoder(ensure_ascii=True, sort_keys=True,
                               separators=(',', ':'), allow_nan=False)
    chunks, pending = [], bytearray()
    digest = hashlib.sha256()

    def emit(part):
        budget[0] += len(part)
        _require(budget[0] <= CHECKPOINT_BYTES, 'encoded checkpoint capacity')
        digest.update(part)
        view = memoryview(part)
        while view:
            take = min(CHUNK_BYTES - len(pending), len(view))
            pending.extend(view[:take])
            view = view[take:]
            if len(pending) == CHUNK_BYTES:
                chunks.append(bytes(pending))
                pending.clear()

    for record in records:
        length = 0
        for part in encoder.iterencode(record):
            data = part.encode('ascii')
            length += len(data)
            _require(length < RECORD_BYTES, 'encoded checkpoint record capacity')
            emit(data)
        emit(b'\n')
    if pending:
        chunks.append(bytes(pending))
    return Section(name, digest.hexdigest(), tuple(chunks))


def _pairs(values):
    result = {}
    for key, value in values:
        _require(key not in result, 'duplicate JSON field')
        result[key] = value
    return result


def _invalid_constant(value):
    raise CheckpointRejected('nonfinite JSON constant')


def _records(section, budget):
    _require(type(section) is Section and type(section.chunks) is tuple, 'invalid section')
    _text(section.name, 128)
    _require(type(section.digest) is str and len(section.digest) == 64, 'invalid section digest')
    digest, pending = hashlib.sha256(), bytearray()
    for chunk in section.chunks:
        _require(type(chunk) is bytes and 0 < len(chunk) <= CHUNK_BYTES, 'invalid chunk size')
        budget[0] += len(chunk)
        _require(budget[0] <= CHECKPOINT_BYTES, 'encoded checkpoint capacity')
        digest.update(chunk)
        pending.extend(chunk)
        while True:
            end = pending.find(b'\n')
            if end < 0:
                break
            _require(end < RECORD_BYTES, 'encoded checkpoint record capacity')
            data = bytes(pending[:end])
            del pending[:end + 1]
            yield json.loads(data, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
        _require(len(pending) < RECORD_BYTES, 'encoded checkpoint record capacity')
    _require(not pending and digest.hexdigest() == section.digest, 'incomplete or damaged section')


def _sample_value(sample):
    return [sample.session[1], sample.order[3], sample['key'], sample['ts'],
            sample['output'], sample['completion_latency_ms']]


def _sample(value, home, condition, proofs):
    _require(type(value) is list and len(value) == 6, 'invalid scalar sample')
    sid, ordinal, key, at, output, elapsed = value
    owner = (home, _text(sid))
    _require(owner in proofs and condition in proofs[owner].buckets, 'sample outside owner scope')
    _integer(ordinal)
    _require(ordinal < proofs[owner].length, 'sample outside prepared length')
    _text(key)
    _number(at)
    _number(output)
    _number(elapsed)
    _require(output >= 128 and elapsed > 0, 'invalid speed sample')
    result = speed._Sample(owner, ordinal,
        dict(key=key, ts=at, output=output, completion_latency_ms=elapsed), condition)
    _require(speed.output_speed(result) is not None, 'invalid sampled speed')
    return result


def _baseline(value):
    fields = {'baseline_speed', 'threshold', 'baseline_count', 'baseline_keys', 'baseline_source'}
    _require(type(value) is dict and value.keys() == fields, 'invalid frozen baseline')
    _require(_number(value['baseline_speed']) > 0 and _number(value['threshold']) > 0,
             'invalid frozen baseline speed')
    count = _integer(value['baseline_count'], speed.MAX_BASELINE)
    _require(count >= speed.MIN_BASELINE and type(value['baseline_keys']) is list
             and len(value['baseline_keys']) == count, 'invalid frozen baseline membership')
    for key in value['baseline_keys']:
        _text(key)
    _require(value['baseline_source'] in ('session', 'peers'), 'invalid baseline source')
    return value


def _state_value(state):
    result = dict(state)
    for field in ('recent', 'recovery'):
        if field in result:
            result[field] = [_sample_value(row) for row in result[field]]
    return result


def _state(value, owner, condition, proofs):
    fields = {'baseline', 'recent', 'recovery', 'active', 'incident_id'}
    _require(type(value) is dict and value.keys() <= fields, 'invalid reducer state')
    if not value:
        return {}
    _require({'baseline', 'recent'} <= value.keys(), 'incomplete reducer state')
    result = dict(value)
    result['baseline'] = _baseline(value['baseline'])
    for field, maximum in (('recent', speed.SLOW_CALLS), ('recovery', speed.RECOVERY_CALLS - 1)):
        if field in result:
            _require(type(result[field]) is list and len(result[field]) <= maximum, 'invalid sample list')
            result[field] = [_sample(row, owner[0], condition, proofs) for row in result[field]]
            _require(all(row.session == owner for row in result[field]), 'sample in another owner state')
    _require(bool(result['recent']), 'missing recent samples')
    if 'active' in result:
        _require(result['active'] is True and len(result['recent']) == speed.SLOW_CALLS
                 and 'incident_id' in result, 'invalid active incident')
        _text(result['incident_id'])
    else:
        _require(len(result['recent']) < speed.SLOW_CALLS and 'recovery' not in result
                 and 'incident_id' not in result, 'invalid pending incident')
    return result


def _result(value, state, condition):
    _require(type(value) is dict and type(value.get('active')) is bool, 'invalid result')
    if not state.get('active'):
        _require(value == {'active': False}, 'inactive result mismatch')
        return value
    fields = {'active', 'baseline_speed', 'threshold', 'baseline_count', 'baseline_keys',
              'baseline_source', 'recent_speed', 'recent_count', 'drop_percent', 'recent_keys',
              'incident_id', 'call_id', 'model', 'effort', 'tier'}
    _require(value.keys() == fields and value['active'] is True, 'incomplete active result')
    _require(all(_exact(value[key],item) for key,item in state['baseline'].items()), 'baseline result mismatch')
    _integer(value['recent_count'],speed.SLOW_CALLS)
    _number(value['recent_speed']);_number(value['drop_percent'])
    recent = state['recent']
    average = speed.output_speed_summary(recent)['value']
    _require(value['recent_keys'] == [row['key'] for row in recent]
             and value['recent_count'] == len(recent) and value['recent_speed'] == average
             and value['drop_percent'] == max(0, (1 - average / state['baseline']['baseline_speed']) * 100)
             and value['incident_id'] == state['incident_id'] and value['call_id'] == recent[-1]['key']
             and (value['model'], value['effort'], value['tier']) == condition[:3], 'active result mismatch')
    return value


def _scope_name(home, condition):
    data = json.dumps([home, condition], ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return 'scope:' + hashlib.sha256(data).hexdigest()


def _owner_name(owner):
    data = json.dumps(owner, ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return 'owner:' + hashlib.sha256(data).hexdigest()


def _token(token):
    return (token.instance, token.generation) if token is not None else None


def export_checkpoint(tracker, source, *, preparation, base=None):
    """Return an initial full checkpoint or the latest completed cohort delta.

    ``base`` is the exact token in the writer's durable checkpoint header.
    Omitting it requests a full recovery export. A mismatching predecessor is
    rejected; a writer cannot silently skip an unpublished classifier cohort.
    No unchanged owner or peer section is serialized for a valid delta.
    """
    try:
        _signature(source)
        with source.read_scope():
            return _export(tracker, source, preparation, base)
    except CheckpointRejected:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError,
            speed._Capacity, speed._Unsupported) as error:
        raise CheckpointRejected('invalid speed checkpoint export') from error


def _export(tracker, source, preparation, base):
    signature = _signature(source)
    _require(tracker.engine_ref is not None and tracker.engine_ref() is source.binding
             and signature == tracker.signature and tracker.proofs.keys() == source.owners.keys()
             and tracker.checkpoint_token == source.token,
             'classifier is not current incremental state')
    token = _token(source.token)
    complete = base is None
    if not complete:
        _require(type(base) is tuple and base == _token(tracker.checkpoint_from_token),
                 'checkpoint delta predecessor mismatch')
    header = _binding(token, preparation, source.source_identity)
    header.update(owners=len(tracker.proofs), scopes=len(tracker.replays))
    budget = [0]
    sections = [_section('header', (header,), budget)]
    owners = tracker.proofs.keys() if complete else tracker.changed_owners
    scopes = tracker.replays.keys() if complete else tracker.changed_scopes
    for owner in sorted(owners):
        proof = tracker.proofs[owner]
        stamp = source.owners[owner]
        replay = tracker.replays.get((owner[0], proof.condition))
        state = replay.states[owner] if replay is not None else None
        records = (
            ['owner', owner, _dependencies(stamp.revision), proof.length, stamp.last_ordinal,
             [[condition, count, key_bytes] for condition, (count, key_bytes) in sorted(proof.buckets.items())],
             proof.condition, proof.timestamp],
            ['state', _state_value(state) if state is not None else None],
            ['result', tracker.results[owner]],
        )
        sections.append(_section(_owner_name(owner), records, budget))
    for home, condition in sorted(scopes):
        replay = tracker.replays[(home, condition)]
        def records():
            yield ['scope', home, condition, replay.last_order]
            for sid, sample in replay.pool:
                _require(sid == sample.session[1], 'pool owner mismatch')
                yield ['pool', *_sample_value(sample)]
        sections.append(_section(_scope_name(home, condition), records(), budget))
    removed = () if complete else tuple(sorted(
        [_owner_name(owner) for owner in tracker.removed_owners]
        + [_scope_name(*bucket) for bucket in tracker.removed_scopes]))
    _require(_signature(source) == signature, 'prepared metadata changed during checkpoint export')
    return CheckpointDelta(base, token, tuple(sections), removed, complete)


def restore_checkpoint(tracker, source, sections, *, preparation):
    """Validate completely, then bind a restored reducer to the canonical engine.

    A rejected restore leaves the existing tracker unchanged. Revision numbers
    are rebound only after durable dependency equality and prepared lengths
    match. The source supplies typed canonical lengths, not history lists.
    """
    try:
        _signature(source)
        with source.read_scope():
            restored=_restore(source,sections,preparation)
    except CheckpointRejected:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, RecursionError,
            speed._Capacity, speed._Unsupported) as error:
        raise CheckpointRejected('invalid speed checkpoint') from error
    tracker.__dict__.update(restored.__dict__)


def _restore(source,sections,preparation):
    signature = _signature(source)
    _require(type(sections) is tuple and 1 <= len(sections)
             <= len(source.owners) + speed.MAX_TRACKED_SCOPES + 1,
             'invalid section count')
    _require(all(type(section) is Section for section in sections),'invalid checkpoint sections')
    budget = [0]
    _require(sections[0].name == 'header', 'missing checkpoint header')
    records = iter(_records(sections[0], budget))
    header = next(records, None)
    _require(type(header) is dict and next(records, None) is None, 'invalid checkpoint header')
    token=(source.token.instance,source.token.generation)
    expected = _binding(token, preparation, source.source_identity)
    _require(header.keys() == expected.keys() | {'owners', 'scopes'}
             and all(_exact(header[key],value) for key,value in expected.items()), 'prepared binding mismatch')
    _require(_integer(header['owners']) == len(source.owners)
             and _integer(header['scopes'], speed.MAX_TRACKED_SCOPES)
                 == len(sections) - len(source.owners) - 1,
             'checkpoint cardinality mismatch')
    restored = speed.SpeedHealth()
    restored._metrics()
    owner_states = {}
    names = {'header'}
    scope_sections = []
    for section in sections[1:]:
        _require(section.name not in names, 'duplicate section')
        names.add(section.name)
        if section.name.startswith('scope:'):
            scope_sections.append(section)
            continue
        rows = iter(_records(section, budget))
        row = next(rows, None)
        _require(type(row) is list and len(row) == 8 and row[0] == 'owner', 'invalid owner proof')
        owner = _owner(row[1])
        _require(owner in source.owners and owner not in restored.proofs
                 and section.name == _owner_name(owner)
                 and _dependencies(row[2]) == source.owners[owner].revision, 'owner dependency mismatch')
        length = _integer(row[3])
        stamp = source.owners[owner]
        _require(length == stamp.length and _exact(row[4], stamp.last_ordinal), 'prepared owner frontier mismatch')
        _require(type(row[5]) is list and len(row[5]) <= speed.MAX_TRACKED_SCOPES, 'invalid proof buckets')
        buckets = {}
        for entry in row[5]:
            _require(type(entry) is list and len(entry) == 3, 'invalid proof bucket')
            condition = _condition(entry[0])
            _require(condition not in buckets, 'duplicate proof bucket')
            count = _integer(entry[1], length)
            key_bytes = _integer(entry[2], speed.MAX_TEXT_CHARS * 4 + 256)
            _require(count > 0 and key_bytes > 0, 'empty proof bucket')
            buckets[condition] = (count, key_bytes)
        _require(sum(count for count, _ in buckets.values()) <= length, 'proof row count mismatch')
        condition = _condition(row[6], optional=True)
        timestamp = _number(row[7]) if length else None
        _require((length > 0 or row[7] is None and condition is None and not buckets)
                 and (condition is None or condition in buckets), 'invalid proof frontier')
        revision = stamp.revision
        restored.proofs[owner] = speed._Proof(revision, length, buckets, condition, timestamp)
        state_row, result_row = next(rows, None), next(rows, None)
        _require(type(state_row) is list and len(state_row) == 2 and state_row[0] == 'state'
                 and type(result_row) is list and len(result_row) == 2 and result_row[0] == 'result'
                 and next(rows, None) is None, 'incomplete owner reducer records')
        if condition is None:
            _require(state_row[1] is None and _exact(result_row[1], {'active':False}),
                     'invalid owner has live reducer state')
        else:
            state = _state(state_row[1], owner, condition, restored.proofs)
            result = _result(result_row[1], state, condition)
            owner_states[owner] = (state, result)
    _require(restored.proofs.keys() == source.owners.keys(), 'incomplete owner proofs')
    pools = restored._admit(restored.proofs)
    _require(len(pools) == header['scopes'], 'peer scope count mismatch')
    for section in scope_sections:
        rows = iter(_records(section, budget))
        first = next(rows, None)
        _require(type(first) is list and len(first) == 4 and first[0] == 'scope', 'missing peer scope')
        home, condition = _text(first[1]), _condition(first[2])
        bucket = (home, condition)
        _require(bucket in pools and bucket not in restored.replays
                 and section.name == _scope_name(home, condition), 'peer scope mismatch')
        order = first[3]
        _require(type(order) is list and len(order) == 4 and order[1] == home, 'invalid scope frontier')
        _number(order[0]); _text(order[2]); _integer(order[3])
        frontier_owner = (home, order[2])
        _require(frontier_owner in restored.proofs and condition in restored.proofs[frontier_owner].buckets
                 and order[3] < restored.proofs[frontier_owner].length, 'scope frontier outside owner')
        replay = speed._ScopeReplay(condition)
        replay.last_order = tuple(order)
        for row in rows:
            _require(type(row) is list and bool(row) and row[0] == 'pool'
                     and len(replay.pool) < speed.MAX_BASELINE * 4, 'invalid peer pool')
            sample = _sample(row[1:], home, condition, restored.proofs)
            # Recovery inserts its two retained samples together. A peer may
            # have arrived between them, so preserve causal insertion order.
            _require(sample.order <= replay.last_order, 'pool beyond scope frontier')
            replay.pool.append((sample.session[1], sample))
        restored.replays[bucket] = replay
    _require(restored.replays.keys() == pools.keys() and _signature(source) == signature,
             'incomplete or changed prepared generation')
    for owner, (state, result) in owner_states.items():
        replay = restored.replays[(owner[0], restored.proofs[owner].condition)]
        replay.states[owner], replay.results[owner] = state, result
    restored._finish(restored.proofs)
    restored.signature = signature
    restored.engine_ref = ref(source.binding)
    restored.checkpoint_token = source.token
    restored.checkpoint_from_token = source.token
    return restored
