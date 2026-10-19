"""Publish prepared owner deltas without rebuilding or retaining call history."""
from collections import Counter
import sys
import uuid

from .analysis_delivery import MODEL_KEYS
from .analytics import effort_key, project_choices
from .prepared_ingest import _weight, pages
from .prepared_store import CapacityExceeded, NotReady


class PreparedPublisher:
    # Includes owner metadata, exact small choice/alert packets and compact
    # summaries. Historical calls and health section bodies have no cache here.
    STATE_BYTES=256*1024*1024

    def __init__(self,cache_floor):
        from .prepared_overlays import PreparedOverlays
        self.overlay=PreparedOverlays()
        self.owners={};self.choices={};self.alerts={}
        self.owner_weights={};self.owner_bytes=0
        self.sessions={};self.overlays={}
        self.token=None
        self.cache_floor=cache_floor
        self.reset()

    def reset(self):
        self.epoch=uuid.uuid4().hex
        self.sequence=0
        self.sent_token=None
        self.sessions={};self.overlays={}
        self.pending_alerts={
            (key,identity):dict(home=key[0],sid=key[1],title=self.owners[key]['metadata'].get('title',''),
                                **dict(zip(MODEL_KEYS,signature)))
            for key,alerts in self.alerts.items() if not self.owners[key]['metadata'].get('_internal_review')
            for identity,signature in alerts.items()}
        self.changed=set(self.owners)

    def adopt(self,store,changed,removed=()):
        if self.token==store.token:
            return
        # Admit the whole small delta and order before replacing retained state.
        # Failures leave the previous token and its content references coherent.
        owners=self.owners.copy();choices=self.choices.copy();alerts=self.alerts.copy()
        weights=self.owner_weights.copy();owner_bytes=self.owner_bytes
        pending_alerts=self.pending_alerts.copy();content_changed=set()
        with store.read_current(seconds=60) as view:
            if not view.db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0]:
                raise NotReady('cannot deliver a partial owner conversion')
            if self.token is None:
                entries=((row['owner'],row) for row in pages(view.catalog))
                removed=()
            else:
                entries=((tuple(key),view.owner(*key)) for key in changed)
            for key in removed:
                key=tuple(key)
                owners.pop(key,None);choices.pop(key,None);alerts.pop(key,None)
                owner_bytes-=weights.pop(key,0)
            for key,entry in entries:
                if entry is None:
                    raise NotReady('published change references an absent prepared owner')
                entry={name:value for name,value in entry.items() if name!='owner'}
                previous=owners.get(key)
                if previous is not None and (
                        {name:value for name,value in previous.items() if name!='owner_ordinal'} ==
                        {name:value for name,value in entry.items() if name!='owner_ordinal'}):
                    # Order alone retains every verified content reference.
                    continue
                old_alerts=alerts.get(key,{})
                new_alerts=view.derived(*key,'model_alerts') or {}
                owner_choices=view.derived(*key,'choices') or {}
                weight=_weight((key,entry,owner_choices,new_alerts),self.STATE_BYTES)
                owner_bytes=owner_bytes-weights.get(key,0)+weight
                if owner_bytes>self.STATE_BYTES:
                    raise CapacityExceeded('prepared publication metadata exceeds its retention budget')
                weights[key]=weight;owners[key]=entry
                choices[key]=owner_choices;alerts[key]=new_alerts
                content_changed.add(key)
                if entry['metadata'].get('_internal_review'):
                    continue
                # A resolved alert still emits the corrected selected row.
                keys=[identity for identity in old_alerts.keys()|new_alerts.keys()
                      if old_alerts.get(identity)!=new_alerts.get(identity)]
                for identity in keys:
                    signature=new_alerts.get(identity)
                    if signature is None:
                        row=view.record(*key,identity)
                        if row is None:continue
                        signature=tuple(row.get(name) for name in MODEL_KEYS)
                    pending_alerts[(key,identity)]=dict(home=key[0],sid=key[1],
                        title=entry['metadata'].get('title',''),**dict(zip(MODEL_KEYS,signature)))
            order={};order_bytes=0
            for entry in pages(view.ordering):
                key,ordinal=entry['owner'],entry['owner_ordinal']
                order_bytes+=_weight((key,ordinal),self.STATE_BYTES)+160
                if order_bytes>self.STATE_BYTES:
                    raise CapacityExceeded('prepared order metadata exceeds its retention budget')
                if key in order:
                    raise NotReady('catalog order repeats a publication owner')
                order[key]=ordinal
            if order.keys()!=owners.keys():
                raise NotReady('catalog order does not match adopted publication owners')
            for key,ordinal in order.items():
                previous=owners[key]['owner_ordinal']
                difference=sys.getsizeof(ordinal)-sys.getsizeof(previous)
                owner_bytes+=difference;weights[key]+=difference
                if ordinal!=previous:
                    owners[key]=dict(owners[key],owner_ordinal=ordinal)
            if owner_bytes>self.STATE_BYTES:
                raise CapacityExceeded('prepared order metadata exceeds its retention budget')
            _weight((owners,choices,alerts,pending_alerts),self.STATE_BYTES)
            token=view.token
        self.owners=owners;self.choices=choices;self.alerts=alerts
        self.owner_weights=weights;self.owner_bytes=owner_bytes
        self.pending_alerts=pending_alerts;self.changed.update(content_changed)
        self.token=token

    def _filter_choices(self,snapshot,sessions):
        counts=Counter();transports=set();transport_sources=set();policies=set()
        for key in sessions:
            value=self.choices[key]
            counts.update({(c['model'],c['effort'],c['service_tier']):c['count']
                           for c in value.get('combinations',())})
            transports.update(value.get('transports',()))
            transport_sources.update(value.get('transport_sources',()))
            policies.update(value.get('cache_policies',()))
        return dict(models=sorted({key[0] for key in counts}),
            efforts=sorted({key[1] for key in counts},key=effort_key),
            modes=sorted({key[2] for key in counts}),
            combinations=[dict(model=m,effort=e,service_tier=t,count=n)
                          for (m,e,t),n in sorted(counts.items())],
            transports=sorted(transports),transport_sources=sorted(transport_sources),
            cache_policies=sorted(policies),
            homes=sorted(set(snapshot.get('homes',()))|{key[0] for key in sessions}),
            sources=sorted({s.get('source','unknown') for s in sessions.values()}),
            **project_choices(sessions.values()))

    def publish(self,snapshot,store,speeds=None):
        if self.token is None:
            self.adopt(store,())
        if store.token!=self.token:
            raise NotReady('delivery must adopt the exact committed owner delta')
        self.sequence+=1
        message={key:value for key,value in snapshot.items() if key not in ('sessions','request_activity')}
        base=dict(protocol=1,epoch=self.epoch,sequence=self.sequence,reset=self.sequence==1,
                  data_revision=self.token.generation,call_revision=self.token.generation,
                  prepared_token=(self.token.instance,self.token.generation))
        if self.sent_token==self.token and self.sequence!=1:
            return dict(message,**base,sessions=[],overlay_sessions=[],removed=[],
                        model_candidates=[],cache_candidates=[],internal_review_calls=self.internal_count)
        sessions={};internal=0
        excluded={'_internal_review','_collection_pending','history','requests','groups','totals',
                  'turn_records','turn_states','transports','unmatched_transports','coverage_gaps','remaining'}
        for key,entry in sorted(self.owners.items(),key=lambda pair:pair[1]['owner_ordinal']):
            if entry['metadata'].get('_internal_review'):
                internal+=entry['dependencies'].get('call_count',0)
            else:
                sessions[key]={name:value for name,value in entry['metadata'].items() if name not in excluded}
        with store.read_current(seconds=60) as view:
            overlays=self.overlay.collect(view,self.owners,
                None if self.sequence==1 else self.changed,self.sessions.keys()-sessions.keys(),speeds or {})
            from .prepared_health import notification_health
            candidates=[notification_health(view,key,since=self.cache_floor)
                        for key in sessions if key in self.changed]
        overlays={(value['home'],value['id']):value for value in overlays}
        message.update(**base,sessions=[value for key,value in sessions.items() if self.sessions.get(key)!=value],
            overlay_sessions=[value for key,value in overlays.items() if self.overlays.get(key)!=value],
            removed=list(self.sessions.keys()-sessions.keys()),model_candidates=list(self.pending_alerts.values()),
            cache_candidates=candidates,internal_review_calls=internal,
            filter_choices=self._filter_choices(snapshot,sessions),
            models=sorted({model for key in sessions for model in self.choices[key].get('models',())}))
        _weight((self.owners,self.choices,self.alerts,sessions,overlays,
                 self.overlay.own,self.overlay.partials,self.overlay.presenter.cache,
                 self.overlay.presenter.rollups,self.overlay.presenter.presented,
                 self.overlay.detail_revisions),self.STATE_BYTES)
        self.sessions=sessions;self.overlays=overlays;self.internal_count=internal
        self.sent_token=self.token
        self.changed.clear();self.pending_alerts.clear()
        return message
