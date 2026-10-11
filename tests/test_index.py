import json
import sqlite3
from pathlib import Path
import pytest

from cachemonitor.index import UsageIndex
from cachemonitor.analytics import analyze
from test_core import event, usage, fixture_home, TID


def finish(index, now=10010):
    for _ in range(100):
        result = index.poll(now)
        if not result['index']['loading']:
            return result
    raise AssertionError('index did not finish')


def test_top_level_compaction_is_indexed_and_backfilled_without_private_payload(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    with path.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(event('compacted',10001,message='PRIVATE_COMPACTION_TEXT'))+'\n')
        stream.write(json.dumps(usage('after',time=10002))+'\n')
    for legacy in (False,True):
        if legacy:
            with sqlite3.connect(cache) as db:
                db.execute("DELETE FROM events WHERE data LIKE '%compacted%'")
                db.execute('PRAGMA user_version=3')
        index=UsageIndex([home],cache)
        try:
            value=finish(index)
            history=analyze(value['sessions'])['responses']
            assert next(r for r in history if r['key']=='after')['compaction_epoch']==1
            stored=''.join(r[0] for r in index.db.execute('SELECT data FROM events'))
            assert 'compacted' in stored and 'PRIVATE_COMPACTION_TEXT' not in stored
        finally:index.close()


def test_split_history_dedup_effort_restart_and_private_content(tmp_path):
    home, current = fixture_home(tmp_path)
    folder = home / 'sessions'
    folder.mkdir()
    old = folder / 'old.jsonl'
    old_events = [event('session_meta',id=TID,source='vscode'),
        event('turn_context',turn_id='one',model='gpt-6-astra',effort='xhigh', instructions='PRIVATE_PROMPT_SENTINEL'),
        event('event_msg',type='task_started',turn_id='one'),
        event('token_usage_record',thread_id=TID,turn_id='one',response_id='r1',usage={'input_tokens':100,'output_tokens':20,'reasoning_output_tokens':15}),
        event('event_msg',10001,type='task_complete',turn_id='one')]
    old.write_text('\n'.join(json.dumps(e) for e in old_events)+'\n',encoding='utf8')
    latest = [event('turn_context',10002,turn_id='two',model='gpt-6-astra',effort='ultra'),
        event('event_msg',10002,type='task_started',turn_id='two'),
        old_events[3], # replayed response, must not duplicate
        event('token_usage_record',10003,thread_id=TID,turn_id='two',response_id='r2',usage={'input_tokens':300,'output_tokens':30,'reasoning_output_tokens':20},thread_token_usage={'input_tokens':400,'output_tokens':50,'reasoning_output_tokens':35}),
        event('token_usage_record',10004,thread_id='foreign',response_id='fork',usage={'input_tokens':99999,'output_tokens':99}),
        event('event_msg',10004,type='task_complete',turn_id='two')]
    current.write_text('\n'.join(json.dumps(e) for e in latest)+'\n',encoding='utf8')
    cache=tmp_path/'index.sqlite'
    index=UsageIndex([home],cache)
    v=finish(index)
    a=analyze(v['sessions'])
    assert len(a['responses'])==2
    assert a['totals']['total']==450
    assert {(g['effort'],g['n']) for g in a['groups']}=={('xhigh',1),('ultra',1)}
    assert a['unclassified']['total']==0
    assert len(analyze(v['sessions'],unit='turn')['units'])==2
    before=index.bytes_read
    finish(index,10011)
    assert index.bytes_read==before
    index.close()
    db=sqlite3.connect(cache)
    assert 'PRIVATE_PROMPT_SENTINEL' not in ''.join(row[0] for row in db.execute('select data from events'))
    db.close()
    index=UsageIndex([home],cache)
    restored=finish(index)
    assert analyze(restored['sessions'])['totals']['total']==450
    assert index.bytes_read==0
    index.close()


def test_incremental_partial_line_and_truncation(tmp_path):
    home,path=fixture_home(tmp_path)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    first=finish(index)
    assert len(analyze(first['sessions'])['responses'])==1
    data=(json.dumps(usage('r2',time=10011))+'\n').encode()
    with path.open('ab') as f:f.write(data[:40])
    assert len(analyze(finish(index,10012)['sessions'])['responses'])==1
    with path.open('ab') as f:f.write(data[40:])
    assert len(analyze(finish(index,10013)['sessions'])['responses'])==2
    path.write_text(json.dumps(usage('r3',time=10014))+'\n',encoding='utf8')
    v=finish(index,10015)
    assert [r['key'] for r in analyze(v['sessions'])['responses']]==['r3']
    index.close()


