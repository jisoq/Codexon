from cachemonitor.overlay_navigation import navigation_target
from test_overlay_layout_controller import layout_controller


def test_calculation_labels_open_one_line_without_changing_navigation(tmp_path):
    from dataclasses import replace
    from PySide6.QtCore import Qt, QEvent, QPoint
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay import SessionOverlay
    from cachemonitor.overlay_chrome import OverlayLinks, OverlayDetail, CalculationNote, named_item
    from cachemonitor.overlay_appearance import default_appearance
    from cachemonitor.overlay_view import palette
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([]);window=SessionOverlay()
    links=OverlayLinks(window.content_model);detail=OverlayDetail(window.content_model)
    note=window.content_model.calculation_note=CalculationNote(window.content_model)
    note.winId()  # The controller registers the native popup before its first use.
    data=summary();window.set_content(data)
    try:
        for dark,size in ((False,14),(True,21)):
            window.set_content(data,appearance=replace(default_appearance(dark),font_size=size))
            links.resize(window.panel_width(),window.panel_height());links.sync();links.show()
            QTest.qWait(40)
            item=named_item(links.quick.rootObject(),'nav-formula-cache')
            assert item is not None
            item.activate();QTest.qWait(40)
            note=window.content_model.calculation_note
            assert note.isVisible() and note.label.text()=='최근 호출 캐시 읽기 ÷ 입력 × 100'
            assert not note.label.wordWrap()
            assert note.label.fontMetrics().horizontalAdvance(note.label.text())<=note.label.width()
            rendered=note.label.grab().toImage();ink=palette(window.content_model.appearance)['ink']
            assert sum(max(abs(rendered.pixelColor(x,y).red()-ink.red()),
                           abs(rendered.pixelColor(x,y).green()-ink.green()),
                           abs(rendered.pixelColor(x,y).blue()-ink.blue()))<25
                       for y in range(rendered.height()) for x in range(rendered.width()))>50
            assert note.grab().save(str(tmp_path/f'formula-{dark}-{size}.png'))
            item.activate();assert not note.isVisible()
            links.view.showCalculation('formula-mean',20,300)
            assert note.label.text()=='세션 비용 ÷ 호출 수'
            window.set_content(summary(count=13),appearance=replace(default_appearance(dark),font_size=size))
            assert note.isVisible()  # A refresh does not interrupt the explanation.
            QApplication.sendEvent(note,QKeyEvent(QEvent.KeyPress,Qt.Key_Escape,Qt.NoModifier))
            assert not note.isVisible()
            # Screen-edge placement uses the real popup dimensions, not cursor state.
            area=links.screen().availableGeometry()
            note.present(links,'edge','최근 호출 출력 토큰 ÷ 소요 시간',area.bottomRight())
            assert area.contains(note.geometry());note.hide()
            received=[];links.view.navigationRequested.connect(received.append)
            links.view.keyboardActivate('cache')
            assert received[-1].call_id==data['latest']['id']
            assert not links.qml_errors
            links.view.navigationRequested.disconnect(received.append)
        window.set_content(data);detail.resize(240,800);detail.show();QTest.qWait(30)
        labels=window.content_model.detail_links()
        speed=next(r for r in labels if r.get('formula')=='선택 호출 출력 토큰 ÷ 소요 시간')
        item=named_item(detail.quick.rootObject(),'evidence-'+speed['id'])
        item.activate();assert window.content_model.calculation_note.label.text()==speed['formula']
        changed=dict(data,id='another');window.set_content(changed)
        assert not window.content_model.calculation_note.isVisible()
        composition=data['token_composition']
        assert [p['label'] for p in composition['output_parts']]==['추론','추론 외']
        assert [p['tokens'] for p in composition['output_parts']]==[4800,7200]
        assert composition['output_total']==12000
        assert not detail.qml_errors
    finally:
        detail.close();links.close();window.close();app.processEvents()


def test_pointer_intent_survives_call_refresh_but_not_session_change():
    from types import SimpleNamespace
    from cachemonitor.overlay_chrome import NavigationModel
    content=SimpleNamespace(data={'home':'h','id':'s','latest':{'id':'old'}})
    model=NavigationModel(content);received=[];model.navigationRequested.connect(received.append)
    old=navigation_target(content.data,'cache')
    model.link_state([dict(id='cache',target=old)])
    model.captureNavigation('cache')
    content.data['latest']={'id':'new'}
    model.link_state([dict(id='cache',target=navigation_target(content.data,'cache'))])
    model.activateNavigation()
    assert received==[old]
    model.captureNavigation('cache');content.data={'home':'h','id':'other'}
    model.activateNavigation()
    assert received==[old] and model.captured is None


def test_selected_call_outside_recent_window_is_footer_target():
    from PySide6.QtWidgets import QApplication
    from cachemonitor.overlay import SessionOverlay
    from cachemonitor.overlay_chrome import DetailModel
    from test_overlay_presentation import summary
    app=QApplication.instance() or QApplication([]);window=SessionOverlay()
    try:
        data=summary(count=30);window.set_content(data);content=window.content_model
        old=data['all_calls'][0];content.select(old['id']);model=DetailModel(content)
        assert old not in content.rows()
        assert model.targets['selected'].call_id==old['id']
        content.set_content(summary(count=31))
        assert model.targets['selected'].call_id==old['id']
        assert model.state['overlaySurface']==content.state['overlaySurface']
        model.content.changed.disconnect(model.sync)
    finally:window.close();app.processEvents()


def test_dashboard_navigation_validates_session_and_selects_old_event_call(layout_controller):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from cachemonitor.overlay_chrome import named_item
    from cachemonitor.overlay_tracking import Selection
    from test_overlay_presentation import summary
    c=layout_controller;data=summary(count=30);old=data['all_calls'][0]['id']
    data['cache_degradation']['events']=[dict(id='event',occurrence_keys=[old])]
    c.receive_snapshot({'overlay_sessions':[data]})
    c.receive_target({'target':{'hwnd':1},'selection':Selection(data['id'])})
    activated=[];c.native.confirm_selection=lambda window:Selection(data['id'])
    c.native.activate_target=lambda hwnd:activated.append(hwnd) or True
    target=navigation_target(data,'incident',event=data['cache_degradation']['events'][0])
    assert c.navigate_from_dashboard(target)['enabled']
    assert activated==[1] and c.widget.content_model.selected_id==old
    received=[];c.navigation_requested.connect(received.append)
    c.detail.focus_control('openDashboard');QTest.qWait(20)
    button=named_item(c.detail.quick.rootObject(),'openDashboard')
    assert button.hasActiveFocus()
    QTest.keyClick(c.detail.quick,Qt.Key_Space)
    assert received[-1].call_id==old
    c.native.confirm_selection=lambda window:Selection('other')
    assert not c.navigate_from_dashboard(target)['enabled'] and activated==[1]
