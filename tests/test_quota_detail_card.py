import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.quota_chart import QuotaHistory
from cachemonitor.quick_qa import mount, render_plot, dispose, walk
from cachemonitor.theme import shared_theme
from cachemonitor.quota_view import prepare_series, prepare_quota_view
from cachemonitor.i18n import set_language


@pytest.mark.parametrize('width,dark,language',[(520,False,'ko'),(1120,True,'ko'),(520,True,'en')])
def test_two_usd_scales_detail_pin_escape_and_gap_card(tmp_path,width,dark,language):
    app=QApplication.instance() or QApplication([])
    shared_theme().configure('dark' if dark else 'light')
    set_language(language)
    rows=[dict(at=1000+i*60,remaining=90-i,cycle_cost=i*10,cycle_value=1000+i*50,
               connect=bool(i),reset_kind=None,label=f'관측 {i}') for i in range(4)]
    rows[-1].update(at=40000,connect=False)
    chart=QuotaHistory();chart.money=True;chart.reference=1200;chart.set_rows(rows)
    host=mount(chart,width,450)
    try:
        plot=render_plot(host,chart)
        # Drive application slots without depending on the physical cursor or focus.
        from PySide6.QtCore import QObject
        plot.findChild(QObject,'plotHover').setProperty('enabled',False)
        assert chart.cost_ceiling==pytest.approx(33.6)
        assert 0<chart.value_floor<1000
        assert chart.ceiling>1200
        assert chart.ceiling-chart.value_floor<400
        # Ten dollars now span their own cost scale, not the much larger weekly value scale.
        assert chart.point(0,'cycle_cost').y()-chart.point(1,'cycle_cost').y()>chart.box.height()*.25
        x=chart.x_at(1);y=chart.box.center().y()
        plot.showTip(x,y)
        assert [item['label'] for item in plot.detail['items']]==['잔여량','누적 API 환산액','주간 동등 가치']
        assert [item['value'] for item in plot.detail['items']]==['89%','$10.00','$1,050.00']
        assert plot.detail['completed']==dict(label='완료 구간별 API 환산액',value='$10.00')
        assert not plot.detailPinned
        plot.clearHover();assert not plot.detail
        plot.activateAt(x,y);plot.clearHover();QTest.qWait(30)
        assert plot.detailPinned and plot.detail['items'][2]['value']=='$1,050.00'
        cards=[item for item in walk(host.quick.quickWindow().contentItem())
               if item.objectName()=='quotaDetailCard' and item.isVisible()]
        assert len(cards)==1
        card=cards[0];position=card.mapToScene(QPointF(0,0))
        assert card.height()<240 and card.width()<=340
        assert position.x()>=0 and position.y()>=0
        assert position.x()+card.width()<=host.quick.width()
        assert position.y()+card.height()<=host.quick.height()
        completed=next(item for item in walk(card) if item.objectName()=='quotaCompletedCost')
        assert completed.isVisible() and completed.property('text')=='$10.00'
        assert not plot.detail['note']
        assert host.grab().save(str(tmp_path/f'compact-card-{width}-{dark}-{language}.png'))
        # A late cost correction updates a pinned record without selecting another time.
        corrected=[dict(r) for r in rows];corrected[1]['cycle_cost']=12
        chart.set_rows(corrected);QTest.qWait(40);host.quick.grabFramebuffer()
        assert plot.detail['items'][1]['value']=='$12.00'
        assert plot.detail['completed']['value']=='$8.00'
        assert completed.property('text')=='$8.00'
        assert plot.detail['at']==rows[1]['at']
        plot.key(Qt.Key_End);QTest.qWait(30)
        assert plot.detail['items'][0]['value']=='87%'
        assert plot.detail['completed'] is None and not completed.isVisible()
        assert host.grab().save(str(tmp_path/f'current-card-{width}-{dark}-{language}.png'))
        plot.key(Qt.Key_Escape)
        assert not plot.detail
        gap=(chart.gap_lefts[0]+chart.gap_rights[0])/2
        plot.activateAt(gap,y);QTest.qWait(30)
        assert '수집 공백' in plot.detail['title'] and len(plot.detail['items'])==2
        assert host.grab().save(str(tmp_path/f'detail-{width}-{dark}.png'))
        assert not host.qml_errors
    finally:dispose(host);shared_theme().configure('light');set_language('ko')


