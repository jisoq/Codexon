"""Prepare selected canonical owners without retaining the source catalog's rows.

The coordinator owns catalog completeness, candidate election and publication.
These functions never acknowledge collection or publish a partially read owner.
"""
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from hashlib import sha256
import sys
import sqlite3

from .analysis_engine import AnalysisEngine
from .analysis_delivery import SnapshotPublisher, MODEL_KEYS
from .analytics import prepare_row
from .overlay_data import summarize_session
from .prepared_store import CapacityExceeded, OWNER_FIELDS, FORBIDDEN_OWNER, RAW_TRANSPORT_FIELDS, pack
from .pricing import Rate, pricing_signature
from .request_modes import mode_parents


PREPARATION = ('canonical-owner', 6)
CLASSIFIERS = ('cache-miss', 1, 'cache-health', 1)


def preparation_signature():
    return sha256(pack((PREPARATION,CLASSIFIERS))).hexdigest()


class ModeEvidenceUnavailable(RuntimeError):
    """Optional ledger evidence failed; verified source usage remains usable."""


@dataclass(frozen=True)
class IngestLimits:
    # A source is released after its prepared rows have been staged. Ancestor
    # context has a separate aggregate admission, not one allowance per parent.
    owner_source_bytes: int = 256 * 1024 * 1024
    context_bytes: int = 512 * 1024 * 1024
    catalog_bytes: int = 32 * 1024 * 1024


def _weight(value, remaining):
    """Conservative tree weight; JSON source values have no shared subgraphs."""
    stack = [iter((value,))]
    total = 0
    while stack:
        item = next(stack[-1], stack)
        if item is stack:
            stack.pop()
            continue
        total += sys.getsizeof(item)
        if total > remaining:
            raise CapacityExceeded('selected source exceeds its transient preparation budget')
        if isinstance(item, dict):
            stack.append(iter(part for pair in item.items() for part in pair))
        elif isinstance(item, (list, tuple)):
            stack.append(iter(item))
    return total


def read_catalog(client, *, cancel=None, limits=IngestLimits()):
    """Admit one coherent traversal; never infer removal from a partial page."""
    cursor = None
    result = None
    owners = {}
    weight = 0
    while True:
        if cancel is not None and cancel():
            raise InterruptedError('prepared catalog cancelled')
        page = client.poll_catalog(cursor=cursor, include_metadata=True,
                                   metadata_kind='lineage', defer_incomplete=cursor is None)
        if not page['membership_complete']:
            # Older collectors may ignore the optional deferral flag. Their
            # first incomplete page is still sufficient for status; following
            # its cursor cannot produce usable membership for this traversal.
            return {**{key:value for key,value in page.items() if key not in ('owners','cursor')},
                    'owners':{}}
        if result is None:
            result = {key:value for key,value in page.items() if key not in ('owners','cursor')}
            weight = _weight(result, limits.catalog_bytes)
        elif any(page[key] != result[key] for key in ('scope','path','selected_homes','catalog_identity')):
            raise ValueError('catalog pages belong to different committed sources')
        # A volatile heartbeat may change while the committed source does not.
        # Use the oldest captured freshness and require every page to be complete.
        result['membership_complete'] &= page['membership_complete']
        for key, entry in page['owners'].items():
            if key in owners:
                raise ValueError('catalog repeats an owner')
            weight += _weight((key, entry), limits.catalog_bytes - weight) + 96
            if weight > limits.catalog_bytes:
                raise CapacityExceeded('prepared catalog metadata budget exceeded')
            owners[key] = dict(entry, source_ordinal=len(owners))
        cursor = page['cursor']
        if cursor is None:
            catalog = dict(result, owners=owners)
            # Preparation needs the parent link, not display metadata. Older
            # collectors can provide their existing full header or, only when
            # absent, a verified selected-body fallback.
            for key,entry in owners.items():
                if entry.get('owner_lineage') is not None:
                    continue
                if entry.get('owner_metadata') is not None:
                    lineage={name:value for name,value in entry['owner_metadata'].items() if name=='parent_thread_id'}
                    weight += _weight(lineage, limits.catalog_bytes-weight)
                    entry['owner_lineage']=lineage
                    continue
                metadata = {}
                with client.selected_owner_stream(key,catalog=catalog,cancel=cancel) as stream:
                    for field,kind,ordinal,member,value in stream.records(metadata_only=True):
                        if kind != 'value':
                            raise ValueError('selected owner metadata must contain scalars')
                        if field=='parent_thread_id':metadata[field] = value
                added = _weight(metadata, min(16*1024,limits.catalog_bytes-weight))
                weight += added
                if weight > limits.catalog_bytes:
                    raise CapacityExceeded('recovered owner metadata exceeds the catalog budget')
                entry['owner_lineage'] = metadata
            return catalog


