"""Dashboard contract and actual Qt Quick interactions for the replacement UI."""
import copy
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.dashboard import Dashboard, choose
from cachemonitor.core import Session
from cachemonitor.analytics import project_key
from cachemonitor.overlay_navigation import NavigationTarget
from cachemonitor.quick_qa import control, click, click_row, table_view


def snapshot(now=None):
    now=now or time.time();sessions=[]
    for si in range(3):
        s=Session(f's{si}','fixture',title=f'분석 세션 {si}');s.cwd=f'V:/work/project-{si}'
        for i in range(16):
            s.add_usage(now-3600+i*60,f'r{si}-{i}',dict(input_tokens=10000+i*100,
                cached_input_tokens=0 if i in (4,5) else 8000,cache_write_input_tokens=100,
                output_tokens=100+i,reasoning_output_tokens=10),
                'gpt-6-astra',f't{i//2}','high',service_tier=None if i==15 else 'Fast' if i%2 else 'Standard')
        view=s.view(now);view.update(source='user',archived=False,collection_complete=True,
            turn_states={f't{i}':'완료' for i in range(8)},
            turn_records={f't{i}':dict(started_at=now-3610+i*120,ended_at=now-3500+i*120,state='완료') for i in range(8)})
        for row in view['history']:
            row.update(response_status='completed',completion_latency_ms=1500,generation_latency_ms=500,
                       request_observed_at=row['ts']-1.5,generation_observed_at=row['ts']-1,completed_observed_at=row['ts'])
        sessions.append(view)
    return dict(ts=now,sessions=sessions,homes=['fixture'],errors=[],unassigned=[],index={'loading':False})


@pytest.fixture
def dashboard(tmp_path):
    app=QApplication.instance() or QApplication([])
    from cachemonitor.fonts import load_bundled_fonts
    load_bundled_fonts();previous=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    settings=QSettings(str(tmp_path/'dashboard.ini'),QSettings.IniFormat);settings.setValue('ui/theme','light')
    window=Dashboard(['fixture'],start_worker=False,settings=settings,static_snapshot=snapshot())
    window.show();QTest.qWait(70)
    yield window
    window.quitting=True;window.tick.stop();window.tray.hide();window.observer_panel.stop();window.close()
    app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_new_shell_default_scope_and_unknown_mode_are_distinct(dashboard):
    w=dashboard
    assert [w.nav.itemText(i) for i in range(w.nav.count())]==['사용 현황','조건 비교','세션 기록','사용 한도']
    assert w.period.currentData()=='30d' and w.current_page==0
    assert w.metrics[0].text()!='—' and '산정 45 / 관측 48' in w.metric_notes[0].text()
    w.mode.setCurrentIndex(w.mode.findData('미확인'))
    assert len(w.analysis['responses'])==3 and all(r['cost'] is None for r in w.analysis['responses'])
    w.nav.setCurrentRow(2)
    assert w.mode.currentData()=='' and len(w.analysis['responses'])==48
    w.nav.setCurrentRow(0)
    assert w.mode.currentData()=='미확인' and len(w.analysis['responses'])==3
    assert not w.qml_errors


def test_sequential_drilldown_and_full_call_detail(dashboard):
    w=dashboard;w.nav.setCurrentRow(2);QTest.qWait(40)
    click_row(w,w.parent_table,0);assert w.record_view=='requests'
    click_row(w,w.table,0);assert w.record_view=='calls'
    click_row(w,w.table,0);assert w.selected_call and w.detail_scroll.isVisible()
    assert not w.table.isVisible()  # body < 1280: one full-width detail
    assert 'gpt-6-astra' in w.detail_sections['conditions'][1].text()
    assert '전송 관측 없음' not in w.detail_sections['conditions'][1].text()
    assert w.detail_sections['time'][0].isVisible()
    assert '1.50' in w.detail_sections['time'][1].text()
    assert '평균 출력 속도' in w.detail_sections['usage'][1].text()
    assert not w.detail_sections['evidence'][0].isVisible()
    assert '전체 입력' in w.detail_sections['usage'][1].text()
    assert w.input_composition.rows[0]['label']=='일반 입력'
    w.resize(1800,1000);QTest.qWait(50)
    wide=w.width()-248>=1280
    assert w.table.isVisible()==wide
    assert w.detail_scroll.state['width']==(520 if wide else -1)
    old=w.selected_call;w.receive(copy.deepcopy(w.snapshot));assert w.selected_call==old
    w.close_record_detail();assert w.table.isVisible()
    assert not w.qml_errors


