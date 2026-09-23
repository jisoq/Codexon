import time
from PySide6.QtCore import QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.core import Session
from cachemonitor.dashboard import Dashboard
from cachemonitor.quick_qa import click_row


def test_sequential_time_tables_keep_order_cost_sort_and_selected_call(tmp_path):
    app=QApplication.instance() or QApplication([]);now=time.time()
    before=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    older=Session('old','h',title='older expensive');newer=Session('new','h',title='newer cheap')
    def add(session,offset,key,turn,inputs=100):
        session.add_usage(now-offset,key,dict(input_tokens=inputs,cached_input_tokens=0,cache_write_input_tokens=0,
            output_tokens=10,reasoning_output_tokens=0),'gpt-6-astra',turn,'high',service_tier='Standard')
    add(older,90,'old-1','old-turn',100000);add(older,80,'old-2','old-turn',100000)
    add(newer,60,'new-1','first');add(newer,40,'new-2','first');add(newer,20,'new-3','second')
    def snapshot():
        views=[]
        for session in (older,newer):
            view=session.view(now);turns={r.turn for r in session.requests}
            view['turn_states']={turn:'완료' for turn in turns}
            view['turn_records']={turn:dict(started_at=min(r.ts for r in session.requests if r.turn==turn)-1,
                ended_at=max(r.ts for r in session.requests if r.turn==turn)+.1,state='완료') for turn in turns}
            views.append(view)
        return dict(ts=now,sessions=views,homes=['h'],errors=[],unassigned=[],index={'loading':False})
    window=Dashboard([],start_worker=False,live_limits=False,settings=QSettings(str(tmp_path/'order.ini'),QSettings.IniFormat))
    try:
        window.receive(snapshot());window.resize(1800,1000);window.show();window.nav.setCurrentRow(2);QTest.qWait(50)
        assert window.parent_kind=='sessions'
        assert [r['sid'] for r in window.parent_rows]==['new','old']
        click_row(window,window.parent_table,0)
        assert window.record_view=='requests' and [r['turn'] for r in window.record_rows]==['second','first']
        click_row(window,window.table,1)
        assert window.record_view=='calls' and [r['key'] for r in window.record_rows]==['new-2','new-1']
        click_row(window,window.table,1)
        assert window.selected_call=='new-1' and window.detail_scroll.isVisible()
        add(newer,1,'new-4','first');window.receive(snapshot());QTest.qWait(50)
        assert window.selected_turn=='first' and window.selected_call=='new-1'
        assert window.exact_record['key']=='new-1'
        assert [r['key'] for r in window.record_rows]==['new-4','new-2','new-1']
        assert [r['key'] for r in window.lookup['turns'][('h','new','first')]]==['new-1','new-2','new-4']
        window.close_record_detail();window.go_back();window.go_back()
        window.sort.setCurrentIndex(window.sort.findData('cost_desc'))
        assert window.parent_kind=='sessions' and [r['sid'] for r in window.parent_rows]==['old','new']
        assert not window.qml_errors
    finally:window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',before)


def test_calls_without_request_id_use_explicit_unlinked_group_and_time_order(tmp_path):
    app=QApplication.instance() or QApplication([]);now=time.time();session=Session('no-turn','h',title='No request ID')
    before=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    for offset in (30,10,20):session.add_usage(now-offset,str(offset),dict(input_tokens=100,cached_input_tokens=0,output_tokens=10),'gpt-5.5',service_tier='Standard')
    window=Dashboard([],start_worker=False,live_limits=False,settings=QSettings(str(tmp_path/'unlinked.ini'),QSettings.IniFormat))
    try:
        window.receive(dict(ts=now,sessions=[session.view(now)],homes=['h'],errors=[],unassigned=[]))
        window.show();window.nav.setCurrentRow(2);QTest.qWait(50)
        assert window.record_view=='requests' and len(window.record_rows)==1
        assert window.record_rows[0]['turn']=='__unlinked__'
        click_row(window,window.table,0)
        assert window.record_view=='calls' and [r['key'] for r in window.record_rows]==['10','20','30']
        assert not window.qml_errors
    finally:window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',before)