def test_append_reuses_session_but_rewrite_plus_growth_rebuilds(tmp_path):
    home,path=fixture_home(tmp_path)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        finish(index)
        key=(str(home.resolve()),TID);previous=index.monitor.sessions[key]
        with path.open('a',encoding='utf8') as file:file.write(json.dumps(usage('r2',time=10011))+'\n')
        finish(index,10012)
        assert index.monitor.sessions[key] is previous
        original=path.read_text(encoding='utf8')
        changed=original.replace('r2','corrected-response')+json.dumps(usage('r3',time=10013))+'\n'
        path.write_text(changed,encoding='utf8')
        result=finish(index,10014)
        assert index.monitor.sessions[key] is not previous
        keys={r['key'] for r in analyze(result['sessions'])['responses']}
        assert 'corrected-response' in keys and 'r2' not in keys and 'r3' in keys
    finally:index.close()


@pytest.mark.parametrize('blocks',(8,16))
def test_unchanged_catchup_reads_each_byte_once(tmp_path,monkeypatch,blocks):
    import cachemonitor.index as implementation
    monkeypatch.setattr(implementation,'FILE_READ_BUDGET',1024)
    home,path=fixture_home(tmp_path)
    content=(json.dumps(event('session_meta',id=TID))+'\n').encode()
    content+=(b'x'*255+b'\n')*blocks
    path.write_bytes(content)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        index.queue.append((str(path),home,TID));reads=[]
        while index.queue:
            before=index.bytes_read
            index.scan_file(*index.queue.popleft())
            reads.append(index.bytes_read-before)
        assert len(reads)>1
        assert sum(reads)==len(content)
        assert max(reads)<=1024+256
        assert not index.source_work
    finally:index.close()


@pytest.mark.parametrize('restart',(False,True))
def test_appended_prefix_verification_resumes_within_read_budget(tmp_path,monkeypatch,restart):
    import cachemonitor.index as implementation
    monkeypatch.setattr(implementation,'FILE_READ_BUDGET',1024)
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    with path.open('ab') as stream:stream.write((b'x'*255+b'\n')*24)
    original=path.read_bytes()
    index=UsageIndex([home],cache)
    try:
        finish(index)
        if restart:
            index.close();index=UsageIndex([home],cache)
        tail=(json.dumps(usage('after',time=10011))+'\n').encode()
        with path.open('ab') as stream:stream.write(tail)
        index.queue.append((str(path),home,TID));reads=[]
        while index.queue:
            before=index.bytes_read
            index.scan_file(*index.queue.popleft())
            reads.append(index.bytes_read-before)
        assert len(reads)>2
        assert sum(reads)==len(original)+len(tail)
        assert max(reads)<=1024+max(256,len(tail))
        index.rebuild((str(home.resolve()),TID),10012)
        assert [row['key'] for row in index.monitor.sessions[(str(home.resolve()),TID)].view(10012)['history']]==['r1','after']
        assert not index.source_work
    finally:index.close()


def test_prefix_change_during_resumed_validation_rebuilds_corrected_history(tmp_path,monkeypatch):
    import cachemonitor.index as implementation
    monkeypatch.setattr(implementation,'FILE_READ_BUDGET',1024)
    home,path=fixture_home(tmp_path)
    with path.open('ab') as stream:stream.write((b'x'*255+b'\n')*24)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        finish(index)
        with path.open('a',encoding='utf8') as stream:stream.write(json.dumps(usage('after',time=10011))+'\n')
        before=index.bytes_read
        assert index.scan_file(str(path),home,TID) is None
        assert index.bytes_read-before==1024
        path.write_bytes(path.read_bytes().replace(b'r1',b'r9')+b'\n')
        result=finish(index,10012)
        assert [row['key'] for row in result['sessions'][0]['history']]==['r9','after']
    finally:index.close()


def test_catchup_digest_capacity_waits_without_losing_files(tmp_path,monkeypatch):
    import cachemonitor.index as implementation
    monkeypatch.setattr(implementation,'FILE_READ_BUDGET',1024)
    monkeypatch.setattr(implementation,'MAX_SOURCE_WORK',2)
    home,path=fixture_home(tmp_path)
    content=path.read_bytes()+(b'x'*255+b'\n')*20
    paths=[path.with_name(f'large-{i}.jsonl') for i in range(5)]
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        for item in paths:
            item.write_bytes(content)
            index.queue.append((str(item),home,TID))
        for _ in range(100):
            if not index.queue:break
            index.scan_file(*index.queue.popleft())
            assert len(index.source_work)<=2
        assert not index.queue and not index.source_work
        assert index.bytes_read==len(paths)*len(content)
        assert index.db.execute('SELECT COUNT(*) FROM files WHERE offset=size').fetchone()[0]==len(paths)
    finally:index.close()


