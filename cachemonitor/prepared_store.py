"""Analysis-owned, rebuildable prepared facts.

One writer stages changed facts in bounded transactions. A publication marker and
owner metadata pointers publish them atomically. Readers hold one SQLite snapshot;
later detail requests must present that snapshot's token or receive StaleGeneration.
No collector, source, proof, quota, notification, or acknowledgement writes occur.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import sys
import time
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from cachemonitor.analysis_records import compact_record, public_record
from cachemonitor.observer_state import ProcessLock


FORMAT = 5
APPLICATION_ID = 0x43505250
# Only versions whose dedicated, disposable schema is known to this writer.
REBUILDABLE_FORMATS = frozenset((1, 2, 3, 4))
METRICS = (
    'ts', 'session_ordinal', 'input', 'cached', 'written', 'output', 'reasoning',
    'total', 'uncached', 'ordinary_input', 'non_reasoning', 'rate', 'cost',
    'cost_input', 'cost_uncached', 'cost_cached', 'cost_written', 'cost_unclassified',
    'cost_output', 'cost_reasoning', 'cost_non_reasoning', 'duration',
    'generation_wait', 'output_speed', 'completion_latency_ms',
    'generation_latency_ms', 'first_token_latency_ms', 'response_status',
    'timing_valid', 'observation_missing', 'model', 'configured_model',
    'requested_model', 'effort', 'service_tier', 'model_conflict', 'mode_conflict',
    'input_conflict', 'output_conflict', 'cache_miss', 'cache_degradation', 'cache_incident_id', 'state', 'purpose',
    'transport', 'transport_source', 'cache_policy_conflict', 'service_tier_source',
    'tps', 'display_service_tier',
)
SPEED_FIELDS = ('ts', 'session_ordinal', 'input', 'cached', 'output', 'reasoning',
                'completion_latency_ms', 'response_status', 'timing_valid',
                'observation_missing', 'model_conflict', 'output_conflict',
                'mode_conflict', 'input_conflict', 'model', 'requested_model',
                'effort', 'service_tier', 'transport', 'transport_source',
                'cache_policy_conflict', 'service_tier_source')
OWNER_FIELDS = ('home', 'sid', 'title', 'cwd', 'project', 'project_name', 'source',
                'parent_thread_id', 'agent_path', 'agent_nickname', 'spawn_depth')
LINEAGE = OWNER_FIELDS[7:]
DEPENDENCIES = ('source_digest', 'pricing', 'mode', 'ownership', 'classifier', 'preparation')
FORBIDDEN_OWNER = frozenset(('history', 'records', 'timestamps', 'boundaries',
                            'whole_turns', 'turn_records', 'turn_states'))
RAW_TRANSPORT_FIELDS = frozenset(('transports', 'unmatched_transports'))
KINDS = ('calls', 'requests', 'candidates', 'headers', 'sections')
CHECKPOINT_TABLES = ('checkpoint_chunks', 'checkpoint_sections', 'checkpoint_versions',
                     'checkpoint_seen', 'checkpoint_retired', 'checkpoint_heads')
DERIVED = ('summary', 'choices', 'model_alerts', 'checkpoint')
UNCHANGED = object()
METRIC_COLUMNS = ','.join('m_' + name for name in METRICS)


class PreparedError(RuntimeError):
    pass


class StaleGeneration(PreparedError):
    pass


class NotReady(PreparedError):
    pass


class RebuildRequired(PreparedError):
    pass


class CapacityExceeded(PreparedError):
    pass


class WriteDeferred(CapacityExceeded):
    """An existing reader temporarily prevents bounded WAL reuse."""


class ReadCancelled(PreparedError):
    pass


class IncompletePublication(PreparedError):
    pass


@dataclass(frozen=True)
class Limits:
    # The main-file limit includes current, staging, obsolete rows, and indices.
    database_bytes: int = 1024 * 1024 * 1024
    wal_bytes: int = 128 * 1024 * 1024
    write_reserve_bytes: int = 32 * 1024 * 1024
    staging_bytes: int = 256 * 1024 * 1024
    packet_bytes: int = 64 * 1024
    key_bytes: int = 2048
    page_bytes: int = 4 * 1024 * 1024
    page_objects_bytes: int = 32 * 1024 * 1024
    page_rows: int = 512
    write_rows: int = 16
    fact_write_rows: int = 256
    fact_write_bytes: int = 256 * 1024
    fact_write_objects: int = 4 * 1024 * 1024
    owners_per_batch: int = 128
    packet_nodes: int = 4096
    read_seconds: float = 2.0
    read_vm_steps: int = 2_000_000
    speed_range_rows: int = 80_000
    speed_temp_bytes: int = 64 * 1024 * 1024
    speed_sort_bytes: int = 512 * 1024 * 1024
    checkpoint_bytes: int = 64 * 1024 * 1024
    checkpoint_chunk_bytes: int = 64 * 1024
    checkpoint_sections: int = 100_000
    checkpoint_namespaces: int = 8


@dataclass(frozen=True)
class Token:
    instance: str
    generation: int


@dataclass(frozen=True)
class Page:
    rows: tuple
    after: tuple | None
    complete: bool
    encoded_bytes: int
    object_bytes: int


def _size(value):
    """Conservative retained size: shared children are deliberately counted twice."""
    n = sys.getsizeof(value)
    if isinstance(value, dict):
        n += sum(_size(k) + _size(v) for k, v in value.items())
    elif isinstance(value, (tuple, list)):
        n += sum(_size(v) for v in value)
    return n


def pack(value, limits=Limits(), *, maximum=None):
    """Exact restricted Python value codec; never unpickle an optional cache."""
    maximum = maximum or limits.packet_bytes
    budget = [0, 0]

    def tag(item, depth=0):
        budget[0] += 1
        if depth > 32 or budget[0] > limits.packet_nodes:
            raise CapacityExceeded('packet structure exceeds the decoded-object budget')
        t = type(item)
        if item is None:
            result = ['n']
        elif t is bool:
            result = ['b', item]
        elif t is int:
            result = ['i', str(item)]
        elif t is float:
            result = ['f', struct.pack('>d', item).hex()]
        elif t is str:
            budget[1] += len(item) * 6
            result = ['s', item]
        elif t is bytes:
            budget[1] += len(item) * 2
            result = ['y', item.hex()]
        elif t in (tuple, list):
            result = ['t' if t is tuple else 'l', [tag(v, depth + 1) for v in item]]
        elif t is dict:
            result = ['d', [[tag(k, depth + 1), tag(v, depth + 1)] for k, v in item.items()]]
        else:
            raise TypeError(f'unsupported prepared value type: {t.__name__}')
        if budget[1] > maximum * 6:
            raise CapacityExceeded('packet text exceeds the serialization budget')
        return result

    result = json.dumps(tag(value), ensure_ascii=True, separators=(',', ':')).encode('ascii')
    if len(result) > maximum:
        raise CapacityExceeded('encoded packet exceeds its byte budget')
    return result


def unpack(value, limits=Limits(), *, maximum=None):
    maximum = maximum or limits.packet_bytes
    if not isinstance(value, bytes) or len(value) > maximum:
        raise RebuildRequired('invalid prepared packet size')
    count = [0]

    def read(item, depth=0):
        count[0] += 1
        if depth > 32 or count[0] > limits.packet_nodes or not isinstance(item, list):
            raise RebuildRequired('invalid prepared packet structure')
        tag = item[0]
        if tag == 'n':
            return None
        if tag == 'b':
            return bool(item[1])
        if tag == 'i':
            return int(item[1])
        if tag == 'f':
            return struct.unpack('>d', bytes.fromhex(item[1]))[0]
        if tag == 's':
            return item[1]
        if tag == 'y':
            return bytes.fromhex(item[1])
        if tag in ('l', 't'):
            values = [read(v, depth + 1) for v in item[1]]
            return tuple(values) if tag == 't' else values
        if tag == 'd':
            return {read(k, depth + 1): read(v, depth + 1) for k, v in item[1]}
        raise RebuildRequired('unknown prepared packet tag')

    try:
        return read(json.loads(value))
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError) as error:
        raise RebuildRequired('invalid prepared packet') from error


def identity(value, limits=Limits()):
    """Match Python hash/equality for existing record keys, retaining values elsewhere."""
    def canonical(item):
        t = type(item)
        if t is bool:
            return int(item)
        if t is float:
            if not math.isfinite(item):
                raise TypeError('nonfinite record identities are not persistable')
            return int(item) if item.is_integer() else item
        if t is tuple:
            return tuple(canonical(v) for v in item)
        if t not in (type(None), int, str, bytes):
            raise TypeError('record identities must be immutable scalar values or tuples')
        return item
    return pack(canonical(value), limits, maximum=limits.key_bytes)


def _detail_pack(raw):
    """Compress one bounded nonmetric packet; changed calls remain independent."""
    compressed = zlib.compress(raw, 3) if len(raw) >= 64 else raw
    return b'\x01' + compressed if len(compressed) + 16 < len(raw) else b'\x00' + raw


def _detail_unpack(packet, limits):
    if not isinstance(packet, bytes) or not packet or len(packet) > limits.packet_bytes + 1:
        raise RebuildRequired('invalid detail packet envelope')
    if packet[0] == 0:
        raw = packet[1:]
    elif packet[0] == 1:
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(packet[1:], limits.packet_bytes + 1)
        except zlib.error as error:
            raise RebuildRequired('invalid compressed detail packet') from error
        if len(raw) > limits.packet_bytes or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise RebuildRequired('detail packet exceeds its decoded bound or is incomplete')
    else:
        raise RebuildRequired('unknown detail packet encoding')
    return unpack(raw, limits)


def _scalar(value, present):
    if not present:
        return 0, None
    if value is None:
        return 1, None
    t = type(value)
    if t is bool:
        return 2, int(value)
    if t is int:
        return (3, value) if -(2 ** 63) <= value < 2 ** 63 else (9, str(value))
    if t is float:
        if not math.isfinite(value):
            return 8, struct.pack('>d', value)
        if value == 0 and math.copysign(1, value) < 0:
            return 10, 0.0
        return 4, value
    if t is str:
        return 5, value
    if t is bytes:
        return 6, value
    raise TypeError('metric columns accept scalar values; structured details belong in the packet')


def _restore_scalar(tag, value):
    if tag == 1:
        return None
    if tag == 2:
        return bool(value)
    if tag in (3, 9):
        return int(value)
    if tag == 4:
        return float(value)
    if tag == 8:
        return struct.unpack('>d', value)[0]
    if tag == 10:
        return -0.0
    return value


def _number(value, name):
    if type(value) not in (int, float):
        raise TypeError(f'{name} must be a finite number')
    if type(value) is int and not -(2 ** 63) <= value < 2 ** 63:
        raise TypeError(f'{name} exceeds SQLite exact integer range')
    if not math.isfinite(value):
        raise TypeError(f'{name} must be a finite number')
    return value


def _chunks(items, size):
    group = []
    for item in items:
        group.append(item)
        if len(group) == size:
            yield group
            group = []
    if group:
        yield group


def _schema():
    metric_sql = ','.join('m_' + name for name in METRICS)  # no SQLite affinity coercion
    return f'''
        CREATE TABLE meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            format INTEGER NOT NULL,instance TEXT NOT NULL,generation INTEGER NOT NULL,
            ready INTEGER NOT NULL,source BLOB NOT NULL);
        CREATE TABLE publications(id INTEGER PRIMARY KEY AUTOINCREMENT,state INTEGER NOT NULL,
            source BLOB NOT NULL,input_bytes INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX publications_state ON publications(state,id);
        CREATE TABLE owners(id INTEGER PRIMARY KEY,home TEXT NOT NULL,sid TEXT NOT NULL,
            UNIQUE(home,sid));
        CREATE TABLE owner_versions(owner INTEGER NOT NULL,batch INTEGER NOT NULL,
            deleted INTEGER NOT NULL,source_ordinal INTEGER NOT NULL,request_mask INTEGER NOT NULL,pending INTEGER NOT NULL,metadata BLOB NOT NULL,call_metadata BLOB NOT NULL,dependencies BLOB NOT NULL,
            summary BLOB NOT NULL,choices BLOB NOT NULL,model_alerts BLOB NOT NULL,
            checkpoint BLOB NOT NULL,PRIMARY KEY(owner,batch)) WITHOUT ROWID;
        CREATE INDEX owner_versions_batch ON owner_versions(batch,owner);
        CREATE INDEX owner_versions_order ON owner_versions(source_ordinal,owner,batch);
        CREATE TABLE owner_heads(owner INTEGER PRIMARY KEY,batch INTEGER NOT NULL);
        CREATE INDEX owner_heads_batch ON owner_heads(batch);
        CREATE TABLE calls(owner INTEGER NOT NULL,identity BLOB NOT NULL,batch INTEGER NOT NULL,
            deleted INTEGER NOT NULL,signature BLOB NOT NULL,key_value BLOB,turn_value BLOB,
            turn_identity BLOB,owner_mask INTEGER,types BLOB,detail BLOB,encoded_bytes INTEGER,
            {metric_sql},PRIMARY KEY(owner,identity,batch));
        CREATE INDEX calls_time ON calls(owner,m_ts,m_session_ordinal,identity,batch);
        CREATE INDEX calls_ordinal ON calls(owner,m_session_ordinal,identity,batch);
        CREATE INDEX calls_turn ON calls(owner,turn_identity,m_ts,m_session_ordinal,identity,batch);
        CREATE INDEX calls_batch ON calls(batch,owner,identity);
        CREATE INDEX calls_global_time ON calls(m_ts,owner,m_session_ordinal,identity,batch);
        CREATE INDEX calls_dimensions ON calls(m_model,m_service_tier,m_effort,m_ts,owner,m_session_ordinal,identity,batch);
        CREATE INDEX calls_model_mode_time ON calls(m_model,m_service_tier,m_ts,owner,m_session_ordinal,identity,batch);
        CREATE TABLE requests(owner INTEGER NOT NULL,identity BLOB NOT NULL,batch INTEGER NOT NULL,
            deleted INTEGER NOT NULL,signature BLOB NOT NULL,key_value BLOB,ordinal INTEGER,
            started_at,ended_at,types BLOB,detail BLOB,encoded_bytes INTEGER,
            PRIMARY KEY(owner,identity,batch));
        CREATE INDEX requests_start ON requests(owner,started_at,ordinal,identity,batch);
        CREATE INDEX requests_end ON requests(owner,ended_at,ordinal,identity,batch);
        CREATE INDEX requests_batch ON requests(batch,owner,identity);
        CREATE INDEX requests_ordinal ON requests(owner,ordinal,identity,batch);
        CREATE TABLE headers(owner INTEGER NOT NULL,identity BLOB NOT NULL,batch INTEGER NOT NULL,
            deleted INTEGER NOT NULL,signature BLOB NOT NULL,key_value BLOB,display_ordinal INTEGER,
            effective_at,call_count INTEGER,minimum,maximum,first_key BLOB,dimensions BLOB,
            turn_state BLOB,state_present INTEGER,request_present INTEGER,encoded_bytes INTEGER,
            PRIMARY KEY(owner,identity,batch));
        CREATE INDEX headers_display ON headers(owner,display_ordinal,identity,batch);
        CREATE INDEX headers_effective ON headers(owner,effective_at,display_ordinal,identity,batch);
        CREATE INDEX headers_batch ON headers(batch,owner,identity);
        CREATE TABLE candidates(owner INTEGER NOT NULL,identity BLOB NOT NULL,batch INTEGER NOT NULL,
            deleted INTEGER NOT NULL,signature BLOB NOT NULL,key_value BLOB,ordinal INTEGER,
            scoped INTEGER,encoded_bytes INTEGER,PRIMARY KEY(owner,identity,batch)) WITHOUT ROWID;
        CREATE INDEX candidates_identity ON candidates(identity,scoped,owner,batch);
        CREATE INDEX candidates_batch ON candidates(batch,owner,identity);
        CREATE INDEX candidates_ordinal ON candidates(owner,ordinal,identity,batch);
        CREATE TABLE sections(owner INTEGER NOT NULL,identity BLOB NOT NULL,batch INTEGER NOT NULL,
            deleted INTEGER NOT NULL,signature BLOB NOT NULL,section_identity BLOB,section_value BLOB,
            item_value BLOB,ordinal INTEGER,at,at_type INTEGER,detail BLOB,encoded_bytes INTEGER,
            PRIMARY KEY(owner,identity,batch));
        CREATE INDEX sections_order ON sections(owner,section_identity,ordinal,identity,batch);
        CREATE INDEX sections_time ON sections(owner,section_identity,at,ordinal,identity,batch);
        CREATE INDEX sections_batch ON sections(batch,owner,identity);
        CREATE TABLE checkpoint_versions(namespace TEXT NOT NULL,batch INTEGER NOT NULL,
            token BLOB NOT NULL,total_bytes INTEGER NOT NULL,section_count INTEGER NOT NULL,
            PRIMARY KEY(namespace,batch));
        CREATE INDEX checkpoint_versions_batch ON checkpoint_versions(batch);
        CREATE TABLE checkpoint_heads(namespace TEXT PRIMARY KEY,batch INTEGER NOT NULL);
        CREATE INDEX checkpoint_heads_batch ON checkpoint_heads(batch);
        CREATE TABLE checkpoint_sections(namespace TEXT NOT NULL,name TEXT NOT NULL,batch INTEGER NOT NULL,
            deleted INTEGER NOT NULL,digest TEXT,signature BLOB,chunk_count INTEGER,encoded_bytes INTEGER,
            PRIMARY KEY(namespace,name,batch));
        CREATE INDEX checkpoint_sections_batch ON checkpoint_sections(batch);
        CREATE TABLE checkpoint_chunks(namespace TEXT NOT NULL,name TEXT NOT NULL,batch INTEGER NOT NULL,
            ordinal INTEGER NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(namespace,name,batch,ordinal));
        CREATE INDEX checkpoint_chunks_batch ON checkpoint_chunks(batch);
        CREATE TABLE checkpoint_seen(batch INTEGER NOT NULL,namespace TEXT NOT NULL,name TEXT NOT NULL,
            PRIMARY KEY(batch,namespace,name));
        CREATE TABLE checkpoint_retired(namespace TEXT NOT NULL,name TEXT NOT NULL,batch INTEGER NOT NULL,
            PRIMARY KEY(namespace,name,batch));
        CREATE INDEX checkpoint_retired_batch ON checkpoint_retired(batch);
        CREATE TABLE seen(batch INTEGER NOT NULL,kind TEXT NOT NULL,owner INTEGER NOT NULL,
            identity BLOB NOT NULL,PRIMARY KEY(batch,kind,owner,identity)) WITHOUT ROWID;
        CREATE INDEX seen_owner ON seen(owner);
        CREATE TABLE retired(kind TEXT NOT NULL,owner INTEGER NOT NULL,identity BLOB NOT NULL,
            batch INTEGER NOT NULL,PRIMARY KEY(kind,owner,identity,batch)) WITHOUT ROWID;
        CREATE INDEX retired_batch ON retired(batch);
        CREATE TABLE owner_retired(owner INTEGER NOT NULL,batch INTEGER NOT NULL,
            PRIMARY KEY(owner,batch)) WITHOUT ROWID;
        CREATE INDEX owner_retired_batch ON owner_retired(batch);
    '''


def _latest(kind, alias='r', *, staged=False, batch=None, publication='p'):
    marker = ':batch' if batch is None else str(int(batch))
    visible = f'({publication}.state=1 OR {publication}.id={marker})' if staged else f'{publication}.state=1'
    newer = f'(np.state=1 OR np.id={marker})' if staged else 'np.state=1'
    return (f'{visible} AND NOT EXISTS (SELECT 1 FROM {kind} newer '
            f'JOIN publications np ON np.id=newer.batch WHERE newer.owner={alias}.owner '
            f'AND newer.identity={alias}.identity AND newer.batch>{alias}.batch AND {newer})')


def _checkpoint_latest(alias='r', batch=None):
    visible = f'(p.state=1 OR p.id={int(batch)})' if batch is not None else 'p.state=1'
    newer = f'(np.state=1 OR np.id={int(batch)})' if batch is not None else 'np.state=1'
    return (f'{visible} AND NOT EXISTS(SELECT 1 FROM checkpoint_sections n '
            f'JOIN publications np ON np.id=n.batch WHERE n.namespace={alias}.namespace '
            f'AND n.name={alias}.name AND n.batch>{alias}.batch AND {newer})')


def _checkpoint_name(value):
    if type(value) is not str or not value or len(value.encode('utf-8')) > 128:
        raise TypeError('checkpoint namespace/name must be nonempty bounded text')
    return value


def _portable_token(value):
    if isinstance(value,Token):
        value = (value.instance,value.generation)
    if (type(value) is not tuple or len(value) != 2 or type(value[0]) is not str or
            not value[0] or len(value[0]) > 128 or type(value[1]) is not int or value[1] < 0):
        raise TypeError('checkpoint token must be an exact portable store token')
    return value


class PreparedStore:
    """Exclusive writer for one dedicated, disposable analysis database."""
    def __init__(self, path, limits=Limits()):
        self.path, self.limits = Path(path).resolve(), limits
        self.db = None
        self.stage = None
        self.lock = ProcessLock(self.path.with_suffix(self.path.suffix + '.writer.lock'))
        self.lock.__enter__()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists() and self.path.stat().st_size > 0
            self.db = sqlite3.connect(self.path, timeout=.2, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            if existed:
                if self._validate_format() != FORMAT:
                    self._rebuild_format()
            else:
                self._initialize(self.db)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=NORMAL')
            self.db.execute('PRAGMA mmap_size=0')
            self.db.execute('PRAGMA cache_size=-8192')
            self.db.execute('PRAGMA temp_store=MEMORY')
            self.db.execute('PRAGMA wal_autocheckpoint=1024')
            self.db.execute(f'PRAGMA journal_size_limit={limits.wal_bytes}')
            pages = limits.database_bytes // self.db.execute('PRAGMA page_size').fetchone()[0]
            actual = self.db.execute(f'PRAGMA max_page_count={pages}').fetchone()[0]
            if actual > pages:
                raise CapacityExceeded('existing prepared database exceeds its page budget')
            # There is one writer lock; an unfinished predecessor cannot still publish.
            with self._transaction():
                self.db.execute('UPDATE publications SET state=-1 WHERE state=0')
        except BaseException:
            self.close()
            raise

    def _validate_format(self):
        # Never reinterpret busy, permission, disk-full, or damaged-header errors
        # as permission to erase a file whose ownership cannot be established.
        if self.db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
            raise RebuildRequired('not a prepared database')
        row = self.db.execute('SELECT format FROM meta WHERE singleton=1').fetchone()
        if row is None or type(row[0]) is not int or row[0] not in (FORMAT, *REBUILDABLE_FORMATS):
            raise RebuildRequired('unknown prepared database format')
        return row[0]

    def _initialize(self, db):
        db.execute('PRAGMA page_size=4096')
        db.executescript('BEGIN IMMEDIATE;' + _schema())
        db.execute('INSERT INTO meta VALUES(1,?,?,?,?,?)',
                   (FORMAT, uuid.uuid4().hex, 0, 0, pack(None, self.limits)))
        db.execute(f'PRAGMA application_id={APPLICATION_ID}')
        db.execute('COMMIT')

    def _rebuild_format(self):
        """Replace a recognized old cache before readers start, under writer lock.

        SQLite commits the empty schema atomically. An interrupted replacement
        leaves either the old recognized format or the new, unready instance.
        Only the new schema is copied: at most 1 MiB of pages plus WAL framing.
        Source ledgers and recovery files are never opened or removed here.
        """
        if self.db.execute('PRAGMA page_size').fetchone()[0] != 4096:
            raise RebuildRequired('unrecognized prepared database page size')
        pages = min(256, self.limits.database_bytes // 4096,
                    (self.limits.wal_bytes - 32) // 4120)
        if pages < 1:
            raise CapacityExceeded('empty prepared schema exceeds its budget')
        self.db.execute('PRAGMA cache_size=-8192')
        self.db.execute('PRAGMA mmap_size=0')
        self.db.execute('PRAGMA locking_mode=EXCLUSIVE')
        self.db.execute('BEGIN EXCLUSIVE')
        self.db.execute('COMMIT')  # backup requires no active destination transaction
        if self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
            raise sqlite3.OperationalError('prepared format recovery requires WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        if self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise sqlite3.OperationalError('prepared format recovery is busy')
        empty = sqlite3.connect(':memory:', isolation_level=None)
        try:
            empty.execute('PRAGMA page_size=4096')
            empty.execute(f'PRAGMA max_page_count={pages}')
            self._initialize(empty)
            deadline = time.monotonic() + 2

            def progress(status, remaining, total):
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise sqlite3.OperationalError('prepared format recovery is busy')
                if total > pages:
                    raise CapacityExceeded('empty prepared schema exceeds its budget')
                if time.monotonic() > deadline:
                    raise TimeoutError('prepared format recovery exceeded its deadline')

            empty.backup(self.db, pages=16, progress=progress, sleep=0)
        finally:
            empty.close()
        if self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
            raise sqlite3.OperationalError('prepared format recovery is busy')
        # Closing releases EXCLUSIVE locking before the ordinary query actor opens.
        self.db.close()
        self.db = None
        self.db = sqlite3.connect(self.path, timeout=.2, isolation_level=None)
        self.db.row_factory = sqlite3.Row

    @property
    def token(self):
        row = self.db.execute('SELECT instance,generation FROM meta WHERE singleton=1').fetchone()
        return Token(*row)

    def disk_usage(self):
        result = {}
        for label, suffix in (('database', ''), ('wal', '-wal'), ('shm', '-shm')):
            path = Path(str(self.path) + suffix)
            try:result[label] = path.stat().st_size
            except FileNotFoundError:result[label] = 0
        return result

    def _wal_size(self):
        try:return Path(str(self.path) + '-wal').stat().st_size
        except FileNotFoundError:return 0

    def _wal_admission(self):
        limit = self.limits.wal_bytes - self.limits.write_reserve_bytes
        if self._wal_size() <= limit:
            return
        self.db.execute('PRAGMA busy_timeout=0')
        try:
            checkpoint = self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        finally:
            self.db.execute('PRAGMA busy_timeout=200')
        if self._wal_size() > limit:
            if checkpoint[0]:
                raise WriteDeferred('reader snapshot pins WAL; retry after it closes')
            raise CapacityExceeded('reader snapshot pins WAL; close/cancel it before writing more')

    @contextmanager
    def _transaction(self):
        self._wal_admission()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise
        if self._wal_size() > self.limits.wal_bytes:
            raise CapacityExceeded('WAL crossed its guard after one bounded transaction')

    def begin(self, source_identity, *, expected=None):
        if self.stage is not None:
            raise IncompletePublication('finish or abort the existing staging batch first')
        expected = expected or self.token
        if expected != self.token:
            raise StaleGeneration('writer generation changed')
        source = pack(source_identity, self.limits)
        with self._transaction():
            batch = self.db.execute('INSERT INTO publications(state,source) VALUES(0,?)',
                                    (source,)).lastrowid
        self.stage = Stage(self, batch, expected, source)
        return self.stage

    @contextmanager
    def read_current(self, *, staged=False, cancel=None, seconds=None):
        """Writer-only conversion view. Normal readers still require readiness."""
        if staged and self.stage is None:
            raise IncompletePublication('there is no active staged batch')
        batch = self.stage.batch if staged else None
        with PreparedReader(self.path, self.limits) as reader:
            with reader._pin(self.token, cancel=cancel, seconds=seconds,
                             allow_incomplete=True, staged_batch=batch) as snapshot:
                yield snapshot

    def maintain(self, *, rows=16):
        """One bounded slice. Call from the writer's existing maintenance duty."""
        rows = min(max(1, rows), self.limits.write_rows)
        abandoned = self.db.execute('SELECT id FROM publications WHERE state=-1 LIMIT 1').fetchone()
        if abandoned:
            batch = abandoned[0]
            for table in CHECKPOINT_TABLES[:-1]:
                keys = [row[0] for row in self.db.execute(f'SELECT rowid FROM {table} WHERE batch=? LIMIT ?',(batch,rows))]
                if keys:
                    with self._transaction():
                        self.db.executemany(f'DELETE FROM {table} WHERE rowid=?',((key,) for key in keys))
                    return len(keys)
            for table in (*KINDS, 'seen', 'owner_versions', 'retired', 'owner_retired'):
                keys = [tuple(r) for r in self.db.execute(
                    (f'SELECT kind,owner,identity FROM seen WHERE batch=? LIMIT ?' if table == 'seen' else
                     f'SELECT owner,identity FROM {table} WHERE batch=? LIMIT ?')
                    if table not in ('owner_versions', 'owner_retired', 'retired') else
                    (f'SELECT owner,batch FROM {table} WHERE batch=? LIMIT ?'
                     if table in ('owner_versions', 'owner_retired') else
                     'SELECT kind,owner,identity FROM retired WHERE batch=? LIMIT ?'), (batch, rows))]
                if keys:
                    with self._transaction():
                        for key in keys:
                            if table in ('owner_versions', 'owner_retired'):
                                self.db.execute(f'DELETE FROM {table} WHERE owner=? AND batch=?', key)
                                self._prune_owner(key[0])
                            elif table == 'retired':
                                self.db.execute('DELETE FROM retired WHERE kind=? AND owner=? AND identity=? AND batch=?', (*key, batch))
                            elif table == 'seen':
                                self.db.execute('DELETE FROM seen WHERE kind=? AND owner=? AND identity=? AND batch=?', (*key, batch))
                            else:
                                self.db.execute(f'DELETE FROM {table} WHERE owner=? AND identity=? AND batch=?', (*key, batch))
                    return len(keys)
            with self._transaction():
                self.db.execute('DELETE FROM publications WHERE id=?', (batch,))
            return 1
        task = self.db.execute('SELECT q.* FROM retired q CROSS JOIN publications p ON p.id=q.batch '
                               'WHERE p.state=1 LIMIT 1').fetchone()
        if task:
            kind, owner, key, batch = tuple(task)
            table = 'owner_versions' if kind == 'owner' else kind
            if table not in (*KINDS, 'owner_versions'):
                raise RebuildRequired('invalid cleanup task')
            condition = 'owner=? AND batch<?'
            args = (owner, batch)
            if kind != 'owner':
                condition += ' AND identity=?'
                args += (key,)
            old = self.db.execute(f'SELECT batch FROM {table} WHERE {condition} LIMIT ?', (*args, rows)).fetchall()
            with self._transaction():
                for row in old:
                    self.db.execute(f'DELETE FROM {table} WHERE {condition} AND batch=?', (*args, row[0]))
                    self._prune_publication(row[0])
                if len(old) < rows:
                    self.db.execute('DELETE FROM retired WHERE kind=? AND owner=? AND identity=? AND batch=?', tuple(task))
                    if kind != 'owner':
                        self.db.execute(f'DELETE FROM {table} WHERE owner=? AND identity=? AND batch=? AND deleted=1',
                                        (owner, key, batch))
                    self._prune_publication(batch)
            return max(1, len(old))
        checkpoint_work = self._maintain_checkpoint(rows)
        if checkpoint_work:
            return checkpoint_work
        # Completed reconciliation inventories are the only remaining stage-only rows.
        seen = self.db.execute('SELECT s.batch,s.kind,s.owner,s.identity FROM seen s '
                               'CROSS JOIN publications p ON p.id=s.batch WHERE p.state=1 LIMIT ?', (rows,)).fetchall()
        if seen:
            with self._transaction():
                self.db.executemany('DELETE FROM seen WHERE batch=? AND kind=? AND owner=? AND identity=?', seen)
                for batch in {row[0] for row in seen}:
                    self._prune_publication(batch)
            return len(seen)
        removed = self.db.execute('SELECT q.owner,q.batch FROM owner_retired q '
                                  'CROSS JOIN publications p ON p.id=q.batch WHERE p.state=1 LIMIT 1').fetchone()
        if removed:
            owner, batch = removed
            with self._transaction():
                head = self.db.execute('SELECT batch FROM owner_heads WHERE owner=?', (owner,)).fetchone()
                if head and head[0] == batch:
                    self.db.execute('DELETE FROM owner_heads WHERE owner=?', (owner,))
                    self.db.execute('DELETE FROM owner_versions WHERE owner=? AND batch=?', (owner, batch))
                self.db.execute('DELETE FROM owner_retired WHERE owner=? AND batch=?', (owner, batch))
                self._prune_owner(owner)
                self._prune_publication(batch)
            return 1
        return 0

    def _maintain_checkpoint(self, rows):
        task = self.db.execute('SELECT q.namespace,q.name,q.batch FROM checkpoint_retired q '
                               'CROSS JOIN publications p ON p.id=q.batch WHERE p.state=1 LIMIT 1').fetchone()
        if task:
            namespace,name,batch = task
            for table in ('checkpoint_chunks','checkpoint_sections'):
                old = self.db.execute(f'SELECT rowid,batch FROM {table} WHERE namespace=? AND name=? AND batch<? LIMIT ?',
                                      (namespace,name,batch,rows)).fetchall()
                if old:
                    with self._transaction():
                        self.db.executemany(f'DELETE FROM {table} WHERE rowid=?',((row[0],) for row in old))
                        for previous in {row[1] for row in old}:
                            self._prune_publication(previous)
                    return len(old)
            with self._transaction():
                self.db.execute('DELETE FROM checkpoint_sections WHERE namespace=? AND name=? AND batch=? AND deleted=1',tuple(task))
                self.db.execute('DELETE FROM checkpoint_retired WHERE namespace=? AND name=? AND batch=?',tuple(task))
                self._prune_publication(batch)
            return 1
        seen = self.db.execute('SELECT s.rowid,s.batch FROM checkpoint_seen s CROSS JOIN publications p '
                               'ON p.id=s.batch WHERE p.state=1 LIMIT ?',(rows,)).fetchall()
        if seen:
            with self._transaction():
                self.db.executemany('DELETE FROM checkpoint_seen WHERE rowid=?',((row[0],) for row in seen))
                for batch in {row[1] for row in seen}:
                    self._prune_publication(batch)
            return len(seen)
        return 0

    def _prune_publication(self, batch):
        row = self.db.execute('SELECT state FROM publications WHERE id=?', (batch,)).fetchone()
        if row is None or row[0] != 1:
            return
        for table in (*KINDS, 'owner_versions', 'seen', 'retired', 'owner_heads', 'owner_retired', *CHECKPOINT_TABLES):
            if self.db.execute(f'SELECT 1 FROM {table} WHERE batch=? LIMIT 1', (batch,)).fetchone():
                return
        self.db.execute('DELETE FROM publications WHERE id=?', (batch,))

    def _prune_owner(self, owner):
        for table in (*KINDS, 'owner_versions', 'owner_heads', 'seen'):
            if self.db.execute(f'SELECT 1 FROM {table} WHERE owner=? LIMIT 1', (owner,)).fetchone():
                return
        self.db.execute('DELETE FROM owners WHERE id=?', (owner,))

    def close(self):
        if self.db is not None:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.__exit__(None, None, None)
            self.lock = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Stage:
    def __init__(self, store, batch, expected, source):
        self.store, self.db, self.limits = store, store.db, store.limits
        self.batch, self.expected, self.source = batch, expected, source
        self.owners = {}
        self.reconciled = set()
        self.reconciling = set()
        self.empty_reconciliations = set()
        self.context_changed = set()
        self.facts_started = set()
        self.checkpoint_namespaces = set()
        self.checkpoint_totals = None
        self.failed = False
        self.finished = False
        self.writes = dict.fromkeys((*KINDS, 'owners', 'derived', 'seen', 'checkpoint_chunks', 'checkpoint_sections'), 0)

    @property
    def prospective_token(self):
        self._live()
        return Token(self.expected.instance, self.expected.generation + 1)

    @contextmanager
    def read_snapshot(self, *, cancel=None, seconds=None):
        """Trusted writer view; the public reader still rejects incomplete data."""
        self._live()
        with self.store.read_current(staged=True, cancel=cancel, seconds=seconds) as snapshot:
            if snapshot.staged_batch != self.batch or snapshot.publication_token != self.prospective_token:
                raise StaleGeneration('staging generation changed before its classifier read')
            yield snapshot

    def derived(self, owner, *, summary=UNCHANGED, choices=UNCHANGED,
                model_alerts=UNCHANGED, checkpoint=UNCHANGED):
        """Finalize only derived packets after preparing facts; joins stay fixed."""
        owner = self._owner(owner)
        packets = {name:pack(value,self.limits) for name,value in
                   zip(DERIVED,(summary,choices,model_alerts,checkpoint)) if value is not UNCHANGED}
        if not packets:
            return
        with self.store._transaction():
            self._charge(sum(map(len,packets.values())))
            self.db.execute('UPDATE owner_versions SET '+','.join(name+'=?' for name in packets)+
                            ' WHERE owner=? AND batch=?',(*packets.values(),owner,self.batch))
        self.writes['derived'] += 1

    def _live(self):
        if self.finished or self.store.stage is not self:
            raise IncompletePublication('staging batch is closed')

    def _charge(self, amount):
        current = self.db.execute('SELECT input_bytes FROM publications WHERE id=?', (self.batch,)).fetchone()[0]
        if current + amount > self.limits.staging_bytes:
            raise CapacityExceeded('staging payload and inventory budget exceeded')
        self.db.execute('UPDATE publications SET input_bytes=input_bytes+? WHERE id=?', (amount, self.batch))

    def owner(self, home, sid, metadata, dependencies, *, source_ordinal=None, request_maps=UNCHANGED, call_metadata=UNCHANGED, replace=False, summary=UNCHANGED,
              choices=UNCHANGED, model_alerts=UNCHANGED, checkpoint=UNCHANGED):
        self._live()
        if type(home) is not str or type(sid) is not str:
            raise TypeError('owner identity must be the exact source home and session strings')
        pack((home, sid), self.limits, maximum=self.limits.key_bytes)
        if FORBIDDEN_OWNER.intersection(metadata):
            raise TypeError('owner metadata must not contain retained history populations')
        if any(name not in dependencies for name in DEPENDENCIES):
            raise TypeError('all source/pricing/mode/ownership/classifier/preparation dependencies are required')
        digest = dependencies['source_digest']
        if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise TypeError('source_digest must be the canonical source body SHA256')
        # Older prepared owners retained growing source transport diagnostics.
        # Calls already carry their resolved transport; keep legacy restaging
        # within the same packet budget without copying those unused arrays.
        metadata = {name:value for name,value in metadata.items() if name not in RAW_TRANSPORT_FIELDS}
        metadata_packet, dependency_packet = (pack(v, self.limits) for v in (metadata, dependencies))
        key = (home, sid)
        if key not in self.owners and len(self.owners) >= self.limits.owners_per_batch:
            raise CapacityExceeded('owner batch count exceeded')
        replacing = key in self.owners
        if replacing and (not replace or self.owners[key] in self.facts_started):
            raise IncompletePublication('owner replacement is allowed only before non-candidate facts begin')
        with self.store._transaction():
            self.db.execute('INSERT OR IGNORE INTO owners(home,sid) VALUES(?,?)', key)
            owner = self.db.execute('SELECT id FROM owners WHERE home=? AND sid=?', key).fetchone()[0]
            prior = (self.db.execute('SELECT * FROM owner_versions WHERE owner=? AND batch=?',
                                    (owner,self.batch)).fetchone() if replacing else
                     self.db.execute('SELECT v.* FROM owner_heads h JOIN owner_versions v '
                                     'ON v.owner=h.owner AND v.batch=h.batch WHERE h.owner=?', (owner,)).fetchone())
            if source_ordinal is None and prior:
                source_ordinal = prior['source_ordinal']
            if type(source_ordinal) is not int or not 0 <= source_ordinal < 2 ** 63:
                raise TypeError('new owners require their exact nonnegative source traversal ordinal')
            if request_maps is UNCHANGED:
                request_mask = prior['request_mask'] if prior else 0
            else:
                request_maps = tuple(request_maps)
                if any(name not in ('turn_records', 'turn_states') for name in request_maps):
                    raise ValueError('request map presence contains an unknown source field')
                request_mask = sum(1 << i for i,name in enumerate(('turn_records','turn_states')) if name in request_maps)
            call_values = ({**metadata, **unpack(prior['call_metadata'], self.limits)}
                           if call_metadata is UNCHANGED and prior else metadata
                           if call_metadata is UNCHANGED or call_metadata is None else call_metadata)
            joined = dict(call_values, home=home, sid=sid)
            overrides = {name:value for name,value in joined.items() if name in OWNER_FIELDS and
                         (name not in metadata or pack(value, self.limits) != pack(metadata[name], self.limits))}
            call_packet = pack(overrides, self.limits)
            if prior:
                before = {**unpack(prior['metadata'], self.limits), **unpack(prior['call_metadata'], self.limits)}
                presence_changed = any((name in before) != (name in joined) for name in OWNER_FIELDS)
                lineage_changed = any((name in before) != (name in joined) or
                    (name in before and pack(before[name], self.limits) != pack(joined[name], self.limits))
                    for name in LINEAGE)
                if presence_changed or lineage_changed:
                    self.context_changed.add(owner)
            derived = (summary, choices, model_alerts, checkpoint)
            packets = (metadata_packet, call_packet, dependency_packet, *(prior[name] if value is UNCHANGED and prior else
                       pack(None if value is UNCHANGED else value, self.limits)
                       for name, value in zip(DERIVED, derived)))
            self._charge(sum(map(len, packets)))
            self.db.execute('INSERT OR REPLACE INTO owner_versions VALUES(?,?,0,?,?,?,?,?,?,?,?,?,?)',
                            (owner, self.batch, source_ordinal, request_mask, int(bool(metadata.get('_collection_pending'))), *packets))
            if prior and prior['batch'] < self.batch:
                self.db.execute('INSERT OR IGNORE INTO retired VALUES(?,?,?,?)', ('owner', owner, b'', self.batch))
        self.owners[key] = owner
        self.writes['owners'] += 1
        return owner

    def _owner(self, key):
        self._live()
        if key not in self.owners:
            raise IncompletePublication('owner metadata/dependencies must be declared first')
        return self.owners[key]

    def _call(self, owner, row, metadata):
        row = dict(compact_record(row))
        key = identity(row['key'], self.limits)
        key_value = pack(row.pop('key'), self.limits, maximum=self.limits.key_bytes)
        timestamp = _number(row['ts'], 'ts')
        ordinal = row['session_ordinal']
        if type(ordinal) is not int or not 0 <= ordinal < 2 ** 63:
            raise TypeError('session_ordinal must be a nonnegative exact integer')
        turn_value = pack(row.pop('turn'), self.limits, maximum=self.limits.key_bytes) if 'turn' in row else None
        turn = unpack(turn_value, self.limits, maximum=self.limits.key_bytes) if turn_value else None
        turn_key = identity(turn, self.limits) if turn else None
        mask = 0
        for i, name in enumerate(OWNER_FIELDS):
            if name not in row:
                # A second bit bank distinguishes absence from a per-call
                # override, so projections need not decode an absent field.
                mask |= 1 << (i+len(OWNER_FIELDS))
            elif name in metadata and pack(row[name], self.limits) == pack(metadata[name], self.limits):
                row.pop(name)
                mask |= 1 << i
        tags, values = [], []
        for name in METRICS:
            tag, value = _scalar(row.get(name), name in row)
            tags.append(tag)
            values.append(value)
            row.pop(name, None)
        detail = pack(row, self.limits)
        types = bytes(tags)
        encoded = pack((key_value, turn_value, mask, types, values, detail), self.limits)
        # timestamp/ordinal are columns already; this check guards future field drift.
        assert values[0] == timestamp and values[1] == ordinal
        signature = hashlib.sha256(encoded).digest()
        return (owner, key, self.batch, 0, signature, key_value, turn_value, turn_key,
                mask, types, _detail_pack(detail), len(encoded), *values)

    def _request(self, owner, item):
        key, ordinal, metadata = item
        if type(ordinal) is not int or not 0 <= ordinal < 2 ** 63:
            raise TypeError('request ordinal must be a nonnegative exact integer')
        detail = dict(metadata)
        tags, values = [], []
        for field in ('started_at', 'ended_at'):
            value = detail.get(field)
            if value is not None:
                _number(value, field)
            tag, stored = _scalar(value, field in detail)
            tags.append(tag)
            values.append(stored)
            detail.pop(field, None)
        key_value = pack(key, self.limits, maximum=self.limits.key_bytes)
        packet = pack(detail, self.limits)
        encoded = pack((key_value, ordinal, bytes(tags), values, packet), self.limits)
        return (owner, identity(key, self.limits), self.batch, 0, hashlib.sha256(encoded).digest(),
                key_value, ordinal, *values, bytes(tags), _detail_pack(packet), len(encoded))

    def _candidate(self, owner, item):
        key, ordinal = item
        if type(ordinal) is not int or not 0 <= ordinal < 2 ** 63:
            raise TypeError('candidate source ordinal must be an exact nonnegative integer')
        key_value = pack(key, self.limits, maximum=self.limits.key_bytes)
        encoded = pack((key_value, ordinal), self.limits)
        return (owner, identity(key, self.limits), self.batch, 0, hashlib.sha256(encoded).digest(),
                key_value, ordinal, int(str(key).startswith(('record:', 'legacy:'))), len(encoded))

    def _header(self, owner, item):
        """Store caller-computed request facts; never reconstruct them during a query."""
        key, facts = item
        ordinal, count = facts['display_ordinal'], facts['call_count']
        if type(ordinal) is not int or ordinal < 0 or type(count) is not int or count < 0:
            raise TypeError('request display ordinal/count must be exact nonnegative integers')
        at = facts['effective_at']
        if ordinal == 0 and (count or facts['request_present'] or at is not None or 'turn_state' not in facts):
            raise TypeError('ordinal zero is reserved for source state-only entries')
        minimum, maximum = facts['minimum'], facts['maximum']
        for name, value in (('effective_at', at), ('minimum', minimum), ('maximum', maximum)):
            if value is not None:
                _number(value, name)
        if count and (minimum is None or maximum is None or minimum > maximum):
            raise TypeError('populated request needs exact first/last timestamps')
        if not count and any(v is not None for v in (minimum, maximum)):
            raise TypeError('empty request cannot have call timestamps')
        dimensions = facts['dimensions']
        if set(dimensions) != {'model', 'effort', 'service_tier'}:
            raise TypeError('request facts require all three exact dimensions')
        first = identity(facts['first_key'], self.limits) if count else None
        state_present = 'turn_state' in facts
        state = pack(facts['turn_state'], self.limits) if state_present else None
        key_value = pack(key, self.limits, maximum=self.limits.key_bytes)
        dimensions = pack(dimensions, self.limits)
        values = (key_value, ordinal, at, count, minimum, maximum, first, dimensions,
                  state, int(state_present), int(bool(facts['request_present'])))
        encoded = pack(values, self.limits)
        return (owner, identity(key, self.limits), self.batch, 0, hashlib.sha256(encoded).digest(),
                *values, len(encoded))

    def _section(self, owner, item):
        section,key,ordinal,at,value = item
        if type(ordinal) is not int or not 0 <= ordinal < 2 ** 63:
            raise TypeError('section ordinal must be an exact nonnegative integer')
        if at is not None:
            _number(at,'section timestamp')
        tag,stored_at = _scalar(at,True)
        section_value = pack(section,self.limits,maximum=self.limits.key_bytes)
        key_value = pack(key,self.limits,maximum=self.limits.key_bytes)
        detail = pack(value,self.limits)
        prefix = pack((section_value,key_value,ordinal,tag,stored_at),self.limits)
        signature = hashlib.sha256(len(prefix).to_bytes(8,'big')+prefix+detail).digest()
        return (owner,identity((section,key),self.limits),self.batch,0,signature,
                identity(section,self.limits),section_value,key_value,ordinal,stored_at,tag,
                _detail_pack(detail),len(prefix)+len(detail))

    def start_reconcile(self, owner_key, kind):
        if kind not in KINDS:
            raise ValueError('unknown fact kind')
        owner = self._owner(owner_key)
        if kind != 'candidates':
            self.facts_started.add(owner)
        pair = (owner, kind)
        if pair in self.reconciling or pair in self.reconciled:
            raise IncompletePublication('one complete inventory is allowed per owner and batch')
        self.reconciling.add(pair)
        # A first inventory has nothing to withdraw. Its fact primary keys
        # already provide duplicate detection, so a second identity inventory
        # and its later cleanup would only repeat the same writes.
        if self.db.execute(f'SELECT 1 FROM {kind} WHERE owner=? LIMIT 1', (owner,)).fetchone() is None:
            self.empty_reconciliations.add(pair)

    def _fact_groups(self, kind, items, encode):
        records=[]; encoded=0; objects=0
        for item in items:
            record=encode(item)
            size=record[11] if kind=='calls' else record[-1]
            retained=_size(record)
            if size>self.limits.fact_write_bytes or retained>self.limits.fact_write_objects:
                raise CapacityExceeded('one encoded fact exceeds its transaction budget')
            if records and (len(records)>=self.limits.fact_write_rows or
                            encoded+size>self.limits.fact_write_bytes or
                            objects+retained>self.limits.fact_write_objects):
                yield records
                records=[];encoded=0;objects=0
            records.append(record);encoded+=size;objects+=retained
        if records:yield records

    def _upsert(self, kind, owner_key, items, *, reconcile):
        owner = self._owner(owner_key)
        if kind != 'candidates':
            self.facts_started.add(owner)
        if reconcile:
            self.start_reconcile(owner_key, kind)
        track_inventory = (owner, kind) in self.reconciling
        fresh = track_inventory and (owner, kind) in self.empty_reconciliations
        metadata = None
        if kind == 'calls':
            row = self.db.execute('SELECT metadata,call_metadata FROM owner_versions WHERE owner=? AND batch=?',
                                  (owner, self.batch)).fetchone()
            metadata = {**unpack(row[0], self.limits), **unpack(row[1], self.limits)}
        encode = ((lambda row: self._call(owner, row, metadata)) if kind == 'calls' else
                  (lambda row: self._request(owner, row)) if kind == 'requests' else
                  (lambda row: self._header(owner, row)) if kind == 'headers' else
                  (lambda row: self._section(owner, row)) if kind == 'sections' else
                  (lambda row: self._candidate(owner, row)))
        try:
            for records in self._fact_groups(kind, items, encode):
                args = dict(owner=owner, batch=self.batch)
                args.update((f'key{i}', record[1]) for i, record in enumerate(records))
                placeholders = ','.join(f':key{i}' for i in range(len(records)))
                previous = {} if fresh else {r['identity']: (r['signature'], r['deleted'], r['batch']) for r in self.db.execute(
                    f'SELECT r.identity,r.signature,r.deleted,r.batch FROM {kind} r JOIN publications p ON p.id=r.batch '
                    f'WHERE r.owner=:owner AND r.identity IN({placeholders}) AND {_latest(kind, staged=True)}', args)}
                changed = [r for r in records if previous.get(r[1], ())[:2] != (r[4], 0)]
                retire = [(kind, owner, r[1], self.batch) for r in changed
                          if r[1] in previous and previous[r[1]][2] < self.batch]
                inventory = [(self.batch, kind, owner, r[1]) for r in records] if track_inventory and not fresh else []
                charge = sum(r[11] if kind == 'calls' else r[-1] for r in changed)
                charge += sum(len(r[3]) + 32 for r in inventory)
                with self.store._transaction():
                    self._charge(charge)
                    if inventory:
                        self.db.executemany('INSERT INTO seen VALUES(?,?,?,?)', inventory)
                    if changed:
                        insertion='INSERT' if fresh else 'INSERT OR REPLACE'
                        self.db.executemany(f'{insertion} INTO {kind} VALUES({",".join("?" for _ in changed[0])})', changed)
                        self.db.executemany('INSERT OR IGNORE INTO retired VALUES(?,?,?,?)', retire)
                self.writes[kind] += len(changed)
                self.writes['seen'] += len(inventory)
            if reconcile:
                self.finish_reconcile(owner_key, kind)
        except BaseException:
            self.failed = True
            raise

    def finish_reconcile(self, owner_key, kind):
        owner = self._owner(owner_key)
        if (owner, kind) not in self.reconciling:
            raise IncompletePublication('start reconciliation before finishing it')
        if (owner,kind) in self.empty_reconciliations:
            self.reconciled.add((owner,kind))
            self.reconciling.remove((owner,kind))
            self.empty_reconciliations.remove((owner,kind))
            return
        try:
            # Only verified source EOF permits the caller to finish this inventory.
            cursor = None
            while True:
                args = dict(owner=owner, batch=self.batch, kind=kind, after=cursor or b'', n=self.limits.write_rows)
                missing = self.db.execute(f'SELECT r.identity FROM {kind} r JOIN publications p ON p.id=r.batch '
                    f'WHERE r.owner=:owner AND r.deleted=0 AND r.identity>:after AND {_latest(kind, staged=True)} '
                    'AND NOT EXISTS(SELECT 1 FROM seen s WHERE s.batch=:batch AND s.kind=:kind '
                    'AND s.owner=r.owner AND s.identity=r.identity) ORDER BY r.identity LIMIT :n', args).fetchall()
                if not missing:
                    break
                self._delete_keys(kind, owner, [r[0] for r in missing])
                cursor = missing[-1][0]
            self.reconciled.add((owner, kind))
            self.reconciling.remove((owner, kind))
        except BaseException:
            self.failed = True
            raise

    def calls(self, owner, rows, *, reconcile=False):
        self._upsert('calls', owner, rows, reconcile=reconcile)

    def requests(self, owner, rows, *, reconcile=False):
        """Rows are (request identity, original request ordinal, boundary metadata)."""
        self._upsert('requests', owner, rows, reconcile=reconcile)

    def candidates(self, owner, rows, *, reconcile=False):
        """Provenance refs only: (record key, original source ordinal), no losing payload."""
        self._upsert('candidates', owner, rows, reconcile=reconcile)

    def headers(self, owner, rows, *, reconcile=False):
        """Rows are (turn, exact facts). Explicit and implicit turns both need headers."""
        self._upsert('headers', owner, rows, reconcile=reconcile)

    def sections(self, owner, rows, *, reconcile=False):
        """Rows are (section key,item key,ordinal,optional timestamp,exact value)."""
        self._upsert('sections',owner,rows,reconcile=reconcile)

    def _checkpoint_previous(self, namespace, name):
        return self.db.execute('SELECT r.* FROM checkpoint_sections r JOIN publications p ON p.id=r.batch '
            f'WHERE r.namespace=? AND r.name=? AND {_checkpoint_latest(batch=self.batch)}',
            (namespace,name)).fetchone()

    def _checkpoint_budget(self, namespace, total, count):
        totals = dict(self.checkpoint_totals)
        totals[namespace] = (total,count)
        if (total < 0 or count < 0 or sum(value[0] for value in totals.values()) > self.limits.checkpoint_bytes or
                sum(value[1] for value in totals.values()) > self.limits.checkpoint_sections or
                len(totals) > self.limits.checkpoint_namespaces):
            raise CapacityExceeded('merged checkpoint exceeds its global byte/section/namespace budget')

    def _checkpoint_tombstone(self, namespace, name):
        previous = self._checkpoint_previous(namespace,name)
        if previous is None or previous['deleted']:
            return 0,0
        with self.store._transaction():
            self._charge(len(name.encode('utf-8'))+128)
            self.db.execute('INSERT INTO checkpoint_sections(namespace,name,batch,deleted) VALUES(?,?,?,1)',
                            (namespace,name,self.batch))
            self.db.execute('INSERT OR IGNORE INTO checkpoint_retired VALUES(?,?,?)',(namespace,name,self.batch))
        self.writes['checkpoint_sections'] += 1
        return previous['encoded_bytes'],1

    def checkpoint(self, namespace, sections, *, base, token, tombstones=(), complete=False):
        """Stage a bounded namespace delta; no codec import or synthetic owner.

        sections yields (name,content SHA256,tuple of byte chunks). The caller's
        header codec binds source and classifier semantics. This layer binds the
        exact prepared generation and the merged durable payload budget.
        """
        self._live()
        try:
            namespace = _checkpoint_name(namespace)
            if namespace in self.checkpoint_namespaces:
                raise IncompletePublication('one checkpoint replacement per namespace and stage is allowed')
            self.checkpoint_namespaces.add(namespace)
            token = _portable_token(token)
            if token != _portable_token(self.prospective_token):
                raise StaleGeneration('checkpoint targets a different prepared publication')
            if type(complete) is not bool:
                raise TypeError('checkpoint completeness must be an exact boolean')
            head = self.db.execute('SELECT v.* FROM checkpoint_heads h JOIN checkpoint_versions v '
                'ON v.namespace=h.namespace AND v.batch=h.batch WHERE h.namespace=?',(namespace,)).fetchone()
            previous_token = unpack(head['token'],self.limits) if head else None
            if base is not None:
                base = _portable_token(base)
            if (not complete and (head is None or base != previous_token)) or (complete and base is not None and base != previous_token):
                raise StaleGeneration('checkpoint delta base is no longer current')
            if self.checkpoint_totals is None:
                self.checkpoint_totals = {row['namespace']:(row['total_bytes'],row['section_count']) for row in self.db.execute(
                    'SELECT v.namespace,v.total_bytes,v.section_count FROM checkpoint_heads h '
                    'JOIN checkpoint_versions v ON v.namespace=h.namespace AND v.batch=h.batch')}
            total,count = (0,0) if complete else self.checkpoint_totals.get(namespace,(0,0))
            self._checkpoint_budget(namespace,total,count)
            header = False
            input_bytes = input_sections = 0
            for name,digest,chunks in sections:
                name = _checkpoint_name(name)
                if (type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest) or
                        type(chunks) is not tuple or not chunks or
                        len(chunks) > (self.limits.checkpoint_bytes+self.limits.checkpoint_chunk_bytes-1)//self.limits.checkpoint_chunk_bytes):
                    raise TypeError('checkpoint section needs a digest and bounded chunk tuple')
                content,layout = hashlib.sha256(),hashlib.sha256()
                size = 0
                for ordinal,chunk in enumerate(chunks):
                    if type(chunk) is not bytes or not 0 < len(chunk) <= self.limits.checkpoint_chunk_bytes:
                        raise CapacityExceeded('checkpoint chunk exceeds its byte bound')
                    if ordinal+1 < len(chunks) and len(chunk) != self.limits.checkpoint_chunk_bytes:
                        raise TypeError('checkpoint chunks except the last must fill the declared chunk size')
                    size += len(chunk)
                    if size > self.limits.checkpoint_bytes:
                        raise CapacityExceeded('checkpoint section exceeds its global byte budget')
                    content.update(chunk);layout.update(len(chunk).to_bytes(8,'big'));layout.update(chunk)
                if content.hexdigest() != digest:
                    raise RebuildRequired('checkpoint section digest does not match its chunks')
                input_bytes += size
                input_sections += 1
                if input_bytes > self.limits.checkpoint_bytes or input_sections > self.limits.checkpoint_sections:
                    raise CapacityExceeded('checkpoint input exceeds its bounded encoded inventory')
                signature = layout.digest()
                previous = self._checkpoint_previous(namespace,name)
                old_size = previous['encoded_bytes'] if previous is not None and not previous['deleted'] else 0
                old_count = int(previous is not None and not previous['deleted'])
                total,count = (total+size,count+1) if complete else (total-old_size+size,count-old_count+1)
                with self.store._transaction():
                    self._charge(len(name.encode('utf-8'))+64)
                    self.db.execute('INSERT INTO checkpoint_seen VALUES(?,?,?)',(self.batch,namespace,name))
                header |= name == 'header'
                if previous is not None and not previous['deleted'] and previous['signature'] == signature:
                    continue
                with self.store._transaction():
                    self._charge(size+len(name.encode('utf-8'))+128)
                    self.db.execute('INSERT INTO checkpoint_sections VALUES(?,?,?,0,?,?,?,?)',
                        (namespace,name,self.batch,digest,signature,len(chunks),size))
                    if previous is not None:
                        self.db.execute('INSERT OR IGNORE INTO checkpoint_retired VALUES(?,?,?)',(namespace,name,self.batch))
                for group in _chunks(enumerate(chunks),self.limits.write_rows):
                    with self.store._transaction():
                        self.db.executemany('INSERT INTO checkpoint_chunks VALUES(?,?,?,?,?)',
                            ((namespace,name,self.batch,ordinal,payload) for ordinal,payload in group))
                    self.writes['checkpoint_chunks'] += len(group)
                self.writes['checkpoint_sections'] += 1
            if not header:
                raise IncompletePublication('every checkpoint publication requires its new header')
            for name in tombstones:
                name = _checkpoint_name(name)
                if name == 'header':
                    raise IncompletePublication('checkpoint header cannot be removed')
                with self.store._transaction():
                    self._charge(len(name.encode('utf-8'))+64)
                    self.db.execute('INSERT INTO checkpoint_seen VALUES(?,?,?)',(self.batch,namespace,name))
                removed,number = self._checkpoint_tombstone(namespace,name)
                if not complete:
                    total,count = total-removed,count-number
            if complete:
                after = ''
                while True:
                    missing = self.db.execute('SELECT r.name FROM checkpoint_sections r JOIN publications p ON p.id=r.batch '
                        f'WHERE r.namespace=? AND r.name>? AND r.deleted=0 AND {_checkpoint_latest(batch=self.batch)} '
                        'AND NOT EXISTS(SELECT 1 FROM checkpoint_seen s WHERE s.batch=? AND s.namespace=r.namespace AND s.name=r.name) '
                        'ORDER BY r.name LIMIT ?',(namespace,after,self.batch,self.limits.write_rows)).fetchall()
                    if not missing:
                        break
                    for row in missing:
                        self._checkpoint_tombstone(namespace,row[0])
                    after = missing[-1][0]
            self._checkpoint_budget(namespace,total,count)
            with self.store._transaction():
                self._charge(256+len(namespace.encode('utf-8')))
                self.db.execute('INSERT INTO checkpoint_versions VALUES(?,?,?,?,?)',
                                (namespace,self.batch,pack(token,self.limits),total,count))
            self.checkpoint_totals[namespace] = (total,count)
        except BaseException:
            self.failed = True
            raise

    def _delete_keys(self, kind, owner, keys):
        with self.store._transaction():
            for key in keys:
                self._charge(len(key) + 64)
                self.db.execute(f'INSERT OR REPLACE INTO {kind}(owner,identity,batch,deleted,signature) '
                                'VALUES(?,?,?,1,?)', (owner, key, self.batch, b''))
                self.db.execute('INSERT OR IGNORE INTO retired VALUES(?,?,?,?)', (kind, owner, key, self.batch))
                self.writes[kind] += 1

    def delete(self, kind, owner, keys):
        if kind not in KINDS:
            raise ValueError('unknown fact kind')
        owner = self._owner(owner)
        if kind != 'candidates':
            self.facts_started.add(owner)
        for group in _chunks((identity(k, self.limits) for k in keys), self.limits.write_rows):
            self._delete_keys(kind, owner, group)

    def remove_owner(self, owner):
        owner_id = self._owner(owner)
        for kind in KINDS:
            self._upsert(kind, owner, (), reconcile=True)
        with self.store._transaction():
            self.db.execute('UPDATE owner_versions SET deleted=1 WHERE owner=? AND batch=?', (owner_id, self.batch))
            self.db.execute('INSERT INTO owner_retired VALUES(?,?)', (owner_id, self.batch))

    def publish(self, source_identity, *, ready):
        self._live()
        if self.failed or self.reconciling:
            raise IncompletePublication('a failed or unfinished inventory cannot publish')
        if pack(source_identity, self.limits) != self.source:
            raise StaleGeneration('source identity changed during preparation')
        if self.store.token != self.expected:
            raise StaleGeneration('prepared generation changed during preparation')
        if any((owner, 'calls') not in self.reconciled for owner in self.context_changed):
            raise IncompletePublication('owner join context changed: complete call reconciliation is required')
        published = self.prospective_token
        try:
            with self.store._transaction():
                for owner in self.owners.values():
                    self.db.execute('INSERT INTO owner_heads VALUES(?,?) ON CONFLICT(owner) '
                                    'DO UPDATE SET batch=excluded.batch', (owner, self.batch))
                for namespace in self.checkpoint_namespaces:
                    previous = self.db.execute('SELECT batch FROM checkpoint_heads WHERE namespace=?',(namespace,)).fetchone()
                    self.db.execute('INSERT INTO checkpoint_heads VALUES(?,?) ON CONFLICT(namespace) '
                                    'DO UPDATE SET batch=excluded.batch',(namespace,self.batch))
                    if previous is not None:
                        self.db.execute('DELETE FROM checkpoint_versions WHERE namespace=? AND batch=?',(namespace,previous[0]))
                        self.store._prune_publication(previous[0])
                self.db.execute('UPDATE publications SET state=1 WHERE id=?', (self.batch,))
                self.db.execute('UPDATE meta SET generation=generation+1,ready=?,source=? WHERE singleton=1',
                                (int(ready), self.source))
                self.store._prune_publication(self.batch)
        except CapacityExceeded:
            # A WAL pressure check may run after COMMIT. Durable publication is
            # success; the next transaction still has to pass WAL admission.
            if self.store.token != published:
                raise
        finally:
            if self.store.token == published:
                self.finished = True
                self.store.stage = None
        return published

    def abort(self):
        self._live()
        with self.store._transaction():
            self.db.execute('UPDATE publications SET state=-1 WHERE id=? AND state=0', (self.batch,))
        self.finished = True
        self.store.stage = None


