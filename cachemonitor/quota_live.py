"""Read-only usage with an existing access token; never own the login lifecycle."""
import base64
import hashlib
import json
import os
from pathlib import Path
import ssl
import threading
import time
import tomllib
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from .quota import clean_limits
from .banked_resets import normalize_reset_credits


def normalize_credits(value):
    """Keep a reported balance exact; missing or invalid balances stay unknown."""
    if not isinstance(value, dict):
        return None
    balance = value.get('balance')
    try:
        number = Decimal(balance) if isinstance(balance, str) else None
        balance = format(number, 'f') if number is not None and number.is_finite() and number >= 0 else None
    except InvalidOperation:
        balance = None
    return {'balance': balance, 'unlimited': value.get('unlimited') is True,
            'has_credits': value.get('hasCredits') if type(value.get('hasCredits')) is bool else None}


def normalize_limits(result, observed, account):
    buckets = result.get('rateLimitsByLimitId')
    raw = buckets.get('codex') if isinstance(buckets, dict) else result.get('rateLimits')
    if not raw or raw.get('limitId') not in (None, 'codex'):
        raise ValueError('Codex 공용 한도 정보가 없습니다')
    converted = {'limit_id': 'codex', 'plan_type': raw.get('planType')}
    for name in ('primary', 'secondary'):
        window = raw.get(name)
        converted[name] = {'used_percent': window.get('usedPercent'),
                           'window_minutes': window.get('windowDurationMins'),
                           'resets_at': window.get('resetsAt')} if isinstance(window, dict) else None
    clean = clean_limits(converted)
    if clean is None:
        raise ValueError('사용 가능한 한도 창이 없습니다')
    # Current Pro policy has no five-hour cap. Apply only to a successful live
    # response with a valid weekly window and an explicitly absent second window.
    # Reported five-hour limits, malformed windows and local records take precedence.
    if (clean['plan_type'] == 'pro' and 'weekly' in clean['windows']
            and not clean['has_five_hour'] and not clean['window_conflicts']
            and any(name in raw and raw[name] is None for name in ('primary', 'secondary'))
            and all(raw.get(name) is None or (isinstance(raw[name], dict)
                    and raw[name].get('windowDurationMins') == 10080)
                    for name in ('primary', 'secondary'))):
        clean['unlimited_windows'] = ['five_hour']
    separate = sorted({b['normalModelSlug'] for key, b in (buckets or {}).items()
                       if key != 'codex' and isinstance(b, dict) and b.get('normalModelSlug')})
    return {**clean, 'observed_at': observed, 'source': 'live', 'max_age': 90,
            'account': account, 'bucket': 'codex', 'separate_models': separate,
            'credits': normalize_credits(raw.get('credits')),
            'reset_credits':normalize_reset_credits(result.get('rateLimitResetCredits'))}


def token_claims(token):
    try:
        part=token.split('.')[1]
        value=json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4)))
        return value if isinstance(value,dict) else {}
    except (ValueError,IndexError,TypeError):
        return {}


def saved_access(home):
    """Read credentials without copying, renewing or writing them anywhere."""
    try:
        config=Path(home)/'config.toml'
        settings=tomllib.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
        if settings.get('cli_auth_credentials_store') not in (None,'file'):
            raise RuntimeError('현재 로그인 저장 방식에서는 로컬 관측 기록을 사용합니다.')
        if settings.get('chatgpt_base_url') not in (None,'https://chatgpt.com/backend-api','https://chatgpt.com/backend-api/'):
            raise RuntimeError('사용자 지정 인증 서버에서는 로컬 관측 기록을 사용합니다.')
        raw=(Path(home)/'auth.json').read_bytes()
        auth=json.loads(raw)
        tokens=auth.get('tokens') or {}
        access=tokens.get('access_token')
        account=tokens.get('account_id')
        if auth.get('auth_mode') not in (None,'chatgpt','chatgptAuthTokens') or not all(
                isinstance(v,str) and v for v in (access,account)):
            raise ValueError()
        claims=token_claims(access)
        expiry=claims.get('exp')
        if type(expiry) in (int,float) and expiry<=time.time()+15:
            raise RuntimeError('로그인 토큰 갱신 대기: ChatGPT 앱에서 로그인 상태를 확인하세요.')
        identity=token_claims(tokens.get('id_token') or '').get('email') or account
        key=hashlib.sha256(str(identity).encode()).hexdigest()
        return access,account,key,hashlib.sha256(raw).digest()
    except (OSError,ValueError,AttributeError,TypeError):
        raise RuntimeError('읽을 수 있는 로그인 정보가 없습니다. 로컬 관측 기록을 사용합니다.') from None


