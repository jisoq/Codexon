"""Rendered aligned lanes and reversible-exclusion range gestures."""
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.quick_qa import mount, render_plot, dispose
from cachemonitor.quota_chart import QuotaHistory
from cachemonitor.quota_view import prepare_series
from cachemonitor.theme import shared_theme


def observations():
    return [dict(at=1000+i*60,remaining=90-i,cycle_cost=i*10,cycle_value=1000+i*50,
                 connect=bool(i),reset_kind=None,label=str(i)) for i in range(8)]


def test_compact_rows_share_time_and_drag_snaps_to_original_observations(tmp_path):
    app=QApplication.instance() or QApplication([])
    chart=QuotaHistory();chart.money=True;chart.set_rows(observations())
    host=mount(chart,560,650)
    try:
        plot=render_plot(host,chart)
        boxes=list(chart.row_boxes.values())
        assert len(boxes)==3
        assert all(a.bottom()<b.top() for a,b in zip(boxes,boxes[1:]))
        for box in boxes:
            assert chart.index_at(chart.x_at(3),box.center().y())==3
        ranges=[];chart.range_selected.connect(lambda start,end:ranges.append((start,end)))
        chart.set_exclusion_selection(1000,1060,True)
        app.processEvents()
        start=plot.mapToScene(QPointF(chart.x_at(1)+2,boxes[0].center().y())).toPoint()
        end=plot.mapToScene(QPointF(chart.x_at(5)-2,boxes[2].center().y())).toPoint()
        QTest.mousePress(host.quick,Qt.LeftButton,pos=start)
        QTest.mouseMove(host.quick,end)
        assert not ranges
        QTest.mouseRelease(host.quick,Qt.LeftButton,pos=end)
        assert ranges==[(1060,1300)]
        assert chart.exclusion_selection==(1060,1300)
        assert not plot.detailPinned
        app.processEvents();host.quick.grabFramebuffer()
        assert host.grab().save(str(tmp_path/'aligned-range-560.png'))
        chart.set_exclusion_selection(enabled=False)
        assert chart.exclusion_selection is None
        plot.activateAt(chart.x_at(3),boxes[1].center().y())
        assert chart.cursor==3 and plot.detailPinned
        plot.key(Qt.Key_Right)
        assert plot.detail['at']==1240
        assert not host.qml_errors
    finally:dispose(host)


def test_exclusions_break_analysis_curves_without_erasing_real_remaining(tmp_path):
    app=QApplication.instance() or QApplication([])
    rows=observations()
    for i in (2,3):rows[i]['user_excluded']=True
    series=prepare_series(rows)
    series['exclusions']=[dict(start=1060,end=1180,reason='test',id=1)]
    chart=QuotaHistory();chart.money=True;chart.set_series(series)
    host=mount(chart,560,650)
    try:
        render_plot(host,chart)
        assert not chart.connects(1,3,'cycle_cost')
        assert chart.connects(1,3,'remaining')
        assert chart.connects(3,4,'cycle_cost')
        assert len(chart.gap_lefts)==0
        detail=chart.detail_for(2)
        assert next(item['value'] for item in detail['items'] if item['metric']=='remaining')=='88%'
        assert all(item['value']=='집계 제외' for item in detail['items'] if item['metric']!='remaining')
        picture=chart._picture;ratio=picture.devicePixelRatio()
        def curve_present(key,token):
            point=chart.point(1,key);x=(chart.x_at(1)+chart.x_at(2))/2
            expected=QColor(shared_theme().palette[token])
            return any(picture.pixelColor(px,py).alpha()>=240 and
                       max(abs(a-b) for a,b in zip(picture.pixelColor(px,py).getRgb()[:3],expected.getRgb()[:3]))<=2
                       for px in range(round(x*ratio)-2,round(x*ratio)+3)
                       for py in range(round(point.y()*ratio)-2,round(point.y()*ratio)+3))
        assert not curve_present('cycle_cost','output')
        assert curve_present('remaining','written')
        assert host.grab().save(str(tmp_path/'exclusion-lanes.png'))
        assert not host.qml_errors
    finally:dispose(host)
