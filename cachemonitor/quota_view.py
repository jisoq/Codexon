"""Immutable chart preparation, run by the quota reader outside the UI thread."""
from datetime import datetime
from array import array
import json
import math
import pickle
import hashlib
import sys
from bisect import bisect_left, bisect_right
from heapq import merge
from itertools import groupby
from .quota_tracking import ResetTracker, RESET_NAMES, OBSERVATION_FRESHNESS
from .quota_cycles import _PackedHistory, _PackedRows, quota_statistics, quota_value_history
from .native_backend import get_native

def clock(ts, full=False):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S' if full else '%m/%d %H:%M') if ts else '—'


def history_rows(report, mode, start=None, end=None):
    return list(_history_rows(report,mode,start,end))


def _history_rows(report, mode, start=None, end=None, *, labels=True, _resume=None, _checkpoint=None, _classification=None):
    """Show remaining allowance and only actual full-recovery events."""
    count=0;removed=False
    previous=None
    detector=ResetTracker()
    controls=sorted((report.get('tracking') or {}).get('controls',[]),key=lambda r:r['at'])
    control_position=0
    enabled=True
    account=report.get('account')
    identified=account and any(r.get('account')==account and r['window']==mode for r in report.get('history',[]))
    history=report.get('history',[])
    ordered=isinstance(history,_PackedHistory)
    resume_at=None
    if _resume is not None:
        resume_at,(count,removed,previous,state,control_position,enabled)=_resume
        detector=ResetTracker(*state)
        history=history.iter_from(resume_at)
    edits=[c for c in report.get('cycle_clears',[]) if c['account']==account] if mode=='weekly' else []
    deletes=sorted((c for c in edits if c['baseline']=='null'),key=lambda c:c['start'])
    delete_starts=[c['start'] for c in deletes]
    clears=[c for c in edits if c['baseline']!='null']
    if clears:
        clears=sorted((c for c in clears if c['account']==account),key=lambda c:c['start'])
        starts=[c['start'] for c in clears]
        def retained(record):
            index=bisect_right(starts,record['at'])-1
            return index<0 or record.get('account')!=account or record['at']>clears[index]['end']
        baselines=[{**json.loads(c['baseline']),'at':c['end'],'id':'clear:'+str(c['end']),
                    'measurement_reset':True} for c in clears if resume_at is None or c['end']>=resume_at]
        history=(merge(filter(retained,history),sorted(baselines,key=lambda r:r['at']),key=lambda r:r['at'])
                 if ordered else [r for r in history if retained(r)]+baselines)
    candidates=(r for r in history if r['window']==mode and (not identified or r.get('account')==account))
    rank={'live':2,'local':1,'history':0}
    def preferred():
        if ordered:
            # Packed worker history already has timestamp order. Hold only
            # the preferred source at one timestamp, not another full copy.
            for _,records in groupby(candidates,key=lambda r:r['at']):
                best=None
                for record in records:
                    if best is None or rank.get(record.get('source'),0)>=rank.get(best.get('source'),0):best=record
                yield best
        else:
            by_time={}
            for record in candidates:
                other=by_time.get(record['at'])
                if other is None or rank.get(record.get('source'),0)>=rank.get(other.get('source'),0):
                    by_time[record['at']]=record
            yield from sorted(by_time.values(),key=lambda r:r['at'])
    emitted=False
    classified_position=0
    for record in preferred():
        if _classification is not None:
            times,flags,kind=_classification
            if classified_position>=len(times):break
            if record['at']!=times[classified_position]:continue
            row=dict(record);reset_kind=kind if classified_position==0 else None
            row.update(remaining=100-record['used'],reset_kind=reset_kind,
                markers=[RESET_NAMES[reset_kind]] if reset_kind else [],
                connect=bool(flags[classified_position]&1),tracking_continuous=bool(flags[classified_position]&2),label=None)
            classified_position+=1
            if labels:row['label']=_history_label(row)
            yield row
            continue
        if _checkpoint is not None:
            before=(count,removed,previous,(detector.remaining,detector.at,detector.scheduled_at,
                    detector.consumed_at,detector.recovering),control_position,enabled)
        interrupted=False
        while control_position<len(controls) and controls[control_position]['at']<=record['at']:
            interrupted=True
            enabled=bool(controls[control_position]['enabled'])
            control_position+=1
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
        if record.get('measurement_reset'):
            row['reset_kind']='measurement_reset';continuous=False
        elif previous and previous.get('measurement_reset'):
            continuous=False
        row['markers']=[RESET_NAMES[row['reset_kind']]] if row['reset_kind'] else []
        row['connect']=bool(continuous and count)
        row['tracking_continuous']=same_tracking
        row['label']=_history_label(row) if labels else None
        previous=row
        if start is not None and record['at']<start:continue
        count+=1
        if deletes:
            # Classify resets before hiding deleted observations, so surviving
            # cycles keep their boundaries across a removed middle cycle.
            index=bisect_right(delete_starts,row['at'])-1
            if index>=0 and row['at']<deletes[index]['end']:
                removed=True
                continue
            if removed:
                row={**row,'connect':False,'tracking_continuous':False}
                removed=False
        if _checkpoint is not None and (not emitted or row['reset_kind']):
            _checkpoint['value']=(record['at'],before)
        emitted=True
        yield row


