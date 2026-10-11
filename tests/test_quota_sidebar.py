"""Account data remains readable outside the cost-tracking page."""
from datetime import datetime
import time

import pytest
from PySide6.QtCore import QPointF, QTimeZone
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor import i18n
from cachemonitor.presentation import Column, Group
from cachemonitor.quota_sidebar import QuotaSidebar
from cachemonitor.quick_qa import click, control, dispose, mount, scroll_extent, walk
from cachemonitor.theme import shared_theme


@pytest.fixture
def sidebar():
    app=QApplication.instance() or QApplication([])
    from cachemonitor.fonts import load_bundled_fonts
    load_bundled_fonts();shared_theme().configure('light');i18n.set_language('ko')
    card=QuotaSidebar()
    yield app,card
    if card.reset_dialog:card.reset_dialog.reject();app.processEvents()
    shared_theme().configure('light');i18n.set_language('ko')


def sample(now,count=2):
    return dict(source='live',observed_at=now,plan_type='pro',unlimited_windows=['five_hour'],
        windows={'weekly':dict(used_percent=77,resets_at=now+604800)},
        credits={'balance':'62500.1250','unlimited':False},
        reset_credits={'available_count':count,'credits':[
            dict(status='available',expires_at=now+86400*(i+1),granted_at=now-3600*(i+1))
            for i in reversed(range(count))]})


def wait_until(predicate):
    deadline=time.monotonic()+3
    while not predicate() and time.monotonic()<deadline:QTest.qWait(10)
    assert predicate()


def test_sidebar_masks_stale_balances_and_distinguishes_missing_from_unlimited(sidebar,monkeypatch):
    _,card=sidebar;now=1800000000
    monkeypatch.setattr('cachemonitor.quota_sidebar.time.time',lambda:now)
    quota=sample(now)
    card.receive({'quota':quota})
    assert card.weekly.text()=='23.0%' and card.five.text()=='∞'
    assert card.credit_balance.text()=='62,500.125'
    assert card.current['weekly']['bar'].remaining==23
    quota['unlimited_windows']=[];card.refresh_status()
    assert card.five.text()=='—'
    now+=90;card.refresh_status()
    assert card.weekly.text()==card.credit_balance.text()=='—'
    assert card.current['weekly']['bar'].remaining is None
    assert not card._reset_rows and '—' in card.reset_button.text()
    quota=sample(now);quota['credits']['balance']='0';quota['reset_credits']={'available_count':0,'credits':[]}
    card.receive({'quota':quota})
    assert card.credit_balance.text()=='0' and '0개' in card.reset_button.text()
    card.receive({'quota':None})
    assert card.weekly.text()==card.credit_balance.text()=='—'


@pytest.mark.integration
@pytest.mark.essential
def test_sidebar_scrolls_all_reset_details_without_growing_and_preserves_local_time(sidebar,monkeypatch,tmp_path):
    _,card=sidebar;now=time.time();quota=sample(now,12)
    stamp=datetime.fromisoformat('2026-10-23T05:23:00+09:00').timestamp()
    for index,row in enumerate(quota['reset_credits']['credits']):
        row['expires_at']=stamp+index*86400
    zone=[QTimeZone(b'Asia/Seoul')]
    class Zone:
        @staticmethod
        def systemTimeZone():return zone[0]
    monkeypatch.setattr('cachemonitor.quota_sidebar.QTimeZone',Zone)
    i18n.set_language('en');shared_theme().configure('dark')
    card.receive({'quota':quota})
    root=Group();column=Column(root);column.setContentsMargins(16,16,16,16)
    card.setFixedWidth(168);column.addWidget(card);column.addStretch()
    host=mount(root,200,420)
    try:
        assert '12 resets' in card.reset_button.state['text']
        for node in (card.weekly,card.credit_balance):
            labels=[item for item in walk(control(host,node))
                    if item.metaObject().indexOfProperty('truncated')>=0 and item.isVisible()]
            assert labels and all(not item.property('truncated') for item in labels)
        height=control(host,card).height()
        click(host,control(host,card.reset_button))
        wait_until(lambda:card.reset_dialog is not None and card.reset_dialog.host.quick.rootObject() is not None)
        dialog=card.reset_dialog
        wait_until(lambda:scroll_extent(dialog.host,card.details_scroll)>0)
        assert len(card.reset_items)==12
        assert abs(control(host,card).height()-height)<1
        assert card.reset_items[0]['expiry'].text()=='10/23 05:23'
        assert card.details_zone.toolTip()=='Asia/Seoul'
        last=card.reset_items[-1]['card'];card.details_scroll.ensureWidgetVisible(last)
        def last_visible():
            item=control(dialog.host,last);point=item.mapToScene(QPointF(item.width()/2,item.height()/2))
            return dialog.host.quick.rect().contains(point.toPoint())
        wait_until(last_visible)
        assert all(item['granted'].text() for item in card.reset_items)
        for identifier,expected in ((b'UTC','10/22 20:23'),(b'America/New_York','10/22 16:23')):
            zone[0]=QTimeZone(identifier);card.refresh_status()
            assert card.reset_items[0]['expiry'].text()==expected
            assert card.details_zone.toolTip()==identifier.decode()
        quota['observed_at']=time.time()-90;card.refresh_status()
        assert all(not item['card'].isVisible() for item in card.reset_items)
        assert '—' in card.details_title.text()
        assert not host.qml_errors and not dialog.host.qml_errors
        assert host.grab().save(str(tmp_path/'sidebar-dark.png'))
        click(dialog.host,control(dialog.host,card.details_close))
        wait_until(lambda:card.reset_dialog is None)
    finally:dispose(host)


def test_sidebar_uses_server_count_when_only_some_reset_expirations_are_known(sidebar):
    _,card=sidebar;now=time.time();quota=sample(now,1)
    quota['reset_credits']['available_count']=4
    quota['reset_credits']['credits'].append(dict(status='redeemed',expires_at=now-100,granted_at=None))
    card.receive({'quota':quota})
    assert '4개' in card.reset_button.text()
    assert card.expiry.text().startswith('확인된 만료')
    assert '보유 4개 중 상세 1개 제공' in card.expiry.toolTip()
    assert card._reset_rows[0]['status']=='redeemed'
