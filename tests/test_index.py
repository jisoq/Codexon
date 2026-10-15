import json
import sqlite3
from pathlib import Path
import pytest

from cachemonitor.index import UsageIndex
from cachemonitor.event_store import EventStore, pack_event, unpack_event
from cachemonitor.analytics import analyze
from test_core import event, usage, fixture_home, TID


def _interrupted_event_migration(path, control, point):
    import cachemonitor.event_store as storage
    storage.MIGRATION_ROWS=2
    db=sqlite3.connect(path);db.execute('PRAGMA journal_mode=WAL')
    store=EventStore(db,path)
    def stop(sql):
        if (point=='copy' and sql.startswith('DELETE FROM events_legacy WHERE rowid=') or
                point=='finish' and sql=='DROP TABLE events_legacy'):
            control.send(point);control.recv()
    db.set_trace_callback(stop)
    try:
        while store.pending:
            store.migrate()
            if point=='committed':break
        if point=='committed':control.send(point);control.recv()
    finally:db.close();control.close()


@pytest.mark.integration
@pytest.mark.parametrize('point',['copy','committed','finish'])
def test_event_packing_recovers_after_kill_and_preserves_replacement_order(tmp_path,monkeypatch,point):
    import multiprocessing as mp
    from contextlib import closing
    from types import SimpleNamespace
    import cachemonitor.event_store as storage
    path=tmp_path/'legacy.sqlite'
    rows=[('retained',n,'home','session',100+n+.125,json.dumps({'seq':n,'text':'기록'*128},ensure_ascii=False)) for n in range(6)]
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE events(path TEXT,pos INTEGER,home TEXT,tid TEXT,ts REAL,data TEXT,PRIMARY KEY(path,pos))')
        db.executemany('INSERT INTO events VALUES(?,?,?,?,?,?)',rows)
    context=mp.get_context('spawn');parent,pipe=context.Pipe()
    child=context.Process(target=_interrupted_event_migration,args=(str(path),pipe,point))
    child.start();pipe.close()
    try:
        assert parent.poll(15),f'event migration did not reach interruption; exit={child.exitcode}'
        assert parent.recv()==point
        child.kill();child.join(10)
        assert not child.is_alive() and child.exitcode!=0
        with closing(sqlite3.connect(path)) as db:
            store=EventStore(db,path);before=store.upper()
            actual=lambda:[(*r[:5],unpack_event(r[5])) for r in db.execute('SELECT * FROM events ORDER BY path,pos')]
            assert actual()==rows
            replacement=('retained',3,'other','other-session',150.75,rows[3][-1])
            extra=('retained',99,'home','session',199.25,rows[0][-1])
            with db:
                for row in (replacement,extra):db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?)',(*row[:5],pack_event(row[5])))
            rows[3]=replacement;rows.append(extra)
            assert store.upper()>before
            assert db.execute('SELECT count(*) FROM event_rows WHERE rowid<=?',(before,)).fetchone()[0]==5
            with monkeypatch.context() as patch:
                patch.setattr(storage.shutil,'disk_usage',lambda path:SimpleNamespace(free=0))
                assert store.migrate()==0 and actual()==rows
            monkeypatch.setattr(storage,'MIGRATION_ROWS',2)
            for _ in range(5):store.migrate()
            assert not store.legacy and not store.pending
            assert actual()==rows
            assert list(store.owners(['home','other']))==[('home','session'),('other','other-session')]
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:
        if child.is_alive():child.kill()
        child.join(10);parent.close()


def finish(index, now=10010):
    for _ in range(100):
        result = index.poll(now)
        if not result['index']['loading']:
            return result
    raise AssertionError('index did not finish')


