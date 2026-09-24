"""Full-session, provisional cache incident classification and retained evidence."""
from collections import deque
from copy import deepcopy
from .pricing import request_tier
from .core import token_number


def segment(row):
    return (row.get('model') or None, row.get('effort') or None,
            request_tier(row), row.get('cache_policy'))


class CacheHealth:
    def __init__(self):
        self.rows=[]
        self.reset()

    def reset(self):
        self.baseline=deque(maxlen=5)
        self.scope=None;self.incident=None;self.last=None
        self.count=0;self.recovery=0;self.candidate=0;self.missing=0
        self.candidate_rows=[];self.recovery_rows=[];self.events=[]
        self.state='판정 보류';self.reason='비교 표본 부족'

    @staticmethod
    def valid(row):
        i,c=token_number(row.get('input')),token_number(row.get('cached'))
        return (i is not None and i>0 and c is not None and c<=i
                and not any(row.get(k) for k in ('observation_missing','cache_policy_conflict','model_conflict','mode_conflict','input_conflict')))

    def update(self,rows):
        rows=[r for r in rows if r.get('purpose')!='maintenance']
        rows=sorted(rows,key=lambda r:r['ts'])
        if len(rows)<len(self.rows) or any(a is not b for a,b in zip(self.rows,rows)):
            self.reset();self.rows=[]
        for row in rows[len(self.rows):]:self.consume(row)
        self.rows=list(rows)
        recent=[]
        for row in reversed(rows[-5:]):
            if segment(row)!=self.scope:break
            recent.append(row)
        valid=[r for r in recent if self.valid(r)]
        total=sum(r['input'] for r in valid)
        from .cache_audit import observations
        return dict(state=self.state,reason=self.reason,incident=deepcopy(self.incident),audit=observations(rows[-101:])[-100:],
                    incidents=len(self.events),events=deepcopy(self.events),
                    recent_rate=sum(r['cached'] for r in valid)/total*100 if total else None,
                    valid=len(valid),sample=len(recent),partial=len(valid)!=len(recent),provisional=True)

    def consume(self,row):
        scope=segment(row)
        if scope!=self.scope or (self.last is not None and (
                row.get('compaction_epoch')!=self.last.get('compaction_epoch') or
                (self.valid(row) and self.valid(self.last) and row['input']<self.last['cached']))):
            self.baseline.clear();self.scope=scope;self.candidate_rows=[];self.recovery_rows=[]
            self.candidate=0;self.recovery=0;self.incident=None
        if not self.valid(row):
            self.state='판정 보류';self.reason='필수 관측 누락 또는 조건 충돌'
            self.candidate_rows=[];self.recovery_rows=[];self.candidate=0;self.recovery=0
            self.missing+=1;self.last=row;return
        base=list(self.baseline)
        base_input=sum(r['input'] for r in base)
        rate=sum(r['cached'] for r in base)/base_input if base_input else 0
        read=sum(r['cached'] for r in base)/len(base) if base else 0
        comparable=len(base)>=3 and all(v not in (None,'','미확인') for v in scope[:3]) and read>0
        bad=comparable and rate-row['cached']/row['input']>=.30-1e-12 and row['cached']<=read*.50
        if self.incident and not self.incident['resolved']:
            event=self.incident
            recovered=(row['cached']>0 and row['cached']>=event['baseline_read']*.8
                       and row['cached']/row['input']>=event['baseline_rate']-.1-1e-12)
            self.recovery_rows=self.recovery_rows+[row] if recovered else []
            if bad:
                event['occurrence_keys'].append(row['key']);event['count']=len(event['occurrence_keys'])
                event['latest_key']=row['key'];event['updated_at']=row['ts']
            if len(self.recovery_rows)>=2:
                event.update(resolved=True,ended_at=row['ts'],recovery_keys=[r['key'] for r in self.recovery_rows],
                             recovery_key=row['key'])
                self.state='회복 확인';self.reason='동일 조건의 유효 호출 2회 회복'
                self.baseline.clear();self.baseline.extend(self.recovery_rows);self.candidate_rows=[]
            else:
                self.state='캐시 저하 의심';self.reason=f"저하 호출 {event['count']}회 · 기준 {len(event['baseline_keys'])}호출"
        elif bad:
            self.candidate_rows.append(row)
            if len(self.candidate_rows)>=2:
                self.count+=1
                self.incident=dict(id='cache:'+str(self.candidate_rows[0]['key']),first_key=self.candidate_rows[0]['key'],
                    latest_key=row['key'],ts=self.candidate_rows[0]['ts'],updated_at=row['ts'],
                    count=len(self.candidate_rows),segment=scope,baseline_read=read,baseline_rate=rate,
                    baseline_keys=[r['key'] for r in base],occurrence_keys=[r['key'] for r in self.candidate_rows],
                    recovery_keys=[],state='캐시 저하 의심',resolved=False)
                self.events.append(self.incident)
                self.state='캐시 저하 의심';self.reason=f'저하 호출 {len(self.candidate_rows)}회 · 기준 {len(base)}호출'
            else:self.state='판정 보류';self.reason='연속 저하 1회 · 다음 유효 관측 필요'
        else:
            self.candidate_rows=[]
            self.state='캐시 읽기 0' if row['cached']==0 else '판정 보류'
            self.reason='비교 표본 부족' if not comparable else '반복 저하 근거 없음'
            self.baseline.append(row)
        self.candidate=len(self.candidate_rows);self.recovery=len(self.recovery_rows);self.last=row
