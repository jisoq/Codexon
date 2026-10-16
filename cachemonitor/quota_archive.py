"""Disposable closed-period read projections, with immutable detail packets.

The ledger remains authoritative. A prefix is reused only while its source
dependencies remain valid. A reader owns a version, never a long SQLite read
transaction; two versions retain detail selection across ordinary refreshes.
"""
from collections import OrderedDict
from contextlib import closing
import hashlib
import json
import pickle
import sqlite3
from uuid import uuid4
import zlib

VERSION=3
MAX_BYTES=64*1024*1024
MAX_PACKET_BYTES=8*1024*1024
MAX_DECODED_BYTES=32*1024*1024
MAX_ROWS=65536
MAX_PERIODS=4096
MAX_HOMES=4
MAX_SCOPE_HOMES=64
MAX_REPAIR_SQL_STEPS=5_000_000
DETAIL_CACHE_BYTES=16*1024*1024
IGNORED_CHANGES={'state','tracking_health','tracking_tasks','tracking_gaps'}
CALL_CHANGES={'calls','tracking_wire','tracking_call_boundaries'}


class ArchiveUnavailable(ValueError):
    def __init__(self,epoch):
        self.epoch=epoch
        super().__init__('Quota projection version unavailable')


def _json_value(value):
    if isinstance(value,tuple):return {'$tuple':[_json_value(item) for item in value]}
    if isinstance(value,list):return [_json_value(item) for item in value]
    if isinstance(value,dict):
        if '$tuple' in value:raise ValueError('Reserved archive field')
        return {key:_json_value(item) for key,item in value.items()}
    if value is None or type(value) in (str,int,float,bool):return value
    raise TypeError(type(value).__name__)


def _object(value):
    if set(value)=={'$tuple'}:return tuple(value['$tuple'])
    return value


def _weight(value):
    # Reject oversized object graphs before constructing the JSON-compatible
    # copy. Each packet, the encoder buffer and the temporary write store have
    # independent bounds; a large period remains readable from the ledger.
    pending=[iter((value,))];weight=0
    while pending:
        item=next(pending[-1],pending)
        if item is pending:pending.pop();continue
        if isinstance(item,(list,tuple,dict)):
            if len(item)>MAX_ROWS:raise ValueError('Archive row budget')
            weight+=64+(32 if isinstance(item,dict) else 8)*len(item)
            pending.append(iter(item.values() if isinstance(item,dict) else item))
        elif isinstance(item,str):weight+=len(item)*4+64
        else:weight+=32
        if weight>MAX_DECODED_BYTES or len(pending)>64:raise ValueError('Archive object budget')
    return weight


def encode(value):
    _weight(value)
    raw=bytearray()
    for part in json.JSONEncoder(ensure_ascii=True,separators=(',',':')).iterencode(_json_value(value)):
        if len(raw)+len(part)>MAX_DECODED_BYTES:raise ValueError('Archive packet budget')
        raw.extend(part.encode('ascii'))
    payload=zlib.compress(raw,1)
    if len(payload)>MAX_PACKET_BYTES:raise ValueError('Archive packet budget')
    return payload


