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


def test_english_overlay_translates_visible_text_before_detail_layout(monkeypatch):
    import re
    from PySide6.QtGui import QFontMetrics
    from cachemonitor.i18n import set_language, tr, Verbatim
    from cachemonitor.overlay_view import Drawing, font
    app=QApplication.instance() or QApplication([])
    data=subagent_summary();data['title']='Sample task'
    data['subagent_finance']['coverage_gap']=True
    data['subagent_finance']['models'][0]['model']='gpt-shared-long-model-name-'+'variant-'*6
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
        w.set_content(data);w.set_layout(detail=True);m=w.content_model;m.set_detail_group('model')
        for unit in ('tokens','usd'):
            for tab in ('latest','history'):
                m.monitor_action('unit-'+unit);m.monitor_action('tab-'+tab)
                image=QImage(m.panel_width(),m.panel_height(),QImage.Format_ARGB32_Premultiplied)
                image.fill(0);p=QPainter(image);m.paint(p);p.end()
        detail=[line for item in m._detail_items for line in (item[0] if isinstance(item[0],list) else [item[0]])]
        assert not [value for value in drawn+detail if re.search('[가-힣]',value)]
        assert 'Output speed' in drawn and 'Current' in drawn and 'Recent' in drawn
        assert tr(m.state['detailTitle'])=='Sub details' and 'gpt-shared-long-model-name' in ''.join(detail)
        assert data==original
        # Full sentences must be translated before line splitting, not fragment by fragment.
        formulas=[item for item in m._detail_items if isinstance(item[0],list)]
        for item in formulas:
            metrics=QFontMetrics(font(m.appearance.family,item[5],item[7]))
            assert all(metrics.horizontalAdvance(line)<=item[3] for line in item[0])
        assert tr(Verbatim('세션 2회 40.56초'))=='세션 2회 40.56초'
    finally:
        w.close();set_language('ko');app.processEvents()
    assert tr('40.56초, 2회')=='40.56초, 2회'


def test_subagent_labels_show_own_call_evidence_and_elide_names():
    from cachemonitor.overlay_view import OverlayContent
    app=QApplication.instance() or QApplication([])
    m=OverlayContent();data=subagent_summary()
    child=data['subagent_finance']['agents'][0]
    child.update(model='gpt-6.1-sol',effort='high',response_model='gpt-6.1-sol',model_state='일치',model_mismatch=False)
    m.set_content(data);m.set_layout(detail=True)
    height=m.state['detailBodyHeight']
    changed=copy.deepcopy(data)
    full_name='Analyzer capacity design with a deliberately long task name'
    changed['subagent_finance']['agents'][0]['title']=full_name
    m.set_content(changed)
    assert m.state['detailBodyHeight']==height
    label=next(item for item in m._detail_items if item[0]==full_name)
    assert label[8]['kind']=='single-line'
    action=next(item for item in m.state['detailActions'] if full_name in item['label'])
    assert full_name in action['tooltip'] and 'gpt-6.1-sol, high (일치)' in action['tooltip']
    for state,mismatch,response,expected in (
            ('일치',False,'gpt-6.1-sol','일치'),('모델명 불일치',True,'gpt-6-luna','불일치'),
            ('관측 충돌',False,'gpt-6-luna','미확인'),('관측 누락',False,None,'미확인'),('미확인',False,None,'미확인')):
        value=dict(child,model_state=state,model_mismatch=mismatch,response_model=response)
        assert m.subagent_call_label(value)==f'gpt-6.1-sol, high ({expected})'


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
        m.select(m.call_id(m.rows()[0]))
        assert m.selected_id!=fixed and m._detail_items==body
        w.set_content(None,'기록 확인 중');assert m.selected_id is None and m.rows()==[] and m.selected()=={}
        assert m.notice_card()[0]=='기록 확인 중' and not m.monitor_links()
        assert all(value!='—' for value in w.lines())
    finally:w.close();app.processEvents()


def test_pricing_ratio_requires_an_unpriced_work_call():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=subagent_summary()
    try:
        data['subagent_finance'].update(coverage_gap=True,missing=0)
        w.set_content(data);w.set_layout(detail=True)
        assert not any('산정 ' in str(item[0]) for item in w.content_model._detail_items)
        data=copy.deepcopy(data);sub=data['subagent_finance'];sub.update(priced=sub['calls']-1,missing=1)
        w.set_content(data)
        assert any('산정 ' in str(item[0]) for item in w.content_model._detail_items)
        assert w.content_model.state['detailSubPartial']
    finally:w.close();app.processEvents()


