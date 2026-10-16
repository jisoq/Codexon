"""Publish prepared speed state with its exact canonical fact generation."""
from dataclasses import dataclass

from .prepared_store import IncompletePublication, ReadCancelled, Snapshot, Stage, StaleGeneration, pack
from .prepared_speed import OwnerSpeedStamp, PreparedSpeedInput, speed_prefix_signature
from .speed_checkpoint import (
    CHECKPOINT_BYTES, CHUNK_BYTES, CheckpointRejected, Section,
    export_checkpoint, restore_checkpoint,
)
from .speed_health import MAX_TRACKED_SCOPES, SpeedHealth


SPEED_CHECKPOINT_NAMESPACE = 'speed-health-v2'


@dataclass(frozen=True, slots=True)
class SpeedPrefixCertificate:
    revision: str
    length: int
    last_ordinal: int | None

    @property
    def append_from(self):
        return self.revision, self.length

    @property
    def tail_after(self):
        return self.last_ordinal


def certify_speed_prefix(snapshot, owner, previous_stamp, canonical_rows):
    """Compare a pinned committed owner prefix without retaining fingerprints.

    The caller supplies its persisted typed predecessor metadata and newly
    elected canonical owner rows before replacing that owner's facts. A raw
    ordinal is not a dense count: both are verified against the typed cursor.
    A changed or shortened prefix returns None and requires scope replay.
    """
    if not isinstance(snapshot, Snapshot) or snapshot.staged_batch is not None:
        raise TypeError('speed prefix requires a pinned committed snapshot')
    if type(previous_stamp) is not OwnerSpeedStamp:
        raise TypeError('speed prefix requires typed predecessor metadata')
    current = iter(canonical_rows)
    missing = object()
    old_rows = snapshot.iter_speed_rows({owner: None})
    length, frontier, equal = 0, None, True
    try:
        for old_owner, raw_ordinal, old_row in old_rows:
            if (old_owner != owner or type(raw_ordinal) is not int
                    or raw_ordinal <= (frontier or 0)
                    or old_row['session_ordinal'] != raw_ordinal):
                raise ValueError('invalid canonical predecessor stream')
            length += 1
            frontier = raw_ordinal
            if equal:
                new_row = next(current, missing)
                equal = (length <= previous_stamp.length and new_row is not missing
                         and speed_prefix_signature(old_row) == speed_prefix_signature(new_row))
        if (not equal or length != previous_stamp.length
                or frontier != previous_stamp.last_ordinal):
            return None
        return SpeedPrefixCertificate(previous_stamp.revision, length, frontier)
    finally:
        old_rows.close()


def _token(token):
    return token.instance, token.generation


def _frontier(stamp):
    return stamp.revision, stamp.length, stamp.last_ordinal


def _require(condition, message):
    if not condition:
        raise CheckpointRejected(message)


