"""Packaged-application smoke checks against rendered Qt Quick scenes."""
import json
import time
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtQuickWidgets import QQuickWidget
from .quick_qa import control, table_view


def start_smoke(window,app,path,fonts,depth='full'):
    path=Path(path).resolve();path.parent.mkdir(parents=True,exist_ok=True)
    started=time.monotonic()

    def settle():
        deadline=time.monotonic()+30
        while window.analysis_pending or window.view_result is None:
            app.processEvents();QTest.qWait(20)
            if window.analysis_errors:raise AssertionError(window.analysis_errors)
            if time.monotonic()>deadline:raise AssertionError('Analysis response timed out')
        QTest.qWait(60)

    def finish():
        if window.snapshot.get('index',{}).get('loading',False) or window.analysis_pending:
            if time.monotonic()-started<180:QTimer.singleShot(250,finish);return
        errors=[]
        report={'framework':'PySide6 / Qt Quick','depth':depth,'errors':errors,'fonts':fonts,'screens':[]}
        try:
            assert not window.snapshot.get('usage_errors',window.snapshot.get('errors',[])),window.snapshot.get('errors')
            assert not window.quota_service or not window.quota_service.live,'Smoke must disable live allowance RPC'
            if window.worker:window.worker.freeze_collection()
            settle()
            assert window.quick.status()==QQuickWidget.Status.Ready
            assert window.taskbar_quota.native is None
            assert window.nav.count()==4
            report['sessions']=len(window.snapshot['sessions'])
            report['model_requests']=0;report['live_quota_requests']=0
            for page,name in enumerate(('overview','comparison','sessions','quota','settings')):
                if page==4:window.open_settings()
                else:window.nav.setCurrentRow(page)
                settle()
                target=path.with_name(path.stem+'-'+name+'.png')
                assert window.grab().save(str(target));report['screens'].append(str(target))
                assert not window.quick.errors(),[e.toString() for e in window.quick.errors()]
            window.open_settings();settle()
            window.settings_page.navigation.setCurrentRow(0)
            tracking=window.settings_page.controls['weekly_tracking']
            from .quick_qa import click
            original=tracking.isChecked()
            click(window,control(window,tracking))
            assert window.settings.value('quota/trackingEnabled',True,type=bool) is not original
            click(window,control(window,tracking))
            assert window.settings.value('quota/trackingEnabled',True,type=bool) is original
            report['weekly_monitoring_toggle']=True
            for category in range(6 if depth=='full' else 1):
                window.settings_page.navigation.setCurrentRow(category);QTest.qWait(60)
                target=path.with_name(path.stem+f'-settings-{category}.png')
                assert window.grab().save(str(target));report['screens'].append(str(target))
            window.nav.setCurrentRow(2);settle()
            search=control(window,window.search);search.forceActiveFocus()
            QTest.keyClicks(window.quick,'__qml_no_such_session__');QTest.qWait(60)
            assert window.table.rowCount()==0
            assert window.record_message.text() in ('조건에 맞는 기록 없음','사용 기록 없음')
            QTest.keyClick(window.quick,Qt.Key_A,Qt.ControlModifier);QTest.keyClick(window.quick,Qt.Key_Backspace)
            QTest.qWait(80);assert window.search.text()==''
            report['search_keyboard']=True
            parents=[(i,row) for i,row in enumerate(window.parent_rows) if row.get('descendants')]
            if parents:
                from .quick_qa import click_row
                i,row=parents[0]
                click_row(window,window.parent_table,i);settle()
                assert window.selected_session==(row['home'],row['sid'])
                assert '세션 비용' in window.session_scope.text()
                if row['cost'] is not None:
                    assert abs(row['cost']-(row['own_cost'] or 0)-(row['child_cost'] or 0))<1e-9
                target=path.with_name(path.stem+'-session-cost.png')
                assert window.grab().save(str(target));report['screens'].append(str(target))
                report['session_rollup']=dict(sid=row['sid'],cost=row['cost'],
                    own=row['own_cost'],children=row['child_cost'],descendants=row['descendants'])
            if depth=='full':
                from .qa import interaction_probe
                report['interactions']=interaction_probe(window,app,settle)
            if window.selected_call:window.close_record_detail()
            window.nav.setCurrentRow(2);settle()
            for mode in ('weekly','five_hour'):
                window.set_quota_mode(mode)
                assert window.settings.value('tray/quotaMode')==mode
            report['tray_modes']=True
            for width,height in (((1120,760),(1440,940)) if depth=='full' else ((1120,760),)):
                window.resize(width,height);QTest.qWait(100)
                assert table_view(window,window.table).width()>150
                if window.table.rowCount():
                    table=table_view(window,window.table);table.forceActiveFocus()
                    QTest.keyClick(window.quick,Qt.Key_End)
                    assert window.table.currentRow()==window.table.rowCount()-1
                    QTest.keyClick(window.quick,Qt.Key_Home)
                    assert window.table.currentRow()==0
            report['record_keyboard']=True
            window.nav.setCurrentRow(0);settle()
            assert window.grab().save(str(path))
            if window.tray.isSystemTrayAvailable():
                window.hide_to_tray();assert not window.isVisible()
                window.show_window();QTest.qWait(40);assert window.isVisible()
                report['close_reopen']=True
            else:
                report['close_reopen']={'excluded':'현재 플랫폼에 시스템 트레이 없음'}
            assert not window.qml_errors,window.qml_errors
            report['duration_seconds']=round(time.monotonic()-started,3)
        except Exception as error:
            import traceback
            errors.append(''.join(traceback.format_exception(error)))
        path.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        window.quit_app();app.exit(1 if errors else 0)
    QTimer.singleShot(250,finish)