@pytest.mark.parametrize('legacy',[False,True])
def test_stored_replay_preserves_source_and_supports_both_event_formats(tmp_path,legacy):
    from contextlib import closing
    home,log=fixture_home(tmp_path);path=tmp_path/'index.sqlite'
    fields=('home','sid','key','ts','model','service_tier','input','cached','written','output','reasoning','cost')
    def values(sessions):return [tuple(row.get(key) for key in fields) for row in analyze(sessions)['responses']]
    index=UsageIndex([home],path)
    try:expected=values(finish(index)['sessions'])
    finally:index.close()
    if legacy:
        destination=tmp_path/'legacy.sqlite'
        with closing(sqlite3.connect(path)) as source,closing(sqlite3.connect(destination)) as target,target:
            for name in ('files','metadata','usage_archive'):
                target.execute(source.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0])
                rows=source.execute('SELECT * FROM '+name).fetchall()
                if rows:target.executemany('INSERT INTO '+name+' VALUES('+','.join('?' for _ in rows[0])+')',rows)
            target.execute('CREATE TABLE events(path TEXT,pos INTEGER,home TEXT,tid TEXT,ts REAL,data TEXT,PRIMARY KEY(path,pos))')
            target.executemany('INSERT INTO events VALUES(?,?,?,?,?,?)',
                [(*row[:5],unpack_event(row[5])) for row in source.execute('SELECT * FROM events')])
        path=destination
    log.unlink();before=path.read_bytes()
    reader=UsageIndex.for_replay(path)
    try:
        for key in reader.event_store.owners().fetchall():reader.rebuild(key,10010)
        assert values([session.view(10010) for session in reader.monitor.sessions.values()])==expected
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            reader.db.execute("UPDATE metadata SET data='{}'")
        with pytest.raises(ValueError,match='읽기 전용'):reader.poll(10010)
    finally:reader.close()
    assert path.read_bytes()==before


def test_large_unfinished_event_obeys_read_budget_and_resumes_without_idle_rereads(tmp_path):
    from cachemonitor.index import FILE_READ_BUDGET
    home,path=fixture_home(tmp_path);index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        finish(index)
        record=usage('large',time=10011)
        prefix=json.dumps({k:v for k,v in record.items() if k!='payload'})[:-1]+',"payload":{"ignored":"'
        with path.open('ab') as stream:
            stream.write(prefix.encode())
            for _ in range(32):stream.write(b'x'*(1024*1024))
        reads=[]
        for _ in range(8):
            before=index.bytes_read;index.scan_file(str(path),home,TID)
            reads.append(index.bytes_read-before)
            if str(path) in index.partial_work:break
        assert max(reads)<=FILE_READ_BUDGET
        assert sum(reads)>=32*1024*1024
        before=index.bytes_read
        for _ in range(3):index.scan_file(str(path),home,TID)
        assert index.bytes_read==before
        with path.open('ab') as stream:
            stream.write(('",'+json.dumps(record['payload'])[1:]+'}\n').encode())
        result=finish(index,10012)
        assert [r['key'] for r in analyze(result['sessions'])['responses']]==['r1','large']
        assert not result['errors']
    finally:index.close()


def test_large_restart_replay_yields_between_event_batches(tmp_path,monkeypatch):
    import cachemonitor.index as implementation
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    with path.open('a',encoding='utf8') as stream:
        for n in range(24):stream.write(json.dumps(usage(f'r{n+2}',time=10001+n))+'\n')
    index=UsageIndex([home],cache)
    try:expected=analyze(finish(index,10030)['sessions'])['totals']
    finally:index.close()
    index=UsageIndex([home],cache);clock=[0.];consumed=[]
    monkeypatch.setattr(implementation,'REBUILD_BATCH',4)
    monkeypatch.setattr(implementation.time,'monotonic',lambda:clock[0])
    original=implementation.Session.consume
    def consume(session,event,now):
        original(session,event,now);consumed.append(event);clock[0]+=.12
    monkeypatch.setattr(implementation.Session,'consume',consume)
    try:
        partial=index.poll(10030)
        assert partial['index']['loading'] and not partial['usage_collection_complete']
        assert len(consumed)==4
        actual=finish(index,10030)
        assert actual['usage_collection_complete']
        assert analyze(actual['sessions'])['totals']==expected
        assert len(consumed)==25
    finally:index.close()


