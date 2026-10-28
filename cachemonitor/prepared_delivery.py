"""Publish prepared owner deltas without rebuilding or retaining call history."""
from collections import Counter
import sys
import uuid

from .analysis_delivery import MODEL_KEYS
from .analytics import effort_key, project_choices
from .prepared_ingest import _weight, pages
from .prepared_store import UNCHANGED, CapacityExceeded, NotReady, pack, unpack


class _WeightMemo:
    """Reuse subtree arithmetic for one admission, still charging every alias."""
    __slots__=('entries','limit','peak_bytes','peak_frames')
    MAX_ENTRIES=8192
    MAX_BYTES=4*1024*1024
    ENTRY_BYTES=512
    FRAME_BYTES=768
    BASE_BYTES=1024
    MIN_TREE_BYTES=4096

    def __init__(self,limit):
        self.entries={};self.limit=limit
        self.peak_bytes=self.BASE_BYTES;self.peak_frames=0

    def _reserve(self,depth,*,entry=False):
        size=self.BASE_BYTES+(len(self.entries)+int(entry))*self.ENTRY_BYTES+depth*self.FRAME_BYTES
        if size>self.limit:
            if entry:return False
            self.entries.clear()
            size=self.BASE_BYTES+depth*self.FRAME_BYTES
            if size>self.limit:
                raise CapacityExceeded('publication weight traversal exceeds its scratch budget')
        self.peak_bytes=max(self.peak_bytes,size)
        self.peak_frames=max(self.peak_frames,depth)
        return True

    def tree(self,value,remaining):
        stack=[(None,iter((value,)),0,None)];active=set();total=0
        getsizeof=sys.getsizeof
        self._reserve(1)
        try:
            while stack:
                parent,children,start,identity=stack[-1]
                for item in children:
                    if isinstance(item,(dict,list,tuple)):
                        child_id=id(item);cached=self.entries.get(child_id)
                        if cached is not None and cached[0] is item:
                            total+=cached[1]
                        else:
                            if child_id in active:
                                raise CapacityExceeded('publication weight contains a cyclic container')
                            child_start=total;total+=getsizeof(item)
                            if total>remaining:
                                raise CapacityExceeded('selected source exceeds its transient preparation budget')
                            self._reserve(len(stack)+1)
                            active.add(child_id)
                            child_values=(iter(part for pair in item.items() for part in pair)
                                          if isinstance(item,dict) else iter(item))
                            stack.append((item,child_values,child_start,child_id))
                            break
                    else:
                        total+=getsizeof(item)
                    if total>remaining:
                        raise CapacityExceeded('selected source exceeds its transient preparation budget')
                else:
                    # A cached size is complete; a failed partial walk never
                    # leaves an entry. Small trees cost less to walk than memo.
                    size=total-start
                    if parent is not None and size>=self.MIN_TREE_BYTES:
                        self.remember(parent,size,depth=len(stack))
                    stack.pop()
                    if identity is not None:active.remove(identity)
            return total
        finally:
            active.clear();stack.clear()

    def remember(self, value, size, *, depth=0):
        # An unchanged retained root already has an exact conservative charge.
        # New views can share its body without walking it again. This memo is
        # still scoped to one admission and never retains a mutable generation.
        if (not isinstance(value,(dict,list,tuple)) or size<self.MIN_TREE_BYTES or
                len(self.entries)>=self.MAX_ENTRIES): return
        identity=id(value)
        if identity in self.entries:return
        if self._reserve(depth,entry=True):
            self.entries[identity] = (value,size)

    def scratch_bytes(self):
        # Verify actual memo allocation once, not on every insertion. Fixed
        # frame reservations cover iterators, stack capacity and active ids.
        actual=sys.getsizeof(self)+sys.getsizeof(self.entries)
        actual+=sum(sys.getsizeof(key)+sys.getsizeof(value)+sys.getsizeof(value[1])
                    for key,value in self.entries.items())
        actual+=self.peak_frames*self.FRAME_BYTES
        if actual>self.limit:
            raise CapacityExceeded('publication weight memo exceeds its scratch budget')
        return max(actual,self.peak_bytes)

    def close(self):
        self.entries.clear()