HISTORY_GROUP_CACHE_BYTES=16*1024*1024
HISTORY_GROUP_CACHE_COUNT=4096


class _PeriodHistory:
    """Replay one selected period without retaining classified rows for every period."""
    def __init__(self,report,start,end,checkpoint,classification):
        self.report=dict(history=report['history'].range_blocks(start,end),account=report.get('account'),
            tracking={'controls':(report.get('tracking') or {}).get('controls',[])},
            cycle_clears=report.get('cycle_clears',[]))
        self.end=end;self.checkpoint=checkpoint
        self.classification=classification

    def __iter__(self):
        return _history_rows(self.report,'weekly',end=self.end,labels=False,
            _resume=pickle.loads(self.checkpoint),_classification=self.classification)


def _history_groups(report, previous):
    """Reuse classified closed periods without another retained row copy."""
    history=report.get('history')
    if not isinstance(history,_PackedHistory):return None,None
    account=report.get('account') or next((c.get('account') for c in report.get('cycles',[]) if c.get('account')), '')
    context=hashlib.sha256(pickle.dumps((report.get('account'),account,
        (report.get('tracking') or {}).get('controls',[]),report.get('cycle_clears',[]),
        any(r.get('account')==report.get('account') and r['window']=='weekly' for r in history)),protocol=5)).digest()
    prior=(previous or {}).get('_history_groups')
    old=prior if prior and prior['context']==context and prior['groups'] is not None else None
    reuse=0
    if old:
        if old['source'] is history:reuse=len(old['groups'])
        else:
            common=0
            for before,after in zip(old['source'].blocks,history.blocks):
                if before is not after:break
                common+=1
            if common:
                # A timestamp can straddle blocks. Replay its period and the
                # formerly current period so resets and duplicate ranks stay exact.
                changed_at=old['source'].time_ends[common-1]
                starts=[entry[0] for entry in old['groups']]
                reuse=max(0,min(len(starts)-1,bisect_left(starts,changed_at)-1))
    cache=dict(context=context,source=history,groups=[],weight=0,last=None)
    def remember(entry):
        cache['last']=entry
        if cache['groups'] is None:return
        weight=sys.getsizeof(entry)+sum(sys.getsizeof(value) for value in entry)+128
        if len(cache['groups'])>=HISTORY_GROUP_CACHE_COUNT or cache['weight']+weight>HISTORY_GROUP_CACHE_BYTES:
            cache['groups']=None;cache['weight']=0
        else:cache['groups'].append(entry);cache['weight']+=weight
    def saved(entry):
        source=_PeriodHistory(report,entry[0],entry[3],entry[4],(entry[7],entry[8],entry[1]))
        return dict(start=entry[0],reset_kind=entry[1],partial=entry[2],rows=None,
            _last_at=entry[3],_signature=entry[5],_matches_account=entry[6],_times=entry[7],
            _source=source,_load=lambda:iter(source))
    def source():
        if old:
            for entry in old['groups'][:reuse]:
                remember(entry)
                yield saved(entry)
            if old['source'] is history:return
        resume=(pickle.loads(old['groups'][reuse][4]) if old and reuse else report.get('_history_resume'))
        checkpoint={};group=None;state=None
        def finished(group,state):
            digest=hashlib.sha256(pickle.dumps(group,protocol=5)).digest()
            entry=(group['start'],group['reset_kind'],group['partial'],group['rows'][-1]['at'],
                   pickle.dumps(state,protocol=5),digest,
                   any(not account or row.get('account')==account for row in group['rows']),
                   array('d',(row['at'] for row in group['rows'])),
                   bytes(int(row['connect'])+2*int(row['tracking_continuous']) for row in group['rows']))
            remember(entry)
            return saved(entry)
        for row in _history_rows(report,'weekly',labels=False,_resume=resume,_checkpoint=checkpoint):
            if group is None or row['reset_kind']:
                if group is not None:yield finished(group,state)
                group=dict(start=row['at'],reset_kind=row['reset_kind'],partial=not bool(row['reset_kind']),rows=[])
                state=checkpoint['value']
            group['rows'].append(row)
        if group is not None:yield finished(group,state)
    return source(),cache