class PreparedReader:
    """Open one connection per query actor; it never migrates or mutates the store."""
    def __init__(self, path, limits=Limits()):
        self.path, self.limits = Path(path).resolve(), limits
        self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.2, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.active = None
        try:
            self.db.execute('PRAGMA temp_store=FILE')
            self.db.execute('PRAGMA temp.cache_size=-4096')
            self.db.execute('PRAGMA query_only=ON')
            self.db.execute('PRAGMA cache_size=-4096')
            self.db.execute('PRAGMA mmap_size=0')
            if self.db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
                raise RebuildRequired('reader opened a different database type')
        except BaseException:
            self.close()
            raise

    def pin(self, expected=None, *, cancel=None, seconds=None):
        return self._pin(expected, cancel=cancel, seconds=seconds)

    def _pin(self, expected=None, *, cancel=None, seconds=None, allow_incomplete=False, staged_batch=None):
        if self.active is not None:
            raise PreparedError('close the existing read transaction before opening another')
        snapshot = Snapshot(self, expected, cancel, seconds, allow_incomplete, staged_batch)
        self.active = snapshot
        return snapshot

    def close(self):
        if self.active is not None:
            self.active.close()
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _read_operation(function):
    @wraps(function)
    def guarded(self, *args, **kwargs):
        try:
            self._check()
            return function(self, *args, **kwargs)
        except BaseException:
            self.close()
            raise
    return guarded


