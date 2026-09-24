from cachemonitor.charts import comparison_axis
from cachemonitor.quota_cycles import quota_value_history
from cachemonitor.quota_view import history_rows, prepare_quota_view
from test_quota_attribution import example


def test_confirmed_ratio_survives_next_request_and_updates_on_completion():
    report=example([(100,100),(130,98),(160,98),(190,97),(220,96),(250,95),(280,95)],
                   [('a',110,115,2),('b',175,240,6)])
    rows,_=quota_value_history(report,history_rows(report,'weekly'))
    bytime={r['at']:r for r in rows}
    assert bytime[160]['cycle_value']==100
    assert bytime[190]['value_pending']
    assert bytime[190]['cycle_value']==bytime[220]['cycle_value']==100
    assert bytime[280]['cycle_value']==160
    overall=prepare_quota_view(report)['overall']['rows']
    assert next(r for r in overall if r['at']==220)['cycle_value']==100
    assert overall[-1]['cycle_value']==160


def test_distribution_axis_covers_visible_marks_without_hidden_maximum():
    row=dict(n=100,p10=10,q1=12,median=15,q3=18,p90=20,value=16,maximum=10000)
    lo,hi=comparison_axis([row],'cost')
    assert lo<10<20<hi and hi-lo<15
    lo,hi=comparison_axis([dict(n=2,points=[1,500],value=250)],'cost')
    assert lo<=1 and hi>500
    for value in (0,100):
        lo,hi=comparison_axis([dict(n=1,points=[value],value=value)],'cache_ratio')
        assert lo<=value<=hi and hi>lo
