"""End-to-end call speed uses exact completed evidence, never turn duration."""
import copy
import time
from dataclasses import replace

import pytest

from cachemonitor.analytics import output_speed, output_speed_summary
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.overlay_data import OverlaySummaries, call_summary


def measured(**changes):
    return dict(dict(output=3000,reasoning=2000,response_status='completed',timing_valid=True,
                     completion_latency_ms=30000),**changes)


def test_speed_includes_reasoning_once_and_uses_real_seconds():
    assert output_speed(measured())==100
    assert output_speed(measured(output=0,reasoning=0))==0
    assert output_speed(measured(generation_latency_ms=20000))==100
    assert call_summary(measured(),1)['output_speed']==100


@pytest.mark.parametrize('change',[
    {'response_status':'incomplete'},{'response_status':'failed'}, {'response_status':'pending'},
    {'timing_valid':False},{'observation_missing':True},{'model_conflict':True},
    {'output_conflict':True},{'transport_source':'conflict'},{'reasoning':3001},
    {'output':None},{'output':-1},{'output':True},
    {'completion_latency_ms':None},{'completion_latency_ms':0},
    {'completion_latency_ms':-1},{'completion_latency_ms':float('nan')},
    {'completion_latency_ms':float('inf')},{'completion_latency_ms':True},
])
def test_unmeasurable_calls_are_not_zero_or_guessed(change):
    assert output_speed(measured(**change)) is None


def test_summary_weights_by_measured_time_and_excludes_both_sides_of_missing_pairs():
    rows=[measured(),measured(output=2000,reasoning=0,completion_latency_ms=10000),
          measured(output=999999,timing_valid=False)]
    result=output_speed_summary(rows)
    assert result==dict(value=125,output=5000,seconds=40,n=2,N=3,missing=1)
    assert output_speed_summary([])['value'] is None
    assert output_speed_summary([rows[-1]])['value'] is None


def snapshot():
    now=time.time();session=Session('speed-task','synthetic-home',title='출력 속도 확인',cwd='C:/sample')
    for i,output in enumerate((999,2000,3000)):
        session.add_usage(now-60+i*20,f'call-{i}',dict(input_tokens=100000,cached_input_tokens=98500,
                          cache_write_input_tokens=0,output_tokens=output,reasoning_output_tokens=500),
                          'gpt-6-astra',f'turn-{i}','high','Standard')
    view=session.view(now)
    for i,row in enumerate(view['history']):
        row.update(response_status='completed',timing_valid=i>0,completion_latency_ms=(5000,10000,30000)[i])
    return dict(ts=now,sessions=[view],homes=['synthetic-home'],errors=[],unassigned=[])


def test_filtered_dashboard_and_overlay_share_call_values_without_previous_fallback():
    source=snapshot();engine=AnalysisEngine();engine.ingest(source['sessions'])
    result=engine.query(dict(page=0,start=0,end=source['ts']+1))
    assert result['overview']['summary']['output_speed']['value']==125
    data=OverlaySummaries().collect(engine)[0]
    assert data['latest']['output_speed']==100
    assert [r['output_speed'] for r in data['all_calls']]==[None,200,100]
    end=source['sessions'][0]['history'][-1]['ts']
    assert engine.query(dict(page=0,start=end,end=source['ts']+1))['overview']['summary']['output_speed']['value']==100
    source['sessions'][0]['history'][-1]['timing_valid']=False
    engine.ingest(source['sessions'])
    assert OverlaySummaries().collect(engine)[0]['latest']['output_speed'] is None


