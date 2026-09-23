"""Model-driven comparison, navigation and hover through the real Qt scene."""
import copy
import json
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, Qt, QSettings
from PySide6.QtTest import QTest
from cachemonitor.dashboard import Dashboard, choose
from cachemonitor.quick_qa import control, click, click_row, render_plot, table_view, walk
from cachemonitor.theme import shared_theme, Theme, contrast_color
from cachemonitor.pricing import usd
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


def test_all_efforts_and_fast_pairs_not_limited_to_eight(dashboard):
    w=dashboard;source,efforts=many_efforts();w.receive(source);w.nav.setCurrentRow(1)
    assert [t['effort'] for t in w.targets]==efforts
    assert len(w.compare_groups)==9 and all(g['n']==12 for g in w.compare_groups)
    assert w.result_view.currentData()=='distribution' and not w.result_view.isVisible()
    QTest.qWait(60);click(w,control(w,w.comparison_tabs['mode']))
    assert len(w.compare_groups)==18
    for effort in efforts:
        pair=[g for g in w.compare_groups if g['effort']==effort]
        assert [g['service_tier'] for g in pair]==['Standard','Fast']
        assert pair[1]['value']==pytest.approx(2*pair[0]['value'])
    choose(w.comparison_mode,'repricing');w.comparison_metric_changed()
    assert len(w.compare_groups)==18 and all(g['n']==24 for g in w.compare_groups)
    assert not w.condition_details.isVisible()
    assert not w.qml_errors


def test_missing_fast_stays_missing_and_model_survives_empty_refresh(dashboard):
    w=dashboard;source,efforts=many_efforts()
    source['sessions'][0]['history']=[r for r in source['sessions'][0]['history'] if r['service_tier']=='Standard']
    w.receive(source);w.nav.setCurrentRow(1);w.select_compare_type('mode')
    assert all(g['value'] is None and g['n']==0 for g in w.compare_groups if g['service_tier']=='Fast')
    model=w.compare_model.currentData()
    choose(w.band,'272001:inf');w.render()
    assert w.compare_model.currentData()==model and len(w.compare_groups)==18
    assert all(g['n']==0 and g['value'] is None for g in w.compare_groups)
    assert not w.qml_errors


def test_old_preferences_migrate_to_model_and_distribution(dashboard,tmp_path):
    state=dashboard.capture_state();state.pop('compare_model');state.pop('design_version')
    state['page']=1;state['targets']=[dict(id='B',model='gpt-6-astra',effort='high',service_tier='Fast')]
    state['choices'].update(compare_type='custom',result_view='difference',band='10000:50000')
    settings=QSettings(str(tmp_path/'migration.ini'),QSettings.IniFormat)
    settings.setValue('dashboard/state',json.dumps(state))
    second=Dashboard([],start_worker=False,settings=settings,static_snapshot=snapshot())
    try:
        assert second.compare_model.currentData()=='gpt-6-astra'
        assert second.compare_type.currentData()=='effort'
        assert second.result_view.currentData()=='distribution'
        assert second.band.currentData()=='10000:50000'
        assert second.targets[0]['effort']=='high'
    finally:second.quit_app()


def test_home_preserves_filters_and_back_restores_exact_call(dashboard):
    w=dashboard;w.nav.setCurrentRow(2);click_row(w,w.parent_table,1);click_row(w,w.table,1);click_row(w,w.table,1)
    before=w.capture_state();assert w.selected_call
    click(w,control(w,w.home_button))
    assert w.record_view=='requests' and w.selected_call is None and w.selected_turn is None
    assert w.period.currentData()==before['common']['period']
    click(w,control(w,w.back_button))
    assert w.selected_call==before['selected_call'] and w.selected_turn==before['selected_turn']
    assert w.detail_scroll.isVisible() and not w.qml_errors


def hover(host,item,x=None,y=None):
    # A prior Qt Quick test can leave the pointer at the same scene coordinate;
    # leave and re-enter so the hover transition is actually delivered.
    QTest.mouseMove(host.quick.quickWindow(),QPointF(1,1).toPoint());QTest.qWait(30)
    point=item.mapToScene(QPointF(item.width()/2 if x is None else x,item.height()/2 if y is None else y))
    QTest.mouseMove(host.quick.quickWindow(),point.toPoint());QTest.qWait(60)


