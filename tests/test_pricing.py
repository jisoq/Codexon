import pytest

from cachemonitor.pricing import token_cost, RATES, ALIASES, SUBSCRIPTION_FAST_MULTIPLIERS, COST_KEYS, SUPPORTED_MODELS
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


@pytest.mark.parametrize('tier, expected', [
    ('default', (.02, .008, .025, .02, .073)),
    ('priority', (.05, .02, .0625, .05, .1825)),
])
def test_gpt61_sol_official_rates(tier, expected):
    priced=token_cost(row('gpt-6.1-sol',service_tier=tier))
    assert priced['price_model']=='gpt-6.1-sol'
    assert tuple(priced[key] for key in (
        'cost_uncached','cost_cached','cost_written','cost_output','cost'))==pytest.approx(expected)
    assert priced['price_issues']==[]
    assert token_cost(row('gpt-6-sol'))['cost']==pytest.approx(.081)


@pytest.mark.parametrize('tier, expected', [
    ('default', (.01, .004, .0125, .01, .0365)),
    ('priority', (.025, .01, .03125, .025, .09125)),
])
def test_gpt61_terra_official_rates_and_analysis(tier, expected):
    priced=token_cost(row('gpt-6.1-terra',service_tier=tier))
    assert priced['price_model']=='gpt-6.1-terra'
    assert tuple(priced[key] for key in (
        'cost_uncached','cost_cached','cost_written','cost_output','cost'))==pytest.approx(expected)
    assert priced['price_issues']==[]
    session=Session('terra','h',title='Terra')
    session.add_usage(10000,'terra-call',dict(input_tokens=100000,cached_input_tokens=80000,
        cache_write_input_tokens=10000,output_tokens=2000,reasoning_output_tokens=1000),
        'gpt-6.1-terra','turn','high',service_tier=tier)
    analysis=analyze([session.view(10001)])
    assert len(analysis['responses'])==1
    assert analysis['totals']['cost']==pytest.approx(expected[-1])


def test_subscription_conversion_has_no_long_context_surcharge():
    short=token_cost({**row(),'input':272000})
    long=token_cost({**row(),'input':272001})
    assert not short['long_context'] and not long['long_context']
    assert long['cost_cached']==pytest.approx(short['cost_cached'])
    assert long['cost_written']==pytest.approx(short['cost_written'])
    assert long['cost_output']==pytest.approx(short['cost_output'])
    assert long['cost']-short['cost']==pytest.approx(RATES['gpt-6-astra'].input/1e6)


def test_unknown_prices_or_usage_are_not_zero():
    for change in ({'model':'codex-auto-review'},{'written':None},{'cached':None},{'written':999999}):
        p=token_cost({**row(),**change})
        assert p['cost'] is None and p['price_issue']
        assert p['cost_output'] is None
    assert token_cost(row('gpt-daybreak-blue-latest'))['cost']==token_cost(row('gpt-5.6-sol'))['cost']
    old=token_cost({**row('gpt-5.5'),'written':None,'cached':90000})
    assert old['cost']==pytest.approx(.155)


def test_codex_catalog_new_sol_rates_and_historical_models():
    assert set(SUPPORTED_MODELS)=={'gpt-6.1-sol','gpt-6.1-terra','gpt-6-astra','gpt-6-sol','gpt-6-luna',
        'gpt-5.6-sol','gpt-5.6-terra','gpt-5.6-luna','gpt-5.5'}
    assert set(SUBSCRIPTION_FAST_MULTIPLIERS)==set(RATES)-{'gpt-5.5-pro'}
    # Historical records keep their prices even after removal from the picker.
    assert token_cost(row('gpt-5.4-mini'))['cost']==pytest.approx(.03)
    for alias,model in ALIASES.items():
        assert token_cost(row(alias))['cost']==token_cost(row(model))['cost']
    sol=token_cost(row('gpt-6.1-sol'))
    assert sol['cost']==pytest.approx(.073)
    assert sol['cost_cached']==pytest.approx(.008)
    assert sol['cost_written']==pytest.approx(.025)
    assert sol['cost_output']==pytest.approx(.02)


