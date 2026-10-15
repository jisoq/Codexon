from cachemonitor.charts import comparison_axis
from cachemonitor.quota_view import prepare_quota_view
from test_quota_attribution import example


def test_weekly_value_updates_with_cost_or_consumption_during_running_request():
    report=example([(100,100),(130,98),(160,98),(190,96),(220,96),(250,95),(280,95)],
                   [('a',110,115,2),('long',175,260,6),('overlap',200,210,1)])
    view=prepare_quota_view(report)
    bytime={r['at']:r for r in view['periods'][0]['series']['rows']}
    assert bytime[100]['cycle_value'] is None
    assert bytime[130]['cycle_value']==bytime[160]['cycle_value']==100
    assert bytime[160]['cycle_cost']==bytime[190]['cycle_cost']==2
    assert bytime[190]['cycle_value']==50
    assert bytime[190]['cycle_delta']==bytime[220]['cycle_delta']==4
    assert bytime[220]['cycle_cost']==3
    assert bytime[220]['cycle_value']==75
    assert bytime[250]['cycle_value']==60
    assert bytime[280]['cycle_value']==180
    assert view['overall']['rows'][0]['cycle_value']==180


def test_distribution_axis_covers_visible_marks_without_hidden_maximum():
    row=dict(n=100,p10=10,q1=12,median=15,q3=18,p90=20,value=16,maximum=10000)
    lo,hi=comparison_axis([row],'cost')
    assert lo<10<20<hi and hi-lo<15
    lo,hi=comparison_axis([dict(n=2,points=[1,500],value=250)],'cost')
    assert lo<=1 and hi>500
    for value in (0,100):
        lo,hi=comparison_axis([dict(n=1,points=[value],value=value)],'cache_ratio')
        assert lo<=value<=hi and hi>lo
