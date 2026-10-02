"""Standard API token rates weighted by included subscription usage.

Verified 2026-09-23 against OpenAI's pricing and individual model pages.
GPT-6.1 Sol added from its official model pricing on 2026-09-30.
Included-subscription Fast multiplier verified on 2026-10-01.
All amounts are USD per million tokens. Cache writes replace ordinary input
pricing for the written portion; they are not added at full price a second time.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

VERIFIED = '2026-09-23'
PRICE_POLICY = 'subscription-standard-api-equivalent-v6'
MULTIPLIER_VERIFIED = '2026-10-01'
FAST_MULTIPLIER = 2.5
SUPPORTED_MODELS = ('gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna',
                    'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.5')
MODEL_VERIFIED = '2026-09-30'
RATE_VERIFIED = {'gpt-6.1-sol': '2026-09-30'}
PROFILE_NAME = VERIFIED + ' 단가 / ' + MULTIPLIER_VERIFIED + ' 구독 배율'
SOURCE = 'https://developers.openai.com/api/docs/pricing'
MODEL_SOURCE = 'https://learn.chatgpt.com/docs/models'
MULTIPLIER_SOURCE = 'https://learn.chatgpt.com/docs/agent-configuration/speed'
CACHE_SOURCE = 'https://developers.openai.com/api/docs/guides/prompt-caching'
COST_COMPONENTS = ('cost_uncached', 'cost_cached', 'cost_written', 'cost_unclassified', 'cost_output')
COST_KEYS = ('cost', 'cost_input', 'cost_uncached', 'cost_cached', 'cost_written', 'cost_unclassified',
             'cost_output', 'cost_reasoning', 'cost_non_reasoning')


@dataclass(frozen=True)
class Rate:
    input: float
    cached: float | None
    output: float
    written: float | None = None
    long_threshold: int | None = 272000
    promo_minimum: str = ''


RATES = {
    'gpt-5.4-mini': Rate(.75, .075, 4.5, long_threshold=None),
    'gpt-5.5': Rate(5, .5, 30),
    'gpt-5.5-pro': Rate(30, None, 180),
    'gpt-5.6-sol': Rate(4, .4, 20, 5, promo_minimum='2026-11-21'),
    'gpt-5.6-terra': Rate(2, .2, 12, 2.5),
    'gpt-5.6-luna': Rate(.2, .02, 1.2, .25),
    'gpt-6-astra': Rate(10, 1, 50, 12.5),
    'gpt-6.1-sol': Rate(2, .1, 10, 2.5),
    'gpt-6-sol': Rate(2, .2, 10, 2.5),
    'gpt-6-luna': Rate(.1, .01, .5, .125),
}
ALIASES = {
    'gpt-5.6': 'gpt-5.6-sol',
    'gpt-daybreak-blue-latest': 'gpt-5.6-sol',
    'gpt-5.4-mini-2026-03-17': 'gpt-5.4-mini',
    'gpt-5.5-2026-04-23': 'gpt-5.5',
}
# Included subscription usage only. Purchased credits use a different multiplier.
SUBSCRIPTION_FAST_MULTIPLIERS = dict.fromkeys((model for model in RATES if model != 'gpt-5.5-pro'), FAST_MULTIPLIER)
FAST_RATES = {model: replace(RATES[model], **{
    key: value * multiplier if value is not None else None
    for key, value in vars(RATES[model]).items() if key in ('input', 'cached', 'output', 'written')})
    for model, multiplier in SUBSCRIPTION_FAST_MULTIPLIERS.items()}


def supported_model(model):
    return ALIASES.get(model, model) in SUPPORTED_MODELS


def standard_rate(model):
    return RATES.get(ALIASES.get(model, model))


def subscription_multiplier(model, tier):
    if tier == 'Standard' and standard_rate(model) is not None:
        return 1.0
    # Check the selected Codex model before resolving its pricing alias.
    # Daybreak Blue has Standard prices but does not offer Fast.
    if tier != 'Fast' or model == 'gpt-daybreak-blue-latest':
        return None
    return SUBSCRIPTION_FAST_MULTIPLIERS.get(ALIASES.get(model, model))


def request_rate(row):
    model, tier = row.get('model'), request_tier(row)
    if subscription_multiplier(model, tier) is None:
        return None
    return FAST_RATES.get(ALIASES.get(model, model)) if tier == 'Fast' else standard_rate(model)


def rate_verified(model):
    return RATE_VERIFIED.get(ALIASES.get(model, model), VERIFIED)


def pricing_signature():
    return (VERIFIED, tuple(RATE_VERIFIED.items()), MULTIPLIER_VERIFIED, MODEL_VERIFIED,
            PRICE_POLICY, tuple(RATES.items()), tuple(FAST_RATES.items()),
            tuple(SUBSCRIPTION_FAST_MULTIPLIERS.items()), tuple(ALIASES.items()))


def request_tier(row):
    value=row.get('service_tier')
    if row.get('mode_conflict') or row.get('service_tier_source') == 'conflict':
        return '미확인'
    return {'priority':'Fast','fast':'Fast','default':'Standard','standard':'Standard'}.get(value,value or '미확인')


def display_tier(row):
    """Classification is identical in every view and never assumes Standard."""
    tier = row.get('display_service_tier') or request_tier(row)
    return tier


def unknown_mode_calls(row):
    return row.get('unknown_mode_calls', int(request_tier(row) == '미확인'))


def mode_count_note(row):
    count = row.get('unknown_mode_samples', unknown_mode_calls(row))
    return f"미확인 {count:,}{row.get('mode_count_unit', '')}" if count else ''


def usd(value):
    if value is None:
        return '—'
    if 0<value<.0000005:return '<$0.000001'
    precision = 6 if 0 < abs(value) < .001 else 4 if abs(value) < 1 else 2
    return f'${value:,.{precision}f}'


def _token_cost(row):
    model = ALIASES.get(row.get('model'), row.get('model'))
    tier=request_tier(row)
    rate = request_rate(row)
    result = {key: None for key in COST_KEYS}
    result.update(price_model=model, price_tier=tier, price_assumed=False, price_issue='', long_context=False,
                  price_profile=rate_verified(model) + ' 단가 / ' + MULTIPLIER_VERIFIED + ' 구독 배율')
    if tier not in ('Fast','Standard'):
        result['price_issue']='요청 모드 미확인' if tier=='미확인' else '요청 모드 단가 미지원'
        return result
    if rate is None:
        result['price_issue'] = '구독 배율 미확인' if model in RATES else '기준 단가 미확인'
        return result
    inp, cached, output = row.get('input'), row.get('cached'), row.get('output')
    if any(type(v) is not int or v < 0 for v in (inp, output)):
        result['price_issue'] = '입력/출력 미확인'
        return result
    # Subscription usage is compared at base API rates, without API context surcharges.
    p_input, p_cached, p_output = rate.input, rate.cached, rate.output
    result['cost_output'] = output*p_output/1e6
    reasoning = row.get('reasoning')
    if type(reasoning) is int and 0 <= reasoning <= output:
        result['cost_reasoning'] = reasoning*p_output/1e6
        result['cost_non_reasoning'] = (output-reasoning)*p_output/1e6
    if type(cached) is not int or cached < 0 or cached > inp:
        result.update(dict.fromkeys(COST_KEYS))
        result['price_issue'] = '캐시 읽기 미확인'
        return result
    if cached and p_cached is None:
        result.update(dict.fromkeys(COST_KEYS))
        result['price_issue'] = '캐시 요율 미확인'
        return result
    written = row.get('written')
    if written is not None and (type(written) is not int or written < 0 or written > inp-cached):
        result.update(dict.fromkeys(COST_KEYS))
        result['price_issue'] = '캐시 쓰기 미확인/범위 오류'
        return result
    if row.get('input_conflict'):
        result['price_issue']='입력 구성 관측 충돌'
        return result
    if written is None and rate.written is not None:
        result.update(dict.fromkeys(COST_KEYS))
        result['price_issue'] = '캐시 쓰기 미확인'
        return result
    result['cost_uncached'] = (inp-cached-written)*p_input/1e6 if written is not None else 0.0
    result['cost_unclassified'] = (inp-cached)*p_input/1e6 if written is None else 0.0
    result['cost_cached'] = cached*(p_cached or 0)/1e6
    result['cost_written'] = (written or 0)*(rate.written if rate.written is not None else rate.input)/1e6
    result['cost_input'] = sum(result[k] for k in COST_COMPONENTS[:-1])
    result['cost'] = result['cost_input']+result['cost_output']
    return result


def sum_cost(rows, strict=False):
    rows = list(rows)
    out = {}
    for key in COST_KEYS:
        known = [r[key] for r in rows if r.get(key) is not None]
        out[key] = None if (strict and len(known) != len(rows)) or (rows and not known) else sum(known)
    return out


def price_note():
    note = f'{PROFILE_NAME} / Standard API 단가 × 구독 차감 배율'
    if date.today().isoformat() > '2026-11-21':
        note += ' / GPT-5.6 Sol 프로모션 단가 재확인 필요'
    return note


def mode_assumptions(rows):
    """Separate optional scenarios; never change observed modes or base totals."""
    unknown = [r for r in rows if request_tier(r) == '미확인']
    result = {}
    for mode in ('Standard', 'Fast'):
        values = [token_cost({**r, 'service_tier': mode, 'mode_conflict': False,
                              'service_tier_source': 'assumption'})['cost'] for r in unknown]
        known = [v for v in values if v is not None]
        result[mode] = {'total': sum(known) if len(known) == len(values) else None,
                        'partial_sum': sum(known) if known or not values else None,
                        'n': len(known), 'N': len(values), 'missing': len(values)-len(known)}
    return result


def token_cost(row):
    result=_token_cost(row)
    issues=[]
    if result['cost'] is None:
        model=ALIASES.get(row.get('model'),row.get('model'))
        mode=request_tier(row)
        rate=request_rate(row)
        if not model:issues.append('분석 모델 미확인')
        elif model not in RATES:issues.append('모델 기준 단가 미확인')
        if mode=='미확인':issues.append('요청 모드 미확인')
        elif mode not in ('Standard','Fast'):issues.append('요청 모드 단가 미지원')
        elif model in RATES and rate is None:issues.append('구독 배율 미확인')
        inp,cached,written,output=(row.get(k) for k in ('input','cached','written','output'))
        valid=lambda value:type(value) is int and value>=0
        if not valid(inp):issues.append('입력 토큰 미확인')
        if not valid(output):issues.append('출력 토큰 미확인')
        if not valid(cached):issues.append('캐시 읽기 미확인')
        elif valid(inp) and cached>inp:issues.append('캐시 읽기 범위 오류')
        if row.get('input_conflict'):issues.append('입력 구성 관측 충돌')
        if written is not None and (not valid(written) or valid(inp) and valid(cached) and written>inp-cached):issues.append('캐시 쓰기 범위 오류')
        elif written is None and rate and rate.written is not None:issues.append('캐시 쓰기 미확인')
        if rate and cached and rate.cached is None:issues.append('캐시 읽기 단가 미지원')
        if not issues:issues.append(result['price_issue'])
    result['price_issues']=issues
    return result
