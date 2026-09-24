"""Persistent quota observations and local response costs, with matched-window estimates."""
from dataclasses import asdict
from bisect import bisect_left, bisect_right
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .pricing import ALIASES, RATES, FAST_RATES, VERIFIED, PRICE_POLICY, token_cost, request_tier


TOKEN_FIELDS = ('input', 'cached', 'written', 'output', 'reasoning')
PRICE_SNAPSHOT = json.dumps({'rates': {k: asdict(v) for k, v in RATES.items()},
                             'fast_rates': {k:asdict(v) for k,v in FAST_RATES.items()}, 'policy':PRICE_POLICY, 'aliases': ALIASES, 'verified': VERIFIED}, sort_keys=True)
PRICE_ID = hashlib.sha256(PRICE_SNAPSHOT.encode()).hexdigest()[:16]
RESET_TOLERANCE = 120
MAX_OBSERVATION_GAP = 30 * 60
DISCONTINUITY = '사용률 불연속: 감소·충돌 후 복귀량은 신규 소모로 계산하지 않음'


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
            groups.append({'id':'uncertain:'+tail[0]['id'], 'reason':'사용률 불연속 · 끝점 미확정',
                           'boundary':obs[-1]['at'], 'observations':[obs[-1],tail[-1]],
                           'rejected':tail, 'discontinuous':True})

    timeline = [(r['at'],1,r['id'],r) for r in observations]
    timeline += [(r['at'],0,r['id'],r) for r in manual_resets]
    for _, kind, _, item in sorted(timeline, key=lambda r:r[:3]):
        if kind == 0:
            close_tail()
            epochs.clear()  # Explicit replenishment may keep the same reset timestamp.
            append_group(item, '리셋권 사용 · 사용자 기록', manual_reset=item)
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
            reason='요금제·한도 변경'
        elif item['minutes']!=previous['minutes']:
            reason='한도 창 변경 · 경계 미확인'
        elif item['reset'] and previous['reset'] and abs(item['reset']-previous['reset'])>RESET_TOLERANCE:
            shift=item['reset']-previous['reset']
            natural=item['at']>=previous['reset']-RESET_TOLERANCE and abs(shift-item['minutes']*60)<=RESET_TOLERANCE
            reason='예정 리셋 확인' if natural else '한도 시각 변경 · 관측 구간 분리'
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
        reasons.append('API 환산액 미확인')
    if not 0 <= n <= 100:
        reasons.append('n은 0~100 범위여야 합니다')
    if reasons:
        return {'usd': None, 'per_percent': None, 'sensitivity': None, 'reasons': list(dict.fromkeys(reasons))}
    return {'usd': cost / delta * n, 'per_percent': cost / delta,
            'sensitivity': (cost / (delta + 1) * n, cost / (delta - 1) * n) if delta > 1 else None, 'reasons': []}


