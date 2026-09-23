import copy
from dataclasses import replace
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QFontMetrics
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from cachemonitor.core import Session
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.overlay_data import OverlaySummaries
from cachemonitor.overlay import SessionOverlay
from cachemonitor.overlay_appearance import default_appearance
from cachemonitor.overlay_view import Drawing, money, amount, percent, palette, contrast, font


def summary(misses=0, count=12):
    s=Session('clean','home',title='CacheMonitor 세션 오버레이 디자인 구현')
    for i in range(count):
        s.add_usage(1789884000+i,str(i),dict(input_tokens=100000,cached_input_tokens=0 if i>=count-misses else 90000,
                    cache_write_input_tokens=0,output_tokens=1000,reasoning_output_tokens=400),
                    'gpt-6-astra','turn','high','Standard')
    engine=AnalysisEngine();engine.ingest([s.view(1789884100)])
    return OverlaySummaries().collect(engine)[0]


def test_monitor_fixed_coordinates_and_only_conditional_rows_change_height():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=summary()
    try:
        w.set_content(data);m=w.content_model
        assert (w.width(),w.height())==(380,578)
        assert m.layout()['tokens']==370 and m.layout()['status']==546
        assert [b['key'] for b in m.visible_bars()]==['uncached','written','output','reasoning']
        larger=copy.deepcopy(data);larger.update(latest_cost=987654321,cost=987654321,mean_cost=987654,calls=987654321)
        w.set_content(larger);assert w.height()==578
        w.set_content(data,'수집 오류');assert w.height()==594
        unknown=copy.deepcopy(data);unknown['token_composition']['bars'].append(dict(key='unknown',tokens=10,share=.0001))
        w.set_content(unknown);assert w.height()==602 and m.layout()['status']==570
        w.set_content(unknown,'수집 오류');assert w.height()==618
        w.set_layout(reduced=True);assert w.height()==414 and m.layout()['status']==366
        w.set_content(data);assert w.height()==398
    finally:w.close();app.processEvents()


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
        assert m.panel_width()==620 and m.panel_height()==578 and m.monitor_x==240
        normal_crop=normal.copy(16,48,348,496);detail_crop=detail.copy(256,48,348,496)
        a=bytes(normal_crop.constBits());b=bytes(detail_crop.constBits())
        # Qt's translated antialias coverage may differ by one channel level.
        assert max(abs(x-y) for x,y in zip(a,b))<=1
        for size in (10,14,21):
            w.set_content(summary(),appearance=replace(default_appearance(True),font_size=size));s=max(1,size/14)
            assert m.panel_width()==round(620*s) and m.panel_height()==round(578*s)
    finally:w.close();app.processEvents()


def test_formatting_distinguishes_zero_unknown_tiny_and_large():
    assert [money(v) for v in (None,0,.00001,.0001,.42,1,12345,2345678)]==['—','$0.00','<$0.0001','$0.0001','$0.42','$1.00','$12.35K','$2.35M']
    assert money(2345678,False)=='$2,345,678.00'
    assert percent(None)=='—' and percent(0)=='0.0%' and percent(.01)=='<0.1%'
    assert amount(987654321,True)=='987,654,321'


def test_monochrome_graph_marks_and_shared_call_centers():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();m=w.content_model
    rows=[dict(id='a',cache_rate=0,cost=0),dict(id='b',cache_rate=None,cost=None),dict(id='c',cache_rate=100,cost=2)]
    image=QImage(380,100,QImage.Format_ARGB32_Premultiplied);p=QPainter(image);d=Drawing(p,m);lines=[];rects=[];dots=[]
    d.line=lambda *a,**k:lines.append((a,k));d.rect=lambda *a,**k:rects.append((a,k));d.dot=lambda *a,**k:dots.append((a,k))
    try:
        d.graphs(rows,16,0,348,12)
        step=312/12;centers=[52+(9+i+.5)*step for i in range(3)]
        assert any(a[0]==centers[0]-min(16,step*.55)/2 and a[1]==52 for a,k in lines)
        assert any(len(a)>6 and a[6] is True for a,k in lines)
        assert any(abs(a[0]+a[2]/2-centers[2])<.01 for a,k in rects)
        assert any(a[0]==centers[2] and a[1]==80 and a[2]==centers[2] for a,k in lines)
    finally:p.end();w.close();app.processEvents()


def test_default_text_contrast_and_category_palette_remain_distinct():
    for dark in (False,True):
        colors=palette(default_appearance(dark))
        for key in ('ink','secondary','meta'):assert contrast(colors[key],colors['surface'])>=4.5
        assert len({colors[key].name() for key in ('cached','uncached','written','output','reasoning','unknown')})==6


