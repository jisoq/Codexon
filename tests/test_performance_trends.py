import copy
from types import SimpleNamespace
import pytest
from cachemonitor.performance_trends import PerformanceTrends,assessment


@pytest.fixture(autouse=True)
def restore_shell_integration():
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    previous=app.property('cachemonitorDisableShellIntegration')
    yield
    app.setProperty('cachemonitorDisableShellIntegration',previous)


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
    stats=cache['lines'][0]['points'][0]['distribution']
    assert (stats['q1'],stats['median'],stats['q3'])==(30,50,70)


def test_box_statistics_exact_quartiles_whiskers_and_bounded_outliers():
    from cachemonitor.performance_trends import box_statistics
    stats=box_statistics([1,2,3,4,5,100])
    assert (stats['q1'],stats['q3'],stats['whisker_low'],stats['whisker_high'])==(2.25,4.75,1,5)
    assert stats['outliers']==[100] and stats['outlier_count']==1
    assert box_statistics([])['q1'] is None
    for values in ([7],[7]*20):
        stats=box_statistics(values)
        assert stats['q1']==stats['q3']==stats['whisker_low']==stats['whisker_high']==7
        assert not stats['outliers'] and stats['outlier_count']==0
    stats=box_statistics([0]*10000+list(range(1,2001)))
    assert stats['outlier_count']==2000 and len(stats['outliers'])<=128
    assert stats['outliers'][0]==1 and stats['outliers'][-1]==2000


def test_rolling_representatives_valid_populations_and_common_windows():
    rows=[]
    for mode in ('Standard','Fast'):
        for i in range(10):
            rows.append(row(len(rows),ts=2800,service_tier=mode,cost=91 if i==9 else 1,
                duration=11 if i==9 else 1,output=100,completion_latency_ms=9000 if i==9 else 1000,
                output_speed=100/9 if i==9 else 100,sid='small' if i==9 else 'large',
                input=10 if i==9 else 100,cached=1 if i==9 else 90))
        rows.append(row(len(rows),ts=2800,service_tier=mode,cost=None,duration=None,output_speed=999,
                        completion_latency_ms=0,input=None,cached=None,sid='missing'))
    data=PerformanceTrends().query(engine(rows),query(now=4600,time_range=[1000,4600],granularity='auto',plot_width=768))
    def at(key):return next(p for p in panel(data,key)['lines'][0]['points'] if p['ts']==2800)
    assert data['window_seconds']==300
    assert at('cost')['value']==10 and at('cost')['distribution']['median']==1
    assert at('cost')['coverage']==(10,11) and at('cost')['band_valid'] and not at('cost')['outer_valid']
    assert at('duration')['value']==1 and at('duration')['mean']==2
    speed=at('output_speed')
    assert speed['value']==pytest.approx(1000/18) and speed['coverage']==(10,11)
    assert speed['totals']==dict(output=1000,seconds=18)
    assert speed['distribution']['median']==100
    assert at('session_cache')['value']==50 and at('session_cache')['coverage']==(2,3)
    assert not at('session_cache')['band_valid']
    for spec in data['panels']:
        a,b=spec['lines']
        assert [(p['ts'],p['start'],p['end']) for p in a['points']]==[(p['ts'],p['start'],p['end']) for p in b['points']]
        if spec['key']=='cost':assert a['coverage']==(10,11) and (spec['n'],spec['N'])==(20,22)
    assert any(p['value'] is None for p in panel(data,'cost')['lines'][0]['points'])


def test_rolling_zoom_count_boundaries_thresholds_and_empty_range():
    from cachemonitor.performance_trends import rolling_window
    assert [rolling_window(t) for t in (3600,21600,86400)]==[300,900,3600]
    rows=[row(i,ts=t) for i,t in enumerate((999,1000,1300,1300,4599,4600,4601))]
    projector=PerformanceTrends();source=engine(rows)
    data=projector.query(source,query(now=5000,time_range=[1000,4600],granularity='auto'))
    counts=panel(data,'count')['lines'][0]['points']
    assert [p['value'] for p in counts[:2]]==[1,2]
    assert counts[-1]['value']==2 and sum(p['value'] for p in counts)==5
    assert not any(p['band_valid'] for p in panel(data,'duration')['lines'][0]['points'])
    assert all(p['value'] is None for p in panel(data,'duration')['lines'][0]['points'])
    assert panel(data,'cost')['n']==5
    empty=projector.query(source,query(now=7000,time_range=[6000,7000],granularity='auto'))
    assert all(not p['lines'] for p in empty['panels']) and empty['models']==['m']
    dense=PerformanceTrends().query(engine([row(i,ts=2800,cost=i) for i in range(30)]),
        query(now=4600,time_range=[1000,4600],granularity='auto',plot_width=768))
    point=next(p for p in panel(dense,'cost')['lines'][0]['points'] if p['ts']==2800)
    assert point['outer_valid'] and point['distribution']['p10']==pytest.approx(2.9)
    assert point['distribution']['p90']==pytest.approx(26.1)


