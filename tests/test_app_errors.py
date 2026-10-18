"""Operational failures use source facts, bounded history and explicit recovery."""
from collections import deque
import json
import time

import pytest

from cachemonitor.app_errors import ErrorState, TITLES, detect, strongest


def test_six_categories_merge_independent_sources_and_require_confirmed_recovery():
    state=ErrorState(clock=lambda:100)
    failures=(
        ('collection.service','collection',dict(start_failed=True,retry_exhausted=True)),
        ('collection.source','collection',dict(read_failed=True)),
        ('analysis.process','analysis',dict(process_exited=True,retry_exhausted=True)),
        ('connection.status','connection',dict(configured=True,probe_state='refused',notification_confirmed=True)),
        ('observation.health','observation',dict(storage_failure_streak=3)),
        ('quota.live','quota',dict(lookup_failed=True,reason='account_missing')),
        ('update.status','update',dict(phase='failed')),
    )
    for source,kind,facts in failures:state.update(source,kind,facts)
    assert set(state.grouped())==set(TITLES) and len(TITLES)==6
    assert state.grouped()['collection'].severity=='error'
    revision=state.revision
    for source,kind,facts in failures:assert state.update(source,kind,facts) is None
    assert state.revision==revision and len(state.history)==7
    state.update('connection.status','connection',dict(configured=True,probe_state='unknown'))
    assert 'connection' in state.grouped()
    state.update('collection.service','collection',{},healthy=True)
    assert state.grouped()['collection'].code=='read'
    state.update('collection.source','collection',{},healthy=True)
    assert 'collection' not in state.grouped()
    assert len(state.sources)<=len(state.SOURCES)
    for _ in range(100):
        state.update('update.status','update',{},healthy=True)
        state.update('update.status','update',dict(phase='failed'))
    assert len(state.history)==50 and len(state.records)<=len(state.SOURCES)


def test_collection_delay_history_is_distinct_from_failure_and_recovers():
    state=ErrorState(clock=lambda:100)
    state.update('collection.service','collection',dict(snapshot_at=60))
    delay=state.grouped()['collection']
    assert delay.code=='stale' and delay.severity=='warning' and not delay.notify
    assert state.history[0]['title']==delay.title!=TITLES['collection']
    state.update('collection.service','collection',dict(runtime_failed=True))
    failure=state.grouped()['collection']
    assert failure.code=='runtime' and failure.title==TITLES['collection']
    assert state.history[1]['resolution']=='상태 변경'
    state.update('collection.service','collection',{},healthy=True)
    assert 'collection' not in state.grouped() and state.history[0]['resolution']


def test_pending_cancelled_old_and_unknown_facts_do_not_create_errors():
    assert detect('collection',dict(starting=True,snapshot_at=10),100) is None
    assert detect('collection',dict(expected_stop=True,runtime_failed=True),100) is None
    assert detect('analysis',dict(query_failed=True,request_id=1,current_request_id=2),100) is None
    assert detect('connection',dict(configured=True,probe_state='unknown'),100) is None
    assert detect('connection',dict(configured=True,probe_state='refused',update=dict(phase='switching')),100) is None
    assert detect('observation',dict(observation_dropped=4,previous_dropped=4),100) is None
    assert detect('quota',dict(stale=True,calculation_failed=True),100) is None
    assert detect('update',dict(phase='needs_exit'),100) is None
    assert detect('update',dict(phase='failed',cancel_requested=True),100) is None
    assert detect('update',dict(phase='partial',app=dict(state='verified'),proxy=dict(phase='waiting')),100) is None
    query=detect('analysis',dict(query_failed=True,request_id=2,current_request_id=2),100)
    assert strongest([query]) is None


def test_recovery_failure_notifies_once_when_two_status_sources_report_it():
    state=ErrorState()
    facts=dict(phase='recovery_failed',incident=dict(recovery_error='private diagnostic'))
    assert state.update('connection.status','connection',facts) is not None
    assert state.update('connection.protection','connection',facts) is None
    assert len(state.grouped())==1


