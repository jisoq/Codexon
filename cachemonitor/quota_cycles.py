"""Persistent quota observations and local response costs, with matched-window estimates."""
from dataclasses import asdict
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from itertools import chain, groupby
from heapq import merge
import hashlib
import json
import math
import os
import operator
import pickle
from pathlib import Path
import sqlite3
import time
import zlib

from .pricing import (ALIASES, RATES, FAST_RATES, VERIFIED, RATE_VERIFIED, MODEL_VERIFIED,
                      MULTIPLIER_VERIFIED, SUBSCRIPTION_FAST_MULTIPLIERS, PRICE_POLICY, token_cost, request_tier)
from .workload import INTERNAL_REVIEW_MODEL


TOKEN_FIELDS = ('input', 'cached', 'written', 'output', 'reasoning')
MODE_SCAN_ROWS = 10_000
MODE_SCAN_MATCHES = 500


def _stored_cost(row):
    # The ordinary signature stays byte-identical. This optional marker
    # retains the one pricing guard not represented by calls columns.
    row=dict(row)
    signature=row.get('signature')
    if isinstance(signature,str) and signature.endswith(',{"input_conflict":true}]'):
        try:values=json.loads(signature)
        except (ValueError,TypeError):values=None
        if isinstance(values,list) and len(values)==len(TOKEN_FIELDS)+4 and values[-1]=={'input_conflict':True}:
            row['input_conflict']=True
    return token_cost(row)['cost']

PRICE_SNAPSHOT = json.dumps({'rates': {k: asdict(v) for k, v in RATES.items()},
                             'fast_rates': {k:asdict(v) for k,v in FAST_RATES.items()},
                             'subscription_fast_multipliers': SUBSCRIPTION_FAST_MULTIPLIERS,
                             'multiplier_verified': MULTIPLIER_VERIFIED, 'model_verified': MODEL_VERIFIED,
                             'rate_verified': RATE_VERIFIED,
                             'policy':PRICE_POLICY, 'aliases': ALIASES, 'verified': VERIFIED}, sort_keys=True)
PRICE_ID = hashlib.sha256(PRICE_SNAPSHOT.encode()).hexdigest()[:16]


class _ModeValues(dict):
    """Notify once for each actual top-level mode/evidence value change.

    Evidence writers replace complete scalar maps in rebuild_mode_evidence;
    they do not mutate nested evidence in place.
    """
    def __init__(self, changed, values=()):
        super().__init__(values)
        self._changed=changed

    def __setitem__(self,key,value):
        same=key in self and self[key]==value
        super().__setitem__(key,value)
        if not same:self._changed(key)

    def __delitem__(self,key):
        super().__delitem__(key)
        self._changed(key)

    def pop(self,key,*default):
        present=key in self
        value=super().pop(key,*default)
        if present:self._changed(key)
        return value

    def popitem(self):
        key,value=super().popitem()
        self._changed(key)
        return key,value

    def clear(self):
        while self:self.popitem()

    def update(self,*args,**kwargs):
        for key,value in dict(*args,**kwargs).items():self[key]=value

    def setdefault(self,key,default=None):
        if key not in self:self[key]=default
        return self[key]

    def __ior__(self,other):
        self.update(other)
        return self


_MODE_DEPENDENCY_DOMAIN=b'quota-mode-dependency-v1\0'
_EMPTY_MODE_DEPENDENCY='mode-v1:'+hashlib.sha256(_MODE_DEPENDENCY_DOMAIN+b'[[],[]]').hexdigest()


class _PriceEquivalence:
    """Reuse a stored calculation when only its verification metadata changed."""
    METADATA = {'verified', 'model_verified', 'rate_verified', 'multiplier_verified'}
    SNAPSHOT_BYTES = 256 * 1024
    CACHE_BYTES = 64 * 1024
    CACHE_ENTRIES = 64

    def __init__(self, db, identifier, snapshot):
        self.db = db
        self.identifier = identifier
        self.signature = self._signature(snapshot)
        self.cache = OrderedDict()
        self.cache_bytes = 0

    @classmethod
    def _signature(cls, snapshot):
        try:
            value = json.loads(snapshot)
            if not isinstance(value, dict) or not isinstance(value.get('policy'), str):
                return None
            if not all(isinstance(value.get(key), dict) for key in
                       ('rates', 'fast_rates', 'subscription_fast_multipliers', 'aliases')):
                return None
            # Unknown future fields remain significant. Only these four display
            # verification fields are absent from token_cost's arithmetic.
            data = {key: item for key, item in value.items() if key not in cls.METADATA}
            return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).digest()
        except (TypeError, ValueError):
            return None

    def __call__(self, identifier):
        if identifier == self.identifier:
            return True
        cached = self.cache.get(identifier)
        if cached is not None:
            self.cache.move_to_end(identifier)
            return cached[0]
        row = self.db.execute('SELECT data FROM prices WHERE id=? AND length(CAST(data AS BLOB))<=?',
                              (identifier, self.SNAPSHOT_BYTES)).fetchone()
        same = self.signature is not None and row is not None and self._signature(row[0]) == self.signature
        weight = 128 + len(str(identifier)) * 4
        if weight <= self.CACHE_BYTES:
            self.cache[identifier] = (same, weight)
            self.cache_bytes += weight
            while len(self.cache) > self.CACHE_ENTRIES or self.cache_bytes > self.CACHE_BYTES:
                _, (_, removed) = self.cache.popitem(last=False)
                self.cache_bytes -= removed
        return same


RESET_TOLERANCE = 120
MAX_OBSERVATION_GAP = 30 * 60
DISCONTINUITY = '사용률 불연속: 감소, 충돌 후 복귀량은 신규 소모로 계산하지 않음'


class _HistoryFields:
    """Produce ordinary dictionaries sharing one fixed set of field names.

    All history rows have these twelve fields. Instance dictionaries share
    their keys in the pinned Python runtime, including after the short-lived
    instance is released. Consumers still receive mutable, JSON-ready dicts.
    """
    def __init__(self, row, window, identifier=None):
        self.id=row['id'] if identifier is None else identifier
        self.home=row['home'];self.at=row['at'];self.source=row['source']
        self.account=row['account'];self.plan=row['plan'];self.bucket=row['bucket']
        self.window=window;self.used=row['used'];self.reset=row['reset']
        self.minutes=row['minutes'];self.separate=row['separate']


class _PackedRows:
    """Lossless internal rows with two decoded blocks for indexed selection."""
    def __init__(self, rows, *, timestamps=False):
        self.blocks=[];self.ends=[];self.times=array('d');self.time_ends=array('d')
        self._timestamped=timestamps;self.count=0;self._cache={}
        batch=[];weight=0
        for row in rows:
            values=row.values() if isinstance(row,dict) else row
            size=512+sum(len(v)*4 for v in values if isinstance(v,str))
            if batch and (len(batch)>=512 or weight+size>2*1024*1024):
                self._pack(batch)
                batch=[];weight=0
            batch.append(row);weight+=size;self.count+=1
            if timestamps and (not self.times or self.times[-1]!=row['at']):self.times.append(row['at'])
        if batch:self._pack(batch)

    def _pack(self, batch):
        pending=[batch]
        while pending:
            rows=pending.pop();payload=zlib.compress(pickle.dumps(rows,protocol=5),1)
            if len(payload)>64*1024 and len(rows)>1:
                middle=len(rows)//2;pending.extend((rows[middle:],rows[:middle]))
            else:
                self.blocks.append(payload)
                self.ends.append((self.ends[-1] if self.ends else 0)+len(rows))
                if self._timestamped:self.time_ends.append(rows[-1]['at'])

    def __len__(self):return self.count

    def __iter__(self):
        # Only this process creates these payloads; no disk/network pickle input.
        from .quota_cancellation import checkpoint
        for block in self.blocks:
            checkpoint()
            yield from pickle.loads(zlib.decompress(block))

    def __getitem__(self, index):
        if isinstance(index,slice):
            start,stop,step=index.indices(self.count)
            if step!=1:return [self[i] for i in range(start,stop,step)]
            if start>=stop:return []
            result=[]
            for block in range(bisect_right(self.ends,start),bisect_left(self.ends,stop)+1):
                base=self.ends[block-1] if block else 0
                result.extend(self._block(block)[max(0,start-base):stop-base])
            return result
        index=operator.index(index)
        if index<0:index+=self.count
        if not 0<=index<self.count:raise IndexError(index)
        block=bisect_right(self.ends,index)
        return self._block(block)[index-(self.ends[block-1] if block else 0)]

    def _block(self, block):
        rows=self._cache.pop(block,None)
        if rows is None:rows=pickle.loads(zlib.decompress(self.blocks[block]))
        self._cache[block]=rows
        while len(self._cache)>2:self._cache.pop(next(iter(self._cache)))
        return rows

    def signature(self, start=0, stop=None):
        """Compare immutable packed inputs without decoding unchanged history.

        Only boundary blocks need decoding. Exclude their neighboring rows so
        a correction in another interval does not invalidate this interval.
        """
        stop=self.count if stop is None else stop
        digest=hashlib.sha256()
        if start<stop:
            first=bisect_right(self.ends,start)
            base=self.ends[first-1] if first else 0
            digest.update(pickle.dumps((start-base,stop-start),protocol=5))
            for index in range(first,bisect_left(self.ends,stop)+1):
                base=self.ends[index-1] if index else 0
                if start<=base and stop>=self.ends[index]:
                    payload=self.blocks[index]
                else:
                    payload=json.dumps(self._block(index)[max(0,start-base):stop-base],
                                       sort_keys=True,separators=(',',':')).encode()
                digest.update(len(payload).to_bytes(8,'little'))
                digest.update(payload)
        return digest.digest()

    def __eq__(self, other):
        if self is other:return True
        try:return len(self)==len(other) and all(a==b for a,b in zip(self,other))
        except TypeError:return NotImplemented

    def __getstate__(self):
        return {key:value for key,value in vars(self).items() if key!='_cache'}

    def __setstate__(self, state):
        vars(self).update(state);self._cache={}


class _PackedHistory(_PackedRows):
    """Worker-owned timestamp-ordered evidence with compact boundary lookup."""
    def __init__(self, rows):super().__init__(rows,timestamps=True)

    def iter_from(self, at):
        for block in self.blocks[bisect_left(self.time_ends,at):]:
            for row in pickle.loads(zlib.decompress(block)):
                if row['at']>=at:yield row

    def range_blocks(self,start,end):
        """Retain just the immutable blocks needed to replay one display period."""
        first=bisect_left(self.time_ends,start)
        last=min(len(self.blocks),bisect_right(self.time_ends,end)+1)
        base=self.ends[first-1] if first else 0
        result=_PackedHistory(())
        result.blocks=self.blocks[first:last]
        result.ends=[value-base for value in self.ends[first:last]]
        result.count=result.ends[-1] if result.ends else 0
        result.time_ends=self.time_ends[first:last]
        return result

    def with_suffix(self, at, rows):
        """Share unchanged blocks and replace every record at or after at."""
        block=bisect_left(self.time_ends,at)
        retained=(r for r in pickle.loads(zlib.decompress(self.blocks[block])) if r['at']<at) if block<len(self.blocks) else ()
        tail=_PackedHistory(chain(retained,rows))
        count=self.ends[block-1] if block else 0
        result=_PackedHistory(())
        result.blocks=self.blocks[:block]+tail.blocks
        result.ends=self.ends[:block]+[count+end for end in tail.ends]
        result.count=count+tail.count
        result.time_ends=self.time_ends[:block]+tail.time_ends
        result.times=self.times[:bisect_left(self.times,at)]+tail.times[bisect_left(tail.times,at):]
        return result


def observation_parts(home, quota):
    """Prepare identical storage identities for live writes and history replay."""
    windows=[];weekly_ref=None
    for name, window in (quota or {}).get('windows', {}).items():
        if name not in ('weekly','five_hour') or not quota.get('observed_at'):continue
        key=hashlib.sha256(json.dumps([home,quota['observed_at'],quota.get('source'),
            quota.get('account'),quota.get('plan_type'),quota.get('bucket'),name,window],sort_keys=True).encode()).hexdigest()
        if (name=='weekly' and 'used_percent' in window and 'window_minutes' in window
                and home is not None and quota.get('source','local') is not None and quota.get('account','') is not None):
            weekly_ref=key
            continue
        windows.append((key,home,quota['observed_at'],quota.get('source','local'),quota.get('account',''),
            quota.get('plan_type') or '',quota.get('bucket') or ('codex' if quota.get('source')=='live' else ''),
            name,window.get('used_percent'),window.get('resets_at'),window.get('window_minutes'),
            json.dumps(quota.get('separate_models',[]))))
    window=(quota or {}).get('windows',{}).get('weekly');at=(quota or {}).get('observed_at')
    if not window or not at or 'used_percent' not in window or 'window_minutes' not in window:
        return windows,None,None,weekly_ref
    source=quota.get('source','local')
    key=hashlib.sha256(json.dumps([home,at,source,quota.get('account'),quota.get('plan_type'),
        quota.get('bucket'),quota.get('separate_models',[]),window],sort_keys=True).encode()).hexdigest()
    observation=(key,home,at,source,quota.get('account',''),window['used_percent'],
        window.get('resets_at'),window['window_minutes'],json.dumps(quota.get('separate_models',[])))
    context=(key,quota.get('plan_type') or '',quota.get('bucket') or ('codex' if source=='live' else ''))
    return windows,observation,context,weekly_ref


def ledger_path(index_path=None):
    if index_path:
        index = Path(index_path)
        return index.with_name(index.stem + '-quota-cycles.sqlite')
    return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'CacheMonitor' / 'quota-cycles.sqlite'


def split_cycles(observations, manual_resets=()):
    """Use nonoverlapping, monotone endpoints; retain costs across rejected ticks."""
    # Unidentified history is retained for inspection but must not interrupt
    # an identified account's calibration stream. Never assign it an account.
    identified=[r for r in observations if r.get('account')]
    if identified:observations=identified
    groups = []
    epochs = []

    def epoch(item):
        if not item['reset']:
            return None
        for record in epochs:
            if (record['minutes']==item['minutes'] and abs(record['reset']-item['reset'])<=RESET_TOLERANCE
                    and all(not record.get(k) or not item.get(k) or record[k]==item[k]
                            for k in ('account','plan','bucket'))):
                return record
        record={**item, 'high':item['used']}
        epochs.append(record)
        return record

    def accept(item):
        record=epoch(item)
        if record and item['used'] < record['high']:
            groups[-1]['rejected'].append(item)
            return
        if record:record['high']=max(record['high'],item['used'])
        groups[-1]['observations'].append(item)
    observations = sorted(observations, key=lambda r:(r['at'], r['id']))
    by_time = {}
    for r in observations:
        by_time.setdefault(r['at'], set()).add((r['used'], r['reset'], r['account'], r['minutes'],
                                               r.get('plan',''),r.get('bucket','')))
    conflicts = {at for at, values in by_time.items() if len(values) > 1}
    # A single contradictory spike/dip bracketed by equal observations does
    # not establish consumption. Do not edit the original observations.
    isolated = set()
    for a, b, c in zip(observations, observations[1:], observations[2:]):
        if (a['at'] < b['at'] < c['at'] and c['at']-a['at'] <= MAX_OBSERVATION_GAP
                and a['used'] == c['used'] != b['used']
                and all(r['reset'] and abs(r['reset']-a['reset']) <= RESET_TOLERANCE
                        and r['minutes']==a['minutes'] and r['account']==a['account']
                        and r.get('plan','')==a.get('plan','')
                        and r.get('bucket','')==a.get('bucket','')
                        and r['source']==a['source'] for r in (b,c))
                and not any(a['at'] <= m['at'] <= c['at'] for m in manual_resets)):
            isolated.add(b['id'])

    def append_group(item, reason, boundary=None, **extra):
        groups.append({'id':item['id'], 'reason':reason,
                       'boundary':item['at'] if boundary is None else boundary,
                       'observations':[], 'rejected':[], **extra})

    def close_tail():
        if not groups:
            return
        group = groups[-1]
        obs = group['observations']
        tail = [r for r in group.get('rejected',[]) if not obs or r['at'] > obs[-1]['at']]
        if tail and obs:
            groups.append({'id':'uncertain:'+tail[0]['id'], 'reason':'사용률 불연속 (끝점 미확정)',
                           'boundary':obs[-1]['at'], 'observations':[obs[-1],tail[-1]],
                           'rejected':tail, 'discontinuous':True})

    timeline = [(r['at'],1,r['id'],r) for r in observations]
    timeline += [(r['at'],0,r['id'],r) for r in manual_resets]
    for _, kind, _, item in sorted(timeline, key=lambda r:r[:3]):
        if kind == 0:
            close_tail()
            epochs.clear()  # Explicit replenishment may keep the same reset timestamp.
            append_group(item, '리셋권 사용, 사용자 기록', manual_reset=item)
            groups[-1]['id']='manual:'+item['id']
            continue
        if not groups:
            append_group(item, '부분 관측')
        group=groups[-1]
        marker=group.get('manual_reset')
        if marker and item['at'] < marker['baseline_after']:
            continue
        if item['at'] in conflicts or item['id'] in isolated:
            group['rejected'].append(item)
            continue
        if not group['observations']:
            if marker and marker['account'] and item['account'] and marker['account']!=item['account']:
                append_group(item,'계정 변경')
            accept(item)
            continue
        previous=group['observations'][-1]
        reason=None
        boundary=item['at']
        if item['account']!=previous['account']:
            reason='계정 변경'
        elif any(item.get(k)!=previous.get(k) for k in ('plan','bucket','separate')):
            reason='요금제 또는 한도 변경'
        elif item['minutes']!=previous['minutes']:
            reason='한도 창 변경 (경계 미확인)'
        elif item['reset'] and previous['reset'] and abs(item['reset']-previous['reset'])>RESET_TOLERANCE:
            shift=item['reset']-previous['reset']
            natural=item['at']>=previous['reset']-RESET_TOLERANCE and abs(shift-item['minutes']*60)<=RESET_TOLERANCE
            reason='예정 리셋 확인' if natural else '한도 시각 변경: 관측 구간 분리'
            if natural:boundary=previous['reset']
        elif item['used'] < previous['used']:
            group['rejected'].append(item)
            continue
        gap=False  # Elapsed time alone is not evidence of missing calls.
        if reason or gap:
            if not gap:close_tail()
            if gap:
                groups.append({'id':'gap:'+previous['id']+':'+item['id'],
                               'reason':'30분 초과 관측 공백','boundary':previous['at'],
                               'observations':[previous,item], 'gap':True, 'rejected':[]})
            append_group(item, reason or '관측 재개', boundary)
        accept(item)
    close_tail()
    return groups


