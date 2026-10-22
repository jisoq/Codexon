from collections import OrderedDict, deque
from contextlib import nullcontext
import json
import math
from pathlib import Path
import sqlite3
import time

from .analytics import call_identity, prepare_row
from .observer_state import ProcessLock

COLUMNS = ('기록 시각', '호출 모델', '응답모델', '입력', '출력', '추론',
           '캐시 적중률', '소요시간', '평균 출력 속도', '상태')
APPLICATION_ID = 0x434D4D48
FIELDS = (
    'home', 'sid', 'key', 'ts', 'requested_model', 'response_model', 'effort',
    'effort_source', 'configured_effort', 'requested_effort', 'requested_effort_source',
    'requested_effort_conflict', 'response_effort', 'response_effort_source',
    'response_effort_confirmed', 'response_effort_conflict', 'requested_service_tier',
    'service_tier', 'service_tier_source', 'configured_service_tier', 'request_mode_source',
    'request_mode_action', 'mode_conflict', 'wire_observed',
    'wire_service_tier_present', 'response_service_tier', 'response_tier_confirmed',
    'input', 'output', 'reasoning', 'cached', 'written', 'rate', 'duration', 'output_speed',
    'input_conflict', 'output_conflict', 'timing_valid', 'response_status', 'transport',
    'transport_source', 'request_observed_at', 'generation_observed_at', 'completed_observed_at',
    'model_observation_ts',
    'completion_latency_ms', 'generation_latency_ms', 'observation_missing',
    'model_conflict', 'model_evidence', 'model_match',
)


def archive_path(quota_path):
    return Path(quota_path).with_suffix('.mismatches.sqlite')


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def _identity(value):
    value = tuple(value)
    if len(value) not in (2, 3) or any(type(part) is not str or not part for part in value):
        raise ValueError('invalid mismatch call identity')
    return _dump(value)


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def confirmed_snapshot(row, *, observed_at):
    if not row.get('model_alert_confirmed'):
        return None
    if not row.get('requested_model') or not row.get('response_model'):
        raise ValueError('confirmed mismatch has no explicit model pair')
    prepared = prepare_row(row, dict(home=row['home'], id=row['sid'], title=''), price={})
    result = {name: prepared[name] for name in FIELDS if name in prepared}
    result['identity'] = call_identity(row['home'], row['sid'], row['key'])
    result['ts'] = next((value for value in (row.get('ts'), row.get('model_observation_ts'), observed_at)
                         if _finite(value)), observed_at)
    if not row.get('response_effort_confirmed') or row.get('response_effort_conflict'):
        result['response_effort'] = None
    if not row.get('response_tier_confirmed'):
        result['response_service_tier'] = None
    if row.get('requested_effort_conflict'):
        result['effort'] = None
        result['requested_effort'] = None
    _dump(result)
    return result


def _decode(row):
    result = json.loads(row['snapshot'])
    result['identity'] = tuple(result['identity'])
    result.update(first_captured_at=row['first_captured_at'], changed_at=row['changed_at'],
                  correction_at=row['correction_at'], correction_reason=row['correction_reason'])
    return result


