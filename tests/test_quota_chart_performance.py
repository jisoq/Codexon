import json
import time
import pytest

from PySide6.QtWidgets import QApplication
from cachemonitor.quota_chart import QuotaHistory
from cachemonitor.quota_view import prepare_series
from cachemonitor.quick_qa import mount, render_plot, dispose


@pytest.mark.parametrize('count', [8192, pytest.param(1_000_000, marks=pytest.mark.extended)])
def test_observations_keep_exact_selection_and_bounded_rendering(tmp_path,count):
    app=QApplication.instance() or QApplication([])
    rows=[dict(at=i+1000,remaining=100-i/count*80,cycle_cost=i*100/count,
               cycle_value=200+i%100,connect=i!=count//2+1,reset_kind=None,label=f'관측 {i}')
          for i in range(count)]
    spike=count//2+10
    rows[spike]['cycle_value']=1200
    rows[spike+1]['cycle_value']=None
    started=time.perf_counter();series=prepare_series(rows);preparation=time.perf_counter()-started
    assert spike in series['samples'][768]
    assert len(series['samples'][768])<=768*8
    chart=QuotaHistory();chart.money=True;chart.reference=250;chart.set_series(series)
    host=mount(chart,1120,380)
    try:
        plot=render_plot(host,chart)
        assert chart.box.height()>0
        assert not chart.connects(spike,spike+2,'cycle_value')
        assert chart.connects(spike,spike+2,'cycle_cost')
        assert not chart.connects(count//2,count//2+2,'remaining')
        samples=[];lookup=[];hover=[];switch=[]
        for i in range(25):
            wanted=(i+1)*(count//27)
            x=chart.x_at(wanted)
            start=time.perf_counter()
            chart.activate_at(x,chart.box.center().y())
            lookup.append((time.perf_counter()-start)*1000)
            assert chart.cursor==wanted
            assert chart.tip_at(x,chart.box.center().y())==rows[wanted]['label']+f" / 누적 환산액 ${rows[wanted]['cycle_cost']:.2f}"+f" / 주간 동등 가치 ${rows[wanted]['cycle_value']:.2f}"
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
        if count==1_000_000:
            assert result['selection_p95_ms']<50
            assert result['frame_p95_ms']<100
            assert result['hover_p95_ms']<50
            assert result['new_series_frame_max_ms']<200,result
        else:
            from cachemonitor.quota_cycles import _PackedRows
            from cachemonitor.quota_worker import prepare_paint_rows
            before=chart._picture.copy()
            packed={**series,'rows':_PackedRows(rows)}
            prepare_paint_rows(packed)
            assert packed['_paint_rows']
            for index,row in packed['_paint_rows'].items():
                assert all(row[key]==rows[index][key] for key in ('remaining','cycle_value','cycle_cost','reset_kind'))
                assert row['_previous_remaining']==(rows[index-1]['remaining'] if index else None)
            chart.set_series(packed);chart._cache_key=None
            render_plot(host,chart)
            assert chart._picture==before
            chart.activate_at(chart.x_at(spike),chart.box.center().y())
            assert chart.cursor==spike and chart.rows[spike]==rows[spike]
        assert not host.qml_errors
    finally:dispose(host)
