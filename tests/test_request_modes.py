import json
import sqlite3
import copy
import pytest
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


def test_mode_enrichment_reuses_unchanged_rows_and_replays_corrections(tmp_path):
    home=str(tmp_path/'home')
    raw=[{'turn':'same','service_tier':'Standard'},
         {'turn':'changed','service_tier':'Standard','service_tier_source':'settings'}]
    snapshot={'homes':[],'sessions':[{'home':home,'id':'s','usage_revision':1,'history':raw}]}
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    key=(home,'s','changed')
    try:
        ledger.modes[key]='Fast';ledger.mode_revisions[key[:2]]=1
        ledger.enrich_modes(snapshot)
        enriched=snapshot['sessions'][0]['history']
        assert enriched[0] is raw[0]
        assert enriched[1] is not raw[1] and enriched[1]['service_tier']=='Fast'
        assert raw[1]['service_tier']=='Standard'
        ledger.enrich_modes(snapshot)
        assert snapshot['sessions'][0]['history'] is enriched
        assert snapshot['sessions'][0]['usage_revision']==(1,1)
        ledger.modes.pop(key);ledger.mode_revisions[key[:2]]=2
        ledger.enrich_modes(snapshot)
        assert snapshot['sessions'][0]['history'][1] is raw[1]
        assert snapshot['sessions'][0]['usage_revision']==(1,2)
        assert enriched[1]['service_tier']=='Fast'  # Existing views remain stable.
    finally:ledger.close()


def mode_session(sid,mode='미확인',at=120,parent=None,home='h',**row_fields):
    from cachemonitor.core import Session
    session=Session(sid,home,title=sid)
    session.add_usage(at,sid+'-response',dict(input_tokens=100,cached_input_tokens=80,
        cache_write_input_tokens=0,output_tokens=20,reasoning_output_tokens=5),
        'gpt-6-astra',sid+'-turn','high',service_tier=mode)
    result=session.view(400)
    result.update(usage_revision=1,parent_thread_id=parent,collection_complete=True)
    result['history'][0].update(row_fields)
    return result


@pytest.mark.parametrize('mode,multiplier',[('Standard',1),('Fast',2.5)])
def test_parent_mode_prices_nested_children_in_overlay_and_ledger(tmp_path,mode,multiplier):
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    parent=mode_session('parent',mode,100)
    child=mode_session('child',parent='parent')
    grandchild=mode_session('grandchild',at=130,parent='child')
    snapshot=dict(homes=[],sessions=[grandchild,child,parent],ts=400,index={'loading':False})
    original=copy.deepcopy(snapshot)
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        ledger.enrich_modes(snapshot)
        engine=AnalysisEngine();engine.ingest(snapshot['sessions'])
        ledger.sync(engine,snapshot)
        summary=next(s for s in OverlaySummaries().collect(engine) if s['id']=='parent')
        assert (summary['calls'],summary['priced'],summary['missing'])==(3,3,0)
        assert summary['cost']==pytest.approx(3*.00128*multiplier)
        assert all(r['cost']==pytest.approx(.00128*multiplier) for r in summary['recent'])
        for sid in ('child','grandchild'):
            row=engine.sessions[('h',sid)]['prepared']['history'][0]
            assert row['service_tier']==mode and row['request_mode_source']=='parent'
            stored=ledger.db.execute('select cost,service_tier from calls where sid=?',(sid,)).fetchone()
            assert stored['cost']==pytest.approx(.00128*multiplier) and stored['service_tier']==mode
        assert [s['history'][0]['service_tier'] for s in original['sessions']]==['미확인','미확인',mode]
        assert child['history'][0]['service_tier']=='미확인'
    finally:ledger.close()


def test_parent_inheritance_preserves_own_modes_conflicts_and_scope(tmp_path):
    sessions=[mode_session('parent','Fast',100),
              mode_session('known','Standard',parent='parent'),
              mode_session('wire',parent='parent',service_tier_source='wire',requested_service_tier='default'),
              mode_session('conflict',parent='parent',mode_conflict=True),
              mode_session('unsupported','future-tier',parent='parent'),
              mode_session('orphan',parent='missing'),
              mode_session('other-home',parent='parent',home='other'),
              mode_session('cycle-a','Standard',100,parent='cycle-b'),
              mode_session('cycle-b',parent='cycle-a'),
              mode_session('cycle-child',parent='cycle-a'),
              mode_session('self',parent='self')]
    snapshot=dict(homes=[],sessions=sessions)
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        ledger.enrich_modes(snapshot)
        rows={s['id']:s['history'][0] for s in snapshot['sessions']}
        assert rows['known']['service_tier']==rows['wire']['service_tier']=='Standard'
        assert rows['wire']['service_tier_source']=='wire'
        assert rows['unsupported']['service_tier']=='future-tier'
        for sid in ('conflict','orphan','other-home','cycle-b','cycle-child','self'):
            assert rows[sid]['service_tier']=='미확인'
    finally:ledger.close()


