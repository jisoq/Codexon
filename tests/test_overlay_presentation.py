import copy
from dataclasses import replace
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from cachemonitor.core import Session
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay import SessionOverlay
from cachemonitor.overlay_appearance import default_appearance
from cachemonitor.overlay_view import money, percent


def summary(misses=0, count=12):
    s=Session('clean','home',title='CacheMonitor 세션 오버레이 디자인 구현')
    for i in range(count):
        s.add_usage(1789884000+i,str(i),dict(input_tokens=100000,cached_input_tokens=0 if i>=count-misses else 90000,
                    cache_write_input_tokens=0,output_tokens=1000,reasoning_output_tokens=400),
                    'gpt-6-astra','turn','high','Standard')
    engine=AnalysisEngine();engine.ingest([s.view(1789884100)])
    return OverlaySummaries().collect(engine)[0]


def test_english_overlay_translates_visible_text_before_detail_layout(monkeypatch):
    import re
    from PySide6.QtGui import QFontMetrics
    from cachemonitor.i18n import set_language, tr, Verbatim
    from cachemonitor.overlay_view import Drawing, font
    app=QApplication.instance() or QApplication([])
    data=summary(misses=2);data['title']='Sample task'
    for row in (data['latest'],data['recent'][-1]):
        row.update(output_speed=72.0,completion_latency_ms=40560,
                   requested_model='gpt-6-astra',response_model='gpt-6-astra',
                   model_match='일치',model_setting=False,
                   transport='WebSocket',transport_source='response_id')
    original=copy.deepcopy(data);drawn=[]
    draw_text=Drawing.text
    def record(d,value,*args,**kwargs):
        drawn.append(tr(value))
        return draw_text(d,value,*args,**kwargs)
    monkeypatch.setattr(Drawing,'text',record)
    set_language('en');w=SessionOverlay()
    try:
        w.set_content(data);w.set_layout(detail=True);m=w.content_model
        for unit in ('tokens','usd'):
            for tab in ('latest','history'):
                m.monitor_action('unit-'+unit);m.monitor_action('tab-'+tab)
                image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied)
                image.fill(0);p=QPainter(image);m.paint(p);p.end()
        detail=[line for item in m._detail_items for line in (item[0] if isinstance(item[0],list) else [item[0]])]
        assert not [value for value in drawn+detail if re.search('[가-힣]',value)]
        assert 'Output speed' in drawn and 'Current' in drawn and 'Recent' in drawn
        assert '40.56 s' in detail
        assert data==original
        # Full sentences must be translated before line splitting, not fragment by fragment.
        formulas=[item for item in m._detail_items if isinstance(item[0],list)]
        assert formulas
        for item in formulas:
            metrics=QFontMetrics(font(m.appearance.family,item[5],item[7]))
            assert all(metrics.horizontalAdvance(line)<=item[3] for line in item[0])
        assert tr(Verbatim('세션 2회 40.56초'))=='세션 2회 40.56초'
    finally:
        w.close();set_language('ko');app.processEvents()
    assert tr('40.56초 · 2회')=='40.56초 · 2회'


def test_call_selection_is_by_identifier_and_never_changes_monitor():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=summary(count=24)
    try:
        w.set_content(data);m=w.content_model;latest=m.selected_id
        m.select(m.call_id(m.rows()[0]));old=m.selected_id;before=w.lines()
        assert old!=latest and not m.follow_latest
        updated=summary(count=25);w.set_content(updated)
        assert m.selected_id==old and m.call_id(m.selected())==old and w.lines()[3:7]==[percent(updated['cache_rate']),money(updated['cost']),money(updated['mean_cost']),money(updated['latest_cost'])]
        m.select(m.call_id(m.rows()[-1]));assert m.follow_latest
        w.set_content(summary(count=26));assert m.selected_id==m.call_id(m.rows()[-1])
        m.select(m.call_id(m.rows()[-2]));fixed=m.selected_id
        body=copy.deepcopy(m._detail_items)
        m.detailGraph.hover_at(208-4,50)
        assert m.selected_id==fixed and m.call_id(m.selected())==fixed and m._detail_items==body
        m.detailGraph.clear_hover();assert m.call_id(m.selected())==fixed
        w.set_content(None,'기록 확인 중');assert m.selected_id is None and m.rows()==[] and m.selected()=={}
        assert m.notice_card()[0]=='기록 확인 중' and not m.monitor_links()
        assert all(value!='—' for value in w.lines())
    finally:w.close();app.processEvents()


