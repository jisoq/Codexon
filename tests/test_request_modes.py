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


def structured_body(mode='priority',prompt='',start='None'):
    operation=body(mode,prompt,start).split('op: ',1)[1].rsplit(' }',1)[0]
    return (f'Submission thread_id=thread-000000000000 submission_id={TURN} '
            f'operation=TurnInput parent_turn_id=Some("parent-000000000000") '
            f'root_turn_id=Some("root-000000000000") op={operation}')


def test_structured_submission_fields_keep_exact_turn_and_explicit_mode():
    assert request_mode(structured_body())==(TURN,'Fast')
    assert request_mode(structured_body('default'))==(TURN,'Standard')
    assert request_mode(structured_body(start='Some("default")'))==(TURN,'Standard')
    assert request_mode(structured_body().replace(f'submission_id={TURN}',f'submission_id="{TURN}"'))==(TURN,'Fast')
    assert request_mode(structured_body(None)) is None
    assert request_mode(structured_body(None,prompt=structured_body())) is None
    assert request_mode(json.dumps(structured_body())) is None
    assert request_mode(structured_body().replace('submission_id=','other_id=')) is None


def test_new_mode_log_format_recovers_previously_scanned_calls_once(tmp_path):
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.overlay_data import OverlaySummaries
    home=str(tmp_path)
    with sqlite3.connect(tmp_path/'logs_2.sqlite') as source:
        source.execute('create table logs(id integer primary key,thread_id text,target text,feedback_log_body text)')
        source.execute('insert into logs values(1,?,?,?)',('s','codex_core::session::handlers',structured_body('default')))
    path=tmp_path/'ledger.sqlite';ledger=QuotaLedger(path)
    ledger.db.execute('INSERT INTO mode_scan VALUES(?,?,?)',(home,1,0))
    ledger.db.execute('DROP TABLE mode_scan_format');ledger.db.commit();ledger.close()
    def snapshot():
        current=mode_session('s',home=home)
        current['history'][0]['turn']=TURN
        return dict(homes=[home],sessions=[current])
    ledger=QuotaLedger(path)
    try:
        value=snapshot();ledger.enrich_modes(value)
        assert value['sessions'][0]['history'][0]['service_tier']=='Standard'
        engine=AnalysisEngine();engine.ingest(value['sessions'])
        overlay=OverlaySummaries().collect(engine)[0]
        assert overlay['priced']==1 and overlay['cost'] is not None
        assert len(ledger.db.execute('SELECT * FROM mode_events').fetchall())==1
    finally:ledger.close()
    ledger=QuotaLedger(path)
    try:
        statements=[];ledger.db.set_trace_callback(statements.append)
        value=snapshot();ledger.enrich_modes(value)
        assert value['sessions'][0]['history'][0]['service_tier']=='Standard'
        assert not any(sql.startswith('BEGIN') for sql in statements)
    finally:ledger.close()


def test_enrichment_requires_same_home_session_and_turn_and_persists(tmp_path,monkeypatch):
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
    absent=ledger.mode_dependency(home,'s')
    first=snapshot();ledger.enrich_modes(first)
    dependency=ledger.mode_dependency(home,'s')
    assert dependency!=absent and ledger.mode_dependency(home,'other')==absent
    assert [s['history'][0]['service_tier'] for s in first['sessions']]==['Fast','미확인','미확인']
    statements=[];ledger.db.set_trace_callback(statements.append)
    scans=[];scan=ledger.scan_modes
    def record_scan(homes):
        scans.append(tuple(homes));return scan(homes)
    monkeypatch.setattr(ledger,'scan_modes',record_scan)
    ledger.scan_modes([home])
    second=snapshot();ledger.enrich_modes(second,scan=False)
    assert scans==[(home,)]
    assert ledger.mode_dependency(home,'s')==dependency
    ledger.db.set_trace_callback(None)
    assert not any(sql.startswith('BEGIN') for sql in statements)
    assert [s['usage_revision'] for s in first['sessions']]==[s['usage_revision'] for s in second['sessions']]
    ledger.close()
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    statements=[];ledger.db.set_trace_callback(statements.append)
    restored=snapshot();ledger.enrich_modes(restored)
    ledger.db.set_trace_callback(None)
    assert not any(sql.startswith('BEGIN') for sql in statements)
    assert restored['sessions'][0]['history'][0]['service_tier']=='Fast'
    assert ledger.mode_dependency(home,'s')==dependency
    ledger.close()


