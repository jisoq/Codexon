"""Private proofs for reusing closed collection bodies without replaying events.

Bodies stay in the collection checkpoint. This index stores only their identity,
small session headers, and response membership used to validate wire evidence.
A missing or mismatched proof always falls back to ordinary source replay.
"""
from collections import deque
from hashlib import sha256
import json
import sqlite3
import time

from .core import Session, Transport, WINDOW
from .model_evidence import home_key, observation_record
from .version import VERSION


# Bump for projection or parser changes within one development release. Public
# releases also invalidate through VERSION, independently of SQLite schemas.
FORMAT = 1
BODY_BUDGET = 64 * 1024 * 1024
BODY_LIMIT = 2 * 1024 * 1024
HEADER_LIMIT = 16 * 1024
INDEX_BUDGET = 128 * 1024 * 1024
PROOF_ROWS = 65536
COMMIT_BYTES = 2 * 1024 * 1024
COMMIT_SECONDS = 5
CAPTURE_SECONDS = .05
CLEANUP_KEYS = 4096
CLEANUP_KEY_BUDGET = 16 * 1024 * 1024
CLEANUP_BATCH = 256


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def response_key(home, response):
    return sha256(encoded((home, response))).digest()


class EncodedSession(dict):
    """Collector-only header sharing an immutable, already encoded body."""
    def __init__(self, header, payload, fingerprint, digest):
        super().__init__(header)
        self.payload = payload
        self.fingerprint = fingerprint
        self.digest = digest

    def with_state(self, **state):
        return EncodedSession({**self, **state}, self.payload, self.fingerprint, self.digest)


def observation_digest(record):
    return sha256(encoded(observation_record(record))).digest()


def integrity(proof, header, digest, fingerprint, evidence):
    return sha256(encoded((proof.hex(), header.hex(), digest, fingerprint.hex(), evidence.hex()))).digest()


