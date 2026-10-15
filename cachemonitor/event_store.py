"""Lossless event payloads and shared source metadata in the usage index."""
from pathlib import Path
import shutil
import sqlite3
import time
import uuid
import zlib


PREFIX=b'\x00CE1'
MAX_PAYLOAD=2*1024*1024
MAX_MIGRATION_ROW=4*MAX_PAYLOAD
MIGRATION_ROWS=512
MIGRATION_BYTES=1024*1024
MIGRATION_WAL_BYTES=8*1024*1024
TABLES={'event_paths','event_sessions','event_records','event_store_state','events_legacy','sqlite_sequence'}


def index_tables_valid(tables, *, require_core=False):
    modern={'event_paths','event_sessions','event_records','event_store_state','sqlite_sequence'}
    if tables-{'files','events','metadata','usage_archive'}-TABLES:return False
    if tables&TABLES and not modern.issubset(tables):return False
    return not require_core or {'files','metadata'}.issubset(tables) and ('events' in tables or modern.issubset(tables))


def pack_event(value):
    if not isinstance(value,str):return value
    raw=value.encode('utf8')
    if not 256<=len(raw)<=MAX_PAYLOAD:return value
    packed=PREFIX+bytes([b'"rate_limits"' in raw])+len(raw).to_bytes(4,'little')+zlib.compress(raw,1)
    return packed if len(packed)<len(raw)*.9 else value


def unpack_event(value):
    if not isinstance(value,bytes) or not value.startswith(PREFIX):return value
    if len(value)<9 or value[4] not in (0,1):raise ValueError('Invalid stored event header')
    size=int.from_bytes(value[5:9],'little')
    if size>MAX_PAYLOAD:raise ValueError('Stored event exceeds its decoding budget')
    try:
        decoder=zlib.decompressobj();raw=decoder.decompress(value[9:],size+1)
        if len(raw)!=size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Incomplete stored event')
        return raw.decode('utf8')
    except (zlib.error,UnicodeError) as error:
        raise ValueError('Invalid stored event payload') from error


def quota_events(db, home):
    modern=db.execute("SELECT 1 FROM sqlite_master WHERE name='event_store_state' AND type='table'").fetchone()
    if modern:
        return db.execute('SELECT ts,data FROM event_rows WHERE home=? AND quota=1',(home,))
    return db.execute('SELECT ts,data FROM events WHERE home=? AND data LIKE ?',(home,'%"rate_limits"%'))


def event_owners(db, homes=None):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    params=tuple(homes) if homes is not None else ()
    scope='home IN ('+','.join('?' for _ in params)+')' if homes is not None else '1'
    if 'event_store_state' not in tables:
        return db.execute(f'SELECT DISTINCT home,tid FROM events WHERE {scope} ORDER BY home,tid',params)
    sql=f'''SELECT home,tid FROM event_sessions s WHERE {scope}
        AND EXISTS(SELECT 1 FROM event_records r WHERE r.session_id=s.id)'''
    if 'events_legacy' in tables:
        sql+=f' UNION SELECT DISTINCT home,tid FROM events_legacy WHERE {scope}'
        params+=params
    return db.execute(sql+' ORDER BY home,tid',params)


def _quota_sql(value):
    return f"CASE WHEN typeof({value})='blob' AND substr({value},1,4)=x'00434531' THEN substr({value},5,1)=x'01' ELSE coalesce(instr(cast({value} AS TEXT),'\"rate_limits\"')>0,0) END"