def test_long_model_comparison_wraps_without_losing_distinct_names():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay();data=summary()
    first='gpt-very-long-shared-prefix-2026-09-special-call-model'
    second='gpt-very-long-shared-prefix-2026-09-special-response-model'
    data.update(model=first,response_model=second,model_mismatch=True)
    data['recent'][-1].update(model=first,requested_model=first,response_model=second,model_mismatch=True,
                            model_match='불일치',model_alert_confirmed=True,response_status='completed')
    try:
        w.set_content(data);m=w.content_model
        expected=f'모델 불일치 (요청:{first}, 응답:{second})'
        assert m.context()[0]==expected and ''.join(m.context_rows())==expected
        assert len(m.context_rows())>1
        metrics=QFontMetrics(font(m.appearance.family,12))
        assert all(metrics.horizontalAdvance(line)<=348 for line in m.context_rows())
        assert m.layout()['cache']>=48+len(m.context_rows())*18+18
        assert m.layout()['height']>=m.layout()['status']+32
        detail=[''.join(r[0]) if isinstance(r[0],list) else r[0] for r in m.detail_items()]
        assert expected in detail
        assert not any(value in detail for value in ('요청 모델','응답 모델','모델명 불일치'))
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


def test_record_checking_empty_history_and_remote_never_retain_old_numbers():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        w.set_content(summary());m=w.content_model;m.detailGraph.key(Qt.Key_Home)
        w.set_content(summary(),'기록 확인 중')
        assert m.rows()==[] and m.selected()=={} and all(v=='—' for v in w.lines()[3:7])
        assert '호출 기록 없음' not in m.status_text()
        w.set_content(summary());w.set_content(summary(count=0))
        assert m.selected()=={} and m.selected_id is None
        w.set_content(None,'원격 작업 · 로컬 기록 없음')
        values=[''.join(r[0]) if isinstance(r[0],list) else r[0] for r in m.detail_items()]
        assert '0회' not in values and '$0.00' not in values
    finally:w.close();app.processEvents()


def test_fixed_money_cells_do_not_shrink_fonts_or_clip_large_values():
    from cachemonitor.overlay_view import fitted_money
    app=QApplication.instance() or QApplication([])
    from cachemonitor.fonts import load_bundled_fonts
    load_bundled_fonts()
    for width,size in ((124,24),(98,20),(136,18),(112,18)):
        metrics=QFontMetrics(font('Pretendard JP',size,600))
        for value in (None,0,.00001,.9999,9999,999999,1234567890,123456789012):
            assert metrics.horizontalAdvance(fitted_money(value,width,size,'Pretendard JP'))<=width


def test_many_call_counts_fit_fixed_slot_and_history_does_not_make_large_texture():
    from cachemonitor.overlay_view import call_count, metric_width
    from cachemonitor.fonts import load_bundled_fonts
    app=QApplication.instance() or QApplication([]);load_bundled_fonts()
    metrics=QFontMetrics(font('Pretendard JP',18,600))
    for total in (24,1000,12345,999999,12345678):
        text=call_count(dict(calls=total,priced=total-2,missing=2),'Pretendard JP')
        assert metric_width(text,'Pretendard JP',18)<=76
    w=SessionOverlay()
    try:
        data=summary();data['cache_misses']['events']=[dict(ts=1789884000+i,input=12345) for i in range(2000)]
        w.set_content(data);m=w.content_model
        assert m.state['detailBodyHeight']>32000
        m.detailBody.setScrollOffset(32000);assert m.detailBody.scroll_offset==32000
    finally:w.close();app.processEvents()


def test_dominant_cache_uses_numeric_summary_and_non_cache_bars_only():
    from cachemonitor.overlay_data import token_composition
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        data=summary();data['token_composition']=token_composition({'history':[
            dict(input=999000,cached=998000,written=0,output=1000,reasoning=800)]})
        w.set_content(data);m=w.content_model
        image=QImage(380,560,QImage.Format_ARGB32_Premultiplied);image.fill(0)
        p=QPainter(image);d=Drawing(p,m);rectangles=[];texts=[]
        d.rect=lambda *args,**kwargs:rectangles.append((args,kwargs))
        d.text=lambda value,*args,**kwargs:texts.append((value,args,kwargs))
        try:d.tokens(16,352)
        finally:p.end()
        tracks=[args for args,_ in rectangles if args[4]=='track']
        assert len(tracks)==2 and all(args[2]==166 and args[3]==6 for args in tracks)
        bars={args[4]:args[2] for args,_ in rectangles if args[4]!='track'}
        assert abs(bars['cached']-166*998000/999000)<.0001
        assert bars['output']==33.2 and bars['reasoning']==132.8
        assert {'입력 구성','출력 구성','998.0K'}<=set(v for v,_,_ in texts)
        assert m.composition_height()==164 and m.layout()['status']==546
    finally:w.close();app.processEvents()


