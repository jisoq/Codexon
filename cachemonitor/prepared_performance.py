"""Pinned scalar cursors and exact causal predecessors for prepared charts.

Metadata and seeds retain their admission budget. Active numeric columns and
exact reduction scratch share the page-object budget. SQLite's temporary scalar
sort has an explicit payload admission; the pinned deadline and VM guard cover
all statements, including work before their first delivered row.
"""
from collections import defaultdict
from heapq import heappush, heapreplace

from .analytics import effort_key
from .prepared_store import METRICS, CapacityExceeded, NotReady, _restore_scalar, _size, unpack


FIELDS = ('ts', 'session_ordinal', 'duration', 'output_speed', 'cost', 'input',
          'cached', 'reasoning', 'output', 'completion_latency_ms', 'model',
          'service_tier', 'effort', 'input_conflict')
RAW_FIELDS = ('duration', 'output_speed', 'cost', 'input', 'reasoning')
DIMENSIONS = ('model', 'service_tier', 'effort')
POSITIONS = {name: i for i, name in enumerate(METRICS)}


def _tag(name):
    return f'substr(r.types,{POSITIONS[name]+1},1)'


def _valid(name):
    column = 'r.m_' + name
    return (f'(({_tag(name)} IN (x\'03\',x\'04\',x\'0a\') AND {column}>=0) '
            f'OR ({_tag(name)}=x\'09\' AND substr({column},1,1)<>\'-\'))')


def _value(row, name):
    return _restore_scalar(row['types'][POSITIONS[name]], row['m_' + name])


def _label(value):
    return value or '미확인'


def _sort_charge(keys):
    charges = ["CASE typeof(r.m_" + name + ") WHEN 'text' THEN length(CAST(r.m_" + name +
               " AS BLOB)) WHEN 'blob' THEN length(r.m_" + name + ") ELSE 8 END+16" for name in FIELDS]
    charge = '(256+length(r.types)+length(r.identity)+' + '+'.join(charges) + ')'
    return '(' + charge + '+length(r.key_value)+COALESCE(length(r.turn_value),0))' if keys else charge


