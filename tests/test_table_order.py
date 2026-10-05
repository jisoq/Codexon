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
        window.receive(snapshot());window.resize(1800,1000);window.show();window.nav.setCurrentRow(window.navigation_pages.index(2));QTest.qWait(50)
        window.activate_record(0)
        assert window.record_view=='sessions'
        assert [r['sid'] for r in window.record_rows]==['new','old']
        click_row(window,window.table,0)
        assert window.record_view=='requests' and [r['turn'] for r in window.record_rows]==['first','second']
        click_row(window,window.table,0)
        assert window.record_view=='calls' and [r['key'] for r in window.record_rows]==['new-1','new-2']
        click_row(window,window.table,0)
        assert window.selected_call=='new-1' and window.detail_scroll.isVisible()
        add(newer,1,'new-4','first');window.receive(snapshot());QTest.qWait(50)
        assert window.selected_turn=='first' and window.selected_call=='new-1'
        assert window.exact_record['key']=='new-1'
        assert [r['key'] for r in window.record_rows]==['new-1','new-2','new-4']
        assert [r['key'] for r in window.engine.query(window.query())['lookup']['turns'][('h','new','first')]]==['new-1','new-2','new-4']
        window.close_record_detail();window.go_back();window.go_back()
        window.sort.setCurrentIndex(window.sort.findData('cost_desc'))
        assert window.record_view=='sessions' and [r['sid'] for r in window.record_rows]==['old','new']
        assert not window.qml_errors
    finally:window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',before)


def test_restored_selection_uses_absolute_index_in_paged_records(tmp_path):
    app=QApplication.instance() or QApplication([])
    before=app.property('cachemonitorDisableShellIntegration');app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([],start_worker=False,live_limits=False,settings=QSettings(str(tmp_path/'paged.ini'),QSettings.IniFormat))
    now=time.time();session=Session('paged','fixture',title='Paged calls')
    for i in range(350):
        session.add_usage(now-400+i,str(i),dict(input_tokens=100,output_tokens=10),'gpt-6-astra')
    try:
        window.receive(dict(ts=now,sessions=[session.view(now)],homes=[],errors=[],unassigned=[],index={'loading':False}))
        window.nav.setCurrentRow(window.navigation_pages.index(2));window.selected_session=None;window.selected_turn=None;window.record_view='calls'
        window.use_history_table();window.table.first_visible=180;window.table.last_visible=190;window.render_explorer()
        assert window.record_rows.start>0
        window.table.select_row(185);state=window.capture_state();key=window.table.row_key(window.record_rows[185])
        window.table.select_row(-1);window._restore_positions=state
        window.apply_result(window.view_result,window.query())
        assert window.table.currentRow()==185
        assert window.table.row_key(window.record_rows[window.table.currentRow()])==key
    finally:window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',before)
