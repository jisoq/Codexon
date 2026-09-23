"""Immutable chart preparation, run by the quota reader outside the UI thread."""
from datetime import datetime
import math
from bisect import bisect_left
from .quota_tracking import ResetTracker, RESET_NAMES, OBSERVATION_FRESHNESS
from .quota_cycles import quota_statistics, quota_value_history

def clock(ts, full=False):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S' if full else '%m/%d %H:%M') if ts else '—'


def history_rows(report, mode, start=None, end=None):
    """Show remaining allowance and only actual full-recovery events."""
    rows=[]
    previous=None
    detector=ResetTracker()
    controls=iter(sorted((report.get('tracking') or {}).get('controls',[]),key=lambda r:r['at']))
    control=next(controls,None)
    enabled=True
    by_time={}
    account=report.get('account')
    identified=account and any(r.get('account')==account and r['window']==mode for r in report.get('history',[]))
    for record in report.get('history',[]):
        if identified and record.get('account')!=account:continue
        if record['window']!=mode:
            continue
        # Source metadata must not create duplicate points or event noise.
        rank={'live':2,'local':1,'history':0}
        other=by_time.get(record['at'])
        if other is None or rank.get(record.get('source'),0)>=rank.get(other.get('source'),0):
            by_time[record['at']]=record
    for record in sorted(by_time.values(),key=lambda r:r['at']):
        interrupted=False
        while control is not None and control['at']<=record['at']:
            interrupted=True
            enabled=bool(control['enabled'])
            control=next(controls,None)
        if type(record.get('used')) not in (int,float) or not math.isfinite(record['used']) or not 0<=record['used']<=100:
            previous=None
            continue
        if end is not None and record['at']>end:break
        row=dict(record)
        same_tracking=bool(previous and enabled and not interrupted and
                           record['at']>previous['at'])
        if previous and previous.get('account') and record.get('account') and previous['account']!=record['account']:
            same_tracking=False
        continuous=bool(same_tracking and record['at']-previous['at']<=OBSERVATION_FRESHNESS)
        row['remaining']=100-record['used']
        row['reset_kind']=(detector.observe(row['remaining'],record['at'],record.get('reset'),same_tracking)
                           if mode=='weekly' else None)
        row['markers']=[RESET_NAMES[row['reset_kind']]] if row['reset_kind'] else []
        row['connect']=bool(continuous and rows)
        row['label']=f"{clock(row['at'],True)} · 잔여 {row['remaining']:g}%"+(' · '+row['markers'][0] if row['markers'] else '')
        if start is None or record['at']>=start:rows.append(row)
        previous=row
    return rows


def observation_label(row, money=False):
    from .pricing import usd
    label=row['label']
    if 'reported_remaining' in row:
        label=f"{clock(row['at'],True)} · 누적 소모 {row['remaining']:g}%p"
    if money:
        label+=(f" · 누적 API {usd(row.get('cycle_cost'))}"
                f" · 주간 동등 가치 {usd(row.get('cycle_value'))}")
        if row.get('value_held'):label+=' · 마지막 확인값'
    return label