def test_overlay_context_clears_blocking_filters_and_back_restores(dashboard):
    w=dashboard;w.nav.setCurrentRow(1);choose(w.band,'0:10000');w.render()
    before=w.capture_state()
    target=NavigationTarget('fixture','s1',view='calls',call_id='r1-3',section='pricing')
    w.navigate(target);assert w.current_page==2 and w.selected_call=='r1-3'
    assert w.exact_record['key']=='r1-3' and w.temporary_context['sid']=='s1'
    depth=len(w.back_stack);w.navigate(target);assert len(w.back_stack)==depth
    w.go_back();assert w.current_page==1 and w.compare_model.currentData()==before['compare_model']
    assert w.band.currentData()==before['choices']['band'] and w.temporary_context is None
    assert not w.qml_errors


def test_compare_populations_repricing_and_metric_switch(dashboard):
    w=dashboard;w.nav.setCurrentRow(1)
    assert len(w.targets)==1 and w.targets[0]['service_tier']=='Standard'
    QTest.qWait(60);click(w,control(w,w.comparison_tabs['mode']))
    assert len(w.targets)==2 and [t['service_tier'] for t in w.targets]==['Standard','Fast']
    choose(w.comparison_metric,'reasoning');w.comparison_metric_changed()
    assert all(g['value']==10 for g in w.compare_groups)
    w.comparison_mode.setCurrentIndex(w.comparison_mode.findData('repricing'))
    assert w.comparison_metric.currentData()=='cost' and len(w.compare_groups)==2
    assert w.compare_groups[0]['n']==w.compare_groups[1]['n']==45
    assert w.compare_groups[1]['mean']==pytest.approx(2*w.compare_groups[0]['mean'])
    assert not w.qml_errors


def test_keyboard_record_selection_and_missing_target(dashboard):
    w=dashboard;w.nav.setCurrentRow(2);QTest.qWait(40)
    table=table_view(w,w.parent_table);table.forceActiveFocus();QTest.keyClick(w.quick,Qt.Key_End)
    assert w.parent_table.currentRow()==2
    QTest.keyClick(w.quick,Qt.Key_Return);assert w.selected_session and w.record_view=='requests'
    w.navigate(NavigationTarget('fixture','missing',call_id='absent',view='calls'))
    assert '대상 기록을 찾을 수 없음' in w.record_message.text() and w.exact_record is None
    w.go_back();assert w.selected_session is not None


def test_quota_and_settings_have_independent_scope(dashboard):
    w=dashboard;w.nav.setCurrentRow(3)
    assert not w.common_filters.isVisible() and not any(n.isVisible() for n in (w.model,w.effort,w.mode))
    click(w,control(w,w.settings_button));assert w.current_page==4
    w.settings_page.navigation.setCurrentRow(0)
    toggle=w.settings_page.controls['weekly_tracking']
    assert toggle.isChecked()
    click(w,control(w,toggle))
    assert not w.settings.value('quota/trackingEnabled',True,type=bool)
    from cachemonitor.settings_page import SettingsPage
    restored=SettingsPage(w)
    assert not restored.controls['weekly_tracking'].isChecked()
    restored.deleteLater()
    assert not w.price_button.isVisible()
    assert not w.qml_errors


def test_scope_specific_assumptions_and_request_denominators(dashboard):
    w=dashboard;choose(w.overview_metric,'cache_ratio');w.render()
    w.select_aggregate(w.source_bars.rows[0])
    assert '$' in w.overview_detail['text'].text()
    assert '포함 1 / 산정 불가 0' in w.overview_detail['assumptions'].body.text()
    assert len(w.aggregate_records)==16
    w.open_summary(0)
    assert '포함 3 / 산정 불가 0' in w.overview_detail['assumptions'].body.text()
    w.open_summary(2)
    assert '유효 21 / 대상 24' in w.overview_detail['text'].text()
    assert len(w.aggregate_records)==42