def estimate(cost, delta, n, blocked=()):
    reasons = list(blocked)
    if not math.isfinite(delta) or delta <= 0:
        reasons.append('소모율 변화 없음: 다음 관측 대기')
    if cost is None or not math.isfinite(cost) or cost < 0:
        reasons.append('구독 가치 환산액 미확인')
    if not 0 <= n <= 100:
        reasons.append('n은 0~100 범위여야 합니다')
    if reasons:
        return {'usd': None, 'per_percent': None, 'sensitivity': None, 'reasons': list(dict.fromkeys(reasons))}
    return {'usd': cost / delta * n, 'per_percent': cost / delta,
            'sensitivity': (cost / (delta + 1) * n, cost / (delta - 1) * n) if delta > 1 else None, 'reasons': []}


def partition_coverage(groups, issues):
    """Cut only observation edges intersecting an evidenced counter discrepancy."""
    gaps = [r for r in issues if r['reason'].startswith(('누계 차액 증가','로컬 호출 없는 한도 증가','필수 토큰 또는 단가 누락','요청 모드 미확인'))
            and r['start'] is not None and r['end'] is not None]
    indexed={}
    for gap in gaps:
        reason=('누계 차액 증가: 호출 비용 누락, 누계 보정 미해결'
                if gap['reason'].startswith('누계') else gap['reason'])
        indexed.setdefault(reason,[]).append(gap)
    for reason,items in indexed.items():
        spans=sorted((g['start'],g['end']) for g in items if g['start']<g['end'])
        maximum=float('-inf');ends=[]
        for _,end in spans:
            maximum=max(maximum,end);ends.append(maximum)
        indexed[reason]=([a for a,_ in spans],ends,sorted(g['start'] for g in items if g['start']==g['end']))
    result = []
    for group in groups:
        obs = group['observations']
        if len(obs)<2 or group.get('gap') or group.get('discontinuous'):
            result.append(group)
            continue
        parts=[]
        for a,b in zip(obs,obs[1:]):
            reasons=set()
            for reason,(starts,ends,points) in indexed.items():
                last=bisect_left(starts,b['at'])-1
                if (last>=0 and ends[last]>a['at']) or bisect_right(points,b['at'])>bisect_right(points,a['at']):
                    reasons.add(reason)
            bad=bool(reasons)
            if parts and parts[-1]['coverage_reasons']==reasons:
                parts[-1]['observations'].append(b)
                parts[-1]['coverage_reasons'].update(reasons)
            else:
                parts.append({**group, 'observations':[a,b], 'coverage_blocked':bad,'coverage_reasons':reasons})
        if len(parts)==1 and not parts[0]['coverage_blocked']:
            result.append(group)
            continue
        for index,part in enumerate(parts):
            start,end=part['observations'][0]['at'],part['observations'][-1]['at']
            part['id']=group['id']+':coverage:'+str(index)
            part['boundary']=start
            part['rejected']=[r for r in group.get('rejected',[]) if start<r['at']<=end]
            if index:part.pop('manual_reset',None)
            if part['coverage_blocked']:part['reason']='비용, 소모 대응 미확인 구간'
            elif index:part['reason']='대응 미확인 경계 이후 관측'
            result.append(part)
    return result


def _clip_quota_interval(cycle, start=None, end=None, *, selection=None):
    endpoints=selection[0] if selection is not None else [r for r in cycle.get('endpoints',[]) if
               (start is None or r[0]>=start) and (end is None or r[0]<=end)]
    if len(endpoints)<2:return None
    lo,hi=endpoints[0][0],endpoints[-1][0]
    candidates=selection[1] if selection is not None else [r for r in cycle.get('cost_rows',[]) if lo<r['ts']<=hi]
    rows=[r for r in candidates if r.get('request_start') is None or r['request_start']>=lo]
    models=[]
    for model in cycle['models']:
        own=[r for r in rows if r['model']==model['model'] and r['service_tier']==model['service_tier']]
        known=[r['cost'] for r in own if r['cost'] is not None]
        if own:models.append({**model,'calls':len(own),'cost':sum(known) if known else None,'priced':len(known)})
    eligible=[r for r in rows if ALIASES.get(r['model'],r['model']) not in cycle['separate_models']]
    alternatives=[token_cost({**r,'service_tier':'Standard'})['cost'] if r['service_tier']=='미확인'
                  else r['cost'] for r in eligible]
    pending=[at for at in cycle.get('pending_usage_times',[]) if lo<at<=hi]
    complete = (cycle['cost_complete_at'] is not None and cycle['cost_complete_at'] >= hi) if 'cost_complete_at' in cycle else cycle.get('cost_complete', False)
    return {**cycle,'cost_complete':complete,'pending_usage_times':pending,'pending_usage_calls':len(pending),
            'start':lo,'end':hi,'delta':endpoints[-1][1]-endpoints[0][1],'models':models,
            'endpoints':endpoints,'cost_rows':rows,'used_start':endpoints[0][1],'used_end':endpoints[-1][1],
            'cost':(sum(r['cost'] for r in eligible if r['cost'] is not None) if any(r['cost'] is not None for r in eligible)
                    else 0 if not eligible and complete and not pending else None)
                   if cycle.get('forward_tracking') else
                   sum(r['cost'] for r in eligible) if eligible and all(r['cost'] is not None for r in eligible) else None,
            'priced_calls':sum(r['cost'] is not None for r in eligible),
            'boundary_excluded_calls':(cycle.get('boundary_excluded_calls',0) if lo==cycle['start'] else 0)+len(candidates)-len(rows),
            'mode_standard_cost':sum(alternatives) if alternatives and all(c is not None for c in alternatives) else None}


def _quota_clipper(cycle):
    """Build one native time index for the many local intervals in a cycle."""
    from .native_backend import get_native
    native = get_native()
    if native is not None and len(cycle.get('endpoints',[])) >= 128:
        try:
            index = native.QuotaClipIndex(cycle.get('endpoints',[]), cycle.get('cost_rows',[]))
        except (ValueError, OverflowError):
            pass
        else:
            return lambda start,end: _clip_quota_interval(cycle,start,end,selection=index.select(start,end))
    return lambda start,end: _clip_quota_interval(cycle,start,end)


def _continuous_quota_intervals(cycles):
    """Exclude unobserved edges and their costs, retaining both observed sides."""
    from .quota_tracking import OBSERVATION_FRESHNESS
    for cycle in cycles:
        runs=[]
        for endpoint in cycle.get('endpoints',[]):
            at=endpoint[0]
            if not runs or at-runs[-1][1]>OBSERVATION_FRESHNESS:
                runs.append([at,at])
            else:runs[-1][1]=at
        if len(runs)<=1:
            yield cycle
        else:
            clip=_quota_clipper(cycle)
            for lo,hi in runs:
                clipped=clip(lo,hi)
                if clipped is not None:
                    yield {**clipped,'id':f"{cycle['id']}:observed:{lo}",
                           'observation_gaps_excluded':True}


def _quota_scope(report, start, end, requests, cache=None, cache_readonly=False):
    """Select observed intervals before calculating their exact request attribution."""
    from .quota_exclusions import retained_intervals
    from .quota_cancellation import checkpoint
    checkpoint()
    current_account = report.get('account') or next(
        (c.get('account') for c in report.get('cycles', []) if c.get('account')), '')
    scoped=[]
    cycles=(cycle for cycle in report.get('cycles',[]) if
            (start is None or cycle['end']>=start) and (end is None or cycle['start']<=end))
    for cycle in retained_intervals(_continuous_quota_intervals(cycles), report, _clip_quota_interval):
        if (start is not None and cycle.get('start',0)<start) or (end is not None and cycle.get('end',float('inf'))>end):
            cycle=_clip_quota_interval(cycle,start,end)
            if cycle is None:continue
        scoped.append(cycle)
    observed_count=len(scoped)
    attribution=[]
    account_delta=[max(0,c['delta']) for c in scoped if not current_account or c.get('account')==current_account]
    if report.get('tracking') is not None:
        from .quota_attribution import local_request_intervals, RequestWindows
        requests=requests if requests is not None else RequestWindows(report.get('request_windows',[]))
        local=[]
        for cycle in scoped:
            checkpoint()
            if current_account and cycle.get('account')!=current_account:continue
            points=cycle.get('endpoints',[])
            overlapping=list(requests.overlapping(points[0][0],points[-1][0])) if points else []
            key=signature=cached=None
            if cache is not None and 'id' in cycle:
                key=(cycle['id'],cycle['start'],cycle['end'])
                signature=({**{k:v for k,v in cycle.items() if k not in ('cost_complete_at','cycle_end')},
                            'cost_complete_at':min(cycle['cost_complete_at'],cycle['end']) if cycle.get('cost_complete_at') is not None else None},overlapping)
                cached=cache.get(key)
            if cached and cached[0]==signature:
                parts,diagnostics=cached[1:3]
                parts=[{**part,**{k:cycle[k] for k in ('cost_complete_at','cycle_end') if k in cycle}} for part in parts]
            else:
                parts,diagnostics=local_request_intervals(cycle,overlapping,_clip_quota_interval,_quota_clipper)
                if key is not None and not cache_readonly:
                    weight=len(points)+len(cycle.get('cost_rows',[]))+len(overlapping)
                    weight+=sum(1+len(part.get('endpoints',[]))+len(part.get('cost_rows',[])) for part in parts)
                    if weight<=131072:cache[key]=(signature,parts,diagnostics,weight)
                    while len(cache)>256 or sum(item[3] for item in cache.values())>131072:
                        cache.pop(next(iter(cache)))
            local.extend(parts)
            attribution.append(diagnostics)
        scoped=local
    return scoped,attribution,account_delta,observed_count


class _PreparedQuotaStatistics:
    """Reuse complete accounting intervals across summaries and changed reports.

    Only ledger-issued immutable cycle versions enable this path. Arbitrary
    mutable report dictionaries retain the ordinary calculation path. Entries
    share the endpoint/call objects already owned by report outputs; the extra
    lists and interval dictionaries are conservatively budgeted at 64 MiB.
    """
    MAX_BYTES=64*1024*1024
    MAX_CYCLES=4096

    def __init__(self,report,requests,previous=None):
        from .quota_exclusions import active_exclusions
        self.entries={};self.weight=0
        self.policy=(report.get('exclusions'),report.get('cycle_clears'),report.get('account'))
        versions=report.get('_cycle_versions',{})
        context=(report.get('account'),report.get('tracking') is not None)
        old=previous.entries if previous is not None else {}
        for cycle in report.get('cycles',[]):
            version=versions.get(cycle.get('id'))
            if version is None:continue
            minimum=256+16*(len(cycle.get('endpoints',[]))+len(cycle.get('cost_rows',[])))
            if len(self.entries)>=self.MAX_CYCLES or self.weight+minimum>self.MAX_BYTES:continue
            start,end=cycle['start'],cycle['end']
            exclusions=[r for r in active_exclusions(report,cycle.get('account','')) if r['start']<end and r['end']>start]
            clears=[r for r in report.get('cycle_clears',[]) if r['account']==cycle.get('account','') and r['start']<end and r['end']>start]
            signature=(context,version,exclusions,clears,requests.signature(start,end) if requests is not None else None)
            signature=hashlib.sha256(pickle.dumps(signature,protocol=5)).digest()
            prior=old.get(cycle['id'])
            if prior and prior[0]==signature:
                value=prior[1];weight=prior[2]
            else:
                value=_quota_scope({**report,'cycles':[cycle]},None,None,requests)
                weight=256+sum(2048+16*(len(row.get('endpoints',[]))+len(row.get('cost_rows',[]))) for row in value[0])
            if len(self.entries)<self.MAX_CYCLES and self.weight+weight<=self.MAX_BYTES:
                self.entries[cycle['id']]=(signature,value,weight)
                self.weight+=weight

    def scope(self,report,start,end,requests,cache,cache_readonly):
        if self.policy!=(report.get('exclusions'),report.get('cycle_clears'),report.get('account')):
            return _quota_scope(report,start,end,requests,cache,cache_readonly)
        rows=[];diagnostics=[];delta=[];count=0
        for cycle in report.get('cycles',[]):
            if (start is not None and cycle['end']<start) or (end is not None and cycle['start']>end):continue
            cached=self.entries.get(cycle.get('id'))
            whole=(start is None or start<=cycle['start']) and (end is None or end>=cycle['end'])
            value=(cached[1] if cached and whole else
                   _quota_scope({**report,'cycles':[cycle]},start,end,requests,cache,cache_readonly))
            parts,attribution,account_delta,observed_count=value
            if cached and whole:
                metadata={key:cycle[key] for key in ('cost_complete_at','cycle_end') if key in cycle}
                parts=[{**part,**metadata} if any(part.get(key)!=value for key,value in metadata.items()) else part for part in parts]
            rows.extend(parts);delta.extend(account_delta);count+=observed_count
            diagnostics.extend(attribution)
        return rows,diagnostics,delta,count


def quota_statistics(report, start=None, end=None, include_mode_assumptions=False, *, _requests=None, _cache=None, _cache_readonly=False):
    """Use complete disjoint intervals; preserve the base result under assumptions."""
    from .analytics import quantile
    intervals=[]
    current_account=report.get('account') or next(
        (c.get('account') for c in report.get('cycles',[]) if c.get('account')), '')
    prepared=report.get('_prepared_statistics')
    scoped,diagnostics,deltas,count=(prepared.scope(report,start,end,_requests,_cache,_cache_readonly)
        if prepared is not None else _quota_scope(report,start,end,_requests,_cache,_cache_readonly))
    attribution=dict(idle_delta=0,pending_delta=0,unmatched_calls=0,pending_windows=0)
    for values in diagnostics:
        for key in attribution:attribution[key]+=values[key]
    account_delta=sum(deltas)
    user_removed_all=bool(report.get('cycles')) and bool(report.get('exclusions')) and not count
    for cycle in scoped:
        blockers = list(cycle['blocked'])
        if current_account and cycle.get('account') and cycle['account'] != current_account:
            blockers.append('다른 계정의 관측 구간')
        result = estimate(cycle['cost'], cycle['delta'], 1, blockers)
        if cycle.get('forward_tracking') and cycle['delta'] == 0 and not blockers:
            result['reasons'] = []
        waiting = cycle.get('pending') or cycle.get('reset_pending') or any('수집' in r and '대기' in r for r in blockers)
        insufficient = (cycle['delta'] == 0 and all(
            '소모율 변화' in r or r in ('대응하는 모델 호출 없음','구독 가치 환산액 미확인')
            for r in result['reasons']))
        assumption_blockers = [r for r in blockers if not r.startswith('요청 모드 미확인')]
        assumption = estimate(cycle.get('mode_standard_cost'),cycle['delta'],1,assumption_blockers)
        assumption_only = bool(cycle.get('unknown_mode_calls')) and not assumption['reasons']
        status = ('잠정' if cycle.get('provisional') else '사용') if not result['reasons'] else (
            '수집 대기' if waiting else '가정 필요' if assumption_only else
            '누적 대기' if insufficient else '제외')
        if cycle.get('forward_tracking'):
            status = ', '.join(cycle.get('assumptions', []))
        intervals.append({**cycle, 'per_percent':result['per_percent'],
            'excluded':result['reasons'], 'sensitivity':result['sensitivity'], 'status':status,
            'assumed_per_percent':assumption['per_percent'] if assumption_only else None,
            'calls':sum(m['calls'] for m in cycle['models'] if not m['separate'])+cycle.get('pending_usage_calls',0)})
    valid = [r for r in intervals if r['per_percent'] is not None or
             (r.get('forward_tracking') and r['delta']==0 and r['cost'] is not None and not r['excluded'])]
    observed = [r for r in intervals if not r.get('forward_tracking') or not current_account or r.get('account')==current_account]
    forward = report.get('tracking') is not None
    total_cost = sum(r['cost'] for r in valid)
    total_delta = sum(r['delta'] for r in valid)
    if forward:
        # Preserve all measured consumption in the denominator. A known cost
        # subtotal remains useful with its priced/total count, but absent costs
        # must not turn into zero dollars.
        known_costs = [r['cost'] for r in observed if r['cost'] is not None]
        total_cost = sum(known_costs) if known_costs else 0 if not observed and (report['tracking'].get('complete') or user_removed_all) else None
        if not observed and attribution['pending_windows']:total_cost=None
        total_delta = sum(max(0,r['delta']) for r in observed)
    values = [r['per_percent'] for r in valid if r['per_percent'] is not None]
    distribution = {'label':'관측 구간', 'group':('quota','per_percent'),
        'n':len(values), 'missing':0, 'unknown_mode_samples':0,
        'mean':sum(values)/len(values) if values else None,
        **{name:quantile(values,q) for name,q in
           (('p10',.1),('q1',.25),('median',.5),('q3',.75),('p90',.9))},
        'points':values if len(values)<10 else []}
    additional = [r for r in intervals if r['assumed_per_percent'] is not None] if include_mode_assumptions else []
    assumed_delta = total_delta + sum(r['delta'] for r in additional if not r.get('forward_tracking'))
    assumed_cost = (total_cost or 0) + sum(r['mode_standard_cost']-(r['cost'] or 0)
        if r.get('forward_tracking') else r['mode_standard_cost'] for r in additional)
    return {'per_percent':total_cost/total_delta if total_delta and total_cost is not None else None,
        'cost':total_cost, 'delta':total_delta, 'valid':len(valid), 'total':len(intervals),
        # Each disconnected interval contributes its own endpoint rounding error.
        # This is a sensitivity scenario, not a statistical confidence interval.
        'precision_range':(total_cost/(total_delta+len(valid)),total_cost/(total_delta-len(valid)))
            if valid and total_cost is not None and total_delta>len(valid) else None,
        'excluded':len(intervals)-len(valid), 'calls':sum(r['calls'] for r in valid),
        'historical':sum(not r['live'] for r in valid), 'intervals':intervals,
        'pending':sum(r['status']=='수집 대기' for r in intervals),
        'provisional':sum(r['status']=='잠정' for r in intervals),
        'assumed':len(additional),
        'assumed_per_percent':(assumed_cost/assumed_delta
                              if additional and assumed_delta else None),
        'insufficient':sum(r['status']=='누적 대기' for r in intervals),
        'observed_calls':sum(r['calls'] for r in observed),
        'observed_priced_calls':sum(r.get('priced_calls',sum(m.get('priced',m.get('calls',0)) for m in r['models'] if not m['separate'])) for r in observed),
        'boundary_excluded_calls':sum(r.get('boundary_excluded_calls',0) for r in observed),
        'observed_delta':sum(max(0,r['delta']) for r in observed),
        'observed_cost':total_cost if forward else sum(sum(m.get('cost') or 0 for m in r['models'] if not m['separate']) for r in observed),
        'mode_fast_scenario':None, 'distribution':distribution,
        'account_delta':account_delta,'attribution':attribution}