def test_restart_restores_retained_usage_after_source_removal_without_crossing_home_scope(tmp_path):
    folders=[tmp_path/'selected',tmp_path/'other']
    for folder in folders:folder.mkdir()
    selected,path=fixture_home(folders[0]);other,_=fixture_home(folders[1])
    cache=tmp_path/'index.sqlite'
    index=UsageIndex([selected,other],cache)
    try:
        before=finish(index)
        expected=next(session for session in before['sessions'] if session['home']==str(selected.resolve()))
        retained=list(index.db.execute('SELECT path,pos,home,tid,ts,data FROM events ORDER BY path,pos'))
    finally:index.close()
    path.unlink()
    index=UsageIndex([selected],cache)
    try:
        restored=finish(index,10030)
        assert {(session['home'],session['id']) for session in restored['sessions']}=={(str(selected.resolve()),TID)}
        actual=restored['sessions'][0]
        assert [(row['key'],row['ts'],row['total']) for row in actual['history']]==[
            (row['key'],row['ts'],row['total']) for row in expected['history']]
        assert actual['activity']==expected['activity']
        assert restored['usage_collection_complete']
        assert list(index.db.execute('SELECT path,pos,home,tid,ts,data FROM events ORDER BY path,pos'))==retained
    finally:index.close()


@pytest.mark.parametrize('retained_scope',['unrelated','child','unknown'])
def test_retained_restart_recovery_uses_poll_budget_once_and_reports_partial_loading(tmp_path,monkeypatch,retained_scope):
    import cachemonitor.index as implementation
    home,path=fixture_home(tmp_path);paths=[path]
    for number in (1,2):
        tid=f'8765432{number}-abcd-abcd-abcd-123456789012'
        source=home/f'old-{number}.jsonl'
        header=([dict(type='session_meta',payload=dict(id=tid,parent_thread_id=TID))]
                if retained_scope=='child' and number==1 else [])
        source.write_text(''.join(json.dumps(row)+'\n' for row in [*header,usage(f'old-{number}',tid=tid)]),encoding='utf8')
        paths.append(source)
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',
                       (tid,str(source),10000,'old','old','test','openai','project'))
    cache=tmp_path/'index.sqlite';index=UsageIndex([home],cache)
    try:assert len(analyze(finish(index)['sessions'])['responses'])==3
    finally:index.close()
    for source in paths:source.unlink()
    if retained_scope=='unknown':
        with sqlite3.connect(cache) as db:db.execute('DELETE FROM metadata WHERE tid=?',(tid,))
        with sqlite3.connect(home/'state_5.sqlite') as db:db.execute('DELETE FROM threads WHERE id=?',(tid,))
    index=UsageIndex([home],cache);clock=[0.];rebuilt=[];rebuild=index.rebuild
    monkeypatch.setattr(implementation.time,'monotonic',lambda:clock[0])
    def measured(key,now):
        rebuild(key,now);rebuilt.append(key);clock[0]+=.41
    monkeypatch.setattr(index,'rebuild',measured)
    try:
        partial=index.poll(10030)
        assert len(rebuilt)==1 and partial['index']['loading']
        assert not partial['usage_collection_complete']
        assert partial['sessions'][0]['collection_complete'] is (retained_scope=='unrelated')
        complete=finish(index,10030)
        assert len(analyze(complete['sessions'])['responses'])==3
        assert len(rebuilt)==len(set(rebuilt))==3
        assert complete['usage_collection_complete']
        assert all(session['collection_complete'] for session in complete['sessions'])
        index.poll(10031)
        assert len(rebuilt)==3
    finally:index.close()


def test_restart_validates_rewritten_active_source_before_retained_recovery(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    index=UsageIndex([home],cache)
    try:assert [row['key'] for row in analyze(finish(index)['sessions'])['responses']]==['r1']
    finally:index.close()
    path.write_text(json.dumps(usage('corrected',time=10015,input=20000,cached=10000))+'\n',encoding='utf8')
    index=UsageIndex([home],cache)
    try:
        restored=finish(index,10030)
        assert [row['key'] for row in analyze(restored['sessions'])['responses']]==['corrected']
        assert len(restored['sessions'])==1
    finally:index.close()


def test_retained_recovery_progresses_when_source_scans_exhaust_each_poll_and_discovery_is_due(tmp_path,monkeypatch):
    import cachemonitor.index as implementation
    home,_=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    tid='87654321-abcd-abcd-abcd-123456789012';removed=home/'retained.jsonl'
    removed.write_text(json.dumps(usage('retained-only',tid=tid))+'\n',encoding='utf8')
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',
                   (tid,str(removed),10000,'old','old','test','openai','project'))
    index=UsageIndex([home],cache)
    try:finish(index)
    finally:index.close()
    removed.unlink();index=UsageIndex([home],cache)
    clock=[0.];scans=[];discoveries=[];scan=index.scan_file;discover=index.discover
    monkeypatch.setattr(implementation.time,'monotonic',lambda:clock[0])
    def expensive_scan(*args):
        value=scan(*args);scans.append(args[0]);clock[0]+=.41;return value
    def discovery(now):discoveries.append(now);return discover(now)
    monkeypatch.setattr(index,'scan_file',expensive_scan)
    monkeypatch.setattr(index,'discover',discovery)
    try:
        index.poll(10030);index.poll(10030)
        key=(str(home.resolve()),tid)
        assert key not in index.loaded and index.cached_restore
        progress=index.poll(10041)
        assert discoveries==[10030,10041]
        assert key in index.loaded
        assert any(row['key']=='retained-only' for session in progress['sessions'] for row in session['history'])
        complete=finish(index,10041)
        assert complete['usage_collection_complete']
        assert len(analyze(complete['sessions'])['responses'])==2
    finally:index.close()


