"""Durable forward tracking inputs. No historical quota is imported here."""
import json
import time
import os
from dataclasses import replace
from . import quota_payloads

POLICY_VERSION = "continuous-account-v2"
REPLAY_CACHE_BYTES = 64*1024*1024
from .quota_tracking import Observation, TrackingState


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS tracking_config(home TEXT PRIMARY KEY, started REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tracking_policies(home TEXT, version TEXT, started REAL NOT NULL,
            PRIMARY KEY(home,version));
        CREATE TABLE IF NOT EXISTS tracking_call_boundaries(home TEXT, uid TEXT, started REAL,
            PRIMARY KEY(home,uid));
        CREATE TABLE IF NOT EXISTS tracking_controls(home TEXT, at REAL, enabled INTEGER,
            PRIMARY KEY(home,at));
        CREATE TABLE IF NOT EXISTS tracking_tasks(home TEXT, identity TEXT, start REAL, end REAL,
            PRIMARY KEY(home,identity));
        CREATE INDEX IF NOT EXISTS tracking_tasks_time ON tracking_tasks(home,start,end);
        CREATE TABLE IF NOT EXISTS tracking_health(home TEXT PRIMARY KEY, at REAL, complete INTEGER);
        CREATE TABLE IF NOT EXISTS tracking_observations(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, home TEXT, requested REAL, received REAL, data TEXT);
        CREATE INDEX IF NOT EXISTS tracking_observations_time ON tracking_observations(home,received);
        CREATE TABLE IF NOT EXISTS tracking_gaps(home TEXT, start REAL, end REAL,
            PRIMARY KEY(home,start));
        CREATE TABLE IF NOT EXISTS tracking_wire(home TEXT, identity TEXT, response TEXT,
            start REAL, end REAL, status TEXT, PRIMARY KEY(home,identity));
        -- Current activity and its revision counters are authoritative. The old
        -- append-only journal has no reader and must not grow with every refresh.
        DROP TABLE IF EXISTS tracking_activity_versions;
    ''')
    quota_payloads.initialize(db)
    _initialize_activity_revisions(db)


def _initialize_activity_revisions(db):
    statements = ['''CREATE TABLE IF NOT EXISTS tracking_activity_revisions(
        home TEXT, kind TEXT, revision INTEGER NOT NULL, PRIMARY KEY(home,kind));''']
    for kind, table in (('task', 'tracking_tasks'), ('wire', 'tracking_wire')):
        def bump(home, condition=''):
            return f'''INSERT INTO tracking_activity_revisions(home,kind,revision)
                SELECT {home},'{kind}',1 WHERE {condition or '1'}
                ON CONFLICT(home,kind) DO UPDATE SET revision=revision+1;'''
        statements.extend((
            f'''CREATE INDEX IF NOT EXISTS {table}_open ON {table}(home,start) WHERE end IS NULL;''',
            f'''CREATE TRIGGER IF NOT EXISTS {table}_activity_insert AFTER INSERT ON {table}
                BEGIN {bump('NEW.home')} END;''',
            f'''CREATE TRIGGER IF NOT EXISTS {table}_activity_delete AFTER DELETE ON {table}
                BEGIN {bump('OLD.home')} END;''',
            f'''CREATE TRIGGER IF NOT EXISTS {table}_activity_update AFTER UPDATE OF home,start,end ON {table}
                WHEN OLD.home IS NOT NEW.home OR OLD.start IS NOT NEW.start OR OLD.end IS NOT NEW.end
                BEGIN {bump('OLD.home')} {bump('NEW.home', 'NEW.home IS NOT OLD.home')} END;'''))
    db.executescript('\n'.join(statements))


class ActivitySummary:
    """Cache one home's lifetime aggregates; evaluate recent activity at each read."""
    def __init__(self):
        self.db = self.home = None
        self.cached = {}

    def read(self, db, home, since):
        if db is not self.db or home != self.home:
            self.db, self.home = db, home
            self.cached.clear()
        own_transaction = not db.in_transaction
        if own_transaction:
            db.execute('BEGIN')
        try:
            revisions = dict(db.execute(
                'select kind,revision from tracking_activity_revisions where home=?', (home,)))
            result = []
            for kind, table in (('task', 'tracking_tasks'), ('wire', 'tracking_wire')):
                revision = revisions.get(kind, 0)
                cached = self.cached.get(kind)
                if cached is None or cached[0] != revision:
                    summary = tuple(db.execute(
                        f'select count(*),max(start),max(end) from {table} where home=?', (home,)).fetchone())
                    # Never retain uncommitted revisions: rollback can reuse their numbers.
                    if own_transaction:
                        self.cached[kind] = revision, summary
                else:
                    summary = cached[1]
                recent = db.execute(f'select count(*) from {table} where home=? and end is null and start>=?',
                                    (home, since)).fetchone()[0] if summary[1] is not None or summary[2] is not None else None
                result.extend((*summary, recent))
            return tuple(result)
        finally:
            if own_transaction:
                db.rollback()


def enable(db, home, at=None, enabled=True):
    at = time.time() if at is None else at
    policy = db.execute('select started from tracking_policies where home=? and version=?',
                        (home, POLICY_VERSION)).fetchone()
    if policy is None:
        old = db.execute('select started from tracking_config where home=?', (home,)).fetchone()
        if old:
            db.execute('insert or ignore into tracking_policies values(?,?,?)', (home, 'legacy', old[0]))
        db.execute('insert into tracking_policies values(?,?,?)', (home, POLICY_VERSION, at))
        db.execute('insert or replace into tracking_config values(?,?)', (home, at))
        # Start this policy at this real activation instant, even when the
        # previous policy had monitoring ON. Keep every old observation intact.
        db.execute('insert or replace into tracking_controls values(?,?,?)', (home, at, int(enabled)))
    previous = db.execute('select enabled from tracking_controls where home=? order by at desc limit 1', (home,)).fetchone()
    if previous is None or bool(previous[0]) != enabled:
        db.execute('insert or replace into tracking_controls values(?,?,?)', (home, at, int(enabled)))
    db.commit()


def sync_activity(db, snapshot, cache=None):
    cache = {} if cache is None else cache
    if not db.execute("select 1 from sqlite_master where name='tracking_config'").fetchone():
        return
    for home in snapshot.get('homes', []):
        enabled = db.execute('select started from tracking_config where home=?', (home,)).fetchone()
        if not enabled:
            continue
        control=db.execute('select enabled from tracking_controls where home=? order by at desc limit 1',(home,)).fetchone()
        if control is not None and not control[0]:
            continue
        complete = snapshot.get('usage_collection_complete', snapshot.get('index', {}).get('usage_complete', False))
        previous = db.execute('select * from tracking_health where home=?', (home,)).fetchone()
        if previous and snapshot['ts']-previous['at'] > 15:
            db.execute('insert or replace into tracking_gaps values(?,?,?)', (home, previous['at'], snapshot['ts']))
        db.execute('''insert into tracking_health values(?,?,?) on conflict(home) do update set
            at=excluded.at,complete=excluded.complete''', (home, snapshot['ts'], int(complete)))
        for session in snapshot.get('sessions', []):
            if session['home'] != home:
                continue
            cache_key=('turns',home,session['id'])
            records=session.get('turn_records', {})
            token=('revision',session['request_revision']) if session.get('request_revision') is not None else records
            if cache.get(cache_key)==token:
                continue
            if session.get('request_revision') is not None:cache[cache_key]=token
            elif len(records)<=4096:cache[cache_key]={key:dict(value) for key,value in records.items()}
            while len(cache)>4096:cache.pop(next(iter(cache)))
            for turn, record in session.get('turn_records', {}).items():
                start, end = record.get('started_at'), record.get('ended_at')
                if start is None or (end is not None and end < enabled[0]):
                    continue
                if start < enabled[0] and end is None:
                    continue
                identity=session['id'] + ':' + turn
                db.execute('insert into tracking_tasks values(?,?,?,?) on conflict(home,identity) '
                           'do update set start=excluded.start,end=excluded.end '
                           'where tracking_tasks.start is not excluded.start or tracking_tasks.end is not excluded.end',
                           (home, session['id'] + ':' + turn, start, end))
        for request in snapshot.get('request_activity', []):
            if os.path.normcase(request['home']) != os.path.normcase(home):
                continue
            if request.get('request_observed_at') is None:
                continue  # Response-only evidence does not establish a local in-flight request.
            cache_key=('wire',home,request['attempt'])
            if cache.get(cache_key)==request:
                continue
            cache[cache_key]=dict(request)
            while len(cache)>4096:cache.pop(next(iter(cache)))
            start = request.get('request_observed_at') or request['ts']
            end = request.get('completed_observed_at')
            terminal = request['status'] in ('completed','failed','incomplete','disconnected','http_error','unparsed','cancelled')
            if start < enabled[0] and end is None:
                continue  # Pre-activation pending records do not prove a request is still running.
            # Older observers only timestamp successful completion. Preserve
            # uncertainty instead of using the original request timestamp as end.
            if terminal and end is None:
                end = snapshot['ts']
            if end is not None and end < enabled[0]:
                continue
            old = db.execute('select * from tracking_wire where home=? and identity=?', (home,request['attempt'])).fetchone()
            if old and old['end'] is not None and not request.get('completed_observed_at'):
                end = old['end']
            if old is not None and (old['response'],old['start'],old['end'],old['status'])==(
                request.get('response_id',''),start,end,request['status']):continue
            db.execute('insert or replace into tracking_wire values(?,?,?,?,?,?)',
                (home,request['attempt'],request.get('response_id',''),start,end,request['status']))


def observe(db, home, quota):
    if quota.get('source') != 'live':
        return
    requested=quota.get('requested_at',quota['observed_at'])
    # A buffered observation can be persisted after a later OFF/ON change.
    control=db.execute('select enabled,at from tracking_controls where home=? and at<=? '
                       'order by at desc limit 1',(home,requested)).fetchone()
    if control is None or not control[0] or requested < control[1]:
        return
    if db.execute('select 1 from tracking_observations where home=? and received=? and requested=?',
                  (home,quota['observed_at'],requested)).fetchone():
        return
    db.execute('update tracking_gaps set end=? where home=? and end is null',(requested,home))
    elapsed=quota.get('elapsed')
    if elapsed is not None and abs((quota['observed_at']-requested)-elapsed)>1:
        db.execute('insert or replace into tracking_gaps values(?,?,?)',
                   (home,min(requested,quota['observed_at']),max(requested,quota['observed_at'])))
        db.commit()
        return
    data, context = quota_payloads.encode(db, quota, requested, quota['observed_at'])
    db.execute('insert into tracking_observations(home,requested,received,data,payload_context) values(?,?,?,?,?)',
               (home, requested, quota['observed_at'], data, context))
    db.commit()


def failed_observation(db, home, at):
    if not db.execute('select 1 from tracking_gaps where home=? and end is null',(home,)).fetchone():
        last=db.execute('select received from tracking_observations where home=? and requested >= coalesce((select started from tracking_config where home=?),?) order by seq desc limit 1',(home,home,at)).fetchone()
        db.execute('insert or ignore into tracking_gaps values(?,?,NULL)',(home,last[0] if last else at))
        db.commit()


def build(db, home, now, *, context_only=False, read_rows=None, pack_groups=False,
          replay_cache=None, changes=None):
    config = db.execute('select started from tracking_config where home=?', (home,)).fetchone()
    if not config:
        return None
    policy = db.execute('select started from tracking_policies where home=? and version=?', (home, POLICY_VERSION)).fetchone()
    if policy is None:
        # A reader can start before the polling service performs migration.
        # Never expose a transient recalculation of the legacy policy window.
        return dict(groups=[],homes=[home],enabled=False,account=None,
                    ownership_gaps=[],phase='off',started=None,policy=POLICY_VERSION,
                    complete=False,lookup_failed=False,last_observed_at=None,pending_responses=[])
    started = policy[0]
    payloads = quota_payloads.PayloadReader(db)
    latest = db.execute('select * from tracking_observations where home=? and requested>=? and received<=? order by received desc,seq desc limit 1',
                        (home, started, now)).fetchone()
    account = payloads.decode(latest).get('account') if latest else None
    controls = list(db.execute('select at,enabled from tracking_controls where home=? and at>=? and at<=? order by at', (home, started, now)))
    if context_only:
        return dict(account=account, controls=[dict(r) for r in controls])
    homes = [home]
    if account:
        for candidate in db.execute('select home,started from tracking_config where home<>?', (home,)):
            other = db.execute('select * from tracking_observations where home=? and requested>=? and received<=? order by received desc,seq desc limit 1',
                               (candidate['home'], candidate['started'], now)).fetchone()
            if other and payloads.decode(other).get('account') == account:
                homes.append(candidate['home'])
    slots = ','.join('?' for _ in homes)
    if not pack_groups:replay_cache=None
    cached=replay_cache.get('checkpoint') if pack_groups and replay_cache is not None and changes is not None else None
    if replay_cache is not None:replay_cache.clear()
    if cached and (cached['key']!=(home,started) or now<cached['at'] or
        any(table in ('tracking_config','tracking_policies','tracking_controls') or
            table=='tracking_observations' and (at is None or at<=cached['at']) for table,at in changes)):
        cached=None
    if cached:
        state=replace(cached['state'],observations=list(cached['state'].observations),
                      closed=[],resets=replace(cached['state'].resets))
        raw=dict(cached['raw']);groups=[dict(group) for group in cached['groups']]
        last_observed_at=cached['last_observed_at']
        monitoring=cached['monitoring'];enabled_at=cached['enabled_at']
    else:
        state = TrackingState(started)
        raw = {};groups=[];last_observed_at=None
        monitoring=False;enabled_at=started
    control_rows=iter(row for row in controls if not cached or row['at']>cached['at'])
    control=next(control_rows,None)
    if pack_groups:
        from .quota_cycles import _PackedRows

    def finish_groups():
        # A closed segment cannot consume future observations. Transfer its
        # display rows now instead of retaining every Observation and a second
        # full timeline until the end of a multi-year replay.
        closed=state.closed;state.closed=[]
        for interval in closed:
            first=interval['start']
            rows=[raw.pop(value.sequence) for value in interval['observations']]
            groups.append(dict(id='forward:'+str(first.sequence),observations=_PackedRows(rows) if pack_groups else rows,
                boundary=first.received_at,reason=interval['reason'],rejected=[],
                provisional=False,finished_at=None,boundary_pending=False,boundary_uncertain=False))

    def apply_controls(until):
        nonlocal control,monitoring,enabled_at
        while control is not None and control['at']<=until:
            enabled=bool(control['enabled'])
            if not enabled:
                state.close('monitoring_off');finish_groups()
            elif not monitoring:enabled_at=control['at']
            monitoring=enabled;control=next(control_rows,None)

    sql='select * from tracking_observations where home=? and requested>=? and received<=? order by received,seq'
    if cached:
        records=db.execute(sql.replace(' order by',' and received>? order by'),(home,started,now,cached['at']))
    else:
        # A checkpoint owns the replay cursor. Keeping a second raw-row cache
        # here would miss appends consumed only through the checkpoint path.
        records=(read_rows('tracking_observations',sql,(home,started,now),('tracking_observations',),'received',clock=True)
                 if read_rows and replay_cache is None else db.execute(sql,(home,started,now)))
    previous_separate=object()
    for row in records:
        # SQL already orders observations. Controls sort before observations
        # at the same instant, exactly as in the former combined timeline.
        apply_controls(row['received'])
        quota = payloads.decode(row)
        window = quota.get('windows', {}).get('weekly')
        if not window or not quota.get('account') or not window.get('resets_at'):
            continue
        separate=quota.get('separate_models', [])
        if separate!=previous_separate:
            separate_epoch=tuple(sorted(separate));separate_text=json.dumps(separate)
            previous_separate=separate
        epoch = (quota['account'], quota.get('plan_type'), quota.get('bucket'),
                 window['resets_at'], separate_epoch)
        value = Observation(row['seq'], row['requested'], row['received'], 100-window['used_percent'], epoch,
                            resets_at=window['resets_at'])
        last_observed_at=row['received']
        if not monitoring or value.requested_at<enabled_at:continue
        raw[row['seq']] = dict(id='tracking:'+str(row['seq']), at=row['received'],
            used=window['used_percent'], reset=window['resets_at'], minutes=10080,
            account=quota['account'], plan=quota.get('plan_type',''), bucket=quota.get('bucket',''),
            separate=separate_text, source=quota.get('source','live'))
        previous=state.observations
        if not state.observe(value):raw.pop(value.sequence)
        elif previous is not state.observations:
            # Reset recovery can replace a provisional baseline without closing
            # it. Those discarded display candidates no longer have a consumer.
            raw={value.sequence:raw[value.sequence]}
        finish_groups()
    apply_controls(now)
    if pack_groups and replay_cache is not None and changes is not None:
        # Keep only immutable closed rows and a bounded copy of the unfinished
        # state. Closing it for display must not discard reset-recovery state
        # needed by the next observation. Old edits invalidate this checkpoint.
        weight=sum(512+128*len(group['observations'].blocks)+sum(map(len,group['observations'].blocks)) for group in groups)
        weight+=sum(768+sum(4*len(v) for v in row.values() if isinstance(v,str)) for row in raw.values())
        if weight<=REPLAY_CACHE_BYTES:
            checkpoint=replace(state,observations=list(state.observations),closed=[],resets=replace(state.resets))
            replay_cache['checkpoint']=dict(key=(home,started),at=now,state=checkpoint,raw=dict(raw),
                groups=tuple(groups),last_observed_at=last_observed_at,monitoring=monitoring,
                enabled_at=enabled_at,weight=weight)
    if state.baseline and state.endpoint:
        state.close('on');finish_groups()
    health_rows = list(db.execute(f'select * from tracking_health where home in ({slots})', homes))
    complete = len(health_rows) == len(homes) and all(h['complete'] and now-h['at'] <= 15 for h in health_rows)
    lookup_failed = bool(db.execute('select 1 from tracking_gaps where home=? and start>=? and end is null', (home, started)).fetchone())
    pending_responses=[dict(r) for r in db.execute(
        f"select w.* from tracking_wire w where w.home in ({slots}) and w.start>=? and w.response<>'' "
        f'and not exists(select 1 from calls c where c.home in ({slots}) and c.uid=w.response)',
        (*homes,started,*homes))]
    return dict(groups=groups, homes=homes, enabled=monitoring, account=account,
                controls=[dict(r) for r in controls],
                ownership_gaps=[], phase='on' if monitoring else 'off',
                started=started, policy=POLICY_VERSION, complete=complete,
                lookup_failed=lookup_failed,
                last_observed_at=last_observed_at,
                pending_responses=pending_responses)
