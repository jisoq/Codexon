import copy
from types import SimpleNamespace
import pytest
from cachemonitor.performance_trends import PerformanceTrends,assessment

def row(i,**changes):
    return dict(dict(ts=1000+i,home='test',sid='s',key=str(i),turn='t',model='m',service_tier='Standard',effort='high',
                     cost=1.,input=100,cached=90,output=100,reasoning=50,duration=1.,completion_latency_ms=1000,output_speed=100),**changes)

def engine(rows):
    return SimpleNamespace(revision=1,sessions={'s':{'prepared':{'history':rows}}})

def query(**changes):return dict(dict(now=2000,plot_width=64),**changes)

def panel(view,key):return next(p for p in view['panels'] if p['key']==key)

def test_causal_outliers_and_zero_deviation():
    assert assessment(100,[1]*9)['status']=='비교 기록 부족'
    assert assessment(2,[1]*10)['direction']==1
    assert assessment(.1,[1]*10)['direction']==-1
    assert assessment(95,[90]*10,cache=True)['direction']==0
    assert assessment(60,[90]*10,cache=True)['direction']==-1
    assert assessment(100,[1]*10,complete=False)['direction']==0

def test_metrics_equal_session_weight_speed_and_missing():
    rows=[row(i) for i in range(12)]+[row(15,sid='other',input=10,cached=1,output=200,completion_latency_ms=2000,output_speed=100)]
    rows.append(row(16,cost=None,reasoning=None,output_speed=None,duration=None,input=None,cached=None))
    data=PerformanceTrends().query(engine(rows),query())
    cache=panel(data,'session_cache')
    assert cache['lines'][0]['points'][0]['value']==50
    assert panel(data,'output_speed')['lines'][0]['points'][0]['value']==100
    assert panel(data,'cost')['n']==13 and panel(data,'cost')['N']==14
    assert panel(data,'reasoning:high')['n']==13
    assert 'cost_total' not in {p['key'] for p in data['panels']}
    assert panel(data,'cost')['lines'][0]['points'][0]['coverage']==(13,14)

def test_bounded_outliers_and_corrections():
    rows=[row(i,ts=1000+i*.001) for i in range(2000)]
    rows[100]['cost']=100;rows[102]['cost']=.01
    e=engine(rows);projector=PerformanceTrends();data=projector.query(e,query())
    cost=panel(data,'cost');assert len(cost['points'])<=64*6
    assert {p['direction'] for p in cost['points'] if p.get('count')}=={-1,1}
    assert sum(p['count'] for p in cost['points'] if p.get('count'))>=2
    rows[100]['cost']=1;e.revision+=1
    assert panel(projector.query(e,query()),'cost')['high']==1

def test_maintenance_and_unknown_records():
    data=PerformanceTrends().query(engine([row(0,purpose='maintenance'),row(1,model='',service_tier='미확인'),row(2,model='codex-auto-review')]),query())
    assert data['calls']==1 and data['models']==['미확인']

def test_same_timestamp_never_supplies_baseline_and_zoom_excludes_future():
    rows=[row(i,ts=1000,cost=1) for i in range(12)]+[row(12,ts=1000,cost=10),row(13,ts=1050,cost=100)]
    data=PerformanceTrends().query(engine(rows),query(time_range=[999,1001]))
    cost=panel(data,'cost')
    assert cost['high']==10 and not any(p['direction'] for p in cost['points'])

def test_partial_boundary_bucket_is_not_compared_to_full_intervals():
    rows=[row(i,ts=600+i*60) for i in range(30)]
    data=PerformanceTrends().query(engine(rows),query(now=2500,time_range=[1801,2400],plot_width=1024))
    # The first populated bucket here is complete; an edge call creates a partial one.
    rows.append(row(99,ts=1805,cost=100))
    data=PerformanceTrends().query(engine(rows),query(now=2500,time_range=[1801,2400],plot_width=1024))
    partial=min(panel(data,'count')['points'],key=lambda p:p['ts'])
    assert partial['direction']==0 and partial['status']=='진행 중인 구간'


def test_interval_comparison_matches_model_mode_and_completed_adjacent_period():
    from datetime import datetime
    base=datetime(2026,1,1).timestamp();day=86400
    rows=[row(b*2+j,ts=base+b*day+j,service_tier=mode,cost=(b+1)*(3 if mode=='Fast' else 1))
          for b in range(12) for j,mode in enumerate(('Standard','Fast'))]
    data=PerformanceTrends().query(engine(rows),query(now=base+12*day,plot_width=1024))
    for line in panel(data,'cost')['lines']:
        points=line['points'];scale=3 if line['service_tier']=='Fast' else 1
        assert points[0]['baseline'] is None
        assert points[1]['baseline']==scale and points[1]['delta']==scale
        assert points[1]['coverage']==(1,1) and points[1]['previous_end']==points[1]['start']
    rows=[r for r in rows if not base+day<=r['ts']<base+2*day]
    data=PerformanceTrends().query(engine(rows),query(now=base+11*day+30,plot_width=1024))
    for line in panel(data,'cost')['lines']:
        assert line['points'][1]['baseline'] is None
        assert line['points'][-1]['baseline'] is None and not line['points'][-1]['complete']