def test_pricing_ratio_requires_an_unpriced_work_call():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();w.set_layout(detail=True);data=summary()
    try:
        # A cumulative coverage gap is independent of pricing the observed calls.
        data.update(partial=True,coverage_gap=True,missing=0)
        w.set_content(data)
        assert not any('산정 ' in str(item[0]) for item in w.content_model._detail_items)
        data={**data,'priced':data['calls']-1,'missing':1}
        w.set_content(data)
        assert any('산정 ' in str(item[0]) for item in w.content_model._detail_items)
    finally:w.close();app.processEvents()


def test_detail_expansion_preserves_monitor_pixels_and_font_scale():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        w.set_content(summary());m=w.content_model
        def render():
            image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied);image.fill(0)
            p=QPainter(image);m.paint(p);p.end();return image
        normal=render();m.set_layout(detail=True);detail=render()
        assert m.panel_width()==590 and m.panel_height()==542 and m.monitor_x==240
        normal_crop=normal.copy(12,48,326,490);detail_crop=detail.copy(252,48,326,490)
        a=bytes(normal_crop.constBits());b=bytes(detail_crop.constBits())
        # Qt's translated antialias coverage may differ by one channel level.
        assert max(abs(x-y) for x,y in zip(a,b))<=1
        for size in (10,14,21):
            w.set_content(summary(),appearance=replace(default_appearance(True),font_size=size));s=max(1,size/14)
            assert m.panel_width()==round(590*s) and m.panel_height()==round(542*s)
    finally:w.close();app.processEvents()


def test_rendered_variants_have_no_qml_errors_or_idle_motion(tmp_path):
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();base=summary()
    try:
        for dark in (False,True):
            for size in (14,21):
                w.set_content(base,appearance=replace(default_appearance(dark),font_size=size));w.show();QTest.qWait(40)
                first=w.grab().toImage();QTest.qWait(40);assert first==w.grab().toImage()
                assert first.save(str(tmp_path/f'monitor-{dark}-{size}.png'))
        for name,value,note in [('warning',summary(misses=3),''),('loading',None,'기록 확인 중'),('error',base,'수집 오류'),('empty',summary(count=0),'')]:
            w.set_content(value,note);QTest.qWait(30);assert w.grab().save(str(tmp_path/f'{name}.png'))
        assert not w.qml_errors
    finally:w.close();app.processEvents()


def test_collection_errors_show_concise_observed_states_without_paths_or_tracebacks():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    errors=[
        'Traceback (most recent call last):\n  File "C:\\private\\worker.py", line 20\nsqlite3.DatabaseError: database disk image is malformed',
        'sqlite3.DatabaseError: database disk image is malformed: C:\\private\\cache.sqlite',
        'PermissionError: [WinError 5] Access is denied: C:\\private\\records.jsonl',
        'RuntimeError: database operation failed at /private/backend.py:19',
        '기록 수집 지연',
    ]
    try:
        data=summary();data['_collection']={'errors':errors.copy()};w.set_content(data,'수집 오류');m=w.content_model
        values=[''.join(item[0]) if isinstance(item[0],list) else item[0] for item in m.detail_items()]
        for expected in ('기록 손상 · 2건','기록 접근 실패','기록 읽기 실패','기록 수집 지연'):assert values.count(expected)==1
        assert '수집 오류' not in values and m.status_text()=='수집 오류 +4'
        visible='\n'.join(values)+m.detailBody.state['accessible']
        for diagnostic in ('Traceback','C:\\private','/private/backend.py','PermissionError','sqlite3','RuntimeError'):assert diagnostic not in visible
        assert data['_collection']['errors']==errors and m.data['_collection']['errors']==errors
    finally:w.close();app.processEvents()