def test_sparse_mode_scan_bounds_source_work_and_resumes_both_directions(tmp_path,monkeypatch):
    from pathlib import Path
    monkeypatch.setattr('cachemonitor.quota_cycles.MODE_SCAN_ROWS',500)
    homes=[str(tmp_path/name) for name in ('first','second')]
    for home,mode,count in zip(homes,('priority','default'),(1100,1)):
        directory=Path(home);directory.mkdir()
        with sqlite3.connect(directory/'logs_2.sqlite') as source:
            source.execute('create table logs(id integer primary key,thread_id text,target text,feedback_log_body text)')
            source.executemany('insert into logs values(?,?,?,?)',
                ((i,'s','codex_core::session::handlers' if i==1 else 'unrelated',structured_body(mode))
                 for i in range(1,count+1)))
    connect=sqlite3.connect;steps=[0]
    def count_steps():
        steps[0]+=100
        return 0
    def measured_connect(database,*args,**kwargs):
        connection=connect(database,*args,**kwargs)
        if 'logs_2.sqlite' in str(database):connection.set_progress_handler(count_steps,100)
        return connection
    monkeypatch.setattr(sqlite3,'connect',measured_connect)
    path=tmp_path/'ledger.sqlite';ledger=QuotaLedger(path)
    try:
        ledger.scan_modes(homes)
        initial_steps=steps[0]
        # The old evidence lies beyond this cycle's two 500-source-row pages.
        assert (homes[0],'s',TURN) not in ledger.modes
        assert ledger.modes[(homes[1],'s',TURN)]=='Standard'
        assert ledger.mode_offsets[homes[0]]==1100
        assert ledger.mode_backfill[homes[0]]==101
    finally:ledger.close()
    ledger=QuotaLedger(path)
    try:
        ledger.scan_modes(homes)
        assert [ledger.modes[(home,'s',TURN)] for home in homes]==['Fast','Standard']
        with connect(Path(homes[0])/'logs_2.sqlite') as source:
            source.executemany('insert into logs values(?,?,?,?)',
                ((i,'s','unrelated',structured_body('default')) for i in range(1101,1104)))
        ledger.scan_modes(homes)
        assert ledger.mode_offsets[homes[0]]==1103
        assert ledger.mode_backfill[homes[0]]==0
    finally:ledger.close()
    ledger=QuotaLedger(path)
    try:
        steps[0]=0;ledger.scan_modes(homes)
        assert steps[0]*10<initial_steps
        assert ledger.mode_offsets[homes[0]]==1103
        assert [ledger.modes[(home,'s',TURN)] for home in homes]==['Fast','Standard']
    finally:ledger.close()


def test_mode_scan_body_budget_does_not_skip_forward_or_backfilled_evidence(tmp_path,monkeypatch):
    monkeypatch.setattr('cachemonitor.quota_cycles.MODE_SCAN_ROWS',20)
    monkeypatch.setattr('cachemonitor.quota_cycles.MODE_SCAN_MATCHES',2)
    home=str(tmp_path);source=sqlite3.connect(tmp_path/'logs_2.sqlite')
    source.execute('create table logs(id integer primary key,thread_id text,target text,feedback_log_body text)')
    def append(start,end):
        source.executemany('insert into logs values(?,?,?,?)',
            ((i,'s','codex_core::session::handlers',structured_body().replace(TURN,f'turn-{i:020}'))
             for i in range(start,end+1)))
        source.commit()
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    def identifiers():
        return [row[0] for row in ledger.db.execute('select id from mode_events order by id')]
    try:
        append(1,9);ledger.scan_modes([home])
        assert identifiers()==[6,7,8,9]
        assert ledger.mode_backfill[home]==6
        for _ in range(3):ledger.scan_modes([home])
        assert identifiers()==list(range(1,10))
        append(10,14);ledger.scan_modes([home])
        assert identifiers()==list(range(1,12))
        assert ledger.mode_offsets[home]==11
        for _ in range(2):ledger.scan_modes([home])
        assert identifiers()==list(range(1,15))
        assert ledger.mode_offsets[home]==14
    finally:ledger.close();source.close()


def test_mode_enrichment_reuses_unchanged_rows_and_replays_corrections(tmp_path):
    home=str(tmp_path/'home')
    raw=[{'turn':'same','service_tier':'Standard'},
         {'turn':'changed','service_tier':'Standard','service_tier_source':'settings',
          'request_mode_source':'applied_settings','configured_service_tier':'default'}]
    snapshot={'homes':[],'sessions':[{'home':home,'id':'s','usage_revision':1,'history':raw}]}
    ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    key=(home,'s','changed')
    try:
        absent=ledger.mode_dependency(*key[:2])
        ledger.modes[key]='Fast';ledger.mode_revisions[key[:2]]=1
        fast=ledger.mode_dependency(*key[:2])
        assert fast!=absent
        ledger.modes.update({key:'Fast'})
        assert ledger.mode_dependency(*key[:2])==fast
        identity=ledger.enrich_modes(snapshot)
        enriched=snapshot['sessions'][0]['history']
        assert enriched[0] is raw[0]
        assert enriched[1] is not raw[1] and enriched[1]['service_tier']=='Fast'
        assert raw[1]['service_tier']=='Standard'
        assert ledger.enrich_modes(snapshot) is identity
        assert snapshot['sessions'][0]['history'] is enriched
        assert snapshot['sessions'][0]['usage_revision']==(1,1)
        ledger.modes.pop(key);ledger.mode_revisions[key[:2]]=2
        assert ledger.mode_dependency(*key[:2])==absent
        assert ledger.enrich_modes(snapshot) is not identity
        assert snapshot['sessions'][0]['history'][1] is raw[1]
        assert snapshot['sessions'][0]['usage_revision']==(1,2)
        assert enriched[1]['service_tier']=='Fast'  # Existing views remain stable.
        evidence=dict(mode='Fast',configured_service_tier='priority',request_mode_action='set',
            request_mode_source='settings_override',mode_conflict=False)
        ledger.mode_evidence[key]=evidence
        explicit=ledger.mode_dependency(*key[:2])
        assert explicit!=absent
        ledger.mode_evidence[key]={**evidence,'request_mode_source':'inherited_settings'}
        assert ledger.mode_dependency(*key[:2])!=explicit
        ledger.mode_evidence[key]=dict(reversed(tuple(evidence.items())))
        assert ledger.mode_dependency(*key[:2])==explicit
        ledger.mode_evidence.clear()
        assert ledger.mode_dependency(*key[:2])==absent
        # A no-change submission is not evidence against the applied setting.
        ledger.modes[key]='미확인'
        ledger.mode_evidence[key]=dict(mode='미확인',request_mode_source='unobserved',
                                      request_mode_action='unchanged',mode_conflict=False)
        ledger.mode_revisions[key[:2]]=3
        ledger.enrich_modes(snapshot)
        assert snapshot['sessions'][0]['history'][1] is raw[1]
        assert snapshot['sessions'][0]['history'][1]['service_tier']=='Standard'
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
