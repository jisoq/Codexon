"""Shared, transactional cache schema; normal readers do not run startup DDL."""
import sqlite3
import time
from pathlib import Path


TABLES=(
    'cache_preferences(key TEXT PRIMARY KEY,value TEXT)',
    'cache_profiles(home TEXT,sid TEXT,model TEXT,data TEXT,PRIMARY KEY(home,sid,model))',
    'cache_inputs(home TEXT,sid TEXT,turn TEXT,at REAL,model TEXT,kind TEXT,PRIMARY KEY(home,sid,turn,kind))',
    'cache_tickets(id TEXT PRIMARY KEY,home TEXT,sid TEXT,turn TEXT,model TEXT,digest TEXT,created REAL,expires REAL,state TEXT)',
    'cache_status(home TEXT,sid TEXT,data TEXT,PRIMARY KEY(home,sid))',
    'cache_forecasts(home TEXT,sid TEXT,data TEXT,PRIMARY KEY(home,sid))',
    'cache_gaps(home TEXT,sid TEXT,turn TEXT,data TEXT,PRIMARY KEY(home,sid,turn))',
    'cache_jobs(id TEXT PRIMARY KEY,home TEXT,sid TEXT,generation INTEGER,state TEXT,started REAL,ended REAL,response_id TEXT,model TEXT,effort TEXT,tier TEXT,usage TEXT)',
    'cache_operating_proposals(id TEXT PRIMARY KEY,home TEXT,data TEXT,at REAL)',
    'cache_operating_grants(id TEXT PRIMARY KEY,home TEXT,data TEXT,created REAL,expires REAL,stopped TEXT)',
)
COLUMNS=(('snapshot','TEXT'),('round','INTEGER'),('anchor','REAL'),('scope_read_lower','INTEGER'),
    ('operation','TEXT'),('expected_cost','REAL'),('adverse_cost','REAL'),('output_high','INTEGER'),('read_required','INTEGER'),('purpose','TEXT'),('transport','TEXT'))


def revoke_grants(path,home):
    """Offline revocation needs only SQLite, never the request execution runtime."""
    if not Path(path).exists():return
    db=connect(path,timeout=5)
    try:
        db.execute("UPDATE cache_operating_grants SET stopped=COALESCE(stopped,'revoked') WHERE home=?",(str(home),))
    finally:db.close()


def connect(path,timeout=.5):
    if str(path)!=':memory:':Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(str(path),timeout=min(timeout,.05),isolation_level=None)
    deadline=time.monotonic()+timeout
    try:
        while True:
            try:
                if db.execute('PRAGMA user_version').fetchone()[0]>=2:break
                if db.execute('PRAGMA journal_mode').fetchone()[0].lower()!='wal':db.execute('PRAGMA journal_mode=WAL')
                db.execute('BEGIN IMMEDIATE')
                # Recheck after acquiring the migration lock: another process
                # may have initialized or migrated the database while we waited.
                if db.execute('PRAGMA user_version').fetchone()[0]<2:
                    for table in TABLES:db.execute('CREATE TABLE IF NOT EXISTS '+table)
                    columns={r[1] for r in db.execute('PRAGMA table_info(cache_jobs)')}
                    for name,kind in COLUMNS:
                        if name not in columns:db.execute(f'ALTER TABLE cache_jobs ADD COLUMN {name} {kind}')
                    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_cache_job ON cache_jobs(home,sid) WHERE state IN ('reserved','sent')")
                    db.execute('PRAGMA user_version=2')
                db.execute('COMMIT');break
            except sqlite3.OperationalError as exc:
                if db.in_transaction:db.rollback()
                if (getattr(exc,'sqlite_errorcode',0)&255) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED) or time.monotonic()>=deadline:raise
                time.sleep(min(.05,max(0,deadline-time.monotonic())))
        db.execute(f'PRAGMA busy_timeout={max(0,int(timeout*1000))}')
        return db
    except BaseException:
        db.close();raise