def test_owned_publication_tokens_cover_metadata_evidence_and_window_expiry(tmp_path):
    from cachemonitor.core import WINDOW
    from cachemonitor.model_evidence import EvidenceStore
    from cachemonitor.usage_collection import LiveCollection
    home,_=fixture_home(tmp_path);evidence=EvidenceStore(tmp_path/'evidence.sqlite')
    index=UsageIndex([home],tmp_path/'index.sqlite',evidence.path)
    live=LiveCollection('', '')
    try:
        first=finish(index);key=(str(home.resolve()),TID)
        original=index.collection_changes.sessions[key]
        candidate=live.prepare(first,'owner',1,changes=index.collection_changes);live.publish(candidate)
        unchanged=index.poll(10011)
        assert index.collection_changes.sessions[key]==original
        current=live.prepare(unchanged,'owner',2,changes=index.collection_changes)
        assert current['sessions'][key] is candidate['sessions'][key]
        # Metadata not used by the analysis revision must still reach publication.
        index.metadata[key]['project_id']='new-project'
        metadata=index.poll(10012)
        assert index.collection_changes.sessions[key]!=original
        updated=live.prepare(metadata,'owner',3,changes=index.collection_changes)
        assert updated['sessions'][key] is not candidate['sessions'][key]
        previous=index.collection_changes.sessions[key]
        response=metadata['sessions'][0]['history'][0]['key']
        evidence.write(home,'attempt',10012,'WebSocket',response,'asked','responded','completed')
        joined=index.poll(10013)
        assert index.collection_changes.sessions[key]!=previous
        assert joined['sessions'][0]['history'][0]['requested_model']=='asked'
        previous=index.collection_changes.sessions[key]
        expired=index.poll(10014+WINDOW)
        assert not expired['sessions'][0]['requests']
        assert index.collection_changes.sessions[key]!=previous
        assert len(index.publication_rows)==len(expired['sessions'])
    finally:index.close();evidence.close()


def test_publication_owner_does_not_retain_a_closed_index(tmp_path):
    import gc
    import weakref
    from cachemonitor.usage_collection import LiveCollection
    home,_=fixture_home(tmp_path);index=UsageIndex([home],tmp_path/'index.sqlite')
    live=LiveCollection('', '')
    try:
        snapshot=finish(index)
        live.publish(live.prepare(snapshot,'owner',1,changes=index.collection_changes))
    finally:index.close()
    reference=weakref.ref(index)
    del index
    gc.collect()
    assert reference() is None and live.current['sessions']


def test_prefix_validation_priority_is_bounded_and_preserves_rewrite_detection(tmp_path,monkeypatch):
    import cachemonitor.index as implementation
    monkeypatch.setattr(implementation,'FILE_READ_BUDGET',1024)
    home,path=fixture_home(tmp_path)
    with path.open('ab') as stream:stream.write((b'x'*255+b'\n')*24)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        finish(index)
        with path.open('ab') as stream:stream.write((json.dumps(usage('after',time=10011))+'\n').encode())
        target=(str(path),home,TID);waiting=('waiting.jsonl',home,TID)
        index.queue.append(waiting);index.prefix_priority=2048
        assert index.scan_file(*target) is None
        assert index.queue.popleft()==target
        assert index.scan_file(*target) is None
        assert index.queue.popleft()==target
        assert index.scan_file(*target) is None
        assert index.queue.popleft()==waiting and index.queue.popleft()==target
        path.write_bytes(path.read_bytes().replace(b'r1',b'r9')+b'\n')
        result=finish(index,10012)
        assert [row['key'] for row in result['sessions'][0]['history']]==['r9','after']
    finally:index.close()


