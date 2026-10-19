import json
import pytest

from cachemonitor.core import Session, stamp
from cachemonitor.index import UsageIndex, sanitized
from test_core import event, usage, fixture_home, TID
from test_index import finish


def setting(ts,tier='priority',tid=TID):
    return event('event_msg',ts,type='thread_settings_applied',thread_id=tid,
                 thread_settings={'service_tier':tier,'instructions':'PRIVATE','cwd':'PRIVATE'})


def complete_setting(ts,tid=TID):
    return event('event_msg',ts,type='thread_settings_applied',thread_id=tid,
        thread_settings=dict(model='gpt-6-astra',model_provider_id='openai',approval_policy='never',
            approvals_reviewer='user',permission_profile='disabled',cwd='PRIVATE',
            collaboration_mode={'mode':'default','settings':{'model':'gpt-6-astra','developer_instructions':'PRIVATE'}},
            disabled_plugin_ids=[]))


def test_complete_default_snapshot_prices_future_calls_without_guessing_partial_settings():
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    s=Session(TID,'h')
    partial=event('event_msg',9999,type='thread_settings_applied',thread_id=TID,
                  thread_settings={'model':'gpt-6-astra'})
    assert sanitized(partial) is None
    records=[event('turn_context',10000,turn_id='unknown',model='gpt-6-astra'),usage('unknown',time=10001),
             complete_setting(10002),event('turn_context',10003,turn_id='standard',model='gpt-6-astra'),usage('standard',time=10004),
             setting(10005),event('turn_context',10006,turn_id='fast',model='gpt-6-astra'),usage('fast',time=10007),
             complete_setting(10008),usage('still-fast',time=10009),
             event('turn_context',10010,turn_id='cleared',model='gpt-6-astra'),usage('cleared',time=10011)]
    for record in records:
        if record['type']=='token_usage_record':
            record['payload']['usage']['cache_write_input_tokens']=0
            record['payload']['turn_id']={10001:'unknown',10004:'standard',10007:'fast',10009:'fast',10011:'cleared'}[int(stamp(record['timestamp']))]
        s.consume(sanitized(record),10012)
    assert [r.service_tier for r in s.requests]==['미확인','Standard','priority','priority','Standard']
    assert s.requests[-1].request_mode_source=='applied_settings'
    assert 'PRIVATE' not in json.dumps(sanitized(complete_setting(10002)))
    engine=AnalysisEngine();engine.ingest([s.view(10012)])
    overlay=OverlaySummaries().collect(engine)[0]
    assert overlay['priced']==4 and overlay['latest']['mode']=='Standard'
    assert overlay['latest']['cost'] is not None
    assert sanitized(setting(10012,{}))['payload']['thread_settings']['service_tier']=='미확인'


def test_applied_settings_are_historical_scoped_and_do_not_relabel_active_turn():
    s=Session(TID,'h')
    records=[event('turn_context',10000,turn_id='first',model='m'),
             usage('before',time=10001),setting(10002),
             event('turn_context',10003,turn_id='second',model='m'),usage('fast',time=10004),
             setting(10005,'default','another-thread'),usage('still-fast',time=10006),
             setting(10007,None),event('turn_context',10008,turn_id='third',model='m'),usage('cleared',time=10009)]
    for record in records:
        if record['type']=='token_usage_record':
            record['payload']['turn_id']={10001:'first',10004:'second',10006:'second',10009:'third'}[int(stamp(record['timestamp']))]
        s.consume(sanitized(record),10010)
    assert [r.service_tier for r in s.requests]==['미확인','priority','priority','Standard']
    assert s.requests[-1].request_mode_action=='clear'
    assert s.requests[-1].request_mode_source=='applied_settings'
    assert 'PRIVATE' not in json.dumps(sanitized(setting(10001)))


