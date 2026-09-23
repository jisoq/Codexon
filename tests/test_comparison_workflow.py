"""Model-driven comparison and navigation through the real Qt scene."""
import copy
import json

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from cachemonitor.dashboard import Dashboard, choose
from cachemonitor.quick_qa import control, click, click_row
from cachemonitor.theme import Theme, contrast_color
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