@pytest.mark.parametrize('language',['ko','en'])
def test_single_model_rolling_bands_selection_inspection_and_shared_axis(tmp_path,language):
    from datetime import datetime
    from PySide6.QtCore import QSettings,QObject,Qt
    from cachemonitor.performance_panel import PerformancePanel,SeriesLegend,plot_values
    from cachemonitor.quick_qa import mount,dispose,render_plot,control,click
    from cachemonitor.i18n import set_language
    set_language(language)
    base=datetime(2026,10,1).timestamp();rows=[]
    for minute in range(360):
        if 120<=minute<180:continue
        for model in ('m','other'):
            for mode in ('Standard','Fast'):
                for effort in ('low','high'):
                    value=[1,2,3,4,5,100][minute%6]
                    scale=(1000 if model=='other' else 2 if mode=='Fast' else 1)
                    rows.append(row(len(rows),ts=base+minute*60,model=model,service_tier=mode,
                                    effort=effort,cost=value*scale,reasoning=value*scale*(10 if effort=='high' else 1)))
    rows.append(row(len(rows),ts=base+3600,cost=None,reasoning=None))
    source=engine(rows);projector=PerformanceTrends()
    owner=SimpleNamespace(settings=QSettings(str(tmp_path/'bands.ini'),QSettings.IniFormat))
    ui=PerformancePanel(owner)
    def refresh():ui.apply(projector.query(source,query(now=base+6*3600,plot_width=900,time_range=ui.time_range,granularity=ui.granularity)))
    owner.render=refresh;refresh();host=mount(ui,1440,940)
    try:
        ui.choose_granularity('week');assert ui.granularity=='week'
        ui.model_choice.choose(ui.model_choice.findData('m'))
        plot=ui.plots['cost'];assert ui.granularity=='auto' and plot.bands
        assert not ui.granularity_buttons['week'].isEnabled() and not ui.granularity_buttons['month'].isEnabled()
        assert {p['model'] for p in plot.rows}=={'m'}
        assert not ui.plots['count'].bands and ui.plots['session_cache'].bands
        assert plot.bounds[1]<200 and not plot.strip
        assert plot.data['window_seconds']==900 and not plot.data['raw_visible']
        assert all(v is None or 0<=v<=plot.bounds[1] for v in plot_values(plot.data,plot.selected_style,True))
        assert all(p.bounds[0]==0 for p in ui.plots.values())
        item=render_plot(host,plot)
        for node in ui.plots.values():control(host,node).findChild(QObject,'plotHover').setProperty('enabled',False)
        standard=next(p for p in plot.rows if p['service_tier']=='Standard' and p['band_valid'] and p['mean']>p['distribution']['median'])
        fast=next(p for p in plot.rows if p['service_tier']=='Fast' and p['ts']==standard['ts'])
        assert plot.xy(standard).x()==plot.xy(fast).x()
        click(host,item,plot.xy(standard).x(),plot.xy(standard).y())
        assert ui.inspected==(standard,'cost') and standard['value']==standard['mean']
        assert not ui.details.comparison.isVisible() and ui.details.band_text.isVisible()
        assert 'Standard' in ui.details.mode_comparison.text() and 'Fast' in ui.details.mode_comparison.text()
        assert plot.nearest(plot.x(base+150*60),plot.y(3))['value'] is None
        count_plot=ui.plots['count'];count_point=count_plot.matching_points(base+3600)[0]
        ui.show_interval(count_point,'count')
        assert 'Standard' in ui.details.mode_comparison.text() and 'Fast' in ui.details.mode_comparison.text()
        assert not ui.details.distribution.isVisible() and not ui.details.comparison.isVisible()
        ui.show_interval(standard,'cost')
        legends=ui.legend.findChildren(SeriesLegend)
        assert not ui.show_outer and not hasattr(ui,"outer_button")
        from PySide6.QtGui import QFontMetrics
        labels=[text for _,text,_ in plot.time_labels(QFontMetrics(plot.font()))]
        assert any(':' in text for text in labels)
        render_plot(host,ui.labels['cost'])
        assert host.quick.grabFramebuffer().save(str(tmp_path/('rolling-bands-both-'+language+'.png')))
        ui.set_window(base,base+3600);assert plot.data['window_seconds']==300 and plot.data['raw_visible']
        assert plot.bounds[1]>200 and plot.data['points']
        assert '5' in ui.window_note.text() and ('min' in ui.window_note.text() if language=='en' else '분' in ui.window_note.text())
        assert ui.legend.findChildren(SeriesLegend)==legends
        ui.choose_mode('Standard');assert {p['service_tier'] for p in plot.rows}=={'Standard'}
        assert plot.bounds[0]==0 and plot.bounds[1]<200
        reasoning=ui.plots['reasoning'];bounds=reasoning.bounds
        ui.choose_reasoning_effort('low');assert reasoning.bounds==bounds
        ui.choose_reasoning_effort('high');assert reasoning.bounds==bounds
        plot.key(Qt.Key_Right)
        assert ui.inspected[1]=='cost'
        assert host.quick.grabFramebuffer().save(str(tmp_path/('rolling-bands-'+language+'.png')))
        assert not host.qml_errors
        ui.add_model('other');assert not plot.bands and ui.granularity_buttons['week'].isEnabled()
        ui.choose_granularity('week');assert ui.granularity=='week'
    finally:dispose(host);set_language('ko')

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
    source=engine(rows);projector=PerformanceTrends()
    began=time.perf_counter();data=projector.query(source,query(now=1801000000,plot_width=768));elapsed=time.perf_counter()-began
    points=panel(data,'cost')['points']
    assert any(p.get('count') and p['record']['key']=='543210' for p in points)
    assert len(points)<=768*6
    app=QApplication.instance() or QApplication([])
    owner=SimpleNamespace(settings=QSettings(str(tmp_path/'million.ini'),QSettings.IniFormat),render=lambda:None,open_record=lambda r:None)
    ui=PerformancePanel(owner);ui.apply(data);host=mount(ui,1120,800)
    owner.render=lambda:ui.apply(projector.query(source,query(now=1801000000,plot_width=768,granularity=ui.granularity)))
    try:
        chart=ui.plots['cost'];render_plot(host,chart);frames=[];selection=[]
        for i in range(25):
            point=chart.rows[i*len(chart.rows)//25];xy=chart.xy(point)
            began=time.perf_counter();chart.nearest(xy.x(),xy.y());selection.append((time.perf_counter()-began)*1000)
            began=time.perf_counter();chart.update();app.processEvents();host.quick.grabFramebuffer();frames.append((time.perf_counter()-began)*1000)
        report=dict(calls=count,points=len(points),preparation_seconds=elapsed,selection_p95_ms=sorted(selection)[23],frame_p95_ms=sorted(frames)[23])
        began=time.perf_counter();ui.solo('m');report['rolling_preparation_seconds']=time.perf_counter()-began
        render_plot(host,chart);band_frames=[]
        assert chart.bands and chart.bounds[1]<2
        assert max(p['distribution']['maximum'] for p in chart.rows)==100
        assert len(chart.rows)<=130 and panel(projector.view,'cost')['n']==count
        for _ in range(25):
            began=time.perf_counter();chart.update();app.processEvents();host.quick.grabFramebuffer();band_frames.append((time.perf_counter()-began)*1000)
        report['band_frame_p95_ms']=sorted(band_frames)[23]
        report['band_frame_max_ms']=max(band_frames)
        (tmp_path/'benchmark.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        assert report['selection_p95_ms']<50,report
        assert report['frame_p95_ms']<100,report
        assert report['band_frame_p95_ms']<100,report
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

def test_reasoning_tabs_share_trend_axis_and_keep_other_charts(tmp_path):
    from datetime import datetime
    from PySide6.QtCore import QSettings,QPointF,QObject
    from PySide6.QtWidgets import QApplication
    from cachemonitor.performance_panel import PerformancePanel
    from cachemonitor.quick_qa import mount,dispose,render_plot,control,click,scene_view
    app=QApplication.instance() or QApplication([])
    base=datetime(2026,10,1).timestamp();rows=[]
    for day in range(3):
        for effort,values in (('low',(100,200)),('medium',(None,None)),('high',(1000,2000)),('ultra',(10000,20000))):
            for value in values:
                rows.append(row(len(rows),ts=base+day*86400+3600+len(rows),effort=effort,reasoning=value))
    source=engine(rows);projector=PerformanceTrends();data=projector.query(source,query(now=base+3*86400))
    owner=SimpleNamespace(settings=QSettings(str(tmp_path/'reasoning.ini'),QSettings.IniFormat),render=lambda:None)
    ui=PerformancePanel(owner);ui.apply(data);host=mount(ui,1440,940)
    try:
        assert 'reasoning' in ui.plots and not any(k.startswith('reasoning:') for k in ui.plots)
        assert set(ui.reasoning_buttons)=={'low','medium','high','ultra'}
        chart=ui.plots['reasoning'];item=render_plot(host,chart)
        # Scrolling to the tabs can deliver ambient pointer events to the chart.
        # This check owns the selected interval; hover has separate coverage.
        for node in ui.plots.values():control(host,node).findChild(QObject,'plotHover').setProperty('enabled',False)
        expected=chart.bounds;assert expected[0]==0 and 15000<expected[1]<18000
        assert all(p.bounds[0]==0 for p in ui.plots.values())
        other_bounds={k:p.bounds for k,p in ui.plots.items() if k!='reasoning'}
        ui.inspect(chart.rows[0]['ts']);app.processEvents()
        first_frame=host.quick.grabFramebuffer();cached=item._static_image
        ui.inspect(chart.rows[-1]['ts']);app.processEvents()
        second_frame=host.quick.grabFramebuffer()
        assert item._static_image is cached
        assert first_frame!=second_frame
        point=chart.rows[-1];ui.show_interval(point,'reasoning')
        render_plot(host,ui.reasoning_tabs)
        for effort,value in (('low',150),('ultra',15000),('high',1500)):
            click(host,control(host,ui.reasoning_buttons[effort]))
            assert ui.plots['reasoning'] is chart and chart.bounds==expected
            assert {p['value'] for p in chart.rows}=={value}
            assert ui.reasoning_buttons[effort].isChecked() and sum(b.isChecked() for b in ui.reasoning_buttons.values())==1
            assert ui.inspected[1]=='reasoning' and ui.inspected[0]['effort']==effort
            assert ui.inspected[0]['ts']==point['ts'] and ui.inspected[0]['value']==value
            assert ui.details.identity.text().endswith('/ '+effort)
            assert ui.time_range is None and other_bounds=={k:p.bounds for k,p in ui.plots.items() if k!='reasoning'}
            click(host,control(host,ui.reasoning_buttons[effort]))
            assert ui.reasoning_buttons[effort].isChecked() and chart.bounds==expected
        click(host,control(host,ui.reasoning_buttons['medium']))
        assert not chart.rows and chart.bounds==expected and ui.inspected is None
        click(host,control(host,ui.reasoning_buttons['ultra']))
        assert PerformancePanel(owner).reasoning_effort=='ultra'
        assert ui.legend_scroll.parent() is ui.chart_scroll.parent().parent() is ui.inspector.parent().parent()
        left=scene_view(host,ui.legend_scroll);center=scene_view(host,ui.chart_scroll);right=scene_view(host,ui.inspector)
        x=lambda item:item.mapToScene(QPointF(0,0)).x()
        assert x(left)+left.width()<=x(center) and x(center)+center.width()<=x(right)
        assert ui.legend_scroll.isVisible() and not hasattr(ui,'legend_button')
        guide=scene_view(host,ui.chart_help);legend=scene_view(host,ui.legend)
        assert guide.mapToScene(QPointF(0,0)).y()>legend.mapToScene(QPointF(0,legend.height())).y()
        date=scene_view(host,ui.period);navigator=scene_view(host,ui.navigator)
        assert 0<=navigator.mapToScene(QPointF(0,0)).y()-date.mapToScene(QPointF(0,date.height())).y()<=16
        assert host.quick.grabFramebuffer().save(str(tmp_path/'reasoning-tabs.png'))
        # A changed visible period recalculates the common scale, rather than retaining a global maximum.
        narrower=projector.query(source,query(now=base+3*86400,time_range=[base,base+86400]))
        legend_nodes=list(ui.legend._nodes)
        ui.apply(narrower);assert chart.bounds==expected and ui.reasoning_effort=='ultra'
        assert ui.legend._nodes==legend_nodes
        rows[1]['reasoning']=None
        for r in rows:
            if r['effort']=='ultra':r['reasoning']=300
        source.revision+=1
        ui.apply(projector.query(source,query(now=base+3*86400)))
        assert chart.bounds[0]==0 and 1500<chart.bounds[1]<1800 and ui.reasoning_effort=='ultra'
        assert not host.qml_errors
    finally:dispose(host)


def test_time_navigator_presets_live_updates_and_date_ranges(tmp_path):
    from datetime import datetime
    from PySide6.QtCore import QSettings,QMetaObject,Q_ARG,Qt,QPointF
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from cachemonitor.performance_panel import PerformancePanel
    from cachemonitor.quick_qa import mount,dispose,control,click,render_plot,scene_view
    app=QApplication.instance() or QApplication([])
    base=datetime(2026,1,1).timestamp();day=86400;now=base+100*day
    source=engine([row(i,ts=base+i*day) for i in range(100)]);projector=PerformanceTrends()
    owner=SimpleNamespace(settings=QSettings(str(tmp_path/'navigation.ini'),QSettings.IniFormat),snapshot={'ts':now})
    ui=PerformancePanel(owner)
    def refresh():
        owner.snapshot={'ts':now}
        ui.apply(projector.query(source,query(now=now,plot_width=900,time_range=ui.query_range(now,source.revision))))
    owner.render=refresh;refresh();host=mount(ui,1440,940)
    try:
        # The navigator stays visible when the graph viewport scrolls at either width.
        from cachemonitor.presentation import Scroll
        ancestor=ui.navigator
        while ancestor is not None:
            assert not isinstance(ancestor,Scroll)
            ancestor=ancestor.parent()
        for width,height in ((1440,940),(1000,760)):
            host.resize(width,height)
            render_plot(host,ui.plots['cost'])
            top_y=scene_view(host,ui.navigator).mapToScene(QPointF(0,0)).y()
            render_plot(host,ui.plots['count'])
            navigator=scene_view(host,ui.navigator)
            assert navigator.mapToScene(QPointF(0,0)).y()==pytest.approx(top_y)
            assert 0<=top_y and top_y+navigator.height()<=host.quick.height()
        host.resize(1440,940);QTest.qWait(30)
        click(host,control(host,ui.period_buttons['7d']))
        assert ui.range==pytest.approx((now-7*day,now)) and ui.follow_latest
        assert ui.period_buttons['7d'].isChecked()
        # Idle polling does not move the range or force a complete projection rebuild.
        assert ui.query_range(now+10,source.revision)==list(ui.range)
        item=control(host,ui.navigator)
        def gesture(name,x=None):
            args=() if x is None else (Q_ARG('QVariant',float(x)),)
            assert QMetaObject.invokeMethod(item,name,Qt.DirectConnection,*args)
            QTest.qWait(30)
        def x(fraction):return 16+item.property('railWidth')*fraction
        # Invoke the real gesture handlers without moving the user's cursor.
        gesture('beginGesture',x(.93));gesture('updateGesture',x(.70));gesture('finishGesture')
        assert ui.range==pytest.approx((base+70*day,now)) and ui.follow_latest
        gesture('beginGesture',x(1));gesture('updateGesture',x(.85));gesture('finishGesture')
        assert ui.range==pytest.approx((base+70*day,base+85*day)) and not ui.follow_latest
        gesture('beginGesture',x(.775));gesture('updateGesture',x(.675));gesture('finishGesture')
        assert ui.range==pytest.approx((base+60*day,base+75*day))
        frozen=list(ui.range)
        gesture('beginGesture',x(.675));gesture('updateGesture',x(.5));gesture('cancelGesture')
        assert ui.time_range==frozen
        now+=3601;source.sessions['s']['prepared']['history'].append(row(100,ts=now-1));source.revision+=1;refresh()
        assert ui.range==tuple(frozen) and not any(p['ts']==now-1 for p in ui.specs['cost']['points'])
        click(host,control(host,ui.latest_button))
        assert ui.range==pytest.approx((now-15*day,now)) and ui.follow_latest
        assert any(p['ts']==now-1 for p in ui.specs['cost']['points'])
        now+=3601;source.sessions['s']['prepared']['history'].append(row(101,ts=now-1));source.revision+=1;refresh()
        assert ui.follow_latest and ui.range==pytest.approx((now-15*day,now))
        assert any(p['ts']==now-1 for p in ui.specs['cost']['points'])
        item.forceActiveFocus();QTest.keyClick(host.quick,Qt.Key_Left);QTest.qWait(30)
        assert not ui.follow_latest
        QTest.keyClick(host.quick,Qt.Key_End);QTest.qWait(30);assert ui.follow_latest and ui.range[1]==now
        click(host,control(host,ui.follow_button));assert not ui.follow_latest
        click(host,control(host,ui.follow_button));assert ui.follow_latest
        click(host,control(host,ui.period))
        ui.date_start.edit('2026-02-01');ui.date_end.edit('2026-02-07')
        click(host,control(host,ui.date_apply))
        assert ui.range==(datetime(2026,2,1).timestamp(),datetime(2026,2,8).timestamp())
        assert not ui.date_editor.isVisible() and not ui.follow_latest
        click(host,control(host,ui.period));before=ui.range;ui.date_start.edit('2026-03-01')
        click(host,control(host,ui.date_apply));assert ui.range==before and ui.date_error.text()
        ui.date_start.edit('1900-01-01');ui.date_end.edit(datetime.fromtimestamp(now).strftime('%Y-%m-%d'))
        click(host,control(host,ui.date_apply))
        assert ui.range==ui.full and not ui.follow_latest
        ui.date_editor.hide();click(host,control(host,ui.period_buttons['all']))
        assert ui.time_range is None and ui.follow_latest and ui.range==ui.full
        ui.set_window(base,base+day);ui.zoom(2)
        assert ui.range[1]-ui.range[0]==2*day
        ui.reset()
        assert host.quick.grabFramebuffer().save(str(tmp_path/'navigation.png'))
        assert not host.qml_errors
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
        owner.resize(1028,749);owner.show();owner.change_page(5);QTest.qWait(200)
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
        assert plot.bands and not plot.strip
        assert {p['model'] for p in plot.rows}=={models[0]}
        from cachemonitor.performance_panel import plot_values
        assert all(v is None or plot.bounds[0]<=v<=plot.bounds[1] for v in plot_values(plot.data,plot.selected_style,True))
        assert not ui.granularity_buttons['week'].isEnabled()
        assert host.quick.grabFramebuffer().save(str(tmp_path/'rolling-bands-dense.png'))
        # Multiple-model comparison retains the original trend/causal-outlier flow.
        ui.add_model(models[1]);assert not plot.bands
        assert plot.bounds[1]<plot.data['high']*.6
        assert all(p['kind']=='trend' for p in plot.rows)
        assert 'cost_total' not in ui.plots
        assert ui.labels['input'].text()=='평균 입력 토큰'
        trend=next(p for p in plot.rows if plot.selected_style(p) and p['baseline'] is not None and p['delta'])
        item=render_plot(host,plot)
        # Deliver the hover directly, independent of delayed synthetic click movement.
        from PySide6.QtCore import QObject
        item.findChild(QObject,'plotHover').setProperty('enabled',False)
        xy=plot.xy(trend);item.showTip(xy.x(),xy.y())
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
            # Resolve pending scroll/layout changes before taking click coordinates.
            click(host,render_plot(host,ui.granularity_buttons[unit]))
            if ui.granularity!=unit or projector.view['granularity']!=unit:
                from pathlib import Path
                from PySide6.QtCore import QPointF
                from cachemonitor.quick_qa import scene_view
                folder=Path('artifacts/verification/performance-unit-failure');folder.mkdir(parents=True,exist_ok=True)
                host.quick.grabFramebuffer().save(str(folder/'scene.png'))
                states={}
                for key,node in ui.granularity_buttons.items():
                    item=control(host,node);point=item.mapToScene(QPointF(item.width()/2,item.height()/2))
                    states[key]=dict(x=point.x(),y=point.y(),width=item.width(),height=item.height(),visible=item.isVisible(),enabled=item.isEnabled(),checked=item.property('checked'))
                print('UNIT FAILURE',dict(wanted=unit,actual=ui.granularity,states=states,window=[host.width(),host.height()],quick=[host.quick.width(),host.quick.height()],modal=str(app.activeModalWidget()),qml=host.qml_errors))
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
    assert point['distribution']==dict(median=4.5,p10=.9,p90=8.1,minimum=0.,maximum=9.,
        q1=2.25,q3=6.75,whisker_low=0.,whisker_high=9.,outliers=[],outlier_count=0)
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
