from PySide6.QtTest import QTest
from cachemonitor.quick_qa import mount, dispose, table_view
import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from cachemonitor.quota import quota_display
from cachemonitor.quota_cycles import QuotaLedger, estimate, split_cycles, quota_statistics
from cachemonitor.quota_live import normalize_limits, AccountClient
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
        assert groups[2]['reason']=='한도 시각 변경 · 관측 구간 분리'
        assert all('수동 리셋' not in g['reason'] for g in groups)
        assert groups[3]['discontinuous']
        assert groups[4]['reason']=='계정 변경'
    finally: ledger.close()


@pytest.mark.parametrize('delta',[0,-1])
def test_small_or_negative_denominators_are_not_estimates(delta):
    assert estimate(10,delta,5)['usd'] is None


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


def test_history_is_visible_but_not_mistaken_for_account_calibration(tmp_path):
    ledger=QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h',quota(100,10,source='history',account=''))
        ledger.observe('h',quota(200,20,source='history',account=''))
        sync(ledger,[row()])
        c=ledger.report('h',300)['cycles'][0]
        assert c['cost']>0 and not c['live'] and c['assumptions'] and '계정 식별 미확인' in c['blocked']
        ledger.observe('h',quota(210,21));ledger.observe('h',quota(250,25))
        sync(ledger,[row(),row('new',230)],revision=2)
        c=ledger.report('h',300)['cycles'][0]
        assert c['start']==210 and c['delta']==4
        assert sum(m['calls'] for m in c['models'])==1
        # Unknown historical scope and identified live scope are separate.
        assert sum(m['calls'] for m in c['cycle_models'])==1
        assert len(ledger.report('h',300)['cycles'])==1
        assert len(ledger.report('h',300)['history'])==4
    finally: ledger.close()


def test_live_normalization_and_stale_numeric_display():
    raw={'rateLimits':{'limitId':'other'},'rateLimitsByLimitId':{
        'codex':{'limitId':'codex','planType':'pro','primary':{'usedPercent':63,'windowDurationMins':10080,'resetsAt':1000}},
        'reserve':{'normalModelSlug':'gpt-5.6-luna'}}}
    q=normalize_limits(raw,100,'hashed-account')
    assert q['separate_models']==['gpt-5.6-luna']
    assert quota_display(q,'weekly',110)['text']=='37'
    assert quota_display(q,'weekly',200)['text']=='?'
    assert '갱신 지연' in quota_display(q,'weekly',200)['tooltip']
    with pytest.raises(ValueError): AccountClient('.').rpc('account/rateLimitResetCredit/consume')


def test_panel_keeps_actual_current_limits_separate_from_historical_period(tmp_path):
    import time
    app=QApplication.instance() or QApplication([])
    ledger=QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h',quota(100,20));ledger.observe('h',quota(200,30))
        sync(ledger,[row(),row('later',250)])
        panel=QuotaPanel(QSettings(str(tmp_path/'panel.ini'),QSettings.IniFormat))
        current=quota(time.time(),32,reset=time.time()+1000)
        panel.receive({'quota':current,'report':ledger.report('h',300)})
        assert panel.result.text()=='$9.45'
        assert panel.current['weekly']['value'].text()=='68.0%'
        assert not panel.current['five_hour']['card'].isVisible()
        assert '10%p' in panel.basis.text()
        assert panel.table.item(0,1).text()=='1'
        assert panel.table.item(0,3).text()=='$0.9450'
        selected=panel.selected_id
        ledger.observe('h',quota(310,0,5000))
        panel.receive({'quota':current,'report':ledger.report('h',320)})
        assert panel.selected_id==selected
        assert panel.statistics['valid']==1 and panel.statistics['excluded']==1
        panel.interval_filter.setCurrentIndex(panel.interval_filter.findData('used'))
        assert panel.intervals.rowCount()==1 and not panel.intervals.model().rows[0]['excluded']
        panel.interval_filter.setCurrentIndex(panel.interval_filter.findData('excluded'))
        assert panel.intervals.rowCount()==1 and panel.intervals.model().rows[0]['excluded']
        panel.set_history_period(150,300)
        assert panel.statistics['valid']==0
        assert panel.current['weekly']['value'].text()=='68.0%'
        panel.deleteLater()
    finally:ledger.close()


def test_pooled_one_percent_estimate_uses_consumption_weights_and_reports_variation():
    def interval(identifier,cost,delta,**extra):
        return {'id':identifier,'cost':cost,'delta':delta,'blocked':[],
                'models':[{'calls':3,'separate':False}], 'live':True,'account':'current',**extra}
    report={'cycles':[
        interval('a',40,10),interval('b',120,20),
        interval('tiny',100,.5),interval('missing',None,10,blocked=['단가/토큰 미확인']),
        interval('other-account',900,30,account='old'),
        interval('history',30,10,live=False,account='',blocked=['과거 로컬 기록: 계정 한도 범위 미확인'])]}
    result=quota_statistics(report)
    assert result['valid']==3 and result['total']==6 and result['excluded']==3
    assert result['cost']==260 and result['delta']==30.5
    assert result['per_percent']==pytest.approx(260/30.5)
    assert result['calls']==9 and result['historical']==0
    assert result['intervals'][4]['excluded']==['다른 계정의 관측 구간']
    assert report['cycles'][5]['blocked']==['과거 로컬 기록: 계정 한도 범위 미확인']
    empty=quota_statistics({'cycles':[interval('tiny',1,1)]})
    assert empty['per_percent']==1 and empty['valid']==1


