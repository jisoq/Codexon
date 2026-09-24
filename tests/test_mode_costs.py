import copy
import pytest
from cachemonitor.core import Session
from cachemonitor.pricing import token_cost,display_tier,mode_assumptions,COST_COMPONENTS
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
    for model in ('gpt-6-astra','gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','gpt-5.4-mini'):
        for inp in (100000,272001):
            base={**usage(model=model),'input':inp}
            standard=token_cost(base);fast=token_cost(dict(base,service_tier='Fast'))
            assert fast['cost']==pytest.approx(2*standard['cost'])
            assert sum(fast[k] for k in COST_COMPONENTS)==pytest.approx(fast['cost'])
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


def test_modes_and_mixed_request_keep_exact_metric_denominators():
    a=analyze([history()]);overview=overview_view(a,1000000,1000010)
    assert (overview['call_stats']['n'],overview['call_stats']['missing'])==(5,1)
    assert overview['total']==pytest.approx(2.835)
    assert overview['turn_stats']['n']==3 and overview['completed']==4
    standard=analyze([history()],service_tier='Standard')
    unknown=analyze([history()],service_tier='미확인')
    assert len(standard['responses'])==3 and len(unknown['responses'])==1
    assert next(t for t in standard['turns'] if t['turn']=='mixed')['complete'] is False
    assert next(t for t in a['turns'] if t['turn']=='mixed')['service_tier']=='혼합'
    assert unknown['totals']['cost'] is None and unknown['totals']['reasoning']==1000
    assert display_tier(unknown['responses'][0])=='미확인'


def test_price_corrections_invalidate_worker_and_ledger_without_reclassifying_unknown(tmp_path):
    source=history();engine=AnalysisEngine();engine.ingest([source]);ledger=QuotaLedger(tmp_path/'ledger.sqlite')
    try:
        snap=dict(homes=['h'],sessions=[source],ts=1000010,index={'loading':False})
        ledger.sync(engine,snap);before=engine.metrics['priced_calls']
        changed=copy.deepcopy(source);changed['history'][0]['service_tier']='Fast';engine.ingest([changed])
        ledger.sync(engine,dict(snap,sessions=[changed]))
        assert engine.metrics['priced_calls']==before+1
        assert ledger.db.execute("select cost from calls where uid='0'").fetchone()[0]==pytest.approx(.81)
        assert ledger.db.execute("select cost from calls where uid='5'").fetchone()[0] is None
        q=dict(page=0,start=1000000,end=1000010,service_tier='Standard')
        result=engine.query(q)
        assert result['overview']['calls']==2
        assert all(r['service_tier']=='Standard' for r in result['analysis']['responses'])
    finally:ledger.close()


def test_unknown_price_migration_retains_original_mode_and_excludes_base_amount(tmp_path):
    from cachemonitor.quota_cycles import PRICE_ID
    source=history();engine=AnalysisEngine();engine.ingest([source]);path=tmp_path/'ledger.sqlite'
    ledger=QuotaLedger(path);ledger.sync(engine,dict(homes=['h'],sessions=[source],ts=1000010,index={'loading':False}))
    ledger.db.execute("update calls set cost=.405,price_id='old-standard-assumption' where uid='5'")
    ledger.db.commit();ledger.close();restored=QuotaLedger(path)
    try:
        row=restored.db.execute("select cost,service_tier,price_id from calls where uid='5'").fetchone()
        assert row['cost'] is None and row['service_tier']=='미확인' and row['price_id']==PRICE_ID
    finally:restored.close()
