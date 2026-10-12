"""Sanitized historical usage imported from retired record sources."""
import hashlib
import json
from .core import Session


def _load(db,homes):
    groups={};records={}
    for home,rid,data in db.execute('SELECT home,response_id,data FROM usage_archive ORDER BY home,response_id'):
        if home not in homes:continue
        row=json.loads(data);sid=row['sid'];purpose=row.get('purpose','')
        key=(home,sid,purpose)
        if key not in groups:
            groups[key]=Session((purpose or 'imported')+':'+sid,home,title='추가 요청 기록',parent_thread_id=sid)
        session=groups[key]
        session.add_usage(row['ts'],rid,dict(input_tokens=row.get('input'),cached_input_tokens=row.get('cached'),
            cache_write_input_tokens=row.get('written'),output_tokens=row.get('output'),reasoning_output_tokens=row.get('reasoning')),
            row.get('model',''),turn=rid,effort=row.get('effort','미확인'),service_tier=row.get('service_tier','미확인'))
        request=session.requests[-1];request.purpose=purpose;request.request_start=row.get('request_start')
        session.turn_records[rid]=dict(started_at=row.get('request_start'),ended_at=row.get('request_end'),
            state={'completed':'완료','failed':'중단'}.get(row.get('state'),'미확인'))
        records.setdefault(key,[]).append(row)
    return [(key,session,hashlib.sha256(json.dumps(records[key],sort_keys=True).encode()).hexdigest())
            for key,session in groups.items()]


def _views(groups,now):
    result=[]
    for key,session,revision in groups:
        view=session.view(now,use_cache=True)
        view.update(source='imported',collection_complete=True,archived=False,purpose=key[2],
            usage_revision=revision,request_revision=revision)
        result.append(view)
    return result


def sessions(db,now,homes):
    return _views(_load(db,homes),now)


class ArchiveSessions:
    """Own one current reconstruction, with no serialized rows or older generations."""
    def __init__(self,db):
        self.db=db
        self.key=None
        self.groups=[]
        # Main-connection event writes do not invalidate immutable imported history.
        # TEMP triggers also cover callers that update the archive through this connection.
        db.execute('CREATE TEMP TABLE IF NOT EXISTS usage_archive_revision(value INTEGER)')
        if not db.execute('SELECT 1 FROM usage_archive_revision').fetchone():
            db.execute('INSERT INTO usage_archive_revision VALUES(0)')
        for action in ('INSERT','UPDATE','DELETE'):
            db.execute(f'''CREATE TEMP TRIGGER IF NOT EXISTS usage_archive_{action.lower()}
                AFTER {action} ON main.usage_archive BEGIN
                UPDATE usage_archive_revision SET value=value+1; END''')

    def views(self,now,homes):
        key=(frozenset(homes),self.db.execute('PRAGMA data_version').fetchone()[0],
             self.db.execute('SELECT value FROM usage_archive_revision').fetchone()[0])
        if key!=self.key:
            # data_version conservatively reloads after another connection commits.
            # No cursor or source JSON survives the reconstruction.
            groups=_load(self.db,homes)
            # A rollback also restores the TEMP revision counter. Do not publish
            # transactional values under a revision a later write could reuse.
            if self.db.in_transaction:
                return _views(groups,now)
            self.groups=groups
            self.key=key
        return _views(self.groups,now)

    def clear(self):
        self.groups=[]
        self.key=None