def test_mode_change_preserves_price_snapshot_and_ambiguous_gap_is_excluded(tmp_path):
    ledger=QuotaLedger(tmp_path/'cycles.sqlite')
    try:
        ledger.observe('h',quota(100,20));ledger.observe('h',quota(200,30))
        sync(ledger,[row(),row('gap',250)])
        ledger.db.execute("update calls set price_id='old-rates',cost=42 where uid='r1'")
        ledger.db.commit()
        sync(ledger,[row(service_tier='Fast'),row('gap',250)],revision=2)
        stored=ledger.db.execute("select cost,price_id,service_tier from calls where uid='r1'").fetchone()
        assert stored['cost']==pytest.approx(1.89) and stored['service_tier']=='Fast'
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


def test_observation_gap_does_not_split_or_rewrite_stored_cycle(tmp_path):
    ledger=QuotaLedger(tmp_path/'boundary.sqlite')
    try:
        ledger.observe('h',quota(100,10));ledger.observe('h',quota(1900,20))
        groups=split_cycles([dict(r) for r in ledger.db.execute('select * from observations')])
        assert len(groups)==1
    finally:ledger.close()


def test_large_interval_history_pages_without_nested_table_scroll_and_keeps_selection(tmp_path):
    from cachemonitor.presentation import Scroll
    app=QApplication.instance() or QApplication([])
    ledger=QuotaLedger(tmp_path/'large.sqlite')
    panel=QuotaPanel(QSettings(str(tmp_path/'large.ini'),QSettings.IniFormat))
    try:
        ledger.observe('h',quota(100,20));ledger.observe('h',quota(200,30))
        sync(ledger,[row()],at=300)
        report=ledger.report('h',300);template=report['cycles'][0]
        report['cycles']=[{**template,'id':str(i),'start':100+i*300,'end':200+i*300} for i in range(2000)]
        panel.receive({'report':report,'can_record_reset':True})
        scroll=Scroll();scroll.setWidgetResizable(True);scroll.setWidget(panel)
        host=mount(scroll,1120,760);app.processEvents();QTest.qWait(40)
        assert panel.statistics['total']==panel.statistics['valid']==2000
        assert panel.intervals.rowCount()==25 and panel.intervals.state['inline']
        assert panel.intervals.model().formatted<500
        panel.change_page(79)
        panel.intervals.selectRow(24);app.processEvents();QTest.qWait(40)
        assert panel.selected_id=='1999'
        assert panel.intervals.item(24,3).text()=='$9.45'
        panel.receive({'report':report,'can_record_reset':True})
        assert panel.selected_id=='1999' and panel.page_index==79
    finally:
        dispose(host);ledger.close()


def test_quota_home_switch_clears_values_and_rejects_late_old_home_results(tmp_path):
    import time
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'scope.ini'),QSettings.IniFormat))
    now=time.time();a=str(tmp_path/'a');b=str(tmp_path/'b')
    panel.set_homes([a,b])
    value={'quota':quota(now,30,now+1000),'report':{'home':a,'cycles':[],'at':now,'index_loading':False}}
    assert panel.receive(value)
    assert panel.current['weekly']['value'].text()=='70.0%'
    switched=[];panel.home_selected.connect(switched.append)
    panel.home_choice.setCurrentIndex(1)
    assert switched==[b] and panel.quota is None
    assert panel.current['weekly']['value'].text()=='—'
    assert not panel.current['weekly']['card'].isVisible()
    assert panel.result.text().endswith('—') and not panel.intervals.isVisible()
    assert not panel.receive(value)
    assert panel.quota is None and panel.report['home']==b
    panel.receive({'report':{'home':b,'cycles':[],'manual_reset_pending':True,'index_loading':True}})
    assert not panel.current['weekly']['card'].isVisible()
    assert panel.current['weekly']['bar'].remaining is None
    panel.receive({'report':{'home':b,'cycles':[],'error':True,'index_loading':False},'issue':'원장 오류'})
    assert panel.status.text()=='원장 오류' and panel.status.isVisible()
    assert '확인 불가' not in panel.result.text()
    assert not panel.intervals.isVisible() and not panel.paging.isVisible() and not panel.history.rows
    def walk(node):
        yield node
        for child in node.nodes:yield from walk(child)
    assert not any(node.text()=='사용 한도' for node in walk(panel))
    panel.deleteLater()
