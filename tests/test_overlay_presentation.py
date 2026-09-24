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


def test_english_overlay_keeps_session_title_pixels(tmp_path):
    from cachemonitor.i18n import set_language
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=summary()
    data['title']='사용한도 주석 표시 정리'
    try:
        images=[]
        for language in ('ko','en'):
            set_language(language);w.set_content(data);m=w.content_model
            image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied);image.fill(0)
            painter=QPainter(image);m.paint(painter);painter.end();images.append(image)
            assert image.save(str(tmp_path/f'raw-title-{language}.png'))
        assert images[0].copy(16,12,260,28)==images[1].copy(16,12,260,28)
        assert images[0].copy(16,94,200,36)!=images[1].copy(16,94,200,36)
        assert w.accessibleName().splitlines()[0]==data['title']
    finally:
        w.close();set_language('ko');app.processEvents()


def test_call_selection_is_by_identifier_and_never_changes_monitor():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=summary(count=24)
    try:
        w.set_content(data);m=w.content_model;latest=m.selected_id
        m.detailGraph.key(Qt.Key_Home);old=m.selected_id;before=w.lines()
        assert old!=latest and not m.follow_latest
        updated=summary(count=25);w.set_content(updated)
        assert m.selected_id==old and m.selected()['key']==old and w.lines()[3:7]==[percent(updated['cache_rate']),money(updated['cost']),money(updated['mean_cost']),money(updated['latest_cost'])]
        m.detailGraph.key(Qt.Key_End);assert m.follow_latest
        w.set_content(summary(count=26));assert m.selected_id==m.rows()[-1]['id']
        m.detailGraph.key(Qt.Key_Left);fixed=m.selected_id
        body=copy.deepcopy(m._detail_items)
        m.detailGraph.hover_at(208-4,50)
        assert m.selected_id==fixed and m.selected()['id']==fixed and m._detail_items==body
        m.detailGraph.clear_hover();assert m.selected()['id']==fixed
        w.set_content(None,'기록 확인 중');assert m.selected_id is None and m.rows()==[] and m.selected()=={}
        assert all(value=='—' for value in w.lines()[3:7])
    finally:w.close();app.processEvents()


def test_detail_expansion_preserves_monitor_pixels_and_font_scale():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        w.set_content(summary());m=w.content_model
        def render():
            image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied);image.fill(0)
            p=QPainter(image);m.paint(p);p.end();return image
        normal=render();m.set_layout(detail=True);detail=render()
        assert m.panel_width()==620 and m.panel_height()==546 and m.monitor_x==240
        normal_crop=normal.copy(16,48,348,496);detail_crop=detail.copy(256,48,348,496)
        a=bytes(normal_crop.constBits());b=bytes(detail_crop.constBits())
        # Qt's translated antialias coverage may differ by one channel level.
        assert max(abs(x-y) for x,y in zip(a,b))<=1
        for size in (10,14,21):
            w.set_content(summary(),appearance=replace(default_appearance(True),font_size=size));s=max(1,size/14)
            assert m.panel_width()==round(620*s) and m.panel_height()==round(546*s)
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