class _Input:
    def __init__(self, engine):
        self.engine, self.snapshot = engine, engine.snapshot
        if self.snapshot is None:
            raise NotReady('performance reads require the visible pinned generation')
        self.limit = self.snapshot.limits.page_objects_bytes
        self.retained = 0
        self.owners, self.rank = {}, {}
        self.stats = dict(selected_rows=0, predecessor_rows=0, bucket_rows=0,
                          metadata_groups=0, sql_statements=0, body_decodes=0)
        ranks = {}
        for i, key in enumerate(engine.sessions):
            self._admit(_size((key, i)) + 64)
            ranks[key] = i
        excluded = []
        with self.snapshot._cursor(f'SELECT o.id,o.home,o.sid FROM {self.snapshot._owner_tables()} '
                                   'WHERE v.deleted=0') as cursor:
            for row in cursor:
                key = (row['home'], row['sid'])
                if key in ranks:
                    self._admit(_size((row['id'], key, ranks[key])) + 128)
                    self.owners[row['id']] = key
                    self.rank[row['id']] = ranks[key]
                else:
                    self._admit(_size(row['id']) + 16)
                    excluded.append(row['id'])
        if len(self.owners) != len(ranks):
            raise NotReady('performance owner population differs from the visible generation')
        # Only integer database IDs are interpolated. A separate parameter per
        # active owner would impose SQLite's variable limit on catalog size.
        self.excluded = (' AND r.owner NOT IN (' + ','.join(str(x) for x in excluded) + ')'
                         if excluded else '')
        self._admit(_size(self.excluded))

    def _admit(self, size):
        self.retained += size
        if self.retained > self.limit:
            raise CapacityExceeded('performance metadata and predecessor budget exceeded')

    def sql(self, columns, where='1', *, index='calls_global_time', tail=''):
        return (f'SELECT {columns} FROM calls r INDEXED BY {index} '
                'JOIN publications p ON p.id=r.batch '
                'JOIN owner_heads h ON h.owner=r.owner '
                'JOIN owner_versions v ON v.owner=h.owner AND v.batch=h.batch '
                f'WHERE r.deleted=0 AND v.deleted=0 AND {self.snapshot._visible("calls")} '
                f'AND {_valid("ts")} AND r.m_purpose IS NOT \'maintenance\' '
                "AND COALESCE(NULLIF(r.m_requested_model,''),r.m_model,'')<>'codex-auto-review' "
                f'{self.excluded} AND ({where}) {tail}')

    def cursor(self, *args, **kwargs):
        self.stats['sql_statements'] += 1
        return self.snapshot._cursor(self.sql(*args, **kwargs), self.args)

    def bounds(self):
        self.args = {}
        result = []
        for order in ('ASC', 'DESC'):
            with self.cursor('r.m_ts', tail=f'ORDER BY r.m_ts {order} LIMIT 1') as cursor:
                row = cursor.fetchone()
                result.append(row[0] if row else None)
        return result

    def metadata(self, end, *, sort_start, sort_keys):
        self.args = dict(start=sort_start, end=end)
        columns = ','.join('r.m_' + name for name in DIMENSIONS)
        tags = ','.join(_tag(name) + ' AS t' + str(i) for i, name in enumerate(DIMENSIONS))
        grouped = columns + ',' + ','.join(_tag(name) for name in DIMENSIONS)
        scopes, count = defaultdict(list), 0
        charge = 'COALESCE(SUM(CASE WHEN r.m_ts>=:start THEN ' + _sort_charge(sort_keys) + ' ELSE 0 END),0)'
        size = 0
        with self.cursor(columns + ',' + tags + ',COUNT(*) AS n,' + charge + ' AS sort_bytes', 'r.m_ts<=:end',
                         tail='GROUP BY ' + grouped) as cursor:
            for row in cursor:
                raw = tuple(row['m_' + name] for name in DIMENSIONS)
                types = tuple(row['t' + str(i)][0] for i in range(3))
                labels = tuple(_label(_restore_scalar(tag, value)) for tag, value in zip(types, raw))
                value = (raw, types)
                scopes[labels].append(value)
                self._admit(_size((labels, value)))
                count += row['n']
                size += row['sort_bytes']
                self.stats['metadata_groups'] += 1
        # Metadata already visits these exact scalar rows. Reuse its byte sum
        # for the main window; narrower predecessor queries keep their own guard.
        self.sort_admission = (sort_start, end, sort_keys, size)
        self._admit(_size(self.sort_admission))
        return scopes, count

    def scope(self, raw, types, count=3):
        clauses = []
        for i, name in enumerate(DIMENSIONS[:count]):
            self.args['d' + str(i)] = raw[i]
            self.args['t' + str(i)] = bytes((types[i],))
            clauses.extend((f'r.m_{name} IS :d{i}', f'{_tag(name)}=:t{i}'))
        return ' AND '.join(clauses)

    def order(self, row):
        return (row['m_ts'], self.rank[row['owner']], row['m_session_ordinal'], row['identity'])

    def ordered_rows(self, start, end, *, keys=False, inclusive=True, variants=None, bucket=False):
        """One scalar cursor, preserving the visible engine's exact owner rank."""
        self.args = dict(start=start, end=end)
        where = 'r.m_ts>=:start AND r.m_ts' + ('<=:end' if inclusive else '<:end')
        if not self.rank:
            return
        if not hasattr(self, 'ordering'):
            self.ordering = ','.join(f'({owner},{rank})' for owner, rank in self.rank.items())
            self._admit(_size(self.ordering))
        if variants is not None:
            predicates = []
            for number, (raw, types) in enumerate(variants):
                clauses = []
                for index, name in enumerate(DIMENSIONS[:2]):
                    key = f'v{number}_{index}'
                    self.args[key] = raw[index];self.args[key + 't'] = bytes((types[index],))
                    clauses.extend((f'r.m_{name} IS :{key}', f'{_tag(name)}=:{key}t'))
                predicates.append('(' + ' AND '.join(clauses) + ')')
            if not predicates:return
            where += ' AND (' + ' OR '.join(predicates) + ')'
        columns = 'r.owner,r.types,' + ','.join('r.m_' + name for name in FIELDS)
        if keys:columns += ',r.key_value,r.turn_value'
        admitted = getattr(self, 'sort_admission', None)
        if variants is None and inclusive and admitted is not None and admitted[:3] == (start, end, keys):
            size = admitted[3]
        else:
            with self.cursor('COALESCE(SUM(' + _sort_charge(keys) + '),0)', where) as cursor:
                size = cursor.fetchone()[0]
        self.stats['sort_estimated_bytes'] = max(size, self.stats.get('sort_estimated_bytes', 0))
        if size > self.snapshot.limits.speed_sort_bytes:
            raise CapacityExceeded('performance scalar sort exceeds its admitted temporary payload')
        # The ordering cohort drives this join. Seek each owner's time range;
        # a global-time index would rescan every selected call for every owner.
        sql = self.sql(columns, where, index='calls_time',
            tail='ORDER BY r.m_ts,ordering.rank,r.m_session_ordinal,r.identity')
        sql = sql.replace('JOIN publications p ', 'JOIN ordering ON ordering.owner=r.owner JOIN publications p ', 1)
        sql = 'WITH ordering(owner,rank) AS MATERIALIZED (VALUES ' + self.ordering + ') ' + sql
        self.stats['sql_statements'] += 1
        with self.snapshot._cursor(sql, self.args) as cursor:
            for raw in cursor:
                self.snapshot._check()
                row = {name:_value(raw, name) for name in FIELDS if raw['types'][POSITIONS[name]]}
                row['_owner'] = raw['owner']
                if keys:
                    row['home'], row['sid'] = self.owners[raw['owner']]
                    row['key'] = unpack(raw['key_value'],self.snapshot.limits,maximum=self.snapshot.limits.key_bytes)
                    if raw['turn_value'] is not None:
                        row['turn'] = unpack(raw['turn_value'],self.snapshot.limits,maximum=self.snapshot.limits.key_bytes)
                self.stats['bucket_rows' if bucket else 'selected_rows'] += 1
                yield row

    def predecessors(self, raw, types, field, start):
        self.args = dict(start=start)
        scoped = self.scope(raw, types)
        where = f'{scoped} AND r.m_ts<:start AND {_valid(field)}'
        columns = f'r.owner,r.identity,r.m_ts,r.m_session_ordinal,r.types,r.m_{field}'
        heap = []

        def retain(row):
            item = (self.order(row), _value(row, field))
            if len(heap) < 50:
                heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapreplace(heap, item)
            self.stats['predecessor_rows'] += 1

        with self.cursor(columns, where, index='calls_dimensions',
                         tail='ORDER BY r.m_ts DESC,r.owner DESC,r.m_session_ordinal DESC,r.identity DESC LIMIT 50') as cursor:
            found = list(cursor)
        if len(found) < 50:
            for row in found:
                retain(row)
        else:
            cutoff = found[-1]['m_ts']
            for row in found:
                if row['m_ts'] > cutoff:
                    retain(row)
            # The index owner order is not the source traversal order. Resolve
            # the entire cutoff timestamp group with bounded top-50 ownership.
            self.args['cutoff'] = cutoff
            with self.cursor(columns, where + ' AND r.m_ts=:cutoff', index='calls_dimensions') as cursor:
                for row in cursor:
                    self.snapshot._check()
                    retain(row)
        return sorted(heap)

    def latest(self, variants, before, *, cache=False):
        latest = None
        for raw, types in variants:
            self.args = dict(before=before)
            where = self.scope(raw, types, count=2) + ' AND r.m_ts<:before'
            if cache:
                # A safe candidate superset: bigint/float comparisons and the
                # final per-session sums remain the existing Python semantics.
                where += (f' AND {_valid("input")} AND {_valid("cached")} AND r.m_input>0 '
                          "AND (r.m_input_conflict IS NULL OR r.m_input_conflict IN (0,'',x''))")
            with self.cursor('r.m_ts', where, index='calls_model_mode_time',
                             tail='ORDER BY r.m_ts DESC LIMIT 1') as cursor:
                row = cursor.fetchone()
            if row is not None and (latest is None or row[0] > latest):
                latest = row[0]
        return latest

    def count(self, variants, start, end):
        total = 0
        for raw, types in variants:
            self.args = dict(start=start, end=end)
            where = self.scope(raw, types, count=2) + ' AND r.m_ts>=:start AND r.m_ts<:end'
            with self.cursor('COUNT(*)', where, index='calls_model_mode_time') as cursor:
                total += cursor.fetchone()[0]
        return total



def performance_input(engine, q, width, granularity):
    """Finish an exact chart from one pinned scalar stream and bounded state."""
    source = _Input(engine)
    first, last = source.bounds()
    now = q['now']
    first = now if first is None else first
    full = (first, max(now, now if last is None else last, first + 1))
    start, end = q.get('time_range') or full
    if end <= start:
        start, end = full
    scopes, calls = source.metadata(end, sort_start=start, sort_keys=granularity != 'auto')
    progress=getattr(engine,'progress',None)
    if progress:progress('aggregate')
    if granularity == 'auto':
        from .prepared_trends import rolling
        view = rolling(source, q, width, start, end, full, scopes)
    else:
        from .prepared_calendar import calendar
        view = calendar(source, q, width, start, end, first, full, scopes, calls, granularity)
    engine.read_metrics.update(('trend_' + key, value) for key, value in source.stats.items())
    return dict(prepared_view=view)