def _quota_value_rows(group, summary, intervals, display_ranges, exclusions, account, forward):
    """Derive exact chart rows only for a requested allowance period."""
    result=[];start=group['start']
    display_position=0
    events = []
    for interval in intervals:
        endpoints = [(at, used) for at, used in interval.get('endpoints', [])
                     if interval['start'] <= at <= interval['end']]
        # Older reports without granular evidence can expose only their
        # measured endpoint, never a fabricated interpolation.
        if not endpoints:
            endpoints = [(interval['end'], interval.get('used_end') or 0)]
        costs = sorted(((r['ts'], r['cost']) for r in interval.get('cost_rows', [])
                       if interval['start'] < r['ts'] <= interval['end'] and
                       ALIASES.get(r['model'], r['model']) not in interval['separate_models']), key=lambda r:r[0])
        position = 0
        subtotal = 0.0
        priced = missing = 0
        old_cost = old_delta = old_known = 0
        for at, used in endpoints:
            while position < len(costs) and costs[position][0] <= at:
                value = costs[position][1]
                if value is None:
                    missing += 1
                else:
                    subtotal += value
                    priced += 1
                position += 1
            delta = max(0, used-endpoints[0][1])
            pending = any(t <= at for t in interval.get('pending_usage_times', []))
            # Consumption needs a recorded cost; a collection checkpoint is not zero-dollar evidence.
            cost = (subtotal if priced else 0 if not position and delta == 0 and
                    ((interval['cost_complete_at'] is not None and interval['cost_complete_at'] >= at) if 'cost_complete_at' in interval else interval.get('cost_complete')) and not pending else None)
            if not forward and (missing or not priced):
                cost = None
            if at == interval['end']:
                cost, delta = interval['cost'], max(0, interval['delta'])
            valid = forward or not estimate(cost, delta, 1, interval['blocked'])['reasons']
            known = int(valid and cost is not None)
            value = cost if known else 0
            consumed = delta if valid else 0
            events.append((at, value-old_cost, consumed-old_delta, known-old_known))
            old_cost, old_delta, old_known = value, consumed, known
    events.sort(key=lambda r: r[0])
    cost = delta = known = position = 0
    confirmed_cost = confirmed_delta = 0
    last_ratio = None
    for row in group['rows']:
        while display_position<len(display_ranges) and display_ranges[display_position][1]<row['at']:display_position+=1
        local_observed=(row.get('measurement_reset') or not forward or
                        display_position<len(display_ranges) and display_ranges[display_position][0]<=row['at']<=display_ranges[display_position][1])
        excluded = [r for r in exclusions if r['start'] < row['at'] <= r['end']]
        while position < len(events) and events[position][0] <= row['at']:
            _, amount, consumed, confirmed = events[position]
            cost += amount
            delta += consumed
            known += confirmed
            position += 1
        matches = not account or row.get('account') == account
        value = max(0, cost) if known and matches else None
        if exclusions and matches and not intervals and summary['cost'] == 0:
            value = 0.0
        if row['reset_kind'] and matches:
            value = 0.0
        ratio = value/delta*100 if value is not None and delta > 0 else None
        value_held = ratio is None and last_ratio is not None and matches
        if ratio is None:
            ratio = last_ratio if matches else None
        else:
            last_ratio = ratio
            confirmed_cost, confirmed_delta = value, delta
        result.append({**row, 'cycle_start': start, 'cycle_cost': value,
                       'cycle_value': ratio, 'cycle_delta':delta,
                       'confirmed_cost':confirmed_cost, 'confirmed_delta':confirmed_delta, 'value_held':value_held,
                       'local_observed':local_observed,'local_range':display_position,
                       'user_excluded':bool(excluded),
                       'user_exclusion_end':bool(excluded) and all(r['end']==row['at'] for r in excluded),
                       'exclusion_ids':[r.get('id') for r in excluded],
                       'exclusion_reasons':[r.get('reason', '') for r in excluded],
                       'cycle_partial': group['partial']})
    return result


def quota_value_history(report, rows, *, _requests=None, _cache=None, _statistics_cache=None, _period_sink=None, _groups=None, _defer_rows=False):
    """Align monetary evidence to the chart's *observed* reset boundaries.

    Reset classification belongs to history_rows/ResetTracker. Storage intervals
    are accounting segments, not necessarily whole allowance cycles. Preserve
    those segments and the existing estimator, aggregating them only for display.
    """
    if not rows and _groups is None:
        return [], []
    account = report.get('account') or next(
        (c.get('account') for c in report.get('cycles', []) if c.get('account')), '')
    def groups():
        group=None
        for row in rows:
            if group is None or row['reset_kind']:
                if group is not None:yield group
                group={'start':row['at'],'reset_kind':row['reset_kind'],
                       'partial':not bool(row['reset_kind']),'rows':[]}
            group['rows'].append(row)
        if group is not None:yield group
    def neighbors():
        source=iter(groups() if _groups is None else _groups);group=next(source,None)
        while group is not None:
            following=next(source,None)
            yield group,following
            group=following

    from .quota_exclusions import active_exclusions
    exclusions = active_exclusions(report, account)
    result, periods = [], []
    cycles = report.get('cycles', [])
    forward = report.get('tracking') is not None
    if forward and _requests is None:
        from .quota_attribution import RequestWindows
        _requests=RequestWindows(report.get('request_windows',[]))
    previous_cache=dict(_cache) if _cache is not None else {}
    if _cache is not None:_cache.clear()
    for group,following in neighbors():
        start = group['start']
        end = (math.nextafter(following['start'],-math.inf) if following is not None else
               group['_last_at'] if '_last_at' in group else group['rows'][-1]['at'])
        own = [c for c in cycles if c['end'] >= start and c['start'] <= end]
        signature=None
        if _cache is not None:
            prepared_statistics=report.get('_prepared_statistics')
            versions=prepared_statistics.entries if prepared_statistics is not None else {}
            versioned=all(c.get('id') in versions for c in own)
            inputs=([(c['id'],versions[c['id']][0]) for c in own] if versioned else
                    [{**{k:v for k,v in c.items() if k not in ('cost_complete_at','cycle_end')},
                      'cost_complete_at':min(c['cost_complete_at'],c['end']) if c.get('cost_complete_at') is not None else None}
                     for c in own])
            signature=(account,forward,group.get('_signature',group),inputs,end,
                       [r for r in exclusions if r['start']<end and r['end']>start],
                       [r for r in report.get('cycle_clears',[]) if r['start']<end and r['end']>start],
                       (_requests.signature(start,end) if versioned else list(_requests.overlapping(start,end))) if _requests is not None else None,
                       (report.get('tracking') or {}).get('complete'))
            signature=hashlib.sha256(pickle.dumps(signature,protocol=5)).digest()
            cached=previous_cache.get(start)
            if cached and cached[0]==signature and (_period_sink is not None or cached[1] is not None):
                prepared=cached[1]
                period={**cached[2],'current':following is None} if cached[2] is not None else None
                if period is not None:periods.append(period)
                if _period_sink is None:result.extend(prepared)
                else:_period_sink(prepared,period,True)
                _cache[start]=cached
                continue
        if group.get('rows') is None and not (_defer_rows and '_source' in group):
            group={**group,'rows':list(group['_load']())}
        result_start=len(result);period_start=len(periods)
        summary = quota_statistics({**report, 'account': account, 'cycles': own}, start, end, _requests=_requests,_cache=_statistics_cache)
        intervals = [c for c in summary['intervals']
                     if not account or not c.get('account') or c['account'] == account]
        ranges=sorted((c['start'],c['end']) for c in intervals)
        display_ranges = ranges
        if exclusions:
            original = quota_statistics({**report, 'account': account, 'cycles': own, 'exclusions': []}, start, end, _requests=_requests,_cache=_statistics_cache)
            display_ranges = sorted((c['start'], c['end']) for c in original['intervals'])
        if _defer_rows:
            prepared=dict(source=group['_source'] if '_source' in group else _PackedRows(group['rows']),
                start=start,partial=group['partial'],cost=summary['cost'],intervals=intervals,
                display_ranges=display_ranges,exclusions=exclusions,account=account,forward=forward)
        else:
            prepared=_quota_value_rows(group,summary,intervals,display_ranges,exclusions,account,forward)
            result.extend(prepared)
        present=group['_matches_account'] if '_matches_account' in group else any(not account or r.get('account')==account for r in group['rows'])
        if present:
            value = summary['per_percent']
            periods.append({'start': start, 'end': group['_last_at'] if '_last_at' in group else group['rows'][-1]['at'],
                            'reset_kind': group['reset_kind'], 'partial': group['partial'],
                            'current': following is None,
                            'cost': summary['cost'] if summary['total'] or exclusions else
                                    0 if group['reset_kind'] else None,
                            'delta': summary['delta'],
                            'priced_calls': summary['observed_priced_calls'],
                            'calls': summary['observed_calls'],
                            'value': value*100 if value is not None else None})
            periods[-1]['cost_intervals']=intervals
            if _defer_rows:
                times=group.get('_times')
                periods[-1]['_has_history']=bool(not forward or group['reset_kind']=='measurement_reset' or
                    (any((position:=bisect_left(times,left))<len(times) and times[position]<=right for left,right in display_ranges)
                     if times is not None else any(left<=row['at']<=right for row in group['rows'] for left,right in display_ranges)))
        if _cache is not None:
            # The deferred sink owns packed classified inputs and the exact
            # interval references. No additional decoded rows are retained.
            _cache[start]=(signature,None if _period_sink else prepared,
                           periods[-1] if len(periods)>period_start else None)
        if _period_sink is not None:
            _period_sink(prepared,periods[-1] if len(periods)>period_start else None,False)
            del result[result_start:]
    return result, periods


