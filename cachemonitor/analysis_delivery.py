"""Versioned deltas on the process wire, compatible snapshots inside the GUI."""
import uuid
from collections import Counter
from .analytics import observed_choices, project_choices, effort_key

MODEL_KEYS=('key','ts','model_observation_ts','model_alert_confirmed','requested_model','response_model')


class SnapshotPublisher:
    CHOICE_CACHE_BYTES=4*1024*1024
    CHOICE_CACHE_ENTRIES=4096
    def __init__(self):
        self.epoch=uuid.uuid4().hex;self.sequence=0
        self.sessions={};self.overlays={};self.revisions={};self.models={};self.model_alerts={}
        self.choices={}

    def observed_choices(self, engine):
        # Reuse the small choice summaries, independently of retained history
        # length. A new observation must not force unrelated sessions through
        # a population-wide fallback. Cache revisions rather than owning rows.
        from .analysis_engine import retained_bytes
        current={};weight=0;counts=Counter();transports=set();sources=set();policies=set()
        for key,state in engine.sessions.items():
            rows=state['prepared']['history']
            previous=self.choices.get(key)
            revision=state.get('content_revision',state['revision'])
            if previous is not None and previous[0]==revision:
                value,size=previous[1:]
            else:
                value=self._choices_for_rows(rows)
                size=retained_bytes((key,revision,value),self.CHOICE_CACHE_BYTES)+128
            if len(current)<self.CHOICE_CACHE_ENTRIES and weight+size<=self.CHOICE_CACHE_BYTES:
                current[key]=(revision,value,size);weight+=size
            counts.update({(c['model'],c['effort'],c['service_tier']):c['count'] for c in value['combinations']})
            transports.update(value['transports']);sources.update(value['transport_sources'])
            policies.update(value['cache_policies'])
        self.choices=current
        self.choice_cache_bytes=weight
        return dict(models=sorted({key[0] for key in counts}),
                    efforts=sorted({key[1] for key in counts},key=effort_key),
                    modes=sorted({key[2] for key in counts}),
                    combinations=[dict(model=m,effort=e,service_tier=t,count=n)
                                  for (m,e,t),n in sorted(counts.items())],
                    transports=sorted(transports),transport_sources=sorted(sources),
                    cache_policies=sorted(policies))

    @staticmethod
    def _choices_for_rows(rows):
        return {**observed_choices(rows),
                'transports':sorted({r['transport'] for r in rows if r.get('transport') in ('WebSocket','HTTP/SSE')}),
                'transport_sources':sorted({r['transport_source'] for r in rows
                    if r.get('transport') in ('WebSocket','HTTP/SSE') and r.get('transport_source') in ('response_id','log_time')}),
                'cache_policies':sorted({str(r['cache_policy']) for r in rows
                    if r.get('cache_policy') not in (None,'','unknown','미확인','확인 불가')})}

    def publish(self,snapshot,engine,summaries):
        self.sequence+=1
        sessions={};overlays={(s['home'],s['id']):s for s in summaries}
        models=[];cache=[]
        for source in snapshot['sessions']:
            key=(source['home'],source['id'])
            if key not in engine.sessions:continue
            state=engine.sessions[key]
            light={k:v for k,v in source.items() if k not in ('history','requests','groups','totals','cache_misses',
                'turn_records','turn_states','transports','unmatched_transports','coverage_gaps','_collection_pending')}
            # Sliding-window countdown is derived from activity, not repeatedly shipped.
            light.pop('remaining',None)
            light['cache_misses']={k:v for k,v in state['prepared'].get('cache_misses',{}).items() if k!='events'}
            sessions[key]=light
            revision=state.get('content_revision',state['revision'])
            if self.revisions.get(key)==revision:continue
            self.revisions[key]=revision
            self.models[key]={r.get('model') for r in state['prepared']['history'] if r.get('model')}
            old_alerts=self.model_alerts.get(key,{})
            alerts={}
            for identity,(_,row) in state['records'].items():
                prev=old_alerts.get(identity)
                if not row.get('model_alert_confirmed') and prev is None:continue
                signature=tuple(row.get(k) for k in MODEL_KEYS)
                if row.get('model_alert_confirmed'):alerts[identity]=signature
                if signature!=prev:
                    models.append(dict(home=source['home'],sid=source['id'],title=source['title'],
                                       **dict(zip(MODEL_KEYS,signature))))
            # Only confirmed alert identities are needed to detect corrections
            # and resolution. Keeping the old engine state retains raw history
            # throughout the next collection, analysis and publication cycle.
            self.model_alerts[key]=alerts
            health=overlays[key].get('cache_health',{})
            cache.append(dict(home=source['home'],id=source['id'],title=source['title'],cache_health=health,
                              cache_misses=overlays[key]['cache_misses']))
        removed=list(self.sessions.keys()-sessions.keys())
        self.internal_review_calls=sum(len(s['prepared']['history']) for s in engine.internal_sessions.values())
        message={k:v for k,v in snapshot.items() if k not in ('sessions','request_activity')}
        message.update(protocol=1,epoch=self.epoch,sequence=self.sequence,reset=self.sequence==1,
                       sessions=[s for k,s in sessions.items() if self.sessions.get(k)!=s],
                       overlay_sessions=[s for k,s in overlays.items() if self.overlays.get(k)!=s],
                       removed=removed,data_revision=engine.revision,call_revision=engine.call_revision,
                       model_candidates=models,cache_candidates=cache,
                       internal_review_calls=self.internal_review_calls)
        self.models={k:v for k,v in self.models.items() if k in sessions}
        self.revisions={k:v for k,v in self.revisions.items() if k in sessions}
        self.model_alerts={k:v for k,v in self.model_alerts.items() if k in sessions}
        if self.sequence==1 or engine.call_revision!=getattr(self,'call_revision',None):
            message['models']=sorted(set().union(*self.models.values()) if self.models else set())
            message['filter_choices']={**self.observed_choices(engine),
                'homes':sorted(set(snapshot.get('homes',[]))|{s['home'] for s in sessions.values()}),
                **project_choices(sessions.values()),
                'sources':sorted({s.get('source','unknown') for s in sessions.values()})}
        self.sessions=sessions;self.overlays=overlays;self.revision=engine.revision
        self.call_revision=engine.call_revision
        return message

    def heartbeat(self,snapshot,engine):
        """Refresh the envelope after the worker proves unchanged owner content."""
        if (not self.sequence or self.revision!=engine.revision
                or self.call_revision!=engine.call_revision):
            raise ValueError('A heartbeat requires the current analytical publication')
        self.sequence+=1
        message={k:v for k,v in snapshot.items() if k not in ('sessions','request_activity')}
        message.update(protocol=1,epoch=self.epoch,sequence=self.sequence,reset=False,
            sessions=[],overlay_sessions=[],removed=[],data_revision=engine.revision,
            call_revision=engine.call_revision,model_candidates=[],cache_candidates=[],
            internal_review_calls=self.internal_review_calls)
        return message