def observations(points):
    return [dict(at=1000+i*10,remaining=remaining,cycle_cost=cost,cycle_value=1000,
                 connect=bool(i),reset_kind=None,label=str(i))
            for i,(remaining,cost) in enumerate(points)]


def test_completed_amount_covers_entire_plateau_and_waits_for_next_percent():
    from copy import deepcopy
    rows=observations([(100,0),(100,2),(99,5),(99,7),(98,12),(98,15)])
    original=deepcopy(rows)
    assert prepare_series(rows)['completed_costs']==[5,5,7,7,None,None]
    assert rows==original
    rows.append(dict(rows[-1],at=1060,remaining=97,cycle_cost=19))
    assert prepare_series(rows)['completed_costs']==[5,5,7,7,7,7,None]


def test_incomplete_evidence_is_not_divided_or_bridged():
    rows=observations([(95,4),(94,7),(92,10),(91,12),(90,15),(89,18)])
    rows[4]['connect']=False
    assert prepare_series(rows)['completed_costs']==[None,None,2,None,None,None]
    for changes in ({'cycle_cost':None},{'cycle_cost':float('nan')},
                    {'cycle_cost':-1},{'cycle_cost':3},
                    {'account':'other'},{'reset_kind':'arbitrary_reset'}):
        rows=observations([(100,0),(99,5),(98,12)])
        rows[2].update(changes)
        assert prepare_series(rows)['completed_costs'][1] is None
    rows=observations([(100,0),(99,5),(99,None),(98,12),(97,12)])
    assert prepare_series(rows)['completed_costs']==[5,None,None,0,None]


def test_completed_recorded_cost_survives_running_calls_and_unchanged_idle_gap():
    rows=observations([(82,180),(81,182),(81,190),(81,190),(80,194),(80,196)])
    for row in rows:row.update(value_pending=True,value_held=True)
    rows[3].update(connect=False,tracking_continuous=True)
    chart=QuotaHistory();chart.money=True;chart.set_rows(rows)
    assert chart.series['completed_costs']==[None,12,12,12,None,None]
    detail=chart.detail_for(2)
    assert detail['completed']['value']=='$12.00'
    assert [r['label'] for r in detail['items']]==['잔여량','누적 API 환산액','주간 동등 가치']
    assert detail['note']==''
    # Turning tracking off or an unexplained cost increase still breaks evidence.
    rows[3]['tracking_continuous']=False
    assert prepare_series(rows)['completed_costs'][1] is None
    rows[3].update(tracking_continuous=True,cycle_cost=191)
    assert prepare_series(rows)['completed_costs'][1] is None


def test_period_and_all_cycles_keep_the_same_completed_amounts():
    from test_quota_value_history import interval, report_for
    report=report_for([
        interval([(100,100),(130,99),(160,99),(190,98)],[(115,5),(145,2),(175,5)]),
        interval([(220,100),(250,99),(280,98)],[(235,3),(265,4)])])
    view=prepare_quota_view(report)
    assert [p['series']['completed_costs'] for p in view['periods']]==[[5,7,7,None],[3,4,None]]
    assert view['overall']['completed_costs']==[5,7,7,None,3,4,None]


def test_completed_amount_tolerates_float_roundoff_but_not_cost_corrections():
    rows=observations([(94,20),(93,26.82920079999998),
                       (93,34.1489548),(93,34.148954799999984),
                       (93,34.1489548),(92,41.42049520000001)])
    rows[4].update(connect=False,tracking_continuous=True)
    expected=41.42049520000001-26.82920079999998
    assert prepare_series(rows)['completed_costs'][1:5]==pytest.approx([expected]*4)
    for cost in (34.1489538,34.1489558):
        rows[4]['cycle_cost']=cost
        assert prepare_series(rows)['completed_costs'][1:5]==[None]*4

    rows=observations([(100,0),(99,5),(98,5-1e-14)])
    assert prepare_series(rows)['completed_costs']==[5,0,None]
    rows[-1]['cycle_cost']=5-1e-6
    assert prepare_series(rows)['completed_costs']==[5,None,None]