def _unique_retained(values, limit, *, extra=0):
    """Count live objects once, including the bounded traversal workspace."""
    seen = set()
    stack = [iter(values)]
    total = extra
    while stack:
        value = next(stack[-1], stack)
        if value is stack:
            stack.pop()
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(value)
        if isinstance(value, dict):
            stack.append(iter(part for pair in value.items() for part in pair))
        elif isinstance(value, (list, tuple, set, frozenset)):
            stack.append(iter(value))
        scratch = sys.getsizeof(seen) + len(seen)*sys.getsizeof(identity) + len(stack)*768
        if total + scratch > limit:
            raise CapacityExceeded('prepared publication exceeds its retention budget')
    return total


class _Retention:
    """Cache conservative tree weights only while their root values survive.

    Roots and cached values are replaced, never edited. Cached tree weights
    provide a fast upper bound. Crossing that bound requires an identity-aware
    check of both generations and traversal workspace before rejection.
    """
    def __init__(self):
        self.entries = {}
        self.containers = {}
        self.roots = {}
        self.identity_mode = False
        self.retained_bytes = 0
        self.rescanned = 0

    def stage(self, roots, limit, *, extra=0):
        # Tree weights are a cheap upper bound, not evidence that shared
        # objects exceed memory. Resolve identities before rejecting a cohort.
        if not self.identity_mode:
            try:
                return self._stage_trees(roots, limit, extra=extra)
            except CapacityExceeded:
                pass
        candidate = type(self)()
        candidate.roots = roots
        candidate.identity_mode = True
        candidate.retained_bytes = _unique_retained(
            (roots, candidate.__dict__, self.__dict__), limit, extra=extra)
        return candidate

    def _stage_trees(self, roots, limit, *, extra=0):
        candidate = type(self)()
        candidate.roots = roots
        total = extra
        replaced = 0
        scratch_limit=min(_WeightMemo.MAX_BYTES,limit//32)
        memo=(_WeightMemo(scratch_limit) if scratch_limit>=_WeightMemo.BASE_BYTES+_WeightMemo.FRAME_BYTES else None)
        weigh=_weight if memo is None else memo.tree
        try:
            for name, root in roots.items():
                old = self.entries.get(name, {})
                cached = {}
                container = sys.getsizeof(root)
                total += container
                if self.containers.get(name) != (id(root), container):
                    replaced += self.containers.get(name, (None, 0))[1]
                for key, value in root.items():
                    prior = old.get(key)
                    if prior is not None and prior[0] is value:
                        item = prior
                        if memo is not None:
                            memo.remember(value, prior[1]-weigh(key,limit))
                    else:
                        weight = weigh(key, limit) + weigh(value, limit)
                        item = (value, weight)
                        candidate.rescanned += 1
                        if prior is not None:
                            replaced += prior[1]
                    cached[key] = item
                    total += item[1]
                replaced += sum(item[1] for key, item in old.items() if key not in root)
                candidate.entries[name] = cached
                candidate.containers[name] = (id(root), container)
            # Payload aliases keep their full conservative charge. The local
            # arithmetic memo and old cache are transient admission costs too.
            total += self._metadata_bytes(candidate)
            scratch=0 if memo is None else memo.scratch_bytes()
            if total + replaced + self._metadata_bytes(self) + scratch > limit:
                raise CapacityExceeded('prepared publication exceeds its retention budget')
            candidate.retained_bytes = total
            return candidate
        finally:
            if memo is not None:memo.close()

    @staticmethod
    def _metadata_bytes(cache):
        total = (sys.getsizeof(cache) + sys.getsizeof(cache.__dict__) +
                 sys.getsizeof(cache.entries) + sys.getsizeof(cache.containers) +
                 sys.getsizeof(cache.roots))
        for name, entries in cache.entries.items():
            total += sys.getsizeof(name) + sys.getsizeof(entries)
            total += sum(sys.getsizeof(item) + sys.getsizeof(item[1]) for item in entries.values())
        for item in cache.containers.values():
            total += sys.getsizeof(item) + sum(sys.getsizeof(part) for part in item)
        return total


class PreparedPublisher:
    # Includes owner metadata, exact small choice/alert packets and compact
    # summaries. Historical calls and health section bodies have no cache here.
    STATE_BYTES=256*1024*1024

    def __init__(self,cache_floor):
        from .prepared_overlays import PreparedOverlays
        self.overlay=PreparedOverlays(compact=True)
        self.owners={};self.choices={};self.alerts={}
        self.owner_weights={};self.owner_bytes=0
        self.sessions={};self.overlays={}
        self.pending_alerts={};self.changed=set()
        self.token=None
        self.order_identity=None
        self.cache_floor=cache_floor
        self._retention=_Retention()
        self.scoped=False;self.global_choices={};self.global_models=[];self._choices_dirty=True
        self._projection_dirty=True
        self._alert_store=None;self._alert_work=None
        self.reset()

    def _admit(self, *, owners=None, choices=None, alerts=None, weights=None,
               pending_alerts=None, sessions=None, overlays=None, overlay=None,
               changed=None, global_choices=None, global_models=None, extra=0):
        overlay = self.overlay if overlay is None else overlay
        roots = dict(owners=self.owners if owners is None else owners,
            choices=self.choices if choices is None else choices,
            alerts=self.alerts if alerts is None else alerts,
            weights=self.owner_weights if weights is None else weights,
            global_result=dict(choices=self.global_choices if global_choices is None else global_choices,
                               models=self.global_models if global_models is None else global_models),
            pending_alerts=self.pending_alerts if pending_alerts is None else pending_alerts,
            sessions=self.sessions if sessions is None else sessions,
            overlays=self.overlays if overlays is None else overlays,
            own=overlay.own, partials=overlay.partials, revisions=overlay.revisions,
            detail_revisions=overlay.detail_revisions, summaries=overlay.summaries,
            presenter_cache=overlay.presenter.cache, presenter_partials=overlay.presenter.partials,
            presenter_rollups=overlay.presenter.rollups, presenter_presented=overlay.presenter.presented)
        changed = self.changed if changed is None else changed
        # Fixed slack covers the publisher/presenter objects and scalar fields;
        # the only non-map variable retained container is the changed-owner set.
        extra += 4096 + sys.getsizeof(changed) + sum(_weight(key, self.STATE_BYTES) for key in changed)
        return self._retention.stage(roots, self.STATE_BYTES, extra=extra)

    def reset(self):
        if self._alert_work is None:
            pending_alerts={
                (key,identity):dict(home=key[0],sid=key[1],title=self.owners[key]['metadata'].get('title',''),
                                    **dict(zip(MODEL_KEYS,signature)))
                for key,alerts in self.alerts.items() if not self.owners[key]['metadata'].get('_internal_review')
                for identity,signature in alerts.items()}
        else:
            pending_alerts={}
            rows=self._alert_store.db.execute('SELECT a.home,a.sid,a.identity,a.signature,o.title '
                'FROM delivery_alerts a JOIN delivery_alert_owners o '
                'ON o.work=a.work AND o.home=a.home AND o.sid=a.sid WHERE a.work=? AND o.internal=0 '
                'ORDER BY a.home,a.sid,a.identity',(self._alert_work,))
            for row in rows:
                key=row['home'],row['sid']
                signature=unpack(row['signature'],self._alert_store.limits)
                pending_alerts[(key,row['identity'])]=dict(home=key[0],sid=key[1],title=row['title'],
                    **dict(zip(MODEL_KEYS,signature)))
        changed=set(self.owners)
        sessions={};overlays={}
        retention=self._admit(pending_alerts=pending_alerts,sessions=sessions,overlays=overlays,changed=changed)
        self.epoch=uuid.uuid4().hex
        self.sequence=0
        self.sent_token=None;self._projection_dirty=True
        self.sessions=sessions;self.overlays=overlays
        self.pending_alerts=pending_alerts;self.changed=changed
        self._retention=retention

    def adopt(self,store,changed,removed=(),*,selected=UNCHANGED):
        if selected is UNCHANGED:
            selected=self.owners if self.scoped else None
        scoped=selected is not None
        same_scope=(scoped==self.scoped and (not scoped or len(selected)==len(self.owners)
                    and all(key in selected for key in self.owners)))
        if self.token==store.token and same_scope:
            return
        database=store.db
        database.execute('CREATE TEMP TABLE IF NOT EXISTS delivery_alerts(work TEXT,home TEXT,sid TEXT,'
            'identity TEXT,signature BLOB,PRIMARY KEY(work,home,sid,identity))')
        database.execute('CREATE TEMP TABLE IF NOT EXISTS delivery_alert_owners(work TEXT,home TEXT,sid TEXT,'
            'title TEXT,internal INTEGER,PRIMARY KEY(work,home,sid))')
        prior_work=self._alert_work
        next_work=prior_work
        try:
            with store.read_current(seconds=60) as view:
                if not view.db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0]:
                    raise NotReady('cannot deliver a partial owner conversion')
                order_identity=(view.token.instance,view.order_batch)
                if (self.token is not None and not changed and not removed and same_scope
                        and self.order_identity==order_identity):
                    self.token=view.token
                    return
                scan_alerts=self.token!=view.token or prior_work is None
                if scan_alerts:next_work=uuid.uuid4().hex
                had_alerts=bool(prior_work is not None and database.execute(
                    'SELECT 1 FROM delivery_alerts WHERE work=? LIMIT 1',(prior_work,)).fetchone())
                owners={};choices={};alerts={};weights={};owner_bytes=0
                pending_alerts=self.pending_alerts.copy();content_changed=set()
                overlay=self.overlay._fork()
                entries=(((tuple(key),view.owner(*key,publication=True)) for key in selected)
                         if not scan_alerts and scoped else
                         ((row['owner'],row) for row in view.pages(view.catalog,publication=True)))
                for key,row in entries:
                    if row is None:
                        raise NotReady('published selection references an absent prepared owner')
                    new_alerts=row['model_alerts'] or {}
                    metadata=row['metadata']
                    if scan_alerts:
                        if new_alerts:
                            database.execute('INSERT INTO delivery_alert_owners VALUES(?,?,?,?,?)',
                                (next_work,*key,metadata.get('title',''),int(bool(metadata.get('_internal_review')))))
                            database.executemany('INSERT INTO delivery_alerts VALUES(?,?,?,?,?)',
                                ((next_work,*key,identity,pack(signature,store.limits))
                                 for identity,signature in new_alerts.items()))
                        old_alerts=({item['identity']:unpack(item['signature'],store.limits) for item in
                            database.execute('SELECT identity,signature FROM delivery_alerts '
                                'WHERE work=? AND home=? AND sid=?',(prior_work,*key))}
                            if had_alerts else {})
                        if not metadata.get('_internal_review'):
                            for identity in old_alerts.keys()|new_alerts.keys():
                                signature=new_alerts.get(identity)
                                if old_alerts.get(identity)==signature:continue
                                if signature is None:
                                    record=view.record(*key,identity)
                                    if record is None:continue
                                    signature=tuple(record.get(name) for name in MODEL_KEYS)
                                pending_alerts[(key,identity)]=dict(home=key[0],sid=key[1],
                                    title=metadata.get('title',''),**dict(zip(MODEL_KEYS,signature)))
                    if scoped and key not in selected:continue
                    owner_choices=row['choices'] or {}
                    summary=row['summary']
                    entry={name:value for name,value in row.items()
                           if name not in ('owner','summary','choices','model_alerts')}
                    previous=self.owners.get(key)
                    equal=bool(previous is not None and
                        {name:value for name,value in previous.items() if name!='owner_ordinal'}==
                        {name:value for name,value in entry.items() if name!='owner_ordinal'})
                    if equal:
                        if previous['owner_ordinal']==entry['owner_ordinal']:entry=previous
                        owner_choices=self.choices[key];new_alerts=self.alerts[key]
                        weight=self.owner_weights[key]+sys.getsizeof(entry['owner_ordinal'])-sys.getsizeof(previous['owner_ordinal'])
                    else:
                        preserve_content=bool(previous is not None and key in overlay.own and
                            {name:value for name,value in previous['metadata'].items()
                             if name not in ('status','remaining','usage_revision','request_revision')}==
                            {name:value for name,value in metadata.items()
                             if name not in ('status','remaining','usage_revision','request_revision')} and
                            {name:value for name,value in previous['dependencies'].items()
                             if name not in ('state','state_facts')}==
                            {name:value for name,value in entry['dependencies'].items()
                             if name not in ('state','state_facts')} and
                            {name:value for name,value in previous.items()
                             if name not in ('owner_ordinal','metadata','dependencies')}==
                            {name:value for name,value in entry.items()
                             if name not in ('owner_ordinal','metadata','dependencies')})
                        weight=_weight((key,entry,owner_choices,new_alerts),self.STATE_BYTES)
                        content_changed.add(key)
                        if not metadata.get('_internal_review') and isinstance(summary,dict) and 'owner' in summary:
                            overlay.prime(view,key,summary,preserve_content=preserve_content)
                    owner_bytes+=weight
                    if owner_bytes>self.STATE_BYTES:
                        raise CapacityExceeded('prepared publication metadata exceeds its retention budget')
                    owners[key]=entry;choices[key]=owner_choices;alerts[key]=new_alerts;weights[key]=weight
                if scoped:
                    if len(owners)!=len(selected):raise NotReady('selected publication membership is incomplete')
                elif self.token is not None:
                    if (any(key not in self.owners and key not in changed for key in owners)
                            or any(key not in owners and key not in removed for key in self.owners)):
                        raise NotReady('catalog order does not match adopted publication owners')
                token=view.token
            changed=(self.changed & owners.keys())|content_changed
            retention=self._admit(owners=owners,choices=choices,alerts=alerts,weights=weights,overlay=overlay,
                                  pending_alerts=pending_alerts,changed=changed)
        except BaseException:
            if next_work!=prior_work:
                database.execute('DELETE FROM delivery_alerts WHERE work=?',(next_work,))
                database.execute('DELETE FROM delivery_alert_owners WHERE work=?',(next_work,))
            raise
        if prior_work is not None and prior_work!=next_work:
            database.execute('DELETE FROM delivery_alerts WHERE work=?',(prior_work,))
            database.execute('DELETE FROM delivery_alert_owners WHERE work=?',(prior_work,))
        self._choices_dirty=self._choices_dirty or self.token!=token
        self._alert_store=store;self._alert_work=next_work
        self.owners=owners;self.choices=choices;self.alerts=alerts
        self.owner_weights=weights;self.owner_bytes=owner_bytes
        self.pending_alerts=pending_alerts;self.changed=changed
        self._retention=retention
        self.token=token;self.order_identity=order_identity;self.scoped=scoped
        self.overlay=overlay;self._projection_dirty=True

    def _filter_choices(self,snapshot,sessions,view=None):
        counts=Counter();transports=set();transport_sources=set();policies=set()
        homes=set(snapshot.get('homes',()));sources=set();models=set();internal=0
        def metadata():
            nonlocal internal
            entries=((row['owner'],row['metadata'],row['choices'] or {},row['dependencies'])
                     for row in view.pages(view.catalog,publication=True)) if self.scoped else (
                     (key,row['metadata'],self.choices[key],row['dependencies']) for key,row in self.owners.items())
            for key,owner,choices,dependency in entries:
                if owner.get('_internal_review'):
                    internal+=dependency.get('call_count',0)
                    continue
                counts.update({(item['model'],item['effort'],item['service_tier']):item['count']
                               for item in choices.get('combinations',())})
                transports.update(choices.get('transports',()))
                transport_sources.update(choices.get('transport_sources',()))
                policies.update(choices.get('cache_policies',()))
                models.update(choices.get('models',()))
                homes.add(key[0]);sources.add(owner.get('source','unknown'))
                yield owner
        projects=project_choices(metadata())
        result=dict(models=sorted({key[0] for key in counts}),
            efforts=sorted({key[1] for key in counts},key=effort_key),
            modes=sorted({key[2] for key in counts}),
            combinations=[dict(model=model,effort=effort,service_tier=mode,count=count)
                          for (model,effort,mode),count in sorted(counts.items())],
            transports=sorted(transports),transport_sources=sorted(transport_sources),
            cache_policies=sorted(policies),homes=sorted(homes),sources=sorted(sources),**projects)
        return result,internal,sorted(models)

    def publish(self,snapshot,store,speeds=None):
        if self.token is None:
            self.adopt(store,())
        if store.token!=self.token:
            raise NotReady('delivery must adopt the exact committed owner delta')
        sequence=self.sequence+1
        message={key:value for key,value in snapshot.items() if key not in ('sessions','request_activity')}
        base=dict(protocol=1,epoch=self.epoch,sequence=sequence,reset=sequence==1,
                  data_revision=self.token.generation,call_revision=self.token.generation,
                  prepared_token=(self.token.instance,self.token.generation))
        if self.sent_token==self.token and sequence!=1 and not self._projection_dirty:
            self.sequence=sequence
            return dict(message,**base,sessions=[],overlay_sessions=[],removed=[],
                        model_candidates=[],cache_candidates=[],internal_review_calls=self.internal_count)
        sessions={}
        excluded={'_internal_review','_collection_pending','history','requests','groups','totals',
                  'turn_records','turn_states','transports','unmatched_transports','coverage_gaps','remaining'}
        for key,entry in sorted(self.owners.items(),key=lambda pair:pair[1]['owner_ordinal']):
            if not entry['metadata'].get('_internal_review'):
                previous=self.sessions.get(key)
                sessions[key]=(previous if previous is not None and key not in self.changed else
                               {name:value for name,value in entry['metadata'].items() if name not in excluded})
        overlay=self.overlay._fork()
        with store.read_current(seconds=60) as view:
            overlays=overlay.collect(view,self.owners,
                None if sequence==1 else self.changed,self.sessions.keys()-sessions.keys(),speeds or {},
                bind_generation=False)
            from .prepared_health import notification_health
            candidates=[notification_health(view,key,own=overlay.own[key],
                                            revision=overlay.revisions[key],since=self.cache_floor)
                        for key in sessions if key in self.changed]
            if self._choices_dirty:
                global_choices,internal,models=self._filter_choices(snapshot,sessions,view)
            else:
                global_choices,internal,models=self.global_choices,self.internal_count,self.global_models
        overlays={(value['home'],value['id']):value for value in overlays}
        message.update(**base,sessions=[value for key,value in sessions.items()
                                        if self.sessions.get(key) is not value and self.sessions.get(key)!=value],
            overlay_sessions=[value for key,value in overlays.items()
                              if self.overlays.get(key) is not value and self.overlays.get(key)!=value],
            removed=list(self.sessions.keys()-sessions.keys()),model_candidates=list(self.pending_alerts.values()),
            cache_candidates=candidates,internal_review_calls=internal,
            filter_choices=global_choices,models=models)
        extra=(sys.getsizeof(message)+sum(sys.getsizeof(value) for value in message.values()
                                         if isinstance(value,list))+
               _weight((candidates,global_choices,models),self.STATE_BYTES))
        pending_alerts={};changed=set()
        retention=self._admit(sessions=sessions,overlays=overlays,overlay=overlay,
            pending_alerts=pending_alerts,changed=changed,global_choices=global_choices,global_models=models,extra=extra)
        self.sessions=sessions;self.overlays=overlays;self.internal_count=internal
        self.global_choices=global_choices;self.global_models=models;self._choices_dirty=False
        self.overlay=overlay;self._retention=retention
        self.sequence=sequence
        self.sent_token=self.token
        self.changed=changed;self.pending_alerts=pending_alerts;self._projection_dirty=False
        return message

    def publish_pages(self,snapshot,store,speeds=None):
        """Deliver one admitted result through bounded, atomically consumed fragments."""
        message=self.publish(snapshot,store,speeds)
        arrays=('sessions','overlay_sessions','removed','model_candidates','cache_candidates','models')
        metadata={key:value for key,value in message.items()
                  if key not in (*arrays,'filter_choices','protocol','sequence','reset')}
        minimal={key:value for key,value in metadata.items()
                 if key in ('epoch','prepared_token','data_revision','call_revision','aggregate_request')}
        def packet(number,first):
            return dict(metadata if first else minimal,protocol=2,sequence=number,
                reset=bool(first and message['reset']),more=True,sessions=[],overlay_sessions=[],
                removed=[],model_candidates=[],cache_candidates=[])
        def parts():
            for name in arrays:
                if name not in message:continue
                if not message[name] and name=='models':yield name,None,[]
                for value in message[name]:yield name,None,value
            for name,values in message.get('filter_choices',{}).items():
                if isinstance(values,list):
                    if not values:yield 'filter_choices',name,[]
                    for value in values:yield 'filter_choices',name,[value]
                elif isinstance(values,dict):
                    if not values:yield 'filter_choices',name,{}
                    for key,value in values.items():yield 'filter_choices',name,{key:value}
                else:yield 'filter_choices',name,values
        first=True
        current=packet(message['sequence'],True)
        size=_weight(current,sys.maxsize);count=0
        for name,key,value in parts():
            weight=_weight((name,key,value),sys.maxsize)
            if ((count and (count>=store.limits.page_rows or size+weight>store.limits.page_objects_bytes))
                    or not count and first and size>store.limits.page_objects_bytes):
                self.sequence=current['sequence']
                yield current
                first=False
                current=packet(self.sequence+1,False)
                size=_weight(current,sys.maxsize);count=0
            if name=='filter_choices':
                choices=current.setdefault(name,{})
                if isinstance(value,list):choices.setdefault(key,[]).extend(value)
                elif isinstance(value,dict):choices.setdefault(key,{}).update(value)
                else:choices[key]=value
            elif name=='models':
                current.setdefault(name,[]).extend(value if value==[] else [value])
            else:current[name].append(value)
            size+=weight;count+=1
        current['more']=False
        self.sequence=current['sequence']
        yield current
