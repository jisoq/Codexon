"""Prepare selected canonical owners without retaining the source catalog's rows.

The coordinator owns catalog completeness, candidate election and publication.
These functions never acknowledge collection or publish a partially read owner.
"""
from collections import ChainMap, OrderedDict
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from hashlib import sha256
import sys
import sqlite3

from .analysis_engine import AnalysisEngine
from .analysis_delivery import SnapshotPublisher, MODEL_KEYS
from .analytics import prepare_row
from .overlay_data import summarize_session
from .prepared_store import UNCHANGED, CapacityExceeded, BatchCapacityExceeded, CatalogWork, ConversionPlan, Snapshot, SnapshotInventory, OWNER_FIELDS, FORBIDDEN_OWNER, RAW_TRANSPORT_FIELDS, pack
from .pricing import Rate, pricing_signature
from .request_modes import mode_parents, requires_parent_mode
from .usage_collection import CollectionCatalogUnavailable


PREPARATION = ('canonical-owner', 9)
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


def _catalog_status(page):
    header = {key:value for key,value in page['header'].items()
              if key not in ('session_catalog','demand_members','selected_resolutions')}
    return {**{key:value for key,value in page.items()
        if key not in ('owners','cursor','membership','header','mode_sources','selected_members')},
        'header':header,'owners':{}}


def read_catalog(client, *, store=None, cancel=None):
    cursor = None
    catalog = None
    demand = getattr(client,'active_demand',None)
    selected = demand is not None and not demand['aggregate'] and callable(getattr(client,'poll_selection',None))
    poll = client.poll_selection if selected else client.poll_catalog
    try:
        while True:
            if cancel is not None and cancel():
                raise InterruptedError('prepared catalog cancelled')
            options = {} if selected else dict(defer_incomplete=cursor is None)
            page = poll(cursor=cursor, include_metadata=True, metadata_kind='lineage', **options)
            if not page['membership_complete']:
                if catalog is not None:
                    catalog.close()
                return _catalog_status(page)
            if catalog is None:
                catalog = CatalogWork(_catalog_status(page),store=store,cancel=cancel)
                if 'membership_format' not in page:
                    for name in ('session_catalog','demand_members'):
                        if name in page['header']:
                            catalog.add_membership(name,page['header'][name])
            elif any(page[key] != catalog[key] for key in ('scope','path','selected_homes','catalog_identity')):
                raise ValueError('catalog pages belong to different committed sources')
            if selected and page.get('selection_request') != catalog.get('selection_request'):
                raise ValueError('selection request changed during traversal')
            if page.get('membership_format') != catalog.get('membership_format'):
                raise ValueError('catalog membership format changed during traversal')
            if page.get('membership_format') is not None:
                for name, rows in page['membership'].items():
                    catalog.add_membership(name,rows)
            catalog.add_owners(page['owners'])
            following = page['cursor']
            if following is not None and following == cursor:
                raise ValueError('catalog cursor did not advance')
            cursor = following
            if cursor is None:
                break
        for row in catalog.owner_rows():
            key = row['home'],row['sid']
            entry = catalog.owner_entry(row,financial=False)
            if entry.get('owner_lineage') is not None:
                continue
            if entry.get('owner_metadata') is not None:
                entry['owner_lineage'] = {name:value for name,value in entry['owner_metadata'].items()
                                          if name=='parent_thread_id'}
            else:
                lineage = {}
                with client.selected_owner_stream(key,catalog=catalog,cancel=cancel) as stream:
                    for field,kind,ordinal,member,value in stream.records(metadata_only=True):
                        if kind != 'value':
                            raise ValueError('selected owner metadata must contain scalars')
                        if field=='parent_thread_id':
                            lineage[field] = value
                entry['owner_lineage'] = lineage
            catalog.set_entry(key,entry)
        demand = ((getattr(client,'active_demand',None) or catalog['header']['demand'])
                  if catalog['header'].get('demand') is not None else None)
        if selected:
            catalog.finish_selection()
        else:
            catalog.finish(demand)
        if not catalog['membership_complete']:
            status = _catalog_status(catalog)
            catalog.close()
            return status
        return catalog
    except BaseException:
        if catalog is not None:
            catalog.close()
        raise


