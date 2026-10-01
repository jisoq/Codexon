"""Sanitized historical usage imported from retired record sources."""
import hashlib
import json
from .core import Session


def sessions(db,now,homes):
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
    result=[]
    for key,session in groups.items():
        view=session.view(now)
        view.update(source='imported',collection_complete=True,archived=False,purpose=key[2],
            usage_revision=hashlib.sha256(json.dumps(records[key],sort_keys=True).encode()).hexdigest())
        result.append(view)
    return result
