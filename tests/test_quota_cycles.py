from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.quota_cycles import QuotaLedger, estimate, split_cycles, quota_statistics
from cachemonitor.quota_panel import QuotaPanel


def quota(at, used, reset=1000, source='live', account='a', separate=()):
    return {'windows': {'weekly': {'used_percent': used, 'resets_at': reset, 'window_minutes': 10080}},
            'plan_type': 'pro', 'observed_at': at, 'source': source, 'account': account,
            'separate_models': list(separate)}


def row(key='r1', ts=150, model='gpt-6-astra', **values):
    return {'service_tier':'Standard','key': key, 'ts': ts, 'model': model, 'input': 100000, 'cached': 20000,
            'written': 10000, 'output': 2000, 'reasoning': 500, **values}


def sync(ledger, rows, revision=1, at=300, loading=False):
    engine = SimpleNamespace(sessions={('h', 's'): {'revision': revision, 'prepared': {'history': rows}}})
    ledger.sync(engine, {'homes': ['h'], 'sessions': [{'home': 'h'}], 'ts': at, 'index': {'loading': loading}})


@pytest.mark.parametrize('observations',[128,2048])
def test_report_reuses_history_across_heartbeat_and_invalidates_late_observation(tmp_path,monkeypatch,observations):
    ledger=QuotaLedger(tmp_path/'quota.sqlite')
    try:
        now=observations+200
        sync(ledger,[row()],at=now)
        for n in range(observations):ledger.observe('h',quota(100+n,10+n/100,reset=10000))
        original=ledger._report;builds=[]
        def build(*args,**kwargs):builds.append(1);return original(*args,**kwargs)
        monkeypatch.setattr(ledger,'_report',build)
        first=ledger.report('h',now)
        steps=[0]
        ledger.db.set_progress_handler(lambda:(steps.__setitem__(0,steps[0]+100),0)[1],100)
        same=ledger.report('h',now)
        ledger.db.set_progress_handler(None,0)
        assert same==first and len(builds)==1 and steps[0]<1000
        sync(ledger,[row()],at=now+1)
        fresh=ledger.report('h',now+1)
        assert fresh['index_at']==now+1 and len(builds)==1
        ledger.observe('h',quota(now-1,32,reset=10000))
        changed=ledger.report('h',now+1)
        assert len(builds)==2 and changed!=fresh
        # Cached reads cannot survive rollback with a reused revision number.
        ledger.db.execute('update observations set used=50 where at=100')
        ledger.report('h',now+1);ledger.db.rollback()
        assert ledger.report('h',now+1)==changed
    finally:ledger.close()


def test_cached_report_preserves_coverage_progress_and_future_call_boundaries(tmp_path):
    ledger=QuotaLedger(tmp_path/'quota.sqlite')
    calls=[row(f'r{n}',ts=at) for n,at in enumerate((120,220,320,550))]
    try:
        sync(ledger,calls,at=150)
        for at,used in ((100,10),(200,20),(300,30)):ledger.observe('h',quota(at,used))
        for at,now in ((150,500),(250,501),(350,502),(351,551)):
            sync(ledger,calls,at=at)
            assert ledger.report('h',now)==ledger._report('h',now)
    finally:ledger.close()


def test_activity_cursor_commits_with_usage_and_rolls_back_on_save_failure(tmp_path):
    import sqlite3
    path=tmp_path/'quota.sqlite';ledger=QuotaLedger(path)
    def save(revision,output):
        engine=SimpleNamespace(sessions={('h','s'):{'revision':revision,'prepared':{'history':[row(output=output)]}}})
        ledger.sync(engine,dict(homes=['h'],sessions=[{'home':'h'}],ts=300,index={'loading':False},
                               activity_cursor=('source',revision)))
    try:
        save(1,2000);raw=ledger.db
        class FailedCommit:
            def __getattr__(self,name):return getattr(raw,name)
            def commit(self):raise sqlite3.OperationalError('injected save failure')
        ledger.db=FailedCommit()
        with pytest.raises(sqlite3.OperationalError):save(2,4000)
        ledger.db=raw;ledger.close();ledger=QuotaLedger(path)
        assert ledger.activity_cursor()==('source',1)
        assert ledger.db.execute('SELECT output FROM calls WHERE uid=?',('r1',)).fetchone()[0]==2000
    finally:ledger.close()