def test_imported_history_reuses_unchanged_records_and_refreshes_time_windows(tmp_path):
    from cachemonitor.core import WINDOW
    home,_=fixture_home(tmp_path)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    row=dict(sid='old',ts=10000,input=100,output=20,cached=50,written=0,reasoning=0,
             purpose='maintenance',state='completed',request_start=9999,request_end=10000)
    try:
        index.db.execute('INSERT INTO usage_archive VALUES(?,?,?)',(str(home),'archived',json.dumps(row)))
        index.db.commit();queries=[]
        index.db.set_trace_callback(lambda query:queries.append(query) if query.startswith('SELECT home,response_id,data FROM usage_archive') else None)
        first=index.archive.views(10010,{str(home)})[0]
        index.db.execute('INSERT INTO metadata VALUES(?,?,?)',(str(home),'unrelated','{}'))
        second=index.archive.views(10011,{str(home)})[0]
        expired=index.archive.views(10000+WINDOW+1,{str(home)})[0]
        assert len(queries)==1
        assert second['history'] is first['history'] is expired['history']
        assert second['remaining']==first['remaining']-1
        assert first['totals']['30m']['total']==120 and expired['totals']['30m']['total']==0
        assert first['usage_revision']==expired['usage_revision']
        row['input']=200
        index.db.execute('UPDATE usage_archive SET data=?',(json.dumps(row),));index.db.commit()
        changed=index.archive.views(10011,{str(home)})[0]
        assert len(queries)==2 and changed['totals']['session']['total']==220
        assert changed['usage_revision']!=first['usage_revision']
        with sqlite3.connect(index.path) as other:
            row['input']=300
            other.execute('UPDATE usage_archive SET data=?',(json.dumps(row),))
        changed=index.archive.views(10011,{str(home)})[0]
        assert len(queries)==3 and changed['totals']['session']['total']==320
        index.db.execute('DELETE FROM usage_archive')
        assert index.archive.views(10011,{str(home)})==[] and len(queries)==4
    finally:index.close()


def test_discovery_releases_removed_files_digest_capacity(tmp_path,monkeypatch):
    import cachemonitor.index as implementation
    monkeypatch.setattr(implementation,'FILE_READ_BUDGET',1024)
    monkeypatch.setattr(implementation,'MAX_SOURCE_WORK',1)
    home,path=fixture_home(tmp_path)
    removed=home/'removed.jsonl'
    removed.write_bytes(path.read_bytes()+(b'x'*255+b'\n')*20)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        index.scan_file(str(removed),home,TID)
        removed.unlink()
        index.discover(10010)
        assert index.scan_file(str(path),home,TID)==(str(home),TID)
        assert not index.source_work
        assert index.db.execute('SELECT offset,size FROM files WHERE path=?',(str(path),)).fetchone()==(path.stat().st_size,)*2
    finally:index.close()


def test_imported_history_rollback_cannot_reuse_uncommitted_revision(tmp_path):
    home,_=fixture_home(tmp_path)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    row=dict(sid='old',ts=10000,input=100,output=20,cached=50,written=0,reasoning=0,
             purpose='maintenance',state='completed',request_start=9999,request_end=10000)
    try:
        index.db.execute('INSERT INTO usage_archive VALUES(?,?,?)',(str(home),'archived',json.dumps(row)))
        index.db.commit()
        assert index.archive.views(10010,{str(home)})[0]['totals']['session']['total']==120
        row['input']=200
        index.db.execute('UPDATE usage_archive SET data=?',(json.dumps(row),))
        assert index.archive.views(10010,{str(home)})[0]['totals']['session']['total']==220
        index.db.rollback()
        row['input']=300
        index.db.execute('UPDATE usage_archive SET data=?',(json.dumps(row),));index.db.commit()
        assert index.archive.views(10010,{str(home)})[0]['totals']['session']['total']==320
    finally:index.close()


