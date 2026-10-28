"""Apply only uncommitted prepared owners, with durable source/activity progress."""
from types import SimpleNamespace
from copy import deepcopy
from contextlib import closing
from typing import cast
import sys

from . import quota_prepared_sync as progress
from .prepared_ingest import _groups, _weight, pages
from .prepared_store import CapacityExceeded, NotReady, StaleGeneration, ReadCancelled, Page, pack, unpack


METADATA_BYTES = 64 * 1024 * 1024
FACT_BYTES = 256 * 1024 * 1024
OWNER_BATCH = 16


class _SourceCacheAdmission(CapacityExceeded):
    """Optional retained templates cannot share the invocation's metadata cap."""


class _SourcePopulationAdmission(CapacityExceeded):
    pass


class PreparedLedger:
    def __init__(self, ledger, store, *, cancel=None):
        self.ledger, self.store = ledger, store
        self.cancel = cancel
        progress.initialize(ledger.db)
        self.metrics = dict(owner_reads=0,call_rows=0,request_rows=0,applied_owners=0)
        # Call certificates do not certify tracking applicability. Request maps
        # are replayed once per process and again when tracking policy changes.
        self._initialize_plan()
        self._source_cache={}
        self._cache_token=None
        self._cache_order=None
        self._source_token=None
        self._source_cache_bytes=sys.getsizeof(self._source_cache)

    def _initialize_plan(self):
        db = self.ledger.db
        db.execute(f'PRAGMA temp.cache_size=-{self.ledger.CACHE_KIB}')
        db.executescript('''
            DROP TABLE IF EXISTS temp.prepared_ledger_sources;
            DROP TABLE IF EXISTS temp.prepared_ledger_homes;
            DROP TABLE IF EXISTS temp.prepared_ledger_coverage;
            DROP TABLE IF EXISTS temp.prepared_ledger_retired;
            DROP TABLE IF EXISTS temp.prepared_ledger_requests;
            CREATE TEMP TABLE prepared_ledger_sources(
                ordinal INTEGER PRIMARY KEY,home TEXT NOT NULL,sid TEXT NOT NULL,
                source BLOB NOT NULL,weight INTEGER NOT NULL,certificate TEXT NOT NULL,
                request_digest TEXT NOT NULL,pending INTEGER NOT NULL,
                call_waiting INTEGER NOT NULL,task_waiting INTEGER NOT NULL,UNIQUE(home,sid));
            CREATE INDEX temp.prepared_ledger_waiting
                ON prepared_ledger_sources((call_waiting OR task_waiting),ordinal);
            CREATE TEMP TABLE prepared_ledger_homes(
                home TEXT PRIMARY KEY,policy BLOB NOT NULL,active INTEGER NOT NULL,scope INTEGER NOT NULL,
                unclassified INTEGER NOT NULL DEFAULT 0,pending_count INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX temp.prepared_ledger_scope ON prepared_ledger_homes(scope,home);
            CREATE TEMP TABLE prepared_ledger_coverage(
                home TEXT NOT NULL,sid TEXT NOT NULL,start REAL,end REAL,reason TEXT NOT NULL,
                PRIMARY KEY(home,sid,reason));
            CREATE TEMP TABLE prepared_ledger_retired(home TEXT NOT NULL,sid TEXT NOT NULL,PRIMARY KEY(home,sid));
            CREATE TEMP TABLE prepared_ledger_requests(
                home TEXT NOT NULL,sid TEXT NOT NULL,request_digest TEXT NOT NULL,policy BLOB NOT NULL,
                PRIMARY KEY(home,sid));
        ''')

    def _request_token(self, key):
        row = self.ledger.db.execute('SELECT request_digest,policy FROM temp.prepared_ledger_requests '
                                     'WHERE home=? AND sid=?', key).fetchone()
        return (row[0], unpack(row[1], maximum=METADATA_BYTES)) if row else None

    def _clear_plan(self):
        db = self.ledger.db
        for table in ('sources', 'homes', 'coverage', 'retired'):
            while True:
                if self.cancel is not None and self.cancel():
                    raise ReadCancelled('prepared ledger synchronization cancelled')
                rows = db.execute(f'SELECT rowid FROM temp.prepared_ledger_{table} LIMIT ?', (OWNER_BATCH,)).fetchall()
                if not rows:
                    break
                db.executemany(f'DELETE FROM temp.prepared_ledger_{table} WHERE rowid=?',
                               ((row[0],) for row in rows))
                db.commit()

    def _stage_plan(self, envelope, removed, owners=None):
        db = self.ledger.db
        if db.in_transaction:
            raise RuntimeError('prepare the source plan before starting quota application')
        self._clear_plan()
        ordinal = 0
        token = None

        def home_policy(home, scope=0):
            row = db.execute('SELECT policy,active,scope FROM temp.prepared_ledger_homes WHERE home=?', (home,)).fetchone()
            if row is None:
                active, policy = self._task_policy(home)
                encoded = pack(policy, maximum=METADATA_BYTES)
                db.execute('INSERT INTO temp.prepared_ledger_homes(home,policy,active,scope) VALUES(?,?,?,?)',
                           (home, encoded, int(active), scope))
                return encoded, active
            if scope and not row[2]:
                db.execute('UPDATE temp.prepared_ledger_homes SET scope=1 WHERE home=?', (home,))
            return row[0], row[1]

        for home in envelope.get('homes', ()):
            home_policy(home, 1)
        db.commit()

        def stage_source(key, source, certificate):
            nonlocal ordinal
            self._check_plan(token)
            policy, active = home_policy(key[0])
            pending = bool(source.get('_collection_pending'))
            committed = progress.applied(db, (key,)).get(key)
            request_digest = source['_request_source_digest']
            previous = db.execute('SELECT request_digest,policy FROM temp.prepared_ledger_requests '
                                  'WHERE home=? AND sid=?', key).fetchone()
            calls = not pending and committed != certificate
            tasks = not pending and active and (previous is None or tuple(previous) != (request_digest, policy))
            packet = pack(source, maximum=METADATA_BYTES)
            weight = _weight(source, METADATA_BYTES)+len(packet)
            if weight > METADATA_BYTES:
                raise CapacityExceeded('prepared ledger source exceeds its metadata budget')
            db.execute('INSERT INTO temp.prepared_ledger_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (ordinal, *key, packet, weight, certificate, request_digest, int(pending), int(calls), int(tasks)))
            self.ledger.stage_prepared_coverage(source)
            ordinal += 1
            if ordinal % OWNER_BATCH == 0:
                db.commit()

        cached = None
        try:
            if owners is None:
                cached = self._sources()
        except _SourcePopulationAdmission:
            pass
        if owners is not None:
            with self.store.read_current(cancel=self.cancel,seconds=60) as view:
                self._require_view(view)
                token = view.token
                for group in _groups(owners,OWNER_BATCH):
                    headers = list(view.pages(view.owner_revisions,group))
                    if len(headers) != len(group):
                        raise NotReady('selected ledger plan references an absent owner')
                    with closing(self._source_entries(view,headers,self._source_cache)) as entries:
                        for header,entry in entries:
                            key = header['owner']
                            previous = self._source_cache.get(key)
                            if previous is not None and previous[0]==header['revision']:
                                source = previous[1]
                            else:
                                _,source,_,_ = self._source_slot(key,header['revision'],entry)
                                self.metrics['metadata_reads'] = self.metrics.get('metadata_reads',0)+1
                            stage_source(key,source,source['usage_revision'])
        elif cached is not None:
            sources, certificates = cached
            del cached
            token = self._source_token
            for key, source in sources.items():
                stage_source(key, source, certificates[key])
            del sources, certificates
        else:
            with self.store.read_current(cancel=self.cancel, seconds=60) as view:
                self._require_view(view)
                token = view.token
                for header in view.pages(view.owner_revisions):
                    key = header['owner']
                    entry = view.ledger_source(*key)
                    if entry is None:
                        raise NotReady('prepared ledger inventory references an absent owner')
                    _, source, _, _ = self._source_slot(key, header['revision'], entry)
                    self.metrics['metadata_reads'] = self.metrics.get('metadata_reads', 0)+1
                    stage_source(key, source, source['usage_revision'])
                    del source, entry
        self._check_plan(token)
        if owners is None:
            self._source_token = token
        for number, owner in enumerate(removed, 1):
            db.execute('INSERT OR IGNORE INTO temp.prepared_ledger_retired VALUES(?,?)', owner)
            if number % OWNER_BATCH == 0:
                self._check_plan(token)
                db.commit()
        db.commit()
        if owners is not None:
            return token
        after = None
        while True:
            self._check_plan(token)
            where = '' if after is None else 'WHERE (home,sid)>(?,?) '
            rows = db.execute('SELECT home,sid FROM temp.prepared_ledger_requests '+where+
                'ORDER BY home,sid LIMIT ?', (*(() if after is None else after), OWNER_BATCH)).fetchall()
            if not rows:
                break
            for row in rows:
                if db.execute('SELECT 1 FROM temp.prepared_ledger_sources WHERE home=? AND sid=?', tuple(row)).fetchone() is None:
                    db.execute('DELETE FROM temp.prepared_ledger_requests WHERE home=? AND sid=?', tuple(row))
            db.commit()
            after = tuple(rows[-1])
        return token

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
                    raise _SourcePopulationAdmission('prepared ledger source page exceeds its metadata budget')
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
                        raise _SourcePopulationAdmission('prepared ledger sources exceed their metadata budget')
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
                for row in view.pages(view.calls,key):
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
                    or self._request_token(key)!=(transition.get('prior_request_digest',transition.get('prior_body_digest')),policy)):
                return None
            owner=view.owner(*key)
            if owner is None or progress.source_stamp(owner['dependencies'])!=source['usage_revision']:
                return None
            count=transition.get('count');prior=transition.get('prior_count')
            selected=transition.get('request_keys',())
            if (type(count) is not int or not 0<count<=128 or type(prior) is not int or prior<1
                    or len(selected)>128 or owner['dependencies'].get('call_count')!=prior+count):
                return None
            rows=[];boundaries={};size=0
            for row in view.pages(view.calls,key,ordinal_start=prior+1):
                if len(rows)>=count or row['session_ordinal']!=prior+len(rows)+1:return None
                size+=_weight(row,remaining-size)+16
                if size>remaining:raise CapacityExceeded('prepared ledger tail exceeds its fact budget')
                rows.append(row)
            if len(rows)!=count:return None
            if selected:
                for item in view.pages(view.requests,key,selected):
                    size+=_weight((item['key'],item['metadata']),remaining-size)+96
                    if size>remaining:raise CapacityExceeded('prepared ledger tail requests exceed their fact budget')
                    boundaries[item['key']]=item['metadata']
        self.metrics['owner_reads']+=1;self.metrics['call_rows']+=len(rows);self.metrics['request_rows']+=len(boundaries)
        return dict(history=rows,turn_records=boundaries),size,dict(transition,policy=policy)

    def sync(self, envelope, *, removed=(), owners=None):
        """Yield durable batches with a boolean signal for remaining work."""
        db = self.ledger.db
        if owners is not None:
            if removed:
                raise ValueError('a selected ledger plan cannot retire owners')
            envelope = dict(envelope,collection_partial=True)
        token = self._stage_plan(envelope, removed, owners)
        while True:
            self._check_plan(token)
            rows = db.execute('SELECT home,sid FROM temp.prepared_ledger_retired '
                              'ORDER BY home,sid LIMIT ?', (OWNER_BATCH,)).fetchall()
            if not rows:
                break
            with db:
                db.execute('BEGIN IMMEDIATE')
                self._check_plan(token)
                retired = tuple(tuple(row) for row in rows
                    if not self.ledger._recovered_home(row[0], envelope))
                progress.retire(db, retired)
                db.executemany('DELETE FROM temp.prepared_ledger_retired WHERE home=? AND sid=?',
                               (tuple(row) for row in rows))
                self._check_plan(token)
            if retired:
                yield dict(owners=(), remaining=True, retired=retired)

        after = -1
        allow_append = True
        activity_done = False
        while True:
            self._check_plan(token)
            headers = db.execute('SELECT ordinal,home,sid,weight,certificate,request_digest,call_waiting,task_waiting '
                'FROM temp.prepared_ledger_sources WHERE (call_waiting OR task_waiting)=1 AND ordinal>? '
                'ORDER BY ordinal LIMIT ?', (after, OWNER_BATCH)).fetchall()
            if not headers:
                break
            states = {}
            requests = {}
            append_sources = {}
            selected = {}
            sessions = []
            task_tokens = {}
            task_policies = {}
            size = 0
            metadata_size = 0
            end = after
            committed = progress.applied(db, ((row['home'], row['sid']) for row in headers))
            for header in headers:
                if requests and metadata_size+header['weight'] > METADATA_BYTES:
                    break
                key = (header['home'], header['sid'])
                packet = db.execute('SELECT source FROM temp.prepared_ledger_sources WHERE ordinal=?',
                                    (header['ordinal'],)).fetchone()[0]
                source = cast(dict, unpack(packet, maximum=METADATA_BYTES))
                del packet
                home = db.execute('SELECT policy,scope FROM temp.prepared_ledger_homes WHERE home=?', (key[0],)).fetchone()
                policy = unpack(home['policy'], maximum=METADATA_BYTES)
                try:
                    appended = (self._append(key, source, committed.get(key), policy, FACT_BYTES-size, expected=token)
                                if allow_append and header['call_waiting'] else None)
                    if appended is None:
                        prepared, weight = self._owner(key, FACT_BYTES-size, calls=bool(header['call_waiting']),
                                                       expected=token)
                    else:
                        prepared, weight, append_sources[key] = appended
                    del appended
                except CapacityExceeded:
                    if requests:
                        del source
                        break
                    raise
                size += weight
                metadata_size += header['weight']
                end = header['ordinal']
                if header['call_waiting']:
                    states[key] = dict(prepared=prepared, revision=header['certificate'])
                    selected[key] = header['certificate']
                requests[key] = prepared['turn_records']
                task_tokens[key] = (header['request_digest'], policy)
                task_policies[key] = home['policy']
                sessions.append(dict(source, turn_records=prepared['turn_records'], request_revision=task_tokens[key]))
                del source, prepared
            snapshot = dict(envelope, sessions=sessions, collection_partial=True)
            if activity_done:
                snapshot['homes'] = [home for home in dict.fromkeys(key[0] for key in requests)
                    if db.execute('SELECT scope FROM temp.prepared_ledger_homes WHERE home=?', (home,)).fetchone()[0]]
                snapshot['request_activity'] = []
                snapshot.pop('activity_cursor', None)
            self._check_plan(token)
            try:
                self.ledger.sync(SimpleNamespace(sessions=states, internal_sessions={}), snapshot,
                                 applied_sources=selected, append_sources=append_sources, prepared_plan=True)
            except progress.AppendPredecessorChanged:
                db.rollback()
                allow_append = False
                self.metrics['append_fallbacks'] = self.metrics.get('append_fallbacks', 0)+len(append_sources)
                continue
            saved = progress.applied(db, selected)
            if any(saved.get(key) != stamp for key, stamp in selected.items()):
                raise ValueError('unconfirmed or older recovery owners were not applied to the ledger')
            if not envelope.get('collection_recovery'):
                db.executemany('INSERT INTO temp.prepared_ledger_requests VALUES(?,?,?,?) '
                    'ON CONFLICT(home,sid) DO UPDATE SET request_digest=excluded.request_digest,policy=excluded.policy',
                    ((*key, task_tokens[key][0], task_policies[key]) for key in requests))
                db.commit()
            activity_done = True
            after = end
            self.metrics['applied_owners'] += len(states)
            self.metrics['append_owners'] = self.metrics.get('append_owners', 0)+len(append_sources)
            yield dict(owners=tuple(states), task_owners=tuple(
                (row['home'], row['sid']) for row in headers
                if row['ordinal']<=end and row['task_waiting']), remaining=True, retired=())
            del states, requests, sessions, append_sources, selected, snapshot
        if not activity_done:
            self._check_plan(token)
            self.ledger.sync(SimpleNamespace(sessions={}, internal_sessions={}),
                             dict(envelope, sessions=[], collection_partial=True), applied_sources={}, prepared_plan=True)
            yield dict(owners=(), remaining=True, retired=())
        for _ in self.ledger.reconcile_prepared_coverage(envelope, check=lambda: self._check_plan(token),
                                                       metadata_bytes=METADATA_BYTES):
            yield dict(owners=(), remaining=True, retired=())
        self._clear_plan()
        yield dict(owners=(), remaining=False, retired=())
