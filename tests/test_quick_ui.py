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
    from test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([])
    pages=Stack();pages.put(deferPages=True);pages.addWidget(Text('First page'))
    body=Column();title=Text('Before visit');body.addWidget(title);body.addSpacing(1800)
    page=Scroll();page.setWidget(body);pages.addWidget(page)
    ready=[];pages.pageReady.connect(ready.append)
    page.verticalPosition.setValue(320)
    host=mount(pages,640,480)
    try:
        with pytest.raises(AssertionError,match='Presentation node not rendered'):
            scene_view(host,page)
        assert not pages.isPageReady(1)
        title.setText('Updated before visit')
        pages.setCurrentIndex(1)
        # Switching tabs returns before constructing the unseen page.
        with pytest.raises(AssertionError,match='Presentation node not rendered'):
            scene_view(host,page)
        assert not pages.isPageReady(1)
        # A ready control remains inspectable while an unrelated page loads.
        host._page_incubation.timer.stop()
        try:
            assert host._page_incubation.incubatingObjectCount()>0
            first=control(host,pages.widget(0))
            assert first is not None and not pages.isPageReady(1)
        finally:host._page_incubation.timer.start()
        item=control(host,page)
        assert not host._page_incubation.timer.isActive()
        assert pages.isPageReady(1) and 1 in ready
        assert any(child.property('text')=='Updated before visit' for child in walk(control(host,title)))
        assert item.property('contentItem').property('contentY')==320
        pages.setCurrentIndex(0)
        first=control(host,pages.widget(0))
        wait_for_rendered(first.isVisible)
        page.verticalPosition.setValue(520)
        pages.setCurrentIndex(1)
        wait_for_rendered(lambda:item.isVisible() and item.property('contentItem').property('contentY')==520)
        assert control(host,page) is item
        assert pages.isPageReady(1)
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
        window.change_page(1);window.metric_panel.change('metric','reasoning');window.metric_panel.change('dimension','all')
        window.show()
        from test_ui import wait_for_rendered
        wait_for_rendered(lambda:len(window.metric_panel.data['rows'])==2)
        groups={g['filters']['effort']:g for g in window.metric_panel.data['rows']}
        assert groups['low']['value']==0 and groups['low']['known']==1 and groups['low']['partial']
        assert groups['high']['value']==6000 and groups['high']['known']==2
        summary=window.metric_panel.data['summary']
        assert summary['value']==4000
        assert summary['known']==3 and summary['total']==4 and summary['partial']
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
        window.receive(snapshot);window.change_page(2);window.show();QTest.qWait(100)
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
        for page in (3,1,5,2):window.change_page(page);QTest.qWait(40)
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


@pytest.mark.integration
@pytest.mark.parametrize('mode',['light','dark'])
def test_common_control_roles_keep_keyboard_selection_and_disabled_behavior(tmp_path,mode):
    from PySide6.QtCore import QObject
    from cachemonitor.presentation import Button,Column,Input,Row,Text
    from cachemonitor.quick_qa import click
    from cachemonitor.theme import shared_theme
    app=QApplication.instance() or QApplication([])
    theme=shared_theme();previous=theme.appearance;theme.configure(mode)
    body=Column();body.setContentsMargins(24,24,24,24);body.setSpacing(16)
    title=Text('공통 컨트롤 확인');title.put(fontSize=20,bold=True);body.addWidget(title)
    body.addWidget(Text('동작'))
    actions=Row();body.addLayout(actions)
    execute=Button('실행');execute.put(leadingIcon='check',activateOnReturn=True)
    disabled=Button('사용 불가');disabled.setEnabled(False)
    actions.addWidget(execute);actions.addWidget(disabled);actions.addStretch()
    body.addWidget(Text('보기'))
    tabs=Row();body.addLayout(tabs);table=Button('표');chart=Button('차트')
    def select_tab(selected):
        for node in (table,chart):node.setChecked(node is selected)
    for node in (table,chart):
        node.setCheckable(True);node.put(selectionTab=True)
        node.clicked.connect(lambda selected=node:select_tab(selected));tabs.addWidget(node)
    tabs.addStretch();table.setChecked(True)
    body.addWidget(Text('조회 조건'))
    fields=Row();body.addLayout(fields)
    choice=Choice();choice.setFixedWidth(180)
    for text,value in (('모든 실행 모드','all'),('Standard','standard'),('Fast','fast')):choice.addItem(text,value)
    search=Input();search.setPlaceholderText('세션 검색');search.put(searchIcon=True)
    fields.addWidget(choice);fields.addWidget(search,1);body.addStretch()
    activations=[];execute.clicked.connect(lambda:activations.append('run'))
    disabled.clicked.connect(lambda:activations.append('disabled'))
    host=mount(body,680,400)
    def until(condition):
        deadline=time.monotonic()+2
        while not condition() and time.monotonic()<deadline:QTest.qWait(10)
        assert condition()
    try:
        run_item,disabled_item,table_item,chart_item,choice_item,search_item=[
            control(host,node) for node in (execute,disabled,table,chart,choice,search)]
        assert all(item.isVisible() and item.width()>0 and item.height()>0
                   for item in (run_item,disabled_item,table_item,chart_item,choice_item,search_item))
        run_item.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(host.quick,Qt.Key_Return);QTest.keyClick(host.quick,Qt.Key_Space)
        assert activations==['run','run']
        execute.setEnabled(False);until(lambda:not run_item.isEnabled())
        QTest.keyClick(host.quick,Qt.Key_Return);QTest.keyClick(host.quick,Qt.Key_Space)
        click(host,disabled_item)
        assert activations==['run','run']
        execute.setEnabled(True);until(run_item.isEnabled);run_item.forceActiveFocus(Qt.TabFocusReason)
        for item in (table_item,chart_item,choice_item,search_item):
            QTest.keyClick(host.quick,Qt.Key_Tab)
            until(lambda item=item:bool(item.property('activeFocus')))
            assert item.isVisible()
        chart_item.forceActiveFocus(Qt.TabFocusReason);QTest.keyClick(host.quick,Qt.Key_Space)
        assert chart.isChecked() and not table.isChecked()
        click(host,table_item)
        assert table.isChecked() and not chart.isChecked()
        click(host,choice_item);popup=choice_item.findChild(QObject,'choice-popup')
        until(lambda:bool(popup.property('visible')))
        assert host.quick.grabFramebuffer().save(str(tmp_path/f'common-controls-{mode}-popup.png'))
        QTest.keyClick(host.quick,Qt.Key_Down);QTest.keyClick(host.quick,Qt.Key_Return)
        until(lambda:choice.currentData()=='standard' and not popup.property('visible'))
        choice.setEnabled(False);until(lambda:not choice_item.isEnabled());click(host,choice_item)
        assert not popup.property('visible') and choice.currentData()=='standard'
        choice.setEnabled(True);click(host,search_item);QTest.keyClicks(host.quick,'session-17')
        until(lambda:search.text()=='session-17')
        search.setEnabled(False);until(lambda:not search_item.isEnabled());QTest.keyClicks(host.quick,'ignored')
        assert search.text()=='session-17'
        search.setEnabled(True);until(search_item.isEnabled);search_item.forceActiveFocus(Qt.TabFocusReason)
        assert host.quick.grabFramebuffer().save(str(tmp_path/f'common-controls-{mode}.png'))
        assert not host.qml_errors,host.qml_errors
    finally:
        dispose(host);theme.configure(appearance=previous)
