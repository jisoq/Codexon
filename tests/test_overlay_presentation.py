import copy
import math
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


def subagent_summary():
    parent=Session('parent','home',title='Parent')
    child=Session('child','home',title='Child with two models')
    other=Session('other','home',title='Other child')
    usage=dict(input_tokens=100_000,cached_input_tokens=50_000,cache_write_input_tokens=0,
               output_tokens=10_000,reasoning_output_tokens=4_000)
    for session,key,model in ((parent,'own','gpt-6-astra'),(child,'luna','gpt-6-luna'),
                              (child,'sol','gpt-6-sol'),(other,'other-luna','gpt-6-luna')):
        session.add_usage(1789884000,key,usage,model,'turn','high','Standard')
    views=[parent.view(1789884100)]
    for session in (child,other):
        view=session.view(1789884100);view.update(source='subagent',parent_thread_id='parent')
        views.append(view)
    engine=AnalysisEngine();engine.ingest(views)
    return next(row for row in OverlaySummaries().collect(engine) if row['id']=='parent')


def test_sub_groups_sort_by_own_latest_update_and_keep_expansion_identity():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=subagent_summary()
    try:
        agents={row['id']:row for row in data['subagent_finance']['agents']}
        agents['child']['latest_ts']=100;agents['other']['latest_ts']=200
        w.set_content(data);m=w.content_model
        assert [row['id'] for row in m.detail_groups()]==['other','child']
        key=m.detail_groups()[0]['key'];m.toggle_detail_group(key)
        updated=copy.deepcopy(data)
        next(row for row in updated['subagent_finance']['agents'] if row['id']=='child')['latest_ts']=300
        w.set_content(updated)
        assert [row['id'] for row in m.detail_groups()]==['child','other']
        assert key in m.expanded_groups
    finally:w.close();app.processEvents()


def test_unknown_agent_identity_and_expanded_exact_token_cost_details():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        w.set_content(subagent_summary());m=w.content_model
        assert m.subagent_call_label({})=='미확인'
        assert m.subagent_call_label(dict(model='미확인',effort='미확인'))=='미확인'
        group=m.detail_groups()[0];m.toggle_detail_group(group['key'])
        items=m.detail_items()
        assert any('tps' in str(item[0]) or '평균 출력' in str(item[0]) for item in items)
        categories=[item for item in items if item[8].get('tokenMarker')]
        assert categories and all(item[8].get('square') and not item[8].get('dot') for item in categories)
        for item in categories:
            value=next(row for row in items if row[2]==item[2] and row[8].get('right'))
            assert '/' in str(value[0]) and '$' in str(value[0])
    finally:w.close();app.processEvents()


def test_english_overlay_translates_visible_text_before_detail_layout(monkeypatch):
    import re
    from PySide6.QtGui import QFontMetrics
    from cachemonitor.i18n import set_language,tr,Verbatim
    from cachemonitor.overlay_view import Drawing,font
    app=QApplication.instance() or QApplication([]);data=subagent_summary();data['title']='Sample task'
    original=copy.deepcopy(data);drawn=[];draw_text=Drawing.text
    def record(d,value,*args,**kwargs):
        drawn.append(tr(value));return draw_text(d,value,*args,**kwargs)
    monkeypatch.setattr(Drawing,'text',record);set_language('en');w=SessionOverlay()
    try:
        w.set_content(data);m=w.content_model
        for row in m.detail_groups():m.toggle_detail_group(row['key'])
        m.monitor_action('main-detail')
        image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied);image.fill(0)
        painter=QPainter(image)
        try:m.paint(painter)
        finally:painter.end()
        detail=[line for item in m._detail_items for line in (item[0] if isinstance(item[0],list) else [item[0]])]
        assert not [value for value in drawn+detail if re.search('[가-힣]',value)]
        assert data==original
        for item in [*m._detail_items,*m.main_items()]:
            if isinstance(item[0],list):
                metrics=QFontMetrics(font(m.appearance.family,item[5],item[7]))
                assert all(metrics.horizontalAdvance(line)<=item[3] for line in item[0])
        assert tr(Verbatim('세션 2회 40.56초'))=='세션 2회 40.56초'
    finally:w.close();set_language('ko');app.processEvents()