class SnapshotReceiver:
    def __init__(self):
        self.epoch=None;self.sequence=0;self.sessions={};self.overlays={};self.models=[]
        self.retired=set();self.filter_choices={}

    def receive(self,value):
        if 'protocol' not in value:return value
        if value.get('reset'):
            if value['epoch'] in self.retired:return None
            if value['epoch']==self.epoch and value['sequence']<=self.sequence:return None
            if self.epoch and self.epoch!=value['epoch']:
                if len(self.retired)>=64:raise ValueError('분석 동기화 재시작 필요')
                self.retired.add(self.epoch)
            self.epoch=value['epoch'];self.sequence=0;self.sessions={};self.overlays={}
        if value['epoch']!=self.epoch or value['sequence']<=self.sequence:return None
        if value['sequence']!=self.sequence+1:raise ValueError('분석 변경분 순서 누락')
        self.sequence=value['sequence']
        for key in value['removed']:
            self.sessions.pop(tuple(key),None);self.overlays.pop(tuple(key),None)
        for s in value['sessions']:self.sessions[(s['home'],s['id'])]=s
        for s in value['overlay_sessions']:self.overlays[(s['home'],s['id'])]=s
        if 'models' in value:self.models=value['models']
        if 'filter_choices' in value:self.filter_choices=value['filter_choices']
        return {**value,'changed_sessions':value['sessions'],'sessions':list(self.sessions.values()),
                'overlay_sessions':list(self.overlays.values()),'models':self.models,'filter_choices':self.filter_choices}
