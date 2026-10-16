"""Calendar trends retain one scalar bucket and bounded causal samples."""
from bisect import bisect_left, bisect_right
from collections import deque
from contextlib import contextmanager

from .analytics import effort_key, sorted_quantile
from .performance_trends import METRICS, assessment, box_statistics, calendar_edges, valid
from .prepared_store import _size
from .prepared_sums import _Sum
from .prepared_trends import Budget, Window, RAW


def variants_for(scopes, model_mode):
    result = []
    for scope, variants in scopes.items():
        if scope[:2] != model_mode:continue
        for raw, types in variants:
            pair = (raw[:2], types[:2])
            if pair not in result:result.append(pair)
    return result


def cache_seeds(source, variants, start, end, budget):
    """Only the final fifty session values can enter a causal baseline."""
    with budget.scratch() as reserve:
        totals = {}
        rows = source.ordered_rows(start, end, inclusive=False, variants=variants, bucket=True)
        try:
            for row in rows:
                owner = row['_owner']
                if owner not in totals:
                    reserve(_size(owner) + 480);totals[owner] = [_Sum(), _Sum()]
                if (valid(row.get('input')) and valid(row.get('cached')) and row['cached'] <= row['input']
                        and not row.get('input_conflict')):
                    own = totals[owner];before = _size((own[0].total, own[1].total))
                    own[0].add(row['input']);own[1].add(row['cached'])
                    reserve(max(0, _size((own[0].total, own[1].total)) - before))
        finally:
            rows.close()
        values = deque(maxlen=50)
        for amount, cached in totals.values():
            if amount.value():values.append(100 * cached.value() / amount.value())
        reserve(_size(tuple(values)))
        return list(values)


class Group:
    def __init__(self, key, budget):
        self.key, self.budget = key, budget
        self.scopes, self.efforts, self.refs = set(), {}, {}
        self.last = None
        self.count = 0
        self.bytes = 0
        self.charge(_size(key) + 512)

    def charge(self, amount):
        self.budget.add(amount);self.bytes += amount

    def add(self, row, scope):
        self.count += 1
        if scope not in self.scopes:
            self.charge(96);self.scopes.add(scope)
        effort = row.get('effort') or '미확인'
        if effort not in self.efforts:
            self.charge(_size(effort) + 256);self.efforts[effort] = set()
        self.efforts[effort].add(scope)
        record = {name:row.get(name) for name in ('home', 'sid', 'key', 'turn')}
        owner = row['_owner']
        self.charge(_size(record) - (_size(self.refs[owner]) if owner in self.refs else 0) +
                    (0 if owner in self.refs else _size(owner) + 96))
        self.refs[owner] = record;self.last = record


class Bucket:
    def __init__(self, budget, source):
        self.budget, self.source = budget, source
        self.window = Window(budget, source.snapshot._check)
        self.groups, self.scopes = {}, {}
        self.scope_bytes = 0

    def add(self, row):
        scope = tuple(row.get(name) or '미확인' for name in ('model', 'service_tier', 'effort'))
        if scope not in self.scopes:
            amount = _size(scope) + 96;self.budget.add(amount);self.scope_bytes += amount
            self.scopes[scope] = len(self.scopes)
        number = self.scopes[scope]
        if scope[:2] not in self.groups:self.groups[scope[:2]] = Group(scope[:2], self.budget)
        self.groups[scope[:2]].add(row, number)
        self.window.append(row, number)

    def clear(self):
        self.window.discard(float('inf'))
        self.budget.add(-self.scope_bytes - sum(group.bytes for group in self.groups.values()))
        self.groups.clear();self.scopes.clear();self.scope_bytes = 0


