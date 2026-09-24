import os
from pathlib import Path
import time

from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from cachemonitor.dashboard import Dashboard, STYLE
from cachemonitor.core import Session
from cachemonitor.quick_qa import click_row, table_view


def test_model_columns_counts_filter_and_refresh(tmp_path):
    app=QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration',True)
    app.setStyleSheet(STYLE)
    window=Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'ui.ini'),QSettings.IniFormat))
    now=time.time();session=Session('model-test',str(tmp_path),title='모델 일치성 검증 예시')
    for i in range(3):
        session.add_usage(now-5+i,f'r{i}',{'input_tokens':100,'output_tokens':10},'gpt-6-astra','one','low')
    view=session.view(now)
    view.update(turn_states={'one':'완료'},source='user',archived=False)
    view['history'][0].update(requested_model='gpt-6-astra',response_model='gpt-6-astra',model_match='일치',model_evidence='응답 ID 연결',response_status='completed')
    view['history'][1].update(requested_model='gpt-6-astra',response_model='gpt-5.6-luna',model_match='불일치',model_evidence='응답 ID 연결',response_status='completed',model_alert_confirmed=True)
    snapshot={'ts':now,'sessions':[view],'errors':[],'unassigned':[],'homes':[]}
    try:
        window.receive(snapshot);window.nav.setCurrentRow(2)
        window.resize(1800,1000);window.show();QTest.qWait(60)
        assert window.parent_kind=='sessions' and window.parent_table.rowCount()==1
        assert window.record_view=='requests' and window.table.rowCount()==1
        click_row(window,window.table,0)
        assert window.record_view=='calls' and window.table.rowCount()==3
        assert window.table.model().headers==[
            '기록 시각','요청 모델','추론 설정','요청 모드','비용','캐시 적중률']
        assert [r['key'] for r in window.record_rows]==['r2','r1','r0']
        assert [window.table.item(i,1).text() for i in range(3)]==[
            'gpt-6-astra','gpt-6-astra (불일치)','gpt-6-astra (일치)']
        assert '모델 일치 (gpt-6-astra)' in window.table.item(2,1).toolTip()
        assert window.table.item(1,2).text()=='low'
        model_tooltip=window.table.item(1,1).toolTip()
        assert '실제 요청 기록' in model_tooltip
        assert '요청 모델과 응답 모델이 다릅니다.' in model_tooltip
        assert 'gpt-' not in window.table.item(1,0).text()
        window.extra_column_controls['response_model'].setChecked(True)
        response_column=window.table.model().headers.index('응답 모델')
        assert [window.table.item(i,response_column).text() for i in range(3)]==['—','gpt-5.6-luna','gpt-6-astra']
        click_row(window,window.table,1)
        assert window.selected_call=='r1'
        conditions=window.detail_sections['conditions'][1].text()
        assert '요청 모델  gpt-6-astra' in conditions and conditions.count('gpt-6-astra')==1
        assert '응답 모델  gpt-5.6-luna' in conditions
        assert '모델 일치성  불일치' in conditions
        assert '호출 ID  r1' in window.detail_sections['identity'][1].text()
        assert window.detail_sections['evidence'][1].text()=='요청 모델과 응답 모델이 다릅니다.'
        assert window.exact_record['model_evidence']=='응답 ID 연결'
        window.call_filter_controls['model_mismatch'].setChecked(True)
        assert [r['key'] for r in window.record_rows]==['r1']
        window.receive(snapshot)
        assert window.selected_call=='r1' and window.exact_record['key']=='r1'
        window.search.setText('no such session');QTest.qWait(150)
        assert window.table.rowCount()==0 and window.selected_call=='r1'
        assert '현재 조건에서 제외' in window.record_message.text()
        window.search.clear();window.call_filter_controls['model_mismatch'].setChecked(False)
        if not window.table.isVisible():
            from cachemonitor.quick_qa import click,control
            click(window,control(window,window.close_record_button))
        click_row(window,window.table,0)
        assert '요청 모델  gpt-6-astra' in window.detail_sections['conditions'][1].text()
        assert not window.detail_sections['evidence'][0].isVisible()
        assert '모델 일치성' not in window.detail_sections['conditions'][1].text()
        assert window.table.item(0,response_column).text()=='—'
        view['history'][2].update(requested_model='gpt-6-astra',response_model='gpt-6-astra',model_match='일치',model_evidence='응답 ID 연결',response_status='completed')
        window.receive(snapshot)
        assert window.selected_call=='r2'
        assert window.exact_record['response_model']=='gpt-6-astra'
        assert window.table.item(0,response_column).text()=='gpt-6-astra'
        assert window.detail_sections['conditions'][1].text().count('gpt-6-astra')==1
        assert '모델 일치성  일치' in window.detail_sections['conditions'][1].text()
        assert window.table.item(0,1).text()=='gpt-6-astra (일치)'
        QTest.qWait(60)
        capture=os.environ.get('CACHEMONITOR_QA_CAPTURE')
        if capture:
            path=Path(capture);path.parent.mkdir(parents=True,exist_ok=True)
            assert window.grab().save(str(path))
        visible='\n'.join(text.text() for body,text in window.detail_sections.values() if body.isVisible())
        assert not any(word in visible for word in ('미확인','확인 불가','관측 없음'))
        window.close_record_detail();window.resize(2500,1000);QTest.qWait(60)
        table=table_view(window,window.table)
        assert table.width()>300 and table.property('contentWidth')>=table.width()-2
        assert not hasattr(window,'history') and not hasattr(window,'turn_table')
    finally:
        window.quitting=True;window.tick.stop();window.tray.hide();window.close()
        app.setProperty('cachemonitorDisableShellIntegration',False)
