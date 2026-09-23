import time
import pytest
from cachemonitor.banked_resets import normalize_reset_credits,reset_credit_display
from cachemonitor.quota_live import normalize_limits,AccountClient


def raw_credit(**extra):
    return dict(id='one',title='Full reset',description='Granted reset',status='available',
                resetType='codexRateLimits',grantedAt=1700000000,expiresAt=1900000000,**extra)


@pytest.mark.parametrize('value',[None,{}, {'availableCount':True},{'availableCount':-1},{'availableCount':'2'}])
def test_missing_or_invalid_credit_count_is_not_zero(value):
    assert normalize_reset_credits(value) is None


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


def test_expiry_does_not_decrement_server_count_and_bad_dates_do_not_crash():
    now=time.time();row=raw_credit();row.update(grantedAt=float('nan'),expiresAt=now-1)
    q={'source':'live','observed_at':now,'reset_credits':normalize_reset_credits({'availableCount':1,'credits':[row]})}
    shown=reset_credit_display(q,now)
    assert shown['count']=='1개' and '만료 시각 경과' in shown['note']
    assert '지급 · 정보 미제공' in shown['sections'][0][1]


@pytest.mark.parametrize('width,dark',[(1120,False),(520,False),(1120,True)])
def test_reset_card_layout_details_refresh_and_home_isolation(tmp_path,width,dark):
    from PySide6.QtCore import QSettings,QPointF
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.fonts import load_bundled_fonts
    from cachemonitor.quota_panel import QuotaPanel
    from cachemonitor.presentation import Scroll
    from cachemonitor.quick_qa import mount,control,click,dispose
    from cachemonitor.theme import shared_theme
    app=QApplication.instance() or QApplication([]);load_bundled_fonts();shared_theme().configure('dark' if dark else 'light')
    panel=QuotaPanel(QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    now=time.time();quota=dict(source='live',observed_at=now,windows={'weekly':dict(used_percent=82,window_minutes=10080,resets_at=now+600)},
        reset_credits=normalize_reset_credits({'availableCount':1,'credits':[raw_credit()]}))
    panel.receive({'home':'h','quota':quota,'report':{'home':'h','cycles':[]}})
    scroll=Scroll();scroll.put(fillViewport=True);scroll.setWidget(panel);host=mount(scroll,width,900)
    try:
        QTest.qWait(60)
        left,right=control(host,panel.current['weekly']['card']),control(host,panel.reset_card)
        a,b=left.mapToScene(QPointF()),right.mapToScene(QPointF())
        if width>720:assert b.x()>a.x() and abs(a.y()-b.y())<2
        else:assert b.y()>a.y()
        assert panel.reset_count.text()=='1개'
        click(host,control(host,panel.reset_details.toggle));QTest.qWait(40)
        assert panel.reset_details.content.isVisible()
        assert 'Granted reset' in panel.reset_details.body.text()
        assert host.grab().save(str(tmp_path/f'banked-{width}-{dark}.png'))
        panel.receive({'home':'h','quota':{**quota,'reset_credits':{'available_count':0,'credits':[]}},'report':{'home':'h','cycles':[]}})
        assert panel.reset_count.text()=='0개' and not panel.reset_details.isVisible()
        assert panel.receive({'home':'another','quota':quota}) is False
        assert panel.reset_count.text()=='0개'
        assert not host.qml_errors
    finally:dispose(host);shared_theme().configure('light')