def test_million_calls_keep_anomalies_and_bounded_frames(tmp_path):
    import json,time
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QSettings
    from cachemonitor.performance_panel import PerformancePanel
    from cachemonitor.quick_qa import mount,dispose,render_plot
    count=1_000_000
    rows=[dict(ts=1800000000+i,home='synthetic',sid='s',key=str(i),model='m',service_tier='Standard',effort='high',cost=1.) for i in range(count)]
    rows[543210]['cost']=100
    began=time.perf_counter();data=PerformanceTrends().query(engine(rows),query(now=1801000000,plot_width=768));elapsed=time.perf_counter()-began
    points=panel(data,'cost')['points']
    assert any(p.get('count') and p['record']['key']=='543210' for p in points)
    assert len(points)<=768*6
    app=QApplication.instance() or QApplication([])
    owner=SimpleNamespace(settings=QSettings(str(tmp_path/'million.ini'),QSettings.IniFormat),render=lambda:None,open_record=lambda r:None)
    ui=PerformancePanel(owner);ui.apply(data);host=mount(ui,1120,800)
    try:
        chart=ui.plots['cost'];render_plot(host,chart);frames=[];selection=[]
        for i in range(25):
            point=chart.rows[i*len(chart.rows)//25];xy=chart.xy(point)
            began=time.perf_counter();chart.nearest(xy.x(),xy.y());selection.append((time.perf_counter()-began)*1000)
            began=time.perf_counter();chart.update();app.processEvents();host.quick.grabFramebuffer();frames.append((time.perf_counter()-began)*1000)
        report=dict(calls=count,points=len(points),preparation_seconds=elapsed,selection_p95_ms=sorted(selection)[23],frame_p95_ms=sorted(frames)[23])
        (tmp_path/'benchmark.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        assert report['selection_p95_ms']<50,report
        assert report['frame_p95_ms']<100,report
        assert not host.qml_errors
    finally:dispose(host)

def test_qt_panel_hover_analysis_and_passive_click(tmp_path):
    from PySide6.QtCore import QSettings,Qt
    from PySide6.QtWidgets import QApplication
    from cachemonitor.performance_panel import PerformancePanel
    from cachemonitor.quick_qa import mount,dispose,render_plot,click
    app=QApplication.instance() or QApplication([])
    owner=SimpleNamespace(settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat),render=lambda:None,open_record=lambda r:None)
    ui=PerformancePanel(owner);data=PerformanceTrends().query(engine([row(i) for i in range(20)]),query())
    ui.apply(data);host=mount(ui,1000,800)
    try:
        chart=ui.plots['cost'];render_plot(host,chart);before=chart.bounds
        ui.highlight('m',False,True);app.processEvents();host.quick.grabFramebuffer()
        assert chart.bounds==before
        item=render_plot(host,chart);point=chart.rows[0];xy=chart.xy(point)
        assert ui.inspector.isVisible()
        item.showTip(xy.x(),xy.y())
        assert item.tip=='' and ui.inspected==(point,'cost')
        assert ui.details.sample_values['eligible'].text()=='20'
        assert ui.details.sample_notes['eligible'].text()=='전체 20'
        assert ui.details.median.text()=='$1.00'
        assert ui.details.total_rows[0][2].text()=='$20.00'
        before=ui.inspected
        click(host,item,xy.x(),xy.y());chart.key(Qt.Key_Return);chart.key(Qt.Key_Space)
        assert ui.inspected==before and ui.time_range is None
        assert not host.qml_errors
        assert host.quick.grabFramebuffer().save(str(tmp_path/'performance.png'))
    finally:dispose(host)

def test_dashboard_tab_worker_contract_and_navigation(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.core import Session
    from cachemonitor.quick_qa import dispose
    app=QApplication.instance() or QApplication([])
    app.setProperty('cachemonitorDisableShellIntegration',True)
    w=Dashboard([],start_worker=False,live_limits=False,settings=QSettings(str(tmp_path/'dashboard.ini'),QSettings.IniFormat))
    try:
        session=Session('s','synthetic',title='Synthetic',cwd='C:/sample')
        session.add_usage(1800000000,'r',dict(input_tokens=100,cached_input_tokens=90,cache_write_input_tokens=0,output_tokens=50,reasoning_output_tokens=10),'gpt-6-astra','t','high','Standard')
        w.receive(dict(ts=1800001000,sessions=[session.view(1800001000)],homes=['synthetic'],errors=[],unassigned=[]))
        assert w.navigation_pages==(0,1,5,2,3)
        w.nav.setCurrentRow(2)
        assert w.pages.currentIndex()==5 and not w.common_filters.isVisible()
        assert w.performance_panel.plots['cost'].rows
        q=w.query();result=w.engine.page_query(q)
        w.request_id=123;w.pending_automatic=False;w.pending_query=q
        w.analysis_ready(dict(id=123,logical='performance',result=result,valid_until=result['valid_until']))
        assert q['granularity']=='day'
        w.performance_panel.choose_granularity('month')
        assert w.query()['granularity']=='month'
        assert w.engine.page_query(w.query())['performance']['granularity']=='month'
        w.change_page(4);assert w.current_page==4
    finally:dispose(w)


@pytest.mark.parametrize('shell',[False,True])
def test_dense_workspace_units_and_hover_analysis(tmp_path,shell):
    import math
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from cachemonitor.performance_panel import PerformancePanel
    from cachemonitor.quick_qa import mount,dispose,render_plot,control,click
    models=['gpt-6-astra','gpt-6.1-sol','gpt-5.6-sol']
    rows=[]
    for i in range(18000):
        model=models[i%3];speed=40+12*math.sin(i/160)+i%9
        if i%61<4:speed*=3
        if i%2:speed*=2
        rows.append(row(i,ts=1789000000+i*140,model=model,service_tier='Fast' if i%2 else 'Standard',
            effort='high' if i%5 else 'medium',sid='session-'+str(i//70),cost=(.04+.02*(i%13)+(.8 if i%103==0 else 0))*(2.5 if i%2 else 1),
            input=40000+i%6000,cached=30000+i%5000,output=2000,reasoning=1200+(i%17)*30,
            output_speed=speed,completion_latency_ms=2000000/speed,duration=2000/speed))
    projector=PerformanceTrends();source=engine(rows)
    data=projector.query(source,query(now=rows[-1]['ts']+100,plot_width=900))
    app=QApplication.instance() or QApplication([])
    calls=[];owner=SimpleNamespace(settings=QSettings(str(tmp_path/'dense.ini'),QSettings.IniFormat),open_record=calls.append)
    if shell:
        from cachemonitor.dashboard import Dashboard
        app.setProperty('cachemonitorDisableShellIntegration',True)
        owner=Dashboard([],start_worker=False,live_limits=False,settings=owner.settings)
        owner.resize(1440,940);owner.show();owner.change_page(5);QTest.qWait(200)
        owner.open_record=calls.append;ui=owner.performance_panel;host=owner
    else:ui=PerformancePanel(owner)
    def refresh():
        ui.apply(projector.query(source,query(now=rows[-1]['ts']+100,plot_width=900,time_range=ui.time_range,
            granularity=ui.granularity)))
    owner.render=refresh;ui.apply(data)
    if not shell:host=mount(ui,1440,940)
    try:
        ui.model_choice.choose(ui.model_choice.findData(models[0]));QTest.qWait(60)
        assert [m for m in ui.available if ui.models.get(m,True)]==[models[0]]
        click(host,control(host,ui.add_button));ui.search.setText('6.1');QTest.qWait(50)
        assert len(ui.search_results._nodes)==1
        click(host,control(host,ui.search_results._nodes[0]));assert ui.models[models[1]]
        click(host,control(host,ui.mode_buttons['Fast']));assert ui.modes=={'Standard':False,'Fast':True}
        ui.solo(models[0]);ui.choose_mode('all')
        plot=ui.plots['output_speed'];render_plot(host,plot)
        assert ui.legend_entries==[(models[0],'Standard'),(models[0],'Fast')]
        assert abs(plot.y(plot.bounds[1])-plot.y(plot.bounds[0]))>=290
        from cachemonitor.theme import shared_theme
        assert shared_theme().model_mode_color(models[0],'Standard')!=shared_theme().model_mode_color(models[0],'Fast')
        assert plot.bounds[1]<plot.data['high']*.6
        assert all(p['kind']=='trend' for p in plot.rows)
        assert 'cost_total' not in ui.plots
        assert ui.labels['input'].text()=='평균 입력 토큰'
        trend=next(p for p in plot.rows if plot.selected_style(p) and p['baseline'] is not None and p['delta'])
        item=render_plot(host,plot);xy=plot.xy(trend);item.showTip(xy.x(),xy.y());QTest.qWait(40)
        assert ui.inspected[0]['value']==trend['value'] and item.tip==''
        assert ui.details.range_plot.stats==trend['distribution']
        assert ui.details.total_rows[0][1].text()=='총 출력 토큰'
        assert host.quick.grabFramebuffer().save(str(tmp_path/'hover-analysis.png'))
        assert len(plot.strip)<=160 and any(c['count']>1 for c in plot.strip)
        cell=max(plot.strip,key=lambda c:c['count']);x=76+(cell['index']+.5)*(plot.width()-96)/80
        assert plot.tip_at(x,plot.strip_top()+(3 if cell['direction']>0 else 14))==''
        assert str(cell['count'])==ui.details.value.text()
        assert not calls
        for unit in ('week','month','day'):
            click(host,control(host,ui.granularity_buttons[unit]));QTest.qWait(50)
            assert ui.granularity==unit and projector.view['granularity']==unit
            assert ui.granularity_buttons[unit].isChecked()
            assert PerformancePanel(owner).granularity==unit
        restored=PerformancePanel(owner)
        assert restored.granularity=='day'
        host.resize(1000,760);render_plot(host,ui.plots['cost'])
        assert ui.inspector.isVisible()
        assert host.quick.grabFramebuffer().save(str(tmp_path/'daily-compact.png'))
        render_plot(host,ui.plots['input'])
        ui.chart_scroll.verticalPosition.setValue(max(0,ui.chart_scroll.verticalPosition.value()-30));QTest.qWait(40)
        assert host.quick.grabFramebuffer().save(str(tmp_path/'input-title.png'))
        assert not host.qml_errors
    finally:dispose(host)

def test_calendar_units_leap_months_and_cache_invalidation():
    from datetime import datetime
    stamp=lambda s:datetime.fromisoformat(s).timestamp()
    rows=[row(i,ts=stamp(t),cost=value) for i,(t,value) in enumerate([
        ('2024-01-31T12:00',1),('2024-02-01T12:00',3),('2024-02-29T12:00',5),('2024-03-01T12:00',7)])]
    source=engine(rows);projector=PerformanceTrends();now=stamp('2024-03-05')
    expected={'day':[1,3,5,7],'week':[2,6],'month':[1,4,7]}
    for unit,values in expected.items():
        data=projector.query(source,query(now=now,granularity=unit))
        points=panel(data,'cost')['lines'][0]['points']
        assert [p['value'] for p in points]==values
        assert data['granularity']==unit
        wider=projector.query(source,query(now=now,granularity=unit,plot_width=1024))
        assert panel(wider,'cost')['lines']==panel(data,'cost')['lines']
        if unit=='month':
            february=points[1]
            assert february['start']==stamp('2024-02-01')
            assert february['end']==stamp('2024-03-01')
            assert february['end']-february['start']==29*86400
    # Monday midnight starts a different week.
    rows=[row(0,ts=stamp('2026-10-04T23:59:59')),row(1,ts=stamp('2026-10-05T00:00:00'))]
    data=PerformanceTrends().query(engine(rows),query(now=stamp('2026-10-06'),granularity='week'))
    points=panel(data,'count')['lines'][0]['points']
    assert [p['value'] for p in points]==[1,1]
    assert points[0]['end']==points[1]['start']==stamp('2026-10-05')


def test_interval_analysis_distribution_totals_missing_and_weighted_speed():
    rows=[row(i,cost=float(i)) for i in range(10)]+[row(10,cost=None)]
    point=panel(PerformanceTrends().query(engine(rows),query()),'cost')['lines'][0]['points'][0]
    assert point['coverage']==(10,11)
    assert point['distribution']==dict(median=4.5,p10=.9,p90=8.1,minimum=0.,maximum=9.)
    assert point['totals']=={'sum':45.} and point['sessions']==1
    rows=[row(0,output=100,completion_latency_ms=1000,output_speed=100),
          row(1,output=200,completion_latency_ms=4000,output_speed=50),row(2,output_speed=None)]
    point=panel(PerformanceTrends().query(engine(rows),query()),'output_speed')['lines'][0]['points'][0]
    assert point['value']==60 and point['distribution']['median']==75
    assert point['distribution']['p10'] is None and point['coverage']==(2,3)
    assert point['totals']=={'output':300,'seconds':5}
    # Zooming clips the first day's population without admitting earlier calls.
    from datetime import datetime
    start=datetime(2026,10,1).timestamp()
    rows=[row(i,ts=start+i*3600,cost=i) for i in range(48)]
    point=panel(PerformanceTrends().query(engine(rows),query(now=start+48*3600,
        time_range=[start+12*3600,start+25*3600],granularity='day')),'cost')['lines'][0]['points'][0]
    assert point['coverage']==(12,12) and point['totals']['sum']==sum(range(12,24))
    assert not point['complete'] and point['baseline'] is None
