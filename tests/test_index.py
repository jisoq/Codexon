import json
import sqlite3
from pathlib import Path

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
            assert rows[0]['cost']==pytest.approx(.81)
            assert not any('must-not-persist' in r[0] for r in index.db.execute('select data from events'))
        finally:index.close()
