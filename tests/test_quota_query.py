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
    assert panel.query_loading.isVisible()
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
    assert not panel.query_loading.isVisible() and not panel.query_loading._state['running']
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


def test_selected_graph_rejects_old_choice_and_reuses_matching_refresh(tmp_path):
    from cachemonitor.quota_worker import QuotaComputation
    from cachemonitor.quota_panel import QuotaPanel
    from test_quota_value_history import sample_report
    app=QApplication.instance() or QApplication([])
    engine=QuotaComputation();worker=QueryWorker()
    compact=engine._prepare(sample_report())
    panel=QuotaPanel(QSettings(str(tmp_path/'charts.ini'),QSettings.IniFormat))
    panel.receive(dict(report={**compact,'_worker':worker}))
    assert panel.history.rows[-1]['at']==280
    assert panel.manage_button.isVisible()
    panel.cycle_choice.setCurrentIndex(panel.cycle_choice.findData(100))
    assert not panel.manage_button.isVisible() and panel.history_loading.isVisible()
    assert not panel.history_empty.isVisible()
    first=worker.requests[-1]
    panel.cycle_choice.setCurrentIndex(panel.cycle_choice.findData(160))
    latest=worker.requests[-1]
    worker.result.emit(engine.execute(dict(first,id=2)))
    assert panel.history.rows[-1]['at']==280
    worker.result.emit(engine.execute(dict(latest,id=3)))
    assert panel.history.rows[-1]['at']==220
    assert panel.manage_button.isVisible() and not panel.history_empty.isVisible()
    assert not panel.history_loading.isVisible() and not panel.history_loading._state['running']
    digest=panel.history.series['_content_digest']
    refreshed=engine._prepare(sample_report(),selection=160,known_digest=digest)
    assert refreshed['view']['chart']['series'] is None
    pending=len(worker.requests)
    panel.receive(dict(report={**refreshed,'_worker':worker}))
    assert len(worker.requests)==pending and panel.history.rows[-1]['at']==220
    assert panel.cycle_choice.currentData()==160
    rows=panel.history.rows
    refreshed=engine._prepare(sample_report())
    panel.receive(dict(report={**refreshed,'_worker':worker}))
    request=worker.requests[-1]
    assert panel.history.isVisible() and panel.history.rows is rows
    assert panel.history_loading.isVisible() and not panel.history_empty.isVisible()
    worker.result.emit(dict(kind='chart',key=request['key'],error='unavailable'))
    assert not panel.history_loading.isVisible() and not panel.history_loading._state['running']
    assert panel.history.isVisible() and panel.history.rows is rows
    app.processEvents()
