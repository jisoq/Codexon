"""Publish prepared speed state with its exact canonical fact generation."""
from dataclasses import dataclass, replace
from threading import Condition, Event, Thread
import sys
import traceback

from .prepared_store import (CapacityExceeded, IncompletePublication, PreparedReader, ReadCancelled,
                             Snapshot, Stage, StaleGeneration, WriteDeferred, _size, pack, unpack)
from .prepared_speed import OwnerSpeedStamp, PreparedSpeedInput, speed_prefix_signature
from .speed_checkpoint import (
    CHECKPOINT_BYTES, CHUNK_BYTES, CheckpointDelta, CheckpointRejected, Section,
    export_checkpoint, promote_checkpoint, restore_checkpoint,
)
from .speed_health import MAX_METADATA_BYTES, MAX_TRACKED_SCOPES, SpeedHealth


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


def _stored_sections(snapshot, head, owner_count):
    count, expected_bytes = head['sections'], head['encoded_bytes']
    _require(type(count) is int and 1 <= count <= min(
        snapshot.limits.checkpoint_sections, owner_count + MAX_TRACKED_SCOPES + 1),
        'invalid stored speed checkpoint section count')
    _require(type(expected_bytes) is int and 0 < expected_bytes <= min(
        CHECKPOINT_BYTES, snapshot.limits.checkpoint_bytes),
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

    @property
    def results(self):
        return self.tracker.results

    def owner_stamps(self, token):
        return self.source.owners if self.source.token==token and self.source.stage is None else None

    def prefix_certificate(self, owner, previous, rows):
        with self.store.read_current(seconds=self.read_seconds) as snapshot:
            return certify_speed_prefix(snapshot,owner,previous,rows)

    def publish_facts(self, stage, source_identity, current, previous):
        result=self.prepare(stage,current,stage.source,previous)
        return self.publish(stage,source_identity,ready=True),result

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

    @property
    def checkpoint_recovery_needed(self):
        token = self.store.token
        return (self._current(token, self.source.owners) and
                self._trusted_checkpoint_token != token and bool(self.tracker.retention))

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
                        sections = _stored_sections(snapshot, head, len(stamps))
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


@dataclass(frozen=True,slots=True)
class _WarningTarget:
    token: object
    source: bytes
    owners: dict
    prefixes: dict

    @property
    def scope(self):
        value=unpack(self.source)
        return self.token.instance,*value[:3],*value[4:]


@dataclass(frozen=True,slots=True)
class _WarningCompletion:
    target: _WarningTarget
    results: dict | None
    delta: object
    metrics: dict
    read_rows: int
    checkpoint: dict
    error: tuple | None = None


def _same_facts(left,right):
    return (left.scope==right.scope and left.owners.keys()==right.owners.keys()
            and all(_frontier(value)==_frontier(right.owners[owner]) for owner,value in left.owners.items()))


def _target_bytes(target):
    return (sys.getsizeof(target.owners)+_size(target.prefixes)+
            sum(_size((owner,_frontier(value),value.append_from,value.tail_after))
                for owner,value in target.owners.items()))


class PreparedWarningWorker:
    def __init__(self,store,*,preparation,cancel=None,read_seconds=None,wake=None):
        self.store=store
        self.path,self.limits=store.path,store.limits
        self.preparation=preparation
        self.cancel=cancel
        self.read_seconds=read_seconds
        self.wake=wake
        self.condition=Condition()
        self.stopped=Event()
        self.interrupt=Event()
        self._target=self._known=self._failure=None
        self._pending=self._active=self._anchor=self._completed=self._ack=None
        self._paused=False
        self._committed_token=self._trusted_checkpoint_token=None
        self.last_checkpoint=None
        self.last_read_rows=0
        self.last_metrics={}
        self.thread=Thread(target=self._run,name='prepared-speed-warning',daemon=True)
        self.thread.start()

    @property
    def checkpoint_recovery_needed(self):return False

    @property
    def results(self):
        target=self._target
        if target is None:return {}
        if self._known is not None and _same_facts(self._known.target,target):
            return self._known.results
        failed=self._failure is not None and self._failure.target.token==target.token
        value=dict(active=None,status='unavailable' if failed else 'pending')
        inactive=dict(active=False)
        return {owner:value if stamp.length else inactive for owner,stamp in target.owners.items()}

    def owner_stamps(self,token):
        return self._target.owners if self._target is not None and self._target.token==token else None

    def prefix_certificate(self,owner,previous,rows):return None

    def _offer(self,token,owners,source):
        if self._target is not None and self._target.token==token:return
        previous=self._target
        current=_WarningTarget(token,source,dict(owners),{})
        with self.condition:
            anchor=self._active or self._anchor
            bridge={}
            if anchor is not None and anchor.scope!=current.scope:
                self.interrupt.set();self._completed=None
                anchor=None
            elif self._pending is not None:
                bridge=dict(self._pending.prefixes)
            if anchor is not None and previous is not None:
                for owner in previous.owners.keys()|current.owners.keys():
                    old=previous.owners.get(owner);new=current.owners.get(owner)
                    if old is not None and new is not None and _frontier(old)==_frontier(new):continue
                    if owner in bridge and bridge[owner] is None:continue
                    origin=anchor.owners.get(owner)
                    if (origin is not None and old is not None and new is not None
                            and new.append_from==(old.revision,old.length)
                            and new.tail_after==old.last_ordinal
                            and (owner in bridge or _frontier(old)==_frontier(origin))):
                        bridge[owner]=SpeedPrefixCertificate(origin.revision,origin.length,origin.last_ordinal)
                    else:bridge[owner]=None
                bridge={owner:value for owner,value in bridge.items()
                        if owner in anchor.owners or owner in current.owners}
            current=replace(current,prefixes=bridge)
            self._target=current
            if self._known is not None and not _same_facts(self._known.target,current):self._known=None
            self._failure=None
            allowance=_target_bytes(current)+(_target_bytes(anchor) if anchor is not None else 0)
            if allowance>MAX_METADATA_BYTES:
                current=replace(current,prefixes={})
                self._target=current
                allowance=_target_bytes(current)+(_target_bytes(anchor) if anchor is not None else 0)
            if allowance>MAX_METADATA_BYTES:
                self._failure=_WarningCompletion(current,None,None,{},0,{},
                    ('CapacityExceeded','warning target metadata allowance',''))
                self._pending=None
            else:self._pending=current
            self.condition.notify()

    def current_results(self,current_owner_stamps,encoded_current_source):
        self._offer(self.store.token,current_owner_stamps,encoded_current_source)
        return self.results

    def publish_facts(self,stage,source_identity,current,previous):
        for owner,old in previous.items():
            new=current.get(owner)
            if new is not None and new.revision==old.revision and _frontier(new)!=_frontier(old):
                raise ValueError('canonical speed frontier changed without a dependency revision')
        token=stage.publish(source_identity,ready=True)
        self._committed_token=token
        self._offer(token,current,stage.source)
        return token,self.results

    def cancel_optional(self):
        with self.condition:
            self._paused=True
            self.interrupt.set()
            if self._active is not None and self._pending is None:self._pending=self._target
            self.condition.notify()

    def resume_optional(self):
        with self.condition:
            self._paused=False
            self.condition.notify()

    def _cancelled(self):
        return (self.stopped.is_set() or self.interrupt.is_set() or
                self.cancel is not None and self.cancel.is_set())

    def _run(self):
        reader=None
        tracker=SpeedHealth()
        scope=durable=None
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda:self.stopped.is_set() or self._ack is not None
                        or not self._paused and self._pending is not None)
                    if self.stopped.is_set():break
                    ack,self._ack=self._ack,None
                    if ack is not None:
                        basis,committed,trusted=ack
                        durable=committed.token if trusted else None
                        if tracker.checkpoint_token==basis.token:
                            tracker.checkpoint_token=committed.token
                            if self._anchor is not None and self._anchor.token==basis.token:self._anchor=committed
                        ack=basis=committed=None
                    if self._paused:continue
                    target,self._pending=self._pending,None
                    if target is None:continue
                    self._active=target;self._completed=None
                    self.interrupt.clear()
                source=view=actual=sections=header=result=delta=completion=None
                owner=certificate=checkpoint=head=failure=None
                try:
                    if reader is None:reader=PreparedReader(self.path,self.limits)
                    with reader.pin(target.token,cancel=self._cancelled,seconds=self.read_seconds) as view:
                        source=PreparedSpeedInput.from_snapshot(view)
                        actual=_WarningTarget(source.token,source.source_identity,source.owners,{})
                        if target.source!=actual.source or not _same_facts(target,actual):
                            raise StaleGeneration('warning target differs from the public canonical snapshot')
                        if scope!=target.scope:tracker.__init__();durable=None
                        scope=target.scope
                        restored=False;recovery=None;header=None
                        if tracker.engine_ref is None:
                            head=view.checkpoint_state(SPEED_CHECKPOINT_NAMESPACE)
                            if head is not None and head['token']==_token(target.token):
                                try:
                                    sections=_stored_sections(view,head,len(source.owners))
                                    restore_checkpoint(tracker,source,sections,preparation=self.preparation)
                                    header=sections[0]
                                    durable=target.token;restored=True
                                except CheckpointRejected:
                                    recovery='rejected checkpoint';durable=None
                                finally:
                                    sections=None
                            else:recovery='missing checkpoint' if head is None else 'stale checkpoint'
                        for owner,certificate in target.prefixes.items():
                            if certificate is not None and owner in source.owners:
                                source.owners[owner]=replace(source.owners[owner],
                                    append_from=certificate.append_from,tail_after=certificate.tail_after)
                        result=tracker.collect_prepared(source)
                        view.check()
                        rows=source.read_rows
                        with self.condition:catchup=self._pending is not None
                        delta=None
                        if not catchup and not tracker.metrics['capacity_fallback']:
                            if restored:
                                delta=CheckpointDelta(_token(target.token),_token(target.token),(header,),(),False)
                            else:
                                base=_token(durable) if durable is not None and tracker.checkpoint_from_token==durable else None
                                delta=export_checkpoint(tracker,source,preparation=self.preparation,base=base)
                        view.check()
                        checkpoint=dict(mode=('capacity-fallback' if tracker.metrics['capacity_fallback'] else
                            'complete' if delta is not None and delta.complete else 'delta'),
                            restored=restored,recovery=recovery,sections=len(delta.sections) if delta else 0,
                            encoded_bytes=sum(len(chunk) for section in delta.sections for chunk in section.chunks) if delta else 0)
                        completion=_WarningCompletion(actual,result,delta,dict(tracker.metrics),rows,checkpoint)
                    with self.condition:
                        self._anchor=actual;self._active=None
                        if self._pending is not None:
                            completion=delta=None
                            continue
                        self._completed=completion
                    completion=delta=None
                    if self.wake is not None:self.wake()
                except BaseException as error:
                    tracker.__init__();scope=durable=None
                    failure=(type(error).__name__,str(error),traceback.format_exc(limit=6))
                    with self.condition:
                        self._active=None;self._anchor=None
                        if isinstance(error,ReadCancelled) and self.interrupt.is_set():
                            if not self.stopped.is_set() and self._pending is None:self._pending=self._target
                        else:self._completed=_WarningCompletion(target,None,None,{},0,{},failure)
                    if self.wake is not None:self.wake()
                finally:
                    if source is not None:source.close()
                    source=view=actual=sections=header=result=delta=completion=target=None
                    owner=certificate=checkpoint=head=failure=None
        finally:
            if reader is not None:reader.close()

    def commit_completed(self):
        with self.condition:
            completion,self._completed=self._completed,None
        if completion is None:return None
        target=self._target
        if (target is None or completion.target.token!=self.store.token or
                completion.target.source!=target.source or not _same_facts(completion.target,target)):
            return None
        self.last_metrics=completion.metrics
        self.last_read_rows=completion.read_rows
        self.last_checkpoint=completion.checkpoint
        stage=None
        try:
            stage=self.store.begin(unpack(target.source,self.store.limits))
            delta=completion.delta
            if delta is not None:
                promoted=promote_checkpoint(delta,stage,preparation=self.preparation,source_identity=target.source)
                stage.checkpoint(SPEED_CHECKPOINT_NAMESPACE,
                    ((item.name,item.digest,item.chunks) for item in promoted.sections),
                    base=promoted.base,token=promoted.token,tombstones=promoted.tombstones,complete=promoted.complete)
            token=stage.publish(unpack(target.source,self.store.limits),ready=True)
        except BaseException as error:
            retry=isinstance(error,WriteDeferred)
            with self.condition:
                if retry:self._completed=completion
                elif completion.error is None:
                    self._completed=replace(completion,results=None,delta=None,
                        error=(type(error).__name__,str(error),traceback.format_exc(limit=6)))
            if stage is not None and self.store.stage is stage and not stage.finished:stage.abort()
            if retry or completion.error is not None:raise
            if self.wake is not None:self.wake()
            return None
        committed=replace(target,token=token,prefixes={})
        self._target=committed
        self._known=(replace(completion,target=committed,delta=None) if completion.error is None else None)
        self._failure=(replace(completion,target=committed,delta=None) if completion.error is not None else None)
        self._committed_token=token
        self._trusted_checkpoint_token=token if delta is not None else None
        with self.condition:
            if completion.error is None:self._ack=(completion.target,committed,delta is not None)
            self.condition.notify()
        return self._known or self._failure

    def abort(self,stage=None):
        if stage is not None and self.store.stage is stage and not stage.finished:stage.abort()

    def close(self):
        self.stopped.set();self.interrupt.set()
        with self.condition:self.condition.notify()
        self.thread.join()
        self.abort(self.store.stage)