class PreparedSpeedWriter:
    """One writer-owned reducer with a single staged publication in flight.

    ``prepare`` receives complete current and previous typed owner metadata;
    it never reconstructs a catalog. Initial conversion can omit this adapter
    until the final ready publication, then classify the complete input once.
    A warm restore reads checkpoint sections only, never historical calls.

    Ordinary publication serializes changed owner and peer sections only.
    Missing or rejected checkpoints require an explicit full recovery export.
    The detached checkpoint payload is bounded by 64 MiB and released before
    prepare returns. Capacity fallback retains exact classifier results, but
    cannot checkpoint incremental state; a later restart must classify again.
    """
    def __init__(self, store, *, preparation, cancel=None, read_seconds=None):
        self.store = store
        self.preparation = preparation
        self.cancel = cancel
        self.read_seconds = read_seconds
        self.source = PreparedSpeedInput(store, cancel=cancel, read_seconds=read_seconds)
        self.tracker = SpeedHealth()
        self._committed_token = None
        self._trusted_checkpoint_token = None
        self._pending_stage = None
        self._pending_token = None
        self.last_checkpoint = None
        self.last_read_rows = 0

    def _check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise ReadCancelled('prepared speed work was cancelled')

    def _current(self, token, previous):
        return (self._committed_token == token
                and self.source.token == token and self.source.stage is None
                and self.tracker.checkpoint_token == token
                and self.tracker.engine_ref is not None
                and self.tracker.engine_ref() is self.store
                and self.source.owners.keys() == previous.keys()
                and all(_frontier(stamp) == _frontier(previous[owner])
                        for owner, stamp in self.source.owners.items()))

    def _sections(self, snapshot, head, owner_count):
        count, expected_bytes = head['sections'], head['encoded_bytes']
        _require(type(count) is int and 1 <= count <= min(
            self.store.limits.checkpoint_sections, owner_count + MAX_TRACKED_SCOPES + 1),
            'invalid stored speed checkpoint section count')
        _require(type(expected_bytes) is int and 0 < expected_bytes <= min(
            CHECKPOINT_BYTES, self.store.limits.checkpoint_bytes),
            'invalid stored speed checkpoint size')
        sections, total, after = [], 0, None
        while True:
            page = snapshot.checkpoint_sections(SPEED_CHECKPOINT_NAMESPACE, after=after)
            for info in page.rows:
                _require(len(sections) < count, 'stored speed checkpoint has extra sections')
                chunks, chunk_after, section_bytes = [], None, 0
                while True:
                    chunk_page = snapshot.checkpoint_chunks(
                        SPEED_CHECKPOINT_NAMESPACE, info['name'], after=chunk_after)
                    for row in chunk_page.rows:
                        chunk = row['payload']
                        _require(row['ordinal'] == len(chunks) and type(chunk) is bytes
                                 and 0 < len(chunk) <= CHUNK_BYTES,
                                 'invalid stored speed checkpoint chunk')
                        section_bytes += len(chunk)
                        total += len(chunk)
                        _require(section_bytes <= info['encoded_bytes'] and total <= expected_bytes,
                                 'stored speed checkpoint exceeds its declared size')
                        chunks.append(chunk)
                    if chunk_page.complete:
                        break
                    chunk_after = chunk_page.after
                _require(len(chunks) == info['chunks'] and section_bytes == info['encoded_bytes'],
                         'stored speed checkpoint section is incomplete')
                sections.append(Section(info['name'], info['digest'], tuple(chunks)))
            if page.complete:
                break
            after = page.after
        _require(len(sections) == count and total == expected_bytes
                 and sections[0].name == 'header', 'stored speed checkpoint is incomplete')
        return tuple(sections)

    def _restore_current(self, stamps, token, encoded_source=None):
        use_memory = self._current(token, stamps)
        sections, base, recovery = None, None, None
        try:
            with self.store.read_current(cancel=self.cancel, seconds=self.read_seconds) as snapshot:
                if snapshot.token != token:
                    raise StaleGeneration('speed checkpoint predecessor changed')
                source = pack(snapshot.source_identity, self.store.limits)
                if encoded_source is not None and source != encoded_source:
                    raise ValueError('speed current source differs from the committed source')
                if not use_memory:
                    self.source.bind(token, stamps, source)
                head = snapshot.checkpoint_state(SPEED_CHECKPOINT_NAMESPACE)
                if head is not None and head['token'] == _token(token):
                    if use_memory and self._trusted_checkpoint_token != token:
                        recovery = 'untrusted checkpoint'
                    else:
                        base = head['token']
                    if not use_memory:
                        sections = self._sections(snapshot, head, len(stamps))
                else:
                    recovery = 'missing checkpoint' if head is None else 'stale checkpoint'
        except CheckpointRejected:
            recovery, base, sections = 'rejected checkpoint', None, None
        restored = False
        if not use_memory:
            self.tracker.__init__()
            if sections is not None:
                try:
                    restore_checkpoint(self.tracker, self.source, sections,
                                       preparation=self.preparation)
                    restored = True
                    self._trusted_checkpoint_token = token
                except CheckpointRejected:
                    recovery, base = 'rejected checkpoint', None
            if not restored:
                self._trusted_checkpoint_token = None
        return base, recovery, restored

    def current_results(self, current_owner_stamps, encoded_current_source):
        """Read or recover current results without staging or writing a generation.

        Exact checkpoints restore without historical calls. A missing or
        rejected checkpoint requires one current canonical replay, then this
        writer reuses that state until its committed input changes.
        """
        if self._pending_stage is not None:
            raise IncompletePublication('finish or abort the prepared speed publication first')
        try:
            self._check()
            if type(encoded_current_source) is not bytes:
                raise TypeError('speed source identity must be the encoded writer packet')
            token = self.store.token
            _, recovery, restored = self._restore_current(
                current_owner_stamps, token, encoded_current_source)
            result = self.tracker.collect_prepared(self.source)
            self._check()
            self.last_read_rows = self.source.read_rows
            self._committed_token = token
            self.last_checkpoint = dict(mode='read-only', restored=restored,
                                        recovery=recovery, sections=0, encoded_bytes=0)
            return result
        except BaseException:
            self.discard()
            raise

    def prepare(self, stage, current_owner_stamps, encoded_current_source, previous_owner_stamps):
        """Classify staged facts and persist their checkpoint in the same stage.

        The caller must finish with publish(), or call abort()/discard() on
        every failed outer publication. Results become public only at commit.
        """
        if self._pending_stage is not None:
            raise IncompletePublication('finish or abort the prepared speed publication first')
        try:
            self._check()
            if (not isinstance(stage, Stage) or stage.store is not self.store
                    or self.store.stage is not stage or stage.expected != self.store.token):
                raise StaleGeneration('speed preparation requires the current writer stage')
            if type(encoded_current_source) is not bytes or encoded_current_source != stage.source:
                raise ValueError('speed preparation source differs from the staged source')
            for owner, old in previous_owner_stamps.items():
                if type(old) is not OwnerSpeedStamp:
                    raise TypeError('speed predecessor requires typed owner metadata')
                new = current_owner_stamps.get(owner)
                if (new is not None and new.revision == old.revision
                        and _frontier(new) != _frontier(old)):
                    raise ValueError('canonical speed frontier changed without a dependency revision')
            base, recovery, restored = self._restore_current(previous_owner_stamps, stage.expected)
            self.source.bind_stage(stage, current_owner_stamps, encoded_current_source)
            result = self.tracker.collect_prepared(self.source)
            self._check()
            self.last_read_rows = self.source.read_rows
            if self.tracker.metrics['capacity_fallback']:
                self.last_checkpoint = dict(mode='capacity-fallback', restored=restored,
                                            recovery=recovery, sections=0, encoded_bytes=0)
            else:
                delta = export_checkpoint(self.tracker, self.source,
                                          preparation=self.preparation, base=base)
                self._check()
                stage.checkpoint(SPEED_CHECKPOINT_NAMESPACE,
                    ((section.name, section.digest, section.chunks) for section in delta.sections),
                    base=delta.base, token=delta.token, tombstones=delta.tombstones,
                    complete=delta.complete)
                self._check()
                self.last_checkpoint = dict(mode='complete' if delta.complete else 'delta',
                    restored=restored, recovery=recovery,
                    sections=len(delta.sections), tombstones=len(delta.tombstones),
                    encoded_bytes=sum(len(chunk) for section in delta.sections for chunk in section.chunks))
            self._pending_stage, self._pending_token = stage, self.source.token
            return result
        except BaseException:
            self.discard()
            raise

    def committed(self, stage, token):
        """Complete a successful external Stage.publish without another read."""
        if (stage is not self._pending_stage or not stage.finished
                or token != self._pending_token or token != self.store.token):
            self.discard()
            raise StaleGeneration('speed classifier was not published with its fact generation')
        self.source.bind(token, self.source.owners, self.source.source_identity)
        self._committed_token = token
        self._trusted_checkpoint_token = (token if self.last_checkpoint['mode'] != 'capacity-fallback'
                                          else None)
        self._pending_stage = self._pending_token = None
        return token

    def publish(self, stage, source_identity, *, ready=True):
        try:
            if stage is not self._pending_stage:
                raise IncompletePublication('prepare speed before publishing this stage')
            self._check()
            token = stage.publish(source_identity, ready=ready)
            return self.committed(stage, token)
        except BaseException:
            self.abort(stage)
            raise

    def discard(self):
        """Drop all untrusted in-memory state after an external failure."""
        self.tracker.__init__()
        self._committed_token = self._trusted_checkpoint_token = None
        self._pending_stage = self._pending_token = None
        self.source.token = self.source.stage = self.source.source_identity = None
        self.source.owners = {}

    def abort(self, stage=None):
        stage = stage or self._pending_stage
        self.discard()
        if stage is not None:
            if not isinstance(stage, Stage) or stage.store is not self.store:
                raise TypeError('speed abort requires its writer stage')
            if self.store.stage is stage and not stage.finished:
                stage.abort()

    def close(self):
        try:
            self.abort()
        finally:
            self.source.close()
