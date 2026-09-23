"""Reported account quota only; never infer allowance from token usage or USD."""
import math
from datetime import datetime


def clean_limits(value):
    if not isinstance(value,dict) or value.get('limit_id') not in (None,'codex'):
        return None
    windows={}
    conflicts=[]
    for key in ('primary','secondary'):
        window=value.get(key)
        if not isinstance(window,dict): continue
        minutes=window.get('window_minutes')
        name={300:'five_hour',10080:'weekly'}.get(minutes) if type(minutes) in (int,float) else None
        used=window.get('used_percent'); resets=window.get('resets_at')
        if not name or type(used) not in (int,float) or not math.isfinite(used) or not 0<=used<=100:
            continue
        if type(resets) not in (int,float) or not math.isfinite(resets) or not 0<resets<32503680000:
            resets=None
        cleaned={'used_percent':float(used),'resets_at':resets,'window_minutes':int(minutes)}
        if name in conflicts:continue
        if name in windows and windows[name]!=cleaned:
            windows.pop(name);conflicts.append(name)
        else:windows[name]=cleaned
    plan=value.get('plan_type')
    # An absent window (including Pro) is not evidence of an unlimited allowance.
    unlimited = [name for name in ('weekly', 'five_hour')
                 if isinstance(value.get('unlimited_windows'), (list, tuple))
                 and name in value['unlimited_windows']]
    return {'plan_type':plan if plan in ('free','plus','pro','team','business','enterprise','edu') else '미확인',
            'has_five_hour':any(isinstance(value.get(k),dict) and value[k].get('window_minutes')==300 for k in ('primary','secondary')),
            'windows':windows, 'unlimited_windows':unlimited,'window_conflicts':conflicts}


def quota_display(quota,mode,now):
    label='주간' if mode=='weekly' else '5시간'
    if not quota:
        return {'text':'?','tooltip':f'{label} 미확인','remaining':None,'state':'미확인'}
    observed=quota.get('observed_at')
    checked=datetime.fromtimestamp(observed).strftime('%m/%d %H:%M') if observed else '미확인'
    source = {'live': '계정 조회', 'local': '로컬 기록'}.get(quota.get('source'))
    if source:
        checked += ' · ' + source
    window=quota.get('windows',{}).get(mode)
    reset=(window or {}).get('resets_at')
    if quota.get('reset_pending') or (reset is not None and now>=reset and (not observed or observed<reset)):
        return {'text':'?','tooltip':f'{label} 초기화 후 확인 중\n마지막 확인 {checked}',
                'remaining':None,'state':'초기화 후 확인 중'}
    max_age=quota.get('max_age',90 if quota.get('source')=='live' else 120)
    stale=(now-observed>=max_age if quota.get('source')=='live' else now-observed>max_age) if observed else True
    if stale or observed>now+1:
        return {'text':'?','tooltip':f'{label} 미확인 · 갱신 지연\n마지막 확인 {checked}',
                'remaining':None,'state':'미확인'}
    if window is None:
        if mode in quota.get('window_conflicts',[]):
            return {'text':'?','tooltip':f'{label} 관측 충돌\n마지막 확인 {checked}',
                    'remaining':None,'state':'관측 충돌'}
        if mode in quota.get('unlimited_windows',[]):
            return {'text':'∞','tooltip':f'{label} 제한 없음\n확인 {checked}',
                    'remaining':None,'state':'제한 없음'}
        return {'text':'?','tooltip':f'{label} 미확인 · 정보 없음\n마지막 확인 {checked}',
                'remaining':None,'state':'미확인'}
    used=window.get('used_percent')
    if type(used) not in (int,float) or not math.isfinite(used) or not 0<=used<=100:
        return {'text':'?','tooltip':f'{label} 미확인 · 유효하지 않은 사용률',
                'remaining':None,'state':'미확인'}
    remaining=100-window['used_percent']
    reset_text=datetime.fromtimestamp(reset).strftime('%m/%d %H:%M') if reset else '미확인'
    return {'text':str(math.floor(remaining+1e-9)),
            'tooltip':f'{label} 잔여 {remaining:g}%\n리셋 {reset_text} · 확인 {checked}',
            'remaining':remaining,'state':'확인'}


def select_current_quota(direct,local,now,baseline_after=None):
    """Direct 90s / local 120s freshness, independent of observation ordering."""
    for candidate,age in ((direct,90),(local,120)):
        if not candidate:continue
        observed=candidate.get('observed_at',0)
        elapsed=now-observed
        fresh=elapsed<age if candidate is direct else elapsed<=age
        if observed and 0<=elapsed and fresh and (baseline_after is None or observed>=baseline_after):
            return candidate
    last=max((q for q in (direct,local) if q),key=lambda q:q.get('observed_at',0),default=None)
    if baseline_after is not None and (not last or last.get('observed_at',0)<baseline_after):
        return {**(last or {}),'windows':{},'reset_pending':True}
    return last