def _history_label(row):
    return f"{clock(row['at'],True)}, 잔여 {row['remaining']:g}%"+(', '+row['markers'][0] if row['markers'] else '')


def observation_label(row, money=False):
    from .pricing import usd
    if row.get('period_summary'):
        return (f"{clock(row['cycle_start'])} → {clock(row['at'])}, 주기 소모량 {row['remaining']:g}%p, "
                f"주간 예상 가치 {usd(row['cycle_value'])}, 주기 환산액 {usd(row['cycle_cost'])}")
    label=row.get('label') or _history_label(row)
    if money:
        label+=(f" / 누적 환산액 {usd(row.get('cycle_cost'))}"
                f" / 주간 동등 가치 {usd(row.get('cycle_value'))}")
        if row.get('value_held'):label+=', 마지막 확인값'
    return label


def completed_percent_costs(rows):
    """Price a displayed percent only after its next 1pp decrease is observed.

    Every observation on a plateau shares its completed amount. Never fill in
    skipped percent boundaries or price a partial opening plateau. An idle gap
    with unchanged allowance and cost does not discard the observed plateau.
    This runs during preparation so detail selection stays constant-time.
    """
    if any(r.get('user_excluded') for r in rows):
        return _completed_percent_costs_python(rows)
    native=get_native()
    if native is not None:
        try:return native.completed_percent_costs(rows)
        except OverflowError:pass  # Keep exact integer differences beyond f64.
    return _completed_percent_costs_python(rows)