def test_matrix_targets_remain_rows_and_select_exact_band(dashboard):
    w=dashboard;w.nav.setCurrentRow(1)
    assert w.matrix.rowCount()==1 and w.matrix.columnCount()==7
    assert w.matrix.model().headers[1]=='10k 미만'
    w.select_matrix(0,2)
    assert len(w.aggregate_records)==24
    assert w.comparison_detail['apply_band'].isVisible()
    assert w.aggregate_selection['input_band']==(10000,50000)
    w.apply_selected_band();assert w.band.currentData()=='10000:50000'
    w.select_compare_type('mode')
    assert [t['id'] for t in w.targets]==['high · Standard','high · Fast']
    assert w.result_view.currentData()=='distribution'


def test_preferences_restore_observed_dimensions_before_snapshot(dashboard,tmp_path):
    w=dashboard;w.nav.setCurrentRow(2);choose(w.project,'V:/work/project-1')
    choose(w.model,'gpt-6-astra');choose(w.effort,'high');choose(w.mode,'Standard');w.filter_changed()
    w.save_preferences();state=w.settings.value('dashboard/state')
    settings=QSettings(str(tmp_path/'restored.ini'),QSettings.IniFormat);settings.setValue('dashboard/state',state)
    second=Dashboard(['fixture'],start_worker=False,settings=settings,static_snapshot=snapshot())
    try:
        assert second.current_page==2 and second.mode.currentData()=='Standard'
        assert second.model.currentData()=='gpt-6-astra' and second.effort.currentData()=='high'
        assert second.project.currentData()==project_key('V:/work/project-1')
        assert second.project.currentText()=='project-1'
        assert len(second.analysis['responses'])==8
    finally:second.quitting=True;second.tick.stop();second.tray.hide();second.observer_panel.stop();second.close()


@pytest.mark.parametrize('within_session',[False,True])
def test_call_filter_is_accessible_from_normal_record_views(dashboard,within_session,tmp_path):
    w=dashboard;w.resize(1150,900);w.nav.setCurrentRow(2);QTest.qWait(30)
    if not within_session:
        w.selected_session=None;w.record_view='calls';w.render_explorer()
    scope=w.selected_session
    assert w.record_view==('requests' if within_session else 'calls')
    assert w.record_filters.isVisible()
    click(w,control(w,w.record_filters.toggle))
    checkbox=control(w,w.call_filter_controls['cache_zero'])
    assert checkbox.isVisible()
    click(w,checkbox)
    assert w.record_view=='calls' and w.record_view_choice.currentData()=='calls'
    assert w.selected_session==scope and len(w.record_rows)==(2 if within_session else 6)
    assert all(r['cached']==0 for r in w.record_rows)
    assert w.call_columns_row.isVisible()
    assert w.quick.grabFramebuffer().save(str(tmp_path/'call-filter.png'))
    click(w,control(w,w.call_filter_controls['cache_zero']))
    assert len(w.record_rows)==(16 if within_session else 48)
    assert not w.qml_errors


def test_call_detail_back_restores_same_call_list_before_request_list(dashboard):
    w=dashboard;w.resize(1150,900);w.nav.setCurrentRow(2);QTest.qWait(30)
    click_row(w,w.table,0)
    before=[r['key'] for r in w.record_rows];turn=w.selected_turn;session=w.selected_session
    click_row(w,w.table,0)
    assert w.selected_call and not w.table.isVisible()
    click(w,control(w,w.back_button))
    assert w.record_view=='calls' and w.selected_call is None
    assert w.selected_turn==turn and w.selected_session==session
    assert [r['key'] for r in w.record_rows]==before and w.table.isVisible()
    click(w,control(w,w.back_button))
    assert w.record_view=='requests' and w.selected_turn is None
    assert w.selected_session==session and not w.qml_errors


def test_back_restores_exact_call_not_later_target_detail(dashboard):
    w=dashboard
    first=NavigationTarget('fixture','s1',view='calls',call_id='r1-3',section='pricing')
    second=NavigationTarget('fixture','s2',view='calls',call_id='r2-5',section='usage')
    w.navigate(first);w.navigate(second)
    assert w.exact_record['key']=='r2-5'
    w.go_back()
    assert w.selected_call=='r1-3' and w.selected_call_scope==('fixture','s1')
    assert w.exact_record['key']=='r1-3' and w.exact_record['sid']=='s1'
    assert w.record_section=='pricing'
    assert 'r1-3' in w.detail_sections['identity'][1].text()
    assert 'r2-5' not in w.detail_sections['identity'][1].text()
    assert '읽는 중' not in w.record_message.text()


