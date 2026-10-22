"""Exact model comparison from response-linked observations, through Qt rendering."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QToolTip

from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.model_evidence import EvidenceReader, EvidenceStore
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay_chrome import OverlayChrome, OverlayDetail
from cachemonitor.overlay_view import SessionOverlay, font, fitted_model_runs


class ObservedOverlay:
    def __init__(self, tmp_path):
        self.app = QApplication.instance() or QApplication([])
        self.session = Session('model-summary', 'fixture', title='모델 비교 검증')
        self.store = EvidenceStore(tmp_path / 'wire.sqlite')
        self.reader = EvidenceReader(tmp_path / 'wire.sqlite')
        self.engine = AnalysisEngine()
        self.summaries = OverlaySummaries()
        self.widget = SessionOverlay()
        self.widget.content_model.set_detail_group('model')
        self.header = OverlayChrome('header')
        self.header.resize(268, 36)
        self.detail = None
        self.widget.content_model.put(reducedMotion=True)

    def call(self, key, configured='gpt-6-astra'):
        ts = 1789884000 + len(self.session.requests)
        self.session.add_usage(ts, key,
            dict(input_tokens=1000, cached_input_tokens=800, cache_write_input_tokens=0,
                 output_tokens=50, reasoning_output_tokens=10),
            configured, 'turn-' + key, 'high', service_tier='Standard')
        return ts

    def observe(self, key, requested='gpt-6-astra', response='gpt-6-astra', attempt=None, **changes):
        values = dict(status='completed', conflict=False, observation_missing=False)
        values.update(changes)
        self.store.write('fixture', attempt or 'attempt-' + key, 1789884000, 'WebSocket', key,
                         requested_model=requested, response_model=response, **values)

    def refresh(self, children=()):
        self.reader.poll()
        source = self.session.view(1789884100)
        source['history'] = self.reader.enrich('fixture', source['history'])
        self.engine.ingest([source,*children])
        data = next(value for value in self.summaries.collect(self.engine) if value['id']==source['id'])
        self.widget.set_content(data)
        self.header.set_title(data['title'], self.widget.content_model.context()[0])
        self.widget.show()
        self.header.show()
        QTest.qWait(50)
        assert not self.widget.qml_errors
        return data

    def close(self):
        if self.detail is not None:
            self.detail.close()
        QToolTip.hideText()
        self.header.close()
        self.widget.close()
        self.reader.close()
        self.store.close()
        self.app.processEvents()

    def reveal_comparison(self, expected):
        model = self.widget.content_model
        self.widget.set_layout(detail=True)
        assert self.widget.width() == model.panel_width()
        self.detail = OverlayDetail(model)
        self.detail.resize(240, 640)
        self.detail.show()
        QTest.qWait(50)
        item = next(item for item in model.detail_items()
                    if (''.join(item[0]) if isinstance(item[0], list) else item[0]) == expected)
        viewport = self.detail.quick.rootObject().findChild(QQuickItem, 'detailScroll')
        top = item[2] * model.appearance.scale
        viewport.setProperty('contentY', min(top, viewport.property('contentHeight') - viewport.height()))
        QTest.qWait(30)
        assert model.detailBody.scroll_offset == viewport.property('contentY')
        assert 0 <= top - viewport.property('contentY')
        assert top + item[4] * model.appearance.scale - viewport.property('contentY') <= viewport.height()
        rendered = self.detail.quick.rootObject().findChild(QQuickItem, 'detailBody')
        assert rendered.property('source') is model.detailBody
        assert not self.detail.qml_errors
        return self.detail


@pytest.fixture
def observed_overlay(tmp_path):
    value = ObservedOverlay(tmp_path)
    try:
        yield value
    finally:
        value.close()


def detail_values(model):
    return [''.join(item[0]) if isinstance(item[0], list) else item[0]
            for item in model.detail_items()]


def comparison_lines(value):
    return [line for line in value.splitlines()
            if '모델 일치 (' in line or '모델 불일치 (' in line]


def test_new_call_clears_old_comparison_then_uses_its_own_completed_evidence(observed_overlay):
    fixture = observed_overlay
    fixture.call('old-call')
    fixture.observe('old-call', requested='gpt-6-astra', response='gpt-5.6-sol')
    fixture.refresh()
    model = fixture.widget.content_model
    old = '모델 불일치 (요청:gpt-6-astra, 응답:gpt-5.6-sol)'
    assert model.context()[0] == old
    fixture.call('new-call', configured='gpt-5.6-luna')
    fixture.refresh()
    assert model.selected()['key'] == 'new-call'
    visible = '\n'.join([*model.context(), fixture.widget.accessibleName(),
                         fixture.widget.toolTip(), *detail_values(model)])
    assert comparison_lines(visible) == []
    assert 'gpt-5.6-sol' not in visible
    fixture.observe('new-call', requested='gpt-5.6-luna', response='gpt-5.6-luna')
    fixture.refresh()
    latest = '모델 일치 (gpt-5.6-luna)'
    assert model.context()[0] == latest
    assert fixture.widget.toolTip() == ''
    session_detail=detail_values(model)
    # Inspecting an older graph point leaves session totals and model groups fixed.
    model.select(model.call_id(model.rows()[0]))
    assert model.selected()['key'] == 'old-call'
    assert detail_values(model)==session_detail
    assert model.context()[0] == latest and fixture.widget.toolTip() == ''


def test_long_model_names_are_elided_with_full_accessible_details(observed_overlay, tmp_path):
    fixture = observed_overlay
    requested = 'gpt-shared-long-model-name-' + 'request-variant-' * 5
    response = 'gpt-shared-long-model-name-' + 'response-variant-' * 5
    fixture.call('long-call', configured=requested)
    fixture.observe('long-call', requested=requested, response=response)
    fixture.refresh()
    widget = fixture.widget
    model = widget.content_model
    expected = f'모델 불일치 (요청:{requested}, 응답:{response})'
    assert expected in widget.accessibleName()
    assert widget.toolTip() == ''
    assert fixture.header.toolTip() == ''
    assert ''.join(model.context_rows()) == expected
    top_metrics = QFontMetrics(font(model.appearance.family, 12))
    assert len(model.context_rows()) > 1
    assert all(top_metrics.horizontalAdvance(line) <= 348 for line in model.context_rows())
    from copy import deepcopy
    from cachemonitor.overlay_view import OverlayContent
    short=deepcopy(model.data)
    short['latest'].update(model='short-request',response_model='short-response')
    reference=OverlayContent();reference.set_content(short)
    assert model.base_height()==reference.base_height()  # Model name length must not enlarge the panel.
    assert expected in model.main_model_tooltip()
    assert not any(link.get('interaction')=='tooltip' for link in model.monitor_links())
    assert not any(link['id']=='call-detail' for link in model.monitor_links())
    assert not any(link['id'] in ('cache','cost','speed','latest-context') for link in model.monitor_links())
    child=Session('long-model-child','fixture',title='Long model child')
    child.add_usage(1789884002,'child-call',dict(input_tokens=1000,cached_input_tokens=800,
        cache_write_input_tokens=0,output_tokens=50,reasoning_output_tokens=10),
        requested,'child-turn','high',service_tier='Standard')
    fixture.observe('child-call',requested=requested,response=response)
    fixture.reader.poll()
    child_source=child.view(1789884100)
    child_source['history']=fixture.reader.enrich('fixture',child_source['history'])
    child_source.update(source='subagent',parent_thread_id='model-summary')
    fixture.refresh([child_source])
    grouped_model=model.detail_data()['models'][0]['model']
    item = next(item for item in model.detail_items()
                if isinstance(item[0],str) and item[0].startswith(grouped_model))
    assert item[8]['kind']=='single-line'
    metrics = QFontMetrics(font(model.appearance.family, item[5], item[7]))
    runs=fitted_model_runs(item[8]['modelRuns'],metrics,item[3])
    assert runs[0].text.endswith('…') and runs[1].text.endswith('…')
    from cachemonitor.overlay_icons import agent_mark
    assert runs[0].strike and runs[1].color==agent_mark('long-model-child',model.appearance).color
    assert runs[2].text==', high'
    assert requested in model.detailBody.state['accessible'] and response in model.detailBody.state['accessible']
    detail = fixture.reveal_comparison(item[0])
    assert any(grouped_model in action['label'] for action in model.state['detailActions'])
    assert widget.grab().save(str(tmp_path / 'long-model-summary.png'))
    assert detail.grab().save(str(tmp_path / 'long-model-detail.png'))
    assert not widget.qml_errors


def test_confirmed_main_and_child_models_keep_one_line_and_clear_on_new_call(observed_overlay,tmp_path):
    fixture=observed_overlay
    fixture.call('confirmed-main',configured='gpt-6-astra')
    fixture.observe('confirmed-main',requested='gpt-6-astra',response='gpt-6-sol')
    child=Session('inline-child','fixture',title='Inline child')
    child.add_usage(1789884002,'confirmed-child',dict(input_tokens=1000,cached_input_tokens=800,
        cache_write_input_tokens=0,output_tokens=50,reasoning_output_tokens=10),
        'gpt-5.6-luna','child-turn','medium',service_tier='Fast')
    fixture.observe('confirmed-child',requested='gpt-5.6-luna',response='gpt-6-sol')
    fixture.reader.poll();child_source=child.view(1789884100)
    child_source['history']=fixture.reader.enrich('fixture',child_source['history'])
    child_source.update(source='subagent',parent_thread_id='model-summary')
    fixture.refresh([child_source]);model=fixture.widget.content_model
    from cachemonitor.overlay_data import main_usage
    from cachemonitor.overlay_icons import agent_mark
    from cachemonitor.overlay_monitor import SECTION_WIDTH
    def main_line():
        return next(item for item in model.agent_summary_items(main_usage(model.data),'Main Agent',
            agent_mark(model.data['id'],model.appearance),False,SECTION_WIDTH,main=True) if item[8].get('modelRuns'))
    main=main_line();child_line=next(item for item in model._detail_items if item[8].get('modelRuns'))
    main_runs=main[8]['modelRuns'];child_runs=child_line[8]['modelRuns']
    assert [run.text for run in main_runs]==['gpt-6-astra',' gpt-6-sol',', high']
    assert [run.text for run in child_runs]==['gpt-5.6-luna',' gpt-6-sol',', medium, fast']
    for runs,task_id in ((main_runs,fixture.session.id),(child_runs,child.id)):
        assert runs[0].strike and runs[0].color=='secondary'
        assert runs[1].color==agent_mark(task_id,model.appearance).color and not runs[1].strike
        assert not runs[2].strike
    from PySide6.QtGui import QImage,QPainter
    from cachemonitor.overlay_view import palette
    drawn=[]
    class TextPainter(QPainter):
        def drawText(self,*args):
            drawn.append((str(args[-1]),args[0].y(),self.font().strikeOut(),self.pen().color().name()))
            return super().drawText(*args)
    def capture(width,height,paint,path):
        image=QImage(width,height,QImage.Format_ARGB32_Premultiplied);image.fill(0)
        painter=TextPainter(image)
        try:paint(painter)
        finally:painter.end()
        assert image.save(str(path))
    capture(model.panel_width(),model.panel_height(),model.paint,tmp_path/'inline-main.png')
    capture(round(model.detail_width),model.state['detailBodyHeight'],model.detailBody.paint,tmp_path/'inline-child.png')
    colors=palette(model.appearance)
    for request,task_id in (('gpt-6-astra',fixture.session.id),('gpt-5.6-luna',child.id)):
        rendered=next(value for value in drawn if value[0]==request)
        assert rendered[2] and rendered[3]==colors['secondary'].name()
        response=next(value for value in drawn if value[0]==' gpt-6-sol' and value[1]==rendered[1])
        assert not response[2] and response[3]==agent_mark(task_id,model.appearance).color
    from dataclasses import replace
    previous=model.appearance
    fixture.widget.set_content(model.data,appearance=replace(previous,accent='#ff00ff'))
    assert main_line()[8]['modelRuns'][1].color==agent_mark(fixture.session.id,model.appearance).color
    assert next(item for item in model._detail_items if item[8].get('modelRuns'))[8]['modelRuns'][1].color==agent_mark(child.id,model.appearance).color
    assert palette(model.appearance)['accent'].name()=='#ff00ff'
    fixture.widget.set_content(model.data,appearance=previous)
    geometry=model.layout();height=model.state['detailBodyHeight']
    fixture.call('new-main',configured='gpt-5.6-sol');fixture.refresh([child_source])
    own=main_usage(model.data)
    assert not own['model_alert_confirmed'] and not own['requested_model']
    assert len(model.model_runs(own))==1
    assert model.subagent_call_label(own)=='gpt-5.6-sol, high'
    assert 'gpt-6-astra' not in model.main_model_tooltip()
    fixture.observe('new-main',requested='gpt-5.6-sol',response='gpt-5.6-sol')
    fixture.refresh([child_source]);assert len(model.model_runs(main_usage(model.data)))==1
    fixture.observe('confirmed-main',requested='gpt-6-astra',response='gpt-6-sol',attempt='conflicting-old',conflict=True)
    fixture.refresh([child_source]);assert len(model.model_runs(main_usage(model.data)))==1
    assert model.layout()==geometry and model.state['detailBodyHeight']==height


def test_canonical_withdrawal_removes_inline_style_without_using_old_metadata(observed_overlay):
    from copy import deepcopy
    from cachemonitor.overlay_data import main_usage
    fixture=observed_overlay;fixture.call('withdrawn')
    fixture.observe('withdrawn',response='gpt-6-sol');fixture.refresh()
    model=fixture.widget.content_model;original=deepcopy(model.data)
    assert len(model.model_runs(main_usage(original)))==3
    for state in ('일치','관측 충돌','관측 누락'):
        changed=deepcopy(original);own=main_usage(changed)
        own.update(model_alert_confirmed=False,model_mismatch=False,model_state=state,
                   model='gpt-5.6-luna',requested_model=None,response_model=None,effort=None,mode=None)
        fixture.widget.set_content(changed)
        runs=model.model_runs(main_usage(model.data))
        assert len(runs)==1 and not runs[0].strike and runs[0].color=='secondary'
        assert 'gpt-6-astra' not in runs[0].text and 'gpt-6-sol' not in model.main_model_tooltip()
        assert model.subagent_call_label(main_usage(model.data))=='gpt-5.6-luna, 미확인'
    missing=deepcopy(original);main_usage(missing)['requested_model']=None
    fixture.widget.set_content(missing)
    assert len(model.model_runs(main_usage(model.data)))==1


def test_hiding_overlay_discards_active_glows_and_hidden_updates_do_not_start_timer(observed_overlay):
    from copy import deepcopy
    from cachemonitor.overlay_data import main_usage
    fixture=observed_overlay;fixture.call('hide-glow');fixture.refresh()
    widget=fixture.widget;model=widget.content_model;changed=deepcopy(model.data)
    main_usage(changed)['cost']+=.5;widget.set_content(changed)
    assert model._row_glow_timer.isActive()
    widget.hide()
    assert not model._row_glow_timer.isActive() and not model.row_glows.needs_frame
    changed=deepcopy(changed);main_usage(changed)['cost']+=.5;widget.set_content(changed)
    assert not model._row_glow_timer.isActive() and not model.row_glows.needs_frame
    widget.show()
    assert not model._row_glow_timer.isActive()


def test_capture_compact_native_mismatch_and_glow_frames(observed_overlay):
    import json
    import os
    import time
    from copy import deepcopy
    from pathlib import Path
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QPainter
    from cachemonitor.overlay_data import main_usage
    from cachemonitor.overlay_monitor import MARGIN,SECTION_WIDTH
    from cachemonitor.quick_qa import walk
    destination=os.environ.get('CODEXON_MISMATCH_EVIDENCE')
    if not destination:pytest.skip('CODEXON_MISMATCH_EVIDENCE enables native frame capture.')
    output=Path(destination);output.mkdir(parents=True,exist_ok=True)
    fixture=observed_overlay;fixture.call('capture-main')
    fixture.observe('capture-main',response='gpt-6-sol')
    child=Session('capture-child','fixture',title='Capture child')
    child.add_usage(1789884002,'capture-child-call',dict(input_tokens=1000,cached_input_tokens=800,
        cache_write_input_tokens=0,output_tokens=50,reasoning_output_tokens=10),
        'gpt-5.6-luna','capture-turn','medium',service_tier='Fast')
    fixture.observe('capture-child-call',requested='gpt-5.6-luna',response='gpt-6-sol')
    fixture.reader.poll();source=child.view(1789884100)
    source['history']=fixture.reader.enrich('fixture',source['history'])
    source.update(source='subagent',parent_thread_id='model-summary')
    fixture.refresh([source]);widget=fixture.widget;model=widget.content_model
    model.put(reducedMotion=False);widget.set_layout(reduced=True,detail=True)
    geometry=model.layout();scale=model.appearance.scale
    fixture.detail=OverlayDetail(model)
    fixture.detail.resize(round(SECTION_WIDTH*scale),round(geometry['sub_height']*scale))
    fixture.detail.show()
    main=('fixture',model.data['id']);owner=('fixture','capture-child')
    def until(condition,message=lambda:'Overlay condition did not become observable.'):
        deadline=time.monotonic()+3
        while not condition():
            assert time.monotonic()<deadline,message()
            fixture.app.processEvents()
    def rendered_actions():
        root=fixture.detail.quick.rootObject()
        return {item.objectName():item for item in walk(root)
                if item.objectName().startswith('detailAction-') and item.isVisible() and item.width()>0 and item.height()>0} if root else {}
    def action_geometry():
        expected={'detailAction-'+action['id'] for action in model.state['detailActions']}
        assert expected
        until(lambda:expected<=rendered_actions().keys(),
              lambda:f'Expected rendered controls {sorted(expected)}; observed {sorted(rendered_actions())}.')
        items=rendered_actions()
        return [dict(id=action['id'],x=items['detailAction-'+action['id']].x(),y=items['detailAction-'+action['id']].y(),
                     width=items['detailAction-'+action['id']].width(),height=items['detailAction-'+action['id']].height())
                for action in model.state['detailActions']]
    frames=[];errors=[];started=None
    def capture(stage):
        model.advance_row_glows();fixture.app.processEvents()
        actions=action_geometry()
        captured_at=time.monotonic();main_opacity=model.row_glow_opacity(main);child_opacity=model.row_glow_opacity(owner)
        image=widget.grab().toImage();child_image=fixture.detail.grab().toImage()
        painter=QPainter(image)
        try:painter.drawImage(round(MARGIN*scale),round(geometry['sub_y']*scale),child_image)
        finally:painter.end()
        assert image.save(str(output/(stage+'.png')))
        frames.append(dict(stage=stage,elapsed_ms=None if started is None else (captured_at-started)*1000,
            finished_ms=None if started is None else (time.monotonic()-started)*1000,
            layout=model.layout(),main_opacity=main_opacity,child_opacity=child_opacity,
            main_cost=main_usage(model.data)['cost'],
            child_speed=model.data['subagent_finance']['agents'][0]['output_speed_summary']['value'],actions=actions))
    def safely(callback):
        def run():
            try:callback()
            except Exception as error:errors.append(error)
        return run
    capture('compact-inline')
    changed=deepcopy(model.data);main_usage(changed)['cost']+=.5
    changed['subagent_finance']['agents'][0]['output_speed_summary']['value']=200.
    started=time.monotonic();widget.set_content(changed)
    timers=[]
    def schedule(delay,callback):
        timer=QTimer(model);timer.setSingleShot(True);timer.setTimerType(Qt.PreciseTimer)
        timer.timeout.connect(safely(callback));timer.start(delay);timers.append(timer)
    def mid_update():
        refreshed=deepcopy(model.data);main_usage(refreshed)['cost']+=.5
        refreshed['subagent_finance']['agents'][0]['output_speed_summary']['value']=300.
        widget.set_content(refreshed);capture('glow-mid-update')
    schedule(70,lambda:capture('glow-70ms'))
    schedule(200,mid_update)
    schedule(700,lambda:capture('glow-finished'))
    try:
        until(lambda:len(frames)==4 or errors)
        if errors:raise errors[0]
        assert all(frame['layout']==geometry for frame in frames)
        assert frames[1]['main_opacity']>0 and frames[1]['child_opacity']>0
        assert frames[2]['child_speed']==300. and frames[2]['main_opacity']>0
        assert frames[3]['main_opacity']==frames[3]['child_opacity']==0
        assert not model._row_glow_timer.isActive()
        assert not widget.qml_errors and not fixture.detail.qml_errors
    finally:
        for timer in timers:timer.stop();timer.deleteLater()
        (output/'overlay-frames.json').write_text(json.dumps(frames,ensure_ascii=False,indent=2),encoding='utf-8')
