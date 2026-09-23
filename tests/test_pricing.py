import pytest

from cachemonitor.pricing import token_cost, RATES
from cachemonitor.analytics import analyze
from cachemonitor.core import Session


def row(model='gpt-6-astra', **kwargs):
    kwargs.setdefault('service_tier','Standard')
    return dict(model=model,input=100000,cached=80000,written=10000,output=2000,reasoning=1000,**kwargs)


def test_disjoint_cache_write_and_reasoning_costs():
    p=token_cost(row())
    assert p['cost']==pytest.approx(.405)
    assert p['cost_input']==pytest.approx(.305)
    assert p['cost_reasoning']==pytest.approx(.05)
    assert p['cost']==pytest.approx(p['cost_uncached']+p['cost_cached']+p['cost_written']+p['cost_output'])
    assert p['cost_output']==pytest.approx(p['cost_reasoning']+p['cost_non_reasoning'])
    assert token_cost(row('gpt-5.6-sol'))['cost']==pytest.approx(.162)


def test_long_context_threshold_is_per_call_not_aggregate():
    short=token_cost({**row(),'input':272000})
    long=token_cost({**row(),'input':272001})
    assert not short['long_context'] and long['long_context']
    assert long['cost_cached']==pytest.approx(2*short['cost_cached'])
    assert long['cost_output']==pytest.approx(1.5*short['cost_output'])
    mini=token_cost({**row('gpt-5.4-mini'),'input':300000,'cached':200000,'written':None,'output':1000})
    assert not mini['long_context']
    assert mini['cost']==pytest.approx(.0945)


def test_unknown_prices_or_usage_are_not_zero():
    for change in ({'model':'codex-auto-review'},{'written':None},{'cached':None},{'written':999999}):
        p=token_cost({**row(),**change})
        assert p['cost'] is None and p['price_issue']
        assert p['cost_output'] is None
    assert token_cost(row('gpt-5.6'))['cost']==token_cost(row('gpt-5.6-sol'))['cost']
    old=token_cost({**row('gpt-5.5'),'written':None,'cached':90000})
    assert old['cost']==pytest.approx(.155)


@pytest.mark.parametrize('model,rates', [('gpt-6-sol',(2,.2,2.5,10)),
                                       ('gpt-6-luna',(.1,.01,.125,.5))])
@pytest.mark.parametrize('mode,factor', [('Standard',1),('Fast',2)])
@pytest.mark.parametrize('tokens,long', [(272000,False),(272001,True)])
def test_gpt6_sol_luna_official_prices(model,rates,mode,factor,tokens,long):
    p=token_cost({**row(model,service_tier=mode),'input':tokens})
    inp,cached,written,output=rates
    assert p['long_context'] is long
    assert p['cost_uncached']==pytest.approx((tokens-90000)*inp*factor*(2 if long else 1)/1e6)
    assert p['cost_cached']==pytest.approx(80000*cached*factor*(2 if long else 1)/1e6)
    assert p['cost_written']==pytest.approx(10000*written*factor*(2 if long else 1)/1e6)
    assert p['cost_output']==pytest.approx(2000*output*factor*(1.5 if long else 1)/1e6)
    assert p['cost']==pytest.approx(sum(p[k] for k in ('cost_uncached','cost_cached','cost_written','cost_output')))
    assert token_cost({**row(model),'written':None})['cost'] is None


def test_new_prices_revalue_calls_and_archive_previous_costs(tmp_path):
    from cachemonitor.quota_cycles import QuotaLedger,PRICE_ID
    from test_quota_cycles import sync,row as ledger_row
    path=tmp_path/'prices.sqlite'
    ledger=QuotaLedger(path)
    sync(ledger,[ledger_row('sol',model='gpt-6-sol'),ledger_row('luna',model='gpt-6-luna')])
    ledger.db.execute("update calls set cost=null,price_id='previous-price-table'")
    ledger.db.commit();ledger.close()
    ledger=QuotaLedger(path)
    try:
        calls=list(ledger.db.execute('select * from calls'))
        assert len(calls)==2 and all(r['cost'] is not None and r['price_id']==PRICE_ID for r in calls)
        archive=list(ledger.db.execute("select * from cost_archive where price_id='previous-price-table'"))
        assert len(archive)==2 and all(r['cost'] is None for r in archive)
    finally:ledger.close()


def test_call_and_turn_cost_use_the_same_completed_population():
    s=Session('s','h',title='task')
    for i,turn in enumerate(('one','one','one','two','open')):
        s.add_usage(10000+i,str(i),{'input_tokens':100000,'cached_input_tokens':80000,'cache_write_input_tokens':10000,'output_tokens':2000,'reasoning_output_tokens':1000},'gpt-6-astra',turn,'high', service_tier='Standard')
    view=s.view(10010)
    view['turn_states']={'one':'완료','two':'완료','open':'진행'}
    view['turn_records']={'one':{'started_at':10000,'ended_at':10002.5,'state':'완료'},
                          'two':{'started_at':10003,'ended_at':10003.5,'state':'완료'}}
    a=analyze([view])
    b=analyze([view],unit='turn')
    assert a['totals']['cost']==pytest.approx(5*.405)
    assert b['totals']['cost']==pytest.approx(4*.405)
    # Both columns in the side-by-side comparison use the same four calls.
    assert a['basis']==b['basis']
    paired=a['basis'][0]
    assert paired['calls']==4 and paired['turns']==2
    assert paired['call_cost']==pytest.approx(.405)
    assert paired['turn_cost']==pytest.approx(.81)
    assert paired['call_cost']*paired['calls']==pytest.approx(paired['turn_cost']*paired['turns'])
