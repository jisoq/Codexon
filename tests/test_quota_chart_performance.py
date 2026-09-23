import json
import time

from PySide6.QtWidgets import QApplication
from cachemonitor.quota_chart import QuotaHistory
from cachemonitor.quota_view import prepare_series
from cachemonitor.quick_qa import mount, render_plot, dispose


def test_million_observations_keep_exact_selection_and_bounded_rendering(tmp_path):
    app=QApplication.instance() or QApplication([])
    count=1_000_000
    rows=[dict(at=i+1000,remaining=100-i/count*80,cycle_cost=i*.0001,
               cycle_value=200+i%100,connect=i!=500001,reset_kind=None,label=f'관측 {i}')
          for i in range(count)]
    rows[543210]['cycle_value']=1200
    rows[543211]['cycle_value']=None
    started=time.perf_counter();series=prepare_series(rows);preparation=time.perf_counter()-started
    assert 543210 in series['samples'][768]
    assert len(series['samples'][768])<=768*8
    chart=QuotaHistory();chart.money=True;chart.reference=250;chart.set_series(series)
    host=mount(chart,1120,380)
    try:
        plot=render_plot(host,chart)
        assert chart.box.height()>0
        assert not chart.connects(543210,543212,'cycle_value')
        assert chart.connects(543210,543212,'cycle_cost')
        assert not chart.connects(500000,500002,'remaining')
        samples=[];lookup=[];hover=[];switch=[]
        for i in range(25):
            wanted=12345+i*30001
            x=chart.x_at(wanted)
            start=time.perf_counter()
            chart.activate_at(x,chart.box.center().y())
            lookup.append((time.perf_counter()-start)*1000)
            assert chart.cursor==wanted
            assert chart.tip_at(x,chart.box.center().y())==rows[wanted]['label']+f" · 누적 API ${rows[wanted]['cycle_cost']:.2f}"+f" · 주간 동등 가치 ${rows[wanted]['cycle_value']:.2f}"
            start=time.perf_counter();app.processEvents();host.quick.grabFramebuffer()
            samples.append((time.perf_counter()-start)*1000)
            start=time.perf_counter();plot.showTip(x,chart.box.center().y())
            hover.append((time.perf_counter()-start)*1000)
        for _ in range(5):
            start=time.perf_counter();chart.set_series(dict(series));app.processEvents();host.quick.grabFramebuffer()
            switch.append((time.perf_counter()-start)*1000)
        result={'observations':count,'drawn_points':len(series['samples'][768]),
                'background_preparation_seconds':preparation,
                'selection_p95_ms':sorted(lookup)[23],'frame_p95_ms':sorted(samples)[23],
                'hover_p95_ms':sorted(hover)[23],'new_series_frame_max_ms':max(switch),
                'new_series_frames_ms':switch}
        (tmp_path/'performance.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        assert result['selection_p95_ms']<50
        assert result['frame_p95_ms']<100
        assert result['hover_p95_ms']<50
        assert result['new_series_frame_max_ms']<200,result
        assert not host.qml_errors
    finally:dispose(host)
