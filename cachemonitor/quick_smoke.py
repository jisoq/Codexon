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
                from .i18n import tr
                assert tr('비용') in window.session_scope.text()
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
            from .overlay_chrome import named_item
            from .overlay_view import palette
            overlay=window.overlay;overlay.timer.stop();overlay.input_timer.stop()
            links=overlay.links;note=overlay.calculation_note;note.winId()
            links.resize(overlay.widget.panel_width(),overlay.widget.panel_height())
            links.sync();links.show();QTest.qWait(40)
            click(links,named_item(links.quick.rootObject(),'nav-formula-cache'))
            QTest.qWait(40)
            assert note.isVisible() and note.label.text()
            assert note.label.fontMetrics().horizontalAdvance(note.label.text())<=note.label.width()
            rendered=note.label.grab().toImage();ink=palette(overlay.widget.content_model.appearance)['ink']
            assert sum(max(abs(rendered.pixelColor(x,y).red()-ink.red()),
                           abs(rendered.pixelColor(x,y).green()-ink.green()),
                           abs(rendered.pixelColor(x,y).blue()-ink.blue()))<25
                       for y in range(rendered.height()) for x in range(rendered.width()))>50
            target=path.with_name(path.stem+'-calculation.png')
            assert note.grab().save(str(target));report['screens'].append(str(target))
            report['calculation_note']=dict(text=note.label.text(),visible_ink=True)
            note.hide();links.hide()
            from .i18n import language, set_language
            from PySide6.QtGui import QImage, QPainter
            from .analysis_engine import AnalysisEngine
            from .overlay_data import OverlaySummaries
            from .core import Session
            session=Session('title-check','fixture',title='사용한도 주석 표시 정리')
            session.add_usage(time.time(),'call',dict(input_tokens=1000,cached_input_tokens=800,
                output_tokens=100,reasoning_output_tokens=20),'gpt-6-astra','turn','high','Standard')
            engine=AnalysisEngine();engine.ingest([session.view(time.time())])
            original_data=overlay.widget.content_model.data
            data=OverlaySummaries().collect(engine)[0];data['title']='사용한도 주석 표시 정리'
            overlay.widget.set_content(data);model=overlay.widget.content_model
            locale=language();title_images=[]
            try:
                for selected in ('ko','en'):
                    set_language(selected)
                    frame=QImage(model.panel_width(),model.panel_height(),QImage.Format_ARGB32_Premultiplied);frame.fill(0)
                    painter=QPainter(frame);model.paint(painter);painter.end()
                    title_images.append(frame.copy(16,12,260,28))
                    if selected=='en':
                        target=path.with_name(path.stem+'-raw-title.png');assert frame.save(str(target));report['screens'].append(str(target))
                assert title_images[0]==title_images[1]
                report['session_title_preserved']=True
            finally:set_language(locale);overlay.widget.set_content(original_data)
            window.open_settings();window.settings_page.navigation.setCurrentRow(0);QTest.qWait(60)
            choices=window.settings_page.controls;choice=choices['language'];button=choices['restart']
            choice.setCurrentIndex(choice.findData(locale));QTest.qWait(20)
            assert not control(window,button).isEnabled()
            choice.setCurrentIndex(choice.findData('ko' if locale=='en' else 'en'));QTest.qWait(20)
            assert control(window,button).isEnabled()
            target=path.with_name(path.stem+'-restart.png');assert window.grab().save(str(target));report['screens'].append(str(target))
            choice.setCurrentIndex(choice.findData(locale));QTest.qWait(20)
            assert not control(window,button).isEnabled()
            report['restart_setting_state']=True
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