class MismatchArchive:
    """One writer owns a compact snapshot and optional correction per canonical call."""
    def __init__(self, path, cancelled=lambda: False, *, pending_bytes=4 * 1024 * 1024):
        self.path = Path(path)
        self.cancelled = cancelled
        self.pending_limit = pending_bytes
        self.pending = OrderedDict()
        self.pending_bytes = 0
        self.db = self.lock = None
        self.revision = 0
        self.error = ''
        self.gap = False
        self.saved_gap = False
        self.needs_scan = True
        self.retry_at = 0.
        self.failures = 0
        self.scan = None
        self.waiting_scans = deque()

    def health(self):
        return dict(revision=self.revision, status='capture_gap' if self.gap else
                    'retry_pending' if self.error or self.pending else 'ready',
                    error=self.error, pending=len(self.pending), gap=self.gap, reconciling=self.has_work)

    def _open(self):
        if self.db is not None:
            return
        lock = ProcessLock(self.path.with_suffix(self.path.suffix + '.writer.lock'))
        lock.__enter__()
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=.025)
            db.row_factory = sqlite3.Row
            existing = db.execute('PRAGMA application_id').fetchone()[0]
            if existing not in (0, APPLICATION_ID):
                raise ValueError('mismatch archive belongs to another application')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA cache_size=-2048')
            db.execute('PRAGMA mmap_size=0')
            with db:
                db.execute(f'PRAGMA application_id={APPLICATION_ID}')
                db.execute('CREATE TABLE IF NOT EXISTS meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),'
                           'version INTEGER NOT NULL,revision INTEGER NOT NULL)')
                db.execute('INSERT OR IGNORE INTO meta(singleton,version,revision) VALUES(1,1,0)')
                columns={row[1] for row in db.execute('PRAGMA table_info(meta)')}
                if 'capture_gap' not in columns:
                    db.execute('ALTER TABLE meta ADD COLUMN capture_gap INTEGER NOT NULL DEFAULT 0')
                version, revision, saved_gap = db.execute('SELECT version,revision,capture_gap FROM meta WHERE singleton=1').fetchone()
                if version != 1:
                    raise ValueError('unsupported mismatch archive version')
                db.execute('CREATE TABLE IF NOT EXISTS calls(identity TEXT PRIMARY KEY,home TEXT NOT NULL,'
                           'sid TEXT NOT NULL,recorded_at REAL NOT NULL,snapshot TEXT NOT NULL,'
                           'first_captured_at REAL NOT NULL,changed_at REAL NOT NULL,'
                           'correction_at REAL,correction_reason TEXT)')
                db.execute('CREATE INDEX IF NOT EXISTS calls_time ON calls(recorded_at DESC,identity DESC)')
                db.execute('CREATE INDEX IF NOT EXISTS calls_owner ON calls(home,sid,identity)')
                db.execute('CREATE TABLE IF NOT EXISTS sources(identity TEXT NOT NULL,home TEXT NOT NULL,'
                           'sid TEXT NOT NULL,PRIMARY KEY(identity,home,sid)) WITHOUT ROWID')
                db.execute('CREATE INDEX IF NOT EXISTS sources_owner ON sources(home,sid,identity)')
                db.execute('INSERT OR IGNORE INTO sources SELECT identity,home,sid FROM calls')
            self.db, self.lock, self.revision = db, lock, revision
            self.saved_gap = bool(saved_gap)
            self.gap = self.gap or self.saved_gap
        except BaseException:
            if db is not None:
                db.close()
            lock.__exit__(None, None, None)
            raise

    def _failure(self, error):
        self.error = str(error) or type(error).__name__
        self.needs_scan = True
        self.failures += 1
        self.retry_at = time.monotonic() + min(30, .5 * 2 ** min(self.failures - 1, 6))
        if self.db is not None:
            self.db.rollback()

    def _saved(self, key):
        if key in self.pending:
            return self.pending[key][0]
        if self.db is None:
            return None
        row = self.db.execute('SELECT * FROM calls WHERE identity=?', (key,)).fetchone()
        return _decode(row) if row is not None else None

    def _queue(self, key, row):
        raw = _dump(row)
        old = self.pending.get(key)
        actual = len(raw.encode('utf-8')) + len(key.encode('utf-8')) + 256
        size = max(actual, old[1]) if old else actual + 2048
        if self.pending_bytes - (old[1] if old else 0) + size > self.pending_limit and self.db is not None and not self.error:
            self.retry(force=True)
            old = self.pending.get(key)
        if self.pending_bytes - (old[1] if old else 0) + size > self.pending_limit:
            self.gap = True
            self.needs_scan = True
            return
        if old:
            self.pending_bytes -= old[1]
        self.pending[key] = (row, size)
        self.pending_bytes += size

    def _observe(self, view, identity, at):
        if self.cancelled():
            raise InterruptedError('mismatch capture cancelled')
        key = _identity(identity)
        previous = self._saved(key)
        after = None
        selected = None
        pair = None
        contradictory = False
        confirmations = present = 0
        source_owners = set()
        reason = None
        while True:
            page = view.mismatch_occurrences(tuple(identity), after=after)
            for row in page.rows:
                present += 1
                source_owners.add((row['home'], row['sid']))
                snapshot = confirmed_snapshot(row, observed_at=at)
                if snapshot is not None:
                    confirmations += 1
                    current_pair = (snapshot['requested_model'], snapshot['response_model'])
                    contradictory = contradictory or pair is not None and pair != current_pair
                    pair = current_pair
                    if selected is None or ((snapshot.get('model_observation_ts') or 0, snapshot['sid']) >
                                            (selected.get('model_observation_ts') or 0, selected['sid'])):
                        selected = snapshot
                else:
                    reason = ('model_conflict' if row.get('model_conflict') else
                              'model_agrees' if row.get('requested_model') == row.get('response_model')
                              and row.get('requested_model') else 'confirmation_withdrawn')
            if page.complete:
                break
            after = page.after
        if not present:
            return
        if contradictory or confirmations and confirmations != present:
            reason = 'conflicting_occurrences'
        if selected is not None:
            if previous:
                selected['ts'] = previous['ts']
            value = dict(selected, first_captured_at=previous['first_captured_at'] if previous else at,
                         changed_at=at, correction_at=at if reason else None, correction_reason=reason)
        elif previous:
            value = dict(previous, correction_at=at, correction_reason=reason, changed_at=at)
        else:
            return
        value['source_owners'] = [list(owner) for owner in sorted(source_owners)]
        if previous:
            comparable = {name: value for name, value in previous.items()
                          if name not in ('changed_at', 'correction_at')}
            current = {name: value for name, value in value.items()
                       if name not in ('changed_at', 'correction_at')}
            if comparable == current:
                return
        self._queue(key, value)

    def _saved_keys(self, owners):
        if self.db is None:
            return
        selected = tuple(owners) if owners is not None else None
        groups = (None,) if selected is None else (selected[i:i + 128] for i in range(0, len(selected), 128))
        for group in groups:
            after = ''
            while True:
                where = 'identity>?'
                args = [after]
                if group is not None:
                    where += ' AND identity IN (SELECT identity FROM sources WHERE (home,sid) IN (VALUES ' + ','.join('(?,?)' for _ in group) + '))'
                    args.extend(part for owner in group for part in owner)
                rows = self.db.execute('SELECT identity FROM calls WHERE ' + where +
                                       ' ORDER BY identity LIMIT 128', args).fetchall()
                if not rows:
                    break
                for row in rows:
                    yield tuple(json.loads(row[0]))
                after = rows[-1][0]

    @property
    def has_work(self):
        return self.scan is not None or bool(self.waiting_scans)

    def capture(self, view, *, owners=None, seconds=.05, limit=64, release=None):
        full = self.needs_scan or owners is None
        if not full and not owners:
            if release is not None:
                release()
            return self.health()
        if len(self.waiting_scans) + (self.scan is not None) >= 2:
            self.gap = True
            self.needs_scan = True
            if release is not None:
                release()
            return self.retry(force=not self.error)
        self.waiting_scans.append(dict(view=view, release=release, owners=None if full else owners,
                                       phase='candidates', after=None, buffer=deque(), complete=False,
                                       saved=None, pending=None))
        if full:
            self.needs_scan = False
        return self.step(seconds=seconds, limit=limit)

    def _finish_scan(self):
        scan, self.scan = self.scan, None
        if scan is not None and scan['release'] is not None:
            scan['release']()

    def _next_identity(self, view, deadline):
        scan = self.scan
        while True:
            if time.monotonic() >= deadline:
                return None, None
            if scan['buffer']:
                return scan['buffer'].popleft(), scan['phase'] == 'changed'
            phase = scan['phase']
            if phase == 'saved':
                if scan['saved'] is None:
                    scan['saved'] = iter(self._saved_keys(scan['owners']))
                identity = next(scan['saved'], None)
                if identity is not None:
                    return identity, True
                scan['phase'] = 'pending'
                continue
            if phase == 'pending':
                if scan['pending'] is None:
                    scan['pending'] = iter(tuple(self.pending))
                key = next(scan['pending'], None)
                return (tuple(json.loads(key)), True) if key is not None else (None, False)
            if scan['complete']:
                scan.update(phase='changed' if phase == 'candidates' and scan['owners'] is not None else 'saved',
                            after=None, complete=False)
                continue
            if phase == 'candidates':
                page = view.mismatch_candidates(scan['owners'], after=scan['after'])
                scan['buffer'].extend(call_identity(*candidate['owner'], key)
                                      for candidate in page.rows for key in candidate['keys'])
            else:
                page = view.mismatch_changed_identities(scan['owners'], after=scan['after'])
                scan['buffer'].extend(page.rows)
            scan.update(after=page.after, complete=page.complete)

    def step(self, *, seconds=.05, limit=64, closing_deadline=None):
        deadline = time.monotonic() + seconds
        processed = 0
        self.retry()
        try:
            while processed < limit and time.monotonic() < deadline and not self.cancelled():
                if self.scan is None:
                    if not self.waiting_scans:
                        break
                    self.scan = self.waiting_scans.popleft()
                view = self.scan['view']
                with view.mismatch_shutdown(closing_deadline) if closing_deadline is not None else nullcontext():
                    identity, existing_only = self._next_identity(view, deadline)
                    if existing_only is None:
                        break
                    if identity is None:
                        self._finish_scan()
                        if self.error:
                            break
                        continue
                    processed += 1
                    if existing_only and self._saved(_identity(identity)) is None:
                        continue
                    self._observe(view, identity, time.time())
                if len(self.pending) >= 64 and self.db is not None and not self.error:
                    self.retry(force=True)
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
            self._finish_scan()
            self.gap = True
            self._failure(error)
        return self.retry(force=not self.error and (bool(self.pending) or self.gap != self.saved_gap),
                          closing=closing_deadline is not None)

    def retry(self, *, force=False, closing=False):
        now = time.monotonic()
        if not force and now < self.retry_at:
            return self.health()
        try:
            self._open()
            deadline = now + .05
            committed = []
            changed = self.gap and not self.saved_gap
            with self.db:
                if changed:
                    self.db.execute('UPDATE meta SET capture_gap=1 WHERE singleton=1')
                for key, (row, _) in self.pending.items():
                    if (self.cancelled() and not closing) or len(committed) >= 64 or time.monotonic() > deadline:
                        break
                    saved = self.db.execute('SELECT * FROM calls WHERE identity=?', (key,)).fetchone()
                    if saved is None or _decode(saved) != row:
                        snapshot = {name: value for name, value in row.items()
                                    if name not in ('first_captured_at', 'changed_at', 'correction_at', 'correction_reason')}
                        self.db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(identity) '
                                        'DO UPDATE SET home=excluded.home,sid=excluded.sid,snapshot=excluded.snapshot,'
                                        'changed_at=excluded.changed_at,correction_at=excluded.correction_at,'
                                        'correction_reason=excluded.correction_reason',
                                        (key, row['home'], row['sid'], row['ts'], _dump(snapshot),
                                         row['first_captured_at'], row['changed_at'], row['correction_at'], row['correction_reason']))
                        self.db.execute('DELETE FROM sources WHERE identity=?', (key,))
                        self.db.executemany('INSERT INTO sources VALUES(?,?,?)',
                                            ((key, home, sid) for home, sid in row.get('source_owners',
                                                                                     [(row['home'], row['sid'])])))
                        changed = True
                    committed.append(key)
                if changed:
                    self.db.execute('UPDATE meta SET revision=revision+1 WHERE singleton=1')
            for key in committed:
                _, size = self.pending.pop(key)
                self.pending_bytes -= size
            self.revision = self.db.execute('SELECT revision FROM meta WHERE singleton=1').fetchone()[0]
            self.saved_gap = self.gap
            if self.failures:
                self.needs_scan = True
            self.failures = 0
            self.error = ''
            self.retry_at = time.monotonic() + .05 if self.pending else float('inf')
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
            self._failure(error)
        return self.health()

    def close(self, *, deadline=None):
        end=min(deadline if deadline is not None else float('inf'),time.monotonic()+.2)
        cancelled,self.cancelled=self.cancelled,lambda:time.monotonic()>=end
        try:
            while self.has_work and time.monotonic()<end:
                self.step(seconds=min(.05,end-time.monotonic()),closing_deadline=end)
            while self.pending and time.monotonic()<end:
                self.retry(force=True,closing=True)
                if self.error:
                    break
            if self.has_work or self.pending:
                self.gap=True
            self._finish_scan()
            while self.waiting_scans:
                self.scan=self.waiting_scans.popleft()
                self._finish_scan()
            if self.gap != self.saved_gap or self.pending:
                self.retry(force=True,closing=True)
        finally:
            self.cancelled=cancelled
            if self.db is not None:
                self.db.close()
                self.db=None
            if self.lock is not None:
                self.lock.__exit__(None,None,None)
                self.lock=None



