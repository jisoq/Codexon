"""User-facing evidence distinguishes missing source data from real failures."""
import time

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from cachemonitor.dashboard import Dashboard, choose
from cachemonitor.core import Session
from cachemonitor.ui_details import price_reason, record_issues


def sample():
    now=time.time()
    session=Session('task','home',title='Actual task name',cwd='D:/work/long/project')
    session.add_usage(now-5,'response',dict(input_tokens=1000,cached_input_tokens=800,
        cache_write_input_tokens=0,output_tokens=20,reasoning_output_tokens=5),
        'gpt-6-astra','turn','high',service_tier='Standard')
    view=session.view(now)
    view.update(project='codex:project-id',project_name='Named project')
    return dict(ts=now,sessions=[view],homes=['home'],errors=[],unassigned=[])


def dashboard(tmp_path):
    app=QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration',True)
    return Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))


def test_real_conflict_is_visible_but_missing_wire_evidence_is_not_an_error(tmp_path):
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.nav.setCurrentRow(2)
        choose(window.record_view_choice,'calls');window.record_view_changed()
        row=window.record_rows[0]
        assert record_issues(row)==[]
        conflicted=dict(row,requested_model='gpt-6-astra',response_model='gpt-5.6-sol',model_alert_confirmed=True)
        window.render_record_detail(conflicted)
        assert window.detail_sections['evidence'][0].isVisible()
        assert '요청 모델과 응답 모델이 다릅니다' in window.detail_sections['evidence'][1].text()
        assert 'gpt-5.6-sol' in window.detail_sections['conditions'][1].text()
        window.render_record_detail(row)
        assert not window.detail_sections['evidence'][0].isVisible()
    finally:window.quit_app()


def test_unpriced_call_explains_missing_input_without_claiming_collection_error(tmp_path):
    window=dashboard(tmp_path)
    try:
        window.receive(sample());window.nav.setCurrentRow(2)
        choose(window.record_view_choice,'calls');window.record_view_changed()
        row=dict(window.record_rows[0],cost=None,service_tier='미확인',price_issue='요청 모드 미확인',price_issues=['요청 모드 미확인'])
        window.render_record_detail(row)
        assert window.detail_sections['pricing'][1].text()=='환산 제외 · 요청 모드가 기록되지 않음'
        assert not window.detail_sections['evidence'][0].isVisible()
        assert '로컬 기록' in window.response_cell(row,3,Qt.ToolTipRole)
        assert window.response_cell(row,3,Qt.DisplayRole)=='—'
        assert '공식 단가' in price_reason(dict(price_issues=['Fast 장문 단가 미확인']))
        assert price_reason(dict(mode_conflict=True,price_issues=['요청 모드 미확인']))=='요청 모드 기록이 서로 다름'
        assert price_reason(dict(model_conflict=True,price_issues=['분석 모델 미확인']))=='요청 모델 기록이 서로 다름'
    finally:window.quit_app()


def test_open_aggregate_refreshes_values_and_records_without_reveal(tmp_path):
    import copy
    window=dashboard(tmp_path)
    try:
        snapshot=sample();window.receive(snapshot);window.open_summary(0)
        before=window.aggregate_selection['value']
        reveals=[];window.scrollers[0].revealRequested.connect(reveals.append)
        updated=copy.deepcopy(snapshot);updated['ts']+=1
        extra=dict(updated['sessions'][0]['history'][0],key='response-new',ts=updated['ts']-.1)
        updated['sessions'][0]['history'].append(extra)
        updated['sessions'][0]['usage_revision']='new'
        window.receive(updated)
        assert window.aggregate_selection['value']==before*2
        assert len(window.aggregate_records)==2
        assert reveals==[]
        window.select_aggregate(window.source_bars.rows[0]);reveals.clear()
        updated['ts']+=1
        updated['sessions'][0]['history'].append(dict(extra,key='response-third',ts=updated['ts']-.1))
        updated['sessions'][0]['usage_revision']='third';window.receive(updated)
        assert window.aggregate_selection['total']==before*3
        assert len(window.aggregate_records)==3 and reveals==[]
    finally:window.quit_app()