def test_restart_append_preserves_cached_events_and_matches_fresh_index(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    path.write_text(''.join(json.dumps(usage(f'r{i}',time=10000+i))+'\n' for i in range(6)),encoding='utf8')
    tail=(json.dumps(usage('after',time=10011))+'\n').encode()
    index=UsageIndex([home],cache)
    try:
        finish(index)
        with path.open('ab') as stream:stream.write(tail[:40])
        finish(index)
        previous=list(index.db.execute('SELECT * FROM events ORDER BY path,pos'))
        schema=list(index.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))
    finally:index.close()
    with path.open('ab') as stream:stream.write(tail[40:])
    index=UsageIndex([home],cache);fresh=UsageIndex([home],tmp_path/'fresh.sqlite')
    try:
        writes=index.db.total_changes
        actual=finish(index,10012)
        # Existing history is retained, even when a partial line spans the restart.
        assert index.db.total_changes-writes==2
        rows=list(index.db.execute('SELECT * FROM events ORDER BY path,pos'))
        assert rows[:-1]==previous
        assert actual['sessions']==finish(fresh,10012)['sessions']
        assert rows==list(fresh.db.execute('SELECT * FROM events ORDER BY path,pos'))
        assert list(index.db.execute('SELECT name,sql FROM sqlite_master ORDER BY name'))==schema
    finally:index.close();fresh.close()