def test_session_sync_work_does_not_scale_with_unrelated_call_history(tmp_path):
    path=tmp_path/'session-sync.sqlite'
    ledger=QuotaLedger(path)
    try:
        sync(ledger,[row()])
        def unrelated(start, end):
            ledger.db.executemany('insert into calls select home,?, ?,ts,model,input,cached,written,'
                'output,reasoning,cost,price_id,signature,service_tier from calls where home=? and uid=?',
                [(f'other-{i}',f'session-{i}','h','r1') for i in range(start,end)])
            ledger.db.commit()
        def measured_sync(revision):
            steps=[0]
            def progress():steps[0]+=100;return 0
            ledger.db.set_progress_handler(progress,100)
            try:sync(ledger,[row(output=2000+revision)],revision=revision)
            finally:ledger.db.set_progress_handler(None,0)
            assert ledger.db.execute('select output from calls where home=? and uid=?',('h','r1')).fetchone()[0]==2000+revision
            return steps[0]
        unrelated(0,32)
        small=measured_sync(2)
        unrelated(32,4096)
        ledger.close();ledger=QuotaLedger(path)
        large=measured_sync(3)
        # Count SQLite VM work, independent of device speed and cache warmth.
        assert large<=small*2+1000
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==4097
        sync(ledger,[],revision=4)
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==4096
    finally:ledger.close()


def test_restart_and_append_do_not_rewrite_unchanged_call_boundaries(tmp_path):
    path=tmp_path/'boundaries.sqlite';ledger=QuotaLedger(path)
    calls=[row(f'r{i}',ts=150+i,request_ts=100+i) for i in range(64)]
    calls[0]['request_ts']=None
    try:sync(ledger,calls)
    finally:ledger.close()
    ledger=QuotaLedger(path)
    try:
        ledger.db.executescript('''create temp table boundary_writes(uid text);
            create temp trigger changed_boundary after update on tracking_call_boundaries
            begin insert into boundary_writes values(new.uid); end;
            create temp trigger inserted_boundary after insert on tracking_call_boundaries
            begin insert into boundary_writes values(new.uid); end;''')
        sync(ledger,calls)
        assert ledger.db.execute('select count(*) from boundary_writes').fetchone()[0]==0
        calls.append(row('new',ts=220,request_ts=210))
        calls[0]={**calls[0],'request_ts':90}
        calls[1]={**calls[1],'request_ts':None}
        sync(ledger,calls,revision=2)
        assert {r[0] for r in ledger.db.execute('select uid from boundary_writes')}=={'new','r0','r1'}
        assert ledger.db.execute("select started from tracking_call_boundaries where uid='r0'").fetchone()[0]==90
        assert ledger.db.execute("select started from tracking_call_boundaries where uid='r1'").fetchone()[0] is None
    finally:ledger.close()


