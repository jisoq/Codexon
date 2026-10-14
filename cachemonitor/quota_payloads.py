"""Lossless shared payloads for durable quota observations."""
from collections import OrderedDict
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time
import zlib


MAX_PAYLOAD_BYTES = 256 * 1024
CONTEXT_CACHE_BYTES = 1024 * 1024
CONTEXT_CACHE_ENTRIES = 64
CONTEXT_OVERHEAD_BYTES = 128
MIGRATION_ROWS = 512
MIGRATION_BYTES = 1024 * 1024
MIGRATION_ROW_BYTES = 1024 * 1024
MIGRATION_WAL_BYTES = 8 * 1024 * 1024
MIGRATION_PREPARE_SECONDS = .025
# Part of the v1 on-disk format. Changing these bytes requires a new version.
DICTIONARY_V1 = (b'null,true,false,"source":"live","plan_type":"pro","account":,"bucket":"codex",'
    b'"max_age":90,"separate_models":[],"credits":,"reset_credits":,"has_five_hour":false,'
    b'"unlimited_windows":["five_hour"],"window_conflicts":[],"has_credits":,"unlimited":,'
    b'"balance":,"available_count":,"credits":[],"status":"available","expires_at":,'
    b'"requested_at":,"observed_at":,"elapsed":,"windows":{"weekly":{"used_percent":,'
    b'"resets_at":,"window_minutes":10080},"five_hour":{"used_percent":,"resets_at":,"window_minutes":300}}')


