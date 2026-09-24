import json

from cachemonitor.core import Session, stamp
from cachemonitor.index import UsageIndex, sanitized
from test_core import event, usage, fixture_home, TID
from test_index import finish


def setting(ts,tier='priority',tid=TID):
    return event('event_msg',ts,type='thread_settings_applied',thread_id=tid,
                 thread_settings={'service_tier':tier,'instructions':'PRIVATE','cwd':'PRIVATE'})


def test_applied_settings_are_historical_scoped_and_do_not_relabel_active_turn():
    s=Session(TID,'h')
    records=[event('turn_context',10000,turn_id='first',model='m'),
             setting(10001),usage('before',time=10002),
             event('turn_context',10003,turn_id='second',model='m'),usage('fast',time=10004),
             setting(10005,'default','another-thread'),usage('still-fast',time=10006),
             setting(10007,None),event('turn_context',10008,turn_id='third',model='m'),usage('cleared',time=10009)]
    for record in records:
        if record['type']=='token_usage_record':
            record['payload']['turn_id']={10002:'first',10004:'second',10006:'second',10009:'third'}[int(stamp(record['timestamp']))]
        s.consume(sanitized(record),10010)
    assert [r.service_tier for r in s.requests]==['미확인','priority','priority','Standard']
    assert s.requests[-1].request_mode_action=='clear'
    assert s.requests[-1].request_mode_source=='applied_settings'
    assert 'PRIVATE' not in json.dumps(sanitized(setting(10001)))


def test_old_index_backfills_applied_settings_without_changing_token_counts(tmp_path,monkeypatch):
    home,path=fixture_home(tmp_path)
    records=[json.loads(line) for line in path.read_text().splitlines()]
    records.extend([setting(10001),event('turn_context',10002,turn_id='tier-turn',model='m'),
                    event('token_usage_record',10003,thread_id=TID,turn_id='tier-turn',response_id='tier-r',
                          usage={'input_tokens':100,'output_tokens':10})])
    path.write_text(''.join(json.dumps(row)+'\n' for row in records),encoding='utf-8')
    current=sanitized
    with monkeypatch.context() as patch:
        patch.setattr('cachemonitor.index.sanitized',lambda e:None if e.get('payload',{}).get('type')=='thread_settings_applied' else current(e))
        old=UsageIndex([home],tmp_path/'index.sqlite')
        before=next(r for r in finish(old,10010)['sessions'][0]['history'] if r['key']=='tier-r')
        assert before['service_tier']=='미확인'
        old.db.execute('pragma user_version=0');old.db.commit();old.close()
    new=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        after=next(r for r in finish(new,10010)['sessions'][0]['history'] if r['key']=='tier-r')
        assert after['service_tier']=='priority'
        assert (after['input'],after['output'])==(before['input'],before['output'])
    finally:new.close()