def test_subagent_labels_show_own_call_evidence_and_elide_names():
    from cachemonitor.overlay_view import OverlayContent
    app=QApplication.instance() or QApplication([]);m=OverlayContent();data=subagent_summary()
    child=data['subagent_finance']['agents'][0]
    child.update(model='gpt-6.1-sol',effort='high',response_model='gpt-6.1-sol',model_state='일치',model_mismatch=False)
    m.set_content(data);height=m.state['detailBodyHeight'];changed=copy.deepcopy(data)
    name='Analyzer capacity design with a deliberately long task name'
    changed['subagent_finance']['agents'][0]['title']=name;m.set_content(changed)
    assert m.state['detailBodyHeight']==height
    assert next(item for item in m._detail_items if item[0]==name)[8]['kind']=='single-line'
    action=next(item for item in m.state['detailActions'] if name in item['label'])
    assert name in action['label'] and 'gpt-6.1-sol, high' in action['label']
    for state,mismatch,response,expected in (
            ('일치',False,'gpt-6.1-sol',''),('모델명 불일치',True,'gpt-6-luna',' (불일치: gpt-6-luna)'),
            ('관측 충돌',False,'gpt-6-luna',''),('관측 누락',False,None,'')):
        value=dict(child,model_state=state,model_mismatch=mismatch,response_model=response)
        assert m.subagent_call_label(value)==f'gpt-6.1-sol, high{expected}'
    assert m.subagent_call_label(dict(child,mode='Standard'))=='gpt-6.1-sol, high'
    assert m.subagent_call_label(dict(child,mode='Fast'))=='gpt-6.1-sol, high, fast'


def test_call_selection_is_by_identifier_and_never_changes_main_summary():
    from cachemonitor.overlay_data import main_usage
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=summary(count=24)
    try:
        w.set_content(data);m=w.content_model;latest=m.selected_id
        m.select(m.call_id(m.rows()[0]));old=m.selected_id
        assert old!=latest and not m.follow_latest
        updated=summary(count=25);w.set_content(updated)
        assert m.selected_id==old and m.call_id(m.selected())==old
        assert main_usage(m.data)==main_usage(updated)
        m.select(m.call_id(m.rows()[-1]));assert m.follow_latest
        w.set_content(summary(count=26));assert m.selected_id==m.call_id(m.rows()[-1])
        body=copy.deepcopy(m.main_items());m.select(m.call_id(m.rows()[0]))
        assert m.main_items()==body
        w.set_content(None,'기록 확인 중')
        assert m.selected_id is None and m.rows()==[] and m.selected()=={}
        assert m.notice_card()[0]=='기록 준비 중'
    finally:w.close();app.processEvents()


def test_pricing_ratio_always_reports_priced_and_total_calls_in_details():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=subagent_summary()
    try:
        w.set_content(data);m=w.content_model
        group=m.detail_groups()[0];m.toggle_detail_group(group['key'])
        assert any(item[0]==f"호출 {group['priced']:,}/{group['calls']:,}" for item in m.detail_items())
        assert any(item[0]==f"호출 {data['main_usage']['priced']:,}/{data['main_usage']['calls']:,}" for item in m.main_items())
    finally:w.close();app.processEvents()