def source_identity(catalog):
    return (catalog['scope'], catalog['path'], tuple(catalog['selected_homes']),
            tuple(catalog['catalog_identity']))


def read_source(client, key, catalog, *, cancel=None, limits=IngestLimits()):
    """Read and verify exactly one owner. No partial result escapes on failure."""
    source = {}
    weight = sys.getsizeof(source)
    with client.selected_owner_stream(key, catalog=catalog, cancel=cancel) as stream:
        material = stream.materialize(limits.owner_source_bytes)
        if material is not None:
            source = material
            weight = _materialized_weight(source, limits.owner_source_bytes, stream._check)
        for field, kind, ordinal, member, value in (() if material is not None else stream.records()):
            if kind in ('end_map', 'end_array'):
                continue
            if kind in ('start_map', 'start_array'):
                if field in source:
                    raise ValueError('source repeats a top-level field')
                source[field] = {} if kind == 'start_map' else []
                weight += _weight((field, source[field]), limits.owner_source_bytes - weight) + 96
            elif kind == 'value':
                if field in source:
                    raise ValueError('source repeats a top-level field')
                weight += _weight((field, value), limits.owner_source_bytes - weight) + 96
                source[field] = value
            elif kind == 'item':
                weight += _weight(value, limits.owner_source_bytes - weight) + 16
                source[field].append(value)
            elif kind == 'member':
                if member in source[field]:
                    raise ValueError('source repeats a map member')
                weight += _weight((member, value), limits.owner_source_bytes - weight) + 96
                source[field][member] = value
            else:
                raise ValueError('unknown selected source record')
            if weight > limits.owner_source_bytes:
                raise CapacityExceeded('selected source preparation budget exceeded')
    if (source.get('home'), source.get('id')) != tuple(key):
        raise ValueError('selected source owner does not match the catalog')
    entry = catalog['owners'][key]
    # Producer revisions do not prove persisted analytical reuse. The digest
    # does, and is also a valid in-process revision for this selected owner.
    source.update(entry.get('state', {}))
    source['usage_revision'] = entry['body_digest']
    source['request_revision'] = entry['body_digest']
    return source, weight


def _materialized_weight(source, limit, check):
    """Use the same top-level admission as the record-by-record builder."""
    weight = sys.getsizeof({})
    for field, value in source.items():
        check()
        if isinstance(value, (dict, list)):
            weight += _weight((field, {} if isinstance(value, dict) else []), limit-weight)+96
            members = (((key, item), 96) for key, item in value.items()) if isinstance(value, dict) else (
                (item, 16) for item in value)
            for item, overhead in members:
                check()
                weight += _weight(item, limit-weight)+overhead
        else:
            weight += _weight((field, value), limit-weight)+96
        if weight > limit:
            raise CapacityExceeded('selected source preparation budget exceeded')
    return weight


def mode_parent_index(catalog):
    """Apply the existing cycle policy to metadata before selecting ancestors."""
    metadata = []
    for owner, entry in catalog['owners'].items():
        item = entry.get('owner_lineage', entry.get('owner_metadata'))
        if item is None:
            raise ValueError('owner lineage must be resolved before mode dependency selection')
        metadata.append(dict(item, home=owner[0], id=owner[1]))
    return {key:(value['home'], value['id']) for key,value in mode_parents(metadata).items()}


def mode_context(key, catalog, *, parents=None):
    parents = mode_parent_index(catalog) if parents is None else parents
    context = [key]
    while context[-1] in parents:
        context.append(parents[context[-1]])
    return tuple(reversed(context))


def mode_dependency(context, catalog, ledger):
    values = tuple((key, catalog['owners'][key]['body_digest'],
                    ledger.mode_dependency(*key) if ledger is not None else None)
                   for key in context)
    return sha256(pack(('mode-lineage', 1, values))).hexdigest()


