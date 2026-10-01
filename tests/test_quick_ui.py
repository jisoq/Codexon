"""Regressions at the actual QML input/rendering boundary."""
import time
import pytest
from PySide6.QtCore import QSettings, QSignalBlocker, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from cachemonitor.core import Session
from cachemonitor.dashboard import Dashboard
from cachemonitor.presentation import Choice
from cachemonitor.quick_qa import control, mount, dispose, table_view, click_row, wheel, walk


def test_deferred_pages_apply_pending_state_and_keep_loaded_scroll():
    from cachemonitor.presentation import Stack, Scroll, Column, Text
    from cachemonitor.quick_qa import scene_view
    app=QApplication.instance() or QApplication([])
    pages=Stack();pages.put(deferPages=True);pages.addWidget(Text('First page'))
    body=Column();title=Text('Before visit');body.addWidget(title);body.addSpacing(1800)
    page=Scroll();page.setWidget(body);pages.addWidget(page)
    page.verticalPosition.setValue(320)
    host=mount(pages,640,480)
    try:
        with pytest.raises(AssertionError,match='Presentation node not rendered'):
            scene_view(host,page)
        title.setText('Updated before visit')
        pages.setCurrentIndex(1);QTest.qWait(80)
        item=control(host,page)
        assert any(child.property('text')=='Updated before visit' for child in walk(control(host,title)))
        assert item.property('contentItem').property('contentY')==320
        pages.setCurrentIndex(0);QTest.qWait(20)
        page.verticalPosition.setValue(520)
        pages.setCurrentIndex(1);QTest.qWait(40)
        assert control(host,page) is item
        assert item.property('contentItem').property('contentY')==520
        assert not host.qml_errors
    finally:dispose(host)


def test_independent_scenes_survive_other_scene_shutdown():
    from cachemonitor.presentation import Text
    from cachemonitor.quick_runtime import QuickHost
    app=QApplication.instance() or QApplication([])
    first=Text('first');second=Text('second')
    def scene(node):
        host=QuickHost();host.resize(240,100)
        host.set_scene(node,'Main.qml',shared=True);host.show();QTest.qWait(20)
        return host
    a=scene(first);b=scene(second)
    try:
        assert any(item.property('text')=='first' for item in walk(control(a,first)))
        assert any(item.property('text')=='second' for item in walk(control(b,second)))
        dispose(a);a=None
        second.setText('still active');QTest.qWait(20)
        assert any(item.property('text')=='still active' for item in walk(control(b,second)))
        assert not b.quick.grabFramebuffer().isNull() and not b.qml_errors
    finally:
        if a is not None:dispose(a)
        dispose(b)


def test_reasoning_tokens_render_independently_of_cost_and_request_unit(tmp_path):
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([],start_worker=False,settings=QSettings(str(tmp_path/'reasoning.ini'),QSettings.IniFormat))
    now=time.time();session=Session('reasoning','fixture',title='Reasoning comparison')
    for i,(effort,tokens) in enumerate((('low',0),('low',None),('high',4000),('high',8000))):
        usage={'input_tokens':5000,'cached_input_tokens':0,'output_tokens':9000}
        if tokens is not None:usage['reasoning_output_tokens']=tokens
        session.add_usage(now-10+i,str(i),usage,'gpt-6-astra','open',effort,service_tier='Standard')
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
        window.record_view='calls';window.render_explorer();QTest.qWait(40)
        window.extra_column_controls['response_model'].setChecked(True);QTest.qWait(40)
        response_column=window.table.model().headers.index('응답 모델')
        assert response_column==7
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
        assert window.selected_call_scope==('fixture','quick')  # global calls retain exact owning scope
        assert not window.qml_errors,window.qml_errors
    finally:
        window.quit_app();app.setProperty('cachemonitorDisableShellIntegration',previous)