def _completed_percent_costs_python(rows):
    # Summing interval costs in a different order can move the same USD value
    # by a few floating-point bits. This is not a decrease or new gap activity.
    def same_cost(a,b):
        return a is not None and b is not None and math.isclose(a,b,rel_tol=0,abs_tol=1e-9)

    amounts=[None]*len(rows)
    start=0;valid=False
    previous=None
    for index,row in enumerate(rows):
        remaining=row.get('reported_remaining',row['remaining'])
        cost=row.get('period_cost',row.get('cycle_cost'))
        known=(cost is not None and math.isfinite(cost) and cost>=0)
        if row.get('user_excluded'):
            start=index
            valid=(known and row.get('user_exclusion_end',False) and float(remaining).is_integer()
                   and previous is not None
                   and remaining<previous.get('reported_remaining',previous['remaining']))
            previous=row
            continue
        boundary=(previous is None or row.get('reset_kind') or
                  any(row.get(key)!=previous.get(key) for key in ('cycle_start','account')))
        if boundary:
            start=index
            valid=(remaining==100 and known and cost==0)
        elif not row['connect'] and not (
                row.get('tracking_continuous') and known and
                remaining==previous.get('reported_remaining',previous['remaining']) and
                same_cost(cost,previous.get('period_cost',previous.get('cycle_cost')))):
            start=index;valid=False
        else:
            old_remaining=previous.get('reported_remaining',previous['remaining'])
            old_cost=previous.get('period_cost',previous.get('cycle_cost'))
            valid=valid and known and old_cost is not None and (cost>=old_cost or same_cost(cost,old_cost))
            if remaining!=old_remaining:
                if valid and old_remaining-remaining==1 and float(old_remaining).is_integer():
                    opening=rows[start]
                    amount=max(0,cost-opening.get('period_cost',opening.get('cycle_cost')))
                    amounts[start:index]=[amount]*(index-start)
                start=index
                valid=(remaining<old_remaining and float(remaining).is_integer() and known)
        previous=row
    return amounts


def prepare_series(rows):
    """Bound drawing size while retaining extrema and exact selectable records."""
    excluded=boundaries=False
    for row in rows:
        boundaries|=bool(row.get('user_exclusion_end'))
        if row.get('user_excluded'):
            excluded=boundaries=True
            break
    native=get_native()
    if native is not None:
        prepared=rows
        if excluded and not all('completed_cost' in r for r in rows):
            amounts=completed_percent_costs(rows)
            prepared=[{**r,'completed_cost':r.get('completed_cost',amount)} for r,amount in zip(rows,amounts)]
        try:
            series=dict(native.quota_series(prepared),rows=rows)
            return _preserve_exclusion_boundaries(series) if boundaries else series
        except OverflowError:pass  # Preserve arbitrary-size integer accounting.
    series=_prepare_series_python(rows)
    return _preserve_exclusion_boundaries(series) if boundaries else series


def _preserve_exclusion_boundaries(series):
    rows=series['rows']
    boundaries=set()
    for index,row in enumerate(rows):
        if row.get('user_excluded') and (not index or not rows[index-1].get('user_excluded')):
            boundaries.update((max(0,index-1),index))
        if row.get('user_exclusion_end'):
            boundaries.update((index,min(len(rows)-1,index+1)))
    if boundaries:
        for budget,sample in series['samples'].items():
            series['samples'][budget]=sorted(set(sample)|boundaries)
    return series


def _prepare_series_python(rows):
    times=[];breaks=[];gaps=0;maximum=0;low=float('inf');high=0;cost_maximum=0;value_maximum=0
    value_minimum=None
    calculated=completed_percent_costs(rows)
    completed=[row.get('completed_cost',calculated[i]) for i,row in enumerate(rows)]
    completed_maximum=max((value for value in completed if value is not None),default=0)
    missing={key:[] for key in ('cycle_cost','cycle_value','completed_cost')}
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
        value=row.get('cycle_value')
        if value is not None:value_minimum=value if value_minimum is None else min(value_minimum,value)
        for key in missing:
            counts[key]+=(completed[index] if key=='completed_cost' else row.get(key)) is None
            if key=='completed_cost' and index:
                counts[key]+=bool(row.get('reset_kind') or any(
                    row.get(field)!=rows[index-1].get(field) for field in ('cycle_start','account')))
            missing[key].append(counts[key])
    samples={}
    for budget in (256,768):
        if len(rows)<=budget*2:
            samples[budget]=list(range(len(rows)));continue
        selected=set();span=max(1,active);buckets={}
        for i,row in enumerate(rows):
            bucket=min(budget-1,int(active_times[i]/span*budget))
            state=buckets.setdefault(bucket,[i,i,{}]);state[1]=i
            for key in ('remaining','cycle_cost','cycle_value','completed_cost'):
                values=completed if key=='completed_cost' else None
                value=values[i] if values is not None else row.get(key)
                if value is None:continue
                pair=state[2].setdefault(key,[i,i])
                if value<(values[pair[0]] if values is not None else rows[pair[0]][key]):pair[0]=i
                if value>(values[pair[1]] if values is not None else rows[pair[1]][key]):pair[1]=i
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
                cost_minimum=min((r['cycle_cost'] for r in rows if r.get('cycle_cost') is not None),default=None),
                completed_minimum=min((v for v in completed if v is not None),default=None),
                cost_maximum=cost_maximum,value_minimum=value_minimum,value_maximum=value_maximum,
                completed_costs=completed,completed_maximum=completed_maximum)