def source_identity(catalog):
    source = (catalog['scope'], catalog['path'], tuple(catalog['selected_homes']),
              tuple(catalog['catalog_identity']))
    if catalog.get('authority')=='selected':
        source += (('selected',catalog['selection_request']),)
    return source


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
    entry = catalog['owners'].get(key) or catalog.get('mode_sources',{})[key]
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
    if isinstance(catalog, CatalogWork):
        return catalog.parents
    metadata = [dict(home=home,id=sid,parent_thread_id=parent)
                for home,sid,parent in catalog.get('header',{}).get('session_catalog',())]
    for owner, entry in {**catalog.get('mode_sources',{}),**catalog['owners']}.items():
        item = entry.get('owner_lineage', entry.get('owner_metadata'))
        if item is None:
            raise ValueError('owner lineage must be resolved before mode dependency selection')
        metadata.append(dict(item, home=owner[0], id=owner[1]))
    return {key:(value['home'], value['id']) for key,value in mode_parents(metadata).items()}


def mode_context(key, catalog, *, parents=None, ancestry_required=True):
    if ancestry_required is False:
        return (key,)
    parents = mode_parent_index(catalog) if parents is None else parents
    context = [key]
    while context[-1] in parents:
        context.append(parents[context[-1]])
    return tuple(reversed(context))


def mode_dependency(context, catalog, ledger):
    values=[]
    for key in context:
        entry=catalog['owners'].get(key) or catalog.get('mode_sources',{}).get(key,{})
        values.append((key,entry.get('analysis_digest') or entry.get('body_digest'),
                       ledger.mode_dependency(*key) if ledger is not None else None))
    return sha256(pack(('mode-lineage', 1, tuple(values)))).hexdigest()


def read_enriched_source(client, key, catalog, ledger, *, parents=None, cancel=None,
                         source_loader=None, limits=IngestLimits(), include_mode_proof=False):
    """Resolve own evidence before loading ancestors that can affect its rows.

    The optional third result certifies whether ancestry can affect this source.
    Its proof belongs to the own body and own ledger dependency, both of which
    remain part of the resulting dependency even when ancestry is unnecessary.
    """
    size = 0
    def load(owner):
        nonlocal size
        available = limits.context_bytes - size
        if available <= 0:
            raise CapacityExceeded('selected mode ancestry exceeds its shared transient budget')
        admitted = replace(limits, owner_source_bytes=min(limits.owner_source_bytes, available))
        source, weight = (source_loader(owner, admitted) if source_loader is not None else
                          read_source(client, owner, catalog, cancel=cancel, limits=admitted))
        size += weight
        if size > limits.context_bytes:
            raise CapacityExceeded('selected mode ancestry exceeds its shared transient budget')
        return source

    def enrich(snapshot):
        try:ledger.enrich_modes(snapshot, scan=False)
        except sqlite3.Error as error:
            raise ModeEvidenceUnavailable('optional request-mode evidence failed') from error

    try:
        own = load(key)
        snapshot = dict(homes=catalog['selected_homes'], sessions=[own])
        ancestry_required = False
        if ledger is not None:
            enrich(snapshot)
            # Match ParentModeTimeline.inherit's early return exactly. Raw
            # known modes alone are insufficient: local ledger evidence can
            # first turn them into unknown, inheritable rows.
            ancestry_required = any(requires_parent_mode(row) for row in snapshot['sessions'][0]['history'])
        context = mode_context(key, catalog, parents=parents, ancestry_required=ancestry_required)
        if len(context) > 1:
            # Release the trial enrichment before loading the bounded ancestry.
            # Re-enrich raw own rows, never already inherited trial rows.
            ledger._enriched_sessions = {}
            snapshot = None
            sources = [load(owner) for owner in context[:-1]]
            snapshot = dict(homes=catalog['selected_homes'], sessions=[*sources, own])
            enrich(snapshot)
        source = next(s for s in snapshot['sessions'] if (s['home'],s['id']) == key)
        dependency = mode_dependency(context, catalog, ledger)
        entry = catalog['owners'].get(key) or catalog.get('mode_sources',{})[key]
        source = dict(source, usage_revision=(entry['body_digest'], dependency))
        return (source, dependency, ancestry_required) if include_mode_proof else (source, dependency)
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
    for turn, value in facts.items():
        if turn in source.get('turn_states', {}):
            value['turn_state'] = source['turn_states'][turn]
        yield turn, value
    for turn, state in source.get('turn_states', {}).items():
        if turn not in facts:
            yield turn, dict(call_count=0,minimum=None,maximum=None,first_key=None,
                dimensions=dict.fromkeys(('model','effort','service_tier'), '혼합'),turn_state=state)


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
    dependencies=dict(dependencies,pricing=_pricing_dependency(pricing_signature(material['choices']['models'])),
                      coverage_bounds=material['coverage_bounds'],
                      call_count=len(source['history']),
                      speed_frontier=source['history'][-1]['session_ordinal'] if source['history'] else None)
    from .prepared_append import prepare_full
    stored_summary,sections,checkpoint=(prepare_full(material,dependencies,mode_evidence=append_mode_evidence)
        if material['summary'] is not None else (None,(),None))
    stage.owner(*key, metadata, dependencies, source_ordinal=source_ordinal,
                call_metadata=call_metadata, summary=stored_summary, choices=material['choices'],
                model_alerts=material['model_alerts'], replace=replace, checkpoint=checkpoint,
                request_maps=tuple(name for name in ('turn_records','turn_states') if name in source))
    stage.facts(key, sections=sections, calls=source['history'],
                requests=((turn,i,value) for i,(turn,value) in enumerate(source.get('turn_records',{}).items())),
                headers=request_headers(source))
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


