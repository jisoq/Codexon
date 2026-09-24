import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.quota_chart import QuotaHistory
from cachemonitor.quick_qa import mount, render_plot, click, dispose
from cachemonitor.theme import shared_theme


def rows():
    return [dict(at=at,remaining=90-i,cycle_cost=i,cycle_value=100+i,
                 connect=i not in (0,3,6),reset_kind=None,label=f'관측 {i}')
            for i,at in enumerate((1000,1060,1120,35200,35260,35320,35500,35560,35620))]


@pytest.mark.parametrize('width,dark',[(1120,False),(520,False),(1120,True)])
def test_fixed_width_gaps_exact_original_selection_and_duration(tmp_path,width,dark):
    app=QApplication.instance() or QApplication([])
    shared_theme().configure('dark' if dark else 'light')
    chart=QuotaHistory();chart.money=True;chart.set_rows(rows())
    host=mount(chart,width,380)
    try:
        plot=render_plot(host,chart)
        assert chart.gap_width==24
        assert chart.gap_rights[0]-chart.gap_lefts[0]==24
        assert chart.gap_rights[1]-chart.gap_lefts[1]==24
        # Three observed spans of 120 seconds share the remaining width equally.
        unit=(chart.box.width()-48)/3
        assert chart.x_at(2)-chart.x_at(0)==pytest.approx(unit)
        assert chart.x_at(5)-chart.x_at(3)==pytest.approx(unit)
        assert chart.x_at(8)==pytest.approx(chart.box.right())
        for i in range(9):
            assert chart.index_at(chart.x_at(i),chart.box.center().y())==i
        gap_x=(chart.gap_lefts[0]+chart.gap_rights[0])/2
        assert chart.index_at(gap_x,chart.box.center().y()) is None
        assert '9시간 28분' in chart.tip_at(gap_x,chart.box.center().y())
        selected=[];chart.gap_selected.connect(selected.append)
        before=chart.cursor
        click(host,plot,gap_x,chart.box.center().y())
        assert selected and '9시간 28분' in selected[-1] and chart.cursor==before
        plot.showTip(gap_x,chart.box.center().y())
        assert '9시간 28분' in plot.tip
        plot.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Escape)
        assert not plot.detail
        click(host,plot,chart.x_at(4),chart.box.center().y())
        assert chart.cursor==4
        plot.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Left);QTest.keyClick(host.quick,Qt.Key_Left)
        assert chart.cursor==2
        plot.clearHover();QTest.qWait(30)
        assert host.grab().save(str(tmp_path/f'compressed-{width}-{dark}.png'))
        assert not host.qml_errors
    finally:dispose(host);shared_theme().configure('light')