@pytest.mark.parametrize('mode',['light','dark','codex'])
def test_hover_preserves_selection_and_highlights_whole_row_and_chart(dashboard,mode):
    w=dashboard;shared_theme().configure(mode);w.nav.setCurrentRow(1)
    button=control(w,w.comparison_tabs['effort']);hover(w,button)
    assert button.property('checked') and button.property('hovered')
    assert any(c.objectName()=='hover-feedback' and c.opacity()>0 for c in walk(button))
    plot=render_plot(w,w.comparison_chart);box,_,_=w.comparison_chart.hits[0]
    hover(w,plot,box.center().x(),box.center().y())
    assert plot.property('hoverIndex')==0
    QTest.mouseMove(w.quick.quickWindow(),control(w,w.heading).mapToScene(QPointF(2,2)).toPoint());QTest.qWait(30)
    assert plot.property('hoverIndex')==-1
    w.nav.setCurrentRow(2);QTest.qWait(80)
    table=table_view(w,w.parent_table)
    cells=[c for c in walk(table) if c.property('row')==0 and c.metaObject().indexOfProperty('display')>=0]
    assert len(cells)>1
    selected=w.parent_table.currentRow();hover(w,cells[0])
    assert control(w,w.parent_table).property('hoveredRow')==0
    assert all(any(c.objectName()=='row-hover-feedback' and c.opacity()>0 for c in walk(cell)) for cell in cells)
    assert w.parent_table.currentRow()==selected
    assert not w.qml_errors


def test_codex_low_contrast_accent_is_made_readable(monkeypatch):
    from cachemonitor import overlay_appearance
    class Reader:
        desktop=True
        def __init__(self,**kwargs):pass
        def read(self,dark):return overlay_appearance.Appearance(dark=False,surface='#f6f4ed',ink='#263344',accent='#f6f4ed',family='Pretendard JP')
    monkeypatch.setattr(overlay_appearance,'CodexAppearance',Reader)
    theme=Theme();theme.configure('codex')
    assert theme.palette['surface']=='#f6f4ed'
    assert theme.palette['accent']!='#f6f4ed'
    assert contrast_color(theme.palette['accent'],theme.palette['surface'])==theme.palette['accent']


def test_comparison_detail_home_back_and_refresh_preserve_context(dashboard):
    w=dashboard;w.nav.setCurrentRow(1);w.select_comparison(w.comparison_chart.rows[0])
    selected=w.comparison_selection.copy();assert w.home_button.isEnabled()
    w.go_home();assert not w.comparison_detail['group'].isVisible()
    w.go_back();assert w.comparison_selection==selected and w.comparison_detail['group'].isVisible()
    before=w.scrollers[1].verticalPosition.value()
    w.receive(copy.deepcopy(w.snapshot))
    assert w.comparison_selection==selected and w.scrollers[1].verticalPosition.value()==before
    assert not w.qml_errors


def test_record_tabs_keep_active_selection_when_clicked_twice(dashboard):
    w=dashboard;w.nav.setCurrentRow(2);QTest.qWait(50)
    click(w,control(w,w.record_tabs[0]));click(w,control(w,w.record_tabs[0]))
    assert w.record_tabs[0].isChecked() and not w.record_tabs[1].isChecked()
    click(w,control(w,w.record_tabs[1]))
    assert w.record_tabs[1].isChecked() and w.record_view=='calls'
    w.go_back();assert w.record_view=='requests' and w.record_tabs[0].isChecked()


def test_common_control_hover_disabled_and_calendar():
    from PySide6.QtCore import QDate,QObject
    from PySide6.QtWidgets import QApplication
    from cachemonitor.presentation import Button,Toggle,Choice,Slider,Input,DateInput,Group,Column
    from cachemonitor.quick_qa import mount,dispose
    app=QApplication.instance() or QApplication([])
    group=Group();layout=Column(group)
    button=Button('닫기');check=Toggle('체크');switch=Toggle('스위치');switch.put(kind='switch')
    choice=Choice();choice.addItem('하나','one');choice.addItem('둘','two')
    slider=Slider();field=Input();date=DateInput(QDate.currentDate())
    for node in (button,check,switch,choice,slider,field,date):layout.addWidget(node)
    layout.addStretch();host=mount(group,700,600)
    try:
        for node in (button,check,switch,slider):
            item=control(host,node);hover(host,item)
            assert item.property('hovered')
            assert any(c.objectName()=='hover-feedback' and c.opacity()>0 for c in walk(item))
        button.setEnabled(False);item=control(host,button);hover(host,item)
        assert all(c.opacity()==0 for c in walk(item) if c.objectName()=='hover-feedback')
        popup=control(host,choice);click(host,popup);QTest.qWait(40)
        assert popup.findChild(QObject,'choice-popup').property('visible')
        QTest.keyClick(host.quick,Qt.Key_Escape)
        date_item=control(host,date)
        opener=next(c for c in walk(date_item) if c.property('iconName')=='calendar')
        click(host,opener);QTest.qWait(100)
        assert not host.qml_errors,host.qml_errors
        QTest.keyClick(host.quick,Qt.Key_Escape)
    finally:dispose(host)