def prepare_series(rows):
    """Bound drawing size while retaining extrema and exact selectable records."""
    times=[];breaks=[];gaps=0;maximum=0;low=100;high=0;cost_maximum=0;value_maximum=0
    missing={key:[] for key in ('cycle_cost','cycle_value')}
    counts={key:0 for key in missing}
    active_times=[];gap_indices=[];gap_seconds=[0.0];active=0.0
    for index,row in enumerate(rows):
        times.append(row['at'])
        if index:
            elapsed=max(0,row['at']-rows[index-1]['at'])
            if row['connect']:active+=elapsed
            else:
                gap_indices.append(index);gap_seconds.append(gap_seconds[-1]+elapsed)
        active_times.append(active)
        gaps+=not row['connect'];breaks.append(gaps)
        low=min(low,row['remaining']);high=max(high,row['remaining'])
        maximum=max(maximum,row.get('cycle_cost') or 0,row.get('cycle_value') or 0)
        cost_maximum=max(cost_maximum,row.get('cycle_cost') or 0)
        value_maximum=max(value_maximum,row.get('cycle_value') or 0)
        for key in missing:
            counts[key]+=row.get(key) is None
            missing[key].append(counts[key])
    samples={}
    for budget in (256,768):
        if len(rows)<=budget*2:
            samples[budget]=list(range(len(rows)));continue
        selected=set();span=max(1,active);buckets={}
        for i,row in enumerate(rows):
            bucket=min(budget-1,int(active_times[i]/span*budget))
            state=buckets.setdefault(bucket,[i,i,{}]);state[1]=i
            for key in ('remaining','cycle_cost','cycle_value'):
                value=row.get(key)
                if value is None:continue
                pair=state[2].setdefault(key,[i,i])
                if value<rows[pair[0]][key]:pair[0]=i
                if value>rows[pair[1]][key]:pair[1]=i
            if row['reset_kind']:selected.add(i)
        # Preserve both ends of the compressed breaks. Very dense breaks are
        # already represented by the bounded time buckets and the break mask.
        if len(gap_indices)<=budget:
            for i in gap_indices:selected.update((i-1,i))
        for first,last,extrema in buckets.values():
            selected.update((first,last))
            for pair in extrema.values():selected.update(pair)
        samples[budget]=sorted(selected)
    return dict(rows=rows,times=times,breaks=breaks,missing=missing,samples=samples,maximum=maximum,low=low,high=high,
                active_times=active_times,gap_indices=gap_indices,gap_seconds=gap_seconds,
                cost_maximum=cost_maximum,value_maximum=value_maximum)


def prepare_quota_view(report):
    from .pricing import usd
    rows,periods=quota_value_history(report,history_rows(report,'weekly'))
    times=[r['at'] for r in rows]
    overall=[];offset_cost=offset_delta=0;resets=[]
    for i,period in enumerate(periods):
        lo=bisect_left(times,period['start'])
        hi=bisect_left(times,periods[i+1]['start']) if i+1<len(periods) else len(rows)
        own=[dict(row) for row in rows[lo:hi] if row['local_observed']]
        for index,row in enumerate(own):
            if not index or row['local_range']!=own[index-1]['local_range']:row['connect']=False
            cost=offset_cost+(row['cycle_cost'] or 0);delta=offset_delta+row['cycle_delta']
            overall.append({**row,'reported_remaining':row['remaining'],'remaining':delta,
                            'cycle_cost':cost,'cycle_delta':delta,
                            'cycle_value':(offset_cost+row['confirmed_cost'])/(offset_delta+row['confirmed_delta'])*100
                            if offset_delta+row['confirmed_delta']>0 else None})
        period['series']=prepare_series(own)
        period['series']['active_only']=True
        if period['reset_kind']:
            resets.append(dict(at=period['start'],label=RESET_NAMES[period['reset_kind']],number=i+1))
        offset_cost+=period['cost'] or 0;offset_delta+=period['delta']
        state='진행 중' if period['current'] else '종료'
        origin=RESET_NAMES.get(period['reset_kind'],'관측 시작')
        period['label']=(f"{clock(period['start'])} → {clock(period['end'])} · {origin} · {state} · {usd(period['value'])}")
    lifetime=quota_statistics(report,include_mode_assumptions=True)
    now=report.get('at')
    summaries={None:lifetime}
    if now is not None:
        for days in (7,30,90):
            summaries[days]=quota_statistics(report,now-days*86400,include_mode_assumptions=True)
    all_series=prepare_series(overall)
    all_series.update(active_only=True,cumulative=True,resets=resets)
    return dict(periods=periods,overall=all_series,lifetime=lifetime,summaries=summaries,
                five_hour=prepare_series(history_rows(report,'five_hour')))


__all__ = ('clock','history_rows','observation_label','prepare_series','prepare_quota_view')