def test_derived_event_identity_survives_archive_move_restart_and_append(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    records=[event('session_meta',id=TID),usage(''),usage('')]
    path.write_text(''.join(json.dumps(record)+'\n' for record in records),encoding='utf8')
    index=UsageIndex([home],cache)
    try:
        original=finish(index)['sessions'][0]['history']
        assert len(original)==2 and len({row['key'] for row in original})==2
        stored=list(index.db.execute('SELECT path,pos,data FROM events ORDER BY path,pos'))
        assert all('event_id' not in json.loads(row[2]) for row in stored)
    finally:index.close()
    archived=home/'archived_sessions'/path.name
    archived.parent.mkdir();path.rename(archived)
    index=UsageIndex([home],cache)
    try:
        assert finish(index)['sessions'][0]['history']==original
        assert list(index.db.execute('SELECT path,pos,data FROM events WHERE path=? ORDER BY pos',(str(path),)))==stored
        with archived.open('a',encoding='utf8') as stream:
            stream.write(json.dumps(usage('',time=10011))+'\n')
        appended=finish(index,10012)['sessions'][0]['history']
        assert appended[:2]==original and len(appended)==3
        assert len({row['key'] for row in appended})==3
    finally:index.close()
    index=UsageIndex([home],cache)
    try:assert finish(index,10012)['sessions'][0]['history']==appended
    finally:index.close()


def test_restart_rewrite_plus_append_revalidates_cached_prefix(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    index=UsageIndex([home],cache)
    try:finish(index)
    finally:index.close()
    original=path.read_text(encoding='utf8')
    path.write_text(original.replace('r1','r9')+json.dumps(usage('r2',time=10011))+'\n',encoding='utf8')
    index=UsageIndex([home],cache);fresh=UsageIndex([home],tmp_path/'fresh.sqlite')
    try:
        actual=finish(index,10012)
        assert {r['key'] for r in analyze(actual['sessions'])['responses']}=={'r9','r2'}
        assert actual['sessions']==finish(fresh,10012)['sessions']
        assert list(index.db.execute('SELECT * FROM events ORDER BY path,pos'))==list(fresh.db.execute('SELECT * FROM events ORDER BY path,pos'))
    finally:index.close();fresh.close()


def test_restart_append_recovers_malformed_cached_records(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    for number,corrupted in enumerate(('{','[]')):
        index=UsageIndex([home],cache)
        try:
            finish(index)
            index.db.execute('UPDATE events SET data=?',(corrupted,));index.db.commit()
        finally:index.close()
        with path.open('a',encoding='utf8') as stream:
            stream.write(json.dumps(usage(f'after-{number}',time=10011+number))+'\n')
        index=UsageIndex([home],cache);fresh=UsageIndex([home],tmp_path/f'fresh-{number}.sqlite')
        try:
            actual=finish(index,10015)
            assert not actual['errors'] and actual['usage_collection_complete']
            assert actual['sessions']==finish(fresh,10015)['sessions']
        finally:index.close();fresh.close()


def test_restart_append_preserves_malformed_source_warnings(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    with path.open('a',encoding='utf8') as stream:
        stream.write('{"type":"token_usage_record", bad}\n')
    index=UsageIndex([home],cache)
    try:assert finish(index)['errors']
    finally:index.close()
    with path.open('a',encoding='utf8') as stream:stream.write(json.dumps(usage('r2',time=10011))+'\n')
    index=UsageIndex([home],cache);fresh=UsageIndex([home],tmp_path/'fresh.sqlite')
    try:
        actual=finish(index,10012);expected=finish(fresh,10012)
        assert actual['errors']==expected['errors'] and actual['errors']
        assert not actual['usage_collection_complete']
        assert actual['sessions']==expected['sessions']
    finally:index.close();fresh.close()


def test_restart_append_revalidates_changed_session_lineage(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    header=json.dumps(event('session_meta',id=TID,parent_thread_id='parent-aa'))+'\n'
    path.write_text(header+path.read_text(encoding='utf8'),encoding='utf8')
    index=UsageIndex([home],cache)
    try:assert finish(index)['sessions'][0]['parent_thread_id']=='parent-aa'
    finally:index.close()
    original=path.read_text(encoding='utf8')
    path.write_text(original.replace('parent-aa','parent-bb')+json.dumps(usage('r2',time=10011))+'\n',encoding='utf8')
    index=UsageIndex([home],cache);fresh=UsageIndex([home],tmp_path/'fresh.sqlite')
    try:
        finish(index,10012)
        # Discovery publishes the corrected lineage retained by the source scan.
        actual=finish(index,10023)['sessions'][0];expected=finish(fresh,10023)['sessions'][0]
        assert actual['parent_thread_id']==expected['parent_thread_id']=='parent-bb'
        assert actual['history']==expected['history']
    finally:index.close();fresh.close()


def test_replaced_source_with_same_size_and_mtime_is_reindexed(tmp_path):
    import os
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    index=UsageIndex([home],cache)
    try:finish(index)
    finally:index.close()
    original=path.stat();replacement=path.with_suffix('.replacement')
    replacement.write_bytes(path.read_bytes().replace(b'r1',b'r9'))
    os.utime(replacement,ns=(original.st_atime_ns,original.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_ino!=original.st_ino
    index=UsageIndex([home],cache)
    try:
        result=finish(index)
        assert [r['key'] for r in analyze(result['sessions'])['responses']]==['r9']
    finally:index.close()


def test_turn_denominator_median_missing_values_and_filters(tmp_path):
    from cachemonitor.core import Session
    s=Session('s','h',title='task')
    for i,(turn,inp,out) in enumerate([('t1',10,2),('t1',20,4),('t2',100,8),('running',200,20)]):
        s.add_usage(10000+i,str(i),{'input_tokens':inp,'output_tokens':out},'m',turn,'high')
    view=s.view(10010)
    view.update(turn_states={'t1':'완료','t2':'완료','running':'진행'},source='user',archived=False)
    view['turn_records']={'t1':{'started_at':10000,'ended_at':10001,'state':'완료'},
                          't2':{'started_at':10002,'ended_at':10002.5,'state':'완료'}}
    a=analyze([view],unit='turn')
    assert len(a['units'])==2 and a['excluded_turns']==1
    assert a['groups'][0]['stats']['total']['value']==72
    assert a['groups'][0]['stats']['reasoning']['value'] is None
    assert analyze([view],unit='response',method='median')['groups'][0]['stats']['input']['value']==60
    # Cutting through a turn excludes it from complete-turn comparisons.
    assert len(analyze([view],start=10001,unit='turn')['units'])==1
    assert not analyze([view],source='subagent')['units']
    assert len(analyze([view],input_band=(0,50))['units'])==2


def test_index_cannot_write_to_source_or_unrelated_database(tmp_path):
    import pytest
    home,path=fixture_home(tmp_path)
    original=(home/'state_5.sqlite').read_bytes()
    with pytest.raises(ValueError):
        UsageIndex([home],home/'state_5.sqlite')
    assert (home/'state_5.sqlite').read_bytes()==original
    other=tmp_path/'other.sqlite'
    with sqlite3.connect(other) as db:
        db.execute('create table important(value text)')
    before=other.read_bytes()
    with pytest.raises(ValueError):
        UsageIndex([home],other)
    assert other.read_bytes()==before


def test_explicit_record_tier_survives_sanitizing_index_and_restart(tmp_path):
    import pytest
    home,path=fixture_home(tmp_path)
    records=[event('turn_context',10000,turn_id='t',model='gpt-6-astra',effort='high'),
             event('token_usage_record',10001,thread_id=TID,turn_id='t',response_id='tier-call',
                   model='gpt-6-astra',service_tier='priority',secret='must-not-persist',
                   usage={'input_tokens':100000,'cached_input_tokens':80000,'cache_write_input_tokens':10000,
                          'output_tokens':2000,'reasoning_output_tokens':1000})]
    path.write_text('\n'.join(map(json.dumps,records))+'\n',encoding='utf8')
    for _ in range(2):
        index=UsageIndex([home],tmp_path/'index.sqlite')
        try:
            snapshot=finish(index)
            rows=analyze(snapshot['sessions'])['responses']
            assert len(rows)==1 and rows[0]['service_tier']=='Fast'
            assert rows[0]['cost']==pytest.approx(1.0125)
            assert not any('must-not-persist' in r[0] for r in index.db.execute('select data from events'))
        finally:index.close()
