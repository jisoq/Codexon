import json
import sqlite3

from cachemonitor.analytics import analyze
from cachemonitor.core import Session, session_lineage
from cachemonitor.index import UsageIndex, sanitized
from cachemonitor.pricing import request_tier
from test_core import TID, event, fixture_home
from test_index import finish

CHILD='12345678-abcd-abcd-abcd-444444444444'


def metadata(tid=CHILD, parent=TID):
    return event('session_meta',9990,id=tid,parent_thread_id=parent,agent_nickname='Lorentz',
        source={'subagent':{'thread_spawn':{'parent_thread_id':parent,'depth':1,'agent_path':'/root/child'}}},
        base_instructions='PRIVATE_SENTINEL')


def settings(ts,tid,tier):
    return event('event_msg',ts,type='thread_settings_applied',thread_id=tid,
                 thread_settings={'service_tier':tier,'instructions':'PRIVATE_SENTINEL'})


def call(ts,tid,turn,response):
    return event('token_usage_record',ts,thread_id=tid,turn_id=turn,response_id=response,
                 usage={'input_tokens':100,'cached_input_tokens':20,'cache_write_input_tokens':0,'output_tokens':10})


def write(path,records,mode='w'):
    with path.open(mode,encoding='utf-8') as stream:
        stream.write(''.join(json.dumps(row)+'\n' for row in records))


def test_child_calls_use_own_applied_settings_and_keep_old_unknown_on_append_restart(tmp_path):
    home,parent_path=fixture_home(tmp_path)
    child_path=home/'child.jsonl'
    write(parent_path,[event('turn_context',10000,turn_id='parent-turn',model='gpt-6-astra'),
                       call(10001,TID,'parent-turn','parent-response')])
    old=[metadata(),settings(9991,TID,'priority'),
         event('turn_context',9992,turn_id='old-child-turn',model='gpt-6-astra'),
         call(9993,CHILD,'old-child-turn','old-child-response')]
    write(child_path,old)
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('insert into threads values(?,?,?,?,?,?,?,?)',
                   (CHILD,str(child_path),10000,None,'PRIVATE_SENTINEL','gpt-6-astra','openai','project'))
    cache=tmp_path/'index.sqlite';index=UsageIndex([home],cache)
    try:
        first=finish(index)
        child=next(s for s in first['sessions'] if s['id']==CHILD)
        assert request_tier(child['history'][0])=='미확인'
        assert child['parent_thread_id']==TID and child['source']=='subagent'
        assert child['agent_path']=='/root/child' and child['spawn_depth']==1
        assert child['title']=='Lorentz'
        old_row=dict(child['history'][0])
        fresh=[settings(10011,CHILD,'default'),
               event('turn_context',10012,turn_id='new-child-turn',model='gpt-6-astra'),
               call(10013,CHILD,'new-child-turn','new-child-response'),
               call(10013,CHILD,'new-child-turn','new-child-response'),
               call(10013,TID,'parent-turn','parent-response')]
        write(child_path,fresh,'a')
        second=finish(index,10014)
        child=next(s for s in second['sessions'] if s['id']==CHILD)
        assert len(child['history'])==2
        assert child['history'][0]==old_row
        current=child['history'][1]
        assert request_tier(current)=='Standard'
        assert current['request_mode_source']=='applied_settings'
        assert current['configured_service_tier']=='default'
        analysis=analyze(second['sessions'])
        assert len(analysis['responses'])==3
        child_current=next(r for r in analysis['responses'] if r['key']=='new-child-response')
        assert child_current['sid']==CHILD and child_current['cost'] is not None
        persisted=''.join(row[0] for row in index.db.execute('select data from metadata'))
        persisted+=''.join(row[0] for row in index.db.execute('select data from events'))
        assert 'PRIVATE_SENTINEL' not in persisted
    finally:index.close()
    index=UsageIndex([home],cache)
    try:
        restored=finish(index,10015)
        child=next(s for s in restored['sessions'] if s['id']==CHILD)
        assert child['parent_thread_id']==TID
        assert [request_tier(r) for r in child['history']]==['미확인','Standard']
        assert len(analyze(restored['sessions'])['responses'])==3
    finally:index.close()


def test_parent_link_never_infers_mode_and_rejects_forged_or_conflicting_ownership():
    child=Session(CHILD,'home')
    child.consume(metadata(),10020)
    child.consume(sanitized(settings(10000,TID,'priority')),10020)
    child.consume(event('turn_context',10001,turn_id='child-turn',model='gpt-6-astra'),10020)
    child.consume(call(10002,CHILD,'child-turn','response'),10020)
    assert child.view(10020)['parent_thread_id']==TID
    assert request_tier(child.view(10020)['history'][0])=='미확인'
    assert session_lineage(metadata()['payload'],'different-child')=={}
    conflict=metadata()['payload'];conflict['parent_thread_id']='different-parent'
    assert session_lineage(conflict,CHILD)=={}
    assert session_lineage(metadata(parent=CHILD)['payload'],CHILD)=={}