def partition_coverage(groups, issues):
    """Cut only observation edges intersecting an evidenced counter discrepancy."""
    gaps = [r for r in issues if r['reason'].startswith(('누계 차액 증가','로컬 호출 없는 한도 증가','필수 토큰·단가 누락','요청 모드 미확인'))
            and r['start'] is not None and r['end'] is not None]
    indexed={}
    for gap in gaps:
        reason=('누계 차액 증가: 호출 비용 누락·누계 보정 미해결'
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
            if part['coverage_blocked']:part['reason']='비용·소모 대응 미확인 구간'
            elif index:part['reason']='대응 미확인 경계 이후 관측'
            result.append(part)
    return result


def _clip_quota_interval(cycle, start=None, end=None):
    endpoints=[r for r in cycle.get('endpoints',[]) if
               (start is None or r[0]>=start) and (end is None or r[0]<=end)]
    if len(endpoints)<2:return None
    lo,hi=endpoints[0][0],endpoints[-1][0]
    candidates=[r for r in cycle.get('cost_rows',[]) if lo<r['ts']<=hi]
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
    return {**cycle,'pending_usage_times':pending,'pending_usage_calls':len(pending),
            'start':lo,'end':hi,'delta':endpoints[-1][1]-endpoints[0][1],'models':models,
            'endpoints':endpoints,'cost_rows':rows,'used_start':endpoints[0][1],'used_end':endpoints[-1][1],
            'cost':(sum(r['cost'] for r in eligible if r['cost'] is not None) if any(r['cost'] is not None for r in eligible)
                    else 0 if not eligible and cycle.get('cost_complete') and not pending else None)
                   if cycle.get('forward_tracking') else
                   sum(r['cost'] for r in eligible) if eligible and all(r['cost'] is not None for r in eligible) else None,
            'priced_calls':sum(r['cost'] is not None for r in eligible),
            'boundary_excluded_calls':(cycle.get('boundary_excluded_calls',0) if lo==cycle['start'] else 0)+len(candidates)-len(rows),
            'mode_standard_cost':sum(alternatives) if alternatives and all(c is not None for c in alternatives) else None}


def _continuous_quota_intervals(cycles):
    """Exclude unobserved edges and their costs, retaining both observed sides."""
    from .quota_tracking import OBSERVATION_FRESHNESS
    for cycle in cycles:
        runs=[]
        for endpoint in cycle.get('endpoints',[]):
            if not runs or endpoint[0]-runs[-1][-1][0]>OBSERVATION_FRESHNESS:
                runs.append([])
            runs[-1].append(endpoint)
        if len(runs)<=1:
            yield cycle
        else:
            for run in runs:
                clipped=_clip_quota_interval(cycle,run[0][0],run[-1][0])
                if clipped is not None:
                    yield {**clipped,'id':f"{cycle['id']}:observed:{run[0][0]}",
                           'observation_gaps_excluded':True}


def quota_statistics(report, start=None, end=None, include_mode_assumptions=False):
    """Use complete disjoint intervals; preserve the base result under assumptions."""
    from .analytics import quantile
    intervals = []
    current_account = report.get('account') or next(
        (c.get('account') for c in report.get('cycles', []) if c.get('account')), '')
    scoped=[]
    for cycle in _continuous_quota_intervals(report.get('cycles', [])):
        if (start is not None and cycle.get('start',0)<start) or (end is not None and cycle.get('end',float('inf'))>end):
            cycle=_clip_quota_interval(cycle,start,end)
            if cycle is None:continue
        scoped.append(cycle)
    attribution=dict(idle_delta=0,pending_delta=0,unmatched_calls=0,pending_windows=0)
    account_delta=sum(max(0,c['delta']) for c in scoped if not current_account or c.get('account')==current_account)
    if report.get('tracking') is not None:
        from .quota_attribution import local_request_intervals
        local=[]
        for cycle in scoped:
            if current_account and cycle.get('account')!=current_account:continue
            parts,diagnostics=local_request_intervals(cycle,report.get('request_windows',[]),_clip_quota_interval)
            local.extend(parts)
            for key in attribution:attribution[key]+=diagnostics[key]
        scoped=local
    for cycle in scoped:
        blockers = list(cycle['blocked'])
        if current_account and cycle.get('account') and cycle['account'] != current_account:
            blockers.append('다른 계정의 관측 구간')
        result = estimate(cycle['cost'], cycle['delta'], 1, blockers)
        if cycle.get('forward_tracking') and cycle['delta'] == 0 and not blockers:
            result['reasons'] = []
        waiting = cycle.get('pending') or cycle.get('reset_pending') or any('수집' in r and '대기' in r for r in blockers)
        insufficient = (cycle['delta'] == 0 and all(
            '소모율 변화' in r or r in ('대응하는 모델 호출 없음','API 환산액 미확인')
            for r in result['reasons']))
        assumption_blockers = [r for r in blockers if not r.startswith('요청 모드 미확인')]
        assumption = estimate(cycle.get('mode_standard_cost'),cycle['delta'],1,assumption_blockers)
        assumption_only = bool(cycle.get('unknown_mode_calls')) and not assumption['reasons']
        status = ('잠정' if cycle.get('provisional') else '사용') if not result['reasons'] else (
            '수집 대기' if waiting else '가정 필요' if assumption_only else
            '누적 대기' if insufficient else '제외')
        if cycle.get('forward_tracking'):
            status = ' · '.join(cycle.get('assumptions', []))
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
        total_cost = sum(known_costs) if known_costs else 0 if not observed and report['tracking'].get('complete') else None
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


def quota_value_history(report, rows):
    """Align monetary evidence to the chart's *observed* reset boundaries.

    Reset classification belongs to history_rows/ResetTracker. Storage intervals
    are accounting segments, not necessarily whole allowance cycles. Preserve
    those segments and the existing estimator, aggregating them only for display.
    """
    if not rows:
        return [], []
    account = report.get('account') or next(
        (c.get('account') for c in report.get('cycles', []) if c.get('account')), '')
    groups = []
    previous = None
    for row in rows:
        if not groups or row['reset_kind']:
            groups.append({'start': row['at'], 'reset_kind': row['reset_kind'],
                           'partial': not bool(row['reset_kind']), 'rows': []})
        group = groups[-1]
        group['rows'].append(row)
        previous = row

    result, periods = [], []
    cycles = report.get('cycles', [])
    forward = report.get('tracking') is not None
    for index, group in enumerate(groups):
        start = group['start']
        end = (math.nextafter(groups[index+1]['start'], -math.inf)
               if index+1 < len(groups) else rows[-1]['at'])
        own = [c for c in cycles if c['end'] >= start and c['start'] <= end]
        summary = quota_statistics({**report, 'account': account, 'cycles': own}, start, end)
        intervals = [c for c in summary['intervals']
                     if not account or not c.get('account') or c['account'] == account]
        ranges=sorted((c['start'],c['end']) for c in intervals)
        range_position=0
        running_starts=[];running_ends=[]
        for c in intervals:
            if not c.get('local_attributed'):continue
            for r in c.get('cost_rows',[]):
                if r.get('request_start') is not None and r['request_start']<r['ts']:
                    running_starts.append(r['request_start']);running_ends.append(r['ts'])
        running_starts.sort();running_ends.sort()
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
                cost = (subtotal if priced else 0 if not position and
                        interval.get('cost_complete') and not pending else None)
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
            while range_position<len(ranges) and ranges[range_position][1]<row['at']:range_position+=1
            local_observed=(report.get('tracking') is None or
                            range_position<len(ranges) and ranges[range_position][0]<=row['at']<=ranges[range_position][1])
            while position < len(events) and events[position][0] <= row['at']:
                _, amount, consumed, confirmed = events[position]
                cost += amount
                delta += consumed
                known += confirmed
                position += 1
            matches = not account or row.get('account') == account
            value = max(0, cost) if known and matches else None
            if row['reset_kind'] and matches:
                value = 0.0
            ratio = value/delta*100 if value is not None and delta > 0 else None
            value_pending=bisect_right(running_starts,row['at'])>bisect_right(running_ends,row['at'])
            value_held = (value_pending or ratio is None) and last_ratio is not None and matches
            if value_pending or ratio is None:
                ratio = last_ratio if matches else None
            else:
                last_ratio = ratio
                confirmed_cost, confirmed_delta = value, delta
            result.append({**row, 'cycle_start': start, 'cycle_cost': value,
                           'cycle_value': ratio, 'cycle_delta':delta,
                           'confirmed_cost':confirmed_cost, 'confirmed_delta':confirmed_delta, 'value_held':value_held,
                           'local_observed':local_observed,'local_range':range_position,'value_pending':value_pending,
                           'cycle_partial': group['partial']})
        if any(not account or r.get('account') == account for r in group['rows']):
            value = summary['per_percent']
            periods.append({'start': start, 'end': group['rows'][-1]['at'],
                            'reset_kind': group['reset_kind'], 'partial': group['partial'],
                            'current': index == len(groups)-1,
                            'cost': summary['cost'] if summary['total'] else
                                    0 if group['reset_kind'] else None,
                            'delta': summary['delta'],
                            'priced_calls': summary['observed_priced_calls'],
                            'calls': summary['observed_calls'],
                            'value': value*100 if value is not None else None})
            periods[-1]['cost_intervals']=intervals
    return result, periods


class QuotaLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=8)
        try:
            self._initialize()
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
            CREATE TABLE IF NOT EXISTS coverage_issues(
                home TEXT, sid TEXT, start REAL, end REAL, reason TEXT,
                PRIMARY KEY(home,sid,reason));
        ''')
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
        self.db.execute('create table if not exists cost_archive(home text,uid text,price_id text,signature text,cost real,service_tier text,at real,primary key(home,uid,price_id,signature))')
        self.db.execute('create table if not exists mode_scan(home text primary key, latest integer, before_id integer)')
        has_mode_events=bool(self.db.execute("select 1 from sqlite_master where type='table' and name='mode_events'").fetchone())
        self.db.execute('''create table if not exists mode_events(
            home text, id integer, sid text, turn text, data text,
            primary key(home,id,sid,turn))''')
        if not has_mode_events:
            self.db.execute('update mode_scan set latest=-1,before_id=0')
        if has_mode_events:
            for home,latest,before in self.db.execute('select * from mode_scan'):
                if latest<0:continue
                self.mode_offsets[home]=latest
                self.mode_backfill[home]=before
        for old in self.db.execute('select * from calls where price_id!=?',(PRICE_ID,)).fetchall():
            self.db.execute('insert or ignore into cost_archive values(?,?,?,?,?,?,?)',
                (old['home'],old['uid'],old['price_id'],old['signature'],old['cost'],old['service_tier'],time.time()))
            self.db.execute('update calls set cost=?,price_id=? where home=? and uid=?',
                (token_cost(dict(old))['cost'],PRICE_ID,old['home'],old['uid']))
        self.db.commit()
        self.modes = {(r['home'],r['sid'],r['turn']):r['mode'] for r in self.db.execute('select * from modes')}
        self.mode_revisions = {}
        self.mode_evidence = {}
        for home in {r[0] for r in self.db.execute('select distinct home from mode_events')}:
            self.rebuild_mode_evidence(home)
        self.db.commit()

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

    def enrich_modes(self, snapshot):
        from contextlib import closing
        from .request_modes import request_mode_observation
        for home in snapshot.get('homes',[]):
            path=Path(home)/'logs_2.sqlite'
            if not path.exists():continue
            try:
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.3)) as source:
                    maximum=source.execute('select max(id) from logs').fetchone()[0] or 0
                    if maximum<self.mode_offsets.get(home,0):
                        self.mode_offsets.pop(home,None);self.mode_backfill.pop(home,None)
                    if home not in self.mode_offsets:
                        rows=source.execute("select id,thread_id,feedback_log_body from logs where target='codex_core::session::handlers' order by id desc limit 500").fetchall()[::-1]
                        self.mode_backfill[home]=rows[0][0] if rows else 0
                    else:
                        rows=source.execute("select id,thread_id,feedback_log_body from logs where id>? and target='codex_core::session::handlers' order by id limit 500",(self.mode_offsets[home],)).fetchall()
                    latest=max([r[0] for r in rows]+[self.mode_offsets.get(home,0)])
                    before=self.mode_backfill.get(home,0)
                    if before:
                        older=source.execute("select id,thread_id,feedback_log_body from logs where id<? and target='codex_core::session::handlers' order by id desc limit 500",(before,)).fetchall()
                        self.mode_backfill[home]=older[-1][0] if older else 0
                        rows=older[::-1]+rows
                changed=False
                for identifier,sid,body in rows:
                    parsed=request_mode_observation(body)
                    if parsed and sid:
                        data=json.dumps(parsed,sort_keys=True)
                        old=self.db.execute('select data from mode_events where home=? and id=? and sid=? and turn=?',
                                            (home,identifier,sid,parsed['turn'])).fetchone()
                        if old is None or old[0]!=data:
                            self.db.execute('insert or replace into mode_events values(?,?,?,?,?)',
                                            (home,identifier,sid,parsed['turn'],data));changed=True
                if changed:self.rebuild_mode_evidence(home)
                self.mode_offsets[home]=latest
                self.db.execute('insert or replace into mode_scan values(?,?,?)',(home,latest,self.mode_backfill.get(home,0)))
            except sqlite3.Error:pass
        self.db.commit()
        for session in snapshot['sessions']:
            for row in session['history']:
                recorded=row.setdefault('_record_service_tier',request_tier(row))
                evidence=self.mode_evidence.get((session['home'],session['id'],row.get('turn')))
                explicit=self.modes.get((session['home'],session['id'],row.get('turn')))
                if row.get('service_tier_source')=='wire':
                    row['service_tier']='미확인' if row.get('mode_conflict') else request_tier({'service_tier':row.get('requested_service_tier')})
                elif row.get('mode_conflict') and row.get('request_mode_source')!='settings_conflict':
                    row['service_tier']='미확인'
                elif evidence and evidence['request_mode_source']!='unobserved':
                    row['service_tier']=evidence['mode'];row['service_tier_source']='settings'
                    row.update({k:v for k,v in evidence.items() if k!='mode'})
                elif row.get('service_tier_source')=='settings':row['service_tier']=explicit or recorded
                else:row['service_tier']=recorded if recorded!='미확인' else explicit or '미확인'
            session['usage_revision']=(session.get('usage_revision'),self.mode_revisions.get((session['home'],session['id']),0))

    def observe(self, home, quota, commit=True):
        for name, observed_window in (quota or {}).get('windows', {}).items():
            if name not in ('weekly','five_hour') or not quota.get('observed_at'):
                continue
            key=hashlib.sha256(json.dumps([home,quota['observed_at'],quota.get('source'),
                quota.get('account'),quota.get('plan_type'),quota.get('bucket'),name,observed_window],sort_keys=True).encode()).hexdigest()
            self.db.execute('insert or ignore into window_observations values(?,?,?,?,?,?,?,?,?,?,?,?)',
                (key,home,quota['observed_at'],quota.get('source','local'),quota.get('account',''),
                 quota.get('plan_type') or '',quota.get('bucket') or ('codex' if quota.get('source')=='live' else ''),
                 name,observed_window.get('used_percent'),observed_window.get('resets_at'),
                 observed_window.get('window_minutes'),json.dumps(quota.get('separate_models',[]))))
        window = (quota or {}).get('windows', {}).get('weekly')
        at = (quota or {}).get('observed_at')
        if not window or not at:
            if commit:self.db.commit()
            return
        source = quota.get('source', 'local')
        key = hashlib.sha256(json.dumps([home, at, source, quota.get('account'),quota.get('plan_type'),
            quota.get('bucket'),quota.get('separate_models',[]),window], sort_keys=True).encode()).hexdigest()
        self.db.execute('insert or ignore into observations values(?,?,?,?,?,?,?,?,?)',
            (key, home, at, source, quota.get('account', ''), window['used_percent'],
             window.get('resets_at'), window['window_minutes'], json.dumps(quota.get('separate_models', []))))
        self.db.execute('''insert into observation_context values(?,?,?) on conflict(id) do update set
                        plan=case when observation_context.plan='' then excluded.plan else observation_context.plan end,
                        bucket=case when observation_context.bucket='' then excluded.bucket else observation_context.bucket end''',
                        (key, quota.get('plan_type') or '', quota.get('bucket') or
                         ('codex' if source == 'live' else '')))
        if commit: self.db.commit()

    def sync(self, engine, snapshot):
        """Use the existing deduplicated analytical rows; don't parse conversations twice."""
        from .quota_tracking_store import sync_activity
        if not hasattr(self,'_tracking_sync_cache'):self._tracking_sync_cache={}
        sync_activity(self.db, snapshot,self._tracking_sync_cache)
        for key, session in engine.sessions.items():
            revision = session['revision']
            if self.revisions.get(key) == revision:
                continue
            home, sid = key
            current_uids = set()
            stored={r['uid']:r for r in self.db.execute('select uid,signature,service_tier,sid,cost,price_id from calls where home=? and sid=?',(home,sid))}
            previous_usage = {}
            turns = session['prepared'].get('turn_records', {})
            for row in sorted(session['prepared']['history'], key=lambda value: value['ts']):
                response = str(row['key'])
                uid = sid + ':' + response if response.startswith(('legacy:', 'record:')) else response
                current_uids.add(uid)
                turn = row.get('turn')
                requested = row.get('request_observed_at') or row.get('request_ts')
                if requested is None and turn:
                    # A previous completed usage record is a conservative lower
                    # bound for the next call in this same turn.
                    requested = previous_usage.get(turn, turns.get(turn, {}).get('started_at'))
                if turn:
                    previous_usage[turn] = row['ts']
                self.db.execute('insert or replace into tracking_call_boundaries values(?,?,?)',
                                (home, uid, requested))
                signature = json.dumps([row.get(k) for k in ('ts', 'model', 'service_tier', *TOKEN_FIELDS)], separators=(',', ':'))
                old = stored.get(uid)
                if old is None:old=self.db.execute('select signature,service_tier,sid,cost,price_id from calls where home=? and uid=?',(home,uid)).fetchone()
                if old and old['signature'] == signature and old['price_id']==PRICE_ID:
                    if old['sid']!=sid:
                        self.db.execute('update calls set sid=? where home=? and uid=?',(sid,home,uid))
                    continue
                if old:
                    self.db.execute('insert or ignore into cost_archive values(?,?,?,?,?,?,?)',
                        (home,uid,old['price_id'],old['signature'],old['cost'],old['service_tier'],time.time()))
                cost = token_cost(row)['cost']
                self.db.execute('insert or replace into calls values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (home, uid, sid, row['ts'], row.get('model') or '미확인',
                     *[row.get(k) for k in TOKEN_FIELDS], cost, PRICE_ID, signature,row.get('service_tier','미확인')))
            self.revisions[key] = revision
            old_uids = set(stored)
            self.db.executemany('delete from calls where home=? and sid=? and uid=?',
                                [(home,sid,uid) for uid in old_uids-current_uids])
        for home in snapshot.get('homes', []):
            usage_errors=snapshot.get('usage_errors')
            if usage_errors is None:
                usage_errors=[error for error in snapshot.get('errors',[])
                              if '기록 읽기' in error or '세션 목록' in error or '일부 기록 형식' in error]
            self.db.execute('delete from coverage_issues where home=?', (home,))
            for session in snapshot['sessions']:
                if session['home'] != home:
                    continue
                history = session.get('history', [])
                times = [r['ts'] for r in history if r.get('ts') is not None]
                if session.get('unclassified') and not session.get('coverage_available'):
                    self.db.execute('insert or replace into coverage_issues values(?,?,?,?,?)',
                                    (home,session.get('id',''),min(times) if times else None,
                                     max(times) if times else None,'참고: 누계 차액의 발생 경계 미확인'))
                for gap in session.get('coverage_gaps', []):
                    self.db.execute('insert or replace into coverage_issues values(?,?,?,?,?)',
                                    (home, session.get('id',''),gap['start'],gap['end'],
                                     f"누계 차액 증가 {gap['tokens']}토큰 · {gap['start']}"))
            if usage_errors:
                self.db.execute('insert or replace into coverage_issues values(?,?,?,?,?)',
                                (home, '', None, None, '로컬 호출 기록 읽기 실패: '+' · '.join(usage_errors)))
            unclassified = sum(bool(s.get('unclassified')) for s in snapshot['sessions'] if s['home'] == home)
            index=snapshot.get('index',{})
            loading=bool(index.get('loading'))
            complete=index.get('usage_complete',snapshot.get('usage_collection_complete',not loading and not usage_errors))
            previous=self.db.execute('select complete_at from state where home=?',(home,)).fetchone()
            watermark=previous['complete_at'] if previous else None
            if complete:
                stamp=index.get('usage_complete_at') or index.get('last_usage_success') or snapshot.get('last_usage_collection_success') or snapshot['ts']
                watermark=max(watermark or 0,stamp)
            self.db.execute('insert or replace into state(home,at,loading,unclassified,complete_at) values(?,?,?,?,?)',
                (home,snapshot['ts'],loading,unclassified,watermark))
        self.db.commit()

    def import_observations(self, index_path, home):
        """Recover already-sanitized local observations, without modifying the usage index."""
        path = Path(index_path)
        if not path.exists():
            return
        from contextlib import closing
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            rows = db.execute('select ts,data from events where home=? and data like ?', (home, '%"rate_limits"%'))
            for at, data in rows:
                quota = json.loads(data).get('payload', {}).get('rate_limits')
                if quota:
                    self.observe(home, {**quota, 'observed_at': at, 'source': 'history'},commit=False)
        self.db.commit()

    def report(self, home, now=None):
        own_transaction = not self.db.in_transaction
        if own_transaction:self.db.execute('BEGIN')
        try:
            return self._report(home,now)
        finally:
            if own_transaction:self.db.rollback()

    def _report(self, home, now=None):
        now = time.time() if now is None else now
        tables = {r[0] for r in self.db.execute("select name from sqlite_master where type='table'")}
        config=self.db.execute('select started from tracking_config where home=?',(home,)).fetchone() if 'tracking_config' in tables else None
        cutoff=config[0] if config else float('-inf')
        contexts = ({r['id']:dict(r) for r in self.db.execute('select c.* from observation_context c join observations o on c.id=o.id where o.home=? and o.at>=?',(home,cutoff))}
                    if 'observation_context' in tables else {})
        raw = [{**dict(row), **{k:contexts.get(row['id'],{}).get(k,'') for k in ('plan','bucket')}}
               for row in self.db.execute('select * from observations where home=? and at>=? and at<=? order by at,id', (home,cutoff,now))]
        observations = [r for r in raw if type(r['used']) in (int,float)
                        and math.isfinite(r['used']) and 0 <= r['used'] <= 100
                        and r['minutes']==10080]
        manual_resets = [dict(r) for r in self.db.execute(
            'select * from manual_resets where home=? and at<=? order by at,id', (home,now))]
        groups = split_cycles(observations, manual_resets)
        from .quota_tracking_store import build
        tracking = build(self.db, home, now)
        scope = tracking['homes'] if tracking is not None else [home]
        slots = ','.join('?' for _ in scope)
        if tracking is not None:
            groups = tracking['groups']
        elif groups:groups[-1]['provisional']=True
        state = self.db.execute('select * from state where home=?', (home,)).fetchone()
        completed_at=(state['complete_at'] if 'complete_at' in state.keys() else state['at'] if not state['loading'] else None) if state else None
        if tracking is not None:
            marks=list(self.db.execute(f'select complete_at from state where home in ({slots})',scope))
            completed_at=min((r[0] for r in marks),default=None) if len(marks)==len(scope) and all(r[0] is not None for r in marks) else None
        issues = ([dict(r) for r in self.db.execute(f'select * from coverage_issues where home in ({slots})',scope)]
                  if 'coverage_issues' in tables else [])
        calls = list(self.db.execute(f'''select * from (
            select *,row_number() over(partition by uid order by cost is null,home) as copy_rank
            from calls where home in ({slots}) and ts>=?) where copy_rank=1 order by ts''',(*scope,cutoff)))
        if tracking is not None:
            completed={r['response']:r['ended'] for r in self.db.execute(
                f"select response,min(end) as ended from tracking_wire where home in ({slots}) "
                "and status='completed' and end is not null and response<>'' group by response",scope)}
            # A usage record may arrive after the final quota lookup. Exact
            # response identity assigns its cost to the observed completion,
            # without rewriting the underlying usage timestamp.
            boundaries={(r['home'],r['uid']):r['started'] for r in self.db.execute(
                f'select * from tracking_call_boundaries where home in ({slots})',scope)}
            exact_starts={}
            for r in self.db.execute(f"select * from tracking_wire where home in ({slots}) and response<>''",scope):
                boundaries[(r['home'],r['response'])]=r['start']
                exact_starts[(r['home'],r['response'])]=r['start']
            calls=sorted(({**dict(r),'ts':completed.get(r['uid'],r['ts']),
                           'request_start':boundaries.get((r['home'],r['uid'])),
                           'local_request_start':exact_starts.get((r['home'],r['uid']))} for r in calls),key=lambda r:r['ts'])
        call_times=[r['ts'] for r in calls]
        time_cache={}
        for group in groups:
            if group.get('gap') or group.get('discontinuous'):continue
            separate=set().union(*(set(json.loads(r['separate'])) for r in group['observations']))
            key=frozenset(separate)
            if key not in time_cache:
                time_cache[key]=[r['ts'] for r in calls if ALIASES.get(r['model'],r['model']) not in separate]
            times=time_cache[key]
            if group['observations']:
                start,end=group['observations'][0]['at'],group['observations'][-1]['at']
                for row in calls[bisect_right(call_times,start):bisect_right(call_times,end)]:
                    if row['cost'] is None and start<row['ts']<=end and ALIASES.get(row['model'],row['model']) not in separate:
                        issues.append({'start':row['ts'],'end':row['ts'],
                            'reason':'요청 모드 미확인' if row['service_tier']=='미확인' and token_cost({**dict(row),'service_tier':'Standard'})['cost'] is not None else '필수 토큰·단가 누락'})
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
        def window_calls(start, end):
            rows = calls[bisect_right(call_times,start):bisect_right(call_times,end)]
            if tracking is not None:
                rows = [r for r in rows if r.get('request_start') is None or r['request_start'] >= start]
            return rows

        def model_rows(start, end, separate):
            if tracking is not None:
                grouped={}
                for row in window_calls(start, end):
                    grouped.setdefault((row['model'],row['service_tier']),[]).append(row)
                values=[]
                for (model,tier), own in grouped.items():
                    priced=[r['cost'] for r in own if r['cost'] is not None]
                    values.append(dict(model=model,service_tier=tier,displayed_tier=tier,
                        calls=len(own),priced=len(priced),cost=sum(priced) if priced else None,
                        mean_cost=sum(priced)/len(priced) if priced else None,
                        unknown_mode_calls=len(own) if tier=='미확인' else 0,
                        missing=len(own)-len(priced),prices=','.join(sorted({r['price_id'] for r in own})),
                        separate=ALIASES.get(model,model) in separate,
                        **{k:sum(r[k] for r in own) if all(r[k] is not None for r in own) else None for k in TOKEN_FIELDS}))
                return sorted(values,key=lambda r:r['cost'] or 0,reverse=True)
            fields = ','.join(f'case when count({k})=count(*) then sum({k}) else null end as {k}' for k in TOKEN_FIELDS)
            rows = self.db.execute(f'''select model,
                service_tier as displayed_tier,
                count(*) as calls,count(cost) as priced,avg(cost) as mean_cost,{fields},sum(cost) as cost,
                sum(service_tier='미확인') as unknown_mode_calls,
                sum(cost is null) as missing,group_concat(distinct price_id) as prices
                from calls where home=? and ts>? and ts<=? group by model,displayed_tier order by sum(cost) desc''', (home,start,end))
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
            # Never mix legacy/unknown account observations with live-account calibration.
            live = [r for r in obs if r['source'] == 'live']
            matched = obs
            pending = False
            if tracking is not None:
                pending = False
            elif completed_at is not None:
                collected = [r for r in matched if r['at'] <= completed_at]
                if len(collected) >= 2:
                    matched = collected
                else:
                    pending = True
            else:pending=True
            first, last = matched[0], matched[-1]
            start, end = first['at'], last['at']
            separate = set().union(*(set(json.loads(r['separate'])) for r in matched))
            models = model_rows(start,end,separate)
            uncertain_gap = bool(next_group and next_group['reason']!='예정 리셋 확인'
                                 and not next_group.get('manual_reset'))
            if uncertain_gap: cycle_end = obs[-1]['at']
            if group.get('gap'):
                cycle_end = obs[-1]['at']
                uncertain_gap = True
            cycle_models = model_rows(group['boundary'],cycle_end,separate)
            eligible = [r for r in models if not r['separate']]
            pending_usage_times = [r['end'] for r in (tracking or {}).get('pending_responses',[])
                if r['status']=='completed' and r['end'] is not None and start<r['end']<=end and r['start']>=start]
            priced_count = sum(r['priced'] for r in eligible)
            cost_complete = completed_at is not None and completed_at >= end
            cost = sum(r['cost'] or 0 for r in eligible)
            if tracking is not None and not priced_count and (eligible or pending_usage_times or not cost_complete):
                cost = None
            missing = sum(r['missing'] for r in eligible)
            if tracking is None and (not eligible or missing): cost = None
            blocked = []
            if tracking is not None and any(gap['start']<row['ts']<=gap['end'] and row['home']==gap['home']
                    for gap in tracking.get('ownership_gaps',[]) for row in calls[bisect_right(call_times,start):bisect_right(call_times,end)]):
                blocked.append('로컬 홈 계정 확인 경계: 비용 귀속 미확인')
            if group.get('boundary_pending'):
                blocked.append('요청 진행 중 경계: 비용 분리 불가')
            if tracking is not None and not tracking['complete'] and group.get('provisional'):
                blocked.append('로컬 작업·비용 집계 대기')
            if tracking is not None and any(r['start'] < end and (r['end'] is None or r['end'] > start)
                    for r in tracking.get('pending_responses', [])):
                blocked.append('완료 요청의 비용 기록 수집 대기')
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
                blocked.append('요금제·한도 정보 미확인')
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
                    key=(home,row['uid'],row['signature'],row['price_id'])
                    if key not in self._mode_alternatives:
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
                assumptions.append(f"상충·역행 관측 {len(group['rejected'])}건을 끝점에서 제외 · 순소모 사용")
            for issue in issues:
                if issue['reason'].startswith(('누계 차액 증가','로컬 호출 없는 한도 증가','필수 토큰·단가 누락','요청 모드 미확인')):
                    continue
                elif not issue['reason'].startswith('진행 중'):
                    if (issue['start'] is None or issue['start'] <= end) and (issue['end'] is None or issue['end'] > start):
                        assumptions.append('누계·수집 근거 일부 미확인: 기간 전체 누락으로 단정하지 않음')
            if not issues and state and state['unclassified']:
                assumptions.append('미분류 차액 있음: 구간별 증가 여부 확인 필요')
            if live and i == len(groups)-1 and (tracking is None or group.get('provisional')):
                assumptions.append('진행 중 관측 구간: 서버 반영 지연에 따라 갱신됨')
            if completed_at is None or completed_at < end:
                blocked.append('해당 구간의 토큰 수집 완료 대기')
            if missing > mode_only_missing:
                blocked.append(f'단가/토큰 미확인 {missing-mode_only_missing}개 호출')
            if tracking is None and not sum(r['calls'] for r in eligible):
                blocked.append('대응하는 모델 호출 없음')
            boundary_excluded = 0
            if tracking is not None:
                boundary_excluded = sum(r.get('request_start') is not None and r['request_start'] < start
                    for r in calls[bisect_right(call_times,start):bisect_right(call_times,end)]
                    if ALIASES.get(r['model'],r['model']) not in separate)
                blocked = []
                assumptions = []
                if missing:
                    assumptions.append(f'단가 또는 토큰 자료 없는 {missing}호출은 비용 합계에서 제외')
                if boundary_excluded:
                    assumptions.append(f'관측 시작 전에 진행된 {boundary_excluded}호출은 비용 합계에서 제외')
                # Collection errors affect cost coverage, never observed account
                # consumption. Show only evidenced errors, not polling races.
                assumptions.extend(r['reason'] for r in issues if r['reason'].startswith('로컬 호출 기록 읽기 실패:'))
            # The denominator is actual account consumption in the same ON window.
            delta = last['used'] - first['used']
            result.append({'id': group['id'], 'reason': group['reason'], 'start': start, 'end': end,
                'reset': last['reset'], 'used_start': first['used'], 'used_end': last['used'],
                'delta': delta, 'observations': len(matched), 'models': models, 'cost': cost,
                'endpoints':[(r['at'],r['used']) for r in matched],
                'forward_tracking':tracking is not None,
                'finished_at':group.get('finished_at'),
                'boundary_uncertain':group.get('boundary_uncertain',False),
                'boundary_excluded_calls':boundary_excluded,
                'priced_calls':priced_count,
                'cost_complete':cost_complete,
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
        current_account = (tracking.get('account') if tracking is not None else None) or next((r['account'] for r in reversed(observations) if r['account']), '')
        relevant_resets = [r for r in manual_resets if not current_account or not r['account'] or r['account']==current_account]
        last_reset = relevant_resets[-1] if relevant_resets else None
        pending = any(r.get('reset_pending') and r['manual_reset_id']==last_reset['id'] for r in result) if last_reset else False
        if tracking is not None:pending=False
        history = ([dict(r) for r in self.db.execute('select * from window_observations where home=? and at>=? and at<=? order by at,id',(home,cutoff,now))]
                   if 'window_observations' in tables else [])
        # Existing ledgers retain their weekly observations without rewriting raw evidence.
        known={(r['at'],r['source'],r['account'],r['used'],r['reset']) for r in history if r['window']=='weekly'}
        history += [{**r,'window':'weekly'} for r in raw
                    if (r['at'],r['source'],r['account'],r['used'],r['reset']) not in known]
        request_windows=[]
        if tracking is not None:
            by_response={}
            for row in self.db.execute(f"select * from tracking_wire where home in ({slots}) and response<>''",scope):
                if row['status'] not in ('created','completed'):continue
                old=by_response.get(row['response'])
                if old is None or (row['end'] is not None and old['end'] is None):
                    by_response[row['response']]=dict(uid=row['response'],start=row['start'],end=row['end'])
            request_windows=list(by_response.values())
        return {**({'request_windows':request_windows} if tracking is not None else {}),
                'home':home,'account':current_account,'history':sorted(history,key=lambda r:(r['at'],r['id'])),
                'tracking':({k:v for k,v in tracking.items() if k!='groups'} if tracking is not None else None),
                'manual_resets':manual_resets,'cycles': list(reversed(result)), 'price_id': PRICE_ID, 'index_at': state['at'] if state else None,
                'index_loading': bool(state['loading']) if state else True,
                'index_complete_at':completed_at, 'at': now,
                'last_manual_reset':last_reset, 'manual_reset_count':len(relevant_resets),
                'manual_reset_pending':pending}

    def close(self):
        self.db.close()
