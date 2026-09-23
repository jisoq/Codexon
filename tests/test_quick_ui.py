"""Regressions at the actual QML input/rendering boundary."""
import time
from PySide6.QtCore import QSettings, QSignalBlocker, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.core import Session
from cachemonitor.dashboard import Dashboard
from cachemonitor.presentation import Choice
from cachemonitor.quick_qa import control, mount, dispose, table_view, walk, click_row, wheel


def test_price_close_buttons_preserve_dashboard(tmp_path):
    from cachemonitor.quick_runtime import DialogHost
    from cachemonitor.presentation import Button
    from cachemonitor.quick_qa import click
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([],start_worker=False,settings=QSettings(str(tmp_path/'dialogs.ini'),QSettings.IniFormat))
    window.show()
    try:
        for action in (window.show_prices,)*3:
            action();QTest.qWait(40)
            host=next(item for item in app.topLevelWidgets() if isinstance(item,DialogHost) and item.isVisible())
            from cachemonitor.presentation import Text
            text='\n'.join(node.text() for node in host.dialog.findChildren(Text))
            assert 'API에는 장문 할증이 있지만, 구독 사용량 환산에는 반영하지 않습니다.' in text
            import os
            if os.environ.get('CODEXON_PRICE_CAPTURE'):
                assert host.grab().save(os.environ['CODEXON_PRICE_CAPTURE'])
            button=next(node for node in host.dialog.findChildren(Button) if node.text()=='닫기')
            click(host,control(host,button));QTest.qWait(40)
            assert window.isVisible() and window.quick is not None and not window.quitting
    finally:
        window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_reasoning_tokens_render_independently_of_cost_and_request_unit(tmp_path):
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([],start_worker=False,settings=QSettings(str(tmp_path/'reasoning.ini'),QSettings.IniFormat))
    now=time.time();session=Session('reasoning','fixture',title='Reasoning comparison')
    for i,(effort,tokens) in enumerate((('low',0),('low',None),('high',4000),('high',8000))):
        usage={'input_tokens':5000,'cached_input_tokens':0,'output_tokens':9000}
        if tokens is not None:usage['reasoning_output_tokens']=tokens
        session.add_usage(now-10+i,str(i),usage,'unpriced-model','open',effort,service_tier='Standard')
    host=None
    try:
        window.receive({'ts':now,'sessions':[session.view(now)],'homes':[],'errors':[],'unassigned':[]})
        from cachemonitor.dashboard import choose
        window.nav.setCurrentRow(1);choose(window.comparison_metric,'reasoning');window.comparison_metric_changed()
        window.show();QTest.qWait(100)
        groups={g['effort']:g for g in window.compare_groups}
        assert groups['low']['value']==0 and groups['low']['n']==1 and groups['low']['missing']==1
        assert groups['high']['value']==6000 and groups['high']['n']==2
        assert groups['high']['stats']['p90']==7600
        assert all(g['mean_budget']['cost'] is None for g in window.compare_groups)
        choose(window.unit,'turn');window.comparison_metric_changed()
        assert all(g['n']==0 for g in window.compare_groups)  # no complete request boundaries
        assert not window.qml_errors
    finally:
        if host:dispose(host)
        window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_cell_updates_batch_and_highlights_preserve_formatted_cache():
    from cachemonitor.table_model import Table, Cell
    app=QApplication.instance() or QApplication([])
    table=Table(1,2);events=[]
    table.model().dataChanged.connect(lambda *args:events.append(args))
    for column in range(2):
        cell=Cell('old');table.setItem(0,column,cell);cell.setText('new');cell.setToolTip('detail')
    app.processEvents()
    assert len(events)==1
    events.clear();table.item(0,0).setText('new');app.processEvents();assert not events
    table.model().cache[(0,0,Qt.DisplayRole)]='cached'
    table.visibleRows(0,0);table.refresh_highlights()
    assert table.model().cache[(0,0,Qt.DisplayRole)]=='cached'
    assert events[-1][2]==[Qt.UserRole+1]


def test_blocked_choice_updates_still_reach_the_qml_control():
    app=QApplication.instance() or QApplication([])
    choice=Choice();choice.addItem('one',1);host=mount(choice,250,60)
    try:
        events=[];choice.currentIndexChanged.connect(events.append)
        with QSignalBlocker(choice):
            choice.clear();choice.addItem('two',2);choice.addItem('three',3);choice.setCurrentIndex(1)
        QTest.qWait(40)
        item=control(host,choice)
        assert item.property('count')==2 and item.property('currentText')=='three'
        assert not events
        item.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Up);QTest.qWait(20)
        assert choice.currentData()==2 and events==[0]
        assert not host.qml_errors
    finally:dispose(host)


