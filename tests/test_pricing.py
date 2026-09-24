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


def test_subscription_conversion_has_no_long_context_surcharge():
    short=token_cost({**row(),'input':272000})
    long=token_cost({**row(),'input':272001})
    assert not short['long_context'] and not long['long_context']
    assert long['cost_cached']==pytest.approx(short['cost_cached'])
    assert long['cost_written']==pytest.approx(short['cost_written'])
    assert long['cost_output']==pytest.approx(short['cost_output'])
    assert long['cost']-short['cost']==pytest.approx(RATES['gpt-6-astra'].input/1e6)
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
