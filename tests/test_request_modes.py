import json
import sqlite3
from cachemonitor.request_modes import request_mode
from cachemonitor.quota_cycles import QuotaLedger

TURN='019-test-turn-123456789'


def body(mode='priority', prompt='', start='None'):
    tier=f'Some(Some("{mode}"))' if mode else 'None'
    return (f'Submission sub=Submission {{ id: "{TURN}", op: TurnInput {{ request: TurnInputRequest {{ '
            f'input: UserInput {{ text: {json.dumps(prompt)} }}, thread_settings: ThreadSettingsOverrides {{ '
            f'service_tier: {tier} }}, start: TurnStartOptions {{ service_tier: {start} }} }} }} }}')


def test_only_explicit_structural_tier_and_start_override():
    assert request_mode(body())==(TURN,'Fast')
    assert request_mode(body('default'))==(TURN,'Standard')
    assert request_mode(body(None)) is None
    assert request_mode(body(start='Some("default")'))==(TURN,'Standard')
    assert request_mode(body(start='Some("future-tier")'))==(TURN,'future-tier')
    fake='ThreadSettingsOverrides { service_tier: Some(Some("priority")) }'
    assert request_mode(body(None,prompt=fake)) is None
    assert request_mode(json.dumps(body())) is None
    assert request_mode('service_tier: Some("priority")') is None


def test_enrichment_requires_same_home_session_and_turn_and_persists(tmp_path):
    home=str(tmp_path)
    db=sqlite3.connect(tmp_path/'logs_2.sqlite')
    db.execute('create table logs(id integer primary key, thread_id text, target text, feedback_log_body text)')
    db.execute('insert into logs values(1,?,?,?)',('s','codex_core::session::handlers',body()))
    db.commit(); db.close()
    def snapshot():
        return {'homes':[home], 'sessions':[
            {'home':home,'id':sid,'usage_revision':1,'history':[{'turn':turn}]}
            for sid,turn in [('s',TURN),('other',TURN),('s','unmatched')]]}
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    first=snapshot();ledger.enrich_modes(first)
    assert [s['history'][0]['service_tier'] for s in first['sessions']]==['Fast','미확인','미확인']
    second=snapshot();ledger.enrich_modes(second)
    assert [s['usage_revision'] for s in first['sessions']]==[s['usage_revision'] for s in second['sessions']]
    ledger.close()
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    restored=snapshot();ledger.enrich_modes(restored)
    assert restored['sessions'][0]['history'][0]['service_tier']=='Fast'
    ledger.close()