def test_rendered_summary_click_and_overlay_speed_navigation(tmp_path):
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard, choose
    from cachemonitor.overlay_view import SessionOverlay
    from cachemonitor.overlay_chrome import OverlayLinks, named_item
    from cachemonitor.overlay_appearance import default_appearance
    from cachemonitor.quick_qa import click, control, walk
    from cachemonitor.i18n import set_language
    from cachemonitor.i18n import tr
    from cachemonitor.overlay_view import font
    from PySide6.QtGui import QFontMetrics
    app=QApplication.instance() or QApplication([])
    previous_shell=app.property('cachemonitorDisableShellIntegration')
    app.setProperty('cachemonitorDisableShellIntegration',True)
    window=Dashboard([],start_worker=False,live_limits=False,
                     settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    overlay=SessionOverlay();links=OverlayLinks(overlay.content_model)
    try:
        source=snapshot();window.receive(source);window.resize(1280,900);window.show();QTest.qWait(150)
        assert window.metrics[4].text()=='125.0 tok/s'
        assert '2 / 대상 3' in window.metric_notes[4].text()
        assert window.quick.grabFramebuffer().save(str(tmp_path/'dashboard.png'))
        click(window,control(window,window.metrics[4]));QTest.qWait(50)
        assert [r['output_speed'] for r in window.aggregate_records]==[200,100]
        assert '125.0 tok/s' in window.overview_detail['text'].text()
        engine=AnalysisEngine();engine.ingest(source['sessions']);data=OverlaySummaries().collect(engine)[0]
        overlay.set_content(data);overlay.show();QTest.qWait(60)
        m=overlay.content_model
        assert m.headline_metrics()[1]['number']=='100.0'
        m.select(data['all_calls'][1]['id'])
        assert m.selected()['output_speed']==200 and m.headline_metrics()[1]['number']=='100.0'
        m.select(data['latest']['id'])
        assert overlay.quick.grabFramebuffer().save(str(tmp_path/'overlay.png'))
        links.setGeometry(overlay.geometry());links.sync();links.show();QTest.qWait(30)
        targets=[];links.view.navigationRequested.connect(targets.append)
        item=named_item(links.quick.rootObject(),'nav-speed')
        click(links,item)
        assert targets[-1].call_id==data['latest']['id'] and targets[-1].section=='time'
        window.navigate(targets[-1])
        for _ in range(40):
            QTest.qWait(25)
            if window.detail_scroll.verticalPosition.value()>0:break
        assert window.detail_scroll.verticalPosition.value()>0
        assert '100.0 tok/s' in window.detail_sections['usage'][1].text()
        assert '30.00' in window.detail_sections['time'][1].text()
        assert window.quick.grabFramebuffer().save(str(tmp_path/'call-detail.png'))
        choose(window.record_view_choice,'calls');window.record_view_changed()
        window.extra_column_controls['output_speed'].setChecked(True);window.render_explorer()
        index=[key for key,title in window.active_columns].index('output_speed')
        assert window.response_cell(data['latest'],index,Qt.DisplayRole)=='100.0 tok/s'
        window.nav.setCurrentRow(0);window.resize(1000,720);QTest.qWait(60)
        window.scrollers[0].verticalPosition.setValue(0);QTest.qWait(50)
        speed_control=control(window,window.metrics[4])
        assert speed_control.width()>0
        for metric in window.metrics:
            text_items=[item for item in walk(control(window,metric)) if item.metaObject().indexOfProperty('truncated')>=0]
            assert text_items and all(not item.property('truncated') for item in text_items)
        assert window.quick.grabFramebuffer().save(str(tmp_path/'dashboard-small.png'))
        links.hide()
        for language in ('ko','en'):
            set_language(language)
            for compact in (False,True):
                for scale in (1,1.5):
                    overlay.set_content(data,appearance=replace(default_appearance(True),font_size=round(14*scale)))
                    overlay.set_layout(reduced=compact);QTest.qWait(30)
                    for metric in m.headline_metrics():
                        assert metric['text_width']<=metric['width']
                        assert QFontMetrics(font(m.appearance.family,metric['label_size'])).horizontalAdvance(tr(metric['label']))<=metric['width']
                    assert overlay.quick.grabFramebuffer().save(str(tmp_path/f'overlay-{language}-{compact}-{scale}.png'))
        for speed in (None,0,.001,99999.9,1e12):
            large=copy.deepcopy(data);large['latest']['output_speed']=speed;overlay.set_content(large)
            assert all(item['text_width']<=item['width'] for item in m.headline_metrics())
    finally:
        set_language('ko');links.close();overlay.close();window.quit_app();app.processEvents()
        app.setProperty('cachemonitorDisableShellIntegration',previous_shell)