def test_subagent_groups_share_one_population_and_scroll_without_moving_main():
    from cachemonitor.overlay_chrome import OverlayDetail,named_item
    from tests.test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();pane=None
    try:
        data=subagent_summary();w.set_content(data);m=w.content_model
        assert {row['id'] for row in m.detail_groups()}=={'child','other'}
        assert math.isclose(sum(row['cost'] for row in m.detail_groups()),data['subagent_finance']['cost'],abs_tol=1e-12)
        assert m.detail_data()['cost']!=data['cost']
        key=m.detail_groups()[0]['key'];m.toggle_detail_group(key)
        before=copy.deepcopy(m._detail_items);m.select(m.call_id(m.rows()[0]));assert m._detail_items==before
        pane=OverlayDetail(m);pane.resize(276,100);pane.show()
        wait_for_rendered(lambda:pane.quick is not None and pane.quick.rootObject() is not None)
        scroll=named_item(pane.quick.rootObject(),'detailScroll');fixed=m.layout()['main'];height=m.panel_height()
        scroll.setProperty('contentY',40)
        wait_for_rendered(lambda:scroll.property('contentY')>0)
        assert m.layout()['main']==fixed and m.panel_height()==height
        assert named_item(pane.quick.rootObject(),'detailMode-model') is None
        removed=copy.deepcopy(data);removed['subagent_finance']['agents']=[];w.set_content(removed)
        assert key not in m.expanded_groups
        w.set_content(summary());assert not m.has_subagents() and not m.expanded_groups and not m.detail_data()
    finally:
        if pane:pane.close()
        w.close();app.processEvents()


def test_token_quantities_use_k_m_b_without_unit_word():
    from cachemonitor.overlay_view import amount
    assert [amount(n) for n in (999,1_250,1_250_000,1_250_000_000)]==['999','1.2K','1.2M','1.2B']


def test_main_expansion_preserves_charts_and_scales_column_width():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        w.set_content(subagent_summary());m=w.content_model;initial=m.layout()
        m.monitor_action('main-detail');expanded=m.layout()
        assert expanded['charts']==initial['charts'] and expanded['main']==initial['main']
        assert expanded['height']>initial['height'] and expanded['sub_height']==initial['sub_height']
        for size in (10,14,21):
            w.set_content(subagent_summary(),appearance=replace(default_appearance(True),font_size=size))
            assert m.panel_width()==round(300*max(1,size/14))
    finally:w.close();app.processEvents()


def test_rendered_variants_have_no_qml_errors_and_idle_heartbeat_stops(tmp_path):
    from cachemonitor.overlay_chrome import named_item
    from tests.test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();base=summary()
    try:
        for dark in (False,True):
            for size in (14,21):
                w.set_content(base,appearance=replace(default_appearance(dark),font_size=size));w.show()
                wait_for_rendered(lambda:w.quick is not None and w.quick.rootObject() is not None)
                assert not w.content_model.state['heartbeatActive']
                assert w.grab().save(str(tmp_path/f'monitor-{dark}-{size}.png'))
        for name,value,note in [('warning',summary(misses=3),''),('loading',None,'기록 확인 중'),('error',base,'수집 오류'),('empty',summary(count=0),'')]:
            w.set_content(value,note)
            assert w.grab().save(str(tmp_path/f'{name}.png'))
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
        data=subagent_summary();data['_collection']={'errors':errors.copy()};w.set_content(data,'수집 오류');m=w.content_model
        values=[''.join(item[0]) if isinstance(item[0],list) else item[0] for item in m.detail_items()]
        for expected in ('기록 손상, 2건','기록 접근 실패','기록 읽기 실패','기록 수집 지연'):
            assert expected not in values and m.status_detail().count(expected)==1
        assert '수집 오류' not in values and m.status_text()=='수집 오류 +4'
        visible='\n'.join(values)+m.detailBody.state['accessible']+m.status_detail()
        for diagnostic in ('Traceback','C:\\private','/private/backend.py','PermissionError','sqlite3','RuntimeError'):assert diagnostic not in visible
        assert data['_collection']['errors']==errors and m.data['_collection']['errors']==errors
    finally:w.close();app.processEvents()