def test_subagent_groups_share_one_population_and_fixed_header():
    from cachemonitor.overlay_chrome import OverlayDetail
    from cachemonitor.overlay_view import font
    from PySide6.QtGui import QFontMetrics
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();pane=None
    try:
        data=subagent_summary();w.set_content(data);w.set_layout(detail=True);m=w.content_model
        assert m.has_subagents() and m.detail_data()==data['subagent_finance']
        assert m.detail_group=='sub'
        m.set_detail_group('model')
        assert len(m.detail_groups())==2
        assert math.isclose(sum(row['cost'] for row in m.detail_groups()),data['subagent_finance']['cost'],abs_tol=1e-12)
        assert not any(row['model']=='gpt-6-astra' for row in m.detail_groups())
        key=m.detail_groups()[0]['key'];m.toggle_detail_group(key)
        assert key in m.expanded_groups
        before=copy.deepcopy(m._detail_items);m.select(m.call_id(m.rows()[0]))
        assert m._detail_items==before
        assert not any('토큰' in str(item[0]) for item in m._detail_items)
        pane=OverlayDetail(m);pane.resize(240,360);pane.show();app.processEvents()
        def item(name):
            pending=[pane.quick.rootObject()]
            while pending:
                current=pending.pop()
                if current.objectName()==name:return current
                pending.extend(current.childItems())
            raise AssertionError(name)
        button=item('detailMode-sub');point=button.mapToScene(button.boundingRect().center()).toPoint()
        QTest.mouseClick(pane.quick,Qt.LeftButton,Qt.NoModifier,point);app.processEvents()
        assert m.detail_group=='sub' and item('detailMode-sub').property('checked')
        assert {row['id'] for row in m.detail_groups()}=={'child','other'}
        assert math.isclose(sum(row['cost'] for row in m.detail_groups()),data['subagent_finance']['cost'],abs_tol=1e-12)
        group=next(row for row in m.detail_groups() if row['id']=='child');m.toggle_detail_group(group['key'])
        assert len(group['model_breakdown'])==2
        assert m.detail_data()['cost']!=data['cost']
        fixed=item('detailCostHeader').y();height=pane.height()
        scroll=item('detailScroll');scroll.setProperty('contentY',40);app.processEvents()
        assert item('detailCostHeader').y()==fixed and pane.height()==height
        assert item('detailScroll').property('contentY')>0
        large=copy.deepcopy(data);large['own_cost']=123456789012345.67
        large['subagent_finance']['cost']=large['own_cost']
        large['subagent_finance']['agents'][0]['cost']=large['own_cost']
        w.set_content(large);m.set_detail_group('sub')
        group=m.detail_groups()[0]
        if group['key'] not in m.expanded_groups:m.toggle_detail_group(group['key'])
        image=QImage(240,542,QImage.Format_ARGB32_Premultiplied);image.fill(0)
        painter=QPainter(image)
        try:m.detailBody.paint(painter)
        finally:painter.end()
        for row in m._detail_items:
            if row[8].get('kind')=='metric':assert isinstance(row[0],str)
            metrics=QFontMetrics(font(m.appearance.family,row[5],row[7]))
            lines=row[0] if isinstance(row[0],list) else [row[0]]
            if row[8].get('kind')=='single-line':lines=[metrics.elidedText(row[0],Qt.ElideRight,int(row[3]))]
            assert all(metrics.horizontalAdvance(line)<=row[3] for line in lines)
        for value in (123456789012345.67,.00001,.0001,None):
            resized=copy.deepcopy(data);resized['own_cost']=value;resized['subagent_finance']['cost']=value
            w.set_content(resized);app.processEvents()
            for prefix,name in (('detailMain','main'),('detailSub','sub')):
                amount=item('detailCostAmount-'+name)
                assert amount.property('implicitWidth')<=amount.width()
                metrics=QFontMetrics(font(m.appearance.family,m.state[prefix+'CostSize'],600))
                assert metrics.horizontalAdvance(m.state[prefix+'Cost'])<=(m.detail_width-12)/2
                assert m.state[prefix+'Exact']!='' and amount.property('text')==m.state[prefix+'Cost']
        w.set_content(copy.deepcopy(data));assert m.detail_group=='sub'
        assert key in m.expanded_groups and group['key'] in m.expanded_groups
        removed=copy.deepcopy(data);removed['subagent_finance']['models']=[]
        removed['subagent_finance']['agents']=[row for row in removed['subagent_finance']['agents'] if row['id']!='child']
        w.set_content(removed)
        assert key not in m.expanded_groups and group['key'] not in m.expanded_groups
        w.set_content(summary());assert not m.has_subagents() and not m.expanded_groups and not m.detail_data()
    finally:
        if pane:pane.close()
        w.close();app.processEvents()


def test_token_quantities_use_k_m_b_without_unit_word():
    from cachemonitor.overlay_view import amount
    assert [amount(n) for n in (999,1_250,1_250_000,1_250_000_000)]==['999','1.2K','1.2M','1.2B']


def test_detail_expansion_preserves_monitor_pixels_and_font_scale():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        w.set_content(subagent_summary());m=w.content_model
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
            w.set_content(subagent_summary(),appearance=replace(default_appearance(True),font_size=size));s=max(1,size/14)
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
        data=subagent_summary();data['_collection']={'errors':errors.copy()};w.set_content(data,'수집 오류');m=w.content_model
        values=[''.join(item[0]) if isinstance(item[0],list) else item[0] for item in m.detail_items()]
        for expected in ('기록 손상, 2건','기록 접근 실패','기록 읽기 실패','기록 수집 지연'):assert values.count(expected)==1
        assert '수집 오류' not in values and m.status_text()=='수집 오류 +4'
        visible='\n'.join(values)+m.detailBody.state['accessible']
        for diagnostic in ('Traceback','C:\\private','/private/backend.py','PermissionError','sqlite3','RuntimeError'):assert diagnostic not in visible
        assert data['_collection']['errors']==errors and m.data['_collection']['errors']==errors
    finally:w.close();app.processEvents()