class Snapshot:
    def __init__(self, reader, expected, cancel, seconds, allow_incomplete=False, staged_batch=None):
        self.reader, self.db, self.limits = reader, reader.db, reader.limits
        self.cancel = cancel.is_set if cancel is not None and not callable(cancel) else cancel
        self.deadline = time.monotonic() + min(seconds or self.limits.read_seconds, self.limits.read_seconds)
        self.closed = False
        self.staged_batch = staged_batch
        self.speed_active = False
        self.speed_cursor = None
        self.public = not allow_incomplete and staged_batch is None
        self.steps = 0
        try:
            self.db.execute('BEGIN')
            row = self.db.execute('SELECT * FROM meta WHERE singleton=1').fetchone()
            if row is None or row['format'] != FORMAT:
                raise RebuildRequired('prepared schema version changed')
            self.token = Token(row['instance'], row['generation'])
            self.publication_token = (Token(self.token.instance,self.token.generation+1)
                                      if staged_batch is not None else self.token)
            if expected is not None and expected != self.token:
                raise StaleGeneration('requested detail generation is no longer current')
            if not row['ready'] and not allow_incomplete:
                raise NotReady('prepared conversion has not been published complete')
            source = row['source']
            if staged_batch is not None:
                staged = self.db.execute('SELECT source,state FROM publications WHERE id=?',(staged_batch,)).fetchone()
                if staged is None or staged['state'] != 0:
                    raise StaleGeneration('staging publication is no longer active')
                source = staged['source']
            self.source_identity = unpack(source, self.limits)
            self.db.set_progress_handler(self._progress, 1000)
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _progress(self):
        self.steps += 1000
        return int(self._expired() or self.steps > self.limits.read_vm_steps)

    def _visible(self, kind, alias='r', publication='p'):
        return _latest(kind, alias, staged=self.staged_batch is not None,
                       batch=self.staged_batch, publication=publication)

    def _owner_tables(self):
        if self.staged_batch is None:
            return ('owners o JOIN owner_heads h ON h.owner=o.id '
                    'JOIN owner_versions v ON v.owner=h.owner AND v.batch=h.batch')
        return ('owners o LEFT JOIN owner_heads h ON h.owner=o.id '
                'JOIN owner_versions v ON v.owner=o.id AND v.batch=COALESCE('
                f'(SELECT x.batch FROM owner_versions x WHERE x.owner=o.id AND x.batch={int(self.staged_batch)}),h.batch)')

    def _expired(self):
        return time.monotonic() >= self.deadline or self.cancel is not None and self.cancel()

    def _check(self):
        if self.closed:
            raise ReadCancelled('read transaction is closed')
        if self._expired():
            self.close()
            raise ReadCancelled('read deadline or cancellation reached')

    @contextmanager
    def _cursor(self, sql, args=()):
        self._check()
        self.steps = 0
        cursor = None
        try:
            cursor = self.db.execute(sql, args)
            yield cursor
        except sqlite3.OperationalError as error:
            if 'interrupted' in str(error):
                self.close()
                raise ReadCancelled('read deadline, cancellation, or VM-step budget reached') from error
            raise
        finally:
            if cursor is not None:
                cursor.close()

    def _owner(self, key, *, metadata=True):
        columns = 'o.id,v.source_ordinal,v.request_mask,v.metadata,v.call_metadata,v.dependencies' if metadata else 'o.id'
        with self._cursor(f'SELECT {columns} FROM {self._owner_tables()} '
                          'WHERE o.home=? AND o.sid=? AND v.deleted=0', key) as cursor:
            row = cursor.fetchone()
        return row

    @_read_operation
    def owner(self, home, sid):
        row = self._owner((home, sid))
        if row is None:
            return None
        return dict(owner_ordinal=row['source_ordinal'], request_maps=self._request_maps(row), **{
            name: unpack(row[name], self.limits) for name in ('metadata', 'call_metadata', 'dependencies')})

    @staticmethod
    def _request_maps(row):
        return tuple(name for i,name in enumerate(('turn_records','turn_states')) if row['request_mask'] & (1 << i))

    def _call_metadata(self, row):
        return {**unpack(row['metadata'], self.limits), **unpack(row['call_metadata'], self.limits)}

    @_read_operation
    def derived(self, home, sid, name):
        if name not in DERIVED:
            raise ValueError('unknown derived owner value')
        with self._cursor(f'SELECT v.{name} FROM {self._owner_tables()} '
                          'WHERE o.home=? AND o.sid=? AND v.deleted=0', (home, sid)) as cursor:
            row = cursor.fetchone()
        return unpack(row[0], self.limits) if row else None

    def _page(self, sql, args, decode, cursor_key, encoded_size):
        rows, size, objects, after, complete = [], 0, 0, None, True
        with self._cursor(sql, args) as cursor:
            for raw in cursor:
                self._check()
                value = decode(raw)
                n, m = encoded_size(raw), _size(value)
                if (len(rows) >= self.limits.page_rows or size + n > self.limits.page_bytes or
                        objects + m > self.limits.page_objects_bytes):
                    if not rows:
                        raise CapacityExceeded('one result exceeds the delivered-page budget')
                    complete = False
                    break
                rows.append(value)
                size += n
                objects += m
                after = cursor_key(raw)
        return Page(tuple(rows), after, complete, size, objects)

    @_read_operation
    def inventory(self, *, after=None):
        """Conversion inventory excludes public metadata and classifier arrays."""
        args = dict(ordinal=after[0] if after else 0,owner=after[1] if after else 0,n=self.limits.page_rows+1)
        where = '(v.source_ordinal,v.owner)>(:ordinal,:owner)' if after is not None else '1'
        sql = (f'SELECT o.home,o.sid,v.owner,v.source_ordinal,v.dependencies FROM {self._owner_tables()} '
               f'WHERE v.deleted=0 AND {where} ORDER BY v.source_ordinal,v.owner LIMIT :n')
        return self._page(sql,args,lambda r:dict(owner=(r['home'],r['sid']),owner_ordinal=r['source_ordinal'],
                          dependencies=unpack(r['dependencies'],self.limits)),
                          lambda r:(r['source_ordinal'],r['owner']),
                          lambda r:len(r['dependencies'])+len(r['home'])*4+len(r['sid'])*4)

    @_read_operation
    def catalog(self, *, after=None):
        args = dict(ordinal=after[0] if after else 0, owner=after[1] if after else 0, n=self.limits.page_rows + 1)
        where = '(v.source_ordinal,v.owner)>(:ordinal,:owner)' if after is not None else '1'
        sql = (f'SELECT o.home,o.sid,v.owner,v.source_ordinal,v.request_mask,v.metadata,v.call_metadata,v.dependencies FROM {self._owner_tables()} '
               f'WHERE v.deleted=0 AND {where} ORDER BY v.source_ordinal,v.owner LIMIT :n')
        return self._page(sql, args,
            lambda r: dict(owner=(r['home'], r['sid']), owner_ordinal=r['source_ordinal'], request_maps=self._request_maps(r), **{name: unpack(r[name], self.limits)
                         for name in ('metadata', 'call_metadata', 'dependencies')}),
            lambda r: (r['source_ordinal'], r['owner']),
            lambda r: sum(len(r[name]) for name in ('metadata', 'call_metadata', 'dependencies')))

    def _decode_call(self, row, metadata, *, metrics=False):
        result = {} if metrics else _detail_unpack(row['detail'], self.limits)
        result['key'] = unpack(row['key_value'], self.limits, maximum=self.limits.key_bytes)
        if row['turn_value'] is not None:
            result['turn'] = unpack(row['turn_value'], self.limits, maximum=self.limits.key_bytes)
        for i, name in enumerate(METRICS):
            tag = row['types'][i]
            if tag:
                result[name] = _restore_scalar(tag, row['m_' + name])
        if not metrics:
            for i, name in enumerate(OWNER_FIELDS):
                if row['owner_mask'] & (1 << i):
                    result[name] = metadata[name]
            return public_record(result)
        return result

    @_read_operation
    def calls(self, owner, *, start=None, end=None, ordinal_start=None, ordinal_end=None,
              turn=None, after=None, metrics=False):
        meta = self._owner(owner, metadata=not metrics)
        if meta is None:
            return Page((), None, True, 0, 0)
        metadata = None if metrics else self._call_metadata(meta)
        args = dict(owner=meta['id'], n=self.limits.page_rows + 1)
        where = ['r.owner=:owner', 'r.deleted=0', self._visible('calls')]
        ordinal = ordinal_start is not None or ordinal_end is not None
        order = ('r.m_session_ordinal,r.identity' if ordinal else 'r.m_ts,r.m_session_ordinal,r.identity')
        index = 'calls_ordinal' if ordinal else 'calls_turn' if turn is not None else 'calls_time'
        for name, value, op in (('start', start, '>='), ('end', end, '<')):
            if value is not None:
                args[name] = _number(value, name)
                where.append(f'r.m_ts{op}:{name}')
        for name, value, op in (('ordinal_start', ordinal_start, '>='), ('ordinal_end', ordinal_end, '<')):
            if value is not None:
                args[name] = value
                where.append(f'r.m_session_ordinal{op}:{name}')
        if turn is not None:
            args['turn'] = identity(turn, self.limits)
            where.append('r.turn_identity=:turn')
        if after is not None:
            names = ('ordinal', 'key') if ordinal else ('ts', 'ordinal', 'key')
            args.update(zip(names, after))
            where.append(f'({order})>({",".join(":" + name for name in names)})')
        columns = ('r.identity,r.key_value,r.turn_value,r.types,r.encoded_bytes,' +
                   ','.join('r.m_' + name for name in METRICS)) if metrics else 'r.*'
        sql = (f'SELECT {columns} FROM calls r INDEXED BY {index} '
               f'JOIN publications p ON p.id=r.batch WHERE {" AND ".join(where)} '
               f'ORDER BY {order} LIMIT :n')
        cursor_key = ((lambda r: (r['m_session_ordinal'], r['identity'])) if ordinal else
                      (lambda r: (r['m_ts'], r['m_session_ordinal'], r['identity'])))
        return self._page(sql, args, lambda r: self._decode_call(r, metadata, metrics=metrics),
                          cursor_key, lambda r: r['encoded_bytes'])

    @_read_operation
    def projected_calls(self, owner, fields, *, start=None, end=None, after=None):
        """A bounded typed projection, with no unrequested detail or key decode.

        Canonical owner defaults are shared by the writer. Rare explicitly
        overridden owner fields retain their exact detail values instead of
        being replaced by those defaults. Presence and scalar types survive.
        """
        if not isinstance(fields,(tuple,list)) or not fields:
            raise ValueError('projection fields must be a nonempty field sequence')
        selected=tuple(dict.fromkeys(fields))
        if any(type(name) is not str or name not in (*METRICS,*OWNER_FIELDS,'key','turn') for name in selected):
            raise ValueError('unknown prepared call projection field')
        owner_fields=tuple(name for name in selected if name in OWNER_FIELDS)
        meta=self._owner(owner,metadata=bool(owner_fields))
        if meta is None:return Page((),None,True,0,0)
        metadata=self._call_metadata(meta) if owner_fields else {}
        mask=sum(1<<OWNER_FIELDS.index(name) for name in owner_fields)
        metric_fields=tuple(name for name in selected if name in METRICS)
        metric_indices={name:METRICS.index(name) for name in metric_fields}
        columns=['r.identity','r.types','r.m_ts','r.m_session_ordinal']
        columns.extend('r.m_'+name for name in metric_fields if name not in ('ts','session_ordinal'))
        if 'key' in selected:columns.append('r.key_value')
        if 'turn' in selected:columns.append('r.turn_value')
        if owner_fields:
            covered=f'(r.owner_mask | (r.owner_mask >> {len(OWNER_FIELDS)}))'
            columns.extend(('r.owner_mask',f'CASE WHEN ({covered} & {mask})={mask} THEN NULL ELSE r.detail END AS override_detail'))
        args=dict(owner=meta['id'],n=self.limits.page_rows+1)
        where=['r.owner=:owner','r.deleted=0',self._visible('calls')]
        for name,value,operation in (('start',start,'>='),('end',end,'<')):
            if value is not None:
                args[name]=_number(value,name);where.append(f'r.m_ts{operation}:{name}')
        if after is not None:
            if not isinstance(after,(tuple,list)) or len(after)!=3:
                raise ValueError('invalid projected call cursor')
            args.update(zip(('ts','ordinal','key'),after))
            where.append('(r.m_ts,r.m_session_ordinal,r.identity)>(:ts,:ordinal,:key)')
        sql=(f'SELECT {",".join(columns)} FROM calls r INDEXED BY calls_time '
             f'JOIN publications p ON p.id=r.batch WHERE {" AND ".join(where)} '
             'ORDER BY r.m_ts,r.m_session_ordinal,r.identity LIMIT :n')
        def decode(row):
            result={}
            for name,index in metric_indices.items():
                tag=row['types'][index]
                if tag:result[name]=_restore_scalar(tag,row['m_'+name])
            for name,column in (('key','key_value'),('turn','turn_value')):
                if name in selected and row[column] is not None:
                    result[name]=unpack(row[column],self.limits,maximum=self.limits.key_bytes)
            if owner_fields:
                detail=_detail_unpack(row['override_detail'],self.limits) if row['override_detail'] is not None else {}
                if detail:self.projected_detail_rows=getattr(self,'projected_detail_rows',0)+1
                for name in owner_fields:
                    if row['owner_mask'] & (1<<OWNER_FIELDS.index(name)):
                        result[name]=metadata[name]
                    elif not row['owner_mask'] & (1<<(OWNER_FIELDS.index(name)+len(OWNER_FIELDS))) and name in detail:
                        result[name]=detail[name]
            return result
        def charge(row):
            return 128+sum(len(value.encode('utf-8')) if type(value) is str else
                           len(value) if type(value) is bytes else 8 for value in row)
        return self._page(sql,args,decode,lambda row:(row['m_ts'],row['m_session_ordinal'],row['identity']),charge)

    @_read_operation
    def record(self, home, sid, key):
        meta = self._owner((home, sid))
        if meta is None:
            return None
        with self._cursor('SELECT r.* FROM calls r JOIN publications p ON p.id=r.batch '
                          f'WHERE r.owner=? AND r.identity=? AND r.deleted=0 AND {self._visible("calls")}',
                          (meta['id'], identity(key, self.limits))) as cursor:
            row = cursor.fetchone()
        return self._decode_call(row, self._call_metadata(meta)) if row else None

    @_read_operation
    def records(self, owner, keys, *, after=None):
        """Selected exact records, one owner context plus one indexed batch query."""
        meta = self._owner(owner)
        if meta is None:
            return Page((),None,True,0,0)
        args = dict(owner=meta['id'],n=self.limits.page_rows+1)
        where = ['r.owner=:owner','r.deleted=0',self._visible('calls'),
                 'r.identity IN ('+self._keys(keys,args)+')']
        if after is not None:
            args['after'] = after[0]
            where.append('r.identity>:after')
        sql = ('SELECT r.* FROM calls r JOIN publications p ON p.id=r.batch '
               f'WHERE {" AND ".join(where)} ORDER BY r.identity LIMIT :n')
        metadata = self._call_metadata(meta)
        return self._page(sql,args,lambda row:self._decode_call(row,metadata),
                          lambda row:(row['identity'],),lambda row:row['encoded_bytes'])

    def _decode_request(self, row):
        result = _detail_unpack(row['detail'], self.limits)
        for i, name in enumerate(('started_at', 'ended_at')):
            if row['types'][i]:
                result[name] = _restore_scalar(row['types'][i], row[name])
        return dict(key=unpack(row['key_value'], self.limits, maximum=self.limits.key_bytes),
                    ordinal=row['ordinal'], metadata=result)

    @_read_operation
    def boundaries(self, owner, field, start, end, *, after=None):
        if field not in ('started_at', 'ended_at'):
            raise ValueError('boundary stream must select started_at or ended_at')
        meta = self._owner(owner)
        if meta is None:
            return Page((), None, True, 0, 0)
        args = dict(owner=meta['id'], start=_number(start, 'start'), end=_number(end, 'end'), n=self.limits.page_rows + 1)
        where = f'r.owner=:owner AND r.deleted=0 AND r.{field}>=:start AND r.{field}<:end AND {self._visible("requests")}'
        if after is not None:
            args.update(zip(('at', 'ordinal', 'key'), after))
            where += f' AND (r.{field},r.ordinal,r.identity)>(:at,:ordinal,:key)'
        index = 'requests_start' if field == 'started_at' else 'requests_end'
        sql = (f'SELECT r.* FROM requests r INDEXED BY {index} JOIN publications p ON p.id=r.batch '
               f'WHERE {where} ORDER BY r.{field},r.ordinal,r.identity LIMIT :n')
        return self._page(sql, args, self._decode_request,
                          lambda r: (r[field], r['ordinal'], r['identity']), lambda r: r['encoded_bytes'])

    @_read_operation
    def request(self, owner, turn):
        meta = self._owner(owner)
        if meta is None:
            return None
        with self._cursor('SELECT r.* FROM requests r JOIN publications p ON p.id=r.batch '
                          f'WHERE r.owner=? AND r.identity=? AND r.deleted=0 AND {self._visible("requests")}',
                          (meta['id'], identity(turn, self.limits))) as cursor:
            row = cursor.fetchone()
        return self._decode_request(row) if row else None

    @_read_operation
    def request_context(self, owner, turn, *, after=None):
        meta = self._owner(owner)
        if meta is None:
            return None
        key = identity(turn, self.limits)
        with self._cursor('SELECT r.* FROM requests r JOIN publications p ON p.id=r.batch '
                          f'WHERE r.owner=? AND r.identity=? AND r.deleted=0 AND {self._visible("requests")}',
                          (meta['id'], key)) as cursor:
            request = cursor.fetchone()
        with self._cursor('SELECT COUNT(*) FROM calls r INDEXED BY calls_turn JOIN publications p ON p.id=r.batch '
                          f'WHERE r.owner=? AND r.turn_identity=? AND r.deleted=0 AND {self._visible("calls")}',
                          (meta['id'], key)) as cursor:
            whole_count = cursor.fetchone()[0]
        with self._cursor('SELECT r.* FROM calls r INDEXED BY calls_turn JOIN publications p ON p.id=r.batch '
                          f'WHERE r.owner=? AND r.turn_identity=? AND r.deleted=0 AND {self._visible("calls")} '
                          'ORDER BY r.m_ts,r.m_session_ordinal,r.identity LIMIT 1', (meta['id'], key)) as cursor:
            first = cursor.fetchone()
        calls = self.calls(owner, turn=turn, after=after)
        return dict(request=self._decode_request(request) if request else None,
                    whole_count=whole_count, calls=calls,
                    first_call=self._decode_call(first, self._call_metadata(meta)) if first else None)

    def _keys(self, keys, args):
        keys = tuple(keys)
        if len(keys) > self.limits.owners_per_batch:
            raise CapacityExceeded('selected-key batch exceeds its bound')
        args.update((f'key{i}', identity(key, self.limits)) for i, key in enumerate(keys))
        return ','.join(f':key{i}' for i in range(len(keys))) or 'NULL'

    def _decode_header(self, row):
        result = dict(key=unpack(row['key_value'], self.limits, maximum=self.limits.key_bytes),
                      display_ordinal=row['display_ordinal'], effective_at=row['effective_at'],
                      call_count=row['call_count'], minimum=row['minimum'], maximum=row['maximum'],
                      dimensions=unpack(row['dimensions'], self.limits),
                      request_present=bool(row['request_present']))
        if row['state_present']:
            result['turn_state'] = unpack(row['turn_state'], self.limits)
        return result

    @_read_operation
    def request_headers(self, owner, keys=None, *, after=None, with_first=False):
        """Display ordinals are persisted by ingestion, never counted per selected turn."""
        meta = self._owner(owner)
        if meta is None:
            return Page((), None, True, 0, 0)
        args = dict(owner=meta['id'], n=self.limits.page_rows + 1)
        where = ['r.owner=:owner', 'r.deleted=0', self._visible('headers')]
        if keys is not None:
            where.append('r.identity IN (' + self._keys(keys, args) + ')')
        if after is not None:
            args.update(ordinal=after[0], key=after[1])
            where.append('(r.display_ordinal,r.identity)>(:ordinal,:key)')
        columns, joins = 'r.*', ''
        if with_first:
            first_columns = ('key_value', 'turn_value', 'types', 'detail', 'owner_mask',
                             'encoded_bytes', *('m_' + name for name in METRICS))
            columns += ',' + ','.join(f'f.{name} AS first_{name}' for name in first_columns)
            joins = (' LEFT JOIN calls f ON f.owner=r.owner AND f.identity=r.first_key '
                     'LEFT JOIN publications fp ON fp.id=f.batch ')
            where.append('(f.identity IS NULL OR (f.deleted=0 AND ' + self._visible('calls', 'f', 'fp') + '))')
        sql = (f'SELECT {columns} FROM headers r JOIN publications p ON p.id=r.batch {joins} '
               f'WHERE {" AND ".join(where)} ORDER BY r.display_ordinal,r.identity LIMIT :n')
        metadata = self._call_metadata(meta) if with_first else None
        def decode(row):
            value = self._decode_header(row)
            if with_first:
                first = ({name: row['first_' + name] for name in first_columns}
                         if row['first_key_value'] is not None else None)
                if value['call_count'] and first is None:
                    raise NotReady('request header points to a missing first call')
                value['first_call'] = self._decode_call(first, metadata) if first else None
            return value
        return self._page(sql, args, decode, lambda r: (r['display_ordinal'], r['identity']),
                          lambda r: r['encoded_bytes'] + ((r['first_encoded_bytes'] or 0) if with_first else 0))

    @_read_operation
    def requests(self, owner, keys=None, *, after=None):
        meta = self._owner(owner, metadata=False)
        if meta is None:
            return Page((), None, True, 0, 0)
        args = dict(owner=meta['id'], n=self.limits.page_rows + 1)
        where = ['r.owner=:owner', 'r.deleted=0', self._visible('requests')]
        if keys is not None:
            where.append('r.identity IN (' + self._keys(keys, args) + ')')
        if after is not None:
            args.update(ordinal=after[0], key=after[1])
            where.append('(r.ordinal,r.identity)>(:ordinal,:key)')
        sql = ('SELECT r.* FROM requests r JOIN publications p ON p.id=r.batch '
               f'WHERE {" AND ".join(where)} ORDER BY r.ordinal,r.identity LIMIT :n')
        return self._page(sql, args, self._decode_request,
                          lambda r: (r['ordinal'], r['identity']), lambda r: r['encoded_bytes'])

    @_read_operation
    def next_boundary(self, owner, at):
        meta = self._owner(owner, metadata=False)
        if meta is None:
            return None
        values = []
        for table, column, index in (('calls', 'm_ts', 'calls_time'),
                                     ('requests', 'started_at', 'requests_start'),
                                     ('requests', 'ended_at', 'requests_end')):
            with self._cursor(f'SELECT r.{column} FROM {table} r INDEXED BY {index} '
                              'JOIN publications p ON p.id=r.batch '
                              f'WHERE r.owner=? AND r.{column}>=? AND r.deleted=0 AND {self._visible(table)} '
                              f'ORDER BY r.{column} LIMIT 1', (meta['id'], at)) as cursor:
                value = cursor.fetchone()
            if value:
                values.append(value[0])
        return min(values) if values else None

    @_read_operation
    def call_bounds(self, owner, filters=None, *, start=None, end=None):
        meta = self._owner(owner, metadata=False)
        if meta is None:
            return None, None
        args = [meta['id']]
        where = ['r.owner=?', 'r.deleted=0', self._visible('calls')]
        for name,value,operation in (('start',start,'>='),('end',end,'<')):
            if value is not None:
                args.append(_number(value,name))
                where.append('r.m_ts'+operation+'?')
        for name, value in (filters or {}).items():
            if name not in ('model', 'service_tier', 'effort'):
                raise ValueError('bounds filter must be a typed call dimension')
            where.append(f"COALESCE(NULLIF(r.m_{name},''),'미확인')=?")
            args.append(value)
        values = []
        for order in ('ASC', 'DESC'):
            with self._cursor('SELECT r.m_ts FROM calls r INDEXED BY calls_time '
                              'JOIN publications p ON p.id=r.batch '
                              f'WHERE {" AND ".join(where)} ORDER BY r.m_ts {order} LIMIT 1', args) as cursor:
                row = cursor.fetchone()
            values.append(row[0] if row else None)
        return tuple(values)

    @_read_operation
    def category_bounds_for_owners(self, owners, filters=None):
        """Exact category edges for public, canonically prepared owners.

        The caller may prune owners by prepare_row's source/project defaults:
        prepare_row overwrites those fields on every canonical call. This is
        deliberately not a metadata shortcut for arbitrary per-call overrides.
        Model selection scans its global index once per edge, never per owner.
        LIMIT 1 bounds the sort state; selected-model history still costs SQL
        work, while unrelated model history is outside the indexed prefix.
        """
        if not self.public:
            raise ValueError('category bounds require a public prepared snapshot')
        filters=dict(filters or {})
        if any(name not in ('model','service_tier','effort') or type(value) is not str
               for name,value in filters.items()):
            raise ValueError('category bounds require text call dimensions')
        wanted=set()
        admitted=0
        for owner in owners:
            key=tuple(owner)
            if len(key)!=2 or any(type(value) is not str for value in key):
                raise ValueError('category bounds require source home/session identities')
            if key not in wanted:
                admitted+=256+_size(key)
                if admitted>self.limits.page_objects_bytes:
                    raise CapacityExceeded('category bound owner identities exceed the object budget')
                wanted.add(key)
        if not wanted:
            return None,None
        ids=[]
        with self._cursor(f'SELECT o.id,o.home,o.sid FROM {self._owner_tables()} '
                          'WHERE v.deleted=0 ORDER BY v.source_ordinal,o.id') as cursor:
            for row in cursor:
                self._check()
                if (row['home'],row['sid']) in wanted:
                    # Include the eventual decimal SQL literal and container slots.
                    admitted+=128
                    if admitted>self.limits.page_objects_bytes:
                        raise CapacityExceeded('category bound owner IDs exceed the object budget')
                    ids.append(int(row['id']))
        del wanted
        if not ids:
            return None,None
        predicates=[]
        args=[]
        def false_value(name):
            column='r.m_'+name
            return f"({column} IS NULL OR {column} IN ('',0,X''))"
        def text_value(name):
            return f"substr(r.types,{METRICS.index(name)+1},1)=X'05'"
        for name,value in filters.items():
            column='r.m_'+name
            if name=='service_tier':
                conflict=f"(NOT {false_value('mode_conflict')} OR r.m_service_tier_source='conflict')"
                # NULL source is not a conflict; COALESCE keeps NOT exact too.
                conflict=f'COALESCE({conflict},0)'
                if value=='미확인':
                    predicates.append(f'({conflict} OR {false_value(name)} OR '
                                      f'({text_value(name)} AND {column}=?))')
                    args.append(value)
                else:
                    aliases={'Fast':('Fast','priority','fast'),
                             'Standard':('Standard','default','standard')}.get(value,(value,))
                    predicates.append(f'(NOT {conflict} AND {text_value(name)} AND '
                                      f'{column} IN ({",".join("?" for _ in aliases)}))')
                    args.extend(aliases)
            elif value=='미확인':
                predicates.append(f'({false_value(name)} OR ({text_value(name)} AND {column}=?))')
                args.append(value)
            else:
                predicates.append(f'({column}=? AND {text_value(name)})')
                args.append(value)
        common=['r.deleted=0',self._visible('calls'),*predicates]
        values=[None,None]
        if 'model' in filters:
            # IDs originate from SQLite INTEGER PRIMARY KEY, never request text.
            owner_sql=','.join(str(value) for value in ids)
            where=' AND '.join([f'r.owner IN ({owner_sql})',*common])
            for i,order in enumerate(('ASC','DESC')):
                with self._cursor('SELECT r.m_ts,r.types FROM calls r INDEXED BY calls_dimensions '
                                  'JOIN publications p ON p.id=r.batch '
                                  'JOIN owner_heads h ON h.owner=r.owner '
                                  'JOIN owner_versions v ON v.owner=h.owner AND v.batch=h.batch '
                                  f'WHERE {where} ORDER BY r.m_ts {order},v.source_ordinal,'
                                  'r.owner,r.m_session_ordinal,r.identity LIMIT 1',args) as cursor:
                    row=cursor.fetchone()
                if row is not None:
                    values[i]=_restore_scalar(row['types'][METRICS.index('ts')],row['m_ts'])
        else:
            # Owner-only selections can seek each edge without reading its history.
            where=' AND '.join(['r.owner=?',*common])
            for owner in ids:
                for i,order in enumerate(('ASC','DESC')):
                    with self._cursor('SELECT r.m_ts,r.types FROM calls r INDEXED BY calls_time '
                                      'JOIN publications p ON p.id=r.batch '
                                      f'WHERE {where} ORDER BY r.m_ts {order},'
                                      'r.m_session_ordinal,r.identity LIMIT 1',[owner,*args]) as cursor:
                        row=cursor.fetchone()
                    if row is not None:
                        value=_restore_scalar(row['types'][METRICS.index('ts')],row['m_ts'])
                        if values[i] is None or (value<values[i] if i==0 else value>values[i]):
                            values[i]=value
        return tuple(values)

    @_read_operation
    def unlinked_count(self, owner):
        meta = self._owner(owner, metadata=False)
        if meta is None:
            return 0
        with self._cursor('SELECT COUNT(*) FROM calls r INDEXED BY calls_turn '
                          'JOIN publications p ON p.id=r.batch '
                          f'WHERE r.owner=? AND r.turn_identity IS NULL AND r.deleted=0 AND {self._visible("calls")}',
                          (meta['id'],)) as cursor:
            return cursor.fetchone()[0]

    @_read_operation
    def owner_candidates(self, owner, *, after=None):
        """Read one source inventory in ordinal order without preparing call facts."""
        meta = self._owner(owner, metadata=False)
        if meta is None:
            return Page((), None, True, 0, 0)
        args = dict(owner=meta['id'], n=self.limits.page_rows + 1)
        where = ['r.owner=:owner', 'r.deleted=0', self._visible('candidates')]
        if after is not None:
            args.update(ordinal=after[0], key=after[1])
            where.append('(r.ordinal,r.identity)>(:ordinal,:key)')
        sql = ('SELECT r.key_value,r.ordinal,r.identity,r.encoded_bytes FROM candidates r '
               'JOIN publications p ON p.id=r.batch '
               f'WHERE {" AND ".join(where)} ORDER BY r.ordinal,r.identity LIMIT :n')
        return self._page(sql, args, lambda r:dict(key=unpack(r['key_value'],self.limits,maximum=self.limits.key_bytes),
                                                  ordinal=r['ordinal']),
                          lambda r:(r['ordinal'],r['identity']),lambda r:r['encoded_bytes'])

    def _candidate_tables(self):
        """Resolve owner state only after the selected ordinary identities."""
        version = ('h.batch' if self.staged_batch is None else
                   'COALESCE((SELECT x.batch FROM owner_versions x '
                   f'WHERE x.owner=o.id AND x.batch={int(self.staged_batch)}),h.batch)')
        return ('candidates r INDEXED BY candidates_identity '
                'CROSS JOIN owners o ON o.id=r.owner '
                'LEFT JOIN owner_heads h ON h.owner=o.id '
                f'CROSS JOIN owner_versions v ON v.owner=o.id AND v.batch={version} '
                'CROSS JOIN publications p ON p.id=r.batch')

    @_read_operation
    def candidate_matches(self, owner, keys, *, after=None):
        """Return bounded competing refs; the coordinator owns winner election.

        Ordinary keys share a home. record:/legacy: keys remain session-scoped.
        Pending flags are typed owner facts, so no owner/detail packet is decoded.
        """
        args = dict(home=owner[0],sid=owner[1],n=self.limits.page_rows + 1)
        keys = tuple(keys)
        self._keys(keys,args)
        ordinary, scoped = [], []
        for index, key in enumerate(keys):
            (scoped if str(key).startswith(('record:', 'legacy:')) else ordinary).append(f':key{index}')
        where = ['o.home=:home','v.deleted=0','r.deleted=0',self._visible('candidates')]
        if after is not None:
            args.update(key=after[0],owner=after[1])
            where.append('(r.identity,r.owner)>(:key,:owner)')
        columns = 'o.home,o.sid,r.owner AS owner,r.key_value,r.ordinal,r.identity AS identity,r.encoded_bytes,v.pending'
        branches = []
        if ordinary:
            branches.append(f'SELECT {columns} FROM {self._candidate_tables()} '
                f'WHERE r.identity IN ({",".join(ordinary)}) AND r.scoped=0 AND {" AND ".join(where)}')
        if scoped:
            branches.append(f'SELECT {columns} FROM {self._owner_tables()} '
                'CROSS JOIN candidates r ON r.owner=o.id JOIN publications p ON p.id=r.batch '
                f'WHERE r.identity IN ({",".join(scoped)}) AND r.scoped=1 AND o.sid=:sid AND {" AND ".join(where)}')
        if not branches:
            return Page((), None, True, 0, 0)
        sql = ' UNION ALL '.join(branches) + ' ORDER BY identity,owner LIMIT :n'
        return self._page(sql,args,lambda r:dict(owner=(r['home'],r['sid']),ordinal=r['ordinal'],
                          key=unpack(r['key_value'],self.limits,maximum=self.limits.key_bytes),pending=bool(r['pending'])),
                          lambda r:(r['identity'],r['owner']),lambda r:r['encoded_bytes']+len(r['home'])*4+len(r['sid'])*4)

    @_read_operation
    def candidates(self, home, key, *, sid=None, after=None):
        """Return bounded candidate refs. The caller still owns winner election."""
        scoped = str(key).startswith(('record:', 'legacy:'))
        if scoped and sid is None:
            raise ValueError('legacy and record identities require their session scope')
        args = dict(home=home, key=identity(key, self.limits), scoped=int(scoped),
                    sid=sid, after=after[0] if after else 0, n=self.limits.page_rows + 1)
        where = 'AND o.sid=:sid' if scoped else ''
        tables = (f'{self._owner_tables()} CROSS JOIN candidates r ON r.owner=o.id '
                  'JOIN publications p ON p.id=r.batch') if scoped else self._candidate_tables()
        sql = ('SELECT o.home,o.sid,r.owner,r.ordinal,v.dependencies,v.metadata,r.key_value,r.encoded_bytes '
               f'FROM {tables} '
               f'WHERE r.identity=:key AND r.scoped=:scoped AND o.home=:home {where} '
               f'AND r.owner>:after AND r.deleted=0 AND v.deleted=0 AND {self._visible("candidates")} '
               'ORDER BY r.owner LIMIT :n')
        return self._page(sql, args, lambda r: dict(owner=(r['home'], r['sid']), ordinal=r['ordinal'],
                          key=unpack(r['key_value'], self.limits, maximum=self.limits.key_bytes),
                          dependencies=unpack(r['dependencies'], self.limits),
                          pending=bool(unpack(r['metadata'], self.limits).get('_collection_pending'))),
                          lambda r: (r['owner'],), lambda r: r['encoded_bytes'] + len(r['dependencies']) + len(r['metadata']))

    def _decode_section(self, row):
        return dict(section=unpack(row['section_value'],self.limits,maximum=self.limits.key_bytes),
                    key=unpack(row['item_value'],self.limits,maximum=self.limits.key_bytes),
                    ordinal=row['ordinal'],at=_restore_scalar(row['at_type'],row['at']),
                    value=_detail_unpack(row['detail'],self.limits))

    @_read_operation
    def section(self, owner, section_key, *, after=None, since=None, until=None):
        meta = self._owner(owner,metadata=False)
        if meta is None:
            return Page((),None,True,0,0)
        args = dict(owner=meta['id'],section=identity(section_key,self.limits),n=self.limits.page_rows+1)
        where = ['r.owner=:owner','r.section_identity=:section','r.deleted=0',self._visible('sections')]
        for name,value,operator in (('since',since,'>='),('until',until,'<')):
            if value is not None:
                args[name] = _number(value,name)
                where.append(f'r.at{operator}:{name}')
        if after is not None:
            args.update(ordinal=after[0],key=after[1])
            where.append('(r.ordinal,r.identity)>(:ordinal,:key)')
        index = 'sections_time' if since is not None or until is not None else 'sections_order'
        sql = (f'SELECT r.* FROM sections r INDEXED BY {index} JOIN publications p ON p.id=r.batch '
               f'WHERE {" AND ".join(where)} ORDER BY r.ordinal,r.identity LIMIT :n')
        return self._page(sql,args,self._decode_section,lambda row:(row['ordinal'],row['identity']),
                          lambda row:row['encoded_bytes'])

    @_read_operation
    def section_item(self, owner, section_key, key):
        meta = self._owner(owner,metadata=False)
        if meta is None:
            return None
        with self._cursor('SELECT r.* FROM sections r JOIN publications p ON p.id=r.batch '
                          f'WHERE r.owner=? AND r.identity=? AND r.deleted=0 AND {self._visible("sections")}',
                          (meta['id'],identity((section_key,key),self.limits))) as cursor:
            row = cursor.fetchone()
        return self._decode_section(row) if row else None

    def _checkpoint_head(self, namespace):
        namespace = _checkpoint_name(namespace)
        if self.staged_batch is not None:
            sql = ('SELECT v.* FROM checkpoint_versions v WHERE v.namespace=? AND v.batch=COALESCE('
                   '(SELECT batch FROM checkpoint_versions WHERE namespace=? AND batch=?),'
                   '(SELECT batch FROM checkpoint_heads WHERE namespace=?))')
            args = (namespace,namespace,self.staged_batch,namespace)
        else:
            sql = ('SELECT v.* FROM checkpoint_heads h JOIN checkpoint_versions v '
                   'ON v.namespace=h.namespace AND v.batch=h.batch WHERE h.namespace=?')
            args = (namespace,)
        with self._cursor(sql,args) as cursor:
            return cursor.fetchone()

    @_read_operation
    def checkpoint_state(self, namespace):
        row = self._checkpoint_head(namespace)
        return (dict(token=unpack(row['token'],self.limits),encoded_bytes=row['total_bytes'],
                     sections=row['section_count']) if row is not None else None)

    def _checkpoint_section(self, namespace, name):
        head = self._checkpoint_head(namespace)
        if head is None:
            return None
        batch = self.staged_batch if head['batch'] == self.staged_batch else None
        with self._cursor('SELECT r.* FROM checkpoint_sections r JOIN publications p ON p.id=r.batch '
                          f'WHERE r.namespace=? AND r.name=? AND r.deleted=0 AND {_checkpoint_latest(batch=batch)}',
                          (namespace,_checkpoint_name(name))) as cursor:
            return cursor.fetchone()

    @staticmethod
    def _checkpoint_info(row):
        return dict(name=row['name'],digest=row['digest'],chunks=row['chunk_count'],encoded_bytes=row['encoded_bytes'])

    @_read_operation
    def checkpoint_section(self, namespace, name):
        row = self._checkpoint_section(namespace,name)
        return self._checkpoint_info(row) if row is not None else None

    @_read_operation
    def checkpoint_sections(self, namespace, *, after=None):
        head = self._checkpoint_head(namespace)
        if head is None:
            return Page((),None,True,0,0)
        batch = self.staged_batch if head['batch'] == self.staged_batch else None
        args = dict(namespace=namespace,after=after[0] if after else '',n=self.limits.page_rows+1)
        sql = ('SELECT r.* FROM checkpoint_sections r JOIN publications p ON p.id=r.batch '
               f'WHERE r.namespace=:namespace AND r.name>:after AND r.deleted=0 AND {_checkpoint_latest(batch=batch)} '
               'ORDER BY r.name LIMIT :n')
        return self._page(sql,args,self._checkpoint_info,lambda row:(row['name'],),
                          lambda row:len(row['name'].encode('utf-8'))+128)

    @_read_operation
    def checkpoint_chunks(self, namespace, name, *, after=None):
        section = self._checkpoint_section(namespace,name)
        if section is None:
            return Page((),None,True,0,0)
        sql = ('SELECT ordinal,payload FROM checkpoint_chunks WHERE namespace=? AND name=? AND batch=? '
               'AND ordinal>? ORDER BY ordinal LIMIT ?')
        args = (namespace,name,section['batch'],after[0] if after else -1,self.limits.page_rows+1)
        def decode(row):
            payload = row['payload']
            if (type(payload) is not bytes or not 0 < len(payload) <= self.limits.checkpoint_chunk_bytes or
                    not 0 <= row['ordinal'] < section['chunk_count']):
                raise RebuildRequired('checkpoint chunk metadata is invalid')
            return dict(ordinal=row['ordinal'],payload=payload)
        return self._page(sql,args,decode,lambda row:(row['ordinal'],),lambda row:len(row['payload']))

    def _speed_clear(self):
        """TEMP cleanup must run even after cancellation closed the snapshot."""
        if self.reader.db is None:
            return
        self.db.set_progress_handler(None, 0)
        try:
            self.db.execute('PRAGMA query_only=OFF')
            self.db.execute('DROP TABLE IF EXISTS temp.speed_selection')
        finally:
            self.db.execute('PRAGMA query_only=ON')
            if not self.closed:
                self.db.set_progress_handler(self._progress, 1000)

    def _speed_load(self, ranges):
        self.db.execute('PRAGMA query_only=OFF')
        try:
            self.db.execute('CREATE TEMP TABLE speed_selection(owner INTEGER PRIMARY KEY,frontier INTEGER NOT NULL) WITHOUT ROWID')
            page_size = self.db.execute('PRAGMA temp.page_size').fetchone()[0]
            pages = self.limits.speed_temp_bytes // page_size
            actual = self.db.execute(f'PRAGMA temp.max_page_count={pages}').fetchone()[0]
            if actual > pages:
                raise CapacityExceeded('speed selection exceeds its temporary database budget')
            count = size = 0
            for group in _chunks(ranges.items(), self.limits.owners_per_batch):
                self._check()
                args = []
                for owner, frontier in group:
                    if (not isinstance(owner, tuple) or len(owner) != 2 or
                            any(type(value) is not str for value in owner)):
                        raise TypeError('speed ranges require exact (home,sid) tuple identities')
                    if frontier is not None and (type(frontier) is not int or not 0 <= frontier < 2 ** 63):
                        raise TypeError('speed frontier must be an exact nonnegative raw ordinal or None')
                    size += len(pack(owner,self.limits,maximum=self.limits.key_bytes)) + 128
                    count += 1
                    if count > self.limits.speed_range_rows or size > self.limits.speed_temp_bytes:
                        raise CapacityExceeded('speed owner selection exceeds its metadata budget')
                    args.extend((*owner,-1 if frontier is None else frontier))
                sql = ('WITH requested(home,sid,frontier) AS (VALUES '+','.join('(?,?,?)' for _ in group)+') '
                       'INSERT INTO temp.speed_selection(owner,frontier) '
                       f'SELECT o.id,q.frontier FROM requested q,{self._owner_tables()} '
                       'WHERE q.home=o.home AND q.sid=o.sid AND v.deleted=0')
                before = self.db.total_changes
                with self._cursor(sql,args):
                    pass
                if self.db.total_changes-before != len(group):
                    raise NotReady('speed selection contains an owner absent from this generation')
            self.speed_work['selected_owners'] = count
            self.speed_work['selection_input_bytes'] = size
            self.speed_work['selection_database_bytes'] = self.db.execute('PRAGMA temp.page_count').fetchone()[0]*page_size
        finally:
            self.db.execute('PRAGMA query_only=ON')

    def iter_speed_rows(self, ranges):
        """Yield (owner, raw ordinal, scalar row) from one pinned ordered cursor.

        ranges maps owner to an exclusive session_ordinal frontier, or None for
        full replay. Dense classifier positions belong to the caller's proof.
        Invalid observations and other conditions remain in this stream.
        """
        self._check()
        if self.speed_active:
            raise PreparedError('only one grouped speed stream may use a snapshot')
        self.speed_active = True
        self.speed_work = dict(selected_owners=0,selection_input_bytes=0,selection_database_bytes=0,
                               selected_rows=0,sort_estimated_bytes=0,rows_yielded=0,body_decodes=0)
        try:
            self._speed_load(ranges)
            visible = ('r.owner=q.owner AND r.m_session_ordinal>q.frontier AND r.deleted=0 AND '
                       + self._visible('calls'))
            tables = ('temp.speed_selection q CROSS JOIN calls r INDEXED BY calls_ordinal '
                      'JOIN publications p ON p.id=r.batch')
            # The sorter receives typed scalars only. This admission estimate
            # includes conservative per-cell/row overhead, not compressed detail.
            charges = ["CASE typeof(r.m_"+field+") WHEN 'text' THEN length(CAST(r.m_"+field+
                       " AS BLOB)) WHEN 'blob' THEN length(r.m_"+field+") ELSE 8 END+16"
                       for field in SPEED_FIELDS]
            charge = '(256+length(r.key_value)+COALESCE(length(r.turn_value),0)+length(r.types)+'+'+'.join(charges)+')'
            with self._cursor(f'SELECT COUNT(*),COALESCE(SUM({charge}),0) FROM {tables} WHERE {visible}') as cursor:
                count, size = cursor.fetchone()
            self.speed_work.update(selected_rows=count,sort_estimated_bytes=size)
            if size > self.limits.speed_sort_bytes:
                raise CapacityExceeded('speed chronological sort exceeds its admitted temporary payload')
            columns = 'r.key_value,r.turn_value,r.types,'+','.join('r.m_'+name for name in SPEED_FIELDS)
            sql = ('WITH selected AS MATERIALIZED ('
                   'SELECT q.owner,q.frontier,o.home,o.sid,ROW_NUMBER() OVER(ORDER BY o.home,o.sid) AS owner_rank '
                   'FROM temp.speed_selection q JOIN owners o ON o.id=q.owner) '
                   f'SELECT q.home,q.sid,{columns} FROM selected q '
                   'CROSS JOIN calls r INDEXED BY calls_ordinal JOIN publications p ON p.id=r.batch '
                   f'WHERE {visible} ORDER BY r.m_ts,q.owner_rank,r.m_session_ordinal,r.identity')
            positions = tuple((field,METRICS.index(field)) for field in SPEED_FIELDS)
            with self._cursor(sql) as cursor:
                self.speed_cursor = cursor
                while True:
                    self._check()
                    raw = cursor.fetchone()
                    if raw is None:
                        break
                    row = {field:_restore_scalar(raw['types'][index],raw['m_'+field])
                           for field,index in positions if raw['types'][index]}
                    row['key'] = unpack(raw['key_value'],self.limits,maximum=self.limits.key_bytes)
                    if raw['turn_value'] is not None:
                        row['turn'] = unpack(raw['turn_value'],self.limits,maximum=self.limits.key_bytes)
                    if 'model' in row:
                        row['analysis_model'] = row['model']
                    owner = (raw['home'],raw['sid'])
                    row['home'],row['sid'] = owner
                    if _size(row) > self.limits.page_objects_bytes:
                        raise CapacityExceeded('one speed row exceeds the decoded-object budget')
                    self.speed_work['rows_yielded'] += 1
                    yield owner,row['session_ordinal'],row
        except BaseException:
            self.close()
            raise
        finally:
            self.speed_active = False
            self.speed_cursor = None
            self._speed_clear()

    def iter_calls(self, owner, **query):
        """No cursor survives a yield. The enclosing snapshot still owns its transaction."""
        after = None
        while True:
            page = self.calls(owner, after=after, **query)
            yield from page.rows
            if page.complete:
                return
            after = page.after
            del page

    def close(self):
        if not self.closed:
            self.db.set_progress_handler(None, 0)
            if self.speed_cursor is not None:
                self.speed_cursor.close()
                self.speed_cursor = None
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            self.closed = True
            if self.reader.active is self:
                self.reader.active = None

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *_):
        self.close()