def test_navigation_back_restores_settings_page_and_category(dashboard):
    w=dashboard;w.open_settings();w.settings_page.navigation.setCurrentRow(2)
    w.navigate(NavigationTarget('fixture','s1',tab='settings',view='troubleshooting',section='collection'))
    assert w.current_page==4 and w.settings_page.navigation.currentRow()==5
    w.go_back()
    assert w.current_page==4 and w.nav.currentRow()==-1
    assert w.settings_page.navigation.currentRow()==2
    w.navigate(NavigationTarget('fixture','s1',view='calls',call_id='r1-3'))
    assert w.current_page==2
    w.go_back()
    assert w.current_page==4 and w.settings_page.navigation.currentRow()==2


def test_repeat_target_dedup_only_when_actual_destination_still_matches(dashboard):
    w=dashboard;target=NavigationTarget('fixture','s1',view='calls',call_id='r1-3',section='pricing')
    w.navigate(target);depth=len(w.back_stack)
    w.navigate(target);assert len(w.back_stack)==depth
    w.change_page(0);w.navigate(target)
    assert w.current_page==2 and w.exact_record['key']=='r1-3'
    assert len(w.back_stack)==depth+1
    w.go_back();assert w.current_page==0
    w.navigate(target);w.close_record_detail();w.navigate(target)
    assert w.selected_call=='r1-3' and w.detail_scroll.isVisible()
    assert w.record_section=='pricing'


@pytest.mark.parametrize('cache_policy',[None,'session-reuse'])
def test_event_navigation_retains_conditions_and_exact_evidence_phases(dashboard,cache_policy):
    w=dashboard;now=time.time();s=Session('incident','fixture',title='사건 근거')
    for i,cached in enumerate([900]*5+[0]*2+[900]*2):
        s.add_usage(now-20+i,str(i),dict(input_tokens=1000,cached_input_tokens=cached,
            cache_write_input_tokens=0,output_tokens=10),'gpt-6-astra','turn','high',service_tier='Standard')
    s.add_usage(now-2,'unrelated',dict(input_tokens=20,cached_input_tokens=10,output_tokens=10),
        'gpt-5.6-luna','other','low',service_tier='Standard')
    source=s.view(now)
    for row in source['history']:
        if row['turn']=='turn':row['cache_policy']=cache_policy
    w.receive(dict(ts=now,sessions=[source],homes=['fixture'],errors=[],unassigned=[]))
    w.navigate(NavigationTarget('fixture','incident'))
    assert w.record_view=='requests' and '읽는 중' not in w.record_message.text()
    w.navigate(NavigationTarget('fixture','incident',view='incident',event_id='cache:5',section='evidence'))
    assert w.selected_event=='cache:5' and w.detail_scroll.isVisible()
    phases={r['key']:r['_incident_phase'] for r in w.record_rows}
    assert phases=={**{str(i):'기준' for i in range(5)},'5':'발생','6':'발생','7':'회복','8':'회복'}
    assert sum(phase=='발생' for phase in phases.values())==2
    assert 'unrelated' not in phases
    conditions=w.detail_sections['conditions'][1].text()
    assert all(value in conditions for value in ('gpt-6-astra','high','Standard'))
    if cache_policy:assert '캐시 정책  '+cache_policy in conditions
    else:assert '캐시 정책' not in conditions
    evidence=w.detail_sections['evidence'][1].text()
    assert '기준 입력 캐시율  90.0%' in evidence
    assert '기준 캐시 읽기 평균  900' in evidence
    assert '기준 호출 · 0, 1, 2, 3, 4' in evidence
    assert '발생 호출 · 5, 6' in evidence and '회복 호출 · 7, 8' in evidence
    assert '읽는 중' not in w.record_message.text()
    assert not w.input_composition.isVisible() and not w.output_composition.isVisible()
    assert not w.detail_sections['usage'][0].isVisible() and not w.detail_sections['pricing'][0].isVisible()
    visible='\n'.join(text.text() for body,text in w.detail_sections.values() if body.isVisible())
    assert not any(word in visible for word in ('미확인','확인 불가','관측 없음','해당 없음'))


