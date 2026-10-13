import pytest
from PySide6.QtCore import Qt, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.quota_chart import QuotaHistory
from cachemonitor.quick_qa import mount, render_plot, dispose, walk
from cachemonitor.theme import shared_theme
from cachemonitor.quota_view import prepare_series, prepare_quota_view
from cachemonitor.i18n import set_language


def detail_value(detail,metric):
    return next(item['value'] for item in detail['items'] if item['metric']==metric)


def test_hover_and_selection_update_only_interaction_layer():
    import time
    from PySide6.QtCore import QObject
    app=QApplication.instance() or QApplication([])
    chart=QuotaHistory();chart.money=True
    chart.set_rows(observations([(100,0),(99,5),(98,12)]))
    host=mount(chart,560,760)
    try:
        plot=render_plot(host,chart)
        plot.findChild(QObject,'plotHover').setProperty('enabled',False)
        def wait_for_paint(previous):
            deadline=time.monotonic()+3
            while plot._interaction_layer.paint_count<=previous:
                assert time.monotonic()<deadline, 'Interaction layer did not repaint'
                QTest.qWait(10)
        wait_for_paint(0)
        plot.showTip(chart.x_at(0),chart.box.center().y())
        render_plot(host,chart)
        static=plot._static_paint_count;overlay=plot._interaction_layer.paint_count
        plot.showTip(chart.x_at(1),chart.box.center().y())
        wait_for_paint(overlay)
        assert plot._static_paint_count==static
        assert chart.legend_values()['cycle_cost']=='$5.00'
        chart.set_exclusion_selection(1000,1010,True)
        render_plot(host,chart)
        static=plot._static_paint_count;overlay=plot._interaction_layer.paint_count
        chart.selectionPress(chart.x_at(0),chart.box.center().y())
        chart.selectionMove(chart.x_at(2),chart.box.center().y())
        wait_for_paint(overlay)
        assert plot._static_paint_count==static
        chart.set_rows(observations([(100,0),(99,3)]))
        assert not chart.detail_at(200,200) and not chart.tip_at(200,200)
        assert chart.exclusion_selection is None and chart._selection_anchor is None
        render_plot(host,chart)
        assert chart.geometry_ready()
        assert not host.qml_errors
    finally:dispose(host)


def test_zero_usage_bar_uses_its_padded_axis_position(tmp_path):
    from cachemonitor.charts import UsageTrend
    from PySide6.QtGui import QColor
    app=QApplication.instance() or QApplication([])
    chart=UsageTrend();chart.samples=False;chart.set_rows([dict(value=0,n=0,label='zero')])
    host=mount(chart,600,300)
    try:
        plot=render_plot(host,chart)
        picture=host.quick.grabFramebuffer();ratio=picture.width()/host.quick.width()
        x=(86+chart.width()-16)/2;bottom=chart.height()-46;y=(24+bottom)/2
        origin=plot.mapToScene(QPointF(0,0));x+=origin.x();y+=origin.y();bottom+=origin.y()
        accent=QColor(shared_theme().palette['accent'])
        def marked(at):
            # At 1x, the tiny antialiased circle may have no fully covered
            # pixel. Allow only two 8-bit levels of compositor rounding;
            # still require the accent at zero's axis position, not the floor.
            return any(max(abs(a-b) for a,b in zip(picture.pixelColor(px,py).getRgb(),accent.getRgb()))<=2
                for px in range(round((x-4)*ratio),round((x+4)*ratio))
                for py in range(round((at-4)*ratio),round((at+4)*ratio)))
        assert picture.save(str(tmp_path/'zero-usage.png'))
        assert marked(y) and not marked(bottom)
    finally:dispose(host)