@pytest.mark.parametrize('tier',['Fast','fast','priority'])
def test_daybreak_fast_is_unpriced_before_resolving_standard_alias(tier):
    daybreak=token_cost(row('gpt-daybreak-blue-latest',service_tier=tier))
    assert all(daybreak[key] is None for key in COST_KEYS)
    assert daybreak['price_issue']=='구독 배율 미확인'


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


def test_owner_signature_preserves_legacy_and_ignores_unused_policy(monkeypatch):
    from dataclasses import replace
    import cachemonitor.pricing as pricing

    legacy=(pricing.VERIFIED,tuple(pricing.RATE_VERIFIED.items()),pricing.MULTIPLIER_VERIFIED,
        pricing.MODEL_VERIFIED,pricing.PRICE_POLICY,tuple(pricing.RATES.items()),
        tuple(pricing.FAST_RATES.items()),tuple(pricing.SUBSCRIPTION_FAST_MULTIPLIERS.items()),
        tuple(pricing.ALIASES.items()))
    assert pricing.pricing_signature()==legacy
    before=pricing.pricing_signature(['gpt-6-astra'])
    monkeypatch.setattr(pricing,'VERIFIED','later')
    monkeypatch.setattr(pricing,'MULTIPLIER_VERIFIED','later')
    monkeypatch.setattr(pricing,'MODEL_VERIFIED','later')
    monkeypatch.setitem(pricing.RATE_VERIFIED,'gpt-6-astra','later')
    monkeypatch.setitem(pricing.RATES,'unused-model',pricing.Rate(1,2,3))
    monkeypatch.setitem(pricing.ALIASES,'unused-alias','unused-model')
    monkeypatch.setattr(pricing,'SUPPORTED_MODELS',())
    monkeypatch.setitem(pricing.RATES,'gpt-6-astra',replace(pricing.RATES['gpt-6-astra'],
        long_threshold=1,promo_minimum='unused'))
    assert pricing.pricing_signature(['gpt-6-astra'])==before
    assert pricing.pricing_signature()!=legacy
    monkeypatch.setitem(pricing.RATES,'gpt-6-astra',replace(pricing.RATES['gpt-6-astra'],input=99))
    assert pricing.pricing_signature(['gpt-6-astra'])!=before


def test_owner_signature_uses_original_fast_rules_and_one_alias_resolution(monkeypatch):
    import cachemonitor.pricing as pricing

    daybreak=pricing.pricing_signature(['gpt-daybreak-blue-latest'])
    sol=pricing.pricing_signature(['gpt-5.6-sol'])
    monkeypatch.setitem(pricing.SUBSCRIPTION_FAST_MULTIPLIERS,'gpt-5.6-sol',99)
    assert pricing.pricing_signature(['gpt-daybreak-blue-latest'])==daybreak
    assert pricing.pricing_signature(['gpt-5.6-sol'])!=sol
    monkeypatch.setitem(pricing.ALIASES,'first','gpt-6-astra')
    before=pricing.pricing_signature(['first'])
    monkeypatch.setitem(pricing.ALIASES,'gpt-6-astra','unused-second-hop')
    assert pricing.pricing_signature(['first'])==before
    monkeypatch.setitem(pricing.ALIASES,'first','gpt-5.6-sol')
    assert pricing.pricing_signature(['first'])!=before


def test_owner_signature_preserves_unsupported_names_and_unknown_bucket_dependencies(monkeypatch):
    import cachemonitor.pricing as pricing

    unknown=pricing.pricing_signature(['unlisted-original'])
    empty=pricing.pricing_signature([])
    bucket=pricing.pricing_signature([''])
    assert bucket==pricing.pricing_signature(['미확인'])
    assert empty!=bucket and unknown!=bucket
    monkeypatch.setitem(pricing.RATES,'미확인',pricing.Rate(1,2,3))
    assert pricing.pricing_signature([''])!=bucket
    assert pricing.pricing_signature([])==empty
    assert pricing.pricing_signature(['unlisted-original'])==unknown
    monkeypatch.setitem(pricing.RATES,'unlisted-original',pricing.Rate(1,2,3))
    assert pricing.pricing_signature(['unlisted-original'])!=unknown
