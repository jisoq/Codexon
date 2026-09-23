"""Durable forward tracking inputs. No historical quota is imported here."""
import json
import time
import os

POLICY_VERSION = "continuous-account-v2"
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
        CREATE TABLE IF NOT EXISTS tracking_activity_versions(
            seq INTEGER PRIMARY KEY AUTOINCREMENT, home TEXT, identity TEXT,
            seen REAL, start REAL, end REAL, kind TEXT, status TEXT, response TEXT);
        CREATE INDEX IF NOT EXISTS tracking_activity_seen ON tracking_activity_versions(home,seen);
    ''')


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
        db.execute('insert or replace into tracking_health values(?,?,?)', (home, snapshot['ts'], int(complete)))
        for session in snapshot.get('sessions', []):
            if session['home'] != home:
                continue
            cache_key=('turns',home,session['id'])
            records=session.get('turn_records', {})
            if cache.get(cache_key)==records:
                continue
            cache[cache_key]={key:dict(value) for key,value in records.items()}
            for turn, record in session.get('turn_records', {}).items():
                start, end = record.get('started_at'), record.get('ended_at')
                if start is None or (end is not None and end < enabled[0]):
                    continue
                if start < enabled[0] and end is None:
                    continue
                identity=session['id'] + ':' + turn
                old=db.execute('select start,end from tracking_tasks where home=? and identity=?',(home,identity)).fetchone()
                if old is None or tuple(old)!=(start,end):
                    db.execute('insert into tracking_activity_versions(home,identity,seen,start,end,kind,status,response) values(?,?,?,?,?,?,?,?)',
                        (home,identity,snapshot['ts'],start,end,'task','',''))
                db.execute('insert into tracking_tasks values(?,?,?,?) on conflict(home,identity) '
                           'do update set start=excluded.start,end=excluded.end',
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
            start = request.get('request_observed_at') or request['ts']
            end = request.get('completed_observed_at')
            terminal = request['status'] in ('completed','failed','incomplete','disconnected','http_error','unparsed')
            if start < enabled[0] and end is None:
                continue  # Pre-activation pending records do not prove a request is still running.
            # Older observers only timestamp successful completion. Preserve
            # uncertainty instead of using the original request timestamp as end.
            if terminal and end is None:
                end = snapshot['ts']
            if end is not None and end < enabled[0]:
                continue
            old = db.execute('select end from tracking_wire where home=? and identity=?', (home,request['attempt'])).fetchone()
            if old and old['end'] is not None and not request.get('completed_observed_at'):
                end = old['end']
            db.execute('insert or replace into tracking_wire values(?,?,?,?,?,?)',
                (home,request['attempt'],request.get('response_id',''),start,end,request['status']))
            db.execute('insert into tracking_activity_versions(home,identity,seen,start,end,kind,status,response) values(?,?,?,?,?,?,?,?)',
                (home,request['attempt'],snapshot['ts'],start,end,'wire',request['status'],request.get('response_id','')))


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
    db.execute('insert into tracking_observations(home,requested,received,data) values(?,?,?,?)',
               (home, quota.get('requested_at', quota['observed_at']), quota['observed_at'], json.dumps(quota)))
    db.commit()


def failed_observation(db, home, at):
    if not db.execute('select 1 from tracking_gaps where home=? and end is null',(home,)).fetchone():
        last=db.execute('select received from tracking_observations where home=? and requested >= coalesce((select started from tracking_config where home=?),?) order by seq desc limit 1',(home,home,at)).fetchone()
        db.execute('insert or ignore into tracking_gaps values(?,?,NULL)',(home,last[0] if last else at))
        db.commit()


def build(db, home, now):
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
    latest = db.execute('select data from tracking_observations where home=? and requested>=? and received<=? order by received desc,seq desc limit 1',
                        (home, started, now)).fetchone()
    account = json.loads(latest[0]).get('account') if latest else None
    homes = [home]
    if account:
        for candidate in db.execute('select home,started from tracking_config where home<>?', (home,)):
            other = db.execute('select data from tracking_observations where home=? and requested>=? and received<=? order by received desc,seq desc limit 1',
                               (candidate['home'], candidate['started'], now)).fetchone()
            if other and json.loads(other[0]).get('account') == account:
                homes.append(candidate['home'])
    slots = ','.join('?' for _ in homes)
    state = TrackingState(started)
    controls = list(db.execute('select at,enabled from tracking_controls where home=? and at>=? and at<=? order by at', (home, started, now)))
    timeline = [(r['at'], 0, r['at'], bool(r['enabled'])) for r in controls]
    raw = {}
    for row in db.execute('select * from tracking_observations where home=? and requested>=? and received<=? order by received,seq', (home, started, now)):
        quota = json.loads(row['data'])
        window = quota.get('windows', {}).get('weekly')
        if not window or not quota.get('account') or not window.get('resets_at'):
            continue
        epoch = (quota['account'], quota.get('plan_type'), quota.get('bucket'),
                 window['resets_at'], tuple(sorted(quota.get('separate_models', []))))
        value = Observation(row['seq'], row['requested'], row['received'], 100-window['used_percent'], epoch,
                            resets_at=window['resets_at'])
        raw[row['seq']] = dict(id='tracking:'+str(row['seq']), at=row['received'],
            used=window['used_percent'], reset=window['resets_at'], minutes=10080,
            account=quota['account'], plan=quota.get('plan_type',''), bucket=quota.get('bucket',''),
            separate=json.dumps(quota.get('separate_models', [])), source=quota.get('source','live'))
        timeline.append((row['received'], 1, row['seq'], value))
    monitoring = False
    enabled_at = started
    for at, kind, identity, value in sorted(timeline, key=lambda x: (x[0], x[1], x[2])):
        if kind == 0:
            if not value:
                state.close('monitoring_off')
            elif not monitoring:
                enabled_at = at
            monitoring = value
        elif monitoring and value.requested_at >= enabled_at:
            state.observe(value)
    intervals = list(state.closed)
    if state.baseline and state.endpoint:
        intervals.append(dict(start=state.baseline, end=state.endpoint, reason='on',
                              finished_at=None, boundary_uncertain=False, observations=list(state.observations)))
    groups = []
    for interval in intervals:
        first, last = interval['start'], interval['end']
        rows = [raw[observation.sequence] for observation in interval['observations']]
        groups.append(dict(id='forward:'+str(first.sequence), observations=rows,
            boundary=first.received_at, reason=interval['reason'], rejected=[],
            provisional=False, finished_at=None, boundary_pending=False,
            boundary_uncertain=False))
    health_rows = list(db.execute(f'select * from tracking_health where home in ({slots})', homes))
    complete = len(health_rows) == len(homes) and all(h['complete'] and now-h['at'] <= 15 for h in health_rows)
    lookup_failed = bool(db.execute('select 1 from tracking_gaps where home=? and start>=? and end is null', (home, started)).fetchone())
    wire = list(db.execute(f'select * from tracking_wire where home in ({slots}) and start>=?', (*homes, started)))
    return dict(groups=groups, homes=homes, enabled=monitoring, account=account,
                controls=[dict(r) for r in controls],
                ownership_gaps=[], phase='on' if monitoring else 'off',
                started=started, policy=POLICY_VERSION, complete=complete,
                lookup_failed=lookup_failed,
                last_observed_at=max((r['at'] for r in raw.values()), default=None),
                pending_responses=[dict(r) for r in wire if r['response'] and
                    not db.execute(f'select 1 from calls where home in ({slots}) and uid=?', (*homes,r['response'])).fetchone()])
