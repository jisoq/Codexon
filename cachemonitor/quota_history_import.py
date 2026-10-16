"""Cooperative, durable recovery of observations from the read-only usage index.

For verified modern sources the ledger owns observations and the progress marker
in one transaction. Each source snapshot ends before the ledger write. Older
sources retain the established single-snapshot full import until the collector
upgrades their change tracking; they cannot safely resume an unverified cursor.
"""
import json
import sqlite3
import time
from pathlib import Path

from .event_store import EventStore, unpack_event


class ObservationImport:
    SOURCE_ROWS = 16384
    OBSERVATIONS = 128
    SOURCE_BYTES = 1024 * 1024
    STEP_SECONDS = .05

    def __init__(self, ledger, path, homes):
        self.ledger = ledger
        self.path = Path(path).resolve()
        self.homes = frozenset(homes)
        self.scope = json.dumps(sorted(self.homes), ensure_ascii=True)
        self.source = None
        self.cursor = 0
        self.token = None
        self.version = None
        self.schema = None
        self.trusted = False
        self.finished = not self.path.exists() or not self.homes
        self.metrics = dict(source_rows=0, imported_rows=0, steps=0, restarts=0)
        if self.finished:
            return
        self.source = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=.1)
        self.source.execute('PRAGMA cache_size=-2048')
        # This is disposable progress, never a source or collection ACK.
        ledger.db.execute('''CREATE TABLE IF NOT EXISTS observation_import_progress(
            source TEXT PRIMARY KEY, scope TEXT NOT NULL, token TEXT NOT NULL,
            cursor INTEGER NOT NULL, complete INTEGER NOT NULL)''')
        ledger.db.commit()
        saved = ledger.db.execute('SELECT scope,token,cursor,complete FROM observation_import_progress '
                                  'WHERE source=?', (str(self.path),)).fetchone()
        if saved is not None and saved[0] == self.scope:
            try:
                token = json.loads(saved[1])
                if (isinstance(token, list) and len(token) == 3
                        and isinstance(token[0], str) and len(token[0]) == 32
                        and all(type(v) is int and v >= 0 for v in token[1:])
                        and type(saved[2]) is int and 0 <= saved[2] <= token[2]):
                    self.token = tuple(token)
                    self.cursor = saved[2]
            except (ValueError, TypeError):
                pass

    def _layout(self):
        schema = self.source.execute('PRAGMA schema_version').fetchone()[0]
        if schema == self.schema:
            return
        tables = {r[0] for r in self.source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r[1] for r in self.source.execute('PRAGMA table_info(event_store_state)')}
        owners = {r[1] for r in self.source.execute('PRAGMA table_info(event_sessions)')}
        actual = dict(self.source.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        self.trusted = ('replay_epoch' in columns and 'replay_revision' in owners and 'events_legacy' not in tables
            and all(actual.get(name) == sql for name, sql in EventStore.replay_triggers().items()))
        if self.schema is not None:
            self.token = None
            self.cursor = 0
        self.schema = schema

    def _current(self):
        # One row per source owner, not one row per retained event. The result
        # is reused while this connection's data_version is stable.
        epoch = self.source.execute('SELECT replay_epoch FROM event_store_state WHERE id=1').fetchone()[0]
        revision = self.source.execute('SELECT coalesce(sum(replay_revision),0) FROM event_sessions').fetchone()[0]
        maximum = self.source.execute('SELECT coalesce(max(seq),0) FROM event_records').fetchone()[0]
        if (not isinstance(epoch, str) or len(epoch) != 32
                or type(revision) is not int or revision < 0):
            self.trusted = False
            return None
        return (epoch, revision, maximum)

    def _same_prefix(self, current):
        old = self.token
        if old is None:
            return False
        if current == old:
            return True
        if current[0] != old[0] or current[2] < old[2]:
            return False
        appended = self.source.execute('SELECT count(*) FROM event_records WHERE seq>? AND seq<=?',
                                       (old[2], current[2])).fetchone()[0]
        # Inserts add one. Corrections/deletions add extra increments. A seq
        # move adds two, so moving an old row across the cursor cannot mimic
        # insertion. Owner replacement rotates the epoch before losing counts.
        return current[1] - old[1] == appended

    def _rows(self, maximum):
        bound = self.source.execute('SELECT seq FROM event_records WHERE seq>? AND seq<=? '
            'ORDER BY seq LIMIT 1 OFFSET ?', (self.cursor, maximum, self.SOURCE_ROWS - 1)).fetchone()
        end = bound[0] if bound else maximum
        query = '''SELECT r.seq,s.home,r.ts,r.data FROM event_records r
            JOIN event_sessions s ON s.id=r.session_id
            WHERE r.seq>? AND r.seq<=? AND r.quota=1 ORDER BY r.seq LIMIT ?'''
        return self.source.execute(query, (self.cursor, end, self.OBSERVATIONS + 1)), end

    def step(self):
        if self.finished:
            return True
        from .quota_cycles import observation_parts
        started = time.monotonic()
        batch = []
        size = 0
        cursor = self.cursor
        previous_token = self.token
        previous_version = self.version
        try:
            self.source.execute('BEGIN')
            version = self.source.execute('PRAGMA data_version').fetchone()[0]
            self._layout()
            current = (self.token if version == self.version and self.token is not None else self._current()) if self.trusted else None
            if not self.trusted:
                # A changing legacy source has no durable correction proof.
                # Repeatedly restarting short snapshots can starve forever
                # under append. Preserve its existing finite snapshot import.
                self.source.rollback()
                for home in sorted(self.homes):
                    self.ledger.import_observations(self.path,home)
                self.finished=True
                self.metrics['compatibility_import']=True
                self.metrics['steps']+=1
                return True
            if not self._same_prefix(current):
                if self.cursor:
                    self.metrics['restarts'] += 1
                self.cursor = cursor = 0
            rows, end = self._rows(current[2])
            exhausted = True
            try:
                for position, (seq, home, at, data) in enumerate(rows):
                    if position >= self.OBSERVATIONS:
                        exhausted = False
                        break
                    if home not in self.homes:
                        cursor=seq
                        self.metrics['source_rows']+=1
                        continue
                    text = unpack_event(data)
                    row_bytes = len(text.encode('utf8'))
                    if cursor != self.cursor and (size + row_bytes > self.SOURCE_BYTES
                                                 or time.monotonic() - started >= self.STEP_SECONDS):
                        exhausted = False
                        break
                    cursor = seq
                    size += row_bytes
                    self.metrics['source_rows'] += 1
                    quota = json.loads(text).get('payload', {}).get('rate_limits')
                    if quota:
                        batch.append(observation_parts(home, {**quota, 'observed_at':at, 'source':'history'}))
                if exhausted:
                    cursor = end
            finally:
                rows.close()
            self.source.rollback()
            complete = cursor >= current[2]
            # The progress and imported rows become durable together. A crash
            # before commit repeats this bounded slice without losing records.
            self.ledger._import_observation_parts(batch)
            if self.trusted:
                values = (str(self.path), self.scope, json.dumps(current), cursor, int(complete))
                old = self.ledger.db.execute('SELECT source,scope,token,cursor,complete '
                    'FROM observation_import_progress WHERE source=?', (str(self.path),)).fetchone()
                if old is None or tuple(old) != values:
                    self.ledger.db.execute('INSERT OR REPLACE INTO observation_import_progress VALUES(?,?,?,?,?)', values)
            self.ledger.db.commit()
            self.token, self.version, self.cursor = current, version, cursor
            self.finished = complete
            self.metrics['steps'] += 1
            self.metrics['imported_rows'] += len(batch)
            return complete
        except BaseException:
            self.source.rollback()
            self.ledger.db.rollback()
            self.token, self.version = previous_token, previous_version
            raise

    def close(self):
        if self.source is not None:
            self.source.close()
            self.source = None
