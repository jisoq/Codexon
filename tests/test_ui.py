"""Dashboard contract and actual Qt Quick interactions for the replacement UI."""
import copy
import time

import pytest
from PySide6.QtCore import QSettings, QPointF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.dashboard import Dashboard, choose
from cachemonitor.core import Session
from cachemonitor.analytics import project_key
from cachemonitor.overlay_navigation import NavigationTarget
from cachemonitor.quick_qa import control, click, click_row


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


def test_sequential_drilldown_and_full_call_detail(dashboard,tmp_path):
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
    assert '입력  11,500' in w.detail_sections['usage'][1].text()
    assert [r['label'] for r in w.input_composition.rows[:3]]==['캐시 읽기','캐시 쓰기','일반 입력']
    assert [r['label'] for r in w.output_composition.rows[:2]]==['추론','추론 외']
    from cachemonitor.quick_qa import render_plot
    render_plot(w,w.output_composition)
    assert w.grab().save(str(tmp_path/'dashboard-token-hierarchy.png'))
    w.resize(1800,1000);QTest.qWait(50)
    wide=w.width()-248>=1280
    assert w.table.isVisible()==wide
    assert w.detail_scroll.state['width']==(520 if wide else -1)
    old=w.selected_call;w.receive(copy.deepcopy(w.snapshot));assert w.selected_call==old
    w.close_record_detail();assert w.table.isVisible()
    assert not w.qml_errors


@pytest.mark.parametrize('unlinked',[False,True])
@pytest.mark.parametrize('all_unpriced',[False,True])
def test_request_average_call_cost_in_both_tables_and_filtered_scope(dashboard,unlinked,all_unpriced):
    from cachemonitor.pricing import usd
    w=dashboard
    if unlinked or all_unpriced:
        value=copy.deepcopy(w.snapshot)
        for session in value['sessions']:
            for row in session['history']:
                if row['turn'] in ('t0','t7'):
                    if all_unpriced:row['output']=None
                    if unlinked:row['turn']=None
        w.receive(value)
    w.nav.setCurrentRow(2)

    def check(node,rows):
        model=node.model();column=model.headers.index('평균 호출 비용')
        assert column==model.headers.index('비용')+1
        for index,row in enumerate(rows):
            calls=[r for r in w.analysis['responses'] if (r['home'],r['sid'])==w.selected_session
                   and (not r.get('turn') if row['turn']=='__unlinked__' else r.get('turn')==row['turn'])]
            priced=[r['cost'] for r in calls if r['cost'] is not None]
            expected=sum(priced)/len(priced) if priced else None
            assert model.data(model.index(index,column))==usd(expected)

    for mode in ('','Standard'):
        choose(w.mode,mode);w.filter_changed()
        w.selected_turn=None;w.record_view='requests';w.render_explorer()
        check(w.table,w.record_rows)
        missing=next(r for r in w.record_rows if r['turn']==('__unlinked__' if unlinked else 't7'))
        assert (missing['call_mean'] is None)==all_unpriced
        if mode and not unlinked:
            assert missing['responses']==1 and missing['total_responses']==2
        w.activate_record(0)
        assert w.parent_kind=='requests'
        check(w.parent_table,w.parent_rows)
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
    assert '기준 캐시 적중률  90.0%' in evidence
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
