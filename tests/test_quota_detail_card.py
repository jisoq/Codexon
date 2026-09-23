import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.quota_chart import QuotaHistory
from cachemonitor.quick_qa import mount, render_plot, click, dispose, walk
from cachemonitor.theme import shared_theme


@pytest.mark.parametrize('width,dark',[(520,False),(1120,True)])
def test_two_usd_scales_hover_pin_escape_and_gap_card(tmp_path,width,dark):
    app=QApplication.instance() or QApplication([])
    shared_theme().configure('dark' if dark else 'light')
    rows=[dict(at=1000+i*60,remaining=90-i,cycle_cost=i*10,cycle_value=1000+i*50,
               connect=bool(i),reset_kind=None,label=f'관측 {i}') for i in range(4)]
    rows[-1].update(at=40000,connect=False)
    chart=QuotaHistory();chart.money=True;chart.reference=1200;chart.set_rows(rows)
    host=mount(chart,width,450)
    try:
        plot=render_plot(host,chart)
        assert chart.cost_ceiling==pytest.approx(33.6)
        assert chart.ceiling==pytest.approx(1344)
        # Ten dollars now span their own cost scale, not the much larger weekly value scale.
        assert chart.point(0,'cycle_cost').y()-chart.point(1,'cycle_cost').y()>chart.box.height()*.25
        x=chart.x_at(1);y=chart.box.center().y()
        # Exercise the same slot used by QML's HoverHandler. Synthetic native
        # mouse moves are intentionally covered by the broader Quick UI tests;
        # Windows may coalesce them after a long full-suite run.
        plot.showTip(x,y);QTest.qWait(30)
        assert plot.detail['items'][2]['value']=='$10.00'
        assert plot.detail['items'][3]['value']=='+$10.00'
        assert not plot.detailPinned
        plot.clearHover();assert not plot.detail
        click(host,plot,x,y);plot.clearHover();QTest.qWait(30)
        assert plot.detailPinned and plot.detail['items'][4]['value']=='$1,050.00'
        cards=[item for item in walk(host.quick.quickWindow().contentItem())
               if item.objectName()=='quotaDetailCard' and item.isVisible()]
        assert len(cards)==1
        card=cards[0];position=card.mapToScene(QPointF(0,0))
        assert card.height()<240 and card.width()<=340
        assert position.x()>=0 and position.y()>=0
        assert position.x()+card.width()<=host.quick.width()
        assert position.y()+card.height()<=host.quick.height()
        assert not plot.detail['note']
        assert host.grab().save(str(tmp_path/f'compact-card-{width}-{dark}.png'))
        # A late cost correction updates a pinned record without selecting another time.
        corrected=[dict(r) for r in rows];corrected[1]['cycle_cost']=12
        chart.set_rows(corrected);QTest.qWait(40);host.quick.grabFramebuffer()
        assert plot.detail['items'][2]['value']=='$12.00'
        plot.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Escape)
        assert not plot.detail
        gap=(chart.gap_lefts[0]+chart.gap_rights[0])/2
        click(host,plot,gap,y);QTest.qWait(30)
        assert '수집 공백' in plot.detail['title'] and len(plot.detail['items'])==2
        assert host.grab().save(str(tmp_path/f'detail-{width}-{dark}.png'))
        assert not host.qml_errors
    finally:dispose(host);shared_theme().configure('light')