def test_quota_query_error_clears_when_current_prepared_result_is_available(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.quota_panel import QuotaPanel
    app=QApplication.instance() or QApplication([])
    panel=QuotaPanel(QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    events=[];panel.fault_changed.connect(events.append)
    try:
        key=panel._query_key();panel._query_pending=key
        panel._receive_query(dict(kind='query',key=key,error='temporary failure'))
        assert events[-1].get('calculation_failed')
        panel._cache_query(key,{'fixture':'current result'})
        assert panel._prepared_statistics()=={'fixture':'current result'}
        assert events[-1].get('query_healthy')
        count=len(events);panel._prepared_statistics();assert len(events)==count
    finally:panel.deleteLater();app.processEvents()


def test_collector_source_failures_carry_facts_without_exception_text(tmp_path,monkeypatch):
    from cachemonitor.app_services import AppServices
    from cachemonitor.usage_collection import CollectionPending,main
    home=tmp_path/'home';home.mkdir()
    services=AppServices(None,[str(home)],tmp_path/'index.sqlite')
    def fail():raise PermissionError('private filename and credentials')
    monkeypatch.setattr(services,'start_collection',fail)
    facts=services.start()['collection_fault']
    assert facts['start_failed'] and facts['error_type']=='PermissionError'
    assert detect('collection',facts,time.time()).kind=='collection'
    def pending():raise CollectionPending('waiting for the previous owner')
    monkeypatch.setattr(services,'start_collection',pending)
    assert detect('collection',services.start()['collection_fault'],time.time()) is None
    monkeypatch.setattr('cachemonitor.usage_collection.CollectorService',lambda *args:fail())
    monkeypatch.setattr('sys.argv',['collector','--codex-home',str(home),'--index-path',str(tmp_path/'index.sqlite')])
    assert main()==1
    marker=json.loads((tmp_path/'index.sqlite.codexon-collector-error.json').read_text())
    assert marker['facts']['start_failed'] and len(marker['scope'])==64
    assert 'private' not in json.dumps(marker) and len(json.dumps(marker))<1024


def test_record_read_and_observation_write_failures_are_distinct(tmp_path,monkeypatch):
    from cachemonitor.index import UsageIndex
    from cachemonitor.evidence_writer import EvidenceWriter
    home=tmp_path/'home';home.mkdir()
    index=UsageIndex([str(home)],tmp_path/'index.sqlite',tmp_path/'model.sqlite')
    try:
        index.queue=deque([(str(home/'usage.jsonl'),home,'task')])
        def denied(*args):raise PermissionError('fixture read denied')
        monkeypatch.setattr(index,'scan_file',denied)
        snapshot=index.poll()
        assert snapshot['collection_fault']['read_failed']
        assert detect('collection',snapshot['collection_fault'],time.time()).code=='read'
        assert not snapshot['observation_read_failed']
    finally:index.close()
    path=tmp_path/'unwritable.sqlite';path.mkdir()
    writer=EvidenceWriter(path,capacity=4)
    try:
        for number in range(3):writer.write(home,attempt=str(number),ts=time.time())
        deadline=time.monotonic()+5
        while writer.status()['storage_failure_streak']<3 and time.monotonic()<deadline:
            writer.stopping.wait(.005)
        assert detect('observation',writer.status(),time.time()).code=='save'
    finally:
        writer.close();assert not writer.thread.is_alive()


def test_last_automatic_collector_restart_failure_reports_exhausted_budget(tmp_path,monkeypatch):
    from cachemonitor.app_services import AppServices
    class Channel:
        scope='fixture'
        path=tmp_path/'index.sqlite'
        def __init__(self,*args):pass
        def companion(self,suffix):return tmp_path/('fixture'+suffix)
        def read_header(self):return None
        def close(self):pass
    class Task:
        def __init__(self,*args,**kwargs):pass
        def inspect(self):return dict(running=False)
        def start(self,*args,**kwargs):raise PermissionError('fixture task denied')
    services=AppServices(None,[str(tmp_path/'home')],tmp_path/'index.sqlite',clock=lambda:100)
    services.active=True;services.retries['collection']=dict(at=99,count=2)
    monkeypatch.setattr('cachemonitor.usage_collection.CollectionChannel',Channel)
    monkeypatch.setattr('cachemonitor.usage_collection.locked',lambda path:False)
    monkeypatch.setattr('cachemonitor.usage_collection.collector_command',lambda channel:[])
    monkeypatch.setattr('cachemonitor.observer_task.ObserverTask',Task)
    facts=services.poll()['collection_fault']
    assert facts['start_failed'] and facts['retry_exhausted']
    assert detect('collection',facts,time.time()).notify


def test_account_and_update_failures_have_machine_readable_conditions(tmp_path,monkeypatch):
    from cachemonitor.quota_live import AccountClient,QuotaLookupError
    from cachemonitor.update_state import UpdateState
    client=AccountClient(tmp_path)
    monkeypatch.setattr(client,'start',lambda:None)
    monkeypatch.setattr(client,'rpc',lambda *args,**kwargs:{})
    with pytest.raises(QuotaLookupError) as raised:client.fetch()
    assert raised.value.code=='account_missing'
    assert detect('quota',dict(lookup_failed=True,reason=raised.value.code),time.time()).notify
    journal=UpdateState(tmp_path/'update');record=journal.begin('app','fixture')
    journal.fail(record['operation_id'],'fixture installer failure')
    assert detect('update',journal.read(),time.time()).code=='install'


def test_analysis_bridge_preserves_failure_kind_and_request_id(monkeypatch):
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from cachemonitor.analysis_worker import AnalysisBridge
    class Connection:
        def __init__(self,messages=()):self.messages=list(messages)
        def close(self):pass
        def send(self,value):pass
        def poll(self,*args):return bool(self.messages)
        def recv(self):return self.messages.pop(0)
    parent=Connection([dict(kind='error',id=17,error='private source traceback')]);child=Connection()
    class Process:
        sentinel=object()
        def start(self):pass
        def is_alive(self):return bool(parent.messages)
        def join(self,*args):pass
    context=SimpleNamespace(Pipe=lambda: (parent,child),Process=lambda **kwargs:Process())
    monkeypatch.setattr('cachemonitor.analysis_worker.mp.get_context',lambda *args:context)
    monkeypatch.setattr('cachemonitor.analysis_worker.wait',
                        lambda handles:[parent] if parent.messages else [handles[-1]])
    worker=AnalysisBridge([]);observed=[]
    def receive(facts):
        observed.append(facts);worker.requestInterruption()
    worker.fault.connect(receive,Qt.DirectConnection)
    worker.start();assert worker.wait(5000)
    assert len(observed)==1 and observed[0]['query_failed'] and observed[0]['request_id']==17
    assert 'private' not in json.dumps(observed)


def test_cached_update_offer_does_not_resolve_a_failed_metadata_request(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    window=Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    try:
        window.receive_update_error(dict(checking_failed=True))
        window.update_panel.show_plan(dict(kind='current'))
        assert window.error_state.grouped()['update'].code=='check'
        window.receive_update_error(dict(check_healthy=True))
        assert 'update' not in window.error_state.grouped()
    finally:window.quit_app();app.processEvents()


@pytest.mark.integration
def test_sidebar_errors_remain_visible_across_pages_and_do_not_show_main_banner(tmp_path,monkeypatch):
    from PySide6.QtCore import QSettings,QPointF
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.quick_qa import click,control,scene_view
    from test_ui import wait_for_rendered
    app=QApplication.instance() or QApplication([])
    window=Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    sent=[];monkeypatch.setattr(window.tray,'showMessage',lambda *args:sent.append(args))
    try:
        window.tick.stop();window.resize(1050,800);window.show()
        window.notification_master.setChecked(False)
        window.receive_collection_status(dict(collection_fault=dict(start_failed=True,retry_exhausted=True)))
        assert not sent and window.error_button.isVisible()
        for page in (1,2,3,4,5):
            window.change_page(page)
            assert window.error_button.isVisible()
            wait_for_rendered(lambda:window.pages.isPageReady(page-1) and not window.analysis_loading,timeout=5)
            assert window.error_button.isVisible() and not window.analysis_loading
            view=scene_view(window,window.error_button);point=view.mapToScene(QPointF(0,0))
            assert point.x()<200 and point.y()>window.height()/2 and point.y()+view.height()<=window.height()
        click(window,control(window,window.error_button))
        assert window.current_page==4 and window.settings_page.current_category()=='notifications'
        assert window.error_panel.faults['collection'].code=='start'
        window.receive_quota(dict(quota=None,issue='private diagnostic',
            quota_faults=dict(lookup=dict(lookup_failed=True,reason='timeout'))))
        window.change_page(3);app.processEvents()
        assert not window.quota_panel.status.isVisible()
        assert all(card['value'].text()=='—' for card in window.quota_sidebar.current.values())
        window.receive_collection_status(dict(collection_fault={},collection_healthy=True))
        window.receive_quota(dict(quota=None,quota_faults=dict(lookup={})))
        assert not window.error_button.isVisible() and window.index_status.isVisible()
        assert window.notification_log.model().rows[0].get('resolution')
        assert not window.qml_errors
    finally:window.quit_app();app.processEvents()


@pytest.mark.integration
def test_observation_gap_recovers_only_after_new_write_and_ignores_old_counters(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from cachemonitor.dashboard import Dashboard
    app=QApplication.instance() or QApplication([])
    window=Dashboard([],start_worker=False,live_limits=False,
        settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat))
    def observe(instance,dropped,written):
        window.receive_proxy_status(dict(configured=True,probe_state='healthy',phase='active',
            health=dict(instance=instance,observation_dropped=dropped,storage_written=written)))
    try:
        observe('first',5,10)
        assert 'observation' not in window.error_state.grouped()
        observe('first',6,10)
        assert window.error_state.grouped()['observation'].code=='gap'
        observe('first',6,10)
        assert 'observation' in window.error_state.grouped()
        observe('second',0,0)
        assert 'observation' in window.error_state.grouped()
        observe('second',0,1)
        assert 'observation' not in window.error_state.grouped()
        observe('second',1,1)
        assert 'observation' in window.error_state.grouped()
        observe('third',0,3)
        assert 'observation' not in window.error_state.grouped()
    finally:window.quit_app();app.processEvents()