def test_growing_file_finishes_restarted_known_prefix_and_reads_new_usage(tmp_path,monkeypatch):
    import cachemonitor.index as implementation
    monkeypatch.setattr(implementation,'FILE_READ_BUDGET',1024)
    home,path=fixture_home(tmp_path)
    with path.open('ab') as stream:stream.write((b'x'*255+b'\n')*24)
    index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        finish(index)
        with path.open('ab') as stream:stream.write((json.dumps(usage('after',time=10011))+'\n').encode())
        target=(str(path),home,TID)
        assert index.scan_file(*target) is None
        # Another append arrives before the remaining prefix slice is checked.
        with path.open('ab') as stream:stream.write(b'\n')
        index.prefix_priority=implementation.PREFIX_PRIORITY_BUDGET
        before=index.bytes_read
        assert index.scan_file(*target)==(str(home),TID)
        assert index.bytes_read-before <= implementation.PREFIX_PRIORITY_BUDGET+1024
        index.rebuild((str(home),TID),10012)
        assert [row['key'] for row in index.monitor.sessions[(str(home),TID)].view(10012)['history']]==['r1','after']
    finally:index.close()


@pytest.mark.parametrize('restart',(False,True))
def test_continuous_append_beyond_priority_budget_progresses_and_rechecks_rewrites(tmp_path,monkeypatch,restart):
    import cachemonitor.index as implementation
    monkeypatch.setattr(implementation,'FILE_READ_BUDGET',1024)
    monkeypatch.setattr(implementation,'PREFIX_PRIORITY_BUDGET',4096)
    home,path=fixture_home(tmp_path)
    with path.open('ab') as stream:stream.write((b'x'*255+b'\n')*32)
    cache=tmp_path/'index.sqlite';index=UsageIndex([home],cache)
    try:
        finish(index)
        with path.open('ab') as stream:stream.write((json.dumps(usage('after',time=10011))+'\n').encode())
        target=(str(path),home,TID)
        index.prefix_priority=implementation.PREFIX_PRIORITY_BUDGET
        before=index.bytes_read
        assert index.scan_file(*target) is None
        assert index.bytes_read-before==1024
        # Change an already visited part of this pass, then keep appending so
        # the completed hash alone cannot prove that the prefix is current.
        path.write_bytes(path.read_bytes().replace(b'r1',b'r9')+b'\n')
        seen=False
        for _ in range(24):
            with path.open('ab') as stream:stream.write(b'\n')
            index.prefix_priority=implementation.PREFIX_PRIORITY_BUDGET
            before=index.bytes_read
            changed=index.scan_file(*index.queue.popleft())
            assert index.bytes_read-before<=implementation.PREFIX_PRIORITY_BUDGET+1024
            assert len(index.queue)==1 and len(index.source_work)==1
            if changed:
                index.rebuild(changed,10012)
                seen=any(row['key']=='after' for row in index.monitor.sessions[(str(home),TID)].view(10012)['history'])
            if seen:break
        assert seen
        # Pending verification must survive both the live queue and restart
        # after the tentative rows and file metadata have been committed.
        if restart:
            index.db.commit();index.close();index=UsageIndex([home],cache)
        result=finish(index,10012)
        assert result['usage_collection_complete'] and not result['index']['loading']
        assert [row['key'] for row in result['sessions'][0]['history']]==['r9','after']
        assert not index.source_work
    finally:index.close()


def test_new_thread_and_late_rollout_path_wake_discovery_without_repeated_idle_scans(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path);index=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        finish(index,10010);scans=[];discover=index.discover
        def observed(now):scans.append(now);return discover(now)
        monkeypatch.setattr(index,'discover',observed)
        index.poll(10011)
        assert not scans
        sid='new-session';path=home/'new.jsonl';value=usage('new',time=10011)
        value['payload']['thread_id']=sid
        path.write_text(json.dumps(value)+'\n',encoding='utf-8')
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)',(sid,'',10011,'new','','test','openai','project'))
        index.poll(10012)
        assert scans==[10012]
        with sqlite3.connect(home/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(path),sid))
        result=index.poll(10013)
        assert scans==[10012,10013]
        assert next(s for s in result['sessions'] if s['id']==sid)['history'][0]['key']=='new'
        index.poll(10014)
        assert scans==[10012,10013]
    finally:index.close()