def initialize(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(tracking_observations)')}
    if 'payload_context' not in columns:
        db.execute('ALTER TABLE tracking_observations ADD COLUMN payload_context INTEGER')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS tracking_payload_contexts(
            id INTEGER PRIMARY KEY, digest BLOB NOT NULL UNIQUE, data BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS tracking_payload_migration(
            id INTEGER PRIMARY KEY CHECK(id=1), after_seq INTEGER, through_seq INTEGER,
            active INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO tracking_payload_migration
            SELECT 1,NULL,max(seq),0,max(seq) IS NULL FROM tracking_observations;
        CREATE TRIGGER IF NOT EXISTS tracking_payload_contexts_immutable
            BEFORE UPDATE ON tracking_payload_contexts BEGIN
            SELECT RAISE(ABORT, 'quota payload contexts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS tracking_payload_contexts_referenced
            BEFORE DELETE ON tracking_payload_contexts
            WHEN EXISTS(SELECT 1 FROM tracking_observations WHERE payload_context=OLD.id) BEGIN
            SELECT RAISE(ABORT, 'quota payload context is still referenced'); END;
    ''')


def _text(value):
    return json.dumps(value, separators=(',', ':'), ensure_ascii=True)


def _timestamp_reference(value, column):
    if type(value) not in (int, float):
        return False
    try:
        stored = float(column)
    except (TypeError, ValueError, OverflowError):
        return False
    # REAL storage can round large integers and discard a negative zero sign.
    return (math.isfinite(stored) and value == stored and
            not (type(value) is float and value == 0 and math.copysign(1, value) < 0))


def _split(raw, requested, received):
    # Work on the serialized value, including JSON's string-key conversion.
    base = json.loads(raw)
    refs, slots, changing = {}, [], []

    def take(parent, key, path):
        changing.append(parent.pop(key))
        slots.append(path)

    for key, column, number in (('requested_at', 'requested', requested),
                                ('observed_at', 'received', received)):
        if key not in base:
            continue
        if _timestamp_reference(base[key], number):
            refs[key] = [column, 'int' if type(base[key]) is int else 'float']
            del base[key]
        else:
            take(base, key, [key])
    if 'elapsed' in base:
        take(base, 'elapsed', ['elapsed'])
    if isinstance(base.get('windows'), dict):
        for name, window in base['windows'].items():
            if isinstance(window, dict) and 'used_percent' in window:
                take(window, 'used_percent', ['windows', name, 'used_percent'])
    return _text(dict(data=base, refs=refs, slots=slots)), b'Q1' + _text(changing).encode('ascii')


def _unpack_template(data):
    if not isinstance(data, bytes) or not data.startswith(b'T1') or len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError('Invalid quota payload context')
    decoder = zlib.decompressobj(zdict=DICTIONARY_V1)
    raw = decoder.decompress(data[2:], MAX_PAYLOAD_BYTES + 1)
    if len(raw) > MAX_PAYLOAD_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid or oversized quota payload context')
    return raw.decode('ascii')


def encode(db, quota, requested, received):
    """Return a payload and context ID without committing the caller's transaction."""
    raw = _text(quota)
    if len(raw) > MAX_PAYLOAD_BYTES or len(raw) <= CONTEXT_OVERHEAD_BYTES:
        return raw, None
    template, delta = _split(raw, requested, received)
    if len(template) > MAX_PAYLOAD_BYTES or len(delta) + 16 >= len(raw):
        return raw, None
    digest = hashlib.sha256(template.encode('ascii')).digest()
    existing = db.execute('SELECT id,data FROM tracking_payload_contexts WHERE digest=?', (digest,)).fetchone()
    if existing:
        if _unpack_template(existing[1]) != template:
            return raw, None
        return delta, existing[0]
    compressor = zlib.compressobj(1, zdict=DICTIONARY_V1)
    packed = b'T1' + compressor.compress(template.encode('ascii')) + compressor.flush()
    if len(packed) + len(delta) + CONTEXT_OVERHEAD_BYTES >= len(raw):
        return raw, None
    cursor = db.execute('INSERT INTO tracking_payload_contexts(digest,data) VALUES(?,?)', (digest, packed))
    return delta, cursor.lastrowid


class PayloadReader:
    """Read legacy JSON and shared payloads with bounded immutable template reuse."""
    def __init__(self, db):
        self.db = db
        self.contexts = OrderedDict()
        self.context_bytes = 0

    def _template(self, identifier):
        cached = self.contexts.get(identifier)
        if cached is not None:
            self.contexts.move_to_end(identifier)
            return cached[0]
        stored = self.db.execute('SELECT data FROM tracking_payload_contexts WHERE id=?', (identifier,)).fetchone()
        if not stored:
            raise ValueError('Missing quota payload context')
        template = _unpack_template(stored[0])
        weight = len(template) + 192  # ASCII string, mapping entry and scalar key.
        self.contexts[identifier] = (template, weight)
        self.context_bytes += weight
        while self.context_bytes > CONTEXT_CACHE_BYTES or len(self.contexts) > CONTEXT_CACHE_ENTRIES:
            _, (_, removed) = self.contexts.popitem(last=False)
            self.context_bytes -= removed
        return template

    def decode(self, row):
        data = row['data']
        if not isinstance(data, bytes) or not data.startswith(b'Q1'):
            return json.loads(data)
        if len(data) > MAX_PAYLOAD_BYTES:
            raise ValueError('Oversized quota observation payload')
        parts = json.loads(self._template(row['payload_context']))
        value, changing = parts['data'], json.loads(data[2:])
        if not isinstance(changing, list) or len(parts['slots']) != len(changing):
            raise ValueError('Invalid quota observation payload')
        for name, (column, kind) in parts['refs'].items():
            if column not in ('requested', 'received') or kind not in ('int', 'float'):
                raise ValueError('Invalid quota timestamp reference')
            value[name] = int(row[column]) if kind == 'int' else float(row[column])
        for path, item in zip(parts['slots'], changing):
            parent = value
            for name in path[:-1]:
                parent = parent[name]
            parent[path[-1]] = item
        return value


class PayloadMigration:
    """Repack one bounded legacy batch without changing its logical records."""
    def __init__(self, db, path):
        self.db, self.path = db, Path(path)
        self.pending = not db.execute('SELECT done FROM tracking_payload_migration WHERE id=1').fetchone()[0]
        self.columns = [row[1] for row in db.execute('PRAGMA table_info(tracking_observations)')]
        self.selection = ','.join('"' + name.replace('"', '""') + '"' for name in self.columns)

    def step(self):
        if not self.pending or self.db.in_transaction:
            return 0
        db = self.db
        timeout = limit = None
        try:
            if shutil.disk_usage(self.path.parent).free < 16 * 1024 * 1024:
                return 0
            timeout = db.execute('PRAGMA busy_timeout').fetchone()[0]
            db.execute('PRAGMA busy_timeout=0')
            wal = Path(str(self.path) + '-wal')
            if wal.exists() and wal.stat().st_size > MIGRATION_WAL_BYTES:
                checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint[0]:
                    return 0
            db.execute('BEGIN IMMEDIATE')
            progress = db.execute('SELECT after_seq,through_seq,done FROM tracking_payload_migration WHERE id=1').fetchone()
            if progress[2]:
                db.rollback()
                self.pending = False
                return 0
            after, upper = progress[:2]
            condition = 'seq<=?' if after is None else 'seq>? AND seq<=?'
            params = (upper,) if after is None else (after, upper)
            identifiers = [row[0] for row in db.execute(
                'SELECT seq FROM tracking_observations WHERE ' + condition + ' ORDER BY seq LIMIT ?',
                (*params, MIGRATION_ROWS))]
            limit = db.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, min(limit, MIGRATION_ROW_BYTES))
            deadline = time.monotonic() + MIGRATION_PREPARE_SECONDS
            prepared, scanned, size = [], 0, 0
            reader = PayloadReader(db)
            data_index, context_index = self.columns.index('data'), self.columns.index('payload_context')
            for seq in identifiers:
                try:
                    row = db.execute('SELECT ' + self.selection + ' FROM tracking_observations WHERE seq=?', (seq,)).fetchone()
                except sqlite3.DataError as error:
                    if error.sqlite_errorcode != sqlite3.SQLITE_TOOBIG or not db.in_transaction:
                        raise
                    # An oversized row stays verbatim; later ordinary rows still migrate.
                    row = None
                weight = sum(len(v)*4 if isinstance(v, str) else len(v) if isinstance(v, bytes) else 32
                             for v in row) if row else 0
                if scanned and size + weight > MIGRATION_BYTES:
                    break
                scanned += 1
                after = seq
                size += weight
                if row and weight <= MIGRATION_BYTES and isinstance(row['data'], str):
                    try:
                        quota = json.loads(row['data'])
                    except ValueError:
                        quota = None
                    if isinstance(quota, dict):
                        data, context = encode(db, quota, row['requested'], row['received'])
                        if context is not None:
                            restored = reader.decode(dict(data=data, payload_context=context,
                                                          requested=row['requested'], received=row['received']))
                            if json.dumps(restored, sort_keys=True) != json.dumps(quota, sort_keys=True):
                                raise ValueError('Quota payload conversion changed source content')
                            values = list(row)
                            values[data_index], values[context_index] = data, context
                            prepared.append(values)
                if time.monotonic() >= deadline:
                    break
            # Other writers cannot enter until this transaction commits with active=0.
            # Delete/reinsert as a batch releases sparse pages without changing row IDs.
            if prepared:
                db.execute('UPDATE tracking_payload_migration SET active=1 WHERE id=1')
                seq_index = self.columns.index('seq')
                db.execute('DELETE FROM tracking_observations WHERE seq IN (' + ','.join('?' for _ in prepared) + ')',
                           tuple(row[seq_index] for row in prepared))
                db.executemany('INSERT INTO tracking_observations(' + self.selection + ') VALUES(' +
                               ','.join('?' for _ in self.columns) + ')', prepared)
            remaining = db.execute('SELECT 1 FROM tracking_observations WHERE seq>? AND seq<=? LIMIT 1',
                                   (after, upper)).fetchone() if after is not None else None
            db.execute('UPDATE tracking_payload_migration SET after_seq=?,active=0,done=? WHERE id=1',
                       (after, int(remaining is None)))
            db.commit()
            self.pending = remaining is not None
            return scanned
        except (OSError, sqlite3.Error, ValueError) as error:
            db.rollback()
            code = getattr(error, 'sqlite_errorcode', 0) or 0
            if (code & 255) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                self.pending = False
                from .quota_diagnostics import record_failure
                record_failure(self.path, 'quota_payload_migration', error)
            return 0
        finally:
            if limit is not None:
                db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, limit)
            if timeout is not None:
                db.execute(f'PRAGMA busy_timeout={timeout}')
