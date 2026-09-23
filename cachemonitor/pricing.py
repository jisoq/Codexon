"""Request-tier API token-equivalent USD, not a historical Codex bill.

Verified 2026-09-23 against OpenAI's pricing and individual model pages.
All amounts are USD per million tokens. Cache writes replace ordinary input
pricing for the written portion; they are not added at full price a second time.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date

VERIFIED = '2026-09-23'
PRICE_POLICY = 'observed-request-mode-v4'
PROFILE_NAME = VERIFIED + ' 기준'
SOURCE = 'https://developers.openai.com/api/docs/pricing'
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
    'gpt-6-sol': Rate(2, .2, 10, 2.5),
    'gpt-6-luna': Rate(.1, .01, .5, .125),
}
ALIASES = {
    'gpt-5.6': 'gpt-5.6-sol',
    'gpt-daybreak-blue-latest': 'gpt-5.6-sol',
    'gpt-5.4-mini-2026-03-17': 'gpt-5.4-mini',
    'gpt-5.5-2026-04-23': 'gpt-5.5',
}
FAST_RATES = {m:replace(r,input=r.input*2,cached=r.cached*2,output=r.output*2,written=r.written*2)
              for m,r in RATES.items() if m.startswith(('gpt-5.6-','gpt-6-'))}
FAST_RATES.update({'gpt-5.5':Rate(12.5,1.25,75,long_threshold=None),
                   'gpt-5.4-mini':Rate(1.5,.15,9,long_threshold=None)})


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
    rate = (FAST_RATES if tier=='Fast' else RATES if tier=='Standard' else {}).get(model)
    result = {key: None for key in COST_KEYS}
    result.update(price_model=model, price_tier=tier, price_assumed=False, price_issue='', long_context=False,
                  price_profile=PROFILE_NAME)
    if tier not in ('Fast','Standard'):
        result['price_issue']='요청 모드 미확인' if tier=='미확인' else '요청 모드 단가 미지원'
        return result
    if rate is None:
        result['price_issue'] = '기준 단가 미확인'
        return result
    inp, cached, output = row.get('input'), row.get('cached'), row.get('output')
    if any(type(v) is not int or v < 0 for v in (inp, output)):
        result['price_issue'] = '입력/출력 미확인'
        return result
    if tier=='Fast' and model=='gpt-5.5' and inp>272000:
        result['price_issue']='Fast 장문 단가 미확인'
        return result
    long = rate.long_threshold is not None and inp > rate.long_threshold
    result['long_context'] = long
    p_input = rate.input * (2 if long else 1)
    p_cached = rate.cached * (2 if long else 1) if rate.cached is not None else None
    p_output = rate.output * (1.5 if long else 1)
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
    result['cost_written'] = (written or 0)*(rate.written if rate.written is not None else rate.input)*(2 if long else 1)/1e6
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
    note = f'{PROFILE_NAME} · 고정 단가 환산 · 청구액 아님'
    if date.today().isoformat() > '2026-11-21':
        note += ' · GPT-5.6 Sol 프로모션 단가 재확인 필요'
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
        rate=(FAST_RATES if mode=='Fast' else RATES if mode=='Standard' else {}).get(model)
        if not model:issues.append('분석 모델 미확인')
        elif model not in RATES:issues.append('모델 기준 단가 미확인')
        if mode=='미확인':issues.append('요청 모드 미확인')
        elif mode not in ('Standard','Fast') or model in RATES and rate is None:issues.append('요청 모드 단가 미지원')
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
        if mode=='Fast' and model=='gpt-5.5' and valid(inp) and inp>272000:issues.append('Fast 장문 단가 미확인')
        if not issues:issues.append(result['price_issue'])
    result['price_issues']=issues
    return result