class QuotaLedger:
    CACHE_KIB=4096
    HISTORY_CACHE_KIB=128*1024
    IMPORT_CACHE_KIB=64*1024
    INPUT_CACHE_BYTES=64*1024*1024
    REQUEST_CACHE_BYTES=64*1024*1024
    REQUEST_SORT_ROWS=32768
    SYNC_BATCH_ROWS=128
    ARCHIVE_COMPACTION_ROWS=512
    READER_SCHEMA_VERSION=4
    FUTURE_COLUMNS=(('observations',('at',)),('window_observations',('at',)),
        ('manual_resets',('at',)),('calls',('ts',)),('tracking_controls',('at',)),
        ('tracking_observations',('received',)),('tracking_tasks',('start','end')),
        ('tracking_wire',('start','end')))
    def __init__(self, path, *, pack_history=False, weekly_history_only=False, reader=False):
        self._pack_history=pack_history
        self._weekly_history_only=weekly_history_only
        self.reader=reader
        self.path = Path(path)
        if not reader:self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = (sqlite3.connect(self.path.resolve().as_uri()+'?mode=rw',uri=True,timeout=8) if reader else
                   sqlite3.connect(self.path,timeout=8))
        try:
            self.db.execute(f'PRAGMA cache_size=-{self.CACHE_KIB}')
            if reader:
                self.db.row_factory=sqlite3.Row
                ready=self.db.execute('SELECT version,price_id FROM quota_reader_ready WHERE id=1').fetchone()
                if ready is None or tuple(ready)!=(self.READER_SCHEMA_VERSION,PRICE_ID):
                    raise sqlite3.OperationalError('사용한도 저장소를 준비하고 있습니다.')
            else:self._initialize()
        except Exception:
            self.db.close()
            raise

    def _initialize(self):
        self.db.row_factory = sqlite3.Row
        from .quota_tracking_store import initialize
        initialize(self.db)
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS observations(
                id TEXT PRIMARY KEY, home TEXT, at REAL, source TEXT, account TEXT,
                used REAL, reset REAL, minutes INTEGER, separate TEXT);
            CREATE INDEX IF NOT EXISTS observations_home_at ON observations(home,at);
            CREATE TABLE IF NOT EXISTS calls(
                home TEXT, uid TEXT, sid TEXT, ts REAL, model TEXT, input INTEGER,
                cached INTEGER, written INTEGER, output INTEGER, reasoning INTEGER,
                cost REAL, price_id TEXT, signature TEXT, PRIMARY KEY(home,uid));
            CREATE INDEX IF NOT EXISTS calls_home_ts ON calls(home,ts);
            CREATE INDEX IF NOT EXISTS calls_home_sid ON calls(home,sid);
            CREATE TABLE IF NOT EXISTS prices(id TEXT PRIMARY KEY, data TEXT);
            CREATE TABLE IF NOT EXISTS state(home TEXT PRIMARY KEY, at REAL, loading INTEGER, unclassified INTEGER);
            CREATE TABLE IF NOT EXISTS modes(home TEXT, sid TEXT, turn TEXT, mode TEXT,
                PRIMARY KEY(home,sid,turn));
            CREATE TABLE IF NOT EXISTS manual_resets(
                id TEXT PRIMARY KEY, home TEXT NOT NULL, at REAL NOT NULL,
                account TEXT NOT NULL, baseline_after REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS manual_resets_home_at ON manual_resets(home,at);
            CREATE TABLE IF NOT EXISTS observation_context(
                id TEXT PRIMARY KEY, plan TEXT NOT NULL, bucket TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS window_observations(
                id TEXT PRIMARY KEY, home TEXT NOT NULL, at REAL NOT NULL,
                source TEXT NOT NULL, account TEXT NOT NULL, plan TEXT NOT NULL,
                bucket TEXT NOT NULL, window TEXT NOT NULL, used REAL,
                reset REAL, minutes INTEGER, separate TEXT);
            CREATE INDEX IF NOT EXISTS window_observations_home_at ON window_observations(home,at);
            CREATE TABLE IF NOT EXISTS weekly_window_refs(
                window_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS weekly_window_refs_observation ON weekly_window_refs(observation_id);
            CREATE TABLE IF NOT EXISTS coverage_issues(
                home TEXT, sid TEXT, start REAL, end REAL, reason TEXT,
                PRIMARY KEY(home,sid,reason));
            CREATE TABLE IF NOT EXISTS quota_exclusions(
                id TEXT PRIMARY KEY, home TEXT NOT NULL, account TEXT NOT NULL,
                start REAL NOT NULL, end REAL NOT NULL, reason TEXT NOT NULL,
                period_start REAL NOT NULL, period_end REAL NOT NULL,
                created_at REAL NOT NULL, restored_at REAL,
                CHECK(start < end));
            CREATE INDEX IF NOT EXISTS quota_exclusions_scope
                ON quota_exclusions(home,account,start,end);
            CREATE TABLE IF NOT EXISTS collection_activity_cursor(
                id INTEGER PRIMARY KEY CHECK(id=1),source TEXT NOT NULL,revision INTEGER NOT NULL);
        ''')
        # Account observations can join a new home at a future timestamp.
        # Other boundaries use existing home/time indexes where available.
        for name,table,columns in (
                ('tracking_observations_future_received','tracking_observations','received'),
                ('tracking_tasks_home_end','tracking_tasks','home,end'),
                ('tracking_wire_home_start','tracking_wire','home,start'),
                ('tracking_wire_home_response','tracking_wire','home,response'),
                ('tracking_wire_home_end','tracking_wire','home,end')):
            self.db.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})')
        self.db.execute('begin immediate')
        if 'complete_at' not in {r[1] for r in self.db.execute('pragma table_info(state)')}:
            self.db.execute('alter table state add column complete_at REAL')
            self.db.execute('update state set complete_at=at where loading=0')
        if 'service_tier' not in {r[1] for r in self.db.execute('pragma table_info(calls)')}:
            self.db.execute("alter table calls add column service_tier TEXT NOT NULL DEFAULT '미확인'")
        self.db.execute('insert or ignore into prices values(?,?)', (PRICE_ID, PRICE_SNAPSHOT))
        self.db.commit()
        self.revisions = {}
        self.mode_offsets = {}
        self.mode_backfill = {}
        self.db.execute('create table if not exists cost_archive(home text,uid text,price_id text,signature text,cost real,service_tier text,at real,primary key(home,uid,price_id,signature)) without rowid')
        self.db.execute('create table if not exists mode_scan(home text primary key, latest integer, before_id integer)')
        has_mode_events=bool(self.db.execute("select 1 from sqlite_master where type='table' and name='mode_events'").fetchone())
        self.db.execute('''create table if not exists mode_events(
            home text, id integer, sid text, turn text, data text,
            primary key(home,id,sid,turn))''')
        from .request_modes import MODE_LOG_FORMAT
        self.db.execute('CREATE TABLE IF NOT EXISTS mode_scan_format(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)')
        mode_format=self.db.execute('SELECT version FROM mode_scan_format WHERE id=1').fetchone()
        if not has_mode_events or mode_format is None or mode_format[0]!=MODE_LOG_FORMAT:
            # Keep verified evidence, and resume the existing bounded reverse
            # scan once so rows rejected by an older parser can be recovered.
            self.db.execute('update mode_scan set latest=-1,before_id=0')
            self.db.execute('INSERT OR REPLACE INTO mode_scan_format VALUES(1,?)',(MODE_LOG_FORMAT,))
        if has_mode_events:
            for home,latest,before in self.db.execute('select * from mode_scan'):
                if latest<0:continue
                self.mode_offsets[home]=latest
                self.mode_backfill[home]=before
        self._price_equivalent = _PriceEquivalence(self.db, PRICE_ID, PRICE_SNAPSHOT)
        self.db.create_function('codexon_price_equivalent', 1, self._price_equivalent)
        # Filter before constructing Python rows. Acquire write ownership before
        # streaming actual repricing so another writer cannot invalidate a read
        # snapshot halfway through upgrading it to a write transaction.
        stale='price_id!=? and not codexon_price_equivalent(price_id)'
        if self.db.execute('select 1 from calls where '+stale+' limit 1', (PRICE_ID,)).fetchone():
            if not self.db.in_transaction:self.db.execute('BEGIN IMMEDIATE')
            for old in self.db.execute('select * from calls where '+stale, (PRICE_ID,)):
                self.db.execute('insert or ignore into cost_archive values(?,?,?,?,?,?,?)',
                    (old['home'],old['uid'],old['price_id'],old['signature'],old['cost'],old['service_tier'],time.time()))
                self.db.execute('update calls set cost=?,price_id=? where home=? and uid=?',
                    (_stored_cost(old),PRICE_ID,old['home'],old['uid']))
        self.db.commit()
        self._mode_dependency_profiles=None
        self._mode_dependency_digests={}
        self.modes = _ModeValues(self._mode_dependency_changed,
            {(r['home'],r['sid'],r['turn']):r['mode'] for r in self.db.execute('select * from modes')})
        self.mode_revisions = {}
        self.mode_evidence = _ModeValues(self._mode_dependency_changed)
        for home in {r[0] for r in self.db.execute('select distinct home from mode_events')}:
            self.rebuild_mode_evidence(home)
        self.db.commit()

        self.db.execute('''create table if not exists quota_cycle_clears(
            home text, account text, start real, end real, baseline text,
            primary key(home,account,start))''')
        self.db.commit()
        self._initialize_report_revision()
        from .quota_projection import initialize as initialize_projection
        initialize_projection(self.db)
        from .quota_archive import initialize as initialize_archive
        initialize_archive(self.db)
        self.db.execute('CREATE TABLE IF NOT EXISTS quota_reader_ready(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER,price_id TEXT)')
        ready=self.db.execute('SELECT version,price_id FROM quota_reader_ready WHERE id=1').fetchone()
        if ready is None or tuple(ready)!=(self.READER_SCHEMA_VERSION,PRICE_ID):
            self.db.execute('INSERT OR REPLACE INTO quota_reader_ready VALUES(1,?,?)',(self.READER_SCHEMA_VERSION,PRICE_ID))
        self.db.commit()
        self._archive_compaction_pending=True
        self._compact_cost_archive()
        from .quota_payloads import PayloadMigration
        self._payload_migration=PayloadMigration(self.db,self.path)
        self._payload_migration.step()

    def _compact_cost_archive(self):
        """Resume a bounded copy of the append-only price history.

        Its large primary key used to be stored twice. Readers and older
        writers keep the same SQL contract after the atomic table replacement.
        Keep the original authoritative until all rows, including concurrent
        appends, have been copied. Free pages are reused by SQLite; this does
        not schedule a blocking VACUUM in the collection loop.
        """
        if not getattr(self,'_archive_compaction_pending',False):return
        try:
            self.db.execute('BEGIN IMMEDIATE')
            schema=self.db.execute("SELECT sql FROM sqlite_master WHERE name='cost_archive'").fetchone()[0]
            if 'without rowid' in schema.lower():
                self.db.rollback();self._archive_compaction_pending=False;return
            # Leave room for an additional database-sized staging copy and WAL.
            # Low-space maintenance yields to new usage records and retries on
            # a later synchronization; it never consumes the last free pages.
            from shutil import disk_usage
            if disk_usage(self.path.parent).free<self.path.stat().st_size+16*1024*1024:
                self.db.rollback();return
            self.db.execute('''CREATE TABLE IF NOT EXISTS cost_archive_packing(
                home text,uid text,price_id text,signature text,cost real,service_tier text,at real,
                primary key(home,uid,price_id,signature)) WITHOUT ROWID''')
            self.db.execute('CREATE TABLE IF NOT EXISTS cost_archive_progress(id INTEGER PRIMARY KEY CHECK(id=1),last_rowid INTEGER)')
            self.db.execute('INSERT OR IGNORE INTO cost_archive_progress VALUES(1,NULL)')
            cursor=self.db.execute('SELECT last_rowid FROM cost_archive_progress WHERE id=1').fetchone()[0]
            condition='' if cursor is None else ' WHERE rowid>?'
            params=() if cursor is None else (cursor,)
            rows=self.db.execute('SELECT rowid,* FROM cost_archive'+condition+' ORDER BY rowid LIMIT ?',
                                 (*params,self.ARCHIVE_COMPACTION_ROWS)).fetchall()
            self.db.executemany('INSERT INTO cost_archive_packing VALUES(?,?,?,?,?,?,?)',(tuple(row)[1:] for row in rows))
            if rows:
                self.db.execute('UPDATE cost_archive_progress SET last_rowid=? WHERE id=1',(rows[-1][0],))
            if len(rows)<self.ARCHIVE_COMPACTION_ROWS:
                original=self.db.execute('SELECT count(*) FROM cost_archive').fetchone()[0]
                copied=self.db.execute('SELECT count(*) FROM cost_archive_packing').fetchone()[0]
                if original!=copied:raise sqlite3.IntegrityError('Price history copy is incomplete')
                self.db.execute('DROP TABLE cost_archive')
                self.db.execute('ALTER TABLE cost_archive_packing RENAME TO cost_archive')
                self.db.execute('DROP TABLE cost_archive_progress')
                self._archive_compaction_pending=False
            self.db.commit()
        except (sqlite3.Error,OSError) as error:
            self.db.rollback()
            # Optional maintenance must not prevent accounting or observation.
            # The next process can resume from the last committed batch.
            self._archive_compaction_pending=False
            from .quota_diagnostics import record_failure
            record_failure(self.path,'cost_archive_compaction',error)

    def _initialize_report_revision(self):
        # One durable scalar, updated by all writers including older app
        # connections. Heartbeats have separate freshness handling below.
        self.db.execute('CREATE TABLE IF NOT EXISTS report_revision(id INTEGER PRIMARY KEY,value INTEGER NOT NULL)')
        self.db.execute('INSERT OR IGNORE INTO report_revision VALUES(1,0)')
        self.db.execute('CREATE TABLE IF NOT EXISTS report_changes(slot INTEGER PRIMARY KEY,revision INTEGER,table_name TEXT,at REAL)')
        tables=('observations','observation_context','window_observations','weekly_window_refs',
                'calls','manual_resets','coverage_issues','quota_exclusions','quota_cycle_clears','state',
                'tracking_config','tracking_policies','tracking_controls','tracking_tasks',
                'tracking_observations','tracking_gaps','tracking_wire','tracking_health',
                'tracking_call_boundaries')
        for table in tables:
            columns=[r[1] for r in self.db.execute(f'PRAGMA table_info({table})')]
            if not columns:continue
            ignored={'at','complete_at'} if table=='state' else {'at'} if table=='tracking_health' else set()
            condition=' OR '.join(f'old."{column}" IS NOT new."{column}"' for column in columns if column not in ignored)
            for operation in ('INSERT','UPDATE','DELETE'):
                conditions=[f'({condition})'] if operation=='UPDATE' else []
                if table=='tracking_observations':
                    conditions.append('(SELECT active FROM tracking_payload_migration WHERE id=1)=0')
                when=' WHEN '+' AND '.join(conditions) if conditions else ''
                name=f'report_{table}_{operation.lower()}'
                existing=self.db.execute('SELECT sql FROM sqlite_master WHERE type=\'trigger\' AND name=?',(name,)).fetchone()
                linked=table in ('observations','window_observations','weekly_window_refs')
                payload=table=='tracking_observations'
                if (existing and 'ON CONFLICT(slot)' in existing[0] and
                        (not linked or 'min(changed_at)' in existing[0]) and
                        (table not in ('tracking_wire','tracking_call_boundaries','tracking_controls') or 'archive_bounds_v1' in existing[0]) and
                        (not payload or 'tracking_payload_migration' in existing[0] and
                         (operation!='UPDATE' or 'old."payload_context"' in existing[0]))):continue
                self.db.execute(f'DROP TRIGGER IF EXISTS {name}')
                column={'observations':'at','window_observations':'at','calls':'ts',
                        'tracking_observations':'received','tracking_wire':'start',
                        'tracking_call_boundaries':'started','tracking_controls':'at'}.get(table)
                times=[f'{side}.{column}' for side in (('old','new') if operation=='UPDATE' else ('old',) if operation=='DELETE' else ('new',))] if column else []
                if table=='tracking_call_boundaries':
                    times=[f'(COALESCE({side}.started,(SELECT ts FROM calls WHERE home={side}.home AND uid={side}.uid)))'
                           for side in (('old','new') if operation=='UPDATE' else ('old',) if operation=='DELETE' else ('new',))]
                if table in ('observation_context','weekly_window_refs'):
                    field='id' if table=='observation_context' else 'observation_id'
                    times=[f'(SELECT at FROM observations WHERE id={side}.{field})' for side in
                           (('old','new') if operation=='UPDATE' else ('old',) if operation=='DELETE' else ('new',))]
                at=('min('+','.join(times)+')' if len(times)>1 else times[0]) if times else 'NULL'
                if linked:
                    # Legacy window rows may share an ID but differ in time.
                    # Invalidate both sides when a reference appears or moves.
                    linked_times=[]
                    for side in (('old','new') if operation=='UPDATE' else ('old',) if operation=='DELETE' else ('new',)):
                        if table=='observations':
                            linked_times.extend((f'SELECT {side}.at AS changed_at',
                                f'SELECT w.at FROM weekly_window_refs r JOIN window_observations w ON w.id=r.window_id WHERE r.observation_id={side}.id'))
                        elif table=='window_observations':
                            linked_times.extend((f'SELECT {side}.at AS changed_at',
                                f'SELECT o.at FROM weekly_window_refs r JOIN observations o ON o.id=r.observation_id WHERE r.window_id={side}.id'))
                        else:
                            linked_times.extend((f'SELECT at AS changed_at FROM observations WHERE id={side}.observation_id',
                                f'SELECT at FROM window_observations WHERE id={side}.window_id'))
                    at='(SELECT min(changed_at) FROM ('+' UNION ALL '.join(linked_times)+'))'
                # A fixed ring bounds durable change tracking. A lagging reader
                # reloads authoritative rows if any revision has fallen out.
                self.db.execute(f'CREATE TRIGGER {name} AFTER {operation} ON {table}{when} BEGIN /* archive_bounds_v1 */ '
                    'UPDATE report_revision SET value=value+1 WHERE id=1; '
                    f"INSERT INTO report_changes SELECT value%1024,value,'{table}',{at} FROM report_revision WHERE id=1 "
                    'ON CONFLICT(slot) DO UPDATE SET revision=excluded.revision,table_name=excluded.table_name,at=excluded.at; END')
        self.db.commit()

    def _report_rows(self, key, sql, params, tables, time_column=None, clock=False):
        """Reuse immutable SQL rows; re-read only a changed ordered time suffix."""
        cache=getattr(self,'_input_cache',None)
        changes=getattr(self,'_input_changes',None)
        if cache is None or changes is None:return self.db.execute(sql,params)
        previous=cache.get(key)
        signature=params[:-1] if clock else params
        relevant=[at for table,at in changes if table in tables]
        if previous and previous[0]==signature and not relevant:return previous[1]
        lower=None
        if previous and previous[0]==signature and time_column and relevant and all(at is not None for at in relevant):
            lower=min(relevant)
        if lower is None:
            rows=[];weights=[0]
            suffix=self.db.execute(sql,params)
        else:
            field=time_column.split('.')[-1]
            # The cached query is ordered by this timestamp. Late corrections
            # and deletes replace its entire affected suffix, including ties.
            left=0;right=len(previous[1])
            while left<right:
                mid=(left+right)//2
                if previous[1][mid][field]<lower:left=mid+1
                else:right=mid
            head,order=sql.rsplit(' order by ',1)
            suffix=self.db.execute(head+f' and {time_column}>=? order by '+order,(*params,lower))
            rows=previous[1][:left];weights=previous[3][:left+1]
        # Do not evict the earlier queries of every report to admit its later
        # queries: that turns a working set above budget into perpetual misses.
        # Stream oversized results to their consumer. Keeping an entire SQL
        # result beside its decoded history can double a large report's peak.
        available=self.INPUT_CACHE_BYTES-sum(item[2] for name,item in cache.items() if name!=key)
        if weights[-1]>available:
            cache.pop(key,None)
            return chain(rows,suffix)
        for row in suffix:
            weight=weights[-1]+144+32*len(row)+sum(len(v)*4 if isinstance(v,str) else len(v) if isinstance(v,bytes) else 32 for v in row)
            if weight>available:
                cache.pop(key,None)
                return chain(rows,(row,),suffix)
            rows.append(row);weights.append(weight)
        weight=weights[-1]
        cache[key]=(signature,rows,weight,weights)
        return rows

    def _mode_dependency_changed(self,key):
        self._mode_dependency_profiles=None
        self._mode_dependency_digests.pop(key[:2],None)

    def mode_dependency(self,home,sid):
        """Stable own-mode dependency; callers add parent/body dependencies.

        Group current modes/evidence once after value changes, then hash only
        a requested owner whose values changed. Empty owners retain no cache.
        """
        if self._mode_dependency_profiles is None:
            profiles={}
            for index,values in enumerate((self.modes,self.mode_evidence)):
                for key,value in values.items():
                    profiles.setdefault(key[:2],[[],[]])[index].append((key[2],value))
            self._mode_dependency_profiles=profiles
        key=(home,sid)
        profile=self._mode_dependency_profiles.get(key)
        if profile is None:return _EMPTY_MODE_DEPENDENCY
        if key not in self._mode_dependency_digests:
            for values in profile:
                values.sort(key=lambda entry:json.dumps(entry[0],separators=(',',':')))
            digest=hashlib.sha256(_MODE_DEPENDENCY_DOMAIN)
            # Reprepare only owners that could have replaced applied settings
            # with an unknown log mode. Known-only dependency hashes stay stable.
            if any(mode=='미확인' for _,mode in profile[0]):
                digest.update(b'preserve-applied-settings-v1\0')
            encoder=json.JSONEncoder(sort_keys=True,separators=(',',':'),allow_nan=False)
            for part in encoder.iterencode(profile):digest.update(part.encode('utf8'))
            self._mode_dependency_digests[key]='mode-v1:'+digest.hexdigest()
        return self._mode_dependency_digests[key]

    def rebuild_mode_evidence(self,home):
        """Replay sanitized setting events by log order, including backfilled evidence."""
        inherited={}
        candidates={}
        setting_candidates={}
        for record in self.db.execute('select * from mode_events where home=? order by id',(home,)):
            parsed=json.loads(record['data']);sid=record['sid'];key=(home,sid,record['turn'])
            settings=parsed.get('settings_update')
            if settings is None and parsed['source']=='settings_override' and parsed['action']!='unchanged':
                settings=parsed
            if settings is not None:
                inherited[sid]={'mode':settings['mode'],'configured_service_tier':settings.get('configured_service_tier'),
                    'request_mode_action':settings['action'],'request_mode_source':'inherited_settings'}
                setting_candidates.setdefault(key,set()).add(settings['mode'])
                if len(setting_candidates[key])>1:
                    inherited[sid]=dict(mode='미확인',configured_service_tier=None,
                        request_mode_action='conflict',request_mode_source='settings_conflict',mode_conflict=True)
            if parsed['action']!='unchanged':
                candidate={'mode':parsed['mode'],'configured_service_tier':parsed.get('configured_service_tier'),
                    'request_mode_action':parsed['action'],'request_mode_source':parsed['source']}
            else:
                candidate=inherited.get(sid,{'mode':'미확인','configured_service_tier':None,
                    'request_mode_action':'unchanged','request_mode_source':'unobserved'})
            candidates.setdefault(key,[]).append(candidate)
        for key,values in candidates.items():
            known=[r for r in values if r['mode']!='미확인']
            modes={r['mode'] for r in known}
            if len(modes)>1:
                evidence=dict(mode='미확인',configured_service_tier=None,request_mode_action='conflict',
                              request_mode_source='settings_conflict',mode_conflict=True)
            else:
                candidate=known[-1] if known else values[-1]
                evidence={**candidate,'mode_conflict':candidate.get('mode_conflict',False)}
            if self.mode_evidence.get(key)!=evidence:
                self.mode_evidence[key]=evidence
                self.modes[key]=evidence['mode']
                self.db.execute('insert or replace into modes values(?,?,?,?)',(*key,evidence['mode']))
                session_key=key[:2]
                self.mode_revisions[session_key]=self.mode_revisions.get(session_key,0)+1

    def scan_modes(self, homes):
        from contextlib import closing
        from .request_modes import request_mode_observation
        # Bound source work separately from retained bodies so sparse handler
        # logs can advance without loading unrelated text or skipping evidence.
        columns=("select id,case when target='codex_core::session::handlers' then thread_id end,"
                 "case when target='codex_core::session::handlers' then feedback_log_body end from logs")
        def scan(source,condition='',parameters=(),reverse=False):
            rows=[];first=last=None
            order=' desc' if reverse else ''
            with closing(source.execute(columns+condition+' order by id'+order+' limit ?',
                                        (*parameters,MODE_SCAN_ROWS))) as cursor:
                for row in cursor:
                    if first is None:first=row[0]
                    last=row[0]
                    if row[1]:
                        rows.append(row)
                        if len(rows)>=MODE_SCAN_MATCHES:break
            return rows,first,last
        for home in homes:
            path=Path(home)/'logs_2.sqlite'
            if not path.exists():continue
            try:
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.3)) as source:
                    maximum=source.execute('select max(id) from logs').fetchone()[0] or 0
                    if maximum<self.mode_offsets.get(home,0):
                        self.mode_offsets.pop(home,None);self.mode_backfill.pop(home,None)
                    if home not in self.mode_offsets:
                        rows,first,last=scan(source,reverse=True)
                        rows.reverse()
                        latest=first or 0
                        self.mode_backfill[home]=last or 0
                    else:
                        rows,_,last=scan(source,' where id>?',(self.mode_offsets[home],))
                        latest=last if last is not None else self.mode_offsets[home]
                    before=self.mode_backfill.get(home,0)
                    if before:
                        older,_,last=scan(source,' where id<?',(before,),reverse=True)
                        self.mode_backfill[home]=last or 0
                        rows=older[::-1]+rows
                changed=False
                for identifier,sid,body in rows:
                    parsed=request_mode_observation(body)
                    if parsed:
                        data=json.dumps(parsed,sort_keys=True)
                        old=self.db.execute('select data from mode_events where home=? and id=? and sid=? and turn=?',
                                            (home,identifier,sid,parsed['turn'])).fetchone()
                        if old is None or old[0]!=data:
                            self.db.execute('insert or replace into mode_events values(?,?,?,?,?)',
                                            (home,identifier,sid,parsed['turn'],data));changed=True
                if changed:self.rebuild_mode_evidence(home)
                self.mode_offsets[home]=latest
                checkpoint=(latest,self.mode_backfill.get(home,0))
                saved=self.db.execute('select latest,before_id from mode_scan where home=?',(home,)).fetchone()
                # A conditional UPSERT still opens a write transaction when
                # nothing changed. Read the durable cursor before taking that
                # lock; do not trust the in-memory scan cursor after failures.
                if saved is None or tuple(saved)!=checkpoint:
                    self.db.execute('''insert into mode_scan values(?,?,?) on conflict(home) do update set
                        latest=excluded.latest,before_id=excluded.before_id
                        where mode_scan.latest is not excluded.latest or mode_scan.before_id is not excluded.before_id''',
                        (home,*checkpoint))
            except sqlite3.Error:pass
        self.db.commit()

    def enrich_modes(self, snapshot, scan=True):
        from .request_modes import mode_parents, ParentModeTimeline
        if scan:self.scan_modes(snapshot.get('homes',[]))
        previous=getattr(self,'_enriched_sessions',{})
        enriched={};resolved={};timelines={}
        changed=False
        parents=mode_parents(snapshot['sessions'])
        def resolve(source):
            nonlocal changed
            if id(source) in resolved:return resolved[id(source)]
            key=(source['home'],source['id'])
            parent=resolve(parents[key]) if key in parents else None
            revision=self.mode_revisions.get(key,0)
            if parent is not None:
                revision=(revision,id(parent['history']),repr(parent.get('turn_records',{})),
                          repr(source.get('turn_records',{})),parent.get('usage_revision'))
            old=previous.get(key)
            history=old[0] if old and source['history'] is old[2] else source['history']
            source_revision=old[3] if old and source['history'] is old[2] else source.get('usage_revision')
            if old and old[0] is history and old[1]==revision:
                session={**source,'history':old[2],
                    'usage_revision':(source_revision,revision)}
                enriched[key]=old;resolved[id(source)]=session
                return session
            parent_timeline=timeline(parent) if parent is not None else None
            rows=[]
            for row in history:
                recorded=request_tier(row)
                changes={}
                evidence=self.mode_evidence.get((*key,row.get('turn')))
                explicit=self.modes.get((*key,row.get('turn')))
                if row.get('service_tier_source')=='wire':
                    changes['service_tier']='미확인' if row.get('mode_conflict') else request_tier({'service_tier':row.get('requested_service_tier')})
                elif row.get('mode_conflict') and row.get('request_mode_source')!='settings_conflict':
                    changes['service_tier']='미확인'
                elif evidence and evidence['request_mode_source']!='unobserved':
                    changes.update(service_tier=evidence['mode'],service_tier_source='settings')
                    changes.update({k:v for k,v in evidence.items() if k!='mode'})
                elif row.get('service_tier_source')=='settings':
                    # An unchanged submission without prior log evidence does
                    # not contradict a successfully applied settings snapshot.
                    changes['service_tier']=explicit if explicit and explicit!='미확인' else recorded
                else:changes['service_tier']=recorded if recorded!='미확인' else explicit or '미확인'
                # Keep the collector's read-only row when enrichment changes nothing.
                # The cache retains its original history for later evidence corrections.
                value={**row,**changes} if any(k not in row or row[k]!=v for k,v in changes.items()) else row
                rows.append(parent_timeline.inherit(source,value) if parent_timeline is not None else value)
            session={**source,'history':rows}
            session['usage_revision']=(source_revision,revision)
            # Retain the parent history referenced by the dependency identity.
            enriched[key]=(history,revision,rows,source_revision,parent)
            changed=True
            resolved[id(source)]=session
            return session
        def timeline(session):
            key=(session['home'],session['id'])
            if key not in timelines:
                parent=resolve(parents[key]) if key in parents else None
                timelines[key]=ParentModeTimeline(session,self.modes,timeline(parent) if parent is not None else None)
            return timelines[key]
        sessions=[resolve(source) for source in snapshot['sessions']]
        if changed or len(enriched)!=len(previous) or not hasattr(self,'_enriched_identity'):
            self._enriched_identity=object()
        self._enriched_sessions=enriched
        snapshot['sessions']=sessions
        return self._enriched_identity

    def observe(self, home, quota, commit=True):
        self._observe_parts(observation_parts(home,quota))
        if commit:self.db.commit()

    def _observe_parts(self,parts):
        windows,observation,context,weekly_ref=parts
        for window in windows:
            self.db.execute('insert or ignore into window_observations values(?,?,?,?,?,?,?,?,?,?,?,?)',window)
        if observation is None:return
        key=observation[0]
        self.db.execute('insert or ignore into observations values(?,?,?,?,?,?,?,?,?)',observation)
        self.db.execute('''insert into observation_context values(?,?,?) on conflict(id) do update set
                        plan=case when observation_context.plan='' then excluded.plan else observation_context.plan end,
                        bucket=case when observation_context.bucket='' then excluded.bucket else observation_context.bucket end
                        where (observation_context.plan='' and excluded.plan<>'')
                           or (observation_context.bucket='' and excluded.bucket<>'')''',
                        context)
        if weekly_ref is not None:
            # The display ID has a different identity from the canonical observation.
            # Retain its first payload, including when only separate_models changes.
            self.db.execute('''insert or ignore into weekly_window_refs(window_id,observation_id)
                select ?,id from observations where id=? and not exists
                (select 1 from window_observations where id=?)''',(weekly_ref,key,weekly_ref))

    def sync(self, engine, snapshot, *, applied_sources=None, append_sources=None):
        """Use the existing deduplicated analytical rows; don't parse conversations twice."""
        from .quota_tracking_store import sync_activity
        if append_sources:
            from .quota_prepared_sync import require_append
            if applied_sources is None or any(applied_sources.get(key)!=value.get('source')
                    for key,value in append_sources.items()):
                raise ValueError('partial ledger facts require exact current source certificates')
            for key,value in append_sources.items():
                session=engine.sessions.get(key)
                if session is None or len(session['prepared']['history'])!=value['count']:
                    raise ValueError('partial ledger facts differ from the certified tail count')
            require_append(self.db,append_sources)
        pending={(s['home'],s['id']) for s in snapshot.get('sessions',()) if s.get('_collection_pending')}
        if pending:
            # Pending catalog members are a retained read model. They cannot
            # rewrite newer calls, task completions or coverage in the ledger.
            snapshot={**snapshot,'sessions':[s for s in snapshot['sessions'] if (s['home'],s['id']) not in pending]}
        recovered_homes=set()
        if snapshot.get('collection_recovery'):
            # A durable collection cache can predate this ledger's last live
            # heartbeat. Do not replay its old tasks, calls, or coverage over
            # newer evidence, including evidence committed by another writer.
            if not self.db.in_transaction:self.db.execute('BEGIN IMMEDIATE')
            for home in snapshot.get('homes',[]):
                latest=self.db.execute('''select max(at) from (
                    select at from state where home=? union all
                    select at from tracking_health where home=?)''',(home,home)).fetchone()[0]
                if latest is not None and latest>=snapshot['ts']:recovered_homes.add(home)
            if recovered_homes:
                snapshot={**snapshot,'homes':[home for home in snapshot['homes'] if home not in recovered_homes]}
        if not hasattr(self,'_tracking_sync_cache'):self._tracking_sync_cache={}
        sync_activity(self.db, snapshot,self._tracking_sync_cache)
        certificates={}
        for key, session in {**engine.sessions,**getattr(engine,'internal_sessions',{})}.items():
            if key[0] in recovered_homes or key in pending:continue
            revision = (applied_sources[key] if applied_sources is not None else
                        session.get('content_revision',session['revision']))
            if applied_sources is None and self.revisions.get(key) == revision:
                continue
            home, sid = key
            current_uids = set()
            appended=(append_sources or {}).get(key)
            selected='c.home=? AND c.sid=?';arguments=[home,sid]
            if appended is not None:
                uids=[sid+':'+str(row['key']) if str(row['key']).startswith(('legacy:','record:')) else str(row['key'])
                      for row in session['prepared']['history']]
                selected='c.home=? AND c.uid IN ('+','.join('?' for _ in uids)+')'
                arguments=[home,*uids]
            stored={r['uid']:r for r in self.db.execute('''select c.uid,c.signature,c.service_tier,c.sid,c.cost,c.price_id,
                b.uid as boundary_uid,b.started from calls c left join tracking_call_boundaries b
                on b.home=c.home and b.uid=c.uid where '''+selected,arguments)}
            previous_usage = dict(appended['previous_usage']) if appended is not None else {}
            call_batch=[];boundary_batch=[];pending_uids=set()
            def flush_calls():
                # One statement journals each touched page once per bounded
                # batch. Row triggers and the surrounding transaction remain.
                for prefix,rows,suffix in (
                    ('insert or replace into calls',call_batch,''),
                    ('insert into tracking_call_boundaries',boundary_batch,
                     ' on conflict(home,uid) do update set started=excluded.started '
                     'where tracking_call_boundaries.started is not excluded.started')):
                    if rows:
                        values=','.join('('+','.join('?' for _ in row)+')' for row in rows)
                        self.db.execute(prefix+' values '+values+suffix,tuple(value for row in rows for value in row))
                        rows.clear()
                pending_uids.clear()
            turns = session['prepared'].get('turn_records', {})
            for row in sorted(session['prepared']['history'], key=lambda value: value['ts']):
                response = str(row['key'])
                uid = sid + ':' + response if response.startswith(('legacy:', 'record:')) else response
                if uid in pending_uids or len(pending_uids)>=self.SYNC_BATCH_ROWS:flush_calls()
                current_uids.add(uid)
                turn = row.get('turn')
                requested = row.get('request_observed_at') or row.get('request_ts')
                if requested is None and turn:
                    # A previous completed usage record is a conservative lower
                    # bound for the next call in this same turn.
                    requested = previous_usage.get(turn, turns.get(turn, {}).get('started_at'))
                if turn:
                    previous_usage[turn] = row['ts']
                old = stored.get(uid)
                if old is None or old['boundary_uid'] is None or old['started'] != requested:
                    boundary_batch.append((home,uid,requested));pending_uids.add(uid)
                cost = row['cost'] if 'cost' in row else token_cost(row)['cost']
                price_inputs = [row.get(k) for k in ('ts', 'model', 'service_tier', *TOKEN_FIELDS)]
                if row.get('input_conflict'):price_inputs.append({'input_conflict':True})
                signature = json.dumps(price_inputs, separators=(',', ':'))
                if old is None:old=self.db.execute('select signature,service_tier,sid,cost,price_id from calls where home=? and uid=?',(home,uid)).fetchone()
                if old and old['signature'] == signature and old['cost'] == cost and self._price_equivalent(old['price_id']):
                    if old['sid']!=sid:
                        self.db.execute('update calls set sid=? where home=? and uid=?',(sid,home,uid))
                    continue
                if old:
                    self.db.execute('insert or ignore into cost_archive values(?,?,?,?,?,?,?)',
                        (home,uid,old['price_id'],old['signature'],old['cost'],old['service_tier'],time.time()))
                call_batch.append((home, uid, sid, row['ts'], row.get('model') or '미확인',
                     *[row.get(k) for k in TOKEN_FIELDS], cost, PRICE_ID, signature,row.get('service_tier','미확인')))
                pending_uids.add(uid)
            flush_calls()
            self.revisions[key] = revision
            if appended is None:
                old_uids = set(stored)
                self.db.executemany('delete from calls where home=? and sid=? and uid=?',
                                    [(home,sid,uid) for uid in old_uids-current_uids])
            if applied_sources is not None:certificates[key]=revision
        for home in snapshot.get('homes', []):
            usage_errors=snapshot.get('usage_errors')
            if usage_errors is None:
                usage_errors=[error for error in snapshot.get('errors',[])
                              if '기록 읽기' in error or '세션 목록' in error or '일부 기록 형식' in error]
            signatures=getattr(self,'_coverage_signatures',{})
            relevant=[s for s in snapshot['sessions'] if s['home']==home]
            pending_sids=frozenset(sid for pending_home,sid in pending if pending_home==home)
            signature=(tuple(usage_errors),tuple((s.get('id',''),repr(s.get('usage_revision')) if s.get('usage_revision') is not None else tuple(r.get('ts') for r in s.get('history',[])),
                bool(s.get('unclassified')),s.get('coverage_available'),repr(s.get('coverage_gaps',[]))) for s in relevant),pending_sids)
            if signatures.get(home)!=signature:
                if pending_sids:
                    # Keep known uncertainty for unvalidated owners. Visit only
                    # stored issue owners, not every call or every confirmed owner.
                    owners=self.db.execute('SELECT DISTINCT sid FROM coverage_issues WHERE home=?',(home,)).fetchall()
                    self.db.executemany('DELETE FROM coverage_issues WHERE home=? AND sid=?',
                        ((home,row[0]) for row in owners if row[0] not in pending_sids))
                else:self.db.execute('delete from coverage_issues where home=?', (home,))
                for session in snapshot['sessions']:
                    if session['home'] != home:
                        continue
                    if session.get('unclassified') and not session.get('coverage_available'):
                        bounds=session.get('_coverage_bounds')
                        if bounds is None:
                            times = [r['ts'] for r in session.get('history',[]) if r.get('ts') is not None]
                            bounds=(min(times) if times else None,max(times) if times else None)
                        self.db.execute('insert or replace into coverage_issues values(?,?,?,?,?)',
                                        (home,session.get('id',''),*bounds,'참고: 누계 차액의 발생 경계 미확인'))
                    for gap in session.get('coverage_gaps', []):
                        self.db.execute('insert or replace into coverage_issues values(?,?,?,?,?)',
                                        (home, session.get('id',''),gap['start'],gap['end'],
                                         f"누계 차액 증가 {gap['tokens']}토큰, {gap['start']}"))
                if usage_errors:
                    self.db.execute('insert or replace into coverage_issues values(?,?,?,?,?)',
                                    (home, '', None, None, '로컬 호출 기록 읽기 실패: '+', '.join(usage_errors)))
                signatures[home]=signature
                self._coverage_signatures=signatures
            unclassified = sum(bool(s.get('unclassified')) for s in snapshot['sessions'] if s['home'] == home)
            index=snapshot.get('index',{})
            loading=bool(index.get('loading'))
            complete=index.get('usage_complete',snapshot.get('usage_collection_complete',not loading and not usage_errors))
            previous=self.db.execute('select complete_at,unclassified from state where home=?',(home,)).fetchone()
            if pending_sids and previous:unclassified=previous['unclassified']
            watermark=previous['complete_at'] if previous else None
            if complete:
                stamp=index.get('usage_complete_at') or index.get('last_usage_success') or snapshot.get('last_usage_collection_success') or snapshot['ts']
                watermark=max(watermark or 0,stamp)
            self.db.execute('''insert into state(home,at,loading,unclassified,complete_at) values(?,?,?,?,?)
                on conflict(home) do update set at=excluded.at,loading=excluded.loading,
                unclassified=excluded.unclassified,complete_at=excluded.complete_at''',
                (home,snapshot['ts'],loading,unclassified,watermark))
        cursor=snapshot.get('activity_cursor')
        if cursor:self.db.execute('INSERT INTO collection_activity_cursor VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET '
            'source=excluded.source,revision=excluded.revision WHERE source IS NOT excluded.source OR revision IS NOT excluded.revision',tuple(cursor))
        if applied_sources is not None:
            from .quota_prepared_sync import save
            save(self.db,certificates)
        self.db.commit()
        self._compact_cost_archive()
        self._payload_migration.step()

    def activity_cursor(self):
        row=self.db.execute('SELECT source,revision FROM collection_activity_cursor WHERE id=1').fetchone()
        return tuple(row) if row else None

    def import_observations(self, index_path, home):
        """Recover already-sanitized local observations, without modifying the usage index."""
        path = Path(index_path)
        if not path.exists():
            return
        from contextlib import closing
        from .event_store import quota_events, unpack_event
        previous_limit=self.db.execute('PRAGMA cache_size').fetchone()[0]
        self.db.execute(f'PRAGMA cache_size=-{self.IMPORT_CACHE_KIB}')
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
                rows = quota_events(db,home)
                prepared=[];batch_bytes=0
                for at,data in rows:
                    data=unpack_event(data)
                    size=len(data.encode('utf8'))
                    if prepared and (len(prepared)>=128 or batch_bytes+size>1024*1024):
                        self._import_observation_parts(prepared);prepared=[];batch_bytes=0
                    quota=json.loads(data).get('payload',{}).get('rate_limits')
                    if quota:
                        prepared.append(observation_parts(home,{**quota,'observed_at':at,'source':'history'}))
                        batch_bytes+=size
                    if batch_bytes>=1024*1024:
                        self._import_observation_parts(prepared);prepared=[];batch_bytes=0
                self._import_observation_parts(prepared)
            self.db.commit()
        finally:
            self.db.execute(f'PRAGMA cache_size={previous_limit}')
            self.db.execute('PRAGMA shrink_memory')

    def import_observations_step(self,index_path,homes):
        """Commit one recovery slice, releasing the source snapshot before return."""
        from .quota_history_import import ObservationImport
        scope=(str(Path(index_path).resolve()),frozenset(homes))
        previous=getattr(self,'_observation_import',None)
        if previous is None or previous[0]!=scope:
            if previous is not None:previous[1].close()
            previous=self._observation_import=(scope,ObservationImport(self,index_path,homes))
        complete=previous[1].step()
        if complete:previous[1].close()
        return complete

    def _import_observation_parts(self,batch):
        # At most 128 records / 1 MiB of source text, or one oversized record.
        # Only prepared storage values survive until the bounded lookup completes.
        window_ids={row[0] for windows,_,_,_ in batch for row in windows}
        window_ids.update(ref for _,_,_,ref in batch if ref is not None)
        observation_ids={row[0] for _,row,_,_ in batch if row is not None}
        refs={ref for _,_,_,ref in batch if ref is not None}
        def existing(query,keys):
            if not keys:return []
            return list(self.db.execute(query.format(','.join('?' for _ in keys)),tuple(keys)))
        windows={row[0] for row in existing('select id from window_observations where id in ({})',window_ids)}
        observations={row[0]:row[1:] for row in existing('''select o.id,c.plan,c.bucket from observations o
            left join observation_context c on c.id=o.id where o.id in ({})''',observation_ids)}
        references={row[0] for row in existing('select window_id from weekly_window_refs where window_id in ({})',refs)}
        for parts in batch:
            rows,observation,context,ref=parts
            complete=all(row[0] in windows for row in rows)
            if observation is not None:
                old=observations.get(observation[0])
                complete=complete and old is not None and all(
                    present is not None and (present!='' or expected=='') for present,expected in zip(old,context[1:]))
            if ref is not None:complete=complete and (ref in references or ref in windows)
            if not complete:self._observe_parts(parts)

    def preview_exclusion(self, home, start, end, reason=''):
        from .quota_exclusions import preview_exclusion
        return preview_exclusion(self.report(home), start, end, reason)

    def exclude_interval(self, home, start, end, reason=''):
        from uuid import uuid4
        from .quota_exclusions import validate_exclusion
        def save(preview):
            record = {key: preview[key] for key in
                      ('home', 'account', 'start', 'end', 'reason', 'period_start', 'period_end')}
            record.update(id=uuid4().hex, created_at=time.time(), restored_at=None)
            self.db.execute('''insert into quota_exclusions
                (id,home,account,start,end,reason,period_start,period_end,created_at,restored_at)
                values(:id,:home,:account,:start,:end,:reason,:period_start,:period_end,:created_at,:restored_at)''', record)
            return record
        return self._edit_with_report(home,
            lambda report:validate_exclusion(report,start,end,reason),save,edit_start=start)

    def restore_exclusion(self, home, exclusion_id):
        def restore(account):
            record = self.db.execute('select * from quota_exclusions where id=? and home=? and account=?',
                                     (exclusion_id, home, account)).fetchone()
            if record is None:
                raise ValueError('현재 계정의 제외 내역을 찾을 수 없습니다.')
            if record['restored_at'] is not None:
                return False
            self.db.execute('update quota_exclusions set restored_at=? where id=?',
                            (time.time(), exclusion_id))
            return True
        return self._edit_with_report(home,lambda report:report['account'],restore,edit_history=False)

    def clear_selected_cycle(self, home, start, account, *, delete=False):
        from .quota_view import _history_rows
        def prepare(report):
            starts=report.get('_edit_periods',[]);last=report.get('_edit_last')
            if not starts:
                starts=[]
                for last in _history_rows(report,'weekly',labels=False):
                    if not starts or last.get('reset_kind'):starts.append(last['at'])
            if (not account or report['account']!=account or start not in starts
                    or delete != (start!=starts[-1])):
                raise ValueError('선택한 주기가 변경되었습니다. 기록을 확인한 뒤 다시 시도하세요.')
            original=next((r['start'] for r in report.get('cycle_clears',[])
                           if r['end']==start and r['baseline']!='null'),start)
            at=starts[starts.index(start)+1] if delete else time.time()
            baseline=None if delete else {k:last.get(k) for k in ('used','reset','source','account','window')}
            return dict(home=home,account=account,start=original,end=at,baseline=json.dumps(baseline))
        def save(record):
            self.db.execute('''insert into quota_cycle_clears values(:home,:account,:start,:end,:baseline)
                on conflict(home,account,start) do update set end=excluded.end,baseline=excluded.baseline''',record)
            return record
        return self._edit_with_report(home,prepare,save,edit_start=start)

    def _edit_with_report(self, home, prepare, apply, *, edit_start=None, edit_history=True):
        """Validate a stable read snapshot before briefly acquiring the writer lock."""
        for _ in range(3):
            self.db.execute('BEGIN')
            try:
                revision=self.db.execute('PRAGMA data_version').fetchone()[0]
                prepared=prepare(self._report(home,exclusion_only=True,edit_start=edit_start,edit_history=edit_history))
            finally:
                self.db.rollback()
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                if self.db.execute('PRAGMA data_version').fetchone()[0]!=revision:
                    continue
                return apply(prepared)
        raise sqlite3.OperationalError('사용한도 기록이 계속 갱신되어 저장하지 못했습니다. 다시 시도하세요.')

    def report(self, home, now=None):
        now=time.time() if now is None else now
        own_transaction = not self.db.in_transaction
        if own_transaction:self.db.execute('BEGIN')
        try:
            revision=self.db.execute('SELECT value FROM report_revision WHERE id=1').fetchone()[0]
            cached=getattr(self,'_report_cache',None) if own_transaction else None
            if cached and cached['home']==home and cached['revision']==revision and cached['at']<=now<cached['until']:
                report=cached['report'];scope=(report.get('tracking') or {}).get('homes') or [home]
                slots=','.join('?' for _ in scope)
                states=list(self.db.execute(f'SELECT * FROM state WHERE home IN ({slots})',scope))
                state=next((r for r in states if r['home']==home),None)
                complete_at=min((r['complete_at'] for r in states),default=None) if len(states)==len(scope) and all(r['complete_at'] is not None for r in states) else None
                health=list(self.db.execute(f'SELECT * FROM tracking_health WHERE home IN ({slots})',scope))
                healthy=len(health)==len(scope) and all(r['complete'] and now-r['at']<=15 for r in health)
                before=report.get('index_complete_at')
                crossed=bisect_right(cached['ends'],complete_at if complete_at is not None else float('-inf'))!=bisect_right(cached['ends'],before if before is not None else float('-inf'))
                if not crossed and (not report.get('tracking') or healthy==report['tracking']['complete']):
                    return {**report,'at':now,'index_at':state['at'] if state else None,'index_complete_at':complete_at,
                        '_projection_coverage':{r['home']:(r['unclassified'],r['complete_at']) for r in states},
                        'cycles':[{**cycle,'cycle_end':now} if position==0 and cycle['cycle_end']==cached['at'] else cycle
                                  for position,cycle in enumerate(report['cycles'])]}
            if own_transaction:
                before=getattr(self,'_input_revision',None)
                events=(list(self.db.execute('SELECT revision,table_name,at FROM report_changes WHERE revision>? ORDER BY revision',(before,)))
                        if before is not None and before<=revision else [])
                valid=(cached and cached['home']==home and cached['at']<=now<cached['until'] and before is not None
                       and revision-before==len(events))
                if not valid:self._input_cache={};self._request_inputs=None;self._history_inputs=None;self._tracking_inputs={}
                self._input_changes=[(r['table_name'],r['at']) for r in events] if valid else []
            report=self._report(home,now)
            self._report_generation=getattr(self,'_report_generation',0)+1
            if own_transaction:
                self._input_revision=revision
                future=[]
                scope=(report.get('tracking') or {}).get('homes') or [home]
                slots=','.join('?' for _ in scope)
                for table,columns in self.FUTURE_COLUMNS:
                    for column in columns:
                        # A future observation can change account membership;
                        # keep that boundary global, even for currently unrelated homes.
                        condition='' if table=='tracking_observations' else f'home IN ({slots}) AND '
                        params=(now,) if not condition else (*scope,now)
                        boundary=self.db.execute(f'SELECT min({column}) FROM {table} WHERE {condition}{column}>?',params).fetchone()[0]
                        if boundary is not None:future.append(boundary)
                history=report['history'];cycle_ends=sorted({c['end'] for c in report['cycles']})
                ends=(array('d',(at for at,_ in groupby(merge(history.times,cycle_ends))))
                      if isinstance(history,_PackedHistory) else sorted(set(cycle_ends)|{r['at'] for r in history}))
                self._report_cache=dict(home=home,revision=revision,at=now,until=min(future,default=float('inf')),
                    ends=ends,report=dict(report))
            else:self._report_cache=None;self._input_cache={};self._input_revision=None;self._request_inputs=None;self._history_inputs=None;self._tracking_inputs={}
            return report
        except Exception:
            self._input_cache={};self._input_revision=None;self._request_inputs=None;self._history_inputs=None;self._tracking_inputs={}
            raise
        finally:
            self._input_changes=None
            if own_transaction:self.db.rollback()

    def _observations(self, home, cutoff, now):
        # Join the small metadata fields while streaming. A separate context
        # dictionary duplicates every observation identifier in long histories.
        return self._report_rows('observations',
            "select o.*,coalesce(c.plan,'') as plan,coalesce(c.bucket,'') as bucket "
            'from observations o left join observation_context c on c.id=o.id '
            'where o.home=? and o.at>=? and o.at<=? order by o.at,o.id',
            (home,cutoff,now),('observations','observation_context'),'o.at',clock=True)

    def current_report(self,home,now=None,*,retain_projection=False):
        """Read one valid projected prefix and only its subsequent observations."""
        from .quota_projection import load
        from .quota_tracking_store import build
        now=time.time() if now is None else now
        if self.db.in_transaction:return None
        saved={key:getattr(self,key,None) for key in
            ('_input_changes','_input_cache','_tracking_inputs','_request_inputs','_history_inputs','_cycle_cache')}
        self.db.execute('BEGIN')
        try:
            projected=load(self.db,home,now)
            if projected is None:return None
            if projected.get('invalid'):
                self._projection_invalid=home
                return None
            context=build(self.db,home,now,context_only=True)
            if context is None or context.get('account')!=projected['account']:return None
            self._input_changes=[];self._input_cache={};self._request_inputs=None;self._history_inputs=None;self._cycle_cache={}
            self._tracking_inputs={'checkpoint':projected['tracking']}
            result=self._report(home,now,from_at=projected['start'])
            result.update(_history_resume=projected['resume'],history_pending=True)
            if retain_projection:
                result.update(_projection_tracking=dict(self._tracking_inputs),
                    _projection_revision=self.db.execute('SELECT value FROM report_revision WHERE id=1').fetchone()[0],
                    _projection_start=projected['start'],
                    _cycle_versions={key:value[0] for key,value in self._cycle_cache.items()})
            return result
        finally:
            self.db.rollback()
            for key,value in saved.items():
                if value is None:self.__dict__.pop(key,None)
                else:setattr(self,key,value)

    def _report_history(self, home, cutoff, now, tables):
        changes=getattr(self,'_input_changes',None)
        cacheable=getattr(self,'_pack_history',False) and changes is not None
        key=(home,cutoff)
        previous=getattr(self,'_history_inputs',None) if cacheable else None
        prefix=None
        if previous and previous[0]==key:
            relevant=[at for table,at in changes if table in
                      ('observations','observation_context','window_observations','weekly_window_refs')]
            if not relevant:return previous[1]
            if all(at is not None for at in relevant):
                prefix=previous[1];cutoff=max(cutoff,min(relevant))
        metadata={};metadata_bytes=0
        def compact(row, window=None, identifier=None):
            nonlocal metadata_bytes
            value=vars(_HistoryFields(row,row['window'] if window is None else window,identifier))
            for key in ('home','source','account','plan','bucket','window','separate'):
                text=value.get(key)
                if not isinstance(text,str):continue
                shared=metadata.get(text)
                if shared is not None:value[key]=shared
                elif len(metadata)<1024 and metadata_bytes+80+len(text)*4<=1024*1024:
                    metadata[text]=text;metadata_bytes+=80+len(text)*4
            return value
        if getattr(self,'_pack_history',False) and {'window_observations','weekly_window_refs'}<=tables:
            # Two ordered cursors replace the full decoded history lists. The
            # larger page cache is temporary: it avoids repeated reference/index
            # reads while consuming less than the discarded row copies at scale.
            previous_limit=self.db.execute('PRAGMA cache_size').fetchone()[0]
            self.db.execute(f'PRAGMA cache_size=-{self.HISTORY_CACHE_KIB}')
            source=self._history_source_rows(home,cutoff,now,filter_references=prefix is None,
                                            weekly_only=getattr(self,'_weekly_history_only',False))
            try:
                rows=(compact(row) for row in source)
                packed=prefix.with_suffix(cutoff,rows) if prefix is not None else _PackedHistory(rows)
            finally:
                source.close()
                self.db.execute(f'PRAGMA cache_size={previous_limit}')
                self.db.execute('PRAGMA shrink_memory')
            if cacheable:self._history_inputs=(key,packed)
            return packed
        window_clause=" and window='weekly'" if getattr(self,'_weekly_history_only',False) else ''
        history = ([compact(r) for r in self._report_rows('windows','select * from window_observations where home=? and at>=? and at<=?'+window_clause+' order by at,id',(home,cutoff,now),('window_observations',),'at',clock=True)]
                   if 'window_observations' in tables else [])
        referenced=[]
        if 'weekly_window_refs' in tables:
            unreferenced=[]
            for row in self._report_rows('refs', '''select o.*,r.window_id,
                    coalesce(c.plan,'') as plan,coalesce(c.bucket,'') as bucket
                    from observations o left join weekly_window_refs r on r.observation_id=o.id
                    left join observation_context c on c.id=o.id
                    where o.home=? and o.at>=? and o.at<=? order by o.at,r.window_id,o.id''',
                    (home,cutoff,now),('observations','weekly_window_refs','observation_context'),'o.at',clock=True):
                window_id=row['window_id'];value=compact(row,'weekly',window_id)
                if window_id is None:unreferenced.append(value)
                else:
                    referenced.append(value)
            referenced_ids={r['id'] for r in referenced}
            history=[r for r in history if r['id'] not in referenced_ids]
            del referenced_ids
        else:
            unreferenced=[compact(r,'weekly') for r in self._observations(home,cutoff,now)]
        # Existing ledgers retain their weekly observations without rewriting raw evidence.
        if unreferenced:
            known={(r['at'],r['source'],r['account'],r['used'],r['reset']) for r in chain(history,referenced) if r['window']=='weekly'}
            unreferenced=[r for r in unreferenced if (r['at'],r['source'],r['account'],r['used'],r['reset']) not in known]
        # Each input is already ordered. Merge without retaining a second key
        # tuple for every observation during a whole-history sort.
        if getattr(self,'_pack_history',False):
            # These lists belong to this call. Release consumed dictionaries
            # while packing rather than retaining the complete decoded copy.
            def consume(rows):
                rows.reverse()
                while rows:yield rows.pop()
            rows=merge(*(consume(rows) for rows in (history,referenced,unreferenced)),key=lambda r:(r['at'],r['id']))
            packed=prefix.with_suffix(cutoff,rows) if prefix is not None else _PackedHistory(rows)
            if cacheable:self._history_inputs=(key,packed)
            return packed
        return list(merge(history,referenced,unreferenced,key=lambda r:(r['at'],r['id'])))

    def _history_source_rows(self, home, cutoff, now, *, filter_references=True, weekly_only=False):
        # A legacy window can share an ID with a reference at a different time.
        # Match the same source range as the ordinary reader before suppressing it.
        referenced='''SELECT 1 FROM weekly_window_refs f JOIN observations p ON p.id=f.observation_id
            WHERE f.window_id=w.id AND p.home=? AND p.at>=? AND p.at<=?'''
        params=(home,cutoff,now,home,cutoff,now)
        window_clause=" AND window='weekly'" if weekly_only else ''
        bits=None
        if filter_references:
            count=self.db.execute('SELECT count(*) FROM window_observations WHERE home=? AND at>=? AND at<=?'+window_clause,params[:3]).fetchone()[0]
            references=self.db.execute('SELECT count(*) FROM weekly_window_refs').fetchone()[0] if count else 0
            if count and count*4>=references:
                # Two-bit membership rejects impossible reference IDs before
                # random B-tree probes. Positive results still run the exact
                # original query, including home and time, so collisions cannot
                # suppress evidence. At most 8 MiB, with no persistent state.
                size=min(8*1024*1024,max(1024,1 << max(0,(max(1,references)*2-1).bit_length())))
                bits=bytearray(size);mask=size*8-1
                def positions(value):
                    value=hash(value)
                    return value&mask,(value>>32)&mask
                for (identifier,) in self.db.execute('SELECT window_id FROM weekly_window_refs'):
                    for value in positions(identifier):bits[value>>3]|=1 << (value&7)
        if bits is None:
            windows=self.db.execute(f'''SELECT w.* FROM window_observations w
                WHERE w.home=? AND w.at>=? AND w.at<=?{window_clause} AND NOT EXISTS ({referenced}) ORDER BY w.at,w.id''',params)
            window_rows=windows
        else:
            windows=self.db.execute('SELECT * FROM window_observations WHERE home=? AND at>=? AND at<=?'+window_clause+' ORDER BY at,id',params[:3])
            def unreferenced():
                for row in windows:
                    possible=all(bits[value>>3] & (1 << (value&7)) for value in positions(row['id']))
                    if not possible or not self.db.execute(referenced.replace('w.id','?'),(row['id'],*params[:3])).fetchone():
                        yield row
            window_rows=unreferenced()
        observations=None
        try:
            observations=self.db.execute(f'''SELECT COALESCE(r.window_id,o.id) AS id,o.home,o.at,o.source,o.account,
                COALESCE(c.plan,'') AS plan,COALESCE(c.bucket,'') AS bucket,'weekly' AS window,
                o.used,o.reset,o.minutes,o.separate
                FROM observations o LEFT JOIN weekly_window_refs r ON r.observation_id=o.id
                LEFT JOIN observation_context c ON c.id=o.id
                WHERE o.home=? AND o.at>=? AND o.at<=? AND (r.window_id IS NOT NULL OR (
                    NOT EXISTS (SELECT 1 FROM window_observations w WHERE w.home=o.home
                        AND w.window='weekly' AND w.at=o.at AND w.source IS o.source
                        AND w.account IS o.account AND w.used IS o.used AND w.reset IS o.reset
                        AND NOT EXISTS ({referenced}))
                    AND NOT EXISTS (SELECT 1 FROM observations p JOIN weekly_window_refs f ON f.observation_id=p.id
                        WHERE p.home=o.home AND p.at=o.at AND p.source IS o.source
                        AND p.account IS o.account AND p.used IS o.used AND p.reset IS o.reset
                        AND f.window_id IS NOT NULL)))
                ORDER BY o.at,COALESCE(r.window_id,o.id),r.window_id IS NULL,o.id''',params)
            yield from merge(window_rows,observations,key=lambda row:(row['at'],row['id']))
        finally:
            windows.close()
            if observations is not None:observations.close()

    @staticmethod
    def _forward_models(rows,separate):
        grouped={}
        for row in rows:grouped.setdefault((row['model'],row['service_tier']),[]).append(row)
        values=[]
        for (model,tier),own in grouped.items():
            priced=[r['cost'] for r in own if r['cost'] is not None]
            values.append(dict(model=model,service_tier=tier,displayed_tier=tier,
                calls=len(own),priced=len(priced),cost=sum(priced) if priced else None,
                mean_cost=sum(priced)/len(priced) if priced else None,
                unknown_mode_calls=len(own) if tier=='미확인' else 0,
                missing=len(own)-len(priced),prices=','.join(sorted({r['price_id'] for r in own})),
                separate=ALIASES.get(model,model) in separate,
                **{k:sum(r[k] for r in own) if all(r[k] is not None for r in own) else None for k in TOKEN_FIELDS}))
        return sorted(values,key=lambda r:r['cost'] or 0,reverse=True)

    def archive_calls(self,scope,cutoff,start,end,*,row_budget=65536):
        """Bound changed-period accounting without replaying quota observations."""
        calls,times,requests=self._report_calls(scope,cutoff,True,start,to_at=end,row_budget=row_budget)
        slots=','.join('?' for _ in scope)
        pending=[];weight=0
        for row in self.db.execute(f'''SELECT w.* FROM (
                SELECT * FROM tracking_wire WHERE home IN ({slots}) AND end IS NULL AND start<=?
                UNION ALL SELECT * FROM tracking_wire INDEXED BY tracking_wire_home_end
                WHERE home IN ({slots}) AND end>=? AND start<=?) w
            WHERE w.start>=?
            AND w.response<>'' AND NOT EXISTS(SELECT 1 FROM calls c WHERE c.home IN ({slots}) AND c.uid=w.response)
            LIMIT ?''',(*scope,end,*scope,start,end,cutoff,*scope,row_budget+1)):
            weight+=512+sum(64+4*len(value) for value in row if isinstance(value,str))
            if len(pending)>=row_budget or weight>32*1024*1024:raise ValueError('Quota repair pending request budget')
            pending.append(dict(row))
        return calls,times,requests,pending

    def _forward_cycle_costs(self,cycle,calls,times,pending):
        """Refresh call-derived fields while retaining confirmed quota endpoints."""
        if not cycle.get('forward_tracking') or not cycle.get('endpoints'):
            raise ValueError('Quota cycle cannot be repaired without observation replay')
        start,end=cycle['start'],cycle['end'];separate=set(cycle['separate_models'])
        def window(left,right):
            return [row for row in calls[bisect_right(times,left):bisect_right(times,right)]
                    if row.get('request_start') is None or row['request_start']>=left]
        rows=window(start,end)
        models=self._forward_models(rows,separate)
        cycle_models=self._forward_models(window(cycle['cycle_start'],cycle['cycle_end']),separate)
        eligible=[row for row in models if not row['separate']]
        pending_times=[row['end'] for row in pending if row['status']=='completed' and row['end'] is not None
                       and start<row['end']<=end and row['start']>=start]
        priced=sum(row['priced'] for row in eligible);missing=sum(row['missing'] for row in eligible)
        complete=cycle.get('cost_complete_at') is not None and cycle['cost_complete_at']>=end
        cost=sum(row['cost'] or 0 for row in eligible)
        if not priced and (eligible or pending_times or not complete):cost=None
        unknown=sum(row['unknown_mode_calls'] for row in eligible);standard=None
        if unknown:
            alternative=sum(row['cost'] or 0 for row in eligible);known=0
            for row in calls[bisect_right(times,start):bisect_right(times,end)]:
                if row['service_tier']!='미확인' or ALIASES.get(row['model'],row['model']) in separate:continue
                value=token_cost({**row,'service_tier':'Standard'})['cost']
                if value is not None:known+=1;alternative+=value
            if known==missing:standard=alternative
        excluded=sum(row.get('request_start') is not None and row['request_start']<start
                     for row in calls[bisect_right(times,start):bisect_right(times,end)]
                     if ALIASES.get(row['model'],row['model']) not in separate)
        assumptions=[]
        if missing:assumptions.append(f'단가 또는 토큰 자료 없는 {missing}호출은 비용 합계에서 제외')
        if excluded:assumptions.append(f'관측 시작 전에 진행된 {excluded}호출은 비용 합계에서 제외')
        assumptions.extend(value for value in cycle.get('assumptions',[]) if value.startswith('로컬 호출 기록 읽기 실패:'))
        return {**cycle,'models':models,'cycle_models':cycle_models,'cost':cost,
            'priced_calls':priced,'missing':missing,'all_cost':sum(row['cost'] or 0 for row in models),
            'pending_usage_times':pending_times,'pending_usage_calls':len(pending_times),
            'cost_complete':complete,'unknown_mode_calls':unknown,'mode_standard_cost':standard,
            'boundary_excluded_calls':excluded,'assumptions':list(dict.fromkeys(assumptions)),
            'cost_rows':[{**{key:row[key] for key in ('uid','ts','model','service_tier','cost',*TOKEN_FIELDS)},
                          'request_start':row.get('request_start'),'local_request_start':row.get('local_request_start')}
                         for row in rows]}

    def _report_calls(self, scope, cutoff, forward, from_at=None, *, to_at=None, row_budget=None):
        """Reuse immutable request joins while only quota observations change."""
        packed=forward and getattr(self,'_pack_history',False)
        changes=getattr(self,'_input_changes',None)
        cacheable=packed and changes is not None and to_at is None and row_budget is None
        key=(tuple(scope),cutoff,from_at)
        previous=getattr(self,'_request_inputs',None) if cacheable else None
        if (previous and previous[0]==key and not any(table in
                ('calls','tracking_wire','tracking_call_boundaries') for table,_ in changes)):
            return previous[1:4]
        if cacheable:self._request_inputs=None
        slots=','.join('?' for _ in scope)
        # The primary key already makes response IDs unique within one home.
        columns='home,uid,ts,model,'+','.join(TOKEN_FIELDS)+',cost,price_id,service_tier'
        selected=None;selected_params=()
        if from_at is not None and forward:
            upper=' AND ts<=?' if to_at is not None else ''
            wire_upper=' AND end<=?' if to_at is not None else ''
            candidates=f'''SELECT uid FROM calls WHERE home IN ({slots}) AND ts>=?{upper}
                UNION SELECT response FROM tracking_wire WHERE home IN ({slots}) AND status='completed' AND end>=?{wire_upper}'''
            source=f'({candidates}) chosen CROSS JOIN calls c ON c.uid=chosen.uid'
            condition=f'c.home IN ({slots}) AND c.ts>=? AND c.model<>?'
            selected=f'SELECT c.uid FROM {source} WHERE {condition}'
            bound=(from_at,to_at) if to_at is not None else (from_at,)
            selected_params=(*scope,*bound,*scope,*bound,*scope,cutoff,INTERNAL_REVIEW_MODEL)
            if len(scope)==1:
                calls=self.db.execute('SELECT '+','.join('c.'+column for column in columns.split(','))+
                    f' FROM {source} WHERE {condition} ORDER BY c.ts,c.uid',selected_params)
            else:
                calls=self.db.execute(f'''SELECT {columns} FROM (SELECT c.*,
                    row_number() over(partition by c.uid order by c.cost IS NULL,c.home) AS copy_rank
                    FROM {source} WHERE {condition}) WHERE copy_rank=1 ORDER BY ts''',selected_params)
        elif len(scope)==1:
            calls=self._report_rows('calls',f'select {columns} from calls where home=? and ts>=? '
                'and model<>? order by ts,uid',(*scope,cutoff,INTERNAL_REVIEW_MODEL),('calls',),'ts')
        else:
            calls=self.db.execute(f'''select {columns} from (
                select *,row_number() over(partition by uid order by cost is null,home) as copy_rank
                from calls where home in ({slots}) and ts>=? and model<>?) where copy_rank=1 order by ts''',
                (*scope,cutoff,INTERNAL_REVIEW_MODEL))
        request_windows=[]
        if forward:
            reuse_windows=bool(previous and previous[0]==key and
                               not any(table=='tracking_wire' for table,_ in changes))
            completed={};exact_starts={home:{} for home in scope};by_response=None if reuse_windows else {}
            wire_source='tracking_wire w';wire_params=()
            if selected is not None:
                # Include every version of a relevant response so duplicate
                # completion and exact-start precedence remain unchanged.
                if to_at is None:
                    wire_candidates=f'''SELECT uid AS response FROM ({selected})
                        UNION SELECT response FROM tracking_wire WHERE home IN ({slots}) AND end IS NULL
                        UNION SELECT response FROM tracking_wire WHERE home IN ({slots}) AND end>=?
                        UNION SELECT response FROM tracking_wire WHERE home IN ({slots}) AND start>=?'''
                    wire_params=(*selected_params,*scope,*scope,from_at,*scope,from_at)
                else:
                    wire_candidates=f'''SELECT uid AS response FROM ({selected})
                        UNION SELECT response FROM tracking_wire WHERE home IN ({slots}) AND end IS NULL AND start<=?
                        UNION SELECT response FROM tracking_wire WHERE home IN ({slots}) AND end>=? AND start<=?'''
                    wire_params=(*selected_params,*scope,to_at,*scope,from_at,to_at)
                wire_source=f'({wire_candidates}) chosen CROSS JOIN tracking_wire w ON w.response=chosen.response'
            budget_count=0;budget_bytes=0
            def admit(row):
                nonlocal budget_count,budget_bytes
                if row_budget is None:return
                budget_count+=1
                budget_bytes+=512+sum(64+4*len(value) for value in row if isinstance(value,str))
                if budget_count>row_budget or budget_bytes>32*1024*1024:
                    raise ValueError('Quota repair request budget')
            # The ordinary home/end index supplied this precedence before
            # range queries gained a response index. Keep it explicit.
            for row in self.db.execute(f"SELECT w.* FROM {wire_source} WHERE w.home IN ({slots}) AND w.response<>'' ORDER BY w.home,w.end,w.rowid",(*wire_params,*scope)):
                if row_budget is not None:admit(row)
                if row['status']=='completed' and row['end'] is not None:
                    completed[row['response']]=min(completed.get(row['response'],row['end']),row['end'])
                exact_starts[row['home']][row['response']]=row['start']
                if by_response is None or row['status'] not in ('created','completed'):continue
                old=by_response.get(row['response'])
                if old is None or (row['end'] is not None and old[1] is None):
                    by_response[row['response']]=(row['start'],row['end'])
            # Keep only fallback boundaries. Exact wire starts already own the
            # ordinary case, including an explicitly missing start.
            boundaries={home:{} for home in scope}
            boundary_condition=f' AND uid IN ({selected})' if selected is not None else ''
            for row in self.db.execute(f'select * from tracking_call_boundaries where home in ({slots})'+boundary_condition,
                                       (*scope,*selected_params) if selected is not None else scope):
                if row_budget is not None:admit(row)
                if row['uid'] not in exact_starts[row['home']]:boundaries[row['home']][row['uid']]=row['started']
            if reuse_windows:
                request_windows=previous[3]
            else:
                windows=(dict(uid=uid,start=span[0],end=span[1]) for uid,span in by_response.items())
                request_windows=_PackedRows(windows) if packed else list(windows)
                del windows
            del by_response
            # Preserve completion correction and the original stable tie order
            # without retaining all decoded call dictionaries beside the join.
            times=array('d');ordered=True
            def corrected(source):
                nonlocal ordered
                last=None
                for row in source:
                    if row_budget is not None:admit(row)
                    value={**dict(row),'ts':completed.get(row['uid'],row['ts']),
                        'request_start':exact_starts[row['home']].get(row['uid'],boundaries[row['home']].get(row['uid'])),
                        'local_request_start':exact_starts[row['home']].get(row['uid'])}
                    if packed:
                        if last is not None and value['ts']<last:ordered=False
                        last=value['ts'];times.append(last)
                    yield value
            if packed and isinstance(calls,list) and len(calls)<=self.REQUEST_SORT_ROWS:
                # This SQL input already fits the bounded input cache. A small
                # dictionary buffer borrows its values and avoids packing twice
                # when exact completion timestamps change the source order.
                staged=sorted(corrected(calls),key=lambda row:row['ts'])
                times=array('d',(row['ts'] for row in staged));ordered=True
                staged.reverse()
                def consume():
                    while staged:yield staged.pop()
                calls=_PackedRows(consume())
            else:
                calls=_PackedRows(corrected(calls)) if packed else sorted(corrected(calls),key=lambda row:row['ts'])
            del completed,boundaries,exact_starts
            if packed and not ordered:
                source=calls
                order=sorted(range(len(source)),key=lambda i:source[i]['ts'])
                times=array('d')
                def reordered():
                    for i in order:
                        row=source[i];times.append(row['ts']);yield row
                calls=_PackedRows(reordered())
                del source,order
        else:calls=list(calls)
        if not packed:return calls,[row['ts'] for row in calls],request_windows
        weight=len(times)*8+sum(sum(len(block)+64 for block in rows.blocks)+len(rows.ends)*36
                               for rows in (calls,request_windows))
        if cacheable and weight<=self.REQUEST_CACHE_BYTES:
            self._request_inputs=(key,calls,times,request_windows,weight)
        return calls,times,request_windows

    def _report(self, home, now=None, *, exclusion_only=False, from_at=None,edit_start=None,edit_history=True):
        now = time.time() if now is None else now
        if exclusion_only:
            cached=getattr(self,'_report_cache',None)
            if cached and cached['home']==home and cached['at']<=now<cached['until']:
                revision=self.db.execute('SELECT value FROM report_revision WHERE id=1').fetchone()[0]
                if revision==cached['revision']:
                    # Edit validation needs the same immutable observed fields.
                    # The enclosing snapshot and later data_version check still
                    # reject a concurrent writer before applying the edit.
                    return {key:cached['report'][key] for key in
                            ('home','account','cycle_clears','exclusions','tracking','history')}
        tables = {r[0] for r in self.db.execute("select name from sqlite_master where type='table'")}
        config=self.db.execute('select started from tracking_config where home=?',(home,)).fetchone() if 'tracking_config' in tables else None
        cutoff=config[0] if config else float('-inf')
        from .quota_tracking_store import build
        tracking = build(self.db, home, now, context_only=exclusion_only, read_rows=self._report_rows,
                         pack_groups=getattr(self,'_pack_history',False),
                         replay_cache=getattr(self,'_tracking_inputs',None) if getattr(self,'_input_changes',None) is not None else None,
                         changes=getattr(self,'_input_changes',None),request_from=from_at)
        current_account = tracking.get('account') if tracking is not None else None
        if not current_account:
            latest=self.db.execute("select account from observations where home=? and at>=? and at<=? "
                "and typeof(used) in ('integer','real') and used between 0 and 100 "
                "and minutes=10080 and account<>'' order by at desc,id desc limit 1",(home,cutoff,now)).fetchone()
            current_account=latest[0] if latest else ''
        context = dict(home=home,account=current_account,
            cycle_clears=[dict(r) for r in self.db.execute(
                'select * from quota_cycle_clears where home=? and account=? and end<=? order by start',
                (home,current_account,now))],
            exclusions=([dict(r) for r in self.db.execute(
                'select * from quota_exclusions where home=? and account=? and restored_at is null order by start,created_at,id',
                (home,current_account))] if 'quota_exclusions' in tables else []),
            tracking=({k:v for k,v in tracking.items() if k!='groups'} if tracking is not None else None))
        # Editing needs observed boundaries and account ownership, not call costs.
        # Keep this inside the same snapshot/revision check as the full report.
        if exclusion_only:
            if not edit_history:return context
            from .quota_archive import load as load_archive
            from .quota_projection import load as load_projection
            archive=load_archive(self,home,now,allow_repair=False)
            projection=load_projection(self.db,home,now)
            if (archive is not None and projection and not projection.get('invalid')
                    and archive.account==current_account==projection['account']
                    and projection['start']<=archive.cutoff):
                from .quota_view import _history_rows
                context.update(history=self._report_history(home,projection['start'],now,tables),
                               _history_resume=projection['resume'])
                starts=[p['start'] for p in archive.head['periods']];last=None
                for last in _history_rows(context,'weekly',labels=False,_resume=projection['resume']):
                    if not starts or last.get('reset_kind') or last['at']==projection['start']:
                        starts.append(last['at'])
                context.update(_edit_periods=sorted(set(starts)),_edit_last=last)
                period=next((p for p in archive.head['periods']
                             if edit_start is not None and p['start']<=edit_start<=p['end']),None)
                if period is not None:
                    source=archive.packet('period',period['start'])['source']
                    context['_edit_period']=dict(start=period['start'],end=period['end'],
                                                 times=source['classification'][0])
                return context
            context['history']=self._report_history(home,cutoff,now,tables)
            return context
        manual_resets = [dict(r) for r in self.db.execute(
            'select * from manual_resets where home=? and at<=? order by at,id', (home,now))]
        scope = tracking['homes'] if tracking is not None else [home]
        slots = ','.join('?' for _ in scope)
        if tracking is not None:
            groups = tracking['groups']
        else:
            observations=[dict(r) for r in self._observations(home,cutoff,now)
                          if type(r['used']) in (int,float) and math.isfinite(r['used'])
                          and 0<=r['used']<=100 and r['minutes']==10080]
            groups = split_cycles(observations, manual_resets)
            if groups:groups[-1]['provisional']=True
        states=list(self.db.execute(f'select * from state where home in ({slots})',scope))
        state=next((row for row in states if row['home']==home),None)
        completed_at=(state['complete_at'] if 'complete_at' in state.keys() else state['at'] if not state['loading'] else None) if state else None
        if tracking is not None:
            completed_at=min((r['complete_at'] for r in states),default=None) if len(states)==len(scope) and all(r['complete_at'] is not None for r in states) else None
        issues = ([dict(r) for r in self.db.execute(f'select * from coverage_issues where home in ({slots})',scope)]
                  if 'coverage_issues' in tables else [])
        call_start=min((group['observations'][0]['at'] for group in groups if group['observations']),default=from_at) if from_at is not None else None
        calls,call_times,request_windows=self._report_calls(scope,cutoff,tracking is not None,call_start)
        time_cache={}
        for group in groups if tracking is None else ():
            if group.get('gap') or group.get('discontinuous'):continue
            separate=set().union(*(json.loads(value) for value in {r['separate'] for r in group['observations']}))
            key=frozenset(separate)
            if key not in time_cache:
                time_cache[key]=[r['ts'] for r in calls if ALIASES.get(r['model'],r['model']) not in separate]
            times=time_cache[key]
            if group['observations']:
                start,end=group['observations'][0]['at'],group['observations'][-1]['at']
                if isinstance(group['observations'],_PackedRows):group['observations']._cache.clear()
                for row in calls[bisect_right(call_times,start):bisect_right(call_times,end)]:
                    if row['cost'] is None and start<row['ts']<=end and ALIASES.get(row['model'],row['model']) not in separate:
                        issues.append({'start':row['ts'],'end':row['ts'],
                            'reason':'요청 모드 미확인' if row['service_tier']=='미확인' and token_cost({**dict(row),'service_tier':'Standard'})['cost'] is not None else '필수 토큰 또는 단가 누락'})
            # A rise after a confirmed unchanged endpoint with no intervening
            # calls is unresolved attribution, irrespective of tick size.
            obs=group['observations']
            for position,(a,b) in enumerate(zip(obs,obs[1:]) if tracking is None else ()):
                plateau=position
                while plateau>0 and obs[plateau-1]['used']==a['used']:plateau-=1
                if b['used']>a['used'] and bisect_right(times,b['at'])==bisect_right(times,obs[plateau]['at']):
                    issues.append({'start':a['at'],'end':b['at'],
                                   'reason':'로컬 호출 없는 한도 증가: 소모 귀속 미확인'})
        if tracking is None:
            groups = partition_coverage(groups, issues)
        if not hasattr(self, '_mode_alternatives'):
            self._mode_alternatives = {}
        result = []
        previous_cycles=(getattr(self,'_cycle_cache',{}) if tracking is not None and
                         getattr(self,'_input_changes',None) is not None else {})
        cycle_cache={}
        self._cycle_rebuilds=0
        def window_calls(start, end):
            rows = calls[bisect_right(call_times,start):bisect_right(call_times,end)]
            if tracking is not None:
                rows = [r for r in rows if r.get('request_start') is None or r['request_start'] >= start]
            return rows

        def model_rows(start, end, separate):
            if tracking is not None:
                return self._forward_models(window_calls(start,end),separate)
            fields = ','.join(f'case when count({k})=count(*) then sum({k}) else null end as {k}' for k in TOKEN_FIELDS)
            rows = self.db.execute(f'''select model,
                service_tier as displayed_tier,
                count(*) as calls,count(cost) as priced,avg(cost) as mean_cost,{fields},sum(cost) as cost,
                sum(service_tier='미확인') as unknown_mode_calls,
                sum(cost is null) as missing,group_concat(distinct price_id) as prices
                from calls where home=? and ts>? and ts<=? and model<>? group by model,displayed_tier order by sum(cost) desc''', (home,start,end,INTERNAL_REVIEW_MODEL))
            return [{**dict(row),'service_tier':row['displayed_tier'],
                     'separate': ALIASES.get(row['model'],row['model']) in separate} for row in rows]
        for i, group in enumerate(groups):
            obs = group['observations']
            marker = group.get('manual_reset')
            if not obs and not marker and group.get('rejected'):
                obs = [group['rejected'][0],group['rejected'][-1]]
                group['discontinuous'] = True
            next_group = groups[i+1] if i+1 < len(groups) else None
            cycle_end = next_group['boundary'] if next_group else now
            # Exact input equality also catches backfilled costs, model changes,
            # reset-boundary corrections and request completion corrections.
            # The current time and a completed coverage heartbeat are metadata,
            # so they do not invalidate a closed accounting interval.
            signature=None
            if tracking is not None and obs:
                lo=group['boundary'];hi=cycle_end
                coverage=min(completed_at,obs[-1]['at']) if completed_at is not None else None
                left,right=bisect_right(call_times,lo),bisect_right(call_times,hi)
                signature=({**group,'observations':obs.signature() if isinstance(obs,_PackedRows) else obs},(next_group['boundary'],next_group['reason'],next_group.get('manual_reset')) if next_group else None,
                           calls.signature(left,right) if isinstance(calls,_PackedRows) else calls[left:right],
                           [r for r in tracking.get('pending_responses',[]) if r['start']<=hi and (r['end'] is None or r['end']>=lo)],
                           issues,coverage,tracking['complete'],state['unclassified'] if state else None)
                # Retain a content identity, not a second owner of every raw
                # observation and call after the accounting result is built.
                signature=hashlib.sha256(pickle.dumps(signature,protocol=5)).digest()
                cached=previous_cycles.get(group['id'])
                if cached and cached[0]==signature:
                    cycle={**cached[1],'cost_complete_at':completed_at}
                    if not next_group and not cycle['uncertain_gap']:cycle['cycle_end']=now
                    result.append(cycle);cycle_cache[group['id']]=(signature,cycle)
                    continue
            if isinstance(obs,_PackedRows):obs=list(obs)
            self._cycle_rebuilds+=1
            if not obs:
                result.append({'id':group['id'], 'reason':group['reason'],
                    'start':group['boundary'], 'end':group['boundary'],
                    'reset':None, 'used_start':None, 'used_end':None, 'delta':0,
                    'observations':0, 'models':[], 'cost':None,
                    'cycle_start':group['boundary'], 'cycle_end':cycle_end,
                    'cycle_models':model_rows(group['boundary'],cycle_end,set()),
                    'uncertain_gap':False, 'all_cost':0, 'missing':0, 'separate_models':[],
                    'blocked':['리셋권 사용 기록 후 새 한도 조회 대기'],
                    'partial':True, 'live':False, 'account':marker['account'],
                    'manual_reset_id':marker['id'], 'reset_pending':True,
                    'unclassified_sessions':state['unclassified'] if state else None})
                continue
            if tracking is not None:
                first,last=obs[0],obs[-1];start,end=first['at'],last['at']
                separate=set().union(*(json.loads(value) for value in {row['separate'] for row in obs}))
                uncertain_gap=bool(next_group and next_group['reason']!='예정 리셋 확인' and not next_group.get('manual_reset'))
                if uncertain_gap or group.get('gap'):cycle_end=last['at']
                base=dict(id=group['id'],reason=group['reason'],start=start,end=end,reset=last['reset'],
                    used_start=first['used'],used_end=last['used'],delta=last['used']-first['used'],
                    observations=len(obs),endpoints=[(row['at'],row['used']) for row in obs],forward_tracking=True,
                    finished_at=group.get('finished_at'),boundary_uncertain=group.get('boundary_uncertain',False),
                    cost_complete_at=completed_at,cycle_start=group['boundary'],cycle_end=cycle_end,
                    uncertain_gap=bool(uncertain_gap or group.get('gap')),separate_models=sorted(separate),blocked=[],
                    partial=first['used']!=0,live=any(row['source']=='live' for row in obs),pending=False,
                    assumptions=[issue['reason'] for issue in issues if issue['reason'].startswith('로컬 호출 기록 읽기 실패:')],
                    provisional=bool(group.get('provisional')) and not group.get('gap') and not group.get('discontinuous'),
                    source=last['source'],plan=last.get('plan',''),bucket=last.get('bucket',''),
                    rejected_observations=len(group.get('rejected',[])),discontinuous=bool(group.get('discontinuous')),
                    account=last['account'],manual_reset_id=marker['id'] if marker else None,reset_pending=False,
                    observation_gap=bool(group.get('gap')),unclassified_sessions=state['unclassified'] if state else None)
                result.append(self._forward_cycle_costs(base,calls,call_times,tracking.get('pending_responses',[])))
                if signature is not None:cycle_cache[group['id']]=(signature,result[-1])
                continue
            # Never mix legacy/unknown account observations with live-account calibration.
            live = [r for r in obs if r['source'] == 'live']
            matched = obs
            pending = False
            if completed_at is not None:
                collected = [r for r in matched if r['at'] <= completed_at]
                if len(collected) >= 2:
                    matched = collected
                else:
                    pending = True
            else:pending=True
            first, last = matched[0], matched[-1]
            start, end = first['at'], last['at']
            separate = set().union(*(json.loads(value) for value in {r['separate'] for r in matched}))
            models = model_rows(start,end,separate)
            uncertain_gap = bool(next_group and next_group['reason']!='예정 리셋 확인'
                                 and not next_group.get('manual_reset'))
            if uncertain_gap: cycle_end = obs[-1]['at']
            if group.get('gap'):
                cycle_end = obs[-1]['at']
                uncertain_gap = True
            cycle_models = model_rows(group['boundary'],cycle_end,separate)
            eligible = [r for r in models if not r['separate']]
            pending_usage_times = []
            priced_count = sum(r['priced'] for r in eligible)
            cost_complete = completed_at is not None and completed_at >= end
            cost = sum(r['cost'] or 0 for r in eligible)
            missing = sum(r['missing'] for r in eligible)
            if not eligible or missing: cost = None
            blocked = []
            if group.get('boundary_pending'):
                blocked.append('요청 진행 중 경계: 비용 분리 불가')
            assumptions = []
            blocked.extend(sorted(group.get('coverage_reasons',[])))
            if group.get('discontinuous'):
                blocked.append(DISCONTINUITY)
            if group.get('gap'):
                blocked.append('30분 초과 관측 공백: 소모량과 로컬 비용 대응 불가')
            if not live:
                assumptions.append('로컬 기록 관측')
            if not first['account'] or not last['account']:
                blocked.append('계정 식별 미확인')
            if any(r.get('plan') not in ('free','plus','pro','team','business','enterprise','edu')
                   or r.get('bucket') != 'codex' for r in matched):
                blocked.append('요금제 또는 한도 정보 미확인')
            if any(r['reset'] and r['at'] >= r['reset'] for r in matched):
                blocked.append('관측 시점에 만료된 한도')
            if any(not r['reset'] for r in matched):
                blocked.append('초기화 시각 미확인')
            unknown = sum(r['unknown_mode_calls'] for r in eligible)
            mode_standard_cost = None
            mode_only_missing = 0
            if unknown:
                blocked.append(f'요청 모드 미확인 {unknown}개 호출')
                assumptions.append(f'요청 모드 미확인 {unknown}개 호출: Standard 단가 가정 가능')
                alternative_sum = sum(r['cost'] or 0 for r in eligible)
                for row in (r for r in calls[bisect_right(call_times,start):bisect_right(call_times,end)] if r['service_tier']=='미확인'):
                    if ALIASES.get(row['model'],row['model']) in separate:
                        continue
                    key=(row['model'],*[row[k] for k in TOKEN_FIELDS],row['price_id'])
                    if key not in self._mode_alternatives:
                        if len(self._mode_alternatives)>=4096:
                            self._mode_alternatives.pop(next(iter(self._mode_alternatives)))
                        self._mode_alternatives[key]=token_cost({**dict(row),'service_tier':'Standard'})['cost']
                    alternative=self._mode_alternatives[key]
                    if alternative is not None:
                        mode_only_missing += 1
                        alternative_sum += alternative
                if mode_only_missing==missing:
                    mode_standard_cost=alternative_sum
            if pending:
                blocked.append('한도 시점까지 호출 수집 대기')
            if group.get('rejected') and not group.get('discontinuous'):
                assumptions.append(f"상충, 역행 관측 {len(group['rejected'])}건을 끝점에서 제외, 순소모 사용")
            for issue in issues:
                if issue['reason'].startswith(('누계 차액 증가','로컬 호출 없는 한도 증가','필수 토큰 또는 단가 누락','요청 모드 미확인')):
                    continue
                elif not issue['reason'].startswith('진행 중'):
                    if (issue['start'] is None or issue['start'] <= end) and (issue['end'] is None or issue['end'] > start):
                        assumptions.append('누계, 수집 근거 일부 미확인: 기간 전체 누락으로 단정하지 않음')
            if not issues and state and state['unclassified']:
                assumptions.append('미분류 차액 있음: 구간별 증가 여부 확인 필요')
            if live and i == len(groups)-1:
                assumptions.append('진행 중 관측 구간: 서버 반영 지연에 따라 갱신됨')
            if completed_at is None or completed_at < end:
                blocked.append('해당 구간의 토큰 수집 완료 대기')
            if missing > mode_only_missing:
                blocked.append(f'단가/토큰 미확인 {missing-mode_only_missing}개 호출')
            if not sum(r['calls'] for r in eligible):
                blocked.append('대응하는 모델 호출 없음')
            boundary_excluded = 0
            # The denominator is actual account consumption in the same ON window.
            delta = last['used'] - first['used']
            result.append({'id': group['id'], 'reason': group['reason'], 'start': start, 'end': end,
                'reset': last['reset'], 'used_start': first['used'], 'used_end': last['used'],
                'delta': delta, 'observations': len(matched), 'models': models, 'cost': cost,
                'endpoints':[(r['at'],r['used']) for r in matched],
                'forward_tracking':False,
                'finished_at':group.get('finished_at'),
                'boundary_uncertain':group.get('boundary_uncertain',False),
                'boundary_excluded_calls':boundary_excluded,
                'priced_calls':priced_count,
                'cost_complete':cost_complete, 'cost_complete_at':completed_at,
                'pending_usage_calls':len(pending_usage_times),
                'pending_usage_times':pending_usage_times,
                'cost_rows':[{**{k:r[k] for k in ('uid','ts','model','service_tier','cost',*TOKEN_FIELDS)},
                              'request_start':r['request_start'] if 'request_start' in r.keys() else None,
                              'local_request_start':r['local_request_start'] if 'local_request_start' in r.keys() else None}
                             for r in window_calls(start,end)],
                'cycle_start': group['boundary'], 'cycle_end': cycle_end, 'cycle_models': cycle_models,
                'uncertain_gap': uncertain_gap,
                'all_cost': sum(r['cost'] or 0 for r in models), 'missing': missing,
                'separate_models': sorted(separate), 'blocked': blocked,
                'partial': first['used'] != 0, 'live': bool(live),
                'pending':pending, 'assumptions':list(dict.fromkeys(assumptions)),
                'mode_standard_cost':mode_standard_cost, 'unknown_mode_calls':unknown,
                'provisional':bool(group.get('provisional')) and not group.get('gap') and not group.get('discontinuous'),
                'source':last['source'], 'plan':last.get('plan',''), 'bucket':last.get('bucket',''),
                'rejected_observations':len(group.get('rejected',[])),
                'discontinuous':bool(group.get('discontinuous')),
                'account': last['account'],
                'manual_reset_id':marker['id'] if marker else None, 'reset_pending':False,
                'observation_gap':bool(group.get('gap')),
                'unclassified_sessions': state['unclassified'] if state else None})
            if signature is not None:cycle_cache[group['id']]=(signature,result[-1])
        relevant_resets = [r for r in manual_resets if not current_account or not r['account'] or r['account']==current_account]
        last_reset = relevant_resets[-1] if relevant_resets else None
        pending = any(r.get('reset_pending') and r['manual_reset_id']==last_reset['id'] for r in result) if last_reset else False
        if tracking is not None:pending=False
        if getattr(self,'_input_changes',None) is not None:self._cycle_cache=cycle_cache
        # Raw tracking rows and corrected call dictionaries have no consumer
        # after accounting. Release them before loading chart history.
        if tracking is not None:
            tracking.pop('groups',None)
        else:
            del observations
        groups.clear();del calls
        context['history']=self._report_history(home,max(cutoff,from_at) if from_at is not None else cutoff,now,tables)
        return {**({'request_windows':request_windows} if tracking is not None else {}),
                **context,
                'manual_resets':manual_resets,'cycles': list(reversed(result)), 'price_id': PRICE_ID, 'index_at': state['at'] if state else None,
                'index_loading': bool(state['loading']) if state else True,
                '_projection_coverage':{r['home']:(r['unclassified'],r['complete_at']) for r in states},
                'index_complete_at':completed_at, 'at': now,
                'last_manual_reset':last_reset, 'manual_reset_count':len(relevant_resets),
                'manual_reset_pending':pending}

    def release_report_cache(self):
        """Hand retained raw history back after a bounded projection takes over."""
        self._input_cache={};self._cycle_cache={};self._report_cache=None;self._request_inputs=None;self._history_inputs=None;self._tracking_inputs={}
        self._input_revision=None;self._input_changes=None

    def close(self):
        job=getattr(self,'_observation_import',None)
        if job is not None:job[1].close()
        self.db.close()
        self.release_report_cache()
