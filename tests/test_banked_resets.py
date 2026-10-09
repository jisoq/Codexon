import time
import pytest
from cachemonitor.banked_resets import normalize_reset_credits,reset_credit_display
from cachemonitor.quota_live import normalize_limits,normalize_credits,AccountClient
from cachemonitor.quota import quota_display


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


def test_live_pro_policy_preserves_reported_limits_and_unknown_states():
    now=time.time()
    raw={'planType':'pro','primary':{'usedPercent':36,'windowDurationMins':10080,'resetsAt':now+600},
         'secondary':None,'credits':{'balance':'62500.125','hasCredits':True,'unlimited':False}}
    quota=normalize_limits({'rateLimits':raw},now,'account')
    assert quota['credits']['balance']=='62500.125'
    assert quota_display(quota,'five_hour',now)['text']=='∞'
    assert quota_display(quota,'five_hour',now+90)['text']=='?'
    for secondary,expected in (({},'?'),({'usedPercent':18,'windowDurationMins':300,'resetsAt':now+300},'82')):
        quota=normalize_limits({'rateLimits':dict(raw,secondary=secondary)},now,'account')
        assert quota_display(quota,'five_hour',now)['text']==expected
    quota=normalize_limits({'rateLimits':dict(raw,planType='plus')},now,'account')
    assert quota_display(quota,'five_hour',now)['text']=='?'
    assert normalize_credits({'balance':'0'})['balance']=='0'
    for invalid in ('NaN','Infinity','-1','bad',True,None):
        assert normalize_credits({'balance':invalid})['balance'] is None
    assert normalize_credits(None) is None