def test_compact_detail_moves_comparison_graph_without_shrinking_fonts_or_losing_zero_rows():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        w.set_content(summary());m=w.content_model;m.set_layout(reduced=True,detail=True)
        block=next(item for item in m._detail_items if len(item)>8 and item[8].get('kind')=='tokens')
        assert block[0]=='토큰 구성' and block[3]==208 and block[4]==256
        assert m.state['detailBodyHeight']>=block[2]+block[4]
        image=QImage(208,300,QImage.Format_ARGB32_Premultiplied);p=QPainter(image);d=Drawing(p,m)
        texts=[];rectangles=[]
        d.text=lambda value,*args,**kwargs:texts.append((value,args,kwargs))
        d.rect=lambda *args,**kwargs:rectangles.append((args,kwargs))
        try:d.tokens(0,0,208)
        finally:p.end()
        assert [value for value,args,kwargs in texts if value in ('일반 입력','캐시 쓰기','출력·추론 제외','추론')]==['캐시 쓰기','일반 입력','출력·추론 제외','추론']
        assert any(value=='0' for value,_,_ in texts)
        assert all(0<=args[0] and args[0]+args[2]<=208 for args,_ in rectangles)
        assert len([args for args,_ in rectangles if args[4]=='track'])==2
        assert any(args[4]=='cached' for args,_ in rectangles)
        assert all((args[4] if len(args)>4 else kwargs.get('size',11))>=11 for _,args,kwargs in texts)
        m.set_layout(reduced=True,detail=True,inline=True)
        inline=next(item for item in m._detail_items if len(item)>8 and item[8].get('kind')=='tokens')
        assert inline[3:5]==(348,164)
    finally:w.close();app.processEvents()


def test_detail_includes_actual_degradation_baseline_and_units_without_invented_causes():
    from cachemonitor.overlay_data import summarize_session
    from test_overlay_call_data import row,session
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        rows=[row(i) for i in range(40)]
        for i in (5,6):rows[i]['cached']=300
        w.set_content(summarize_session(session(rows)));m=w.content_model
        items=m.detail_items();texts=[''.join(item[0]) if isinstance(item[0],list) else item[0] for item in items]
        for expected in ('기준 평균 읽기','900 토큰','기준 적중률','90.0%','기준 호출','1, 2, 3, 4, 5호출','300 토큰','30.0%','회복 확인'):
            assert expected in texts
        assert '세션 호출당 평균' in texts and '세션 총 토큰' in texts
        for item in items:
            value=item[0]
            if not isinstance(value,list) and value in ('300 토큰','900 토큰'):assert item[8]['right']
            if isinstance(value,list) and any('토큰' in line for line in value):assert item[8]['right']
        assert all(item[8].get('kind')=='metric' for item in items if item[5]==20)
    finally:w.close();app.processEvents()


def test_unknown_identity_clears_data_and_current_errors_take_status_priority():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        data=summary();data['coverage_gap']=True;data['statuses']=[{'text':'수집 오류'}]
        w.set_content(data,'수집 지연');assert w.content_model.status_text().startswith('수집 오류 +')
        for note in ('원격 작업 · 로컬 기록 없음','현재 세션 식별 불가'):
            w.set_content(data,note);m=w.content_model
            assert m.data is None and m.rows()==[] and m.selected()=={}
            assert all(value=='—' for value in w.lines()[3:7])
    finally:w.close();app.processEvents()


def test_cache_text_and_category_graphics_adapt_together_to_surface():
    for dark in (False,True):
        appearance=default_appearance(dark);original=palette(appearance)
        colors=palette(replace(appearance,surface=original['cached'].name()))
        assert contrast(colors['cached_text'],colors['surface'])>=4.5
        assert contrast(colors['cached'],colors['surface'])>=3
        assert len({colors[key].name() for key in ('cached','uncached','written','output','reasoning','unknown')})==6


def test_first_new_call_in_open_empty_session_highlights_once_and_reduced_motion_suppresses_it():
    app=QApplication.instance() or QApplication([]);w=SessionOverlay()
    try:
        w.set_content(summary(count=0));m=w.content_model
        assert m.highlight_id is None
        w.set_content(summary(count=1));assert m.highlight_id==m.rows()[-1]['id']
        m.clear_highlight();w.set_content(summary(count=1));assert m.highlight_id is None
        m.put(reducedMotion=True);w.set_content(summary(count=2));assert m.highlight_id is None
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