@contextmanager
def population(bucket, group, metric, start, end, inclusive):
    scopes = group.efforts[metric.split(':', 1)[1]] if metric.startswith('reasoning:') else group.scopes
    field = 'reasoning' if metric.startswith('reasoning:') else metric
    with bucket.budget.scratch() as reserve:
        reserve(512)
        values, owners, samples = [], {}, []
        outputs, seconds, calls = [], [], 0
        for block, position, _ in bucket.window.entries(start, end, inclusive, scopes):
            calls += 1;owner = block.owners[position]
            if owner not in owners:
                reserve(_size(owner) + 480)
                owners[owner] = [_Sum(), _Sum(), 0, 0] if metric == 'session_cache' else [0, 0, 0, 0]
            own = owners[owner];own[3] += 1
            if metric == 'session_cache':
                amount, cached = (block.values[name].at(position) for name in ('input', 'cached'))
                if amount is not None and cached is not None and cached <= amount and not block.conflicts[position]:
                    before = _size((own[0].total, own[1].total))
                    own[0].add(amount);own[1].add(cached);own[2] += 1
                    reserve(max(0, _size((own[0].total, own[1].total)) - before))
            elif metric != 'count':
                value = block.values[field].at(position)
                if value is None:continue
                reserve(_size(value) + 24);values.append(value)
                if metric == 'output_speed':
                    output = block.values['output'].at(position)
                    elapsed = block.values['completion_latency_ms'].at(position) / 1000
                    reserve(_size(output) + _size(elapsed) + 32)
                    outputs.append(output);seconds.append(elapsed)
        if metric == 'session_cache':
            input_total = cached_total = valid_calls = 0
            for owner, (amount, cached, eligible, total) in owners.items():
                amount, cached = amount.value(), cached.value()
                if not amount:continue
                value = 100 * cached / amount
                reserve(_size(value) + 24 + 128)
                values.append(value);samples.append((value, owner, eligible, total))
                input_total += amount;cached_total += cached;valid_calls += eligible
            denominator = len(owners)
            totals = dict(input=input_total, cached=cached_total, valid_calls=valid_calls)
        else:
            denominator = calls;totals = dict(sum=sum(values)) if values else {}
            if metric == 'output_speed':totals = dict(output=sum(outputs), seconds=sum(seconds))
        mean = (calls if metric == 'count' else totals['output'] / totals['seconds'] if
                metric == 'output_speed' and totals['seconds'] else None if metric == 'output_speed' else
                sum(values) / len(values) if values else None)
        ordered = sorted(values)
        distribution = dict(median=sorted_quantile(ordered,.5),
            p10=sorted_quantile(ordered,.1) if len(ordered)>=10 else None,
            p90=sorted_quantile(ordered,.9) if len(ordered)>=10 else None,
            minimum=min(ordered,default=None),maximum=max(ordered,default=None),**box_statistics(ordered))
        yield dict(values=values, samples=samples, eligible=calls if metric == 'count' else len(values),
            total=denominator, totals=totals, mean=mean, distribution=distribution, calls=calls, sessions=len(owners))