def decode(payload):
    if len(payload)>MAX_PACKET_BYTES:raise ValueError('Archive packet budget')
    decoder=zlib.decompressobj()
    raw=decoder.decompress(payload,MAX_DECODED_BYTES+1)
    if len(raw)>MAX_DECODED_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid archive packet')
    return json.loads(raw,object_hook=_object)


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS quota_archive_heads(
            epoch TEXT PRIMARY KEY,home TEXT,account TEXT,version INTEGER,
            cutoff REAL,at REAL,price TEXT,valid INTEGER,payload BLOB,digest TEXT);
        CREATE INDEX IF NOT EXISTS quota_archive_home ON quota_archive_heads(home);
        CREATE TABLE IF NOT EXISTS quota_archive_entries(
            epoch TEXT,kind TEXT,key TEXT,digest TEXT,PRIMARY KEY(epoch,kind,key));
        CREATE TABLE IF NOT EXISTS quota_archive_blobs(digest TEXT PRIMARY KEY,payload BLOB);
        CREATE TABLE IF NOT EXISTS quota_archive_repairs(
            epoch TEXT PRIMARY KEY,start REAL,end REAL,structural INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS quota_archive_scopes(
            epoch TEXT,home TEXT,PRIMARY KEY(epoch,home));
    ''')
    if 'digest' not in {row[1] for row in db.execute('PRAGMA table_info(quota_archive_heads)')}:
        db.execute('ALTER TABLE quota_archive_heads ADD COLUMN digest TEXT')
    owns=False
    def install(name,sql):
        nonlocal owns
        marker='archive_repair_v2_'+hashlib.sha256(sql.encode()).hexdigest()[:16]
        previous=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
        if previous is not None and marker in previous[0]:return
        if not db.in_transaction:db.execute('BEGIN IMMEDIATE');owns=True
        # Another opener may have installed the complete definition while this
        # connection waited for the writer lock.
        previous=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(name,)).fetchone()
        if previous is not None and marker in previous[0]:return
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
        db.execute(sql.replace('BEGIN','BEGIN /* '+marker+' */',1))
    # A dirty span survives change-ring wrap and reader exits. Its cardinality
    # follows the retained archive heads, never the number of corrected calls.
    for operation in ('INSERT','UPDATE'):
        install(f'quota_archive_changes_{operation.lower()}',f'''CREATE TRIGGER quota_archive_changes_{operation.lower()}
            AFTER {operation} ON report_changes BEGIN
            INSERT INTO quota_archive_repairs
            SELECT epoch,new.at,new.at,1 FROM quota_archive_heads
            WHERE new.table_name NOT IN ('state','tracking_health','tracking_tasks','tracking_gaps',
                'calls','tracking_wire','tracking_call_boundaries') AND (new.at IS NULL OR new.at<=cutoff)
            ON CONFLICT(epoch) DO UPDATE SET structural=1;
            UPDATE quota_archive_heads SET valid=0 WHERE valid=1
            AND new.table_name NOT IN ('state','tracking_health','tracking_tasks','tracking_gaps',
                'calls','tracking_wire','tracking_call_boundaries')
            AND (new.at IS NULL OR new.at<=cutoff); END''')
    # Loading and heartbeat changes do not revise completed historical costs.
    # Lost coverage or changed unresolved evidence does.
    install('quota_archive_state_update','''CREATE TRIGGER quota_archive_state_update AFTER UPDATE ON state
        WHEN old.unclassified IS NOT new.unclassified OR
             (old.complete_at IS NOT new.complete_at AND
              (new.complete_at IS NULL OR new.complete_at<old.complete_at)) BEGIN
        INSERT INTO quota_archive_repairs SELECT epoch,NULL,NULL,1 FROM quota_archive_heads WHERE 1
        ON CONFLICT(epoch) DO UPDATE SET structural=1;
        UPDATE quota_archive_heads SET valid=0 WHERE valid=1; END''')
    install('quota_archive_state_delete','''CREATE TRIGGER quota_archive_state_delete AFTER DELETE ON state BEGIN
        INSERT INTO quota_archive_repairs SELECT epoch,NULL,NULL,1 FROM quota_archive_heads WHERE 1
        ON CONFLICT(epoch) DO UPDATE SET structural=1;
        UPDATE quota_archive_heads SET valid=0 WHERE valid=1; END''')
    for table in sorted(CALL_CHANGES):
        for operation in ('INSERT','UPDATE','DELETE'):
            sides=('old','new') if operation=='UPDATE' else ('old',) if operation=='DELETE' else ('new',)
            homes=[f'{side}.home' for side in sides];times=[]
            key='response' if table=='tracking_wire' else 'uid'
            for side in sides:
                if table=='calls':times.append(f'SELECT {side}.ts AS stamp')
                elif table=='tracking_wire':times.extend((f'SELECT {side}.start AS stamp',f'SELECT coalesce({side}.end,h.cutoff)'))
                else:times.append(f'SELECT {side}.started AS stamp')
                # Include completion overrides and the replaced row before an
                # INSERT OR REPLACE removes its old identity or timestamp.
                times.extend((f'SELECT c.ts FROM quota_archive_scopes s CROSS JOIN calls c '
                    f'ON c.home=s.home AND c.uid={side}.{key} WHERE s.epoch=h.epoch',
                    f'SELECT w.start FROM quota_archive_scopes s CROSS JOIN tracking_wire w '
                    f'ON w.home=s.home AND w.response={side}.{key} WHERE s.epoch=h.epoch',
                    f'SELECT coalesce(w.end,h.cutoff) FROM quota_archive_scopes s CROSS JOIN tracking_wire w '
                    f'ON w.home=s.home AND w.response={side}.{key} WHERE s.epoch=h.epoch',
                    f'SELECT b.started FROM quota_archive_scopes s CROSS JOIN tracking_call_boundaries b '
                    f'ON b.home=s.home AND b.uid={side}.{key} WHERE s.epoch=h.epoch'))
            values=' UNION ALL '.join(times)
            lower=f'(SELECT min(stamp) FROM ({values}))'
            upper=f'(SELECT max(stamp) FROM ({values}))'
            scope='EXISTS(SELECT 1 FROM quota_archive_scopes s WHERE s.epoch=h.epoch)'
            matched=f'(NOT {scope} OR EXISTS(SELECT 1 FROM quota_archive_scopes s WHERE s.epoch=h.epoch AND s.home IN ({",".join(homes)})))'
            # A key-changing replacement can remove a destination row before
            # SQLite exposes it to an AFTER trigger. Keep those rare changes
            # conservative; ordinary cost and request corrections stay scoped.
            identity=('home','identity','response') if table=='tracking_wire' else ('home','uid')
            moved=' OR '.join(f'old.{column} IS NOT new.{column}' for column in identity) if operation=='UPDATE' else '0'
            replaced=("EXISTS(SELECT 1 FROM tracking_wire WHERE home=new.home AND identity=new.identity AND response IS NOT new.response)"
                      if table=='tracking_wire' and operation=='INSERT' else '0')
            structural=f'(NOT {scope} OR {moved} OR {replaced})'
            active='(h.valid=1 OR NOT EXISTS(SELECT 1 FROM quota_archive_repairs r WHERE r.epoch=h.epoch AND r.structural=1))'
            name=f'quota_archive_{table}_{operation.lower()}'
            timing='BEFORE' if operation in ('INSERT','UPDATE') else 'AFTER'
            columns={'calls':('home','uid','ts','model','input','cached','written','output','reasoning','cost','price_id','service_tier'),
                     'tracking_wire':('home','identity','response','start','end','status'),
                     'tracking_call_boundaries':('home','uid','started')}[table]
            changed=' AND ('+' OR '.join(f'old.{column} IS NOT new.{column}' for column in columns)+')' if operation=='UPDATE' else ''
            install(name,f'''CREATE TRIGGER {name} {timing} {operation} ON {table}
                WHEN EXISTS(SELECT 1 FROM quota_archive_heads h WHERE {active}){changed} BEGIN
                INSERT INTO quota_archive_repairs
                SELECT * FROM (
                    WITH bounds AS MATERIALIZED (
                        SELECT h.epoch,h.cutoff,CASE WHEN {structural} THEN NULL ELSE {lower} END AS start,
                            {structural} AS structural FROM quota_archive_heads h WHERE {active} AND {matched})
                    SELECT h.epoch,h.start,CASE WHEN h.structural THEN h.cutoff ELSE coalesce({upper},h.cutoff) END,
                        h.structural OR h.start IS NULL FROM bounds h
                    WHERE h.structural OR h.start IS NULL OR h.start<=h.cutoff) WHERE 1
                ON CONFLICT(epoch) DO UPDATE SET
                    start=CASE WHEN quota_archive_repairs.start IS NULL THEN excluded.start
                               WHEN excluded.start IS NULL THEN quota_archive_repairs.start
                               ELSE min(quota_archive_repairs.start,excluded.start) END,
                    end=CASE WHEN quota_archive_repairs.end IS NULL THEN excluded.end
                             WHEN excluded.end IS NULL THEN quota_archive_repairs.end
                             ELSE max(quota_archive_repairs.end,excluded.end) END,
                    structural=max(quota_archive_repairs.structural,excluded.structural)
                WHERE (quota_archive_repairs.start IS NULL AND excluded.start IS NOT NULL)
                   OR (quota_archive_repairs.end IS NULL AND excluded.end IS NOT NULL)
                   OR excluded.start<quota_archive_repairs.start OR excluded.end>quota_archive_repairs.end
                   OR excluded.structural>quota_archive_repairs.structural;
                UPDATE quota_archive_heads SET valid=0 WHERE valid=1 AND epoch IN
                    (SELECT epoch FROM quota_archive_repairs WHERE end IS NOT NULL); END''')
    # Older readers do not know the account-scope contract. Their existing
    # references must miss as well as rejecting newly published version 3 heads.
    if db.execute('SELECT 1 FROM quota_archive_heads WHERE version<>? AND valid=1 LIMIT 1',(VERSION,)).fetchone():
        if not db.in_transaction:db.execute('BEGIN IMMEDIATE');owns=True
        db.execute('UPDATE quota_archive_heads SET valid=0 WHERE version<>?',(VERSION,))
    if owns:db.commit()


def _thin(row):
    return {key:value for key,value in row.items() if key not in ('cost_rows','endpoints')}


def _scope_value(value):
    rows,diagnostics,deltas,count=value
    return ([_thin(row) for row in rows],diagnostics,deltas,count)


class ArchiveStatistics:
    def __init__(self,archive,current,live_ids):
        self.archive=archive;self.current=current;self.live_ids=live_ids
        self.entries=current.entries if current is not None else {}

    def scope(self,report,start,end,requests,cache,cache_readonly):
        from .quota_cycles import _quota_scope
        from .quota_exclusions import active_exclusions
        rows=[];diagnostics=[];deltas=[];count=0
        for cycle in report['cycles']:
            if start is not None and cycle['end']<start or end is not None and cycle['start']>end:continue
            saved=None if cycle['id'] in self.live_ids else self.archive.cycles.get(cycle['id'])
            def policy(source):
                account=cycle.get('account') or report.get('account')
                return ([r for r in active_exclusions(source,account) if r['start']<cycle['end'] and r['end']>cycle['start']],
                        [r for r in source.get('cycle_clears',[]) if r['account']==account and r['start']<cycle['end'] and r['end']>cycle['start']])
            if saved is None:
                source={**report,'cycles':[cycle],'_prepared_statistics':self.current}
                value=(self.current.scope(source,start,end,requests,cache,cache_readonly) if self.current is not None
                       else _quota_scope(source,start,end,requests,cache,cache_readonly))
            elif ((start is None or start<=cycle['start']) and (end is None or end>=cycle['end']) and
                  policy(report)==policy(self.archive.head['policy'])):
                value=saved['scope']
            else:
                from .quota_attribution import RequestWindows
                packet=self.archive.packet('cycle',cycle['id'])
                source={**report,'cycles':[packet['cycle']]}
                value=_scope_value(_quota_scope(source,start,end,RequestWindows(packet['requests'])))
            parts,attribution,delta,observed=value
            if saved is not None:
                parts=[{**row,'cost_complete_at':report.get('index_complete_at')} for row in parts]
            rows.extend(parts);diagnostics.extend(attribution);deltas.extend(delta);count+=observed
        return rows,diagnostics,deltas,count


class Archive:
    def __init__(self,ledger,row,head):
        self.ledger=ledger;self.epoch=row['epoch'];self.head=head
        self.cutoff=row['cutoff'];self.at=row['at'];self.account=row['account']
        self.cycles={item['cycle']['id']:item for item in head['cycles']}
        self.cache=OrderedDict();self.weight=0

    def packet(self,kind,key):
        identity=(kind,str(key))
        overrides=getattr(self,'overrides',None)
        if overrides is not None:
            row=overrides.execute('SELECT payload FROM packets WHERE kind=? AND key=?',identity).fetchone()
            if row is not None:return decode(row[0])
        cached=self.cache.pop(identity,None)
        if cached is not None:self.cache[identity]=cached;return decode(cached)
        row=self.ledger.db.execute('''SELECT b.digest,b.payload FROM quota_archive_entries e
            JOIN quota_archive_blobs b ON b.digest=e.digest
            WHERE e.epoch=? AND e.kind=? AND e.key=?''',(self.epoch,*identity)).fetchone()
        if row is None:raise ArchiveUnavailable(self.epoch)
        payload=row['payload']
        if hashlib.sha256(payload).hexdigest()!=row['digest']:raise ArchiveUnavailable(self.epoch)
        try:value=decode(payload)
        except (ValueError,TypeError,KeyError,RecursionError,zlib.error) as error:
            raise ArchiveUnavailable(self.epoch) from error
        if len(payload)<=DETAIL_CACHE_BYTES:
            while self.cache and self.weight+len(payload)>DETAIL_CACHE_BYTES:
                self.weight-=len(self.cache.popitem(last=False)[1])
            self.cache[identity]=payload;self.weight+=len(payload)
        return value

    def series(self,start,*,display=False):
        from .quota_view import _PeriodHistory,period_series
        value=self.packet('period',start)
        source=value['source'];source['report']['history']=self.history(source)
        restored=_PeriodHistory.__new__(_PeriodHistory)
        restored.report=source['report'];restored.end=source['end']
        restored.checkpoint=pickle.dumps(source['checkpoint'],protocol=5)
        times,flags,kind=source['classification']
        restored.classification=(times,bytes.fromhex(flags),kind)
        period=value['period'];period['_series_detail']['source']=restored
        period['_series_intervals']=period['_series_detail']['intervals']
        return period_series(period,display=display)

    def history(self,source):
        # Original observations already have a range index. Keep only their
        # replay checkpoint here, not a second persistent copy of every row.
        # Validate and read in one short snapshot so selected detail cannot mix
        # a displayed version with a corrected historical source.
        db=self.ledger.db;own=not db.in_transaction
        if own:db.execute('BEGIN')
        try:
            row=db.execute('SELECT valid FROM quota_archive_heads WHERE epoch=?',(self.epoch,)).fetchone()
            if row is None or not row[0]:raise ArchiveUnavailable(self.epoch)
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return self.ledger._report_history(self.head['policy']['home'],source['checkpoint'][0],source['end'],tables)
        finally:
            if own:db.rollback()

    def history_for(self,report,start):
        period=next((p for p in self.head['periods'] if p['start']<=start<=p['end']),None)
        if period is None:return report
        source=self.packet('period',period['start'])['source']
        return {**report,'history':self.history(source),
                '_history_identified':source['report']['_history_identified'],
                '_history_resume':source['checkpoint']}

    def compose(self,report):
        """Join immutable closed contributions with the freshly calculated tail."""
        from .quota_cycles import quota_statistics
        from .quota_view import summary_deadline
        view=report['view'];current_ids={cycle['id'] for cycle in report['cycles']}
        cycles=[*report['cycles'],*({**item['cycle'],'cost_complete_at':report.get('index_complete_at')}
            for item in self.head['cycles'] if item['cycle']['id'] not in current_ids)]
        current=view.get('_prepared_statistics')
        result={**report,'cycles':cycles,'history_pending':False,'_archive_prefix':self,
                '_prepared_statistics':ArchiveStatistics(self,current,current_ids)}
        periods=list(view['periods']);starts={period['start'] for period in periods}
        for saved in self.head['periods']:
            if saved['start'] not in starts:
                periods.append({**saved,'series':None,'_archive':self,
                                '_series_signature':bytes.fromhex(saved['_series_signature'])})
        periods.sort(key=lambda period:period['start'],reverse=True)
        lifetime=quota_statistics(result,include_mode_assumptions=True)
        summaries={None:lifetime};until={}
        for days in (7,30,90):
            start=report['at']-days*86400
            summaries[days]=quota_statistics(result,start,include_mode_assumptions=True)
            until[days]=summary_deadline(result,start)+days*86400
        result['view']={**view,'periods':periods,'overall':None,'lifetime':lifetime,'summaries':summaries,
            '_summary_until':until,'_prepared_statistics':result['_prepared_statistics'],
            'available_models':sorted({model['model'] for row in lifetime['intervals'] for model in row['models']
                                      if model.get('model') and not model.get('separate')})}
        return result


def repair(ledger,row,now):
    """Replace only closed cost packets; observation edits take the full path."""
    from .quota_cycles import _quota_scope,quota_value_history
    from .quota_attribution import RequestWindows
    from .quota_view import prepare_quota_view,clock
    from .pricing import usd
    db=ledger.db
    if db.in_transaction:return False
    dirty=db.execute('SELECT start,end,structural FROM quota_archive_repairs WHERE epoch=?',(row['epoch'],)).fetchone()
    if dirty is None or dirty['structural'] or dirty['start'] is None:return False
    current=ledger.current_report(row['home'],now,retain_projection=True)
    if current is None or current.get('account')!=row['account'] or current.get('_projection_start')>row['cutoff']:return False
    with closing(sqlite3.connect('')) as stage:
        stage.execute('PRAGMA cache_size=-1024')
        stage.execute('CREATE TABLE packets(kind TEXT,key TEXT,digest TEXT,payload BLOB,PRIMARY KEY(kind,key))')
        total=0
        def packet(kind,key,value):
            nonlocal total
            payload=encode(value);total+=len(payload)
            if total>MAX_BYTES:raise ValueError('Quota repair storage budget')
            stage.execute('INSERT INTO packets VALUES(?,?,?,?)',(kind,str(key),hashlib.sha256(payload).hexdigest(),payload))
        steps=0;interrupted=False;quantum=min(1000,max(1,MAX_REPAIR_SQL_STEPS))
        def progress():
            nonlocal steps,interrupted
            steps+=quantum
            if steps>=MAX_REPAIR_SQL_STEPS and not interrupted:
                interrupted=True
                return True
            return False
        # Admission bounds retained Python rows. A separate VM-step limit also
        # stops SQLite candidate unions and sorting before they return a row.
        db.set_progress_handler(progress,quantum)
        try:
            db.execute('BEGIN')
            if current['_projection_revision']!=db.execute('SELECT value FROM report_revision WHERE id=1').fetchone()[0]:return False
            dirty=db.execute('SELECT start,end,structural FROM quota_archive_repairs WHERE epoch=?',(row['epoch'],)).fetchone()
            if dirty is None or dirty['structural'] or dirty['start'] is None:return False
            stored=db.execute('SELECT CASE WHEN length(payload)<=? THEN payload END,digest FROM quota_archive_heads WHERE epoch=?',
                              (MAX_PACKET_BYTES,row['epoch'])).fetchone()
            if stored is None or stored[0] is None or hashlib.sha256(stored[0]).hexdigest()!=stored[1]:return False
            archive=Archive(ledger,row,decode(stored[0]));archive.overrides=stage
            policy=archive.head['policy'];scope=policy.get('homes')
            tracking=policy.get('tracking')
            if not tracking or not scope or len(scope)>MAX_SCOPE_HOMES:return False
            if sorted((current.get('tracking') or {}).get('homes',[]))!=scope:return False
            context={**current,'exclusions':policy['exclusions'],'cycle_clears':policy['cycle_clears']}
            context.pop('_prepared_statistics',None)
            affected=[]
            for saved in archive.head['cycles']:
                header=saved['cycle']
                if header['cycle_end']<dirty['start'] or header['cycle_start']>dirty['end']:continue
                before=archive.packet('cycle',header['id'])['cycle']
                calls,times,requests,pending=ledger.archive_calls(scope,tracking['started'],before['cycle_start'],before['cycle_end'])
                after=ledger._forward_cycle_costs({**before,'cost_complete_at':current.get('index_complete_at')},calls,times,pending)
                windows=RequestWindows(requests)
                saved['scope']=_scope_value(_quota_scope({**context,'cycles':[after]},None,None,windows))
                packet('cycle',header['id'],dict(cycle=after,requests=list(windows.overlapping(after['start'],after['end']))))
                affected.append(header['id'])
                del calls,times,requests,pending,before,after,windows
            periods=sorted(archive.head['periods'],key=lambda period:period['start'])
            changed_periods=[]
            for position,header in enumerate(periods):
                following=periods[position+1]['start'] if position+1<len(periods) else archive.cutoff
                if following<dirty['start'] or header['start']>dirty['end']:continue
                original=archive.packet('period',header['start']);source=original['source']
                cycles=[];weight=0
                for saved in archive.head['cycles']:
                    value=saved['cycle']
                    if value['end']<header['start'] or value['start']>=following:continue
                    value=archive.packet('cycle',value['id'])['cycle'];weight+=_weight(value)
                    if weight>MAX_DECODED_BYTES:raise ValueError('Quota repair period budget')
                    cycles.append(value)
                calls,times,requests,pending=ledger.archive_calls(scope,tracking['started'],header['start'],following)
                del calls,times,pending
                if weight+_weight(list(requests))>MAX_DECODED_BYTES:raise ValueError('Quota repair period budget')
                group=dict(start=header['start'],reset_kind=header['reset_kind'],partial=header['partial'],rows=None,
                           _source=None,_last_at=header['end'],_matches_account=True,_times=source['classification'][0])
                sentinel=dict(start=following,reset_kind=None,partial=True,rows=None,_source=None,
                              _last_at=following,_matches_account=False,_times=[])
                cache={};details=[]
                quota_value_history({**context,'cycles':cycles},None,_requests=RequestWindows(requests),_cache=cache,
                    _groups=(group,sentinel),_defer_rows=True,
                    _period_sink=lambda detail,period,unchanged:details.append((detail,period)) if period is not None else None)
                detail,period=details[0]
                detail.pop('source',None);period.pop('cost_intervals',None)
                period.update(label=f"{clock(period['start'])} → {clock(period['end'])}: {usd(period['value'])} (종료)",
                              _series_signature=cache[period['start']][0].hex())
                header.clear();header.update(period)
                value={**period,'series':None,'_series_detail':detail,
                       '_series_exclusions':original['period']['_series_exclusions']}
                packet('period',period['start'],dict(period=value,source=source))
                changed_periods.append(period['start'])
                del cycles,requests,original,source,detail,period,details
            current['view']=prepare_quota_view(current,deferred=True)
            composed=archive.compose(current)
            composed['_archive_repair_revision']=current['_projection_revision']
            db.rollback()
            saved=save(ledger,composed,composed['view'])
            if saved:ledger._archive_repair=dict(cycles=affected,periods=changed_periods)
            return saved
        except (ValueError,TypeError,KeyError,IndexError,OverflowError,RecursionError,zlib.error,sqlite3.OperationalError):
            return False
        finally:
            db.set_progress_handler(None,0)
            if db.in_transaction:db.rollback()


def load(ledger,home,now,previous=None,*,allow_repair=True):
    from .quota_cycles import PRICE_ID
    row=ledger.db.execute('''SELECT epoch,home,account,version,cutoff,at,price,valid,digest
        FROM quota_archive_heads WHERE home=? ORDER BY rowid DESC LIMIT 1''',(home,)).fetchone()
    if row is None or row['version']!=VERSION or row['price']!=PRICE_ID or row['at']>now:return None
    if not row['valid']:
        if allow_repair and repair(ledger,row,now):return load(ledger,home,now,allow_repair=False)
        return None
    if previous is not None and previous.epoch==row['epoch']:return previous
    payload=ledger.db.execute('SELECT CASE WHEN length(payload)<=? THEN payload END FROM quota_archive_heads WHERE epoch=?',
                             (MAX_PACKET_BYTES,row['epoch'])).fetchone()
    payload=payload[0] if payload is not None else None
    if payload is None or hashlib.sha256(payload).hexdigest()!=row['digest']:
        ledger._archive_invalid=row['epoch'];return None
    try:return Archive(ledger,row,decode(payload))
    except (ValueError,TypeError,KeyError,RecursionError,zlib.error):
        ledger._archive_invalid=row['epoch'];return None


def save(ledger,report,view):
    """Write closed packets once; encoding never holds the ledger writer lock."""
    from .quota_cycles import PRICE_ID
    from .quota_attribution import RequestWindows
    periods=view.get('periods',[])
    if report.get('history_pending') or not periods or len(periods)>MAX_PERIODS:return False
    current=next((period for period in periods if period['current']),None)
    if current is None:return False
    cutoff=current['start'];home=report['home']
    old=ledger.db.execute('SELECT epoch,cutoff,valid,version,price FROM quota_archive_heads WHERE home=? ORDER BY rowid DESC LIMIT 1',(home,)).fetchone()
    if (old and old['valid'] and old['cutoff']==cutoff and old['version']==VERSION and old['price']==PRICE_ID and
            getattr(ledger,'_archive_invalid',None)!=old['epoch']):return False
    prepared=view.get('_prepared_statistics')
    if prepared is None or not hasattr(prepared,'entries'):return False
    revision=report.get('_projection_revision',getattr(ledger,'_input_revision',None))
    coverage=report.get('_projection_coverage')
    if revision is None or coverage is None or ledger.db.in_transaction:return False
    closed=[cycle for cycle in report['cycles'] if cycle['end']<cutoff]
    if any(cycle.get('cost_complete_at') is None or cycle['cost_complete_at']<cycle['end'] for cycle in closed):return False
    windows=RequestWindows(report.get('request_windows',[]))
    prefix=report.get('_archive_prefix')
    scope=sorted(coverage)
    track=report.get('tracking')
    policy={key:report.get(key) for key in ('home','account','exclusions','cycle_clears')}
    policy.update(homes=scope,tracking=({key:track.get(key) for key in ('started','complete','homes')} if track is not None else None))
    head=dict(cycles=[],periods=[],policy=policy);total=0
    # A private SQLite temporary database bounds encoded packet memory and
    # cleans up on close. It never owns authoritative records or acknowledgments.
    with closing(sqlite3.connect('')) as stage:
        stage.execute('PRAGMA cache_size=-1024')
        stage.execute('CREATE TABLE packets(kind TEXT,key TEXT,digest TEXT,payload BLOB)')
        def packet(kind,key,value):
            nonlocal total
            payload=encode(value);total+=len(payload)
            if total>MAX_BYTES:raise ValueError('Archive storage budget')
            stage.execute('INSERT INTO packets VALUES(?,?,?,?)',(kind,str(key),hashlib.sha256(payload).hexdigest(),payload))
        def reuse(kind,key):
            nonlocal total
            overrides=getattr(prefix,'overrides',None)
            if overrides is not None:
                changed=overrides.execute('SELECT digest,payload FROM packets WHERE kind=? AND key=?',(kind,str(key))).fetchone()
                if changed is not None:
                    total+=len(changed[1])
                    if total>MAX_BYTES:raise ValueError('Archive storage budget')
                    stage.execute('INSERT INTO packets VALUES(?,?,?,?)',(kind,str(key),*changed))
                    return
            row=ledger.db.execute('SELECT e.digest,length(b.payload) FROM quota_archive_entries e '
                'JOIN quota_archive_blobs b ON b.digest=e.digest WHERE e.epoch=? AND e.kind=? AND e.key=?',
                (prefix.epoch,kind,str(key))).fetchone()
            if row is None:raise ValueError('Quota projection version expired')
            total+=row[1]
            if total>MAX_BYTES:raise ValueError('Archive storage budget')
            stage.execute('INSERT INTO packets VALUES(?,?,?,NULL)',(kind,str(key),row[0]))
        try:
            for cycle in closed:
                if prefix is not None and cycle['id'] not in prepared.live_ids:
                    head['cycles'].append(prefix.cycles[cycle['id']]);reuse('cycle',cycle['id']);continue
                entry=prepared.entries.get(cycle['id'])
                if entry is None:return False
                header={key:cycle[key] for key in ('id','start','end','cycle_start','cycle_end','cost_complete_at','account') if key in cycle}
                head['cycles'].append(dict(cycle=header,scope=_scope_value(entry[1])))
                packet('cycle',cycle['id'],dict(cycle=cycle,requests=list(windows.overlapping(cycle['start'],cycle['end']))))
            for period in periods:
                if period['start']>=cutoff:continue
                if period.get('_archive') is not None:
                    head['periods'].append(next(p for p in prefix.head['periods'] if p['start']==period['start']))
                    reuse('period',period['start']);continue
                detail=period.get('_series_detail');source=detail.get('source') if detail else None
                if source is None:return False
                times,flags,kind=source.classification
                account=source.report.get('account')
                raw=dict(report={**{key:value for key,value in source.report.items() if key!='history'},
                    '_history_identified':bool(account and any(row.get('account')==account and row['window']=='weekly' for row in source.report['history']))},end=source.end,
                    checkpoint=pickle.loads(source.checkpoint),classification=(list(times),flags.hex(),kind))
                header={key:value for key,value in period.items() if key!='series' and not key.startswith('_')}
                header.update(_series_signature=period['_series_signature'].hex(),_has_history=period['_has_history'])
                head['periods'].append(header)
                value={**header,'series':None,'_series_detail':{key:value for key,value in detail.items() if key!='source'},
                       '_series_exclusions':period['_series_exclusions']}
                packet('period',period['start'],dict(period=value,source=raw))
            payload=encode(head);total+=len(payload)
            if total>MAX_BYTES:return False
        except (ValueError,TypeError,OverflowError):return False
        db=ledger.db;timeout=db.execute('PRAGMA busy_timeout').fetchone()[0]
        db.execute('PRAGMA busy_timeout=0')
        try:
            db.execute('BEGIN IMMEDIATE')
            slots=','.join('?' for _ in coverage)
            states={row['home']:(row['unclassified'],row['complete_at']) for row in
                    db.execute(f'SELECT home,unclassified,complete_at FROM state WHERE home IN ({slots})',tuple(coverage))}
            if states.keys()!=coverage.keys() or any(
                    states[home][0]!=before[0] or before[1] is not None and
                    (states[home][1] is None or states[home][1]<before[1]) for home,before in coverage.items()):return False
            latest=db.execute('SELECT value FROM report_revision WHERE id=1').fetchone()[0]
            if report.get('_archive_repair_revision') is not None and latest!=report['_archive_repair_revision']:return False
            if latest!=revision:
                changes=list(db.execute('SELECT table_name,at FROM report_changes WHERE revision>?',(revision,)))
                if latest-revision!=len(changes) or any(table not in IGNORED_CHANGES and (at is None or at<=cutoff) for table,at in changes):return False
            epoch=uuid4().hex
            db.execute('INSERT INTO quota_archive_heads VALUES(?,?,?,?,?,?,?,1,?,?)',
                       (epoch,home,report.get('account'),VERSION,cutoff,report['at'],PRICE_ID,payload,hashlib.sha256(payload).hexdigest()))
            db.execute('INSERT INTO quota_archive_repairs VALUES(?,NULL,NULL,?)',(epoch,int(len(scope)>MAX_SCOPE_HOMES or track is None)))
            if len(scope)<=MAX_SCOPE_HOMES:
                db.executemany('INSERT INTO quota_archive_scopes VALUES(?,?)',((epoch,item) for item in scope))
            for kind,key,digest,body in stage.execute('SELECT * FROM packets'):
                if body is not None:db.execute('INSERT OR IGNORE INTO quota_archive_blobs VALUES(?,?)',(digest,body))
                elif not db.execute('SELECT 1 FROM quota_archive_blobs WHERE digest=?',(digest,)).fetchone():return False
                db.execute('INSERT INTO quota_archive_entries VALUES(?,?,?,?)',(epoch,kind,key,digest))
            db.execute('DELETE FROM quota_archive_heads WHERE home=? AND epoch NOT IN '
                       '(SELECT epoch FROM quota_archive_heads WHERE home=? ORDER BY rowid DESC LIMIT 2)',(home,home))
            db.execute('DELETE FROM quota_archive_heads WHERE home NOT IN '
                       '(SELECT home FROM quota_archive_heads GROUP BY home ORDER BY max(rowid) DESC LIMIT ?)',(MAX_HOMES,))
            db.execute('DELETE FROM quota_archive_entries WHERE epoch NOT IN (SELECT epoch FROM quota_archive_heads)')
            db.execute('DELETE FROM quota_archive_repairs WHERE epoch NOT IN (SELECT epoch FROM quota_archive_heads)')
            db.execute('DELETE FROM quota_archive_scopes WHERE epoch NOT IN (SELECT epoch FROM quota_archive_heads)')
            db.execute('DELETE FROM quota_archive_blobs WHERE digest NOT IN (SELECT digest FROM quota_archive_entries)')
            db.commit();ledger._archive_invalid=None;return True
        finally:
            if db.in_transaction:db.rollback()
            db.execute(f'PRAGMA busy_timeout={timeout}')