def read_enriched_source(client, key, catalog, ledger, *, parents=None, cancel=None,
                         source_loader=None, limits=IngestLimits()):
    context = mode_context(key, catalog, parents=parents)
    sources = []
    size = 0
    try:
        for owner in context:
            available = limits.context_bytes - size
            if available <= 0:
                raise CapacityExceeded('selected mode ancestry exceeds its shared transient budget')
            admitted = replace(limits, owner_source_bytes=min(limits.owner_source_bytes, available))
            source, weight = (source_loader(owner, admitted) if source_loader is not None else
                             read_source(client, owner, catalog, cancel=cancel, limits=admitted))
            size += weight
            if size > limits.context_bytes:
                raise CapacityExceeded('selected mode ancestry exceeds its shared transient budget')
            sources.append(source)
        snapshot = dict(homes=catalog['selected_homes'], sessions=sources)
        if ledger is not None:
            try:ledger.enrich_modes(snapshot, scan=False)
            except sqlite3.Error as error:
                raise ModeEvidenceUnavailable('optional request-mode evidence failed') from error
        source = next(s for s in snapshot['sessions'] if (s['home'],s['id']) == key)
        dependency = mode_dependency(context, catalog, ledger)
        source = dict(source, usage_revision=(catalog['owners'][key]['body_digest'], dependency))
        return source, dependency
    finally:
        # This adapter deliberately does not retain raw ancestors between owners.
        # Their dependency digests are persistent; their row objects are not.
        if ledger is not None:
            ledger._enriched_sessions = {}


def request_headers(source):
    """Exact whole-request facts, computed once while preparing an owner."""
    boundaries = source.get('turn_records', {})
    facts = {}
    for row in source['history']:
        turn = row.get('turn')
        if not turn:
            continue
        value = facts.get(turn)
        if value is None:
            value = facts[turn] = dict(call_count=0, minimum=row['ts'], maximum=row['ts'],
                                      first_key=row['key'], dimensions={})
        value['call_count'] += 1
        if row['ts'] < value['minimum']:
            value['minimum'], value['first_key'] = row['ts'], row['key']
        value['maximum'] = max(value['maximum'], row['ts'])
        for field in ('model','effort','service_tier'):
            item = row.get(field) or '미확인'
            old = value['dimensions'].setdefault(field, item)
            if old != item:
                value['dimensions'][field] = '혼합'
    for turn in boundaries:
        facts.setdefault(turn, dict(call_count=0, minimum=None, maximum=None, first_key=None,
                                   dimensions=dict.fromkeys(('model','effort','service_tier'), '혼합')))
    def position(turn):
        started = boundaries.get(turn, {}).get('started_at')
        value = started if started is not None else facts[turn]['minimum']
        return (value if value is not None else float('inf'), turn)
    for ordinal, turn in enumerate(sorted(facts, key=position), 1):
        at = position(turn)[0]
        value = dict(facts[turn], display_ordinal=ordinal,
                     effective_at=None if at == float('inf') else at,
                     request_present=turn in boundaries)
        if turn in source.get('turn_states', {}):
            value['turn_state'] = source['turn_states'][turn]
        yield turn, value
    # A state observation without calls or a boundary is still part of the
    # general source metadata. It must not acquire a displayed request ordinal.
    for turn, state in source.get('turn_states', {}).items():
        if turn not in facts:
            yield turn, dict(display_ordinal=0, effective_at=None, call_count=0,
                minimum=None, maximum=None, first_key=None,
                dimensions=dict.fromkeys(('model','effort','service_tier'), '혼합'),
                request_present=False, turn_state=state)