def dependencies(source_digest, mode, ownership, *, state=None, models=None):
    signature=pricing_signature() if models is None else pricing_signature(models)
    return dict(source_digest=source_digest, pricing=_pricing_dependency(signature), mode=mode,
                ownership=ownership, classifier=CLASSIFIERS, preparation=PREPARATION,
                state=state_dependency(state or {}))


def fact_dependencies(values):
    """Display-only bodies retain their analytical and accounting certificates."""
    digest=values.get('analysis_digest')
    if type(digest) is not str or len(digest)!=64:return values
    result=dict(values,source_digest=digest,candidate_source_digest=digest)
    if 'state_facts' in values:result['state']=values['state_facts']
    return result


def state_facts(state):
    return state_dependency({key:value for key,value in state.items() if key not in ('usage_revision','request_revision')})


def pages(method, *args, **kwargs):
    view = getattr(method, '__self__', None)
    if isinstance(view, Snapshot):
        yield from view.pages(method, *args, **kwargs)
        return
    after = None
    while True:
        page = method(*args, after=after, **kwargs)
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
        current=self.speed_writer.owner_stamps(self.store.token)
        selected = getattr(self,'catalog',{}).get('authority')=='selected'
        result={}
        with self.store.read_current(seconds=60) as view:
            if not selected and current is not None and not view.db.execute(
                    'SELECT selection_batch FROM meta WHERE singleton=1').fetchone()[0]:
                return dict(current)
            entries = ((owner,view.owner(*owner)) for owner in self.catalog['owners']) if selected else (
                (entry['owner'],entry) for entry in pages(view.catalog))
            for owner,entry in entries:
                if entry is not None and entry['dependencies'].get('prepared') and not entry['metadata'].get('_internal_review'):
                    result[owner]=current[owner] if current is not None and owner in current else self._speed_stamp(entry)
        return result

    def current_speeds(self):
        if self.speed_writer is not None:
            with self.store.read_current(seconds=60) as view:
                source=pack(view.source_identity,self.store.limits)
            self.speed_results=self.speed_writer.current_results(self._speed_inventory(),source)
            if self.speed_writer.checkpoint_recovery_needed:
                self.publish_ready(self.catalog)
        return self.speed_results

    def _publish(self,stage,source,ready,previous=None,current=None):
        if ready and getattr(self,'order_changed',True):
            stage.catalog_order((owner,entry['source_ordinal']) for owner,entry in self.catalog['owners'].items())
        if ready and self.speed_writer is not None:
            token,self.speed_results=self.speed_writer.publish_facts(stage,source,current,previous)
        else:
            token = stage.publish(source,ready=ready)
        self.last_writes = dict(stage.writes)
        return token

    def _abort(self,stage):
        if self.speed_writer is not None:self.speed_writer.abort(stage)
        else:stage.abort()

    def _check(self):
        if self.cancel is not None and self.cancel():
            raise InterruptedError('prepared cohort cancelled')

    def _load(self, owner, limits):
        self._check()
        entry=self.catalog['owners'].get(owner) or self.catalog.get('mode_sources',{}).get(owner)
        if entry is None:
            self.client.request_sources((owner,))
            raise CollectionCatalogUnavailable('mode_source_pending')
        cache_key = (owner, entry['body_digest'])
        old = self.cache.get(cache_key)
        if old is not None:
            if old[1] > limits.owner_source_bytes:
                raise CapacityExceeded('cached selected source exceeds the remaining context budget')
            self.cache.move_to_end(cache_key)
            # Captured volatile state belongs to this catalog, not the older
            # cached envelope sharing the same verified source body.
            return dict(old[0], **entry.get('state', {})), old[1]
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
        entry = self.catalog['owners'].get(owner) or self.catalog.get('mode_sources',{}).get(owner)
        if old is not None:
            metadata = dict(old['metadata'])
            dependency = dict(old['dependencies'])
        else:
            metadata = dict(entry.get('owner_lineage', entry.get('owner_metadata', {})),
                            home=owner[0], id=owner[1], **entry.get('state', {}))
            metadata['_internal_review'] = False  # unpublished candidate inventory only
            dependency = dependencies(entry['body_digest'], None, None)
        if entry is not None:
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
        return bool(metadata.get('_collection_pending'))

    def _complete(self, catalog, *, staged, parents):
        count = 0
        with self.store.read_current(staged=staged, seconds=60) as view:
            inventory = self._inventory(view)
            values = (inventory.get(owner) for owner in catalog['owners']) if catalog.get('authority')=='selected' else inventory.values()
            for value in values:
                if value is None:
                    return False
                self.metrics['complete_visits']=self.metrics.get('complete_visits',0)+1
                owner, actual = value['owner'], value['dependencies']
                entry = catalog['owners'].get(owner)
                if (entry is None or not actual.get('prepared') or
                        actual.get('candidate_source_digest') != entry['body_digest']):
                    return False
                context = mode_context(owner, catalog, parents=parents,
                    ancestry_required=actual.get('mode_ancestry_required') is not False)
                wanted = dependencies(entry['body_digest'],
                                      mode_dependency(context, catalog, self.ledger), actual['ownership'],
                                      state=entry.get('state',{}),models=value.get('models'))
                if (any(actual.get(key) != item for key,item in wanted.items() if key!='pricing') or
                        actual.get('pricing') not in (wanted['pricing'],_pricing_dependency(pricing_signature()))):
                    return False
                count += 1
        return count == len(catalog['owners'])

    def _inventory(self,view):
        return SnapshotInventory(view)

    def _capture_catalog(self, catalog, current=None):
        if catalog is getattr(self, 'catalog', None):
            return catalog
        if not any(not entry.get('analysis_digest') for entry in catalog['owners'].values()):
            return catalog
        if current is None:
            with self.store.read_current(seconds=60) as view:
                return self._capture_catalog(catalog,self._inventory(view))
        owners = None
        for owner, entry in catalog['owners'].items():
            old = current.get(owner)
            if entry.get('analysis_digest') or old is None:
                continue
            prior = old['dependencies']
            if (prior.get('prepared') and prior.get('analysis_digest')
                    and prior.get('source_digest') == entry['body_digest']
                    and prior.get('candidate_source_digest') == entry['body_digest']
                    and prior.get('preparation') == PREPARATION
                    and prior.get('classifier') == CLASSIFIERS):
                replacement = dict(entry, analysis_digest=prior['analysis_digest'])
                if isinstance(catalog, CatalogWork):
                    catalog.set_entry(owner,replacement)
                else:
                    if owners is None:
                        owners = dict(catalog['owners'])
                    owners[owner] = replacement
        return dict(catalog,owners=owners) if owners is not None else catalog

    def publish_ready(self, catalog):
        if not catalog['membership_complete']:
            raise ValueError('complete source membership is required before publication')
        self.catalog = catalog = self._capture_catalog(catalog)
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
        self.catalog = catalog = self._capture_catalog(catalog)
        selected = catalog.get('authority')=='selected'
        references = ChainMap(catalog['owners'],catalog.get('mode_sources',{})) if selected else catalog['owners']
        changed, removed = set(changed), set(removed)
        if selected and removed:
            raise ValueError('a selected publication cannot retire owners')
        if (changed & removed or any(owner not in references for owner in changed)
                or any(owner in catalog['owners'] for owner in removed)):
            raise ValueError('cohort membership does not match its captured catalog')
        if len(changed | removed) > self.store.limits.owners_per_batch:
            raise CapacityExceeded('changed cohort requires bounded initial conversion')
        parents = mode_parent_index(catalog) if _parents is None else _parents
        from .prepared_append import try_apply
        self._append_owners=None
        self._append_plans={}
        try:
            appended = (None if any(owner not in catalog['owners'] for owner in changed) else
                        try_apply(self,catalog,changed,removed,ready=ready,parents=parents))
        finally:
            retained=self._append_owners
            self._append_owners=None
            append_plans=self._append_plans
            self._append_plans={}
        if appended is not None:return appended
        current = {}
        display={}
        unchanged={}
        pricing_updates={}
        metadata_checkpoints={}
        before_refs = {}
        candidate_sources = {}
        with self.store.read_current(seconds=60) as view:
            for owner in changed | removed:
                current[owner] = (retained[1][owner][0] if retained is not None and retained[0]==view.token
                                  and owner in retained[1] else view.owner(*owner))
                old = current[owner]
                if owner not in removed and old is not None:
                    entry=references[owner];prior=old['dependencies']
                    context=mode_context(owner,catalog,parents=parents,
                        ancestry_required=prior.get('mode_ancestry_required') is not False)
                    choices=view.derived(*owner,'choices')
                    models=choices.get('models') if choices is not None else None
                    wanted=dependencies(entry['body_digest'],mode_dependency(context,catalog,self.ledger),
                        prior['ownership'],state=entry.get('state',{}),models=models)
                    if (prior.get('prepared') and prior.get('candidate_source_digest')==entry['body_digest']
                            and all(prior.get(name)==value for name,value in wanted.items())):
                        unchanged[owner]=prior
                    if (models is not None and prior.get('prepared')
                            and prior.get('candidate_source_digest')==entry['body_digest']
                            and prior.get('pricing')==_pricing_dependency(pricing_signature())
                            and prior['pricing']!=wanted['pricing']
                            and all(prior.get(name)==value for name,value in wanted.items() if name!='pricing')):
                        pricing_updates[owner]=dict(prior,pricing=wanted['pricing'])
                        if retained is not None and retained[0]==view.token and len(retained[1].get(owner,()))==2:
                            metadata_checkpoints[owner]=retained[1][owner][1]
                    models=choices=None
                    if (entry.get('analysis_digest') and entry['analysis_digest']==prior.get('analysis_digest')
                            and type(entry.get('display_title')) is str and entry.get('append_context')
                            and prior.get('prepared') and 'title' in old['metadata']
                            and prior.get('candidate_source_digest')==prior['source_digest']
                            and prior.get('state_facts')==state_facts(entry.get('state',{}))
                            and all(prior.get(name)==value for name,value in wanted.items() if name not in ('source_digest','state'))):
                        display[owner]=dict(prior,**wanted,candidate_source_digest=entry['body_digest'])
                        if retained is not None and retained[0]==view.token and len(retained[1].get(owner,()))==2:
                            metadata_checkpoints[owner]=retained[1][owner][1]
                candidate_changed = (owner in removed or old is None or
                    old['dependencies'].get('candidate_source_digest') != references[owner]['body_digest'])
                if candidate_changed and owner not in display and owner not in append_plans:
                    keys = [item['key'] for item in pages(view.owner_candidates, owner)]
                    before_refs[owner] = {ref['owner'] for ref in self._matches(view, owner, keys)}
        retained=None
        source = source_identity(catalog)
        previous_speed=self._speed_inventory() if ready and self.speed_writer is not None else None
        speed_certificates={}
        stage = self.store.begin(source)
        affected = set(changed)
        elected=set()
        appended_plans=[]
        try:
            for owner in sorted(changed | removed, key=lambda owner: (references[owner]['source_ordinal']
                    if owner in references else current[owner]['owner_ordinal'])):
                declared_pending=self._declare(stage, owner, current)
                if owner in append_plans:
                    plan=append_plans[owner]
                    stage.candidates(owner,((row['key'],row['session_ordinal']-1) for row in plan.rows))
                    candidate_sources[owner]=references[owner]['body_digest']
                    continue
                if owner in removed:
                    stage.remove_owner(owner)
                    affected.update(before_refs.get(owner, ()))
                    elected.update(before_refs.get(owner, ()))
                    continue
                if owner not in before_refs:
                    continue
                raw, _ = self._load(owner, self.limits)
                pending=bool(raw.get('_collection_pending'))
                if pending!=declared_pending:
                    self._declare(stage, owner, current, replace=True,pending=pending)
                # One candidate reference per identity. Election is by key;
                # normal stable timestamp deduplication still sees every row.
                candidates = {}
                for ordinal, row in enumerate(raw['history']):
                    candidates.setdefault(row.get('key'), ordinal)
                stage.candidates(owner, candidates.items(), reconcile=True)
                candidate_sources[owner] = references[owner]['body_digest']
                self.metrics['candidate_rows'] += len(candidates)
                affected.update(before_refs[owner])
                elected.update(before_refs[owner])
            with self.store.read_current(staged=True, seconds=60) as view:
                for owner in before_refs.keys() - removed:
                    keys = (item['key'] for item in pages(view.owner_candidates, owner))
                    matches={ref['owner'] for ref in self._matches(view, owner, keys)}
                    affected.update(matches);elected.update(matches)
                affected -= removed
                # A complete catalog may withdraw several competing owners in
                # separate bounded cohorts. Future removals need no preparation.
                if selected:
                    missing = {owner for owner in affected if owner not in references
                               or not references[owner].get('state',{}).get('collection_complete')}
                    if missing:
                        self.client.request_sources(missing)
                        raise CollectionCatalogUnavailable('canonical_sources_pending')
                else:
                    affected = {owner for owner in affected if owner in references}
                if len(affected | removed) > self.store.limits.owners_per_batch:
                    raise BatchCapacityExceeded('ownership correction exceeds one coherent cohort')
                for owner in affected - changed:
                    current[owner] = view.owner(*owner)
                refresh = {owner for owner in affected - changed if selected and
                    (current[owner] is None or current[owner]['dependencies'].get('candidate_source_digest')
                     != references[owner]['body_digest'])}
            if refresh:
                self._abort(stage)
                return self.apply(catalog,changed | refresh,removed,ready=ready,_parents=parents)
            for owner in sorted(affected - changed, key=lambda key:references[key]['source_ordinal']):
                self._declare(stage, owner, current)
            for owner in sorted(affected, key=lambda key:references[key]['source_ordinal']):
                self._check()
                if owner in unchanged and owner not in elected:
                    stage.owner(*owner,current[owner]['metadata'],unchanged[owner],
                        source_ordinal=references[owner]['source_ordinal'],replace=True)
                    continue
                if owner in append_plans and owner not in elected:
                    plan=append_plans[owner]
                    plan.apply(stage,references[owner]['source_ordinal'],cancel=self.cancel,
                               replace=True,candidates_staged=True)
                    if previous_speed is not None and owner in previous_speed:
                        speed_certificates[owner]=plan.speed_certificate(previous_speed[owner])
                    appended_plans.append(plan)
                    continue
                if owner in pricing_updates and owner not in elected:
                    old=current[owner];dependency=pricing_updates[owner]
                    with self.store.read_current(seconds=60) as view:
                        checkpoint=(metadata_checkpoints.pop(owner) if owner in metadata_checkpoints
                                    else view.derived(*owner,'checkpoint'))
                    update=UNCHANGED
                    if (isinstance(checkpoint,dict) and
                            checkpoint.get('dependencies',{}).get('pricing')==old['dependencies']['pricing']):
                        update=dict(checkpoint,dependencies=dict(checkpoint['dependencies'],pricing=dependency['pricing']))
                        self.metrics['pricing_checkpoint_updates']=self.metrics.get('pricing_checkpoint_updates',0)+1
                    stage.owner(*owner,old['metadata'],dependency,
                        source_ordinal=references[owner]['source_ordinal'],replace=True,checkpoint=update)
                    self.metrics['pricing_updates']=self.metrics.get('pricing_updates',0)+1
                    continue
                if owner in display and owner not in elected:
                    old=current[owner];entry=references[owner];dependency=display[owner]
                    metadata=dict(old['metadata'],title=entry['display_title'],
                        usage_revision=(entry['body_digest'],dependency['mode']),request_revision=entry['body_digest'])
                    calls=dict(metadata,**old['call_metadata'])
                    calls['title']=entry['display_title']
                    with self.store.read_current(seconds=60) as view:
                        summary=view.derived(*owner,'summary')
                        checkpoint=(metadata_checkpoints.pop(owner) if owner in metadata_checkpoints
                                    else view.derived(*owner,'checkpoint'))
                    if summary is not None:summary=dict(summary,own=dict(summary['own'],title=entry['display_title']))
                    if isinstance(checkpoint,dict):
                        checkpoint=dict(checkpoint,source_digest=entry['body_digest'],
                            candidate_source_digest=entry['body_digest'],context_digest=entry['append_context'],ledger_append=None)
                    stage.owner(*owner,metadata,dependency,source_ordinal=entry['source_ordinal'],replace=True,
                        call_metadata=calls,summary=summary,checkpoint=checkpoint)
                    self.metrics['display_updates']=self.metrics.get('display_updates',0)+1
                    continue
                raw, mode, mode_ancestry_required = read_enriched_source(self.client, owner, catalog, self.ledger,
                    parents=parents, cancel=self.cancel, source_loader=self._load, limits=self.limits,
                    include_mode_proof=True)
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
                    speed_certificates[owner]=self.speed_writer.prefix_certificate(
                        owner,previous_speed[owner],material['state']['prepared']['history'])
                dependency = dependencies(references[owner]['body_digest'], mode, ownership.hexdigest(),
                                          state=references[owner].get('state',{}),models=material['choices']['models'])
                dependency['mode_ancestry_required'] = mode_ancestry_required
                dependency['state_facts']=state_facts(references[owner].get('state',{}))
                if references[owner].get('analysis_digest'):
                    dependency['analysis_digest']=references[owner]['analysis_digest']
                # A competing owner's new body may be prepared before its own
                # conversion cohort reconciles candidates. Keep that inventory's
                # independent proof so later cohorts and restarts still owe it.
                old = current.get(owner)
                dependency['candidate_source_digest'] = candidate_sources.get(owner,
                    old['dependencies'].get('candidate_source_digest') if old is not None else None)
                dependency['prepared'] = True
                stage_owner(stage, material, dependency, references[owner]['source_ordinal'], replace=True,
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
                        if selected and owner not in catalog['owners']:
                            continue
                        entry=view.owner(*owner)
                        if entry['metadata'].get('_internal_review'):
                            current_speed.pop(owner,None)
                        else:
                            current_speed[owner]=self._speed_stamp(entry,speed_certificates.get(owner))
            token = self._publish(stage,source,ready,previous_speed,current_speed)
            if appended_plans:
                self.metrics['append_owners']=self.metrics.get('append_owners',0)+len(appended_plans)
                self.metrics['append_rows']=self.metrics.get('append_rows',0)+sum(len(plan.rows) for plan in appended_plans)
                self.metrics['append_checkpoint_bytes']=sum(plan.checkpoint_bytes for plan in appended_plans)
            return dict(token=token, changed=tuple(affected), removed=tuple(removed), writes=dict(stage.writes))
        except BaseException:
            self._abort(stage)
            raise
        finally:
            # Source rows are useful only within this atomic cohort. Persistent
            # reuse relies on the committed digests, never retained raw objects.
            self.cache.clear()
            self.cache_bytes = 0

    def convert(self, catalog, *, cohort_size=None):
        if not catalog['membership_complete']:
            raise ValueError('conversion waits for complete source membership')
        automatic = cohort_size is None
        cohort_size = 16 if automatic else cohort_size
        if not 1 <= cohort_size <= self.store.limits.owners_per_batch:
            raise ValueError('invalid conversion cohort size')
        was_ready = bool(self.store.db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0])
        selected = catalog.get('authority')=='selected'
        self.order_changed = not was_ready
        work = ConversionPlan(self.store)
        try:
            with self.store.read_current(seconds=60) as view:
                current = self._inventory(view)
                if selected:
                    self.order_changed = self.order_changed or view.source_identity != source_identity(catalog)
                elif view.db.execute('SELECT selection_batch FROM meta WHERE singleton=1').fetchone()[0]:
                    self.order_changed = True
                self.catalog = catalog = self._capture_catalog(catalog,current)
                parents = mode_parent_index(catalog)
                metadata_only = was_ready
                legacy_pricing = _pricing_dependency(pricing_signature())
                for owner,entry in catalog['owners'].items():
                    self._check()
                    old = current.get(owner)
                    if not selected and (old is None or old['owner_ordinal'] != entry['source_ordinal']):
                        self.order_changed = True
                    wanted = dependencies(entry['body_digest'],
                        mode_dependency(mode_context(owner,catalog,parents=parents,
                            ancestry_required=old is not None and old['dependencies'].get('mode_ancestry_required') is not False),
                            catalog,self.ledger),
                        old['dependencies'].get('ownership') if old else None,state=entry.get('state',{}),
                        models=old.get('models') if old else None)
                    metadata_only = metadata_only and (old is not None and old['dependencies'].get('prepared')
                        and old['dependencies'].get('candidate_source_digest') == entry['body_digest']
                        and all(old['dependencies'].get(key) == value for key,value in wanted.items() if key != 'pricing')
                        and old['dependencies'].get('pricing') in (wanted['pricing'],legacy_pricing))
                    if (old is None or not old['dependencies'].get('prepared')
                            or old['dependencies'].get('candidate_source_digest') != entry['body_digest']
                            or any(old['dependencies'].get(key) != value for key,value in wanted.items())):
                        work.add(owner,keep=True)
                if not selected:
                    for owner in current:
                        self._check()
                        if owner not in catalog['owners']:
                            work.add(owner,keep=False)
            total = sum(work.counts.values())
            self.order_changed = self.order_changed or bool(work.counts[False])
            if not total:
                if self.order_changed:
                    token = self.publish_ready(catalog)
                    yield dict(token=token,changed=(),removed=(),writes=self.last_writes,ready=True)
                return
            if automatic and was_ready and cohort_size < total <= self.store.limits.owners_per_batch:
                group = next(work.groups(self.store.limits.owners_per_batch))
                try:
                    result = self.apply(catalog,[key for keep,key in group if keep],
                        [key for keep,key in group if not keep],ready=True,_parents=parents)
                except BatchCapacityExceeded:
                    pass
                else:
                    yield dict(result,ready=True,processed=total,total=total)
                    return
            completed = 0
            for group in work.groups(cohort_size):
                ready = metadata_only and not work.counts[False] or completed + len(group) == total
                result = self.apply(catalog,[key for keep,key in group if keep],
                    [key for keep,key in group if not keep],ready=ready,_parents=parents)
                completed += len(group)
                yield dict(result,ready=ready,processed=completed,total=total)
        finally:
            work.close()