def detail_heading_in_view(window,section):
    body=window.detail_sections[section][0]
    heading=control(window,body.nodes[0].nodes[0])
    viewport=control(window,window.detail_scroll).property('contentItem')
    point=heading.mapToItem(viewport,QPointF(0,0))
    return heading.isVisible() and 0<=point.y() and point.y()+heading.height()<=viewport.height()


def record_model_conflicts(window,*call_ids):
    """Supply an actual recorded problem for tests that navigate to evidence."""
    value=copy.deepcopy(window.snapshot)
    changed=set()
    for session in value['sessions']:
        for row in session['history']:
            if row['key'] in call_ids:
                row.update(model_conflict=True)
                changed.add(row['key'])
    assert changed==set(call_ids)
    window.receive(value)


def wait_for_rendered(predicate,timeout=1.5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        QTest.qWait(10)
        if predicate():return
    assert predicate()


@pytest.mark.parametrize('width',[1120,1440])
@pytest.mark.parametrize('section',['evidence','pricing'])
def test_first_detail_navigation_reveals_actual_heading_and_keeps_close_visible(dashboard,tmp_path,width,section):
    w=dashboard;w.resize(width,760)
    if section=='evidence':record_model_conflicts(w,'r1-3')
    w.navigate(NavigationTarget('fixture','s1',view='calls',request_id='t1',call_id='r1-3',section=section))
    wait_for_rendered(lambda:detail_heading_in_view(w,section))
    if section=='evidence':assert '요청 모델 기록 충돌' in w.detail_sections['evidence'][1].text()
    close=control(w,w.close_record_button)
    position=close.mapToScene(QPointF(0,0))
    assert close.isVisible() and position.x()>=0 and position.y()>=0
    assert position.x()+close.width()<=w.quick.width() and position.y()+close.height()<=w.quick.height()
    assert w.grab().save(str(tmp_path/f'detail-{section}-{width}.png'))
    click(w,close)
    assert not w.detail_scroll.isVisible() and w.table.isVisible()
    assert w.selected_call is None and not w.qml_errors


@pytest.mark.parametrize('section',['time','evidence'])
def test_absent_optional_detail_reveals_recorded_conditions(dashboard,section):
    w=dashboard;w.resize(1120,760)
    if section=='time':
        source=copy.deepcopy(w.snapshot)
        for session in source['sessions']:
            for row in session['history']:row['timing_valid']=False
        w.receive(source)
    w.navigate(NavigationTarget('fixture','s1',view='calls',call_id='r1-3',section=section))
    assert not w.detail_sections[section][0].isVisible()
    assert w.detail_sections[section][1].text()==''
    wait_for_rendered(lambda:detail_heading_in_view(w,'conditions') and control(w,w.detail_scroll).property('revealTarget') is None)
    assert 'gpt-6-astra' in w.detail_sections['conditions'][1].text()
    assert control(w,w.detail_scroll).property('revealTarget') is None
    assert control(w,w.close_record_button).isVisible()
    assert not w.qml_errors


def test_pending_detail_reveal_uses_latest_target_and_yields_to_restore(dashboard):
    w=dashboard;w.resize(1120,760)
    record_model_conflicts(w,'r1-3','r2-5')
    w.navigate(NavigationTarget('fixture','s1',view='calls',call_id='r1-3',section='evidence'))
    w.navigate(NavigationTarget('fixture','s2',view='calls',call_id='r2-5',section='pricing'))
    wait_for_rendered(lambda:detail_heading_in_view(w,'pricing'))
    assert w.exact_record['key']=='r2-5' and w.selected_call_scope==('fixture','s2')
    scroll=control(w,w.detail_scroll);viewport=scroll.property('contentItem')
    for restored in (0.,123.):
        w.detail_scroll.verticalPosition.setValue(restored)
        w.detail_scroll.ensureWidgetVisible(w.detail_sections['evidence'][0])
        assert scroll.property('revealTarget') is not None
        # Even an unchanged value is an explicit restore, not a renderer writeback.
        w.detail_scroll.verticalPosition.setValue(restored)
        assert scroll.property('revealTarget') is None
        QTest.qWait(60)
        assert viewport.property('contentY')==pytest.approx(restored)
    assert not w.qml_errors


def test_pending_detail_reveal_yields_to_user_wheel_and_close(dashboard):
    from cachemonitor.quick_qa import wheel
    w=dashboard;w.resize(1120,760)
    record_model_conflicts(w,'r1-3')
    w.navigate(NavigationTarget('fixture','s1',view='calls',call_id='r1-3',section='pricing'))
    wait_for_rendered(lambda:detail_heading_in_view(w,'pricing'))
    scroll=control(w,w.detail_scroll);viewport=scroll.property('contentItem')
    before=viewport.property('contentY')
    w.detail_scroll.ensureWidgetVisible(w.detail_sections['evidence'][0])
    assert scroll.property('revealTarget') is not None
    wheel(w,w.detail_scroll,120)
    assert scroll.property('revealTarget') is None
    QTest.qWait(60)
    assert viewport.property('contentY')<before and not detail_heading_in_view(w,'evidence')
    w.detail_scroll.ensureWidgetVisible(w.detail_sections['evidence'][0])
    assert scroll.property('revealTarget') is not None
    w.close_record_detail()
    assert scroll.property('revealTarget') is None
    assert not w.detail_scroll.isVisible() and not w.qml_errors


def test_record_actions_do_not_shift_table(dashboard):
    w=dashboard;w.resize(1800,1000);w.nav.setCurrentRow(2);QTest.qWait(60)
    assert not hasattr(w,'overlay_button')
    click_row(w,w.parent_table,0);click_row(w,w.table,0);QTest.qWait(60)
    def table_y():return control(w,w.table).mapToScene(QPointF(0,0)).y()
    before=table_y()
    for _ in range(3):w.check_stale();QTest.qWait(20)
    assert table_y()==pytest.approx(before,abs=1)
    click_row(w,w.table,0);QTest.qWait(60)
    wide=w.width()-248>=1280
    assert w.table.isVisible()==wide and w.close_record_button.isVisible()
    if wide:assert table_y()==pytest.approx(before,abs=1)
    click(w,control(w,w.close_record_button));QTest.qWait(60)
    assert table_y()==pytest.approx(before,abs=1)
    w.nav.setCurrentRow(1);QTest.qWait(30)
    assert not w.close_record_button.isVisible()
    assert not w.qml_errors


@pytest.mark.parametrize('width',[1120,1800])
def test_filter_layout_and_pending_states_keep_table_stationary(dashboard,width,tmp_path):
    w=dashboard;w.resize(width,900);w.nav.setCurrentRow(2);QTest.qWait(100)
    def position():return control(w,w.table).mapToScene(QPointF(0,0)).y()
    original=position();rows=w.table.rowCount()
    controls=[control(w,n) for n in (w.period,w.project,w.source,w.model,w.effort,w.mode)]
    if w.width()>=1800:
        assert len({round(c.mapToScene(QPointF(0,0)).y()) for c in controls})==1
        assert original<330
    for node in (w.pending_label,w.record_message):
        for text in ('계산 중','대상 기록을 읽는 중',''):
            node.setText(text);node.show();QTest.qWait(25)
            assert position()==pytest.approx(original,abs=1)
        node.hide();QTest.qWait(25)
        assert position()==pytest.approx(original,abs=1)
    w.analysis_failed('test: update failure');QTest.qWait(30)
    assert w.pages.isVisible() and w.table.rowCount()==rows
    assert position()==pytest.approx(original,abs=1)
    w.render();QTest.qWait(40)
    assert position()==pytest.approx(original,abs=1)
    assert w.grab().save(str(tmp_path/f'compact-records-{width}.png'))
    assert not w.qml_errors


@pytest.mark.parametrize('page',[0,1,2])
def test_slow_calculation_status_is_delayed_and_never_moves_content(dashboard,page):
    w=dashboard;w.nav.setCurrentRow(page);QTest.qWait(50)
    before=control(w,w.pages).mapToScene(QPointF(0,0)).y()
    w.async_mode=True;w.client_cache.clear();w.render();QTest.qWait(80)
    assert w.analysis_pending and not w.pending_label.isVisible()
    QTest.qWait(250)
    assert w.pending_label.isVisible() and w.pending_label.text()=='계산 중'
    assert control(w,w.pages).mapToScene(QPointF(0,0)).y()==pytest.approx(before,abs=1)
    w.async_mode=False;w.render();QTest.qWait(40)
    assert not w.pending_label.isVisible()
    assert control(w,w.pages).mapToScene(QPointF(0,0)).y()==pytest.approx(before,abs=1)
    assert not w.qml_errors