class MismatchReader:
    """An actor-local read-only connection with bounded independent windows."""
    def __init__(self, path):
        self.path = Path(path)
        self.db = None

    def _open(self):
        if self.db is None:
            if not self.path.exists():
                return False
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.05)
            try:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                db.execute('PRAGMA cache_size=-2048')
                if db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID:
                    raise ValueError('invalid mismatch archive')
                if db.execute('SELECT version FROM meta WHERE singleton=1').fetchone()[0] != 1:
                    raise ValueError('unsupported mismatch archive version')
            except BaseException:
                db.close()
                raise
            self.db = db
        return True

    def _read(self, operation, cancel):
        if not self._open():
            return operation(None, 0)
        deadline = time.monotonic() + 2
        self.db.set_progress_handler(lambda: int(cancel() or time.monotonic() > deadline), 1000)
        try:
            self.db.execute('BEGIN')
            revision = self.db.execute('SELECT revision FROM meta WHERE singleton=1').fetchone()[0]
            return operation(self.db, revision)
        finally:
            self.db.rollback()
            self.db.set_progress_handler(None, 0)

    def window(self, *, start=0, size=128, cancel=lambda: False):
        if type(start) is not int or start < 0 or type(size) is not int or size <= 0:
            raise ValueError('invalid mismatch window')
        size = min(size, 512)
        def read(db, revision):
            total = db.execute('SELECT count(*) FROM calls').fetchone()[0] if db is not None else 0
            offset = min(start, max(0, total - 1))
            rows = db.execute('SELECT * FROM calls ORDER BY recorded_at DESC,identity DESC LIMIT ? OFFSET ?',
                              (size, offset)).fetchall() if db is not None else ()
            gap=bool(db.execute('SELECT capture_gap FROM meta WHERE singleton=1').fetchone()[0]) if db is not None else False
            return dict(window=dict(start=offset, total=total, rows=[_decode(row) for row in rows]),
                        revision=revision,capture_gap=gap)
        return self._read(read, cancel)

    def record(self, identity, *, cancel=lambda: False):
        key = _identity(identity)
        def read(db, revision):
            row = db.execute('SELECT * FROM calls WHERE identity=?', (key,)).fetchone() if db is not None else None
            return dict(row=_decode(row) if row is not None else None, revision=revision)
        return self._read(read, cancel)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None