def _period_rows(period,*,labels=True):
    if period['series'] is not None:return period['series']['rows']
    from .quota_cycles import _quota_value_rows
    detail=period['_series_detail']
    group=dict(rows=list(detail['source']),start=detail['start'],partial=detail['partial'])
    rows=_completed_period_rows(_quota_value_rows(group,{'cost':detail['cost']},
        detail['intervals'],detail['display_ranges'],detail['exclusions'],detail['account'],detail['forward']))
    if labels:
        for row in rows:row['label']=_history_label(row)
    return rows


def _completed_period_rows(rows):
    completed=completed_percent_costs(rows)
    for row,amount in zip(rows,completed):row['completed_cost']=amount
    own=[row for row in rows if row['local_observed']]
    for index,row in enumerate(own):
        if not index or row['local_range']!=own[index-1]['local_range']:row['connect']=False
    return own


CHART_FIELDS=('at','remaining','cycle_start','account','cycle_cost','cycle_value','completed_cost',
    'connect','tracking_continuous','reset_kind','markers','value_held','user_excluded','user_exclusion_end',
    'period_summary','current','partial')


def _chart_row(row):
    """Exact display/selection values, without accounting and source metadata."""
    return {key:row[key] for key in CHART_FIELDS if key in row}


def display_series(series):
    if series.get('_display_only'):return series
    return {**series,'rows':_PackedRows(_chart_row(row) for row in series['rows']),'_display_only':True}


def _period_series(rows, intervals, exclusions,*,display=False):
    from .quota_share import prepare_model_share
    if display:rows=_PackedRows(_chart_row(row) for row in rows)
    series=prepare_series(rows)
    series['model_share']=prepare_model_share(series,intervals,packed=display)
    series.update(exclusions=exclusions,active_only=True)
    if display:series['_display_only']=True
    return series


def period_series(period,*,display=False):
    """Prepare exact detail and model shares only for the requested chart."""
    if period.get('_archive') is not None:return period['_archive'].series(period['start'],display=display)
    if period['series'] is not None:return display_series(period['series']) if display else period['series']
    return _period_series(_period_rows(period,labels=not display),period['_series_intervals'],period['_series_exclusions'],display=display)


def prepare_period_summary(periods):
    """One exact aggregate per observed period, without expanding detail rows."""
    rows=[dict(at=period['end'],cycle_start=period['start'],remaining=period['delta'],
               cycle_cost=period['cost'],cycle_value=period['value'],completed_cost=None,
               reset_kind=None,markers=[],connect=bool(i),tracking_continuous=True,
               period_summary=True,current=period['current'],partial=period.get('partial',False))
          for i,period in enumerate(sorted(periods,key=lambda item:item['start']))]
    series=prepare_series(rows)
    series.update(cycle_summary=True,_display_only=True)
    return series


