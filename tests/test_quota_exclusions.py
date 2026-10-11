"""Exclusions change derived accounting without deleting observed evidence."""
from copy import deepcopy
import sqlite3

import pytest

from cachemonitor.quota_cycles import QuotaLedger, quota_statistics
from cachemonitor.quota_exclusions import preview_exclusion
from cachemonitor.quota_view import prepare_quota_view, prepare_series
from test_quota_integrity import observe, sync
from test_quota_value_history import interval, report_for


def sample_report():
    report = report_for([interval([(100,100),(110,99),(120,94),(130,89),(140,88)],
                                  [(105,5),(115,100),(125,200),(135,7)])])
    return {**report, 'home':'h', 'at':150, 'exclusions':[]}


def exclusion(start=110, end=130, **extra):
    return dict(id=f'{start}:{end}', home='h', account='a', start=start, end=end,
                reason='다른 기기 사용', **extra)


def test_estimate_uses_recorded_cost_over_observed_consumption():
    report=report_for([interval(
        [(100,100),(110,100),(120,99),(130,99),(140,99),(150,98),(160,98)],
        [(105,2),(115,3),(125,7),(135,11),(145,2),(155,500)])])
    view=prepare_quota_view(report)
    assert view['lifetime']['cost']==525
    assert view['lifetime']['per_percent']==262.5
    assert view['periods'][0]['value']==26250
    assert [r['cycle_value'] for r in view['periods'][0]['series']['rows']]==[
        None,None,500,1200,2300,1250,26250]
    assert view['overall']['rows'][-1]['cycle_value']==26250


def test_second_exclusion_recomputes_ratio_and_restore_recovers_it():
    report={**report_for([interval(
        [(100,100),(110,99),(120,98),(130,97),(140,96),(150,95),(160,95)],
        [(105,2),(115,100),(125,6),(135,200),(145,10),(155,500)])]),'home':'h'}
    report['exclusions']=[exclusion(110,120)]
    assert prepare_quota_view(report)['periods'][0]['value']==17950
    preview=preview_exclusion(report,130,140)
    assert preview['after']['cost']==preview['period_after']['cost']==518
    assert preview['after']['delta']==preview['period_after']['delta']==3
    prepared_preview=preview_exclusion({**report,'view':prepare_quota_view(report)},130,140)
    assert prepared_preview['before']==preview['before']
    assert prepared_preview['period_before']['cost']==preview['period_before']['cost']
    assert prepared_preview['period_before']['delta']==preview['period_before']['delta']
    report['exclusions'].append(exclusion(130,140))
    view=prepare_quota_view(report)
    assert view['periods'][0]['value']==pytest.approx(518/3*100)
    assert view['lifetime']['cost']==518
    report['exclusions'].pop()
    assert prepare_quota_view(report)['periods'][0]['value']==17950


def test_subtracts_cost_and_consumption_preserving_chart_time_and_raw_allowance():
    report = sample_report()
    original = deepcopy(report)
    preview = preview_exclusion(report, 110, 130, '다른 기기 사용')
    assert preview['before']['cost'] == 312
    assert preview['after']['cost'] == 12
    assert preview['after']['delta'] == 2
    assert preview['removed'] == dict(cost=300, delta=10, calls=2)
    assert report == original
    report['exclusions'] = [exclusion()]
    view = prepare_quota_view(report)
    series = view['periods'][0]['series']
    assert view['lifetime']['cost'] == view['summaries'][7]['cost'] == 12
    assert view['lifetime']['delta'] == 2
    assert [(c['start'],c['end']) for c in view['lifetime']['intervals']] == [(100,110),(130,140)]
    assert [r['remaining'] for r in series['rows']] == [100,99,94,89,88]
    assert series['active_times'] == [0,10,20,30,40]
    assert [r['user_excluded'] for r in series['rows']] == [False,False,True,True,False]
    assert series['completed_costs'] == [5,None,None,7,None]
    assert series['rows'][-1]['cycle_cost'] == 12
    assert view['overall']['rows'][-1]['remaining'] == 2
    assert sum(b['total'] for b in series['model_share']['bins'].values()) == 12
    assert series['exclusions'] == report['exclusions']
    assert report['history'] == original['history']


