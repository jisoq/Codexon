"""Model-driven comparison and navigation through the real Qt scene."""
import copy

from cachemonitor.quick_qa import control, click, click_row
from test_ui import dashboard, snapshot


def many_efforts():
    source=snapshot()
    efforts=['none','minimal','low','medium','high','xhigh','max','ultra','미확인']
    template=source['sessions'][0]
    rows=[]
    for index,effort in enumerate(efforts):
        for mode in ('Standard','Fast'):
            for n in range(12):
                row=copy.deepcopy(template['history'][0])
                row.update(key=f'{index}-{mode}-{n}',effort=effort,service_tier=mode,turn=f'{index}-{mode}',ts=source['ts']-100-n)
                rows.append(row)
    template['history']=rows;source['sessions']=[template]
    return source,efforts


def test_home_preserves_filters_and_back_restores_exact_call(dashboard):
    w=dashboard;w.nav.setCurrentRow(2);click_row(w,w.parent_table,1);click_row(w,w.table,1);click_row(w,w.table,1)
    before=w.capture_state();assert w.selected_call
    click(w,control(w,w.home_button))
    assert w.record_view=='requests' and w.selected_call is None and w.selected_turn is None
    assert w.period.currentData()==before['common']['period']
    click(w,control(w,w.back_button))
    assert w.selected_call==before['selected_call'] and w.selected_turn==before['selected_turn']
    assert w.detail_scroll.isVisible() and not w.qml_errors


def test_comparison_detail_home_back_and_refresh_preserve_context(dashboard):
    w=dashboard;w.nav.setCurrentRow(1);w.select_comparison(w.comparison_chart.rows[0])
    selected=w.comparison_selection.copy();assert w.home_button.isEnabled()
    w.go_home();assert not w.comparison_detail['group'].isVisible()
    w.go_back();assert w.comparison_selection==selected and w.comparison_detail['group'].isVisible()
    before=w.scrollers[1].verticalPosition.value()
    w.receive(copy.deepcopy(w.snapshot))
    assert w.comparison_selection==selected and w.scrollers[1].verticalPosition.value()==before
    assert not w.qml_errors
