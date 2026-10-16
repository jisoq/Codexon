"""Apply only uncommitted prepared owners, with durable source/activity progress."""
from types import SimpleNamespace

from . import quota_prepared_sync as progress
from .prepared_ingest import _weight, pages
from .prepared_store import CapacityExceeded


METADATA_BYTES = 64 * 1024 * 1024
FACT_BYTES = 256 * 1024 * 1024
OWNER_BATCH = 16


class PreparedLedger:
    def __init__(self, ledger, store):
        self.ledger, self.store = ledger, store
        progress.initialize(ledger.db)
        self.metrics = dict(owner_reads=0,call_rows=0,request_rows=0,applied_owners=0)
        # Call certificates do not certify tracking applicability. Request maps
        # are replayed once per process and again when tracking policy changes.
        self.task_revisions={}

    def _sources(self):
        sources={};certificates={};size=0
        with self.store.read_current(seconds=60) as view:
            for entry in pages(view.catalog):
                key=entry['owner']
                source=dict(entry['metadata'])
                source.setdefault('home',key[0]);source.setdefault('id',key[1])
                bounds=entry['dependencies'].get('coverage_bounds')
                if not isinstance(bounds,tuple) or len(bounds)!=2:
                    raise ValueError('prepared ledger source lacks coverage bounds')
                # Summaries are not loaded. Coverage uses original source
                # bounds, which may differ from deduplicated canonical calls.
                source['_coverage_bounds']=bounds
                source['usage_revision']=progress.source_stamp(entry['dependencies'])
                source['_request_source_digest']=entry['dependencies']['source_digest']
                certificates[key]=source['usage_revision']
                size+=_weight(source,METADATA_BYTES-size)+96
                if size>METADATA_BYTES:
                    raise CapacityExceeded('prepared ledger metadata budget exceeded')
                sources[key]=source
        return sources,certificates

    def _owner(self, key, remaining, *, calls=True, requests=True):
        rows=[];boundaries={};size=0
        with self.store.read_current(seconds=60) as view:
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

    def _append(self,key,source,committed,policy,remaining):
        """Read a bounded tail only after both call and task predecessors match."""
        from .prepared_append import FORMAT as CHECKPOINT_FORMAT
        with self.store.read_current(seconds=60) as view:
            checkpoint=view.derived(*key,'checkpoint')
            if not isinstance(checkpoint,dict) or checkpoint.get('format')!=CHECKPOINT_FORMAT:return None
            transition=checkpoint.get('ledger_append')
            if (not isinstance(transition,dict) or transition.get('format')!=progress.APPEND_FORMAT
                    or transition.get('prior_source')!=committed
                    or transition.get('source')!=source['usage_revision']
                    or self.task_revisions.get(key)!=(transition.get('prior_body_digest'),policy)):
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
                    appended=(self._append(key,sources[key],committed.get(key),policies[key[0]][1],FACT_BYTES-size)
                              if allow_append and key in call_waiting else None)
                    if appended is None:
                        prepared,weight=self._owner(key,FACT_BYTES-size,calls=key in call_waiting,
                                                    requests=key in call_waiting or key in task_waiting)
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
            retired=()
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
                       remaining=len(waiting)-index)
        if not waiting:
            # Freshness, coverage and activity still commit on a heartbeat.
            # There are no historical call reads in this path.
            snapshot=dict(envelope,sessions=list(sources.values()))
            if retired:progress.retire(self.ledger.db,retired)
            self.ledger.sync(SimpleNamespace(sessions={},internal_sessions={}),snapshot,applied_sources={})
            yield dict(owners=(),remaining=0)
        self.task_revisions={key:token for key,token in self.task_revisions.items() if key in sources}