def test_overlaps_count_once_and_scope_does_not_cross_account_or_home():
    report = sample_report()
    report['exclusions'] = [exclusion(110,120), exclusion(110,130),
                            {**exclusion(100,140), 'home':'other'},
                            {**exclusion(100,140), 'account':'other'}]
    stats = quota_statistics(report)
    assert stats['cost'] == 12 and stats['delta'] == 2 and stats['calls'] == 2
    with pytest.raises(ValueError):
        preview_exclusion(report, 110, 120)
    report['exclusions'].pop(1)
    stats = quota_statistics(report)
    assert stats['cost'] == 212 and stats['delta'] == 7


def test_full_period_exclusion_preserves_selectable_rows_and_zeroes_accounting():
    report = sample_report()
    report['tracking']['complete'] = False
    report['exclusions'] = [exclusion(100,140)]
    view = prepare_quota_view(report)
    assert view['lifetime']['cost'] == view['lifetime']['delta'] == 0
    assert view['lifetime']['per_percent'] is None
    period = view['periods'][0]
    assert period['cost'] == period['delta'] == 0
    assert len(period['series']['rows']) == 5
    assert all(r['cycle_cost'] == 0 for r in period['series']['rows'])
    assert period['series']['model_share']['spans'] == []


def test_validation_rejects_nonobserved_bounds_and_crossing_reset():
    report = sample_report()
    for start,end,reason in [(110,110,''),(130,110,''),(float('nan'),130,''),
                             (111,130,''),(110,130,'x'*201)]:
        with pytest.raises(ValueError):
            preview_exclusion(report,start,end,reason)
    second = interval([(150,100),(160,99)],[(155,2)])
    report = {**report_for(report['cycles']+[second]), 'home':'h'}
    with pytest.raises(ValueError):
        preview_exclusion(report,130,160)
    result = preview_exclusion(report,110,130)
    assert result['before']['cost'] == 314
    assert result['after']['cost'] == 14
    assert result['period_before']['cost'] == 312
    assert result['period_after']['cost'] == 12


def test_downsampling_retains_exclusion_edges_and_the_first_surviving_segment():
    rows = [dict(at=100+i, remaining=100, cycle_cost=0, cycle_value=None,
                 connect=i>0, reset_kind=None, cycle_start=100, account='a',
                 user_excluded=721<i<=1379, user_exclusion_end=i==1379)
            for i in range(3000)]
    series = prepare_series(rows)
    for sample in series['samples'].values():
        assert {721,722,1379,1380} <= set(sample)
    assert series['active_times'][-1] == 2999


def test_ledger_exclusion_and_reason_survive_reopen_then_restore_original_totals(tmp_path):
    path = tmp_path/'quota.sqlite'
    ledger = QuotaLedger(path)
    try:
        for at,used in [(100,0),(130,1),(160,5),(190,9),(220,10)]:
            observe(ledger,at,used)
        sync(ledger,[115,145,175,205])
        original = ledger.report('h',1000)
        before = quota_statistics(original)
        raw = {table:[tuple(r) for r in ledger.db.execute('select * from '+table)]
               for table in ('observations','calls','window_observations')}
        record = ledger.exclude_interval('h',130,190,'  실험 작업  ')
        assert record['reason'] == '실험 작업'
        assert quota_statistics(ledger.report('h',1000))['delta'] == 2
        for table,values in raw.items():
            assert [tuple(r) for r in ledger.db.execute('select * from '+table)] == values
        with pytest.raises(ValueError):
            ledger.restore_exclusion('other',record['id'])
    finally:
        ledger.close()
    ledger = QuotaLedger(path)
    try:
        assert ledger.report('h',1000)['exclusions'] == [record]
        assert ledger.restore_exclusion('h',record['id'])
        assert not ledger.restore_exclusion('h',record['id'])
        restored = ledger.report('h',1000)
        assert restored['exclusions'] == []
        assert quota_statistics(restored) == before
        assert restored['history'] == original['history']
    finally:
        ledger.close()