def test_external_price_link_is_rendered_and_activates_without_a_network_request():
    from PySide6.QtCore import QObject, QUrl, Slot
    from PySide6.QtGui import QDesktopServices
    from cachemonitor.presentation import Text
    from cachemonitor.quick_qa import click
    app=QApplication.instance() or QApplication([])
    class Sink(QObject):
        @Slot(QUrl)
        def receive(self,url):self.urls.append(url.toString())
    sink=Sink();sink.urls=[]
    QDesktopServices.setUrlHandler('https',sink,'receive')
    link=Text('<a href="https://example.invalid/prices">가격표</a>');link.setOpenExternalLinks(True)
    host=mount(link,300,70)
    try:
        click(host,control(host,link),35,35)
        assert sink.urls==['https://example.invalid/prices']
        assert not host.qml_errors
    finally:
        dispose(host);QDesktopServices.unsetUrlHandler('https')


def test_quick_pages_keep_scroll_and_render_model_evidence(tmp_path):
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([],start_worker=False,settings=QSettings(str(tmp_path/'quick.ini'),QSettings.IniFormat))
    now=time.time();session=Session('quick','fixture',title='QML regression')
    for i in range(250):
        session.add_usage(now-300+i,str(i),{'input_tokens':5000,'cached_input_tokens':4000,
            'cache_write_input_tokens':0,'output_tokens':100},'gpt-6-astra','turn','high',service_tier='Standard')
    source=session.view(now);source['turn_states']={'turn':'완료'}
    source['history'][-1].update(requested_model='gpt-6-astra',response_model='gpt-6-astra',model_match='일치')
    source['history'][-2].update(requested_model='gpt-6-astra',response_model='gpt-5.6-sol',model_match='불일치')
    source['history'][-3].update(requested_model='gpt-6-astra',response_model=None)
    snapshot={'ts':now,'sessions':[source],'homes':[],'errors':[],'unassigned':[]}
    try:
        window.receive(snapshot);window.nav.setCurrentRow(2);window.show();QTest.qWait(100)
        from cachemonitor.dashboard import choose
        choose(window.record_view_choice,'calls');window.record_view_changed();QTest.qWait(40)
        window.extra_column_controls['response_model'].setChecked(True);QTest.qWait(40)
        response_column=window.table.model().headers.index('응답 모델')
        assert response_column==6
        assert '호출 소요시간' not in window.table.model().headers and '관측 상태' not in window.table.model().headers
        assert window.table.item(0,response_column).text()=='gpt-6-astra'
        assert window.table.item(1,response_column).text()=='gpt-5.6-sol'
        table=table_view(window,window.table);table.setProperty('contentY',1800);QTest.qWait(40)
        before=table.property('contentY')
        wheel(window,window.table,-120);QTest.qWait(20)
        assert table.property('contentY')>before
        wheel(window,window.table,120);QTest.qWait(20)
        assert table.property('contentY')==before
        for page in (0,1,3,2):window.nav.setCurrentRow(page);QTest.qWait(40)
        assert table_view(window,window.table).property('contentY')==before
        window.receive(snapshot);QTest.qWait(40)
        assert table_view(window,window.table).property('contentY')==before
        window.table.verticalScrollBar().setValue(0);QTest.qWait(40)
        click_row(window,window.table,1);QTest.qWait(40)
        assert 'gpt-5.6-sol' in window.detail_sections['conditions'][1].text()
        assert window.detail_scroll.isVisible() and not window.table.isVisible()
        window.close_record_detail();assert window.table.isVisible()
        assert window.selected_session==('fixture','quick')  # opening a call preserves its parent scope
        assert not window.qml_errors,window.qml_errors
    finally:
        window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',previous)


def test_filter_flow_wraps_instead_of_clipping_controls():
    from cachemonitor.presentation import Row
    from cachemonitor.quick_qa import scene_view
    app=QApplication.instance() or QApplication([])
    row=Row();row.put(flow=True,spacing=10)
    choices=[]
    for title in ('모든 모델','전체 모드','모든 작업'):
        choice=Choice();choice.addItem(title);row.addWidget(choice);choices.append(choice)
    host=mount(row,420,110)
    try:
        positions=[scene_view(host,node) for node in choices]
        assert positions[2].mapToScene(QPointF()).y()>=positions[0].height()+10
        assert all(item.mapToScene(QPointF()).x()+item.width()<=420 and item.height()>=36 for item in positions)
        assert not host.qml_errors
    finally:dispose(host)