def normalize_usage(value, observed, account):
    """Translate the usage endpoint into the existing quota normalization contract."""
    if not isinstance(value,dict) or not isinstance(value.get('rate_limit'),dict):
        raise ValueError('사용 가능한 한도 정보가 없습니다')
    def window(raw):
        if raw is None:return None
        if not isinstance(raw,dict):return {}
        seconds=raw.get('limit_window_seconds')
        return dict(usedPercent=raw.get('used_percent'),resetsAt=raw.get('reset_at'),
                    windowDurationMins=seconds/60 if type(seconds) in (int,float) else None)
    limits=value['rate_limit']
    credits=value.get('credits')
    primary=dict(limitId='codex',planType=value.get('plan_type'),
                 primary=window(limits.get('primary_window')),secondary=window(limits.get('secondary_window')),
                 credits={**credits,'hasCredits':credits.get('has_credits')} if isinstance(credits,dict) else None)
    # An absent field is unknown, not an explicitly absent limit.
    for name in ('primary','secondary'):
        if name+'_window' not in limits:primary.pop(name)
    buckets={'codex':primary}
    for item in value.get('additional_rate_limits') or []:
        if isinstance(item,dict) and item.get('metered_feature')!='codex':
            buckets[str(item.get('metered_feature'))]=dict(normalModelSlug=item.get('normal_model_slug'))
    resets=value.get('rate_limit_reset_credits')
    if isinstance(resets,dict):
        def timestamp(value):
            if not isinstance(value,str):return value
            try:
                dt=datetime.fromisoformat(value.replace('Z','+00:00'))
                return dt.timestamp() if dt.tzinfo is not None else None
            except ValueError:return None
        resets={'availableCount':resets.get('available_count'),'credits':resets.get('credits')}
        if isinstance(resets['credits'],list):
            resets['credits']=[{**row,'resetType':'codexRateLimits' if row.get('reset_type')=='codex_rate_limits' else row.get('reset_type'),
                               'grantedAt':timestamp(row.get('granted_at')),'expiresAt':timestamp(row.get('expires_at'))}
                              for row in resets['credits'] if isinstance(row,dict)]
    return normalize_limits(dict(rateLimitsByLimitId=buckets,rateLimitResetCredits=resets),observed,account)


class AccountClient:
    USAGE_URL = 'https://chatgpt.com/backend-api/wham/usage'
    RESET_DETAILS_URL = 'https://chatgpt.com/backend-api/wham/rate-limit-reset-credits'
    _quota_cache = {}
    _quota_lock = threading.Lock()

    def __init__(self, home, *, transport=None):
        self.home = str(home)
        self.client = None
        self.transport = transport
        self.after = 0
        self.cache_seconds = 30

    def read(self, url, headers):
        # Only these two GET destinations receive the cached access token.
        if url not in (self.USAGE_URL,self.RESET_DETAILS_URL):raise ValueError('Unsupported usage URL')
        with self.client.stream('GET',url,headers=headers) as response:
            if response.status_code in (401,403):
                raise RuntimeError('한도 조회 인증 만료: ChatGPT 앱의 로그인 갱신을 기다립니다.')
            if response.status_code!=200:
                raise RuntimeError(f'한도 조회 실패 (HTTP {response.status_code})')
            parts=[];size=0
            for chunk in response.iter_bytes():
                size+=len(chunk)
                if size>1024*1024:raise ValueError('Usage response too large')
                parts.append(chunk)
            return json.loads(b''.join(parts))

    def fetch(self):
        try:
            access,account,key,signature=saved_access(self.home)
            with self._quota_lock:
                cache_key=(key,account)
                cached = self._quota_cache.get(cache_key)
                if cached and time.monotonic()-cached[0]<self.cache_seconds and cached[1]['requested_at']>=self.after:
                    return dict(cached[1])
                if self.client is None:
                    ca=os.environ.get('CODEX_CA_CERTIFICATE') or os.environ.get('SSL_CERT_FILE')
                    self.client=httpx.Client(timeout=httpx.Timeout(12,connect=5),follow_redirects=False,
                                             verify=ssl.create_default_context(cafile=ca),transport=self.transport)
                requested_at = time.time()
                requested_monotonic = time.monotonic()
                headers={'Authorization':'Bearer '+access,'ChatGPT-Account-Id':account}
                result=self.read(self.USAGE_URL,headers)
                if not isinstance(result,dict):raise ValueError('Invalid usage response')
                if result.get('account_id') not in (None,account):raise ValueError('Usage account mismatch')
                resets=result.get('rate_limit_reset_credits')
                if isinstance(resets,dict):
                    try:details=self.read(self.RESET_DETAILS_URL,headers)
                    except (httpx.HTTPError,RuntimeError,ValueError):details=None
                    if isinstance(details,dict) and isinstance(details.get('credits'),list):
                        resets['credits']=details['credits']
                received_at = time.time()
                elapsed = time.monotonic() - requested_monotonic
                if saved_access(self.home)[3]!=signature:
                    raise RuntimeError('한도 조회 중 로그인 변경: 다시 확인합니다')
                quota = {**normalize_usage(result, received_at, key),
                         'requested_at': requested_at, 'elapsed': elapsed}
                self._quota_cache[cache_key] = (time.monotonic(), quota)
                return quota
        except (httpx.HTTPError,ValueError):
            self.close()
            raise RuntimeError('한도 조회 응답을 확인하지 못했습니다. 로컬 관측 기록을 사용합니다.') from None
        except Exception:
            self.close()
            raise

    def close(self):
        if self.client:
            self.client.close()
            self.client = None