def test_restore_rejects_other_current_account(tmp_path):
    ledger = QuotaLedger(tmp_path/'scope.sqlite')
    try:
        for at,used in [(100,10),(130,11),(160,12)]:
            observe(ledger,at,used)
        sync(ledger,[115,145])
        record = ledger.exclude_interval('h',100,130)
        observe(ledger,190,13,account='another')
        assert ledger.report('h')['exclusions'] == []
        with pytest.raises(ValueError):
            ledger.restore_exclusion('h',record['id'])
        row = ledger.db.execute('select restored_at from quota_exclusions where id=?',(record['id'],)).fetchone()
        assert row[0] is None
    finally:
        ledger.close()


@pytest.mark.parametrize('operation',['exclude','restore'])
def test_slow_edit_validation_does_not_hold_observation_writer_lock(tmp_path,monkeypatch,operation):
    from threading import Event, Thread
    ledger=QuotaLedger(tmp_path/'concurrent.sqlite')
    for at,used in [(100,10),(130,11),(160,12)]:observe(ledger,at,used)
    sync(ledger,[115,145])
    record=ledger.exclude_interval('h',100,130) if operation=='restore' else None
    original=ledger._report
    writes=[];errors=[];done=Event();reports=[]
    def writer():
        try:
            with sqlite3.connect(ledger.path,timeout=.2) as db:
                db.execute("update observations set used=13 where at=160")
            writes.append(True)
        except Exception as error:errors.append(error)
        finally:done.set()
    thread=Thread(target=writer)
    def slow_report(home):
        result=original(home)
        reports.append(result)
        if len(reports)==1:
            thread.start()
            assert done.wait(3),'Observation writer was blocked by edit validation'
            assert not errors
        return result
    monkeypatch.setattr(ledger,'_report',slow_report)
    try:
        if operation=='restore':assert ledger.restore_exclusion('h',record['id'])
        else:assert ledger.exclude_interval('h',100,130)['id']
        assert writes==[True] and len(reports)==2
    finally:
        if thread.ident:thread.join(3)
        ledger.close()


def test_changed_snapshot_revalidates_and_never_duplicates_concurrent_exclusion(tmp_path,monkeypatch):
    from cachemonitor import quota_exclusions
    ledger=QuotaLedger(tmp_path/'changed.sqlite')
    for at,used in [(100,10),(130,11),(160,12)]:observe(ledger,at,used)
    sync(ledger,[115,145])
    original=quota_exclusions.validate_exclusion
    validations=[]
    def concurrently_excluded(report,start,end,reason=''):
        prepared=original(report,start,end,reason)
        validations.append(True)
        if len(validations)==1:
            with sqlite3.connect(ledger.path,timeout=.2) as db:
                db.execute('''insert into quota_exclusions
                    (id,home,account,start,end,reason,period_start,period_end,created_at,restored_at)
                    values ('concurrent',:home,:account,:start,:end,:reason,:period_start,:period_end,1,NULL)''',prepared)
        return prepared
    monkeypatch.setattr(quota_exclusions,'validate_exclusion',concurrently_excluded)
    try:
        with pytest.raises(ValueError,match='이미 제외'):
            ledger.exclude_interval('h',100,130)
        assert [row[0] for row in ledger.db.execute('select id from quota_exclusions')]==['concurrent']
    finally:ledger.close()


def test_rebuild_copies_active_and_restored_exclusion_records(tmp_path):
    from tools.rebuild_quota_ledger import rebuild
    source, destination, index = (tmp_path/name for name in ('source.sqlite','new.sqlite','index.sqlite'))
    ledger = QuotaLedger(source)
    try:
        for at,used in [(100,10),(130,11),(160,12)]:
            observe(ledger,at,used)
        sync(ledger,[115,145])
        old = ledger.exclude_interval('h',100,130,'復元')
        ledger.restore_exclusion('h',old['id'])
        ledger.exclude_interval('h',130,160,'keep me')
        expected = [tuple(r) for r in ledger.db.execute('select * from quota_exclusions order by start')]
    finally:
        ledger.close()
    with sqlite3.connect(index) as db:
        db.execute('create table metadata(home text,tid text,data text)')
        db.execute('create table events(home text,tid text,data text)')
    result = rebuild(source,index,destination)
    with sqlite3.connect(destination) as db:
        actual = db.execute('select * from quota_exclusions order by start').fetchall()
    assert actual == expected
    assert result['result']['quota_exclusions'] == 2