def prepare_owner(source, *, owns):
    """Reuse exact business reducers after the writer elects canonical rows.

    ``owns`` receives a source ordinal and row. It is derived from the staged
    global candidate inventory, never from the selected owner alone.
    """
    history=[];minimum=None;maximum=None
    for i,row in enumerate(source['history']):
        ts=row.get('ts')
        if ts is not None:
            minimum=ts if minimum is None else min(minimum,ts)
            maximum=ts if maximum is None else max(maximum,ts)
        if owns(i,row):history.append(row)
    canonical = dict(source, history=history)
    from .prepared_append import capture_health, reduce_full, summary_facts
    engine = AnalysisEngine(health_checkpoint=capture_health)
    engine.ingest([canonical])
    key = (source['home'], source['id'])
    state = engine.sessions.get(key, engine.internal_sessions.get(key))
    prepared = state['prepared']
    partials = {}
    reduced = reduce_full(prepared)
    summary = summarize_session(prepared, prepared.get('cache_health'), _partials=partials,
        _facts=summary_facts(reduced,prepared['cache_health']['incidents']))
    rows = prepared['history']

    return dict(state=state, append_state=reduced, raw_count=len(source['history']),
                summary=dict(own=summary, partials=partials,coverage_bounds=(minimum,maximum)),
                coverage_bounds=(minimum,maximum),
                choices=SnapshotPublisher._choices_for_rows(rows),
                model_alerts={row['key']:tuple(row.get(k) for k in MODEL_KEYS)
                              for row in rows if row.get('model_alert_confirmed')},
                internal_review=key in engine.internal_sessions)


def stage_owner(stage, material, dependencies, source_ordinal, *, candidates=None, replace=False, append_mode_evidence=None):
    """Stage facts and exact request headers; publication remains caller-owned."""
    state = material['state']
    source = state['prepared']
    key = (source['home'], source['id'])
    excluded = FORBIDDEN_OWNER | RAW_TRANSPORT_FIELDS | {'requests','groups','_prepared'}
    metadata = {name:value for name,value in source.items() if name not in excluded}
    from .prepared_health import compact_owner_health
    metadata.update(compact_owner_health(source))
    metadata['_internal_review'] = material['internal_review']
    canonical = prepare_row({}, source)
    call_metadata = {name:canonical[name] for name in OWNER_FIELDS if name in canonical}
    dependencies=dict(dependencies,coverage_bounds=material['coverage_bounds'],
                      call_count=len(source['history']),
                      speed_frontier=source['history'][-1]['session_ordinal'] if source['history'] else None)
    from .prepared_append import prepare_full
    stored_summary,sections,checkpoint=(prepare_full(material,dependencies,mode_evidence=append_mode_evidence)
        if material['summary'] is not None else (None,(),None))
    stage.owner(*key, metadata, dependencies, source_ordinal=source_ordinal,
                call_metadata=call_metadata, summary=stored_summary, choices=material['choices'],
                model_alerts=material['model_alerts'], replace=replace, checkpoint=checkpoint,
                request_maps=tuple(name for name in ('turn_records','turn_states') if name in source))
    stage.sections(key,sections,reconcile=True)
    stage.calls(key, source['history'], reconcile=True)
    stage.requests(key, ((turn,i,value) for i,(turn,value) in enumerate(source.get('turn_records',{}).items())),
                   reconcile=True)
    stage.headers(key, request_headers(source), reconcile=True)
    if candidates is not None:
        stage.candidates(key, candidates, reconcile=True)
    return key


@lru_cache(maxsize=2)
def _pricing_dependency(signature):
    def values(item):
        if type(item) is Rate:
            return ('Rate', tuple(asdict(item).items()))
        if type(item) is tuple:
            return tuple(values(value) for value in item)
        return item
    return sha256(pack(('pricing-inputs', 1, values(signature)))).hexdigest()


def state_dependency(state):
    return sha256(pack({key:value for key,value in sorted(state.items()) if key != 'remaining'})).hexdigest()


def dependencies(source_digest, mode, ownership, *, state=None):
    return dict(source_digest=source_digest, pricing=_pricing_dependency(pricing_signature()), mode=mode,
                ownership=ownership, classifier=CLASSIFIERS, preparation=PREPARATION,
                state=state_dependency(state or {}))


def pages(method, *args):
    after = None
    while True:
        page = method(*args, after=after)
        yield from page.rows
        if page.complete:
            break
        after = page.after


def _groups(values, size=128):
    group = []
    for value in values:
        group.append(value)
        if len(group) == size:
            yield group
            group = []
    if group:
        yield group


