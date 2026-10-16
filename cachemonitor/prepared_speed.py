"""Explicit prepared-writer input to the shared SpeedHealth reducer.

The writer creates OwnerSpeedStamp only after electing canonical calls. An
append certificate covers the exact old prepared prefix, including its raw
session_ordinal frontier. This is not a duck-typed proof of canonical input.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib

from .prepared_store import DEPENDENCIES, PreparedReader, PreparedStore, Stage, Token, pack


def speed_prefix_signature(row):
    """Exact typed speed semantics for one canonical row, including raw order.

    Compare these transient bytes for every old-prefix position while both
    generations are pinned. Invalid rows remain in the comparison: their key,
    timestamp and raw ordinal still affect resets and dense identity order.
    Scope already evaluates every eligibility, model, tier and timing input;
    only eligible rows contribute the reducer's output and latency scalars.
    No per-call signature population is retained by this helper.
    """
    from .speed_health import scope
    condition = scope(row)
    return pack((row['key'], row['ts'], row['session_ordinal'], condition,
                 row['output'] if condition is not None else None,
                 row['completion_latency_ms'] if condition is not None else None))


@dataclass(frozen=True, slots=True)
class OwnerSpeedStamp:
    revision: str
    length: int
    last_ordinal: int | None
    append_from: tuple | None = None
    tail_after: int | None = None

    def __post_init__(self):
        if type(self.revision) is not str or len(self.revision) != 64:
            raise ValueError('owner speed revision must be the durable dependency digest')
        if type(self.length) is not int or self.length < 0:
            raise ValueError('invalid canonical call count')
        if self.length:
            if type(self.last_ordinal) is not int or self.last_ordinal < self.length:
                raise ValueError('invalid canonical raw ordinal frontier')
        elif self.last_ordinal is not None:
            raise ValueError('empty owner has a raw ordinal frontier')
        if self.append_from is None:
            if self.tail_after is not None:
                raise ValueError('tail frontier requires an append certificate')
        else:
            if (type(self.append_from) is not tuple or len(self.append_from) != 2
                    or type(self.append_from[0]) is not str or len(self.append_from[0]) != 64
                    or type(self.append_from[1]) is not int or not 0 <= self.append_from[1] <= self.length):
                raise ValueError('invalid append predecessor')
            old_length = self.append_from[1]
            if old_length:
                if (type(self.tail_after) is not int or self.tail_after < old_length
                        or self.last_ordinal is None or self.tail_after > self.last_ordinal):
                    raise ValueError('invalid certified tail frontier')
            elif self.tail_after is not None:
                raise ValueError('empty predecessor requires the complete owner range')

    @classmethod
    def from_dependencies(cls, dependencies, length, last_ordinal, *, append_from=None, tail_after=None):
        """The writer has elected all canonical rows before issuing this stamp.

        An append predecessor is (old durable revision, old dense length).
        Issue it only after exact speed_prefix_signature equality at every
        old-prefix position. tail_after is that predecessor's raw last_ordinal;
        the new last_ordinal comes from the new canonical final row. Without
        the proof, omit both append fields and the shared reducer replays the
        affected peer scopes. All dependency values must be durable identities.
        """
        if type(dependencies) is not dict or not set(DEPENDENCIES) <= dependencies.keys():
            raise ValueError('incomplete durable owner dependencies')
        revision = hashlib.sha256(pack(dependencies)).hexdigest()
        return cls(revision, length, last_ordinal, append_from, tail_after)


class PreparedSpeedInput:
    """A stable writer-owned binding to one exact, committed prepared token.

    ``owners`` comes from complete persisted canonical owner metadata. Do not
    construct it from a partial changed-owner working set. The store read
    snapshot verifies the token and encoded source identity before streaming.
    The writer withholds publication to consumers until classifier completion.
    """
    def __init__(self, store, *, limits=None, cancel=None, read_seconds=None):
        if not isinstance(store, PreparedStore):
            raise TypeError('PreparedSpeedInput requires the actual prepared writer')
        self.store = store
        self.reader = PreparedReader(store.path, limits or store.limits)
        self.cancel = cancel
        self.read_seconds = read_seconds
        self.instance = store.token.instance
        self.token = None
        self.owners = {}
        self.source_identity = None
        self.snapshot = None
        self.stage = None
        self.read_rows = 0

    @property
    def binding(self):
        return self.store

    def bind(self, token, owners, source_identity):
        if not isinstance(token, Token) or token.instance != self.instance or token != self.store.token:
            raise ValueError('prepared speed token is not the current writer generation')
        self._bind(token,owners,source_identity,None)

    def bind_stage(self, stage, owners, source_identity):
        if not isinstance(stage,Stage) or stage.store is not self.store or self.store.stage is not stage:
            raise TypeError('prepared speed stage is not owned by this writer')
        self._bind(stage.prospective_token,owners,source_identity,stage)

    def _bind(self,token,owners,source_identity,stage):
        if type(source_identity) is not bytes or len(source_identity) > self.store.limits.packet_bytes:
            raise ValueError('invalid encoded prepared source identity')
        if self.snapshot is not None:
            raise RuntimeError('cannot replace a pinned speed generation')
        for owner, stamp in owners.items():
            if (type(owner) is not tuple or len(owner) != 2
                    or any(type(part) is not str or not part for part in owner)
                    or type(stamp) is not OwnerSpeedStamp):
                raise TypeError('owner metadata was not certified by the prepared writer')
        self.token, self.owners, self.source_identity = token, dict(owners), source_identity
        self.stage=stage

    @contextmanager
    def read_scope(self):
        if self.token is None or self.snapshot is not None:
            raise RuntimeError('invalid prepared speed read scope')
        scope=(self.stage.read_snapshot(cancel=self.cancel, seconds=self.read_seconds)
               if self.stage is not None else self.reader._pin(
                   self.token, cancel=self.cancel, seconds=self.read_seconds,
                   allow_incomplete=True))
        with scope as snapshot:
            if getattr(snapshot,'publication_token',snapshot.token)!=self.token:
                raise ValueError('prepared speed snapshot has a different publication token')
            if pack(snapshot.source_identity, self.store.limits) != self.source_identity:
                raise ValueError('prepared source identity does not match the certified generation')
            self.snapshot = snapshot
            self.read_rows = 0
            try:
                yield self
            finally:
                self.snapshot = None

    def rows(self, owner, start):
        for _, ordinal, row in self.ordered({owner: start}):
            yield ordinal, row

    def ordered(self, starts):
        """Translate certified raw frontiers to dense classifier ordinals.

        The store contract is iter_speed_rows({owner: raw_ordinal_or_None}):
        strict-after raw ordinal filtering, one cursor in ts/home/sid/raw-order,
        no OFFSET, and no filtering of invalid or other-condition rows.
        """
        if self.snapshot is None:
            raise RuntimeError('speed rows require the pinned prepared generation')
        if not starts:
            return
        ranges, counts, raw_frontiers = {}, {}, {}
        for owner, start in starts.items():
            stamp = self.owners[owner]
            if type(start) is not int or not 0 <= start <= stamp.length:
                raise ValueError('invalid dense speed range')
            if start:
                if stamp.append_from is None or stamp.append_from[1] != start:
                    raise ValueError('dense tail has no certified raw frontier')
                ranges[owner] = stamp.tail_after
            else:
                ranges[owner] = None
            counts[owner] = start
            raw_frontiers[owner] = ranges[owner] or 0
        previous = None
        for owner, raw_ordinal, row in self.snapshot.iter_speed_rows(ranges):
            if owner not in ranges or type(raw_ordinal) is not int or raw_ordinal <= raw_frontiers[owner]:
                raise ValueError('prepared stream violates its selected owner frontier')
            order = (row['ts'], *owner, raw_ordinal)
            if previous is not None and order < previous:
                raise ValueError('prepared speed stream is not canonical chronological order')
            previous = order
            stamp = self.owners[owner]
            if counts[owner] >= stamp.length or raw_ordinal > stamp.last_ordinal:
                raise ValueError('prepared stream exceeds its canonical owner proof')
            ordinal = counts[owner]
            counts[owner] += 1
            raw_frontiers[owner] = raw_ordinal
            self.read_rows += 1
            yield owner, ordinal, row
        if any(count != self.owners[owner].length for owner, count in counts.items()):
            raise ValueError('prepared stream is incomplete for its canonical owner proof')

    def full_classify(self):
        # Exact compatibility path only when the shared incremental allowance
        # rejects the input. It is not a normal warm-start or append path.
        from .speed_health import classify
        sessions = {owner: dict(home=owner[0], id=owner[1], history=[]) for owner in self.owners}
        for owner, _, row in self.ordered({owner: 0 for owner in self.owners}):
            sessions[owner]['history'].append(row)
        return classify(sessions.values())

    def close(self):
        self.reader.close()
