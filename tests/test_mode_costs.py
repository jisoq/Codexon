import copy
import pytest
from cachemonitor.core import Session
from cachemonitor.pricing import token_cost,display_tier,mode_assumptions,COST_COMPONENTS,COST_KEYS
from cachemonitor.analytics import analyze, overview_view
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.quota_cycles import QuotaLedger


def usage(tier='Standard',model='gpt-6-astra',**extra):
    return {'model':model,'service_tier':tier,'input':100000,'cached':80000,'written':10000,'output':2000,'reasoning':1000,**extra}


def history():
    session=Session('s','h',title='Modes')
    for i,(turn,tier) in enumerate([('standard','Standard'),('standard','Standard'),('fast','Fast'),
                                   ('mixed','Standard'),('mixed','Fast'),('unknown','미확인')]):
        session.add_usage(1_000_000+i,str(i),dict(input_tokens=100000,cached_input_tokens=80000,
            cache_write_input_tokens=10000,output_tokens=2000,reasoning_output_tokens=1000),'gpt-6-astra',turn,'high',service_tier=tier)
    view=session.view(1_000_010);view['turn_states']={t:'완료' for t in ('standard','fast','mixed','unknown')}
    view['turn_records']={turn:dict(started_at=a,ended_at=b,state='완료') for turn,a,b in (
        ('standard',1000000,1000001.5),('fast',1000002,1000002.5),('mixed',1000003,1000004.5),('unknown',1000005,1000005.5))}
    return view


def test_fixed_fast_rates_thresholds_and_unknown_is_never_base_cost():
    for model in ('gpt-6-astra','gpt-6-sol','gpt-6.1-sol','gpt-6-luna','gpt-5.5',
                  'gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','gpt-5.4-mini'):
        for inp in (100000,272001):
            base={**usage(model=model),'input':inp}
            standard=token_cost(base);fast=token_cost(dict(base,service_tier='Fast'))
            for key in COST_KEYS:
                assert fast[key]==pytest.approx(2.5*standard[key])
            assert sum(fast[k] for k in COST_COMPONENTS)==pytest.approx(fast['cost'])
            for component in COST_COMPONENTS:
                assert fast[component]==pytest.approx(2.5*standard[component])
        unknown=usage('미확인',model)
        assert token_cost(unknown)['cost'] is None and not token_cost(unknown)['price_assumed']
        assumptions=mode_assumptions([unknown])
        assert assumptions['Standard']['total']==pytest.approx(token_cost(usage(model=model))['cost'])
        assert assumptions['Fast']['total']==pytest.approx(token_cost(usage('Fast',model))['cost'])
        assert unknown['service_tier']=='미확인'
    long_fast=token_cost({**usage('Fast','gpt-5.5'),'input':272001})
    assert long_fast['cost']==pytest.approx((182001*12.5+80000*1.25+10000*12.5+2000*75)/1e6)
    assert token_cost(usage('Fast','gpt-5.5-pro'))['cost'] is None
    assert token_cost(usage('auto'))['cost'] is None


