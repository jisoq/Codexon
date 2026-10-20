"""Apply only uncommitted prepared owners, with durable source/activity progress."""
from types import SimpleNamespace
from copy import deepcopy
from contextlib import closing
import sys

from . import quota_prepared_sync as progress
from .prepared_ingest import _weight, pages
from .prepared_store import CapacityExceeded, NotReady, StaleGeneration, ReadCancelled, Page


METADATA_BYTES = 64 * 1024 * 1024
FACT_BYTES = 256 * 1024 * 1024
OWNER_BATCH = 16


class _SourceCacheAdmission(CapacityExceeded):
    """Optional retained templates cannot share the invocation's metadata cap."""


class PreparedLedger:
    def __init__(self, ledger, store, *, cancel=None):
        self.ledger, self.store = ledger, store
        self.cancel = cancel
        progress.initialize(ledger.db)
        self.metrics = dict(owner_reads=0,call_rows=0,request_rows=0,applied_owners=0)
        # Call certificates do not certify tracking applicability. Request maps
        # are replayed once per process and again when tracking policy changes.
        self.task_revisions={}
        self._source_cache={}
        self._cache_token=None
        self._cache_order=None
        self._source_token=None
        self._source_cache_bytes=sys.getsizeof(self._source_cache)

    @staticmethod
    def _require_view(view, expected=None):
        if not view.ready or view.staged_batch is not None:
            raise NotReady('prepared ledger requires a complete committed generation')
        if expected is not None and view.token != expected:
            raise StaleGeneration('prepared ledger facts no longer match its source inventory')

    @staticmethod
    def _source_slot(key, revision, entry):
        source=dict(entry['metadata'])
        source.setdefault('home',key[0]);source.setdefault('id',key[1])
        if (source['home'],source['id']) != key:
            raise ValueError('prepared ledger source belongs to another owner')
        bounds=entry['dependencies'].get('coverage_bounds')
        if not isinstance(bounds,tuple) or len(bounds)!=2:
            raise ValueError('prepared ledger source lacks coverage bounds')
        # Cache only immutable prepared content. Per-invocation request facts
        # and policy tokens are attached to independently copied envelopes.
        source['_coverage_bounds']=bounds
        source['usage_revision']=progress.source_stamp(entry['dependencies'])
        source['_request_source_digest']=entry['dependencies'].get('analysis_digest') or entry['dependencies']['source_digest']
        source_bytes=_weight(source,METADATA_BYTES)
        weight=(_weight(key,METADATA_BYTES)+_weight(revision,METADATA_BYTES)+source_bytes+
                sys.getsizeof((revision,source,source_bytes,0))+2*sys.getsizeof(source_bytes))
        return revision,source,source_bytes,weight

    def _check_plan(self, token):
        if self.cancel is not None and self.cancel():
            raise ReadCancelled('prepared ledger synchronization cancelled')
        if self.store.token != token:
            raise StaleGeneration('prepared ledger source plan is no longer current')

    def _sources(self):
        try:
            return self._cached_sources()
        except _SourceCacheAdmission:
            # A cache must not reject an inventory that fits without a second
            # retained copy. Leave the exception scope before rebuilding so its
            # traceback cannot keep rejected candidate templates alive.
            self._source_cache={}
            self._source_cache_bytes=sys.getsizeof(self._source_cache)
            self._cache_token=None
            self._source_token=None
            self.metrics['source_cache_fallbacks']=self.metrics.get('source_cache_fallbacks',0)+1
        return self._uncached_sources()

    def _source_entries(self, view, headers, cache):
        for start in range(0, len(headers), OWNER_BATCH):
            selected = headers[start:start+OWNER_BATCH]
            misses = [row['owner'] for row in selected
                      if row['owner'] not in cache or cache[row['owner']][0] != row['revision']]
            if len(misses) <= 1:
                for header in selected:
                    yield header, (view.ledger_source(*header['owner']) if header['owner'] in misses else None)
                continue
            with view.member_packets(misses, 'ledger') as packets:
                for header in selected:
                    if header['owner'] not in misses:
                        yield header, None
                        continue
                    row = next(packets, None)
                    if row is None or row[:2] != (header['owner'], header['revision']):
                        raise NotReady('prepared ledger miss does not match its committed revision')
                    yield header, row[2]
                    del row

    def _uncached_sources(self):
        """Build one invocation-owned inventory when optional retention cannot fit."""
        sources={};certificates={};retained=0;raw_peak=0
        with self.store.read_current(cancel=self.cancel,seconds=60) as view:
            self._require_view(view)
            after=None
            while True:
                page=view.owner_revisions(after=after)
                page_bytes=(page.object_bytes+sys.getsizeof(page)+sys.getsizeof(vars(page))+
                            sys.getsizeof(page.rows)+_weight((after,page.after,page.complete,
                                page.encoded_bytes,page.object_bytes),METADATA_BYTES))
                raw_peak=max(raw_peak,page_bytes)
                if retained+sys.getsizeof(sources)+sys.getsizeof(certificates)+raw_peak>METADATA_BYTES:
                    raise CapacityExceeded('prepared ledger source page exceeds its metadata budget')
                for header in page.rows:
                    view.check()
                    key=header['owner']
                    if key in sources:
                        raise ValueError('prepared ledger inventory repeats an owner')
                    entry=view.ledger_source(*key)
                    if entry is None:
                        raise NotReady('prepared ledger inventory references an absent owner')
                    decoded=_weight(entry,METADATA_BYTES)
                    slot=self._source_slot(key,header['revision'],entry)
                    self.metrics['metadata_reads']=self.metrics.get('metadata_reads',0)+1
                    # These sources belong to this invocation; no cached alias
                    # survives and no second deep copy is needed.
                    sources[key]=slot[1]
                    certificates[key]=slot[1]['usage_revision']
                    retained+=slot[2]+_weight(key,METADATA_BYTES)+sys.getsizeof(certificates[key])
                    raw_peak=max(raw_peak,page_bytes+decoded+sys.getsizeof(slot)+2*sys.getsizeof(slot[2]))
                    del entry,slot
                    if retained+sys.getsizeof(sources)+sys.getsizeof(certificates)+raw_peak>METADATA_BYTES:
                        raise CapacityExceeded('prepared ledger sources exceed their metadata budget')
                after,complete=page.after,page.complete
                if page.rows:del header
                del page
                if complete:break
            view.check()
            token=view.token
        self._source_token=token
        return sources,certificates

    def _revision_pages(self,view):
        delta=self.store.changes.between(self._cache_token,view.token)
        if (delta is None or self._cache_order!=view.order_batch
                or delta[1] or not delta[0]<=self._source_cache.keys()):
            after=None
            while True:
                page=view.owner_revisions(after=after)
                yield page
                if page.complete:return
                after=page.after
        changed=sorted(delta[0]);versions={}
        for start in range(0,len(changed),128):
            after=None
            while True:
                page=view.owner_revisions(changed[start:start+128],after=after)
                versions.update((row['owner'],row['revision']) for row in page.rows)
                if page.complete:break
                after=page.after
        if versions.keys()!=delta[0]:raise NotReady('ledger transition references an absent owner')
        rows=[]
        for owner,slot in self._source_cache.items():
            rows.append(dict(owner=owner,revision=versions.get(owner,slot[0])))
            if len(rows)==128:
                yield Page(tuple(rows),None,False,0,_weight((rows,versions,changed),METADATA_BYTES));rows=[]
        if rows:yield Page(tuple(rows),None,True,0,_weight((rows,versions,changed),METADATA_BYTES))

    def _cached_sources(self):
        sources={};certificates={};raw_peak=0
        with self.store.read_current(cancel=self.cancel,seconds=60) as view:
            self._require_view(view)
            if self._cache_token == view.token:
                candidate=self._source_cache
                shared=sum(slot[3] for slot in candidate.values())
                candidate_weight=shared
                self.metrics['source_cache_hits']=self.metrics.get('source_cache_hits',0)+len(candidate)
            else:
                candidate={};shared=0;candidate_weight=0
                after=None
                for page in self._revision_pages(view):
                    # The reader retains this decoded page while selected miss
                    # packets and replacement templates are simultaneously live.
                    page_bytes=(page.object_bytes+sys.getsizeof(page)+sys.getsizeof(vars(page))+
                                sys.getsizeof(page.rows)+_weight((after,page.after,page.complete,
                                    page.encoded_bytes,page.object_bytes),METADATA_BYTES))
                    raw_peak=max(raw_peak,page_bytes)
                    if self._source_cache_bytes-shared+candidate_weight+sys.getsizeof(candidate)+raw_peak>METADATA_BYTES:
                        raise _SourceCacheAdmission('prepared ledger revision page exceeds its metadata budget')
                    with closing(self._source_entries(view, page.rows, self._source_cache)) as entries:
                        for header, entry in entries:
                            view.check()
                            key=header['owner'];revision=header['revision']
                            if key in candidate:
                                raise ValueError('prepared ledger inventory repeats an owner')
                            previous=self._source_cache.get(key)
                            if previous is not None and previous[0]==revision:
                                slot=previous;shared+=slot[3]
                                self.metrics['source_cache_hits']=self.metrics.get('source_cache_hits',0)+1
                            else:
                                if entry is None:
                                    raise NotReady('prepared ledger inventory references an absent owner')
                                raw_peak=max(raw_peak,page_bytes+8192+_weight(entry,METADATA_BYTES))
                                slot=self._source_slot(key,revision,entry)
                                self.metrics['metadata_reads']=self.metrics.get('metadata_reads',0)+1
                                del entry
                            candidate[key]=slot
                            candidate_weight+=slot[3]
                            if self._source_cache_bytes-shared+candidate_weight+sys.getsizeof(candidate)+raw_peak>METADATA_BYTES:
                                raise _SourceCacheAdmission('prepared ledger source replacements exceed their metadata budget')
                    after,complete=page.after,page.complete
                    if page.rows:del header
                    # Do not hold the previous page while the reader allocates
                    # its successor. Only the small continuation cursor survives.
                    del page
            cache_bytes=(sys.getsizeof(candidate)+candidate_weight+sys.getsizeof(view.token)+
                         sys.getsizeof(self._source_cache_bytes))
            # Envelope copies have no version/slot tuples. Charging the whole
            # slot still bounds nested copies conservatively without rewalking
            # source trees on hits. Both old replacements and raw misses count.
            envelopes=2*(sys.getsizeof(candidate)+64*len(candidate)+64)+candidate_weight
            admission=cache_bytes+envelopes+self._source_cache_bytes-shared+raw_peak
            if admission>METADATA_BYTES:
                raise _SourceCacheAdmission('prepared ledger cache and envelopes exceed their metadata budget')
            for key,slot in candidate.items():
                view.check()
                sources[key]=deepcopy(slot[1])
                certificates[key]=slot[1]['usage_revision']
            view.check()
            token=view.token
        self._source_cache=candidate
        self._source_cache_bytes=cache_bytes
        self._cache_token=token
        self._cache_order=view.order_batch
        self._source_token=token
        return sources,certificates

    def _owner(self, key, remaining, *, calls=True, requests=True, expected=None):
        rows=[];boundaries={};size=0
        with self.store.read_current(cancel=self.cancel,seconds=60) as view:
            self._require_view(view,expected)
            if calls:
                for row in view.iter_calls(key):
                    size+=_weight(row,remaining-size)+16
                    if size>remaining:raise CapacityExceeded('prepared ledger owner exceeds its fact budget')
                    rows.append(row)
            if requests:
                for item in pages(view.requests,key):
                    size+=_weight((item['key'],item['metadata']),remaining-size)+96
                    if size>remaining:raise CapacityExceeded('prepared ledger requests exceed their fact budget')
                    boundaries[item['key']]=item['metadata']
        self.metrics['owner_reads']+=1
        self.metrics['call_rows']+=len(rows)
        self.metrics['request_rows']+=len(boundaries)
        return dict(history=rows,turn_records=boundaries),size

    def _task_policy(self,home):
        config=self.ledger.db.execute('SELECT started FROM tracking_config WHERE home=?',(home,)).fetchone()
        control=self.ledger.db.execute('SELECT enabled,at FROM tracking_controls WHERE home=? ORDER BY at DESC LIMIT 1',(home,)).fetchone()
        active=config is not None and (control is None or bool(control[0]))
        return active,(config[0] if config else None,tuple(control) if control else None)

    def _append(self,key,source,committed,policy,remaining,*,expected=None):
        """Read a bounded tail only after both call and task predecessors match."""
        from .prepared_append import FORMAT as CHECKPOINT_FORMAT
        with self.store.read_current(cancel=self.cancel,seconds=60) as view:
            self._require_view(view,expected)
            checkpoint=view.derived(*key,'checkpoint')
            if not isinstance(checkpoint,dict) or checkpoint.get('format')!=CHECKPOINT_FORMAT:return None
            transition=checkpoint.get('ledger_append')
            if (not isinstance(transition,dict) or transition.get('format')!=progress.APPEND_FORMAT
                    or transition.get('prior_source')!=committed
                    or transition.get('source')!=source['usage_revision']
                    or self.task_revisions.get(key)!=(transition.get('prior_request_digest',transition.get('prior_body_digest')),policy)):
                return None
            owner=view.owner(*key)
            if owner is None or progress.source_stamp(owner['dependencies'])!=source['usage_revision']:
                return None
            count=transition.get('count');prior=transition.get('prior_count')
            selected=transition.get('request_keys',())
            if (type(count) is not int or not 0<count<=128 or type(prior) is not int or prior<1
                    or len(selected)>128 or owner['dependencies'].get('call_count')!=prior+count):
                return None
            rows=[];boundaries={};size=0;after=None
            while True:
                page=view.calls(key,ordinal_start=prior+1,after=after)
                for row in page.rows:
                    if len(rows)>=count or row['session_ordinal']!=prior+len(rows)+1:return None
                    size+=_weight(row,remaining-size)+16
                    if size>remaining:raise CapacityExceeded('prepared ledger tail exceeds its fact budget')
                    rows.append(row)
                if page.complete:break
                after=page.after
            if len(rows)!=count:return None
            if selected:
                after=None
                while True:
                    page=view.requests(key,selected,after=after)
                    for item in page.rows:
                        size+=_weight((item['key'],item['metadata']),remaining-size)+96
                        if size>remaining:raise CapacityExceeded('prepared ledger tail requests exceed their fact budget')
                        boundaries[item['key']]=item['metadata']
                    if page.complete:break
                    after=page.after
        self.metrics['owner_reads']+=1;self.metrics['call_rows']+=len(rows);self.metrics['request_rows']+=len(boundaries)
        return dict(history=rows,turn_records=boundaries),size,dict(transition,policy=policy)

    def sync(self, envelope, *, removed=()):
        """Yield after bounded commits; caller may ACK only after completion.

        Source certificates and calls commit together. A crash before a later
        owner batch leaves those owners uncommitted and safely replayable.
        """
        sources,certificates=self._sources()
        token=self._source_token
        self.task_revisions={key:token for key,token in self.task_revisions.items() if key in sources}
        committed=progress.applied(self.ledger.db,sources)
        call_waiting={key for key in sources if committed.get(key)!=certificates[key]}
        policies={home:self._task_policy(home) for home in {key[0] for key in sources}}
        task_tokens={key:(source['_request_source_digest'],policies[key[0]][1]) for key,source in sources.items()}
        task_waiting={key for key in sources if policies[key[0]][0] and self.task_revisions.get(key)!=task_tokens[key]}
        waiting=[key for key in sources if key in call_waiting or key in task_waiting]
        # A light uncommitted owner must not populate sync_activity's revision
        # cache with empty request maps before its actual request records arrive.
        for key,source in sources.items():
            if key not in task_waiting:
                source['request_revision']=task_tokens[key]
            else:
                source.pop('request_revision',None)
        retired=tuple(removed)
        index=0;allow_append=True
        while index<len(waiting):
            batch_start=index;states={};requests={};append_sources={};size=0
            while index<len(waiting) and len(requests)<OWNER_BATCH:
                key=waiting[index]
                try:
                    appended=(self._append(key,sources[key],committed.get(key),policies[key[0]][1],FACT_BYTES-size,
                                          expected=token)
                              if allow_append and key in call_waiting else None)
                    if appended is None:
                        prepared,weight=self._owner(key,FACT_BYTES-size,calls=key in call_waiting,
                                                    requests=key in call_waiting or key in task_waiting,expected=token)
                    else:
                        prepared,weight,append_sources[key]=appended
                except CapacityExceeded:
                    if requests:break
                    raise
                size+=weight;index+=1
                if key in call_waiting:states[key]=dict(prepared=prepared,revision=certificates[key])
                requests[key]=prepared['turn_records']
            selected={key:certificates[key] for key in states}
            sessions=[dict(source,turn_records=requests[key],request_revision=task_tokens[key])
                      if key in requests else source for key,source in sources.items()]
            snapshot=dict(envelope,sessions=sessions)
            self._check_plan(token)
            if retired:
                progress.retire(self.ledger.db,retired)
            try:
                self.ledger.sync(SimpleNamespace(sessions=states,internal_sessions={}),snapshot,
                                 applied_sources=selected,append_sources=append_sources)
            except progress.AppendPredecessorChanged:
                # The quota guard runs before activity/call mutations. Retry
                # this invocation through its original full-owner path once.
                self.ledger.db.rollback()
                index=batch_start;allow_append=False
                self.metrics['append_fallbacks']=self.metrics.get('append_fallbacks',0)+len(append_sources)
                continue
            committed_retired=retired;retired=()
            self.metrics['append_owners']=self.metrics.get('append_owners',0)+len(append_sources)
            saved=progress.applied(self.ledger.db,selected)
            if any(saved.get(key)!=stamp for key,stamp in selected.items()):
                raise ValueError('unconfirmed or older recovery owners were not applied to the ledger')
            for key in requests:
                sources[key]['request_revision']=task_tokens[key]
                if not envelope.get('collection_recovery') and not sources[key].get('_collection_pending'):
                    self.task_revisions[key]=task_tokens[key]
            self.metrics['applied_owners']+=len(states)
            yield dict(owners=tuple(states),task_owners=tuple(key for key in requests if key in task_waiting),
                       remaining=len(waiting)-index,retired=committed_retired)
        if not waiting:
            # Freshness, coverage and activity still commit on a heartbeat.
            # There are no historical call reads in this path.
            snapshot=dict(envelope,sessions=list(sources.values()))
            self._check_plan(token)
            if retired:progress.retire(self.ledger.db,retired)
            self.ledger.sync(SimpleNamespace(sessions={},internal_sessions={}),snapshot,applied_sources={})
            yield dict(owners=(),remaining=0,retired=retired)
