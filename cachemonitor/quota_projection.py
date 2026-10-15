"""A disposable current-period replay checkpoint, owned by the quota reader.

Raw observations, calls and recovery acknowledgements remain authoritative.
This projection replaces full-history replay only while its prefix is valid.
It is written on a period transition or after invalidation, not on each poll.
"""
from dataclasses import asdict
import hashlib
import json
import sqlite3
import zlib

VERSION=1
MAX_BYTES=8*1024*1024
MAX_DECODED_BYTES=32*1024*1024
MAX_ROWS=65536
MAX_HOMES=4
TRACKING_TABLES={'tracking_config','tracking_policies','tracking_controls','tracking_observations'}
HISTORY_TABLES={'observations','observation_context','window_observations','weekly_window_refs','quota_cycle_clears'}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS quota_current_projection(
        home TEXT PRIMARY KEY,version INTEGER NOT NULL,at REAL NOT NULL,start REAL NOT NULL,
        account TEXT,valid INTEGER NOT NULL,payload BLOB NOT NULL,digest TEXT NOT NULL)''')
    for table in sorted(TRACKING_TABLES|HISTORY_TABLES):
        for operation in ('INSERT','UPDATE','DELETE'):
            sides=('old','new') if operation=='UPDATE' else ('old',) if operation=='DELETE' else ('new',)
            conditions=[]
            for side in sides:
                if table in ('observation_context','weekly_window_refs'):
                    field='id' if table=='observation_context' else 'observation_id'
                    stamp=f'(SELECT at FROM observations WHERE id={side}.{field})'
                    # A deleted source can no longer establish prefix ownership.
                    conditions.append(f'({stamp} IS NULL OR {stamp}<=start)')
                elif table in ('observations','window_observations','tracking_observations','tracking_controls'):
                    column='received' if table=='tracking_observations' else 'at'
                    bound='at' if table in ('tracking_observations','tracking_controls') else 'start'
                    conditions.append(f'(home={side}.home AND ({side}.{column} IS NULL OR {side}.{column}<={bound}))')
                else:conditions.append(f'home={side}.home')
            predicate=' OR '.join(conditions)
            when=''
            if table=='tracking_observations':
                # Lossless payload migration does not change an observation.
                when=' WHEN (SELECT active FROM tracking_payload_migration WHERE id=1)=0'
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS current_projection_{table}_{operation.lower()}
                AFTER {operation} ON {table}{when} BEGIN
                UPDATE quota_current_projection SET valid=0 WHERE valid=1 AND ({predicate}); END''')


def _tuple(value):
    return tuple(_tuple(item) for item in value) if isinstance(value,list) else value


def load(db,home,now):
    row=db.execute('''SELECT home,version,at,start,account,valid,
        CASE WHEN length(payload)<=? THEN payload END AS payload,digest
        FROM quota_current_projection WHERE home=? AND version=? AND valid=1 AND at<=?''',
                   (MAX_BYTES,home,VERSION,now)).fetchone()
    if row is None:return None
    payload=row['payload']
    if payload is None or hashlib.sha256(payload).hexdigest()!=row['digest']:return {'invalid':True}
    try:
        decoder=zlib.decompressobj();raw=decoder.decompress(payload,MAX_DECODED_BYTES+1)
        if len(raw)>MAX_DECODED_BYTES or not decoder.eof or decoder.unused_data:return {'invalid':True}
        value=json.loads(raw)
        from .quota_tracking import Observation,TrackingState,ResetTracker
        from .quota_cycles import _PackedRows
        checkpoint=value['tracking'];state=checkpoint['state']
        def observation(item):
            return Observation(**{**item,'epoch':_tuple(item['epoch'])}) if item is not None else None
        for key in ('baseline','endpoint','idle_endpoint','last'):state[key]=observation(state[key])
        state['observations']=[observation(item) for item in state['observations']]
        state['resets']=ResetTracker(**state['resets'])
        checkpoint['state']=TrackingState(**state)
        checkpoint['key']=tuple(checkpoint['key'])
        checkpoint['raw']={int(key):item for key,item in checkpoint['raw'].items()}
        for group in checkpoint['groups']:group['observations']=_PackedRows(group['observations'])
        return dict(at=row['at'],start=row['start'],account=row['account'],
                    resume=value['resume'],tracking=checkpoint)
    except (ValueError,TypeError,KeyError,zlib.error):
        return {'invalid':True}


