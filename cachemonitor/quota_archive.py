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

VERSION=1
MAX_BYTES=64*1024*1024
MAX_PACKET_BYTES=8*1024*1024
MAX_DECODED_BYTES=32*1024*1024
MAX_ROWS=65536
MAX_PERIODS=4096
MAX_HOMES=4
DETAIL_CACHE_BYTES=16*1024*1024
IGNORED_CHANGES={'state','tracking_health','tracking_tasks','tracking_gaps'}


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


def encode(value):
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
            cutoff REAL,at REAL,price TEXT,valid INTEGER,payload BLOB);
        CREATE INDEX IF NOT EXISTS quota_archive_home ON quota_archive_heads(home);
        CREATE TABLE IF NOT EXISTS quota_archive_entries(
            epoch TEXT,kind TEXT,key TEXT,digest TEXT,PRIMARY KEY(epoch,kind,key));
        CREATE TABLE IF NOT EXISTS quota_archive_blobs(digest TEXT PRIMARY KEY,payload BLOB);
    ''')
    # The bounded change ring is also a notification stream. Dirty prefixes
    # remain dirty after the ring wraps or the reader exits.
    for operation in ('INSERT','UPDATE'):
        db.execute(f'''CREATE TRIGGER IF NOT EXISTS quota_archive_changes_{operation.lower()}
            AFTER {operation} ON report_changes BEGIN
            UPDATE quota_archive_heads SET valid=0 WHERE valid=1
            AND new.table_name NOT IN ('state','tracking_health','tracking_tasks','tracking_gaps')
            AND (new.at IS NULL OR new.at<=cutoff); END''')
    # Loading and heartbeat changes do not revise completed historical costs.
    # Lost coverage or changed unresolved evidence does.
    db.execute('''CREATE TRIGGER IF NOT EXISTS quota_archive_state_update AFTER UPDATE ON state
        WHEN old.unclassified IS NOT new.unclassified OR
             (old.complete_at IS NOT new.complete_at AND
              (new.complete_at IS NULL OR new.complete_at<old.complete_at)) BEGIN
        UPDATE quota_archive_heads SET valid=0 WHERE valid=1; END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS quota_archive_state_delete AFTER DELETE ON state BEGIN
        UPDATE quota_archive_heads SET valid=0 WHERE valid=1; END''')


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
        from .quota_cycles import _PackedHistory
        from .quota_view import _PeriodHistory,period_series
        value=self.packet('period',start)
        source=value['source'];source['report']['history']=_PackedHistory(source['report']['history'])
        restored=_PeriodHistory.__new__(_PeriodHistory)
        restored.report=source['report'];restored.end=source['end']
        restored.checkpoint=pickle.dumps(source['checkpoint'],protocol=5)
        times,flags,kind=source['classification']
        restored.classification=(times,bytes.fromhex(flags),kind)
        period=value['period'];period['_series_detail']['source']=restored
        return period_series(period,display=display)

    def history_for(self,report,start):
        from .quota_cycles import _PackedHistory
        period=next((p for p in self.head['periods'] if p['start']<=start<=p['end']),None)
        if period is None:return report
        source=self.packet('period',period['start'])['source']
        return {**report,'history':_PackedHistory(source['report']['history']),
                '_history_resume':source['checkpoint']}

    def compose(self,report):
        """Join immutable closed contributions with the freshly calculated tail."""
        from .quota_cycles import quota_statistics
        from .quota_view import summary_deadline
        view=report['view'];current_ids={cycle['id'] for cycle in report['cycles']}
        cycles=[*report['cycles'],*(item['cycle'] for item in self.head['cycles'] if item['cycle']['id'] not in current_ids)]
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


def load(ledger,home,now,previous=None):
    from .quota_cycles import PRICE_ID
    row=ledger.db.execute('SELECT * FROM quota_archive_heads WHERE home=? ORDER BY rowid DESC LIMIT 1',(home,)).fetchone()
    if row is None or not row['valid'] or row['version']!=VERSION or row['price']!=PRICE_ID or row['at']>now:return None
    if previous is not None and previous.epoch==row['epoch']:return previous
    try:return Archive(ledger,row,decode(row['payload']))
    except (ValueError,TypeError,KeyError,RecursionError,zlib.error):return None


def save(ledger,report,view):
    """Write closed packets once; encoding never holds the ledger writer lock."""
    from .quota_cycles import PRICE_ID
    from .quota_attribution import RequestWindows
    periods=view.get('periods',[])
    if report.get('history_pending') or not periods or len(periods)>MAX_PERIODS:return False
    current=next((period for period in periods if period['current']),None)
    if current is None:return False
    cutoff=current['start'];home=report['home']
    old=ledger.db.execute('SELECT cutoff,valid FROM quota_archive_heads WHERE home=? ORDER BY rowid DESC LIMIT 1',(home,)).fetchone()
    if old and old['valid'] and old['cutoff']==cutoff:return False
    prepared=view.get('_prepared_statistics')
    if prepared is None or not hasattr(prepared,'entries'):return False
    revision=report.get('_projection_revision',getattr(ledger,'_input_revision',None))
    if revision is None or ledger.db.in_transaction:return False
    closed=[cycle for cycle in report['cycles'] if cycle['end']<cutoff]
    if any(cycle.get('cost_complete_at') is None or cycle['cost_complete_at']<cycle['end'] for cycle in closed):return False
    windows=RequestWindows(report.get('request_windows',[]))
    prefix=report.get('_archive_prefix')
    head=dict(cycles=[],periods=[],policy={key:report.get(key) for key in ('home','account','exclusions','cycle_clears')});total=0
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
                if source is None or len(source.report['history'])>MAX_ROWS:return False
                times,flags,kind=source.classification
                raw=dict(report={**source.report,'history':list(source.report['history'])},end=source.end,
                    checkpoint=pickle.loads(source.checkpoint),classification=(list(times),flags.hex(),kind))
                header={key:value for key,value in period.items() if key!='series' and not key.startswith('_')}
                header.update(_series_signature=period['_series_signature'].hex(),_has_history=period['_has_history'])
                head['periods'].append(header)
                value={**header,'series':None,'_series_detail':{key:value for key,value in detail.items() if key!='source'},
                       '_series_intervals':period['_series_intervals'],'_series_exclusions':period['_series_exclusions']}
                packet('period',period['start'],dict(period=value,source=raw))
            payload=encode(head);total+=len(payload)
            if total>MAX_BYTES:return False
        except (ValueError,TypeError,OverflowError):return False
        db=ledger.db;timeout=db.execute('PRAGMA busy_timeout').fetchone()[0]
        db.execute('PRAGMA busy_timeout=0')
        try:
            db.execute('BEGIN IMMEDIATE')
            latest=db.execute('SELECT value FROM report_revision WHERE id=1').fetchone()[0]
            if latest!=revision:
                changes=list(db.execute('SELECT table_name,at FROM report_changes WHERE revision>?',(revision,)))
                if latest-revision!=len(changes) or any(table not in IGNORED_CHANGES and (at is None or at<=cutoff) for table,at in changes):return False
            epoch=uuid4().hex
            db.execute('INSERT INTO quota_archive_heads VALUES(?,?,?,?,?,?,?,1,?)',
                       (epoch,home,report.get('account'),VERSION,cutoff,report['at'],PRICE_ID,payload))
            for kind,key,digest,body in stage.execute('SELECT * FROM packets'):
                if body is not None:db.execute('INSERT OR IGNORE INTO quota_archive_blobs VALUES(?,?)',(digest,body))
                elif not db.execute('SELECT 1 FROM quota_archive_blobs WHERE digest=?',(digest,)).fetchone():return False
                db.execute('INSERT INTO quota_archive_entries VALUES(?,?,?,?)',(epoch,kind,key,digest))
            db.execute('DELETE FROM quota_archive_heads WHERE home=? AND epoch NOT IN '
                       '(SELECT epoch FROM quota_archive_heads WHERE home=? ORDER BY rowid DESC LIMIT 2)',(home,home))
            db.execute('DELETE FROM quota_archive_heads WHERE home NOT IN '
                       '(SELECT home FROM quota_archive_heads GROUP BY home ORDER BY max(rowid) DESC LIMIT ?)',(MAX_HOMES,))
            db.execute('DELETE FROM quota_archive_entries WHERE epoch NOT IN (SELECT epoch FROM quota_archive_heads)')
            db.execute('DELETE FROM quota_archive_blobs WHERE digest NOT IN (SELECT digest FROM quota_archive_entries)')
            db.commit();return True
        finally:
            if db.in_transaction:db.rollback()
            db.execute(f'PRAGMA busy_timeout={timeout}')