@pytest.mark.parametrize('width,dark,language',[(520,False,'ko'),(1120,True,'ko'),(520,True,'en')])
def test_three_matching_axes_detail_pin_escape_and_gap_card(tmp_path,width,dark,language):
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
        assert chart.completed_floor==pytest.approx(9.9)
        assert chart.completed_ceiling==pytest.approx(10.1)
        assert chart.box.width()>150
        assert list(chart.row_boxes)==['cycle_value','remaining','cycle_cost']
        for key,_,_,_ in chart.metric_definitions():
            assert chart.row_boxes[key].left()==chart.box.left()
            assert chart.row_boxes[key].right()==chart.box.right()
        assert chart._picture.save(str(tmp_path/f'axes-{width}-{dark}-{language}.png'))
        assert 0<chart.value_floor<1000
        assert chart.ceiling>1200
        assert chart.ceiling-chart.value_floor<400
        # Ten dollars now span their own cost scale, not the much larger weekly value scale.
        assert chart.point(0,'cycle_cost').y()-chart.point(1,'cycle_cost').y()>chart.row_boxes['cycle_cost'].height()*.25
        x=chart.x_at(1);y=chart.box.center().y()
        plot.showTip(x,y)
        assert [item['color'] for item in plot.detail['items']]==['accent','muted','written','output']
        assert [item['value'] for item in plot.detail['items']]==['$1,050.00','$1,200.00','89%','$10.00']
        assert not plot.detailPinned
        plot.clearHover();assert not plot.detail
        plot.activateAt(x,y);plot.clearHover();QTest.qWait(30)
        assert plot.detailPinned and detail_value(plot.detail,'cycle_value')=='$1,050.00'
        cards=[item for item in walk(host.quick.quickWindow().contentItem())
               if item.objectName()=='quotaDetailCard' and item.isVisible()]
        assert len(cards)==1
        card=cards[0];position=card.mapToScene(QPointF(0,0))
        assert card.height()<240 and card.width()<=340
        assert position.x()>=0 and position.y()>=0
        assert position.x()+card.width()<=host.quick.width()
        assert position.y()+card.height()<=host.quick.height()
        values=[item for item in walk(card) if item.objectName()=='quotaDetailValue']
        assert len(values)==4
        reference_index=next(i for i,item in enumerate(plot.detail['items']) if item['metric']=='reference')
        assert plot.detail['items'][reference_index]['subordinate']
        assert values[reference_index].property('text')=='$1,200.00'
        assert not values[reference_index].property('font').bold()
        assert values[0].property('font').bold()
        assert not plot.detail['note']
        assert host.grab().save(str(tmp_path/f'compact-card-{width}-{dark}-{language}.png'))
        # A late cost correction updates a pinned record without selecting another time.
        corrected=[dict(r) for r in rows];corrected[1]['cycle_cost']=12
        chart.set_rows(corrected);QTest.qWait(40);host.quick.grabFramebuffer()
        assert detail_value(plot.detail,'cycle_cost')=='$12.00'
        assert plot.detail['at']==rows[1]['at']
        plot.key(Qt.Key_End);QTest.qWait(30)
        assert detail_value(plot.detail,'remaining')=='87%'
        assert host.grab().save(str(tmp_path/f'current-card-{width}-{dark}-{language}.png'))
        plot.key(Qt.Key_Escape)
        assert not plot.detail
        gap=(chart.gap_lefts[0]+chart.gap_rights[0])/2
        plot.activateAt(gap,y);QTest.qWait(30)
        assert '수집 공백' in plot.detail['title'] and len(plot.detail['items'])==2
        assert host.grab().save(str(tmp_path/f'detail-{width}-{dark}.png'))
        assert not host.qml_errors
    finally:dispose(host);shared_theme().configure('light');set_language('ko')


def test_hover_reuses_detail_rows_while_values_change(tmp_path):
    """Inspecting another point must not destroy and rebuild the detail card."""
    from PySide6.QtCore import QObject
    app=QApplication.instance() or QApplication([])
    chart=QuotaHistory();chart.money=True
    chart.set_rows(observations([(100,0),(99,5),(98,12)]))
    host=mount(chart,520,760)
    try:
        plot=render_plot(host,chart)
        plot.findChild(QObject,'plotHover').setProperty('enabled',False)
        plot.showTip(chart.x_at(0),chart.box.center().y())
        app.processEvents();host.quick.grabFramebuffer()
        card=next(item for item in walk(host.quick.quickWindow().contentItem())
                  if item.objectName()=='quotaDetailCard' and item.isVisible())
        values=[item for item in walk(card) if item.objectName()=='quotaDetailValue']
        for index in (1,2,0):
            plot.showTip(chart.x_at(index),chart.box.center().y())
            app.processEvents();host.quick.grabFramebuffer()
            current=[item for item in walk(card) if item.objectName()=='quotaDetailValue']
            assert current==values
            assert [item.property('text') for item in current]==[item['value'] for item in plot.detail['items']]
        assert not host.qml_errors
    finally:dispose(host)


def observations(points):
    return [dict(at=1000+i*10,remaining=remaining,cycle_cost=cost,cycle_value=1000,
                 connect=bool(i),reset_kind=None,label=str(i))
            for i,(remaining,cost) in enumerate(points)]


