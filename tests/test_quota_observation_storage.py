import copy
import hashlib
import json
import sqlite3

from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor.quota_view import history_rows


def observation(at=100,used=10,**extra):
    return dict(observed_at=at,source='live',account='a',plan_type='pro',bucket='codex',
                windows={'weekly':dict(used_percent=used,resets_at=100000,window_minutes=10080)},**extra)


def legacy_observe(ledger,home,quota):
    for name,window in quota['windows'].items():
        identifier=hashlib.sha256(json.dumps([home,quota['observed_at'],quota.get('source'),
            quota.get('account'),quota.get('plan_type'),quota.get('bucket'),name,window],sort_keys=True).encode()).hexdigest()
        ledger.db.execute('insert or ignore into window_observations values(?,?,?,?,?,?,?,?,?,?,?,?)',
            (identifier,home,quota['observed_at'],quota.get('source','local'),quota.get('account',''),
             quota.get('plan_type') or '',quota.get('bucket') or ('codex' if quota.get('source')=='live' else ''),
             name,window.get('used_percent'),window.get('resets_at'),window.get('window_minutes'),
             json.dumps(quota.get('separate_models',[]))))
    ledger.observe(home,quota)


def test_weekly_references_preserve_conflicting_timestamp_and_first_window_payload(tmp_path):
    current=QuotaLedger(tmp_path/'current.sqlite');legacy=QuotaLedger(tmp_path/'legacy.sqlite')
    values=[observation(100,10),observation(100,13),observation(103,14),
            observation(103,14,separate_models=['gpt-6-astra'])]
    local=observation(106,15)
    for key in ('source','account','plan_type','bucket'):local.pop(key)
    values.append(local)
    try:
        for value in values:
            current.observe('h',value);legacy_observe(legacy,'h',value)
        actual=current.report('h',200);expected=legacy.report('h',200)
        assert actual==expected
        assert history_rows(actual,'weekly')==history_rows(expected,'weekly')
        assert current.db.execute('select count(*) from window_observations').fetchone()[0]==0
        assert current.db.execute('select count(*) from observations').fetchone()[0]==5
        assert current.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==4
        assert next(row for row in actual['history'] if row['at']==103)['separate']=='[]'

        # An older writer can add a full copy later; the first reference still wins.
        legacy_observe(current,'h',values[3])
        assert current.report('h',200)==actual
    finally:current.close();legacy.close()


def test_existing_weekly_rows_remain_unchanged_across_reopen_and_new_observations(tmp_path):
    path=tmp_path/'mixed.sqlite';ledger=QuotaLedger(path)
    first=observation()
    legacy_observe(ledger,'h',first)
    original=[tuple(row) for row in ledger.db.execute('select * from window_observations')]
    initial=ledger.report('h',200);ledger.close()
    ledger=QuotaLedger(path)
    try:
        assert ledger.report('h',200)==initial
        ledger.observe('h',first)
        assert ledger.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==0
        ledger.observe('h',observation(110,11))
        assert [tuple(row) for row in ledger.db.execute('select * from window_observations')]==original
        history=ledger.report('h',200)['history']
        assert len(history)==2 and history[0]==initial['history'][0]
        assert ledger.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==1
    finally:ledger.close()


def test_invalid_raw_values_and_incomplete_windows_remain_recoverable(tmp_path):
    ledger=QuotaLedger(tmp_path/'raw.sqlite');legacy=QuotaLedger(tmp_path/'legacy.sqlite')
    try:
        for index,value in enumerate((None,-1,101,float('nan'),float('inf'),'invalid')):
            quota=observation(100+index,value)
            ledger.observe('h',quota);legacy_observe(legacy,'h',quota)
        assert ledger.report('h',200)==legacy.report('h',200)
        assert ledger.db.execute('select count(*) from observations').fetchone()[0]==6
        assert ledger.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==6
        for index,window in enumerate(({},dict(used_percent=20),dict(window_minutes=10080))):
            quota=observation(120+index);quota['windows']['weekly']=window
            ledger.observe('h',quota)
        five=observation(130)
        five['windows']={'five_hour':dict(used_percent=50,resets_at=2000,window_minutes=300)}
        ledger.observe('h',five)
        history=ledger.report('h',200)['history']
        assert len(history)==10
        assert [(row['used'],row['minutes']) for row in history if 120<=row['at']<=122]==[
            (None,None),(20,None),(None,10080)]
        assert history[-1]['window']=='five_hour'
        assert ledger.db.execute('select count(*) from window_observations').fetchone()[0]==4
        ledger.close();ledger=QuotaLedger(tmp_path/'raw.sqlite')
        assert ledger.report('h',200)['history']==history
    finally:ledger.close();legacy.close()


def test_weekly_reference_and_canonical_payload_roll_back_together(tmp_path):
    ledger=QuotaLedger(tmp_path/'rollback.sqlite')
    try:
        ledger.observe('h',observation(),commit=False)
        assert ledger.db.in_transaction
        assert ledger.db.execute('select count(*) from weekly_window_refs').fetchone()[0]==1
        ledger.db.rollback()
        for table in ('observations','observation_context','weekly_window_refs'):
            assert ledger.db.execute('select count(*) from '+table).fetchone()[0]==0
        ledger.observe('h',observation())
        expected=ledger.report('h',200)
        ledger.close();ledger=QuotaLedger(tmp_path/'rollback.sqlite')
        assert ledger.report('h',200)==expected
        assert ledger.db.execute('''select count(*) from weekly_window_refs r
            left join observations o on o.id=r.observation_id where o.id is null''').fetchone()[0]==0
    finally:ledger.close()


def test_replayed_observation_is_noop_and_context_only_fills_missing_fields(tmp_path):
    ledger=QuotaLedger(tmp_path/'replay.sqlite')
    watcher=sqlite3.connect(ledger.path)
    quota=observation()
    def version():return watcher.execute('pragma data_version').fetchone()[0]
    try:
        ledger.observe('h',quota)
        changes=ledger.db.total_changes;before=version()
        ledger.observe('h',copy.deepcopy(quota))
        assert ledger.db.total_changes==changes and version()==before
        ledger.db.execute("update observation_context set plan='',bucket='existing'");ledger.db.commit()
        changes=ledger.db.total_changes;before=version()
        revision=ledger.db.execute('select value from report_revision where id=1').fetchone()[0]
        ledger.observe('h',quota)
        assert tuple(ledger.db.execute('select plan,bucket from observation_context').fetchone())==('pro','existing')
        # One corrected context row and one durable report invalidation.
        assert ledger.db.total_changes==changes+2 and version()!=before
        assert ledger.db.execute('select value from report_revision where id=1').fetchone()[0]==revision+1
        changes=ledger.db.total_changes;before=version()
        ledger.observe('h',quota)
        assert ledger.db.total_changes==changes and version()==before
    finally:watcher.close();ledger.close()