@pytest.mark.parametrize('snapshot_delay',[0,.001])
def test_turn_context_pairs_with_immediately_following_applied_snapshot(snapshot_delay):
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    s=Session(TID,'h')
    records=[event('turn_context',10000,turn_id='first',model='gpt-6-astra'),
             setting(10000+snapshot_delay,'default'),usage('first',time=10001),
             event('turn_context',10002,turn_id='fast',model='gpt-6-astra'),
             setting(10002+snapshot_delay),usage('fast',time=10003),
             setting(10004,None),usage('still-fast',time=10005),
             event('turn_context',10006,turn_id='override',model='gpt-6-astra',service_tier='priority'),
             complete_setting(10006),usage('override',time=10007)]
    for record in records:
        if record['type']=='token_usage_record':
            record['payload']['turn_id']={10001:'first',10003:'fast',10005:'fast',10007:'override'}[int(stamp(record['timestamp']))]
            record['payload']['usage']['cache_write_input_tokens']=0
        s.consume(sanitized(record),10008)
    assert [r.service_tier for r in s.requests]==['default','priority','priority','priority']
    assert s.requests[0].request_mode_source=='applied_settings'
    assert s.requests[-1].request_mode_source=='turn_context'
    engine=AnalysisEngine();engine.ingest([s.view(10008)])
    overlay=OverlaySummaries().collect(engine)[0]
    assert overlay['priced']==4 and overlay['latest']['mode']=='Fast'
    assert overlay['cost'] is not None


@pytest.mark.parametrize('first_snapshot',[
    setting(10000.001,'priority','another-thread'),
    setting(9999.999,'priority'),
])
def test_snapshot_pairing_rejects_wrong_thread_and_reversed_time(first_snapshot):
    s=Session(TID,'h')
    records=[event('turn_context',10000,turn_id='first',model='gpt-6-astra'),
             first_snapshot,setting(10000.002,'default'),usage('first',time=10001)]
    for record in records:
        if record['type']=='token_usage_record':record['payload']['turn_id']='first'
        s.consume(sanitized(record),10002)
    assert s.requests[0].service_tier=='미확인'
    assert s.requests[0].request_mode_source=='unknown'


@pytest.mark.parametrize('intervening',[
    None,
    event('event_msg',10001.0005,type='user_message',message='PRIVATE'),
    event('response_item',10001.0005,type='message',content='PRIVATE'),
])
def test_index_snapshot_pairing_requires_next_raw_record(tmp_path,monkeypatch,intervening):
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    monkeypatch.setattr('cachemonitor.event_stream.EventLines.CHUNK',32)
    home,path=fixture_home(tmp_path)
    records=[event('turn_context',10001,turn_id='paired',model='gpt-6-astra')]
    if intervening is not None:records.append(intervening)
    records.extend([setting(10001.001,'default'),usage('paired-response',time=10002)])
    records[-1]['payload'].update(turn_id='paired')
    records[-1]['payload']['usage']['cache_write_input_tokens']=0
    path.write_text(''.join(json.dumps(record)+'\n' for record in records),encoding='utf-8')
    cache=tmp_path/'index.sqlite'
    for _ in range(2):
        index=UsageIndex([home],cache)
        try:
            snapshot=finish(index,10010)
            engine=AnalysisEngine();engine.ingest(snapshot['sessions'])
            overlay=OverlaySummaries().collect(engine)[0]
            assert (overlay['latest']['cost'] is not None)==(intervening is None)
            assert overlay['latest']['mode']==('Standard' if intervening is None else '미확인')
            assert all('PRIVATE' not in str(row[0]) for row in index.db.execute('SELECT data FROM events'))
        finally:index.close()


def test_old_index_backfills_applied_settings_without_changing_token_counts(tmp_path,monkeypatch):
    home,path=fixture_home(tmp_path)
    records=[json.loads(line) for line in path.read_text().splitlines()]
    records.extend([complete_setting(10001),event('turn_context',10002,turn_id='tier-turn',model='gpt-6-astra'),
                    event('token_usage_record',10003,thread_id=TID,turn_id='tier-turn',response_id='tier-r',
                          usage={'input_tokens':100,'output_tokens':10})])
    path.write_text(''.join(json.dumps(row)+'\n' for row in records),encoding='utf-8')
    current=sanitized
    with monkeypatch.context() as patch:
        patch.setattr('cachemonitor.index.sanitized',lambda e:None if e.get('payload',{}).get('type')=='thread_settings_applied' else current(e))
        old=UsageIndex([home],tmp_path/'index.sqlite')
        before=next(r for r in finish(old,10010)['sessions'][0]['history'] if r['key']=='tier-r')
        assert before['service_tier']=='미확인'
        old.db.execute('pragma user_version=4');old.db.commit();old.close()
    new=UsageIndex([home],tmp_path/'index.sqlite')
    try:
        after=next(r for r in finish(new,10010)['sessions'][0]['history'] if r['key']=='tier-r')
        assert after['service_tier']=='Standard'
        assert (after['input'],after['output'])==(before['input'],before['output'])
        assert new.db.execute('pragma user_version').fetchone()[0]==6
    finally:new.close()