def test_detail_order_matches_graph_rows_after_hiding_and_restoring_a_metric(tmp_path):
    app=QApplication.instance() or QApplication([])
    chart=QuotaHistory();chart.money=True;chart.set_rows(observations([(90,0),(89,5),(88,8)]))
    host=mount(chart,560,640)
    try:
        plot=render_plot(host,chart)
        plot.activateAt(chart.x_at(1),chart.box.center().y())
        expected=['cycle_value','remaining','cycle_cost']
        for visible,order in ((True,expected),(False,[key for key in expected if key!='cycle_cost']),(True,expected)):
            chart.set_series_visible('cycle_cost',visible)
            render_plot(host,chart)
            assert [item['metric'] for item in plot.detail['items']]==order
            assert list(chart.row_boxes)==order
        assert host.grab().save(str(tmp_path/'matching-detail-order-560.png'))
        assert not host.qml_errors
    finally:dispose(host)


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


def test_completed_recorded_cost_survives_unchanged_idle_gap():
    rows=observations([(82,180),(81,182),(81,190),(81,190),(80,194),(80,196)])
    rows[3].update(connect=False,tracking_continuous=True)
    chart=QuotaHistory();chart.money=True;chart.set_rows(rows)
    assert chart.series['completed_costs']==[None,12,12,12,None,None]
    detail=chart.detail_for(2)
    assert all(item['metric']!='completed_cost' for item in detail['items'])
    assert [r['color'] for r in detail['items']]==['accent','written','output']
    assert detail['note']==''
    # Turning tracking off or an unexplained cost increase still breaks evidence.
    rows[3]['tracking_continuous']=False
    assert prepare_series(rows)['completed_costs'][1] is None
    rows[3].update(tracking_continuous=True,cycle_cost=191)
    assert prepare_series(rows)['completed_costs'][1] is None


def test_period_and_all_cycles_keep_the_same_completed_amounts(tmp_path):
    from test_quota_value_history import interval, report_for
    report=report_for([
        interval([(100,100),(130,99),(160,99),(190,98)],[(115,5),(145,2),(175,5)]),
        interval([(220,100),(250,99),(280,98)],[(235,3),(265,4)])])
    view=prepare_quota_view(report)
    assert [p['series']['completed_costs'] for p in view['periods']]==[[3,4,None],[5,7,7,None]]
    assert view['overall']['completed_costs']==[5,7,7,None,3,4,None]
    app=QApplication.instance() or QApplication([])
    chart=QuotaHistory();chart.money=True
    chart.set_series(view['periods'][0]['series'])
    host=mount(chart,1120,450)
    try:
        for name,series,index,expected in (
                ('first',view['periods'][1]['series'],1,7),
                ('second',view['periods'][0]['series'],1,4),
                ('all',view['overall'],5,4)):
            chart.set_series(series)
            plot=render_plot(host,chart)
            plot.activateAt(chart.x_at(index),chart.box.center().y());QTest.qWait(30)
            assert series['completed_costs'][index]==expected
            assert all(item['metric']!='completed_cost' for item in plot.detail['items'])
            assert detail_value(plot.detail,'remaining').endswith('%p' if name=='all' else '%')
            assert host.grab().save(str(tmp_path/f'completed-{name}.png'))
        assert not host.qml_errors
    finally:dispose(host)


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


def test_completed_curve_keeps_peaks_and_breaks_at_unknown_values_and_resets():
    app=QApplication.instance() or QApplication([])
    rows=observations([(100-i%100,i+(700 if i>=4321 else 0)) for i in range(10000)])
    chart=QuotaHistory();chart.money=True;chart.set_rows(rows)
    assert chart.series['completed_maximum']==701
    assert 4320 in chart.series['samples'][256]
    assert len(chart.series['samples'][256])<=256*10
    assert not chart.connects(98,101,'completed_cost')
    assert chart.connects(4320,4322,'completed_cost')
    for changes in ({'reset_kind':'arbitrary_reset'},{'account':'other'},{'cycle_start':50}):
        rows=observations([(100,0),(99,5),(98,10),(100,0),(99,7)])
        rows[3].update(changes)
        rows[4].update({k:v for k,v in changes.items() if k!='reset_kind'})
        chart.set_rows(rows)
        assert chart.value_at(3,'completed_cost')==7
        assert not chart.connects(1,3,'completed_cost')