class Calendar:
    def __init__(self, source, q, width, start, end, first, scopes, granularity):
        self.source, self.q, self.width = source, q, width
        self.start, self.end, self.scopes, self.unit = start, end, scopes, granularity
        self.edges = calendar_edges(min(first, start), end, granularity)
        source._admit(_size(self.edges))
        boundary = bisect_left(self.edges, start)
        self.boundary = self.edges[boundary - 1] if boundary and self.edges[boundary] == start else None
        self.seed_before = self.boundary if self.boundary is not None else self.edges[bisect_right(self.edges,start)-1]
        self.budget = Budget(source.limit)
        efforts = sorted({scope[2] for scope in scopes}, key=effort_key)
        self.specs = list(METRICS) + [('reasoning:'+effort,'평균 추론 토큰 / '+effort,'reasoning') for effort in efforts] + [('count','호출 수','count')]
        self.state = {metric:dict(pools={}, pending={}, pixels={}, n=0, N=0, low=None, high=None, lines={})
                      for metric, _, _ in self.specs}

    def seed(self, metric, scope):
        state = self.state[metric]
        if scope in state['pools']:return
        values = []
        if metric not in ('session_cache','count'):
            field = 'reasoning' if metric.startswith('reasoning:') else metric
            for raw, types in self.scopes.get(scope, ()):
                values.extend(self.source.predecessors(raw, types, field, self.start))
                values = sorted(values)[-50:]
            values = [value for _, value in values]
        else:
            variants = variants_for(self.scopes, scope[:2])
            before = self.seed_before
            if metric == 'count':before = min(before,calendar_edges(self.q['now'],self.q['now'],self.unit)[0])
            while len(values) < 50:
                stamp = self.source.latest(variants, before, cache=metric=='session_cache')
                if stamp is None:break
                a, z = calendar_edges(stamp, stamp, self.unit)
                older = (cache_seeds(self.source, variants, a, z, self.budget) if metric=='session_cache' else
                         [self.source.count(variants, a, z)])
                values = (older + values)[-50:];before = a
        self.budget.add(_size(scope) + _size(values) + 2048)
        state['pools'][scope] = deque(values,maxlen=50)

    def add(self, metric, value, stamp, group, effort, record, complete=True, coverage=None):
        if not valid(value):return
        scope = (*group, effort if metric not in ('session_cache','count') else '')
        self.seed(metric, scope)
        state = self.state[metric];pool = state['pools'][scope]
        pending = state['pending'].get(scope)
        before = _size(tuple(pool)) + _size(tuple(pending[1]) if pending else ())
        if pending is not None and pending[0] < stamp:
            pool.extend(pending[1]);state['pending'].pop(scope);pending = None
        verdict = assessment(value,pool,metric=='session_cache',complete)
        if complete:
            if pending is None:
                pending = (stamp,deque(maxlen=50));state['pending'][scope] = pending
            pending[1].append(value)
        self.budget.add(_size(tuple(pool)) + _size(tuple(pending[1]) if pending else ()) - before)
        if not self.start <= stamp <= self.end:return
        state['n'] += 1
        if state['low'] is None or value < state['low']:state['low'] = value
        if state['high'] is None or value > state['high']:state['high'] = value
        value = dict(ts=stamp,value=value,model=group[0],service_tier=group[1],effort=effort,
                     record=record,coverage=coverage,**verdict)
        pixel = min(self.width-1,max(0,int((stamp-self.start)/(self.end-self.start)*self.width)))
        key = (*group,pixel);cell = state['pixels'].get(key)
        before = _size(cell) if cell is not None else 0
        if cell is None:
            cell = state['pixels'][key] = dict(first=value,low=value,high=value,last=value,anomalies={})
        else:
            cell['last'] = value
            if value['value'] < cell['low']['value']:cell['low'] = value
            if value['value'] > cell['high']['value']:cell['high'] = value
        direction = verdict['direction']
        if direction:
            anomaly = cell['anomalies'].get(direction)
            cell['anomalies'][direction] = (anomaly[0],anomaly[1]+1) if anomaly else (value,1)
        self.budget.output(_size(cell)-before+(96 if before==0 else 0))

    def raw(self, row):
        group = tuple(row.get(name) or '미확인' for name in ('model','service_tier'))
        effort = row.get('effort') or '미확인'
        record = {name:row.get(name) for name in ('home','sid','key','turn')}
        for field in RAW:
            metric = 'reasoning:'+effort if field=='reasoning' else field
            self.state[metric]['N'] += 1
            self.add(metric,row.get(field),row['ts'],group,effort,record)

    def flush(self, bucket, index, *, boundary=False):
        a, z = self.edges[index:index+2]
        for metric, _, _ in self.specs:
            if boundary and metric not in ('session_cache','count'):continue
            state = self.state[metric]
            for key, group in sorted(bucket.groups.items()):
                if metric.startswith('reasoning:') and metric.split(':',1)[1] not in group.efforts:continue
                with population(bucket,group,metric,a,z,not boundary) as value:
                    eligible,total,mean = value['eligible'],value['total'],value['mean']
                    if metric=='session_cache':
                        for amount,owner,valid_calls,own_calls in value['samples']:
                            self.add(metric,amount,min(z,self.end),key,'',group.refs[owner],coverage=(valid_calls,own_calls))
                        if not boundary:state['N'] += total
                    elif metric=='count':
                        if not boundary:state['N'] += 1
                        self.add(metric,mean,min(z,self.end),key,'',group.last,
                            z<=self.q['now'] and z<=self.end and (z<=self.start or a>=self.start),(eligible,total))
                    if boundary:continue
                    complete = a>=self.start and z<=min(self.q['now'],self.end)
                    series = state['lines'].setdefault(key,[])
                    previous = series[-1] if series else None
                    representative = value['distribution']['median'] if metric=='duration' else mean
                    comparable = bool(complete and previous and previous['complete'] and previous['bucket']==index-1 and
                        valid(representative) and valid(previous['value']))
                    baseline = previous['value'] if comparable else None
                    status = '진행 중인 구간' if z>self.q['now'] else '일부 구간' if not complete else '비교 구간 없음'
                    point = dict(kind='trend',ts=(max(a,self.start)+min(z,self.end))/2,value=representative,mean=mean,bucket=index,
                        start=max(a,self.start),end=min(z,self.end),complete=complete,model=key[0],service_tier=key[1],
                        effort=metric.split(':',1)[1] if metric.startswith('reasoning:') else '',coverage=(eligible,total),
                        baseline=baseline,delta=representative-baseline if baseline is not None else None,
                        distribution=value['distribution'],totals=value['totals'],calls=value['calls'],sessions=value['sessions'],
                        baseline_count=previous['coverage'][0] if comparable else 0,direction=0,status=status,
                        previous_start=previous['start'] if comparable else None,previous_end=previous['end'] if comparable else None)
                    self.budget.output(_size(point)+96);series.append(point)

    def finish(self, full, calls):
        panels = []
        for metric,title,unit in self.specs:
            state = self.state[metric];samples = []
            for cell in state['pixels'].values():
                samples.extend({id(cell[name]):cell[name] for name in ('first','low','high','last')}.values())
                for direction in (-1,1):
                    if direction in cell['anomalies']:
                        first,count = cell['anomalies'][direction]
                        value = dict(first,count=count);self.budget.output(_size(value)+8);samples.append(value)
            panels.append(dict(key=metric,title=title,unit=unit,n=state['n'],N=state['N'],points=sorted(samples,key=lambda p:p['ts']),
                low=0 if state['low'] is None else state['low'],high=1 if state['high'] is None else state['high'],
                lines=[dict(model=key[0],service_tier=key[1],points=points) for key,points in state['lines'].items()]))
        self.source.stats.update(scalar_peak_bytes=self.budget.peak,scratch_peak_bytes=self.budget.scratch_peak,output_bytes=self.budget.result)
        return dict(panels=panels,start=self.start,end=self.end,full=full,granularity=self.unit,
            models=sorted({scope[0] for scope in self.scopes}),calls=calls,
            valid_until=calendar_edges(self.q['now'],self.q['now'],self.unit)[-1])


def calendar(source,q,width,start,end,first,full,scopes,calls,granularity):
    reducer = Calendar(source,q,width,start,end,first,scopes,granularity)
    bucket = Bucket(reducer.budget,source)
    if reducer.boundary is not None:
        rows = source.ordered_rows(reducer.boundary,start,keys=True,inclusive=False,bucket=True)
        try:
            for row in rows:bucket.add(row)
            if bucket.groups:reducer.flush(bucket,bisect_right(reducer.edges,reducer.boundary)-1,boundary=True)
        finally:
            rows.close();bucket.clear()
    rows = source.ordered_rows(start,end,keys=True)
    current = None
    try:
        for row in rows:
            index = bisect_right(reducer.edges,row['ts'])-1
            if current is not None and index != current:
                reducer.flush(bucket,current);bucket.clear()
            current = index
            reducer.raw(row);bucket.add(row)
        if current is not None:reducer.flush(bucket,current)
    finally:
        rows.close();bucket.clear()
    return reducer.finish(full,calls)