class EventStore:
    def __init__(self, db, path, *, read_only=False):
        self.db=db;self.path=Path(path);self.read_only=read_only;self.replay_epoch=None
        self.replay_table='event_rows'
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'event_store_state' in tables:
            version=db.execute('SELECT version FROM event_store_state WHERE id=1').fetchone()
            if version is None or version[0]!=1:raise ValueError('Unsupported usage event storage version')
            self.legacy='events_legacy' in tables
        elif read_only:
            self.legacy=True;self.replay_table='events'
        else:
            self.legacy='events' in tables
            db.execute('BEGIN IMMEDIATE')
            try:
                if self.legacy:db.execute('ALTER TABLE events RENAME TO events_legacy')
                for sql in (
                    'CREATE TABLE event_paths(id INTEGER PRIMARY KEY,path TEXT UNIQUE)',
                    'CREATE TABLE event_sessions(id INTEGER PRIMARY KEY,home TEXT,tid TEXT,UNIQUE(home,tid))',
                    '''CREATE TABLE event_records(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        path_id INTEGER,pos INTEGER,session_id INTEGER,ts REAL,data BLOB,quota INTEGER NOT NULL,
                        UNIQUE(path_id,pos))''',
                    'CREATE INDEX event_records_session ON event_records(session_id,ts,path_id,pos)',
                    'CREATE TABLE event_store_state(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)',
                    'INSERT INTO event_store_state VALUES(1,1)',
                ):db.execute(sql)
                if self.legacy:
                    # New appends cannot reuse an identity belonging to a legacy
                    # row that a later batch has not moved yet.
                    maximum=db.execute('SELECT coalesce(max(rowid),0) FROM events_legacy').fetchone()[0]
                    db.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('event_records',?)",(maximum,))
                self._views()
                db.commit()
            except BaseException:
                db.rollback();raise
        self.pending=self.legacy and not read_only

    def enable_replay_tokens(self):
        """Opt into durable owner invalidation before publishing replay checkpoints.

        Counters share the source transaction, including deletes and in-place
        corrections. Extra columns preserve older readers of the event schema.
        Until a checkpoint consumer enables this, ordinary indexing pays no
        extra writes. Legacy migration must finish before checkpoints are used.
        """
        if self.read_only or self.legacy:return None
        if self.db.in_transaction:raise RuntimeError('Replay tokens require a committed source state')
        changes={
            'replace':'''BEFORE INSERT ON event_records BEGIN
                UPDATE event_sessions SET replay_revision=replay_revision+1 WHERE id IN
                    (SELECT session_id FROM event_records
                     WHERE (path_id=NEW.path_id AND pos=NEW.pos) OR seq=NEW.seq); END''',
            'insert':'''AFTER INSERT ON event_records BEGIN
                UPDATE event_sessions SET replay_revision=replay_revision+1 WHERE id=NEW.session_id; END''',
            'update':'''AFTER UPDATE OF path_id,pos,session_id,ts,data,quota ON event_records
                WHEN OLD.path_id IS NOT NEW.path_id OR OLD.pos IS NOT NEW.pos OR OLD.session_id IS NOT NEW.session_id
                    OR OLD.ts IS NOT NEW.ts OR OLD.data IS NOT NEW.data OR OLD.quota IS NOT NEW.quota BEGIN
                UPDATE event_sessions SET replay_revision=replay_revision+1 WHERE id IN (OLD.session_id,NEW.session_id); END''',
            'delete':'''AFTER DELETE ON event_records BEGIN
                UPDATE event_sessions SET replay_revision=replay_revision+1 WHERE id=OLD.session_id; END''',
            'path':'''AFTER UPDATE OF id,path ON event_paths WHEN OLD.id IS NOT NEW.id OR OLD.path IS NOT NEW.path BEGIN
                UPDATE event_sessions SET replay_revision=replay_revision+1 WHERE id IN
                    (SELECT session_id FROM event_records WHERE path_id IN (OLD.id,NEW.id)); END''',
            'path_delete':'''BEFORE DELETE ON event_paths BEGIN
                UPDATE event_sessions SET replay_revision=replay_revision+1 WHERE id IN
                    (SELECT session_id FROM event_records WHERE path_id=OLD.id); END''',
            'owner':'''AFTER UPDATE OF id,home,tid ON event_sessions
                WHEN OLD.id IS NOT NEW.id OR OLD.home IS NOT NEW.home OR OLD.tid IS NOT NEW.tid BEGIN
                UPDATE event_sessions SET replay_revision=replay_revision+1 WHERE id=NEW.id; END''',
        }
        triggers={'event_replay_'+key:'CREATE TRIGGER event_replay_'+key+' '+sql for key,sql in changes.items()}
        actual={name:sql for name,sql in self.db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
        columns={row[1] for row in self.db.execute('PRAGMA table_info(event_store_state)')}
        owners={row[1] for row in self.db.execute('PRAGMA table_info(event_sessions)')}
        if ('replay_epoch' in columns and 'replay_revision' in owners
                and all(actual.get(name)==sql for name,sql in triggers.items())):
            epoch=self.db.execute('SELECT replay_epoch FROM event_store_state WHERE id=1').fetchone()[0]
            if isinstance(epoch,str) and len(epoch)==32:
                self.replay_epoch=epoch;return epoch
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if 'replay_epoch' not in columns:self.db.execute('ALTER TABLE event_store_state ADD COLUMN replay_epoch TEXT')
            if 'replay_revision' not in owners:self.db.execute('ALTER TABLE event_sessions ADD COLUMN replay_revision INTEGER NOT NULL DEFAULT 0')
            # A repaired/missing trigger could have missed source changes. Rotate
            # the epoch instead of treating old checkpoints as current.
            epoch=uuid.uuid4().hex
            self.db.execute('UPDATE event_store_state SET replay_epoch=? WHERE id=1',(epoch,))
            for name,sql in triggers.items():
                self.db.execute('DROP TRIGGER IF EXISTS '+name);self.db.execute(sql)
            self.db.commit()
        except BaseException:
            self.db.rollback();raise
        self.replay_epoch=epoch
        return epoch

    def owner_replay_token(self, home, tid):
        if self.replay_epoch is None:return None
        row=self.db.execute('SELECT state.replay_epoch,owner.id,owner.replay_revision '
            'FROM event_sessions owner CROSS JOIN event_store_state state '
            'WHERE state.id=1 AND owner.home IS ? AND owner.tid IS ?',(home,tid)).fetchone()
        if (row is None or not isinstance(row[0],str) or len(row[0])!=32
                or type(row[2]) is not int or row[2]<0):return None
        return tuple(row)

    def upper(self):
        if self.replay_table=='events':
            return self.db.execute('SELECT coalesce(max(rowid),0) FROM events').fetchone()[0]
        row=self.db.execute("SELECT seq FROM sqlite_sequence WHERE name='event_records'").fetchone()
        return row[0] if row else 0

    def owners(self, homes=None):
        return event_owners(self.db,homes)

    def _views(self):
        db=self.db
        for name in ('events_insert','events_update','events_delete'):db.execute('DROP TRIGGER IF EXISTS '+name)
        db.execute('DROP VIEW IF EXISTS events')
        db.execute('DROP VIEW IF EXISTS event_rows')
        select='''SELECT p.path,r.pos,s.home,s.tid,r.ts,r.data,r.seq AS rowid,r.quota
            FROM event_records r LEFT JOIN event_paths p ON p.id=r.path_id
            JOIN event_sessions s ON s.id=r.session_id'''
        if self.legacy:
            select+=' UNION ALL SELECT path,pos,home,tid,ts,data,rowid,'+_quota_sql('data')+' FROM events_legacy'
        db.execute('CREATE VIEW event_rows AS '+select)
        db.execute('CREATE VIEW events AS SELECT path,pos,home,tid,ts,data FROM event_rows')
        metadata='''INSERT INTO event_paths(path) SELECT NEW.path WHERE NEW.path IS NOT NULL
                AND NOT EXISTS(SELECT 1 FROM event_paths WHERE path=NEW.path);
            INSERT INTO event_sessions(home,tid) SELECT NEW.home,NEW.tid
                WHERE NOT EXISTS(SELECT 1 FROM event_sessions WHERE home IS NEW.home AND tid IS NEW.tid);'''
        path='(SELECT id FROM event_paths WHERE path=NEW.path)'
        session='(SELECT id FROM event_sessions WHERE home IS NEW.home AND tid IS NEW.tid)'
        quota=_quota_sql('NEW.data')
        old="path_id IS (SELECT id FROM event_paths WHERE path=OLD.path) AND pos IS OLD.pos AND data IS OLD.data AND ts IS OLD.ts AND session_id IS (SELECT id FROM event_sessions WHERE home IS OLD.home AND tid IS OLD.tid)"
        legacy_delete=('DELETE FROM events_legacy WHERE path=NEW.path AND pos=NEW.pos;' if self.legacy else '')
        db.execute(f'''CREATE TRIGGER events_insert INSTEAD OF INSERT ON events BEGIN
            {metadata}{legacy_delete}
            INSERT OR REPLACE INTO event_records(path_id,pos,session_id,ts,data,quota)
            VALUES({path},NEW.pos,{session},NEW.ts,NEW.data,{quota}); END''')
        legacy_update=('''UPDATE events_legacy SET path=NEW.path,pos=NEW.pos,home=NEW.home,tid=NEW.tid,ts=NEW.ts,data=NEW.data
            WHERE path IS OLD.path AND pos IS OLD.pos AND data IS OLD.data AND ts IS OLD.ts AND home IS OLD.home AND tid IS OLD.tid;''' if self.legacy else '')
        db.execute(f'''CREATE TRIGGER events_update INSTEAD OF UPDATE ON events BEGIN
            {metadata} UPDATE event_records SET path_id={path},pos=NEW.pos,session_id={session},
                ts=NEW.ts,data=NEW.data,quota={quota} WHERE {old};
            {legacy_update} END''')
        legacy_delete=('DELETE FROM events_legacy WHERE path IS OLD.path AND pos IS OLD.pos AND data IS OLD.data AND ts IS OLD.ts AND home IS OLD.home AND tid IS OLD.tid;' if self.legacy else '')
        db.execute(f'''CREATE TRIGGER events_delete INSTEAD OF DELETE ON events BEGIN
            DELETE FROM event_records WHERE {old}; {legacy_delete} END''')

    def migrate(self, deadline=None):
        """Move a bounded batch atomically; callers must have no replay cursor."""
        if not self.pending or self.db.in_transaction:return 0
        db=self.db
        try:
            if shutil.disk_usage(self.path.parent).free<16*1024*1024:return 0
            wal=Path(str(self.path)+'-wal')
            if wal.exists() and wal.stat().st_size>MIGRATION_WAL_BYTES:
                timeout=db.execute('PRAGMA busy_timeout').fetchone()[0]
                try:
                    db.execute('PRAGMA busy_timeout=0')
                    checkpoint=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                finally:db.execute(f'PRAGMA busy_timeout={timeout}')
                if checkpoint[0]:return 0
            db.execute('BEGIN IMMEDIATE')
            batch=[];size=0
            # SQLite can materialize a value while evaluating the size filter.
            # Bound that work too, then restore the connection's normal limit.
            previous_limit=db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_MIGRATION_ROW)
            try:
                cursor=db.execute('''SELECT rowid,path,pos,home,tid,ts,data FROM events_legacy
                    WHERE data IS NULL OR length(cast(data AS BLOB))<=? ORDER BY rowid LIMIT ?''',
                    (MAX_PAYLOAD,MIGRATION_ROWS))
                try:
                    for row in cursor:
                        weight=sum(len(v)*4 if isinstance(v,str) else len(v) if isinstance(v,bytes) else 32 for v in row)
                        if batch and size+weight>MIGRATION_BYTES:break
                        batch.append(row);size+=weight
                        if deadline is not None and time.monotonic()>=deadline:break
                finally:cursor.close()
            finally:db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,previous_limit)
            for seq,path,pos,home,tid,ts,data in batch:
                packed=pack_event(data)
                original=unpack_event(data)
                if unpack_event(packed)!=original:raise ValueError('Event encoding changed source content')
                if path is not None:db.execute('INSERT OR IGNORE INTO event_paths(path) VALUES(?)',(path,))
                db.execute('''INSERT INTO event_sessions(home,tid) SELECT ?,? WHERE NOT EXISTS
                    (SELECT 1 FROM event_sessions WHERE home IS ? AND tid IS ?)''',(home,tid,home,tid))
                path_id=db.execute('SELECT id FROM event_paths WHERE path=?',(path,)).fetchone()
                session_id=db.execute('SELECT id FROM event_sessions WHERE home IS ? AND tid IS ?',(home,tid)).fetchone()[0]
                quota=int('"rate_limits"' in original) if isinstance(original,str) else int(b'"rate_limits"' in original) if isinstance(original,bytes) else 0
                db.execute('INSERT INTO event_records VALUES(?,?,?,?,?,?,?)',
                    (seq,path_id[0] if path_id else None,pos,session_id,ts,packed,quota))
                db.execute('DELETE FROM events_legacy WHERE rowid=?',(seq,))
            if not db.execute('SELECT 1 FROM events_legacy LIMIT 1').fetchone():
                self.legacy=False
                self._views()
                db.execute('DROP TABLE events_legacy')
            db.commit()
            self.pending=self.legacy and bool(batch)
            return len(batch)
        except (OSError,sqlite3.Error,ValueError) as error:
            db.rollback()
            self.legacy=bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='events_legacy' AND type='table'").fetchone())
            self.pending=False
            from .quota_diagnostics import record_failure
            record_failure(self.path,'usage event packing',error)
            return 0
