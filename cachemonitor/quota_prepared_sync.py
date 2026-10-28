"""Durable certificates for prepared owner facts already applied to the ledger.

Certificates are saved inside the call/boundary transaction. Mutation triggers
invalidate only affected owners, including SQLite replacement of another owner.
They are optional derived progress; absence requests idempotent reapplication.
"""
from hashlib import sha256


FORMAT = 1
APPEND_FORMAT = 1


class AppendPredecessorChanged(RuntimeError):
    """Partial application was refused before any quota/activity mutation."""


def require_append(db, transitions):
    """Hold the write transaction while checking the exact durable predecessor."""
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    committed=applied(db,transitions)
    policies={}
    for owner,value in transitions.items():
        if owner[0] not in policies:
            config=db.execute('SELECT started FROM tracking_config WHERE home=?',(owner[0],)).fetchone()
            control=db.execute('SELECT enabled,at FROM tracking_controls WHERE home=? ORDER BY at DESC LIMIT 1',
                               (owner[0],)).fetchone()
            policies[owner[0]]=(config[0] if config else None,tuple(control) if control else None)
        if (value.get('format')!=APPEND_FORMAT or
                type(value.get('count')) is not int or not 0<value['count']<=128 or
                committed.get(owner)!=value.get('prior_source') or value.get('policy')!=policies[owner[0]]):
            raise AppendPredecessorChanged('prepared ledger append predecessor changed')


def source_stamp(dependencies):
    from .prepared_store import pack
    from .prepared_ingest import fact_dependencies
    return sha256(pack(('quota-prepared-source', FORMAT, fact_dependencies(dependencies)))).hexdigest()


def _triggers():
    statements = {}
    for event in ('INSERT','UPDATE','DELETE'):
        sides = ('old','new') if event == 'UPDATE' else ('old',) if event == 'DELETE' else ('new',)
        owners = [f'(home={side}.home AND sid={side}.sid)' for side in sides]
        if event in ('INSERT','UPDATE'):
            # REPLACE may silently remove another owner's primary-key row.
            owners.append('(home=new.home AND sid IN (SELECT sid FROM calls WHERE home=new.home AND uid=new.uid))')
        name = 'prepared_applied_calls_' + event.lower()
        statements[name] = (f'CREATE TRIGGER {name} BEFORE {event} ON calls BEGIN '
            f'DELETE FROM prepared_analysis_applied WHERE {" OR ".join(owners)}; END')
        name = 'prepared_applied_boundaries_' + event.lower()
        selections = ' UNION '.join(f'SELECT home,sid FROM calls WHERE home={side}.home AND uid={side}.uid' for side in sides)
        statements[name] = (f'CREATE TRIGGER {name} BEFORE {event} ON tracking_call_boundaries BEGIN '
            f'DELETE FROM prepared_analysis_applied WHERE (home,sid) IN ({selections}); END')
    return statements


def initialize(db):
    """Run only when enabling the prepared writer, never in a query connection."""
    if db.in_transaction:
        raise RuntimeError('initialize prepared progress before starting source application')
    db.execute('BEGIN IMMEDIATE')
    try:
        db.execute('CREATE TABLE IF NOT EXISTS prepared_analysis_applied('
                   'home TEXT NOT NULL,sid TEXT NOT NULL,signature TEXT NOT NULL,PRIMARY KEY(home,sid)) WITHOUT ROWID')
        for name in ('prepared_applied_admit','prepared_applied_insert','prepared_applied_delete'):
            db.execute(f'DROP TRIGGER IF EXISTS {name}')
        db.execute('DROP TABLE IF EXISTS prepared_analysis_budget')
        definitions = _triggers()
        actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'prepared_applied_%'"))
        if any(actual.get(name) != sql for name,sql in definitions.items()):
            # Unknown invalidation coverage cannot certify existing ledger facts.
            db.execute('DELETE FROM prepared_analysis_applied')
            for name,sql in definitions.items():
                if actual.get(name) != sql:
                    db.execute(f'DROP TRIGGER IF EXISTS {name}')
                    db.execute(sql)
        db.commit()
    except BaseException:
        db.rollback()
        raise


def applied(db, owners):
    """One bounded indexed lookup per owner group, without reading call facts."""
    owners = list(owners)
    result = {}
    for start in range(0,len(owners),128):
        group = owners[start:start+128]
        placeholders = ','.join('(?,?)' for _ in group)
        rows = db.execute(f'SELECT home,sid,signature FROM prepared_analysis_applied WHERE (home,sid) IN ({placeholders})',
                          tuple(part for owner in group for part in owner))
        result.update(((row[0],row[1]),row[2]) for row in rows)
    return result


def save(db, certificates):
    """Called after source rows/boundaries, before the same transaction commits."""
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    for (home,sid),signature in certificates.items():
        if (type(home) is not str or type(sid) is not str or type(signature) is not str
                or len(signature) != 64 or any(c not in '0123456789abcdef' for c in signature)):
            raise ValueError('invalid prepared ledger certificate')
        old = db.execute('SELECT signature FROM prepared_analysis_applied WHERE home=? AND sid=?',(home,sid)).fetchone()
        if old is None:
            db.execute('INSERT INTO prepared_analysis_applied VALUES(?,?,?)',(home,sid,signature))
        elif old[0] != signature:
            db.execute('UPDATE prepared_analysis_applied SET signature=? WHERE home=? AND sid=?',(signature,home,sid))


def retire(db, owners):
    db.executemany('DELETE FROM prepared_analysis_applied WHERE home=? AND sid=?', owners)