def test_dynamic_axes_fit_values_and_ignore_missing():
    from cachemonitor.charts import dynamic_bounds, comparison_axis
    for values in ([900,901],[4],[0],[None,50,51],[-12,-10],[.000001,.000002]):
        low,high=dynamic_bounds(values)
        assert low<high
        assert all(low<=v<=high for v in values if v is not None)
    assert dynamic_bounds([900,901])[0]>0
    assert dynamic_bounds([None,50,51])[0]>0
    assert dynamic_bounds([None,float('nan')])==(0,1)
    assert dynamic_bounds([0])[0]<0<dynamic_bounds([0])[1]
    assert dynamic_bounds([0,1])[0]<0
    from cachemonitor.quota_panel import remaining_axis
    assert remaining_axis([dict(remaining=0)])[0]==0
    assert remaining_axis([dict(remaining=100)])[1]>100
    assert remaining_axis([dict(remaining=None)])[0:2]==(0,1)
    assert comparison_axis([{'value':98},{'value':99}], 'cache_ratio')[0]>90


@pytest.mark.parametrize('cumulative',[False,True])
def test_quota_axes_never_pad_below_zero_and_zero_points_remain_selectable(cumulative):
    app=QApplication.instance() or QApplication([])
    rows=[dict(at=1000+i*60,remaining=i,cycle_cost=i*4,cycle_value=i*100,completed_cost=i*2,
               connect=bool(i),reset_kind=None,label=str(i)) for i in range(4)]
    series=prepare_series(rows);series['cumulative']=cumulative
    chart=QuotaHistory();chart.money=True;chart.set_series(series)
    host=mount(chart,1120,440)
    try:
        render_plot(host,chart)
        assert chart.height()==440
        for key in ('remaining','cycle_cost','cycle_value','completed_cost'):
            low,high=chart.bounds(key)
            assert low==0 and high>low
            for i in range(len(rows)):
                point=chart.point(i,key)
                if point is not None:
                    assert chart.box.contains(point)
                    assert chart.index_at(point.x(),point.y())==i
        assert not host.qml_errors
    finally:dispose(host)


def test_cumulative_and_cost_axes_do_not_force_zero():
    app=QApplication.instance() or QApplication([])
    rows=[dict(at=1000+i*60,remaining=900+i,cycle_cost=500+i,cycle_value=1000+i,
               connect=bool(i),reset_kind=None,label=str(i)) for i in range(4)]
    series=prepare_series(rows);series['cumulative']=True
    chart=QuotaHistory();chart.money=True;chart.set_series(series)
    host=mount(chart,1120,450)
    try:
        render_plot(host,chart)
        assert chart.axis[0]>800
        assert chart.bounds('cycle_cost')[0]>490
        assert chart.bounds('cycle_value')[0]>980
        for i in range(4):
            for key in ('remaining','cycle_cost','cycle_value'):
                assert chart.box.contains(chart.point(i,key))
    finally:dispose(host)


def test_live_cost_correction_refreshes_hover_without_pointer_movement():
    from PySide6.QtCore import QObject
    app=QApplication.instance() or QApplication([])
    rows=[dict(at=1000+i*60,remaining=90-i,cycle_cost=i*10,cycle_value=1000+i*50,
               connect=bool(i),reset_kind=None,label=f'Observation {i}') for i in range(4)]
    chart=QuotaHistory();chart.money=True;chart.set_rows(rows)
    host=mount(chart,1120,450)
    try:
        plot=render_plot(host,chart)
        plot.findChild(QObject,'plotHover').setProperty('enabled',False)
        plot.showTip(chart.x_at(2),chart.box.center().y())
        assert not plot.detailPinned and detail_value(plot.detail,'cycle_cost')=='$20.00'
        app.processEvents();host.quick.grabFramebuffer()
        values=[item for item in walk(host.quick.quickWindow().contentItem())
                if item.objectName()=='quotaDetailValue' and item.isVisible()]
        # Moving within one observation should reposition the existing card.
        plot.showTip(chart.x_at(2)+1,chart.box.center().y())
        app.processEvents();host.quick.grabFramebuffer()
        assert values==[item for item in walk(host.quick.quickWindow().contentItem())
                        if item.objectName()=='quotaDetailValue' and item.isVisible()]
        corrected=[dict(r) for r in rows];corrected[2]['cycle_cost']=23
        chart.set_rows(corrected);app.processEvents();host.quick.grabFramebuffer()
        assert plot.detail['at']==rows[2]['at']
        assert detail_value(plot.detail,'cycle_cost')=='$23.00'
        assert not plot.detailPinned and not host.qml_errors
    finally:dispose(host)