class PreparedCohort:
    """Atomic canonical ownership and preparation for a bounded changed cohort.

    Initial catalogs larger than one cohort first require the explicit bounded
    candidate-conversion phase. This class never publishes partial membership as
    ready and never treats an oversized affected cohort as a successful update.
    """
    SOURCE_CACHE_BYTES = 64 * 1024 * 1024

    def __init__(self, store, client, ledger, *, limits=IngestLimits(), cancel=None, speed_writer=None):
        self.store, self.client, self.ledger = store, client, ledger
        self.limits, self.cancel = limits, cancel
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.metrics = dict(source_reads=0, prepared_owners=0, candidate_rows=0)
        self.speed_writer=speed_writer
        self.speed_results={}

    @staticmethod
    def _speed_stamp(entry,certificate=None):
        from .prepared_speed import OwnerSpeedStamp
        values=entry['dependencies']
        return OwnerSpeedStamp.from_dependencies(values,values['call_count'],values['speed_frontier'],
            append_from=certificate.append_from if certificate else None,
            tail_after=certificate.tail_after if certificate else None)

    def _speed_inventory(self):
        if (self.speed_writer.source.token==self.store.token
                and self.speed_writer.source.stage is None):
            return dict(self.speed_writer.source.owners)
        result={}
        with self.store.read_current(seconds=60) as view:
            for entry in pages(view.catalog):
                if entry['dependencies'].get('prepared') and not entry['metadata'].get('_internal_review'):
                    result[entry['owner']]=self._speed_stamp(entry)
        return result

    def current_speeds(self):
        if self.speed_writer is not None:
            with self.store.read_current(seconds=60) as view:
                source=pack(view.source_identity,self.store.limits)
            self.speed_results=self.speed_writer.current_results(self._speed_inventory(),source)
        return self.speed_results

    def _publish(self,stage,source,ready,previous=None,current=None):
        if ready and self.speed_writer is not None:
            self.speed_results=self.speed_writer.prepare(stage,current,stage.source,previous)
            return self.speed_writer.publish(stage,source,ready=True)
        return stage.publish(source,ready=ready)

    def _abort(self,stage):
        if self.speed_writer is not None:self.speed_writer.abort(stage)
        else:stage.abort()

    def _check(self):
        if self.cancel is not None and self.cancel():
            raise InterruptedError('prepared cohort cancelled')

    def _load(self, owner, limits):
        self._check()
        cache_key = (owner, self.catalog['owners'][owner]['body_digest'])
        old = self.cache.get(cache_key)
        if old is not None:
            if old[1] > limits.owner_source_bytes:
                raise CapacityExceeded('cached selected source exceeds the remaining context budget')
            self.cache.move_to_end(cache_key)
            # Captured volatile state belongs to this catalog, not the older
            # cached envelope sharing the same verified source body.
            return dict(old[0], **self.catalog['owners'][owner].get('state', {})), old[1]
        source, weight = read_source(self.client, owner, self.catalog, cancel=self.cancel, limits=limits)
        self.metrics['source_reads'] += 1
        if weight <= self.SOURCE_CACHE_BYTES:
            while self.cache and self.cache_bytes + weight > self.SOURCE_CACHE_BYTES:
                _, (_, size) = self.cache.popitem(last=False)
                self.cache_bytes -= size
            self.cache[cache_key] = (source, weight)
            self.cache_bytes += weight
        return source, weight

    def _matches(self, snapshot, owner, keys):
        for group in _groups(keys):
            yield from pages(snapshot.candidate_matches, owner, group)

    def _declare(self, stage, owner, current, *, final=False, replace=False, pending=None):
        old = current.get(owner)
        if old is not None:
            metadata = dict(old['metadata'])
            dependency = dict(old['dependencies'])
        else:
            entry = self.catalog['owners'][owner]
            metadata = dict(entry.get('owner_lineage', entry.get('owner_metadata', {})),
                            home=owner[0], id=owner[1], **entry.get('state', {}))
            metadata['_internal_review'] = False  # unpublished candidate inventory only
            dependency = dependencies(entry['body_digest'], None, None)
        if owner in self.catalog['owners']:
            entry = self.catalog['owners'][owner]
            dependency['source_digest'] = entry['body_digest']
            ordinal = entry['source_ordinal']
        else:
            ordinal = old['owner_ordinal']
        dependency['prepared'] = bool(final)
        if pending is not None:
            if pending:
                metadata['_collection_pending'] = True
            else:
                metadata.pop('_collection_pending', None)
        stage.owner(*owner, metadata, dependency, source_ordinal=ordinal, replace=replace)

    def _complete(self, catalog, *, staged, parents):
        expected = set(catalog['owners'])
        with self.store.read_current(staged=staged, seconds=60) as view:
            for value in pages(view.inventory):
                owner, actual = value['owner'], value['dependencies']
                if (owner not in expected or not actual.get('prepared') or
                        actual.get('candidate_source_digest') != catalog['owners'][owner]['body_digest']):
                    return False
                expected.remove(owner)
                context = mode_context(owner, catalog, parents=parents)
                wanted = dependencies(catalog['owners'][owner]['body_digest'],
                                      mode_dependency(context, catalog, self.ledger), actual['ownership'],
                                      state=catalog['owners'][owner].get('state',{}))
                if any(actual.get(key) != item for key,item in wanted.items()):
                    return False
                if value['owner_ordinal'] != catalog['owners'][owner]['source_ordinal']:
                    return False
        return not expected

    def publish_ready(self, catalog):
        if not catalog['membership_complete']:
            raise ValueError('complete source membership is required before publication')
        parents = mode_parent_index(catalog)
        if not self._complete(catalog, staged=False, parents=parents):
            raise ValueError('prepared owners do not cover the captured source dependencies')
        stage = self.store.begin(source_identity(catalog))
        try:
            previous=self._speed_inventory() if self.speed_writer is not None else None
            return self._publish(stage,source_identity(catalog),True,previous,previous)
        except BaseException:
            self._abort(stage)
            raise

    def apply(self, catalog, changed, removed=(), *, ready=True, _parents=None):
        """Return only after the complete affected cohort commits atomically."""
        if not catalog['membership_complete']:
            raise ValueError('canonical cohort needs complete source membership')
        self.catalog = catalog
        changed, removed = set(changed), set(removed)
        if changed & removed or not changed <= catalog['owners'].keys() or removed & catalog['owners'].keys():
            raise ValueError('cohort membership does not match its captured catalog')
        if len(changed | removed) > self.store.limits.owners_per_batch:
            raise CapacityExceeded('changed cohort requires bounded initial conversion')
        parents = mode_parent_index(catalog) if _parents is None else _parents
        from .prepared_append import try_apply
        appended=try_apply(self,catalog,changed,removed,ready=ready,parents=parents)
        if appended is not None:return appended
        current = {}
        before_refs = {}
        candidate_sources = {}
        with self.store.read_current(seconds=60) as view:
            for owner in changed | removed:
                current[owner] = view.owner(*owner)
                old = current[owner]
                candidate_changed = (owner in removed or old is None or
                    old['dependencies'].get('candidate_source_digest') != catalog['owners'][owner]['body_digest'])
                if candidate_changed:
                    keys = [item['key'] for item in pages(view.owner_candidates, owner)]
                    before_refs[owner] = {ref['owner'] for ref in self._matches(view, owner, keys)}
        source = source_identity(catalog)
        previous_speed=self._speed_inventory() if ready and self.speed_writer is not None else None
        speed_certificates={}
        stage = self.store.begin(source)
        affected = set(changed)
        try:
            for owner in changed | removed:
                self._declare(stage, owner, current)
                if owner in removed:
                    stage.remove_owner(owner)
                    affected.update(before_refs.get(owner, ()))
                    continue
                if owner not in before_refs:
                    continue
                raw, _ = self._load(owner, self.limits)
                self._declare(stage, owner, current, replace=True,
                              pending=bool(raw.get('_collection_pending')))
                # One candidate reference per identity. Election is by key;
                # normal stable timestamp deduplication still sees every row.
                candidates = {}
                for ordinal, row in enumerate(raw['history']):
                    candidates.setdefault(row.get('key'), ordinal)
                stage.candidates(owner, candidates.items(), reconcile=True)
                candidate_sources[owner] = catalog['owners'][owner]['body_digest']
                self.metrics['candidate_rows'] += len(candidates)
                affected.update(before_refs[owner])
            with self.store.read_current(staged=True, seconds=60) as view:
                for owner in before_refs.keys() - removed:
                    keys = (item['key'] for item in pages(view.owner_candidates, owner))
                    affected.update(ref['owner'] for ref in self._matches(view, owner, keys))
                affected -= removed
                # A complete catalog may withdraw several competing owners in
                # separate bounded cohorts. Future removals need no preparation.
                affected.intersection_update(catalog['owners'])
                if len(affected | removed) > self.store.limits.owners_per_batch:
                    raise CapacityExceeded('ownership correction exceeds one coherent cohort')
                for owner in affected - changed:
                    current[owner] = view.owner(*owner)
            for owner in affected - changed:
                self._declare(stage, owner, current)
            for owner in sorted(affected, key=lambda key:catalog['owners'][key]['source_ordinal']):
                self._check()
                raw, mode = read_enriched_source(self.client, owner, catalog, self.ledger,
                    parents=parents, cancel=self.cancel, source_loader=self._load, limits=self.limits)
                winners = {}
                keys = dict.fromkeys(row.get('key') for row in raw['history'])
                with self.store.read_current(staged=True, seconds=60) as view:
                    for ref in self._matches(view, owner, keys):
                        priority = (ref['pending'], ref['owner'])
                        old = winners.get(ref['key'])
                        if old is None or priority < old:
                            winners[ref['key']] = priority
                def owns(ordinal, row):
                    winner = winners.get(row.get('key'))
                    return winner is not None and winner[1] == owner
                ownership = sha256()
                for ordinal, row in enumerate(raw['history']):
                    if owns(ordinal, row):
                        ownership.update(pack((ordinal, row.get('key'))))
                material = prepare_owner(raw, owns=owns)
                if previous_speed is not None and owner in previous_speed and not material['internal_review']:
                    from .prepared_speed_writer import certify_speed_prefix
                    with self.store.read_current(seconds=60) as view:
                        speed_certificates[owner]=certify_speed_prefix(view,owner,previous_speed[owner],
                                                                     material['state']['prepared']['history'])
                dependency = dependencies(catalog['owners'][owner]['body_digest'], mode, ownership.hexdigest(),
                                          state=catalog['owners'][owner].get('state',{}))
                # A competing owner's new body may be prepared before its own
                # conversion cohort reconciles candidates. Keep that inventory's
                # independent proof so later cohorts and restarts still owe it.
                old = current.get(owner)
                dependency['candidate_source_digest'] = candidate_sources.get(owner,
                    old['dependencies'].get('candidate_source_digest') if old is not None else None)
                dependency['prepared'] = True
                stage_owner(stage, material, dependency, catalog['owners'][owner]['source_ordinal'], replace=True,
                    append_mode_evidence=self.ledger.mode_dependency(*owner) if self.ledger is not None else None)
                self.metrics['prepared_owners'] += 1
                del material, raw, winners, keys
            if ready and not self._complete(catalog, staged=True, parents=parents):
                raise ValueError('cohort cannot publish incomplete prepared dependencies')
            current_speed=None
            if previous_speed is not None:
                current_speed={key:value for key,value in previous_speed.items() if key not in removed}
                with self.store.read_current(staged=True,seconds=60) as view:
                    for owner in affected:
                        entry=view.owner(*owner)
                        if entry['metadata'].get('_internal_review'):
                            current_speed.pop(owner,None)
                        else:
                            current_speed[owner]=self._speed_stamp(entry,speed_certificates.get(owner))
            token = self._publish(stage,source,ready,previous_speed,current_speed)
            return dict(token=token, changed=tuple(affected), removed=tuple(removed), writes=dict(stage.writes))
        except BaseException:
            self._abort(stage)
            raise
        finally:
            # Source rows are useful only within this atomic cohort. Persistent
            # reuse relies on the committed digests, never retained raw objects.
            self.cache.clear()
            self.cache_bytes = 0

    def _reorder(self, catalog, owners, *, ready, parents):
        """Change traversal positions without rereading unchanged source facts."""
        self._check()
        source = source_identity(catalog)
        checkpoint = self.speed_writer is not None
        if checkpoint and not ready:
            from .prepared_speed_writer import SPEED_CHECKPOINT_NAMESPACE
            with self.store.read_current(seconds=60) as view:
                head = view.checkpoint_state(SPEED_CHECKPOINT_NAMESPACE)
                checkpoint = head is not None and tuple(head['token']) == (view.token.instance, view.token.generation)
        previous = self._speed_inventory() if checkpoint else None
        stage = self.store.begin(source)
        try:
            with self.store.read_current(seconds=60) as view:
                for owner in owners:
                    self._check()
                    old = view.owner(*owner)
                    # convert established every dependency and candidate proof.
                    # Preserve facts, summaries and checkpoints in this stage.
                    stage.owner(*owner, old['metadata'], old['dependencies'],
                                source_ordinal=catalog['owners'][owner]['source_ordinal'])
            if ready and not self._complete(catalog, staged=True, parents=parents):
                raise ValueError('reordered cohort cannot publish incomplete prepared dependencies')
            if checkpoint:
                # Facts are unchanged even when traversal order spans batches.
                # Carry their checkpoint with each generation so the final
                # ready publication does not recover all historical calls.
                self.speed_results = self.speed_writer.prepare(stage, previous, stage.source, previous)
                token = self.speed_writer.publish(stage, source, ready=ready)
            else:
                # Capacity fallback has no reusable checkpoint. Keep its full
                # recovery at the final ready publication, not every batch.
                token = stage.publish(source, ready=ready)
            self.metrics['reordered_owners'] = self.metrics.get('reordered_owners', 0) + len(owners)
            return dict(token=token, changed=tuple(owners), removed=(), writes=dict(stage.writes))
        except BaseException:
            self._abort(stage)
            raise

    def convert(self, catalog, *, cohort_size=16):
        """Yield between bounded durable cohorts; resume from their stored proofs.

        This is the initial conversion and restart coordinator. One coherent
        small update publishes atomically. Larger conversions stay unavailable
        until every owner matches the captured complete catalog.
        """
        if not catalog['membership_complete']:
            raise ValueError('conversion waits for complete source membership')
        if not 1 <= cohort_size <= self.store.limits.owners_per_batch:
            raise ValueError('invalid conversion cohort size')
        parents = mode_parent_index(catalog)
        current = {}
        with self.store.read_current(seconds=60) as view:
            for value in pages(view.inventory):
                current[value['owner']] = value
        changed = []; reordered = []
        for owner,entry in catalog['owners'].items():
            old = current.get(owner)
            wanted = dependencies(entry['body_digest'],
                mode_dependency(mode_context(owner,catalog,parents=parents),catalog,self.ledger),
                old['dependencies'].get('ownership') if old else None,state=entry.get('state',{}))
            if (old is None or not old['dependencies'].get('prepared')
                    or old['dependencies'].get('candidate_source_digest') != entry['body_digest']
                    or any(old['dependencies'].get(key) != value for key,value in wanted.items())):
                changed.append(owner)
            elif old['owner_ordinal'] != entry['source_ordinal']:
                reordered.append(owner)
        removed = list(current.keys()-catalog['owners'].keys())
        if not changed and not removed and not reordered:
            if not self.store.db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0]:
                token=self.publish_ready(catalog)
                yield dict(token=token,changed=(),removed=(),writes={},ready=True)
            return
        work = [(False,key) for key in removed] + [(True,key) for key in changed]
        if reordered and changed and not removed:
            # Capture leading new owners before traversal-only
            # batches. A live new session can grow while those batches commit.
            # Use the existing source cache allowance, never a second buffer.
            self.catalog = catalog
            try:
                for owner in changed[:cohort_size]:
                    # Existing owners may use a proven append without reading
                    # their full source. Do not prefetch that history eagerly.
                    if owner in current:continue
                    for member in mode_context(owner,catalog,parents=parents):
                        if (member,catalog['owners'][member]['body_digest']) in self.cache:continue
                        available = self.SOURCE_CACHE_BYTES-self.cache_bytes
                        if available <= 0:raise CapacityExceeded('source capture allowance reached')
                        self._load(member,replace(self.limits,
                            owner_source_bytes=min(self.limits.owner_source_bytes,available)))
            except CapacityExceeded:
                # Oversized owners retain the ordinary verified read path.
                pass
        # Separate order-only work so a single changed source can still use its
        # append path. Every intermediate cohort stays unavailable as before.
        single = (not work or not reordered) and len(work) + len(reordered) <= cohort_size
        for group in _groups(reordered,cohort_size):
            result = self._reorder(catalog,group,ready=single,parents=parents)
            yield dict(result,ready=single)
        completed = 0
        for group in _groups(work,cohort_size):
            completed += len(group)
            ready = completed == len(work)
            result = self.apply(catalog,[key for keep,key in group if keep],
                                [key for keep,key in group if not keep],ready=ready,_parents=parents)
            yield dict(result,ready=ready)
        if not single and not work:
            token=self.publish_ready(catalog)
            yield dict(token=token,changed=(),removed=(),writes={},ready=True)