def test_slow_cycle_diagnostics_are_quiet_normally_and_rate_limited(monkeypatch,tmp_path):
    import json
    from cachemonitor import quota_diagnostics as diagnostics
    clock=[100.];records=[]
    monkeypatch.setattr(diagnostics.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(diagnostics,'_logger',lambda path:SimpleNamespace(warning=records.append))
    last=float('-inf');path=tmp_path/'analysis.sqlite'
    assert diagnostics.record_slow_cycle(path,{'analysis':1.},2.,last)==last
    last=diagnostics.record_slow_cycle(path,{'analysis':11.},12.,last)
    assert json.loads(records[0])['stage_seconds']=={'analysis':11.}
    clock[0]+=59
    assert diagnostics.record_slow_cycle(path,{'analysis':15.},40.,last)==last
    assert len(records)==1
    clock[0]+=1
    last=diagnostics.record_slow_cycle(path,{'analysis':1.},31.,last)
    assert len(records)==2 and json.loads(records[1])['source_age_seconds']==31.
    def full_disk(path):raise OSError('full')
    monkeypatch.setattr(diagnostics,'_logger',full_disk);clock[0]+=60
    assert diagnostics.record_slow_cycle(path,{'analysis':11.},12.,last)==clock[0]


def test_standard_report_keeps_unknown_mode_separate_and_unpriced(tmp_path):
    ledger=QuotaLedger(tmp_path/'merged.sqlite')
    try:
        ledger.observe('h',quota(100,20))
        ledger.observe('h',quota(200,30))
        sync(ledger,[row(),row('unknown',160,service_tier='미확인'),row('fast',170,service_tier='Fast')])
        cycle=ledger.report('h',300)['cycles'][0]
        standard=next(r for r in cycle['models'] if r['service_tier']=='Standard')
        assert len(cycle['models'])==3
        assert standard['calls']==1 and standard['unknown_mode_calls']==0
        unknown=next(r for r in cycle['models'] if r['service_tier']=='미확인')
        assert unknown['calls']==1 and unknown['cost'] is None
        assert ledger.db.execute("select service_tier from calls where uid='unknown'").fetchone()[0]=='미확인'
    finally:ledger.close()


def test_internal_review_stays_in_ledger_but_not_work_usage_or_models(tmp_path):
    ledger=QuotaLedger(tmp_path/'internal-review.sqlite')
    try:
        ledger.observe('h',quota(100,20))
        ledger.observe('h',quota(200,30))
        sync(ledger,[row(),row('review',160,model='codex-auto-review')])
        cycle=ledger.report('h',300)['cycles'][0]
        assert {r['model'] for r in cycle['models']}=={'gpt-6-astra'}
        assert sum(r['calls'] for r in cycle['models'])==cycle['priced_calls']==1
        assert ledger.db.execute("select count(*) from calls where model='codex-auto-review'").fetchone()[0]==1
    finally:ledger.close()


def test_same_window_formula_cache_and_reasoning_not_double_counted(tmp_path):
    ledger = QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h', quota(100, 20, separate=['gpt-5.6-luna']))
        ledger.observe('h', quota(200, 30, 1001, separate=['gpt-5.6-luna']))
        sync(ledger, [row(), row('before', 50), row('after', 250), row('reserve', 160, 'gpt-5.6-luna')])
        cycle = ledger.report('h', 300)['cycles'][0]
        assert cycle['cost'] == pytest.approx(.7 + .02 + .125 + .1)
        assert cycle['delta'] == 10 and cycle['start'] == 100
        assert estimate(cycle['cost'],cycle['delta'],5,cycle['blocked'])['usd'] == pytest.approx(.4725)
        assert len(cycle['models']) == 2 and sum(m['calls'] for m in cycle['cycle_models']) == 3
        assert cycle['partial']
        # Same response ID cannot be counted twice on refresh or restart.
        sync(ledger, [row(), row()], revision=2)
        assert ledger.db.execute('select count(*) from calls').fetchone()[0] == 1
    finally: ledger.close()
    restored = QuotaLedger(tmp_path/'cycles.sqlite')
    try: assert len(restored.report('h',300)['cycles']) == 1
    finally: restored.close()


def test_reset_jitter_natural_manual_account_and_regression_boundaries(tmp_path):
    ledger = QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        for q in (quota(100,20),quota(200,30,1001),quota(1002,0,605800),
                  quota(1100,5,605801),quota(1200,0,700000),quota(1300,10,700001),
                  quota(1400,9,700000),quota(1500,9,700000,account='b')):
            ledger.observe('h',q)
        groups=split_cycles([dict(r) for r in ledger.db.execute('select * from observations')])
        assert len(groups)==5
        assert groups[1]['reason']=='예정 리셋 확인'
        assert groups[1]['boundary']==1001
        assert groups[2]['reason']=='한도 시각 변경: 관측 구간 분리'
        assert all('수동 리셋' not in g['reason'] for g in groups)
        assert groups[3]['discontinuous']
        assert groups[4]['reason']=='계정 변경'
    finally: ledger.close()


def test_missing_price_and_incomplete_index_suppress_conversion(tmp_path):
    ledger=QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h',quota(100,10)); ledger.observe('h',quota(200,20))
        sync(ledger,[row(written=None)],at=150,loading=True)
        c=ledger.report('h',300)['cycles'][0]
        assert c['missing']==1
        assert c['models'][0]['written'] is None
        assert '해당 구간의 토큰 수집 완료 대기' in c['blocked']
        assert estimate(c['cost'],c['delta'],5,c['blocked'])['usd'] is None
        # A later authoritative modern record removes the old legacy row.
        sync(ledger,[row(key='legacy:x')],revision=2)
        sync(ledger,[row(key='modern')],revision=3)
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==1
    finally: ledger.close()


def test_mode_change_preserves_price_snapshot_and_ambiguous_gap_is_excluded(tmp_path):
    ledger=QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h',quota(100,20));ledger.observe('h',quota(200,30))
        sync(ledger,[row(),row('gap',250)])
        ledger.db.execute("update calls set price_id='old-rates',cost=42 where uid='r1'")
        ledger.db.commit()
        sync(ledger,[row(service_tier='Fast'),row('gap',250)],revision=2)
        stored=ledger.db.execute("select cost,price_id,service_tier from calls where uid='r1'").fetchone()
        assert stored['cost']==pytest.approx(2.3625) and stored['service_tier']=='Fast'
        assert ledger.db.execute("select cost from cost_archive where price_id='old-rates'").fetchone()[0]==42
        ledger.observe('h',quota(300,0,2000))
        older=ledger.report('h',350)['cycles'][1]
        assert older['uncertain_gap'] and older['cycle_end']==200
        assert sum(r['calls'] for r in older['cycle_models'])==1
    finally: ledger.close()


def test_observation_gap_excludes_unmatched_cost_and_consumption_but_preserves_raw_records(tmp_path):
    ledger=QuotaLedger(tmp_path/'gap.sqlite')
    try:
        for q in (quota(100,10,10000),quota(200,15,10000),quota(3801,23,10000),quota(3900,28,10000)):
            ledger.observe('h',q)
        sync(ledger,[row('before',150),row('gap-call',3800),row('after',3850)],at=4000)
        report=ledger.report('h',4000)
        assert len(report['cycles'])==1
        summary=quota_statistics(report)
        assert summary['valid']==2 and summary['delta']==10
        assert summary['calls']==2 and summary['cost']==pytest.approx(1.89)
        assert len(report['cycles'])==1 and report['cycles'][0]['delta']==18
        assert ledger.db.execute('select count(*) from calls').fetchone()[0]==3
    finally:ledger.close()


def test_quota_home_switch_clears_values_and_rejects_late_old_home_results(tmp_path):
    import time
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'scope.ini'),QSettings.IniFormat))
    now=time.time();a=str(tmp_path/'a');b=str(tmp_path/'b')
    panel.set_homes([a,b])
    value={'quota':quota(now,30,now+1000),'report':{'home':a,'cycles':[],'at':now,'index_loading':False}}
    assert panel.receive(value)
    assert panel.quota is value['quota']
    switched=[];panel.home_selected.connect(switched.append)
    panel.home_choice.setCurrentIndex(1)
    assert switched==[b] and panel.quota is None
    assert panel.history_waiting.isVisible() and not panel.history.rows
    assert panel.result.text().endswith('—') and not panel.intervals.isVisible()
    assert not panel.receive(value)
    assert panel.quota is None and panel.report['home']==b
    panel.receive({'report':{'home':b,'cycles':[],'manual_reset_pending':True,'index_loading':True}})
    assert panel.history_waiting.isVisible() and not panel.history_controls.isVisible()
    assert not panel.conversion.isVisible()
    panel.receive({'report':{'home':b,'cycles':[],'error':True,'index_loading':False},'issue':'원장 오류'})
    assert panel.status.text()=='원장 오류' and panel.status.isVisible()
    assert '확인 불가' not in panel.result.text()
    assert not panel.intervals.isVisible() and not panel.paging.isVisible() and not panel.history.rows
    def walk(node):
        yield node
        for child in node.nodes:yield from walk(child)
    assert not any(node.text()=='사용 한도' for node in walk(panel))
    panel.deleteLater()