def summary_deadline(report, start):
    """A moving lower bound changes totals only after a retained endpoint."""
    until=float('inf')
    for cycle in report.get('cycles',[]):
        boundary=cycle.get('start')
        if boundary is not None and boundary>=start:until=min(until,boundary)
        points=cycle.get('endpoints',[])
        position=bisect_left(points,start,key=lambda point:point[0])
        if position<len(points):until=min(until,points[position][0])
    return until


def prepare_quota_view(report, previous=None, *, deferred=False, _requests=None):
    from .pricing import usd
    from .quota_exclusions import active_exclusions
    from .quota_attribution import RequestWindows
    exclusions=active_exclusions(report)
    requests=(_requests if _requests is not None else RequestWindows(report.get('request_windows',[]))) if report.get('tracking') is not None else None
    prepared_statistics=None
    if report.get('_cycle_versions') is not None:
        from .quota_cycles import _PreparedQuotaStatistics
        prepared_statistics=_PreparedQuotaStatistics(report,requests,(previous or {}).get('_prepared_statistics'))
        report={**report,'_prepared_statistics':prepared_statistics}
    period_cache=dict((previous or {}).get('_period_cache',{}))
    statistics_cache=dict((previous or {}).get('_statistics_cache',{}))
    prior_periods={p['start']:p for p in (previous or {}).get('periods',[])}
    def prepare_period(rows,period,unchanged):
        if period is None:return
        prior=prior_periods.get(period['start'])
        if unchanged:
            prior=prior or period
            period.update({k:prior[k] for k in ('series','_series_detail','_series_intervals',
                '_series_exclusions','_series_signature','_has_history') if k in prior})
            return
        if isinstance(rows,dict):
            period.update(series=None,_series_detail=rows,
                _series_intervals=period['cost_intervals'],
                _series_exclusions=[r for r in exclusions if r['start']<period['end'] and r['end']>period['start']],
                _series_signature=period_cache[period['start']][0])
            return
        own=_completed_period_rows(rows)
        own_exclusions=[r for r in exclusions if r['start']<period['end'] and r['end']>period['start']]
        period['series']=_period_series(own,period['cost_intervals'],own_exclusions)
    groups,history_groups=_history_groups(report,previous) if deferred else (None,None)
    source=None if groups is not None else _history_rows(report,'weekly',labels=False) if deferred else history_rows(report,'weekly')
    rows,periods=quota_value_history(report,source,_requests=requests,_cache=period_cache,
        _statistics_cache=statistics_cache,_period_sink=prepare_period if deferred else None,_groups=groups,_defer_rows=deferred)
    if not deferred:
        times=[r['at'] for r in rows]
        for i,period in enumerate(periods):
            lo=bisect_left(times,period['start'])
            hi=bisect_left(times,periods[i+1]['start']) if i+1<len(periods) else len(rows)
            unchanged=bool(previous and period_cache.get(period['start']) is previous.get('_period_cache',{}).get(period['start']))
            prepare_period(rows[lo:hi],period,unchanged)
            if period['series'] is None:period['series']=period_series(period)
            for key in ('_series_detail','_series_intervals','_series_exclusions','_series_signature','_has_history'):
                period.pop(key,None)
    for period in periods:
        state='진행 중' if period['current'] else '종료'
        period['label']=(f"{clock(period['start'])} → {clock(period['end'])}: {usd(period['value'])} ({state})")
    lifetime=quota_statistics(report,include_mode_assumptions=True,_requests=requests,_cache=statistics_cache)
    now=report.get('at')
    summaries={None:lifetime};summary_until={}
    if now is not None:
        earliest=min((cycle['start'] for cycle in report.get('cycles',[])),default=now)
        for days in (7,30,90):
            start=now-days*86400
            summaries[days]=(lifetime if start<=earliest else
                             quota_statistics(report,start,include_mode_assumptions=True,_requests=requests,_cache=statistics_cache))
            summary_until[days]=summary_deadline(report,start)+days*86400
    all_series=None if deferred else prepare_period_summary(periods)
    periods=[{k:v for k,v in period.items() if k!='cost_intervals'} for period in periods]
    available_models=sorted({model['model'] for interval in lifetime.get('intervals',[]) for model in interval['models']
                             if model.get('model') and not model.get('separate')})
    return dict(periods=sorted(periods,key=lambda period:period['start'],reverse=True),overall=all_series,lifetime=lifetime,summaries=summaries,available_models=available_models,_period_cache=period_cache,
                _statistics_cache=statistics_cache,_history_groups=history_groups,
                _series_cache=(previous or {}).get('_series_cache',{}),_summary_until=summary_until,
                _prepared_statistics=prepared_statistics,
                five_hour=None if deferred else prepare_series(history_rows(report,'five_hour')))