class CollectionReplay:
    """Owned by one CollectorService, never opened by a screen query worker."""
    @classmethod
    def open(cls, channel, index):
        result = cls.__new__(cls)
        try:
            result.__init__(channel, index)
            return result
        except (sqlite3.Error, ValueError):
            if getattr(result, 'db', None) is not None:
                result.db.close()
            return None

    def __init__(self, channel, index):
        self.channel = channel
        self.index = index
        self.db = sqlite3.connect(channel.companion('.collection-replay.sqlite'), timeout=.1)
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables - {'owners', 'responses'}:
            self.db.close()
            raise ValueError('Invalid collection replay index')
        self.db.execute('PRAGMA journal_mode=WAL')
        # Public bodies and source transactions commit first. Losing only this
        # derived proof cache after a power failure safely requires replay.
        self.db.execute('PRAGMA synchronous=NORMAL')
        # Bound dirty pages by the bounded database rather than repeatedly
        # spilling the same response-index pages during a proof batch.
        self.db.execute('PRAGMA cache_spill=OFF')
        page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
        self.db.execute(f'PRAGMA max_page_count={INDEX_BUDGET // page_size}')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS owners(id INTEGER PRIMARY KEY, home TEXT, sid TEXT,
                proof BLOB, header BLOB, digest TEXT, fingerprint BLOB, evidence BLOB, integrity BLOB,
                UNIQUE(home,sid));
            CREATE TABLE IF NOT EXISTS responses(owner INTEGER, key BLOB,
                PRIMARY KEY(owner,key)) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS response_owners ON responses(key,owner);
        ''')
        self.started = False
        self.enabled = False
        self.weight = 0
        self.evidence_version = None
        self.join_epoch = None
        self.invalidated = set()
        self.pending = set()
        self.restore_job = None
        self.writable = True
        self.available = True
        self.pending_admissions = set()
        self.pending_bytes = 0
        self.admission_started = None
        self.capture_snapshot = None
        self.capture_position = 0
        self.capture_pending = False
        self.cleanup_keys = set()
        self.cleanup_key_bytes = 0
        self.cleanup_scan = 0
        self.cleanup_rows = deque()
        self.cleanup_active = None
        self.cleanup_dirty = False
        self.cleanup_pending = False
        self.cleanup_homes = {str(home) for home in index.homes}
        self.wal_path = channel.companion('.collection-replay.sqlite-wal')

    def source_proof(self, key):
        token = self.index.event_store.owner_replay_token(*key)
        if token is None:
            return None
        return encoded((FORMAT, VERSION, token, self.index.metadata.get(key, {})))

    def reader_version(self):
        reader = self.index.model_evidence
        if reader.error or reader.catching_up:
            return None
        version = reader.db.execute('PRAGMA data_version').fetchone()[0] if reader.db is not None else None
        return reader.identity, reader.generation, reader.offset, version

    def begin(self, now):
        if not self.available:
            return
        try:
            self._begin(now)
        except sqlite3.Error:
            self.available = self.writable = False
            if self.restore_job is not None:
                self.restore_job.close();self.restore_job = None
            self.pending.clear()
            self.invalidated.update(key for key, session in self.index.monitor.sessions.items()
                                    if hasattr(session, '_replay_body'))
            try:self.db.rollback()
            except sqlite3.Error:pass
            self.db.close();self.db = None

    def _begin(self, now):
        if not self.enabled:
            self.enabled = self.index.event_store.enable_replay_tokens() is not None
        if not self.enabled:
            return
        reader = self.index.model_evidence
        # Poll deltas live for one reader batch. Preserve invalidation even when
        # later batches or a read error defer restoration and publication.
        if self.join_epoch is not None and self.join_epoch != reader.join_epoch:
            self.invalidated.update(key for key, session in self.index.monitor.sessions.items()
                                    if hasattr(session, '_replay_body'))
        elif self.started:
            for home, keys in reader.changed_responses.items():
                for key in keys:
                    self.invalidated.update((row[0], row[1]) for row in self.db.execute(
                        'SELECT o.home,o.sid FROM responses r JOIN owners o ON o.id=r.owner WHERE r.key=?',
                        (response_key(home, key),)))
        self.join_epoch = reader.join_epoch
        version = self.reader_version()
        if version is None:
            return
        initial = not self.started
        if initial:
            self.started = True
            # A bounded key catalog lets ordinary file scans defer only owners
            # whose existing proof is being checked. Changed sources still run.
            weight = 0
            for home, sid in self.db.execute('SELECT home,sid FROM owners'):
                if not isinstance(home,str) or not isinstance(sid,str):continue
                weight += 256 + 4 * (len(home) + len(sid))
                if weight > 16 * 1024 * 1024:
                    break
                self.pending.add((home, sid))
            if self.pending:
                self.restore_job = self._restore(version, now)
        if self.restore_job is not None:
            deadline = time.monotonic() + .1
            while time.monotonic() < deadline:
                try:
                    next(self.restore_job)
                except StopIteration:
                    self.restore_job = None
                    self.pending.clear()
                    break
        self.evidence_version = version

    def _restore(self, version, now):
        reader = self.index.model_evidence
        proofs = {}
        if reader.db is not None:
            rows = reader.db.execute('SELECT m.* FROM model_observations m JOIN '
                '(SELECT max(seq) seq FROM model_observations WHERE seq<=? '
                'GROUP BY home,response_id,attempt) latest ON m.seq=latest.seq ORDER BY m.seq', (reader.offset,))
            try:
                for number, row in enumerate(rows, 1):
                    for owner, in self.db.execute('SELECT owner FROM responses WHERE key=?',
                            (response_key(row['home'], row['response_id']),)):
                        proofs.setdefault(owner, sha256(encoded(reader.gaps))).update(observation_digest(row))
                    if number % 128 == 0:
                        yield
                        if self.reader_version() != version:
                            return
            finally:
                rows.close()
        if self.reader_version() != version:
            return
        empty = sha256(encoded(reader.gaps)).digest()
        homes = {str(home) for home in self.index.homes}
        for number, (owner, home, sid, proof, header, digest, fingerprint, evidence, checksum) in enumerate(self.db.execute(
                'SELECT id,home,sid,proof,header,digest,fingerprint,evidence,integrity FROM owners '
                'WHERE length(proof)<=? AND length(header)<=?', (HEADER_LIMIT, HEADER_LIMIT)), 1):
            if number % 32 == 0:
                yield
                if self.reader_version() != version:
                    return
            key = home, sid
            if key not in self.pending:
                continue
            self.pending.discard(key)
            if home not in homes or key in self.index.monitor.sessions or proof != self.source_proof(key):
                continue
            if not all(isinstance(value,bytes) for value in (proof,header,fingerprint,evidence,checksum)):
                continue
            actual = proofs[owner].digest() if owner in proofs else empty
            if (actual != evidence or not isinstance(fingerprint, bytes) or len(fingerprint) != 32
                    or checksum != integrity(proof, header, digest, fingerprint, evidence)):
                continue
            saved = self.channel.db.execute('SELECT CASE WHEN length(payload)<=? THEN payload END '
                'FROM sessions WHERE home=? AND sid=?', (BODY_LIMIT, *key)).fetchone()
            if not saved or saved[0] is None or sha256(saved[0]).hexdigest() != digest:
                continue
            if self.weight + len(saved[0]) + len(header) > BODY_BUDGET:
                break
            try:
                state = json.loads(header)
                if not isinstance(state,dict) or not isinstance(state.get('header'),dict) or not isinstance(state.get('session'),dict):
                    continue
                if state['format'] != FORMAT or state['header']['home'] != home or state['header']['id'] != sid:
                    continue
                if state['header']['activity'] >= now - WINDOW:
                    continue
                body = EncodedSession(state['header'], saved[0], fingerprint, digest)
                self._retire(key, body, state['session'])
            except (KeyError, TypeError, ValueError):
                continue
        self.index.cached_restore = deque(key for key in self.index.cached_restore if key not in self.index.loaded)

    @staticmethod
    def session_state(session):
        fields = ('title', 'model', 'provider', 'cwd', 'activity', 'parent_thread_id',
                  'agent_path', 'agent_nickname', 'spawn_depth', 'title_request', 'metadata_seen')
        return {**{field: getattr(session, field) for field in fields},
                'transports': [dict(vars(value)) for value in session.transports]}

    def _retire(self, key, body, state):
        session = Session(key[1], key[0])
        for field, value in state.items():
            if field != 'transports':
                setattr(session, field, value)
        session.transports = deque(Transport(**value) for value in state['transports'])
        session._replay_body = body
        session._replay_state = self.session_state(session)
        session._replay_metadata = self.index.metadata.get(key, {})
        self.index.monitor.sessions[key] = session
        self.invalidated.discard(key)
        self.index.loaded.add(key)
        self.weight += len(body.payload) + len(encoded(state)) + len(encoded(dict(body)))
        self.index.session_revisions[key] = body.get('usage_revision', 1)
        self.index.request_revisions[key] = (None, body.get('request_revision', 1))
        for mapping in (self.index.history_signatures, self.index.view_signatures,
                        self.index.publication_rows, self.index.turn_states, self.index.event_tails):
            mapping.pop(key, None)

    def view(self, key, session, ready, now):
        body = session._replay_body
        if (key in self.invalidated or body['activity'] >= now - WINDOW
                or session._replay_metadata != self.index.metadata.get(key, {})
                or session._replay_state != self.session_state(session)):
            self.index.rebuild_required.add(key)
            if key not in self.index.pending_rebuilds:
                self.index.pending_rebuilds.append(key)
            ready = False
        return body.with_state(collection_complete=ready)

    def rebuilt(self, key, previous):
        self.invalidated.discard(key)
        if previous is not None and hasattr(previous, '_replay_body'):
            self.weight = max(0, self.weight - len(previous._replay_body.payload)
                - len(encoded(previous._replay_state)) - len(encoded(dict(previous._replay_body))))

    def capture(self, snapshot, now):
        removed=getattr(self.channel,'removed_sessions',())
        self.channel.removed_sessions=()
        if not self.writable:
            self.capture_pending = False
            return False
        self.remember_removed(removed)
        try:
            # Proofs have no role in acknowledging source or public writes.
            # Commit bounded admission batches, not every display publication.
            if not self.db.in_transaction and self.wal_path.exists() and self.wal_path.stat().st_size > INDEX_BUDGET:
                self.db.execute('PRAGMA busy_timeout=0')
                try:
                    if self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                        return
                finally:
                    self.db.execute('PRAGMA busy_timeout=100')
            self.capture_pending = self._capture(snapshot, now)
            if (self.pending_admissions or self.cleanup_dirty) and (self.pending_bytes >= COMMIT_BYTES
                    or time.monotonic() - self.admission_started >= COMMIT_SECONDS
                    or self.cleanup_dirty and not self.cleanup_pending):
                self.commit_admissions()
        except sqlite3.Error:
            self.writable = False
            try:self.db.rollback()
            except sqlite3.Error:pass
            # A rolled-back response index cannot invalidate these live bodies.
            # Rebuild them through the ordinary path before confirming again.
            self.invalidated.update(self.pending_admissions)
            self.pending_admissions.clear()
            self.pending_bytes = 0
            self.admission_started = None
            self.capture_pending = False
            self.cleanup_keys.clear();self.cleanup_rows.clear();self.cleanup_active=None
            self.cleanup_key_bytes=0;self.cleanup_dirty=False
        if not self.capture_pending:self.capture_snapshot = None
        return self.capture_pending

    def commit_admissions(self):
        self.db.commit()
        self.pending_admissions.clear()
        self.pending_bytes = 0
        self.admission_started = None
        self.cleanup_dirty = False

    def remember_removed(self, removed):
        # The publisher already computed this delta and committed its deletion.
        # Overflow schedules one bounded reconciliation, never an unbounded queue.
        for key in removed:
            if key in self.cleanup_keys:continue
            weight=256+4*(len(key[0])+len(key[1]))
            if len(self.cleanup_keys)>=CLEANUP_KEYS or self.cleanup_key_bytes+weight>CLEANUP_KEY_BUDGET:
                self.cleanup_keys.clear();self.cleanup_key_bytes=0
                self.cleanup_rows.clear();self.cleanup_scan=0
                break
            self.cleanup_keys.add(key);self.cleanup_key_bytes+=weight

    def cleanup_changed(self, size):
        self.cleanup_dirty=True;self.pending_bytes+=size
        if self.admission_started is None:self.admission_started=time.monotonic()

    def cleanup(self, snapshot, current, deadline):
        # A missing file is not deletion: retained source events can still own a
        # body. Only complete, error-free membership for selected homes is final.
        if (not snapshot.get('usage_collection_complete') or snapshot.get('index',{}).get('loading')
                or snapshot.get('errors') or snapshot.get('usage_errors') or self.pending):
            return False
        while time.monotonic()<deadline and self.pending_bytes<COMMIT_BYTES:
            if self.cleanup_active is not None:
                owner,home,sid=self.cleanup_active
            elif self.cleanup_keys:
                home,sid=self.cleanup_keys.pop()
                self.cleanup_key_bytes-=256+4*(len(home)+len(sid))
                row=self.db.execute('SELECT id FROM owners WHERE home=? AND sid=?',(home,sid)).fetchone()
                if row is None:continue
                owner=row[0]
            elif self.cleanup_rows:
                owner,home,sid=self.cleanup_rows.popleft()
            elif self.cleanup_scan is not None:
                # Startup reads only bounded owner metadata once. Later work is
                # driven by exact removed keys unless that bounded delta overflowed.
                valid=f"typeof(home)='text' AND typeof(sid)='text' AND length(home)+length(sid)<={HEADER_LIMIT}"
                rows=self.db.execute(f'SELECT id,CASE WHEN {valid} THEN home END,CASE WHEN {valid} THEN sid END '
                    'FROM owners WHERE id>? ORDER BY id LIMIT 64',(self.cleanup_scan,)).fetchall()
                if not rows:
                    self.cleanup_scan=None
                    continue
                self.cleanup_scan=rows[-1][0];self.cleanup_rows.extend(rows)
                continue
            else:return False
            key=home,sid
            session=self.index.monitor.sessions.get(key)
            if (home not in self.cleanup_homes or key in current['sessions']
                    or hasattr(session,'_replay_body')
                    or self.channel.db.execute('SELECT 1 FROM sessions WHERE home=? AND sid=?',key).fetchone()):
                self.cleanup_active=None
                continue
            if self.cleanup_active is None:
                # A crash or a returning owner must never restore a body whose
                # response index has only been partially removed.
                self.db.execute('UPDATE owners SET proof=NULL,header=NULL,digest=NULL,fingerprint=NULL,'
                    'evidence=NULL,integrity=NULL WHERE id=?',(owner,))
                self.cleanup_active=(owner,home,sid)
                self.cleanup_changed(2*HEADER_LIMIT+256)
            deleted=self.db.execute('DELETE FROM responses WHERE owner=? AND key IN '
                '(SELECT key FROM responses WHERE owner=? LIMIT ?)',(owner,owner,CLEANUP_BATCH)).rowcount
            self.cleanup_changed(64*deleted)
            if deleted<CLEANUP_BATCH:
                self.db.execute('DELETE FROM owners WHERE id=?',(owner,))
                self.cleanup_changed(256)
                self.invalidated.discard(key);self.pending_admissions.discard(key)
                self.cleanup_active=None
        return bool(self.cleanup_active is not None or self.cleanup_keys or self.cleanup_rows or self.cleanup_scan is not None)

    def _capture(self, snapshot, now):
        if not self.writable or not self.enabled or not self.started or self.restore_job is not None:
            return False
        deadline = time.monotonic() + CAPTURE_SECONDS
        current = self.channel.live.current if self.channel.live is not None else None
        if current is None:
            return False
        if self.capture_snapshot is not snapshot:
            self.capture_snapshot = snapshot
            self.capture_position = 0
        self.cleanup_pending=self.cleanup(snapshot,current,deadline)
        if self.cleanup_pending:return True
        views = snapshot['sessions']
        for position in range(self.capture_position, len(views)):
            if time.monotonic() >= deadline:
                break
            self.capture_position = position + 1
            view = views[position]
            key = view['home'], view['id']
            session = self.index.monitor.sessions.get(key)
            if (session is None or hasattr(session, '_replay_body') or not view.get('collection_complete')
                    or session.running or session.pending or view.get('requests') or len(session.transports) > 1
                    or session.activity >= now - WINDOW or key in self.index.rebuild_required):
                continue
            proof = self.source_proof(key)
            if proof is None or len(proof) > HEADER_LIMIT or getattr(session, '_replay_source', None) != self.index.event_store.owner_replay_token(*key):
                continue
            prepared = current['sessions'].get(key)
            if prepared is None or len(prepared[2]) > BODY_LIMIT:
                continue
            header = {field: view[field] for field in ('home', 'id', 'activity', 'usage_revision', 'request_revision',
                'collection_complete', 'status', 'remaining', 'totals', 'groups') if field in view}
            state = self.session_state(session)
            packed = encoded(dict(format=FORMAT, header=header, session=state))
            if len(packed) > HEADER_LIMIT or self.weight + len(prepared[2]) + len(packed) > BODY_BUDGET:
                continue
            keys = list(session.keys)
            if len(keys) > PROOF_ROWS:
                continue
            home = home_key(key[0])
            bound=getattr(session,'_evidence_proof',None)
            if bound is None or bound[0] is not view['history'] or bound[1] is None:
                continue
            # This proof describes the committed public body, not a second
            # join against observations which may already have changed.
            evidence=bound[1]
            if self.admission_started is None:
                self.admission_started = time.monotonic()
            self.db.execute('INSERT INTO owners(home,sid,proof,header,digest,fingerprint,evidence,integrity) VALUES(?,?,?,?,?,?,?,?) '
                'ON CONFLICT(home,sid) DO UPDATE SET proof=excluded.proof,header=excluded.header,digest=excluded.digest,'
                'fingerprint=excluded.fingerprint,evidence=excluded.evidence,integrity=excluded.integrity',
                (*key, proof, packed, prepared[3], prepared[1], evidence,
                 integrity(proof, packed, prepared[3], prepared[1], evidence)))
            owner = self.db.execute('SELECT id FROM owners WHERE home=? AND sid=?', key).fetchone()[0]
            previous = {row[0] for row in self.db.execute('SELECT key FROM responses WHERE owner=?', (owner,))}
            current_keys = {response_key(home, response) for response in keys}
            removed, added = previous - current_keys, current_keys - previous
            self.db.executemany('DELETE FROM responses WHERE owner=? AND key=?', ((owner, response) for response in removed))
            self.db.executemany('INSERT INTO responses VALUES(?,?)', ((owner, response) for response in added))
            body = EncodedSession(header, prepared[2], prepared[1], prepared[3])
            self._retire(key, body, state)
            views[position] = body
            self.pending_admissions.add(key)
            self.pending_bytes += len(proof) + len(packed) + 128 + 64 * (len(removed) + len(added))
            if self.pending_bytes >= COMMIT_BYTES:
                break
        return self.capture_position < len(views)

    def close(self):
        self.capture_snapshot = None
        self.cleanup_keys.clear();self.cleanup_rows.clear();self.cleanup_active=None
        if self.restore_job is not None:
            self.restore_job.close()
            self.restore_job = None
        if self.db is None:return
        try:
            self.commit_admissions()
        except sqlite3.Error:
            try:self.db.rollback()
            except sqlite3.Error:pass
        finally:
            self.db.close()