def test_modes_and_mixed_request_keep_exact_metric_denominators(tmp_path):
    a=analyze([history()]);overview=overview_view(a,1000000,1000010)
    assert (overview['call_stats']['n'],overview['call_stats']['missing'])==(5,1)
    assert overview['total']==pytest.approx(3.24)
    assert overview['turn_stats']['n']==3 and overview['completed']==4
    standard=analyze([history()],service_tier='Standard')
    unknown=analyze([history()],service_tier='미확인')
    assert len(standard['responses'])==3 and len(unknown['responses'])==1
    assert next(t for t in standard['turns'] if t['turn']=='mixed')['complete'] is False
    assert next(t for t in a['turns'] if t['turn']=='mixed')['service_tier']=='혼합'
    assert unknown['totals']['cost'] is None and unknown['totals']['reasoning']==1000
    assert display_tier(unknown['responses'][0])=='미확인'
    from cachemonitor.overlay_data import OverlaySummaries
    source=history();source['history']=source['history'][:-1]
    engine=AnalysisEngine();engine.ingest([source]);ledger=QuotaLedger(tmp_path/'totals.sqlite')
    try:
        ledger.observe('h',dict(windows={'weekly':dict(used_percent=10,resets_at=2000000,window_minutes=10080)},
                               plan_type='pro',observed_at=999999,source='live',account='a'))
        ledger.observe('h',dict(windows={'weekly':dict(used_percent=20,resets_at=2000000,window_minutes=10080)},
                               plan_type='pro',observed_at=1000010,source='live',account='a'))
        ledger.sync(engine,dict(homes=['h'],sessions=[source],ts=1000010,index={'loading':False}))
        overlay=OverlaySummaries().collect(engine)[0]
        report=ledger.report('h',1000010)
        assert overlay['cost']==pytest.approx(overview['total'])
        assert engine.query(dict(page=2,start=999999,end=1000010))['session_costs'][('h','s')]['cost']==pytest.approx(overview['total'])
        assert report['cycles'][0]['cost']==pytest.approx(overview['total'])
    finally:ledger.close()


def test_price_corrections_invalidate_worker_and_ledger_without_reclassifying_unknown(tmp_path):
    source=history();engine=AnalysisEngine();engine.ingest([source]);ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        snap=dict(homes=['h'],sessions=[source],ts=1000010,index={'loading':False})
        ledger.sync(engine,snap);before=engine.metrics['priced_calls']
        changed=copy.deepcopy(source);changed['history'][0]['service_tier']='Fast';engine.ingest([changed])
        ledger.sync(engine,dict(snap,sessions=[changed]))
        assert engine.metrics['priced_calls']==before+1
        assert ledger.db.execute("select cost from calls where uid='0'").fetchone()[0]==pytest.approx(1.0125)
        assert ledger.db.execute("select cost from calls where uid='5'").fetchone()[0] is None
        q=dict(page=0,start=1000000,end=1000010,service_tier='Standard')
        result=engine.query(q)
        assert result['overview']['calls']==2
        assert all(r['service_tier']=='Standard' for r in result['analysis']['responses'])
    finally:ledger.close()


def test_price_policy_change_invalidates_revision_shortcut_and_overlay(monkeypatch):
    from dataclasses import replace
    from cachemonitor import pricing
    from cachemonitor.overlay_data import OverlaySummaries
    source=history();source['usage_revision']=1
    engine=AnalysisEngine();overlays=OverlaySummaries();engine.ingest([source])
    first=overlays.collect(engine)[0]
    assert not engine.ingest([source])
    rate=pricing.RATES['gpt-6-astra']
    monkeypatch.setitem(pricing.SUBSCRIPTION_FAST_MULTIPLIERS,'gpt-6-astra',3)
    monkeypatch.setitem(pricing.FAST_RATES,'gpt-6-astra',replace(rate,input=30,cached=3,written=37.5,output=150))
    assert engine.ingest([source])
    assert overlays.collect(engine)[0]['cost']==pytest.approx(3.645)
    assert first['cost']==pytest.approx(3.24)
    assert engine.record('h','s','5')['cost'] is None