def test_top_level_compaction_is_indexed_and_backfilled_without_private_payload(tmp_path):
    home,path=fixture_home(tmp_path);cache=tmp_path/'index.sqlite'
    with path.open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(event('compacted',10001,message='PRIVATE_COMPACTION_TEXT'))+'\n')
        stream.write(json.dumps(usage('after',time=10002))+'\n')
    for legacy in (False,True):
        if legacy:
            with sqlite3.connect(cache) as db:
                for name,pos,data in db.execute('SELECT path,pos,data FROM events').fetchall():
                    if json.loads(unpack_event(data)).get('type')=='compacted':
                        db.execute('DELETE FROM events WHERE path=? AND pos=?',(name,pos))
                db.execute('PRAGMA user_version=3')
        index=UsageIndex([home],cache)
        try:
            value=finish(index)
            history=analyze(value['sessions'])['responses']
            assert next(r for r in history if r['key']=='after')['compaction_epoch']==1
            stored=''.join(unpack_event(r[0]) for r in index.db.execute('SELECT data FROM events'))
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
    assert 'PRIVATE_PROMPT_SENTINEL' not in ''.join(unpack_event(row[0]) for row in db.execute('select data from events'))
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


def test_usage_revision_preserves_corrections_to_sanitized_token_evidence(tmp_path):
    from cachemonitor.analysis_engine import AnalysisEngine
    home,path=fixture_home(tmp_path)
    item=usage();item['payload'].update(model='gpt-6-astra',service_tier='standard')
    item['payload']['usage']['cache_write_input_tokens']=20000
    path.write_text(json.dumps(item)+'\n',encoding='utf8')
    index=UsageIndex([home],tmp_path/'index.sqlite');engine=AnalysisEngine()
    try:
        before=finish(index)['sessions'];engine.ingest(before)
        assert before[0]['history'][0]['written'] is None
        assert before[0]['history'][0]['input_conflict']
        item['payload']['usage']['cache_write_input_tokens']=None
        path.write_text(json.dumps(item)+'\n',encoding='utf8')
        after=finish(index,10020)['sessions']
        assert after[0]['history'][0]['written'] is None
        assert not after[0]['history'][0]['input_conflict']
        assert engine.ingest(after)
        row=engine.record(after[0]['home'],after[0]['id'],'r1')
        assert row['input_conflict'] is False
        assert row['cost']==analyze(after)['responses'][0]['cost']
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
        assert len(first['requests'])==1 and expired['requests']==[]
        assert first['usage_revision']==expired['usage_revision']
        row['input']=200
        index.db.execute('UPDATE usage_archive SET data=?',(json.dumps(row),));index.db.commit()
        changed=index.archive.views(10011,{str(home)})[0]
        assert len(queries)==2 and changed['history'][0]['total']==220
        assert changed['usage_revision']!=first['usage_revision']
        with sqlite3.connect(index.path) as other:
            row['input']=300
            other.execute('UPDATE usage_archive SET data=?',(json.dumps(row),))
        changed=index.archive.views(10011,{str(home)})[0]
        assert len(queries)==3 and changed['history'][0]['total']==320
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
        assert index.archive.views(10010,{str(home)})[0]['history'][0]['total']==120
        row['input']=200
        index.db.execute('UPDATE usage_archive SET data=?',(json.dumps(row),))
        assert index.archive.views(10010,{str(home)})[0]['history'][0]['total']==220
        index.db.rollback()
        row['input']=300
        index.db.execute('UPDATE usage_archive SET data=?',(json.dumps(row),));index.db.commit()
        assert index.archive.views(10010,{str(home)})[0]['history'][0]['total']==320
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
    for number,corrupted in enumerate(('{','[]',pack_event(json.dumps({'value':'x'*1024}))[:-1])):
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
            assert not any('must-not-persist' in unpack_event(r[0]) for r in index.db.execute('select data from events'))
        finally:index.close()
