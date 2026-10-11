"""Display queries keep full-account totals while transferring one table page."""
from copy import deepcopy

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication

from cachemonitor.quota_view import prepare_quota_query, prepare_quota_view
from cachemonitor.pricing import usd
from test_quota_value_history import interval, report_for


def query_report(count=32):
    report=report_for([interval([(100+i*60,100),(130+i*60,99)],[(115+i*60,i+1)])
                       for i in range(count)])
    report.update(home='h',at=100+count*60,exclusions=[])
    report['view']=prepare_quota_view(report)
    return report


def test_display_query_pages_do_not_change_model_totals_or_raw_evidence():
    report=query_report();original=deepcopy(report)
    first=prepare_quota_query(report,dict(model='test',page_size=7))
    last=prepare_quota_query(report,dict(model='test',page=99,page_size=7))
    assert first['model_totals']==last['model_totals']==dict(count=32,calls=32,priced=32,cost=528)
    assert first['total_rows']==last['total_rows']==32
    assert first['statistics']['cost']==last['statistics']['cost']==528
    assert len(first['rows'])==7 and len(last['rows'])==4
    assert last['page']==4 and last['pages']==5
    assert all('cost_rows' not in row and 'endpoints' not in row for row in first['rows'])
    assert first['statistics']['intervals'] is first['rows']
    assert report==original


def test_display_query_custom_range_and_missing_model_keep_exact_results():
    report=query_report(3)
    clipped=prepare_quota_query(report,dict(start=160,end=190,model='test'))
    assert clipped['model_totals']==dict(count=1,calls=1,priced=1,cost=2)
    assert clipped['statistics']['observed_delta']==1
    missing=prepare_quota_query(report,dict(model='absent'))
    assert missing['rows']==[] and missing['total_rows']==0
    assert missing['model_totals']==dict(count=0,calls=0,priced=0,cost=None)


class QueryWorker(QObject):
    result=Signal(object)

    def __init__(self):
        super().__init__();self.requests=[]

    def request(self,kind,**payload):
        self.requests.append(dict(kind=kind,**payload))
        return len(self.requests)


def test_compact_panel_uses_worker_results_and_rejects_stale_filter_response(tmp_path,monkeypatch):
    from cachemonitor import quota_panel as module
    app=QApplication.instance() or QApplication([])
    panel=module.QuotaPanel(QSettings(str(tmp_path/'panel.ini'),QSettings.IniFormat))
    report=query_report(3);view=report['view'];worker=QueryWorker()
    compact={key:value for key,value in report.items() if key not in ('cycles','history','tracking')}
    compact['view']={**view,'lifetime':{**view['lifetime'],'intervals':[]},
                     'summaries':{},'default_query':prepare_quota_query(report,{})}
    compact.update(worker_revision=1,_worker=worker)
    def unexpected(*args,**kwargs):
        raise AssertionError('UI must not aggregate a compact worker report')
    monkeypatch.setattr(module,'prepare_quota_view',unexpected)
    monkeypatch.setattr(module,'quota_statistics',unexpected)
    monkeypatch.setattr(module,'model_cost_intervals',unexpected)
    panel.receive(dict(home='h',report=compact))
    assert panel.cost_value.text()==usd(6)
    panel.model_choice.setCurrentIndex(panel.model_choice.findData('test'))
    old=worker.requests[-1]
    assert not panel.interval_filter.isVisible()
    panel.interval_filter.setCurrentIndex(panel.interval_filter.findData('excluded'))
    assert panel._query_parameters()['mode']=='all'
    panel.set_history_period(160,190)
    current=worker.requests[-1]
    assert current['key']!=old['key']
    worker.result.emit(dict(kind='query',key=old['key'],value=prepare_quota_query(report,old['query'])))
    assert panel.cost_value.text()==usd(6)
    worker.result.emit(dict(kind='query',key=current['key'],value=prepare_quota_query(report,current['query'])))
    assert panel.cost_value.text()==usd(2)
    assert panel.basis.text()=='1회'
    assert len(panel.intervals.model().rows)==1
    app.processEvents()


def test_compact_panel_can_reselect_pending_filter_after_cached_default(tmp_path):
    from cachemonitor.quota_panel import QuotaPanel
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'panel.ini'),QSettings.IniFormat))
    report=query_report(3);worker=QueryWorker()
    report={**report,'worker_revision':1,'_worker':worker,
            'view':{**report['view'],'default_query':prepare_quota_query(report,{})}}
    panel.receive(dict(home='h',report=report))
    panel.model_choice.setCurrentIndex(panel.model_choice.findData('test'))
    abandoned=worker.requests[-1]
    panel.model_choice.setCurrentIndex(0)
    worker.result.emit(dict(kind='query',key=abandoned['key'],value=prepare_quota_query(report,abandoned['query'])))
    panel.model_choice.setCurrentIndex(panel.model_choice.findData('test'))
    assert len(worker.requests)==2
    current=worker.requests[-1]
    worker.result.emit(dict(kind='query',key=current['key'],value=prepare_quota_query(report,current['query'])))
    assert panel.basis.text()=='3회' and panel.cost_value.text()==usd(6)
    app.processEvents()


def test_compact_default_refreshes_keep_a_bounded_result_cache(tmp_path):
    from cachemonitor.quota_panel import QuotaPanel
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'panel.ini'),QSettings.IniFormat))
    report=query_report(3);worker=QueryWorker()
    view={**report['view'],'default_query':prepare_quota_query(report,{})}
    for revision in range(20):
        panel.receive(dict(home='h',report={**report,'worker_revision':revision,'_worker':worker,'view':view}))
    assert len(panel._query_cache)<=8
    assert not worker.requests
    assert panel.cost_value.text()==usd(6)
    app.processEvents()
