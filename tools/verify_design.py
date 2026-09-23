"""Render and exercise the Full dashboard with isolated deterministic records."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def fixture_snapshot(now=None):
    from cachemonitor.core import Session
    now=time.time() if now is None else now
    sessions=[]
    titles=('결제 내역 정산과 월별 사용량 대시보드 구현','API 응답 지연과 재시도 동작 검증',
            '긴 작업 이름이 포함된 세션의 데이터 처리와 사용자 인터페이스 검토')
    for i in range(30):
        session=Session(f'design-{i}','fixture',title=titles[i%3]+f' {i+1}')
        session.cwd=rf'V:\projects\usage-dashboard-{i%3}'
        model='gpt-6-astra' if i%2==0 else 'gpt-5.6-sol'
        for j in range(36):
            session.add_usage(now-i*3600-j*120,f'call-{i}-{j}',
                dict(input_tokens=18000+j*100,cached_input_tokens=0 if j%13==0 else 12000,
                     cache_write_input_tokens=1000,output_tokens=1200+j*20,
                     output_tokens_details={'reasoning_tokens':500}),
                model,f'turn-{i}-{j//6}',('low','medium','high','xhigh','max','ultra')[j//6],service_tier=('Standard','Fast','미확인')[(j//6+i)%3])
        view=session.view(now);view['collection_complete']=True
        view['turn_records']={};view['turn_states']={}
        for turn in {row['turn'] for row in view['history']}:
            rows=[row for row in view['history'] if row['turn']==turn]
            view['turn_records'][turn]=dict(started_at=min(row['ts'] for row in rows)-5,
                ended_at=max(row['ts'] for row in rows)+5,state='완료')
            view['turn_states'][turn]='완료'
        for j,row in enumerate(view['history']):
            response=model if j%11 else 'gpt-5.5'
            row.update(requested_model=model,response_model=response,
                model_match='일치' if response==model else '모델명 불일치',model_evidence='응답 ID 연결',
                transport='HTTP/SSE' if j%4 else 'WebSocket',transport_source='response_id',
                completion_latency_ms=850+j*35,request_to_first_token_ms=100+j*3,
                timing_valid=True,response_status='completed')
        sessions.append(view)
    return dict(ts=now,sessions=sessions,homes=['fixture'],errors=[],usage_errors=[],unassigned=[],
                usage_collection_complete=True,last_usage_collection_success=now,
                index={'loading':False,'usage_complete':True,'last_usage_success':now})


def install_fixture_quota(window,folder,snapshot):
    from cachemonitor.quota_cycles import QuotaLedger
    now=snapshot['ts']
    def quota(at,weekly,five):
        return dict(observed_at=at,source='live',account='fixture-account',plan_type='pro',bucket='codex',
            windows={'weekly':dict(used_percent=weekly,resets_at=now+604800,window_minutes=10080),
                     'five_hour':dict(used_percent=five,resets_at=now+18000,window_minutes=300)})
    ledger=QuotaLedger(Path(folder)/'quota-fixture.sqlite')
    try:
        for offset,weekly,five in ((900,24,10),(600,25,12),(300,27,15),(60,30,19)):
            ledger.observe('fixture',quota(now-offset,weekly,five))
        ledger.sync(window.engine,snapshot)
        window.quota_panel.receive(dict(quota=quota(now,30,19),report=ledger.report('fixture',now)))
    finally:ledger.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--smoke',action='store_true',help='Run packaged smoke interactions on isolated fixture records')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    from cachemonitor.fonts import configure_font_rendering,configure_high_dpi,load_bundled_fonts
    configure_font_rendering();configure_high_dpi()
    from PySide6.QtCore import QSettings,Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.quick_qa import control,table_view,walk,click_row
    from cachemonitor.theme import shared_theme
    app=QApplication([]);fonts=load_bundled_fonts();app.setProperty('cachemonitorDisableShellIntegration',True)
    report=dict(fixture=True,model_requests=0,live_quota_requests=0,captures=[],layout_errors=[],interactions=[])
    with tempfile.TemporaryDirectory(prefix='cachemonitor-design-') as folder:
        window=Dashboard([],start_worker=False,live_limits=False,manage_observer=False,
            settings=QSettings(str(Path(folder)/'settings.ini'),QSettings.IniFormat))
        snapshot=fixture_snapshot();window.receive(snapshot);install_fixture_quota(window,folder,snapshot)
        if args.smoke:
            from cachemonitor.quick_smoke import start_smoke
            window.setAttribute(Qt.WA_ShowWithoutActivating);window.show()
            start_smoke(window,app,args.output/'smoke.png',fonts)
            return app.exec()
        def settle():
            app.processEvents();QTest.qWait(80)
            assert not window.analysis_pending and not window.analysis_errors
            assert not window.qml_errors,window.qml_errors
        def capture(name):
            settle();target=args.output/f'{name}.png';assert window.grab().save(str(target));report['captures'].append(str(target))
        def check_layout(name):
            for item in walk(window.quick.rootObject()):
                if not item.isVisible() or item.metaObject().indexOfProperty('node')<0:continue
                node=item.property('node')
                if not node or item.metaObject().indexOfProperty('sourceComponent')<0:continue
                state=node.state
                if state['kind'] in ('button','choice','input','switch','toggle') and item.height()<34:
                    report['layout_errors'].append(dict(view=name,kind=state['kind'],text=state['text'],height=item.height()))
        try:
            assert window.nav.count()==4 and window.quota_service is None
            window.setAttribute(Qt.WA_ShowWithoutActivating);window.show()
            for theme in ('light','dark','codex'):
                window.settings.setValue('ui/theme',theme)
                shared_theme().configure(theme)
                for width,height in ((1120,760),(1440,940)):
                    window.resize(width,height);prefix=f'{theme}-{width}'
                    for page,name in enumerate(('overview','comparison','sessions','quota','settings')):
                        if page==4:window.open_settings()
                        else:window.nav.setCurrentRow(page)
                        capture(prefix+'-'+name);check_layout(prefix+'-'+name)
                        if theme!='codex':assert shared_theme().dark==(theme=='dark')
                        if page==1:
                            window.select_compare_type('mode');settle();capture(prefix+'-fast-comparison')
                            window.select_compare_type('effort');settle()
                        if page==2:
                            state=window.capture_state()
                            assert window.parent_kind=='sessions' and window.parent_table.rowCount()==30
                            click_row(window,window.parent_table,0);settle();assert window.record_view=='requests'
                            capture(prefix+'-requests')
                            request=window.record_rows[0];click_row(window,window.table,0);settle()
                            assert window.record_view=='calls' and window.table.rowCount()==request['responses']
                            capture(prefix+'-calls')
                            table=table_view(window,window.table);table.forceActiveFocus()
                            QTest.keyClick(window.quick,Qt.Key_Home);QTest.keyClick(window.quick,Qt.Key_Return);settle()
                            assert window.selected_call and window.detail_scroll.isVisible()
                            capture(prefix+'-call-detail')
                            window.detail_scroll.ensureWidgetVisible(window.detail_sections['evidence'][0])
                            capture(prefix+'-call-evidence')
                            window.restore_state(state);settle()
                            report['interactions'].append(dict(size=width,theme=theme,sequential_records=True,keyboard_detail=True))
                        elif page==3:
                            panel=window.quota_panel;panel.window.setCurrentIndex(1)
                            capture(prefix+'-quota-five-hour');panel.window.setCurrentIndex(0)
                            if panel.intervals.rowCount():panel.intervals.selectRow(0)
                            panel.mode_assumption.setChecked(True)
                            scroller=control(window,window.scrollers[3]);flick=scroller.property('contentItem')
                            flick.setProperty('contentY',max(0,flick.property('contentHeight')-flick.height()))
                            capture(prefix+'-quota-interval-detail')
                            panel.mode_assumption.setChecked(False);flick.setProperty('contentY',0)
                        elif page==4:
                            for category in range(6):
                                window.settings_page.navigation.setCurrentRow(category)
                                capture(prefix+f'-settings-{category}');check_layout(prefix+f'-settings-{category}')
                    window.show_prices();QTest.qWait(80)
                    dialog=window.price_dialog
                    target=args.output/f'{prefix}-prices.png';assert dialog.host.grab().save(str(target));report['captures'].append(str(target))
                    assert not dialog.host.qml_errors,dialog.host.qml_errors
                    dialog.reject();QTest.qWait(30)
            assert not report['layout_errors'],report['layout_errors']
        finally:
            report['qml_errors']=list(window.qml_errors)
            (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            window.quit_app();app.processEvents()
    print(json.dumps(dict(captures=len(report['captures']),layout_errors=report['layout_errors'],interactions=len(report['interactions']))))


if __name__=='__main__':raise SystemExit(main())