def save(ledger,report,view):
    """Publish a bounded checkpoint only if concurrent edits preserve its prefix."""
    history=view.get('_history_groups') or {}
    latest=history.get('last') or (history['groups'][-1] if history.get('groups') else None)
    inputs=report.get('_projection_tracking',getattr(ledger,'_tracking_inputs',{}))
    checkpoint=inputs.get('checkpoint') or inputs.get('current_checkpoint')
    if latest is None or checkpoint is None or report.get('history_pending'):return False
    home,at=report['home'],report['at'];start=latest[0]
    if checkpoint.get('retained_after') is not None and checkpoint['retained_after']>=start:return False
    db=ledger.db
    if db.in_transaction:return False
    previous=db.execute('SELECT version,start,valid FROM quota_current_projection WHERE home=?',(home,)).fetchone()
    if previous and tuple(previous)==(VERSION,start,1) and getattr(ledger,'_projection_invalid',None)!=home:return False
    import pickle
    resume=pickle.loads(latest[4])  # In-memory classifier checkpoint, never disk input.
    selected=[group for group in checkpoint['groups']
              if group['observations'] and group['observations'][-1]['at']>=start]
    if len(checkpoint['raw'])+sum(len(group['observations']) for group in selected)>MAX_ROWS:return False
    retained=[{**group,'observations':list(group['observations'])} for group in checkpoint['groups']
              if group['observations'] and group['observations'][-1]['at']>=start]
    state=asdict(checkpoint['state'])
    state['closed']=[]
    data=dict(resume=resume,tracking={**checkpoint,'state':state,'groups':retained})
    data['tracking'].pop('weight',None)
    # The active period is legitimate growing data. Oversized periods use the
    # ordinary reader instead of persisting an unbounded extra representation.
    raw=bytearray()
    for chunk in json.JSONEncoder(ensure_ascii=True,separators=(',',':')).iterencode(data):
        if len(raw)+len(chunk)>MAX_DECODED_BYTES:return False
        raw.extend(chunk.encode('ascii'))
    payload=zlib.compress(raw,1)
    if len(payload)>MAX_BYTES:return False
    revision=report.get('_projection_revision',getattr(ledger,'_input_revision',None))
    if revision is None:return False
    timeout=db.execute('PRAGMA busy_timeout').fetchone()[0]
    db.execute('PRAGMA busy_timeout=0')
    try:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT value FROM report_revision WHERE id=1').fetchone()[0]
        if current!=revision:
            changes=list(db.execute('SELECT table_name,at FROM report_changes WHERE revision>?',(revision,)))
            if current-revision!=len(changes):return False
            # Appends beyond the captured time will be replayed by the reader.
            if any(table in TRACKING_TABLES|HISTORY_TABLES and (stamp is None or stamp<=at)
                   for table,stamp in changes):return False
        db.execute('INSERT OR REPLACE INTO quota_current_projection VALUES(?,?,?,?,?,1,?,?)',
                   (home,VERSION,at,start,report.get('account'),payload,hashlib.sha256(payload).hexdigest()))
        db.execute('''DELETE FROM quota_current_projection WHERE home IN (
            SELECT home FROM quota_current_projection ORDER BY at DESC,home LIMIT -1 OFFSET ?)''',(MAX_HOMES,))
        db.commit();ledger._projection_invalid=None;return True
    finally:
        if db.in_transaction:db.rollback()
        db.execute(f'PRAGMA busy_timeout={timeout}')