def test_unknown_price_migration_retains_original_mode_and_excludes_base_amount(tmp_path):
    from cachemonitor.quota_cycles import PRICE_ID
    source=history();engine=AnalysisEngine();engine.ingest([source]);path=tmp_path/'ledger.sqlite'
    ledger=QuotaLedger(path);ledger.sync(engine,dict(homes=['h'],sessions=[source],ts=1000010,index={'loading':False}))
    ledger.db.execute("update calls set cost=.405,price_id='old-standard-assumption' where uid='5'")
    ledger.db.execute("update calls set cost=.81,price_id='old-fast-2x' where uid='2'")
    ledger.db.commit();ledger.close();restored=QuotaLedger(path)
    try:
        row=restored.db.execute("select cost,service_tier,price_id from calls where uid='5'").fetchone()
        assert row['cost'] is None and row['service_tier']=='미확인' and row['price_id']==PRICE_ID
        fast=restored.db.execute("select * from calls where uid='2'").fetchone()
        assert fast['cost']==pytest.approx(1.0125) and fast['service_tier']=='Fast'
        assert (fast['input'],fast['cached'],fast['written'],fast['output'])==(100000,80000,10000,2000)
        assert restored.db.execute("select cost from cost_archive where uid='2' and price_id='old-fast-2x'").fetchone()[0]==.81
        restored.close();restored=QuotaLedger(path)
        assert restored.db.execute("select count(*) from cost_archive where uid='2'").fetchone()[0]==1
    finally:restored.close()


def test_imported_usage_reprices_without_recreating_retired_permissions(tmp_path):
    import json
    import sqlite3
    from contextlib import closing
    from cachemonitor.usage_archive import sessions
    path=tmp_path/'index.sqlite'
    tokens=dict(input=100000,cached=80000,written=10000,output=2000,reasoning=1000)
    record=dict(tokens,home='fixture',sid='session',ts=2,request_start=1,request_end=2,
        model='gpt-6-astra',effort='high',service_tier='priority',state='completed',purpose='maintenance')
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE usage_archive(home TEXT,response_id TEXT,data TEXT,PRIMARY KEY(home,response_id))')
        db.execute('INSERT INTO usage_archive VALUES(?,?,?)',('fixture','response',json.dumps(record)))
    for _ in range(2):
        with closing(sqlite3.connect(path)) as db:
            history=sessions(db,10,{'fixture'})[0]['history']
            assert len(history)==1 and history[0]['key']=='response'
            assert token_cost(history[0])['cost']==pytest.approx(1.0125)
    assert not path.with_name('cache-control.sqlite').exists()


def test_subscription_policy_migration_archives_old_costs_and_keeps_source_records(tmp_path):
    from cachemonitor.quota_cycles import PRICE_ID, TOKEN_FIELDS
    source=history()
    for index,model in ((0,'gpt-5.5-pro'),(1,'gpt-5.4-mini'),(3,'gpt-5.6'),(4,'gpt-daybreak-blue-latest')):
        source['history'][index].update(model=model,configured_model=model)
    engine=AnalysisEngine();engine.ingest([source]);path=tmp_path/'migration.sqlite'
    snapshot=dict(homes=['h'],sessions=[source],ts=1000010,index={'loading':False})
    ledger=QuotaLedger(path)
    ledger.sync(engine,snapshot)
    ledger.db.execute("insert into prices values('old-v5','{\"policy\":\"old-v5\"}')")
    ledger.db.execute("update calls set cost=.81,price_id='old-v5'")
    ledger.db.commit()
    fields=('home','uid','sid','ts','model','service_tier',*TOKEN_FIELDS)
    original=[tuple(row[key] for key in fields) for row in ledger.db.execute('select * from calls order by uid')]
    ledger.close()
    for _ in range(2):
        restored=QuotaLedger(path)
        try:
            rows=list(restored.db.execute('select * from calls order by uid'))
            assert [tuple(row[key] for key in fields) for row in rows]==original
            assert rows[2]['cost']==pytest.approx(1.0125)
            assert rows[1]['cost']==pytest.approx(.03)
            assert rows[3]['cost']==pytest.approx(.162)
            assert all(rows[index]['cost'] is None for index in (0,4,5))
            assert all(row['price_id']==PRICE_ID for row in rows)
            restored.sync(engine,snapshot);restored.sync(engine,snapshot)
            archived=list(restored.db.execute("select * from cost_archive where price_id='old-v5'"))
            assert len(archived)==len(rows) and all(row['cost']==.81 for row in archived)
            assert restored.db.execute("select data from prices where id='old-v5'").fetchone()[0]=='{"policy":"old-v5"}'
        finally:restored.close()
