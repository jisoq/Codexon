"""Query-only AnalysisEngine adapter for a published prepared-store generation."""
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import replace

from .analysis_engine import AnalysisEngine,retained_bytes
from .analysis_records import compact_record
from .analytics import WholeRequestFacts, analyze, project_matches
from .pricing import display_tier
from .prepared_store import Limits, PreparedReader, NotReady, CapacityExceeded


def batches(values, count=128):
    group = []
    for value in values:
        group.append(value)
        if len(group) == count:
            yield group
            group = []
    if group:
        yield group


class PreparedAnalysisEngine(AnalysisEngine):
    """Retains light owner state and ordinary bounded result caches, never raw history.

    The owner supplies a committed Token after ingestion. Every query/detail pins
    that token; a newer database generation returns StaleGeneration to the actor.
    The initial 60-second transaction lease is explicit and configurable. It does
    not reopen unversioned pages while an allowed whole-history query is running.
    """
    OWNER_BYTES=64*1024*1024

    def __init__(self, path, token, *, limits=None, cancel=None, scope=(None,None)):
        super().__init__()
        limits = limits or replace(Limits(), read_seconds=60, read_vm_steps=20_000_000)
        self.reader = PreparedReader(path, limits)
        self.token = token
        self.cancel = cancel
        self.scope=scope
        self.snapshot = None
        self.revision = self.call_revision = self.generation = token.generation
        self.read_metrics = dict(range_rows=0, whole_rows=0, first_rows=0, header_rows=0)
        self.owner_weights={};self.owner_bytes=0
        self.order_identity=None
        try:
            with self.read_scope():
                with self.snapshot._cursor('SELECT COALESCE(MAX(id),0) FROM publications WHERE state=1') as cursor:
                    self.publication_batch=cursor.fetchone()[0]
                self.order_identity=(token.instance,self.snapshot.order_batch)
                after = None
                while True:
                    page = self.snapshot.catalog(after=after,query=True,home=scope[0],sid=scope[1])
                    for row in page.rows:
                        self._adopt_owner(row['owner'], row['metadata'], row['call_metadata'], row['owner_ordinal'], row['request_maps'])
                    if page.complete:
                        break
                    after = page.after
        except BaseException:
            self.close()
            raise

    def _adopt_owner(self, key, metadata, call_metadata, ordinal, request_maps):
        state, internal = self._owner_state(key, metadata, call_metadata, self.revision, ordinal, request_maps)
        self._set_owner(key, state, internal)

    @staticmethod
    def _owner_state(key, metadata, call_metadata, revision, ordinal, request_maps):
        source = dict(metadata)
        internal = source.pop('_internal_review', None)
        if type(internal) is not bool:
            raise NotReady('prepared owner lacks the canonical internal-review classification')
        if source.get('home', key[0]) != key[0] or source.get('id', key[1]) != key[1]:
            raise NotReady('prepared owner metadata does not match its identity')
        source.setdefault('home', key[0])
        source.setdefault('id', key[1])
        state = dict(prepared=source, source=source, revision=revision,
                     content_revision=revision, owner=key, owner_ordinal=ordinal,
                     call_owner={**source,**call_metadata}, request_maps=request_maps)
        return state, internal

    def _set_owner(self, key, state, internal):
        weight=retained_bytes((key,state),self.OWNER_BYTES)
        admitted=self.owner_bytes-self.owner_weights.get(key,0)+weight
        if admitted>self.OWNER_BYTES:
            raise CapacityExceeded('prepared query owner metadata exceeds its aggregate budget')
        self.owner_weights[key]=weight;self.owner_bytes=admitted
        target = self.internal_sessions if internal else self.sessions
        other = self.sessions if target is self.internal_sessions else self.internal_sessions
        other.pop(key, None)
        target[key] = state

    def adopt(self, token, changed_owners, removed=()):
        """Ingestion supplies its owner delta; no new-generation catalog rescan."""
        if self.snapshot is not None:
            raise RuntimeError('finish the active query before adopting a generation')
        pending = {}
        def selected(key):
            return all(value is None or key[i]==value for i,value in enumerate(self.scope))
        removed={tuple(key) for key in removed if selected(key)}
        with self.reader.pin(token, cancel=self.cancel) as snapshot:
            for key in changed_owners:
                if not selected(key):continue
                if len(pending) >= self.reader.limits.owners_per_batch:
                    raise CapacityExceeded('owner adoption exceeds the committed batch bound')
                key = tuple(key)
                owner = snapshot.owner(*key)
                pending[key] = (self._owner_state(key, owner['metadata'], owner['call_metadata'], token.generation, owner['owner_ordinal'], owner['request_maps'])
                                if owner is not None else None)
            # Request activity and titles do not change category or chart facts.
            # Inspect committed call writes, including deletion markers, rather
            # than treating the publication generation as a call revision.
            with snapshot._cursor('SELECT COALESCE(MAX(id),0) FROM publications WHERE state=1') as cursor:
                publication_batch=cursor.fetchone()[0]
            with snapshot._cursor('SELECT 1 FROM calls r INDEXED BY calls_batch '
                    'JOIN publications p ON p.id=r.batch '
                    'WHERE r.batch>? AND r.batch<=? AND p.state=1 LIMIT 1',
                    (self.publication_batch,publication_batch)) as cursor:
                calls_changed=cursor.fetchone() is not None
            analytical_fields=('home','id','sid','project','project_name','cwd','source','archived',
                               'model','effort','service_tier','mode_conflict',
                               'service_tier_source','display_service_tier')
            same_facts=not calls_changed and not removed
            for key,replacement in pending.items():
                previous=self.sessions.get(key,self.internal_sessions.get(key))
                if previous is None or replacement is None:
                    same_facts=False;break
                state,internal=replacement
                if (internal!=(key in self.internal_sessions) or any(
                        previous['call_owner'].get(field)!=state['call_owner'].get(field)
                        or previous['prepared'].get(field)!=state['prepared'].get(field)
                        for field in analytical_fields)):
                    same_facts=False;break
            order = {}
            order_bytes = 0
            order_identity=(token.instance,snapshot.order_batch)
            same_order=(order_identity==self.order_identity and not removed
                        and all(key in self.sessions or key in self.internal_sessions for key in pending)
                        and all(value is not None for value in pending.values()))
            entries=(dict(owner=key,owner_ordinal=pending[key][0]['owner_ordinal'] if key in pending else state['owner_ordinal'])
                     for states in (self.sessions,self.internal_sessions) for key,state in states.items()) if same_order else self._pages(snapshot.ordering,home=self.scope[0],sid=self.scope[1])
            for entry in entries:
                key, ordinal = entry['owner'], entry['owner_ordinal']
                order_bytes += retained_bytes((key,ordinal),self.OWNER_BYTES)+160
                if order_bytes > self.OWNER_BYTES:
                    raise CapacityExceeded('query catalog order exceeds its metadata budget')
                if key in order:
                    raise NotReady('catalog order repeats a query owner')
                order[key] = ordinal
        expected=(self.sessions.keys()|self.internal_sessions.keys()|pending.keys())-removed
        expected.difference_update(key for key,value in pending.items() if value is None)
        if order.keys() != expected:
            raise NotReady('catalog order does not match adopted query owners')
        differences = {}
        for key,ordinal in order.items():
            value = pending.get(key)
            state = value[0] if value is not None else self.sessions.get(key,self.internal_sessions.get(key))
            differences[key] = retained_bytes(ordinal,self.OWNER_BYTES)-retained_bytes(state['owner_ordinal'],self.OWNER_BYTES)
        weights={key:retained_bytes((key,value[0]),self.OWNER_BYTES) if value is not None else 0
                 for key,value in pending.items()}
        admitted=(self.owner_bytes-sum(self.owner_weights.get(key,0) for key in pending.keys()|removed)
                  +sum(weight for key,weight in weights.items() if key not in removed)+sum(differences.values()))
        if admitted>self.OWNER_BYTES:
            raise CapacityExceeded('prepared query metadata adoption exceeds its aggregate budget')
        history=getattr(self,'_prepared_history_projection',None)
        keep_history=history is not None and same_order and history.unaffected(self,pending,removed)
        saved_page=self.page_results.get(history.page_key) if keep_history else None
        if not keep_history:self.clear_history_projection()
        self.token = token
        self.order_identity=order_identity
        self.publication_batch=publication_batch
        self.revision = self.generation = token.generation
        same_facts=same_facts and same_order
        if not same_facts:self.call_revision=token.generation
        self.results.clear()
        affected=set(pending)|{tuple(key) for key in removed}
        self.parts.discard(lambda key:key[0] in affected)
        self.page_results.clear()
        for name in ('_explorer_projection', '_history_prepared'):
            self.__dict__.pop(name, None)
        if not same_facts:
            for name in ('_category_projection','performance','_trend_catalog','_trend_bounds'):
                self.__dict__.pop(name,None)
        elif getattr(self,'performance',None) is not None and self.performance.signature is not None:
            self.performance.signature=(self.revision,*self.performance.signature[1:])
        for key in removed:
            self.sessions.pop(tuple(key), None)
            self.internal_sessions.pop(tuple(key), None)
            self.owner_bytes-=self.owner_weights.pop(tuple(key),0)
        for key,state in pending.items():
            if key in removed:
                continue
            if state is None:
                self.sessions.pop(key, None)
                self.internal_sessions.pop(key, None)
                self.owner_bytes-=self.owner_weights.pop(key,0)
            else:
                value,internal=state
                target=self.internal_sessions if internal else self.sessions
                other=self.sessions if internal else self.internal_sessions
                other.pop(key,None)
                target[key]=value
                self.owner_weights[key]=weights[key]
        for key, ordinal in order.items():
            state = self.sessions.get(key, self.internal_sessions.get(key))
            difference = differences[key]
            state['owner_ordinal'] = ordinal
            self.owner_weights[key] += difference
        self.owner_bytes=admitted
        self.sessions = OrderedDict(sorted(self.sessions.items(), key=lambda pair:pair[1]['owner_ordinal']))
        self.internal_sessions = OrderedDict(sorted(self.internal_sessions.items(), key=lambda pair:pair[1]['owner_ordinal']))
        if keep_history:
            history.adopt(self)
            if saved_page is not None:
                value,deadline=saved_page
                value=dict(value,key=history.head['key'],revision=self.revision)
                cached=(value,deadline)
                self.page_results.put(history.page_key,cached,
                    retained_bytes((history.page_key,cached),self.page_results.rows))

    @contextmanager
    def read_scope(self):
        if self.snapshot is not None:
            yield self.snapshot
            return
        with self.reader.pin(self.token, cancel=self.cancel) as snapshot:
            self.snapshot = snapshot
            try:
                yield snapshot
            finally:
                self.snapshot = None

    def close(self):
        self.clear_history_projection()
        if hasattr(self, 'reader'):
            self.reader.close()

    def clear_history_projection(self):
        from .prepared_history import close_for_engine
        close_for_engine(self)

    def history_project(self, q):
        from .prepared_history import project
        with self.read_scope():
            return project(self, q)

    def query(self, q):
        with self.read_scope():
            return super().query(q)

    def page_query(self, q):
        with self.read_scope():
            return super().page_query(q)

    def record(self, home, sid, key):
        if (home, sid) not in self.sessions:
            return None
        with self.read_scope():
            return self.snapshot.record(home, sid, key)

    def _pages(self, method, *args, **kwargs):
        after = None
        while True:
            page = method(*args, after=after, **kwargs)
            yield from page.rows
            if page.complete:
                return
            after = page.after
            del page

    def _selected(self, q):
        for key, state in self.sessions.items():
            source = state['prepared']
            if not q.get('archived', True) and source.get('archived'):
                continue
            if q.get('source') and source.get('source', 'unknown') != q['source']:
                continue
            if q.get('home') and key[0] != q['home']:
                continue
            if q.get('sid') and key[1] != q['sid']:
                continue
            if q.get('project') and not project_matches(source, q['project']):
                continue
            yield key, state

    def query_source(self, session_key, state, q):
        source = state['prepared']
        if not q.get('include_whole_history', True):
            result={key:value for key,value in source.items() if key not in ('totals','remaining')}
            # Only selected incident identities are needed for attention and
            # event detail. The annotated canonical rows already name them.
            events=[]
            selection=q.get('explorer') or {}
            event_id=selection.get('event') if tuple(selection.get('session') or ())==session_key else None
            identities=[]
            if q['page']==0:
                part=self.query_part(session_key,state,q['start'],q['end'],q.get('model',''),
                                     q.get('effort',''),q.get('service_tier',''))
                identities=list(dict.fromkeys(row['cache_incident_id'] for row in
                    (part or {}).get('responses',()) if row.get('cache_incident_id') is not None))
            if event_id is not None and str(event_id) not in {str(value) for value in identities}:
                identities.append(event_id)
            if identities:
                from .prepared_health import FORMAT,hydrate_health
                ordered=[]
                for identity in identities:
                    event=hydrate_health(self.snapshot,session_key,event_id=identity,evidence=False)
                    if event is not None:
                        descriptor=self.snapshot.section_item(session_key,(FORMAT,'event-id'),str(identity))
                        ordered.append((descriptor['ordinal'],self._analysis_event(event)))
                events=[event for _,event in sorted(ordered,key=lambda pair:pair[0])]
            if source.get('cache_health') is not None:
                result['cache_health']=dict(source['cache_health'],events=events)
            if source.get('cache_misses') is not None:
                result['cache_misses']=dict(source['cache_misses'],events=[])
            return result
        # The general compatibility API explicitly returns complete request maps.
        result = dict(source)
        if 'turn_records' in state['request_maps']:
            result['turn_records'] = {r['key']: r['metadata'] for r in self._pages(self.snapshot.requests, session_key)}
        if 'turn_states' in state['request_maps']:
            result['turn_states'] = {r['key']: r['turn_state'] for r in self._pages(self.snapshot.request_headers, session_key)
                                     if 'turn_state' in r}
        from .prepared_health import hydrate_health
        health=hydrate_health(self.snapshot,session_key,evidence=False)
        for name in ('cache_health','cache_misses'):
            if name in source and name in health:
                value=health[name]
                if name=='cache_health' and value is not None:
                    value=dict(value)
                    if 'events' in value:value['events']=[self._analysis_event(event) for event in value['events']]
                    if value.get('incident'):value['incident']=self._analysis_event(value['incident'])
                result[name]=value
        return result

    @staticmethod
    def _analysis_event(event):
        # summarize_session adds these projections to a copy of CacheHealth's
        # event. Analysis consumes the original classifier fields, while the
        # selected overlay consumes the enriched event from the same section.
        from .prepared_health import canonical_health_event
        return canonical_health_event(event)

    def whole_query_turns(self, session_key, state):
        result = defaultdict(list)
        for row in self.snapshot.iter_calls(session_key):
            row = compact_record(row)
            self.read_metrics['whole_rows'] += 1
            if row.get('turn'):
                result[(*session_key, row['turn'])].append(row)
        return dict(result)

    def query_part(self, session_key, state, start, end, model, effort, mode):
        part_key = (session_key, state['revision'], start, end, model, effort, mode)
        cached = self.parts.get(part_key)
        if cached is not None:
            return cached
        rows = [compact_record(row) for row in self.snapshot.iter_calls(session_key, start=start, end=end)]
        self.read_metrics['range_rows'] += len(rows)
        boundaries = {}
        if not any((model, effort, mode)):
            for field in ('started_at', 'ended_at'):
                for request in self._pages(self.snapshot.boundaries, session_key, field, start, end):
                    boundaries[request['key']] = request
        if not rows and not boundaries:
            return None
        keys = dict.fromkeys(row['turn'] for row in rows if row.get('turn') and
            (not model or (row.get('model') or '미확인') == model) and
            (not effort or (row.get('effort') or '미확인') == effort) and
            (not mode or display_tier(row) == mode))
        keys.update((key, None) for key in boundaries)
        headers, records = {}, dict(boundaries)
        for selected in batches(keys):
            for header in self._pages(self.snapshot.request_headers, session_key, selected, with_first=True):
                headers[header['key']] = header
                self.read_metrics['header_rows'] += 1
                self.read_metrics['first_rows'] += bool(header['first_call'])
            for request in self._pages(self.snapshot.requests, session_key, selected):
                records[request['key']] = request
        if headers.keys() != keys.keys():
            raise NotReady('the published generation lacks a complete touched-request header')
        whole = {(*session_key, key): WholeRequestFacts(h['call_count'], h['minimum'], h['maximum'],
                  h['dimensions'], compact_record(h['first_call']) if h['first_call'] else None)
                  for key, h in headers.items()}
        source = dict(state['prepared'], history=rows, _prepared=True,
                      turn_records={r['key']: r['metadata'] for r in sorted(records.values(), key=lambda r:r['ordinal'])},
                      turn_states={key:h['turn_state'] for key,h in headers.items() if 'turn_state' in h})
        boundary_keys = [r['key'] for r in sorted(boundaries.values(), key=lambda r:r['ordinal'])]
        part = analyze([source], start, end, model, with_comparisons=False, service_tier=mode, effort=effort,
                       _range_index=((0, len(rows)), {}), _boundary_turns=boundary_keys, _whole_facts=whole)
        self.prepare_part(part)
        self.metrics['part_builds'] += 1
        self.parts.put(part_key, part, len(rows) + len(headers) + 1)
        return part

    def call_ranges(self, q):
        with self.read_scope():
            for _, state in self._selected(q):
                yield state, q['start'], q['end']

    def range_calls(self, state, lo, hi, q):
        model, effort, mode = (q.get(k, '') for k in ('model', 'effort', 'service_tier'))
        with self.read_scope():
            for row in self.snapshot.iter_calls(state['owner'], start=lo, end=hi):
                row = compact_record(row)
                self.read_metrics['range_rows'] += 1
                if ((not model or (row.get('model') or '미확인') == model) and
                    (not effort or (row.get('effort') or '미확인') == effort) and
                    (not mode or display_tier(row) == mode)):
                    yield row

    def whole_request_calls(self, owner, turn):
        with self.read_scope():
            for row in self.snapshot.iter_calls(owner, turn=turn):
                self.read_metrics['whole_rows'] += 1
                yield compact_record(row)

    def request_positions(self, owner, turns):
        result = {}
        with self.read_scope():
            for selected in batches(dict.fromkeys(turns)):
                for header in self._pages(self.snapshot.request_headers, owner, selected):
                    result[header['key']] = (header['display_ordinal'] or None,
                        header['effective_at'] if header['effective_at'] is not None else float('inf'))
                    self.read_metrics['header_rows'] += 1
        return result

    def unlinked_total(self, owner):
        with self.read_scope():
            return self.snapshot.unlinked_count(owner)

    def performance_input(self, query, width, granularity):
        from .prepared_performance import performance_input
        with self.read_scope():
            return performance_input(self, query, width, granularity)

    def prepared_category_data(self, scope, dimension, metric, breakdowns, selection):
        from .prepared_category import project_data
        with self.read_scope():
            return project_data(self,scope,dimension,metric,breakdowns,selection)

    def category_bounds(self, selection):
        from .category_usage import category
        call_fields = {'model', 'effort', 'service_tier'}
        call_filters = {k:v for k,v in selection.items() if k in call_fields}
        owner_filters = {k:v for k,v in selection.items() if k not in call_fields}
        with self.read_scope():
            owners=(owner for owner,state in self.sessions.items() if
                    all(category(state['call_owner'],key)==value for key,value in owner_filters.items()))
            return self.snapshot.category_bounds_for_owners(owners,call_filters)

    def boundary_times(self, at):
        with self.read_scope():
            for owner in self.sessions:
                boundary = self.snapshot.next_boundary(owner, at)
                if boundary is not None:
                    yield boundary
