"""Notification decisions from explicit wire evidence and classified local probes."""
from collections import OrderedDict, deque
import time


class ConfirmedNotifications:
    def __init__(self,wall_clock=time.time,clock=time.monotonic):
        self.wall_clock,self.clock=wall_clock,clock
        self.model_since=wall_clock()
        self.model_floor=self.model_since
        self.model_enabled=True
        self.proxy_enabled=True
        self.seen=OrderedDict()
        self.model_records={}
        self.groups={}
        self.histories={}
        self.records=deque(maxlen=50)
        self.streak=None
        self.incident=None
        self.cache_enabled=True
        self.cache_since=wall_clock()
        self.cache_seen=OrderedDict()
        self.cache_floor=self.cache_since
        self.cache_incidents=OrderedDict()
        self.http_enabled=True
        self.http_since=wall_clock()
        self.http_seen=OrderedDict()
        self.http_floor=self.http_since

    def enable_http(self,enabled):
        if enabled!=self.http_enabled:self.http_since=self.wall_clock()
        self.http_enabled=bool(enabled)

    def http(self,sessions):
        """Notify the explicit fallback observation once, not every later HTTP call."""
        events=[]
        for session in sessions:
            observations=list(session.get('transports',()))
            if not observations and session.get('transport_evidence')=='HTTP 전환 기록':
                observations=[dict(ts=session.get('transport_ts'),kind=session.get('transport'),evidence=session['transport_evidence'])]
            for observed in observations:
                if observed.get('kind')!='HTTP/SSE' or observed.get('evidence')!='HTTP 전환 기록':continue
                stamp=observed.get('ts')
                if type(stamp) not in (int,float) or not self.http_floor<stamp<=self.wall_clock():continue
                key=(session['home'],session['id'],stamp,observed.get('process'),observed.get('turn'))
                if key in self.http_seen:continue
                self.http_seen[key]=stamp
                if len(self.http_seen)>10000:
                    _,old=self.http_seen.popitem(last=False);self.http_floor=max(self.http_floor,old)
                if not self.http_enabled or stamp<=self.http_since:continue
                item=self.record('http_fallback','HTTP 전환',f"{session.get('title') or session['id']} · HTTP 전환 기록 확인")
                from .overlay_navigation import NavigationTarget
                item.update(home=session['home'],sid=session['id'],observation_at=stamp,
                    target=NavigationTarget(session['home'],session['id'],view='calls',filters=('http',),
                        request_id=observed.get('turn') or None,section='evidence').as_dict())
                events.append(item)
        return events

    def model_candidates(self,candidates):
        sessions={}
        for row in candidates:
            if not row.get('model_alert_confirmed'):
                previous=self.model_records.get((row['home'],row.get('key')))
                if previous:previous['resolution']='관측 정정 · 해당 응답의 확정 조건 해제'
                continue
            scope=(row['home'],row['sid'])
            session=sessions.setdefault(scope,dict(home=row['home'],id=row['sid'],title=row['title'],history=[]))
            session['history'].append(row)
        return self.models(sessions.values())

    def enable_cache(self,enabled):
        if enabled!=self.cache_enabled:
            self.cache_since=self.wall_clock()
            self.cache_enabled=enabled

    def cache(self,sessions):
        events=[]
        for session in sessions:
            summary=session.get('cache_misses',{})
            fresh=[]
            for call in summary.get('events',[]):
                identity=(session['home'],session['id'],call['key'])
                if identity in self.cache_seen or call['ts']<=self.cache_floor:continue
                self.cache_seen[identity]=call['ts']
                if len(self.cache_seen)>10000:
                    _,old=self.cache_seen.popitem(last=False)
                    self.cache_floor=max(self.cache_floor,old)
                if self.cache_enabled and call['ts']>self.cache_since:
                    fresh.append(call)
            health=session.get('cache_health')
            if health is not None:
                scope=(session['home'],session['id'])
                incident=health.get('incident')
                previous=self.cache_incidents.get(scope)
                if previous and incident is None:
                    previous['resolution']='기록 정정 · 이상 판정 해제'
                if previous and incident and previous.get('incident_id')==incident['id']:
                    previous.update(count=incident['count'],detail=f"{session['title']} · {health['reason']}")
                    if incident.get('resolved'):
                        previous['resolution']='회복 확인'
                    continue
                if incident and not incident.get('resolved') and self.cache_enabled and incident['ts']>max(self.cache_since,self.cache_floor):
                    item=self.record('cache_miss',incident['state'],f"{session['title']} · {health['reason']} · 잠정 규칙")
                    item.update(home=scope[0],sid=scope[1],count=incident['count'],incident_id=incident['id'],
                                call_id=incident['latest_key'],response_id=incident['latest_key'])
                    from .overlay_navigation import NavigationTarget
                    item['target']=NavigationTarget(scope[0],scope[1],view='incident',event_id=str(incident['id']),section='evidence').as_dict()
                    self.cache_incidents[scope]=item;events.append(item)
                    if len(self.cache_incidents)>2048:
                        _,old=self.cache_incidents.popitem(last=False)
                        self.cache_floor=max(self.cache_floor,old['at'])
                # Raw miss accounting is retained in analysis; alerts use the incident lifecycle.
                continue
            # A zero-read fact alone is never a Windows warning, even when
            # collection has not supplied an incident classifier result yet.
        return events

    def enable_model(self,enabled):
        if enabled!=self.model_enabled:
            self.model_since=self.wall_clock()
            self.histories.clear()
        self.model_enabled=enabled

    def enable_proxy(self,enabled):
        self.proxy_enabled=enabled
        self.streak=None

    def record(self,kind,title,detail):
        item=dict(kind=kind,title=title,detail=detail,count=1,at=self.wall_clock())
        self.records.appendleft(item)
        return item

    def models(self,sessions):
        now=self.clock();events=[]
        self.groups={key:value for key,value in self.groups.items() if now-value[0]<600}
        histories={}
        for session in sessions:
            scope=(session['home'],session['id'])
            rows=session.get('history',[])
            histories[scope]=rows
            if self.histories.get(scope) is rows:continue
            for row in rows:
                stamp=min(row.get('ts') or 0,row.get('model_observation_ts') or 0)
                if (not row.get('model_alert_confirmed') or stamp<=max(self.model_since,self.model_floor)
                        or stamp>self.wall_clock()):continue
                key=(session['home'],row.get('key'))
                if not key[1] or key in self.seen:continue
                self.seen[key]=stamp
                if len(self.seen)>10000:
                    old_key,old=self.seen.popitem(last=False)
                    self.model_records.pop(old_key,None)
                    self.model_floor=max(self.model_floor,old)
                if not self.model_enabled:continue
                requested,responded=row['requested_model'],row['response_model']
                group=(*scope,requested,responded)
                detail=(f"{session['title']} · 요청: {requested} → 응답: {responded}\n"
                        f"응답 ID: {row['key']}\n완료 응답의 모델명 문자열 차이입니다. 실제 실행 모델 변경을 뜻하지 않습니다.")
                if group in self.groups:
                    item=self.groups[group][1]
                    item['count']+=1;item['at']=self.wall_clock();item['detail']=detail
                else:
                    item=self.record('model','모델명 불일치 확인',detail)
                    self.groups[group]=(now,item);events.append(item)
                from .overlay_navigation import NavigationTarget
                item.update(home=scope[0],sid=scope[1],call_id=row.get('call_id') or row['key'],
                    target=NavigationTarget(scope[0],scope[1],view='calls',call_id=str(row.get('call_id') or row['key']),section='evidence').as_dict())
                self.model_records[key]=item
        self.histories=histories
        return events

    def proxy(self,result):
        from .connection_recovery import assess
        now=self.clock()
        assessed=assess({**result,'configured':result.get('configured',False)})
        state=assessed['code']
        if state in ('direct','responding'):
            if self.incident:self.incident['resolution']='프록시 사용 해제' if state=='direct' else '정상 식별 응답 확인'
            self.incident=None;self.streak=None
            return []
        if not self.proxy_enabled or not assessed['confirmed']:
            self.streak=None
            return []
        if self.incident and self.incident.get('code')==state:return []
        if self.streak is None or self.streak[0]!=state or now-self.streak[2]>45:
            self.streak=[state,now,now,1]
            return []
        if now-self.streak[2]<15:return []
        self.streak[2]=now;self.streak[3]+=1
        if self.streak[3]<3 or now-self.streak[1]<30:return []
        if state=='refused':
            title='로컬 프록시 연결 거부'
            detail=f"{result.get('url') or '설정된 프록시'} 연결이 15초 이상 간격으로 3회 거부됐습니다. 설정 → 프록시에서 확인하세요."
        elif state=='identity_mismatch':
            title='프록시 식별 정보 불일치'
            detail='설정된 모델 관측 서비스와 응답의 식별 정보가 3회 다르게 확인됐습니다. 설정 → 프록시에서 확인하세요.'
        else:title,detail=assessed['title'],assessed['detail']
        self.incident=self.record('proxy',title,detail)
        self.incident['code']=state
        return [self.incident]