def test_parent_mode_uses_historical_request_time_and_keeps_unknown_boundaries(tmp_path):
    parent=mode_session('parent','Standard',150)
    parent['turn_records']={'parent-turn':{'started_at':100},'fast':{'started_at':200}}
    parent['history'] += [dict(parent['history'][0],key='fast',turn='fast',ts=250,service_tier='Fast'),
                          dict(parent['history'][0],key='unknown',turn='unknown',ts=300,service_tier='미확인')]
    sessions=[parent]+[mode_session(str(at),at=350,parent='parent',request_observed_at=at)
                       for at in (90,100,190,210,310)]
    snapshot=dict(homes=[],sessions=sessions)
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        ledger.enrich_modes(snapshot)
        assert [s['history'][0]['service_tier'] for s in snapshot['sessions'][1:]]==[
            '미확인','Standard','Standard','Fast','미확인']
    finally:ledger.close()


def test_late_parent_evidence_reprices_children_and_reuses_unchanged_history(tmp_path):
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    parent=mode_session('parent',at=100,service_tier_source='settings')
    child=mode_session('child',parent='parent')
    snapshot=dict(homes=[],sessions=[child,parent])
    ledger=QuotaLedger(tmp_path/'ledger.sqlite');engine=AnalysisEngine();overlays=OverlaySummaries()
    key=('h','parent','parent-turn')
    try:
        ledger.enrich_modes(snapshot);engine.ingest(snapshot['sessions'])
        assert next(s for s in overlays.collect(engine) if s['id']=='parent')['priced']==0
        for revision,mode in enumerate(('Standard','Fast','미확인'),1):
            ledger.modes[key]=mode;ledger.mode_revisions[key[:2]]=revision
            ledger.enrich_modes(snapshot)
            assert engine.ingest(snapshot['sessions'])
            summary=next(s for s in overlays.collect(engine) if s['id']=='parent')
            assert summary['priced']==(0 if mode=='미확인' else 2)
            before=snapshot['sessions'][0]['history']
            assert before[0]['service_tier']==mode
            ledger.enrich_modes(snapshot)
            assert snapshot['sessions'][0]['history'] is before
            assert not engine.ingest(snapshot['sessions'])
        snapshot['sessions']=[snapshot['sessions'][0]]
        ledger.enrich_modes(snapshot)
        assert snapshot['sessions'][0]['history'][0]['service_tier']=='미확인'
        assert child['history'][0]['service_tier']=='미확인'
    finally:ledger.close()


def test_parent_turn_settings_can_price_child_before_parent_usage_arrives(tmp_path):
    parent=mode_session('parent');parent['history']=[]
    parent['turn_records']={'parent-turn':{'started_at':100}}
    child=mode_session('child',parent='parent')
    snapshot=dict(homes=[],sessions=[child,parent])
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        ledger.modes[('h','parent','parent-turn')]='Fast'
        ledger.enrich_modes(snapshot)
        assert snapshot['sessions'][0]['history'][0]['service_tier']=='Fast'
    finally:ledger.close()


def test_nested_inheritance_does_not_backdate_a_later_parent_mode(tmp_path):
    parent=mode_session('parent','Standard',100)
    parent['history'].append(dict(parent['history'][0],key='fast',turn='fast',ts=200,service_tier='Fast'))
    child=mode_session('child',at=250,parent='parent',request_observed_at=240)
    child['turn_records']={'child-turn':{'started_at':150}}
    grandchild=mode_session('grandchild',at=170,parent='child',request_observed_at=160)
    snapshot=dict(homes=[],sessions=[grandchild,child,parent])
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        ledger.enrich_modes(snapshot)
        assert snapshot['sessions'][0]['history'][0]['service_tier']=='Standard'
        assert snapshot['sessions'][1]['history'][0]['service_tier']=='Fast'
        # Removing the parent also removes derived modes on repeated snapshots.
        snapshot['sessions']=snapshot['sessions'][:2]
        ledger.enrich_modes(snapshot)
        assert all(s['history'][0]['service_tier']=='미확인' for s in snapshot['sessions'])
    finally:ledger.close()