def model_cost_intervals(intervals, selected_model):
    """Keep account consumption separate from one model's known API cost."""
    if selected_model is None:
        return intervals
    result=[]
    for interval in intervals:
        models=[row for row in interval['models']
                if row.get('model')==selected_model and not row.get('separate')]
        if not models:
            continue
        known=[row['cost'] for row in models if row['cost'] is not None]
        cost=sum(known) if known else None
        whole=interval['cost']
        calls=sum(row['calls'] for row in models)
        priced=sum(row.get('priced',row['calls']) for row in models)
        total_calls=interval.get('calls',sum(row['calls'] for row in interval['models'] if not row.get('separate')))
        complete=priced==calls and interval.get('priced_calls',total_calls)>=total_calls
        result.append({**interval, 'model_filter':selected_model, 'model_cost':cost,
                       'model_share':cost/whole*100 if complete and cost is not None and whole is not None and whole>0 else None,
                       'model_calls':calls, 'model_priced':priced})
    return result


def prepare_quota_query(report, query, *, _requests=None):
    """Compute filter totals and one display page in the aggregation process."""
    view=report.get('view') or prepare_quota_view(report)
    days=query.get('days');start=query.get('start');end=query.get('end')
    model=query.get('model');mode=query.get('mode','all')
    prepared=view.get('summaries',{})
    summary=(prepared[days] if days in prepared and end is None and (days is not None or start is None)
             else quota_statistics(report,start,end,True,_requests=_requests))
    intervals=model_cost_intervals(summary['intervals'],model)
    known=[row['model_cost'] for row in intervals if row['model_cost'] is not None] if model is not None else []
    totals=dict(count=len(intervals),calls=sum(row['model_calls'] for row in intervals) if model is not None else 0,
                priced=sum(row['model_priced'] for row in intervals) if model is not None else 0,
                cost=sum(known) if known else None)
    rows=([row for row in intervals if not row['excluded']] if mode=='used' else
          [row for row in intervals if row['excluded'] or row.get('assumptions')] if mode=='excluded' else intervals)
    partial=any(row['model_priced']<row['model_calls'] if model is not None else
                row.get('priced_calls',row['calls'])<row['calls'] for row in rows)
    page_size=max(1,min(100,int(query.get('page_size',25))))
    pages=max(1,math.ceil(len(rows)/page_size));page=max(0,min(int(query.get('page',0)),pages-1))
    # Raw evidence stays in the worker. The GUI receives only fields used by
    # the visible table and its model details, never all call/end-point graphs.
    visible=[{key:value for key,value in row.items() if key not in ('cost_rows','endpoints')}
             for row in rows[page*page_size:(page+1)*page_size]]
    selected=next((row for row in visible if row['id']==query.get('selected_id')),visible[0] if visible else None)
    return dict(statistics={**summary,'intervals':visible},model_totals=totals,
                has_unknown=any(row.get('unknown_mode_calls') for row in summary['intervals']),
                partial=partial,total_rows=len(rows),page=page,pages=pages,rows=visible,selected_interval=selected)


__all__ = ('clock','history_rows','observation_label','prepare_series','prepare_quota_view')
