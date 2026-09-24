import time
import pytest
from cachemonitor.banked_resets import normalize_reset_credits,reset_credit_display
from cachemonitor.quota_live import normalize_limits,AccountClient


def raw_credit(**extra):
    return dict(id='one',title='Full reset',description='Granted reset',status='available',
                resetType='codexRateLimits',grantedAt=1700000000,expiresAt=1900000000,**extra)


def test_server_count_wins_over_partial_details_and_no_redemption_rpc():
    now=time.time()
    raw={'rateLimits':{'limitId':'codex','primary':{'usedPercent':82,'windowDurationMins':10080,'resetsAt':now+600}},
         'rateLimitResetCredits':{'availableCount':3,'credits':[raw_credit()]}}
    quota=normalize_limits(raw,now,'account')
    display=reset_credit_display(quota,now)
    assert display['count']=='3개' and len(display['sections'])==1
    assert '상세 1개 제공' in display['note']
    with pytest.raises(ValueError):AccountClient('.').rpc('account/rateLimitResetCredit/consume')


def test_zero_count_details_unavailable_and_stale_are_distinct():
    now=time.time()
    def quota(value):return dict(source='live',observed_at=now,reset_credits=normalize_reset_credits(value))
    assert reset_credit_display(quota({'availableCount':0,'credits':[]}),now)['count']=='0개'
    counts=reset_credit_display(quota({'availableCount':2,'credits':None}),now)
    assert counts['count']=='2개' and counts['note']=='상세 정보 미제공' and not counts['sections']
    stale=reset_credit_display(quota({'availableCount':2,'credits':[]}),now+90)
    assert stale['count']=='—' and '마지막 확인 2개' in stale['note']
    assert reset_credit_display({'source':'local'},now)['count']=='—'
