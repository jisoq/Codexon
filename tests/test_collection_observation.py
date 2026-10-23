import sys
import threading
import time
from types import SimpleNamespace

import pytest

from cachemonitor.app_errors import ErrorState,collection_phase,collection_publication_at
from cachemonitor.collection_observation import CollectionObserver
from cachemonitor.usage_collection import CollectionChannel,CollectionHeaderReader,publish_collector_fault


def wait_until(app,predicate):
    deadline=time.monotonic()+5
    while not predicate():
        app.processEvents()
        assert time.monotonic()<deadline,'Collection observation did not arrive'


@pytest.fixture(scope='module')
def app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture
def publication(tmp_path,monkeypatch):
    from cachemonitor import collection_lifecycle,usage_collection
    writer=CollectionChannel([str(tmp_path/'home')],tmp_path/'index.sqlite')
    process=dict(pid=123,created=1,executable=sys.executable)
    owner=[process]
    command=[sys.executable,'--usage-collector','--codex-home',writer.homes[0],
             '--index-path',str(writer.path)]
    monkeypatch.setattr(usage_collection,'locked',lambda path:owner[0] is not None)
    monkeypatch.setattr(collection_lifecycle.identity,'process_identity',lambda pid:owner[0])
    monkeypatch.setattr(collection_lifecycle.identity,'process_command',lambda pid:command)
    monkeypatch.setattr(collection_lifecycle.identity,'same_process',lambda value:value==owner[0])
    observer=CollectionObserver(writer.homes,writer.path)
    def publish(stamp,sequence,instance='current',**changes):
        snapshot=dict(ts=stamp,homes=writer.homes,sessions=[],usage_collection_complete=True,
                      index=dict(path=str(writer.path),loading=False,usage_complete=True),
                      collection=dict(pid=123,instance=instance,executable=sys.executable))
        writer.publish({**snapshot,**changes},instance,sequence)
    try:yield SimpleNamespace(writer=writer,observer=observer,publish=publish,owner=owner)
    finally:observer.close();writer.close()


def test_passive_header_reader_does_not_create_a_missing_store(tmp_path):
    parent=tmp_path/'missing'
    reader=CollectionHeaderReader([str(tmp_path/'home')],parent/'index.sqlite')
    try:
        assert reader.read_header() is None
        assert not parent.exists()
    finally:reader.close()


def test_observation_preserves_publication_order_and_ages_missing_reads(publication,monkeypatch):
    case=publication
    case.publish(100,1)
    assert case.observer.observe()['collection_snapshot_at']==100
    case.publish(80,2)
    observation=case.observer.observe()
    assert observation['collection_snapshot_at']==80
    monkeypatch.setattr(case.observer.reader,'read_header',lambda:None)
    assert case.observer.observe()['collection_snapshot_at']==80
    evidence=dict(ts=500,collection={'instance':'retained'},usage_collection_complete=True,
                  collection_observation=observation)
    assert collection_publication_at(evidence)==80
    assert collection_phase(evidence,111,started_at=100)=='delayed'
    case.owner[0]=None
    assert case.observer.observe()['collection_snapshot_at'] is None


def test_replaced_owner_cannot_refresh_retained_publication(publication):
    case=publication
    case.publish(100,1)
    assert case.observer.observe()['collection_snapshot_at']==100
    case.owner[0]={**case.owner[0],'executable':'C:/other/collector.exe'}
    case.publish(110,2,instance='replacement')
    observation=case.observer.observe()
    assert observation['collection_snapshot_at'] is None
    assert collection_phase(dict(collection_observation=observation),131,started_at=100)=='delayed'


def test_scope_failure_is_visible_and_recovers_without_clearing_service_fault(publication):
    case=publication
    case.publish(100,1,index=dict(path=str(case.writer.path.with_name('other.sqlite'))))
    broken=case.observer.observe()
    assert broken['collection_fault'].get('status_failed')
    state=ErrorState(clock=lambda:100)
    state.update('collection.service','collection',dict(start_failed=True))
    state.update('collection.observer','collection',broken['collection_fault'])
    case.publish(101,2)
    recovered=case.observer.observe()
    assert recovered['collection_snapshot_at']==101 and not recovered['collection_fault']
    state.update('collection.observer','collection',recovered['collection_fault'],healthy=True)
    assert 'collection.observer' not in state.sources
    assert 'collection.service' in state.sources


@pytest.mark.parametrize('failure',['start_failed','runtime_failed','read_failed','save_failed','status_failed'])
def test_fresh_observation_does_not_clear_explicit_lifecycle_failure(publication,failure):
    case=publication
    case.publish(100,1)
    observed=case.observer.observe()
    assert collection_phase(dict(collection_fault={failure:True},collection_observation=observed),100)=='failed'


def test_new_scoped_collector_failure_marker_survives_fresh_publication(publication):
    case=publication
    case.publish(100,1)
    publish_collector_fault(case.writer.companion('.collector-error.json'),case.writer.scope,
                            'current',OSError('save failed'))
    observed=case.observer.observe()
    assert observed['collection_fault'].get('runtime_failed')
    assert collection_phase(dict(collection_observation=observed),100)=='failed'


def test_observation_worker_is_joined_and_queued_events_are_fenced(tmp_path,monkeypatch,app):
    from cachemonitor.app_services import AppServices
    from cachemonitor.app_shutdown import ServiceController,ShutdownOperation
    from cachemonitor import collection_observation
    entered=threading.Event();release=threading.Event();closed=threading.Event()
    class Reader:
        def __init__(self,*args):pass
        def observe(self):
            entered.set()
            assert release.wait(5)
            return dict(collection_snapshot_at=100,collection_fault={})
        def close(self):closed.set()
    monkeypatch.setattr(collection_observation,'CollectionObserver',Reader)
    owner=AppServices(None,[],tmp_path/"index.sqlite")
    monkeypatch.setattr(owner,"execute",lambda operation:{})
    owner.active=True
    controller=ServiceController(owner,None)
    observations=[];controller.collection_observed.connect(observations.append)
    try:
        controller.start()
        wait_until(app,entered.is_set)
        worker=controller.observer
        controller.set_exit_pending(True)
        shutdown=ShutdownOperation(None,[],None,handoff=True,display_workers=[worker],wait_timeout=5)
        shutdown.start()
        wait_until(app,worker.isInterruptionRequested)
        release.set()
        wait_until(app,lambda:not shutdown.isRunning())
        assert shutdown.wait(5000)
        assert shutdown.outcome.status=='completed' and closed.is_set()
        assert not observations
        wait_until(app,lambda:controller.observer is None)
        controller.set_exit_pending(False)
        assert controller.observer is not None and controller.observer is not worker
        controller.set_exit_pending(True)
    finally:
        release.set()
        if controller.operation is not None:
            assert controller.operation.wait(5000)
        controller.stop();app.processEvents()
        controller.deleteLater()
        from PySide6.QtCore import QCoreApplication,QEvent
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def test_disabled_collection_has_no_observation_worker(app):
    from cachemonitor.app_services import AppServices
    from cachemonitor.app_shutdown import ServiceController
    owner=AppServices(None,[],collection=False);owner.active=True
    controller=ServiceController(owner,None)
    try:
        controller.start()
        assert controller.observer is None
    finally:controller.stop();app.processEvents()


def test_invalid_collection_path_reports_one_fault_without_a_restart_loop(tmp_path,monkeypatch,app):
    from PySide6.QtCore import QCoreApplication,QEvent
    from cachemonitor.app_services import AppServices
    from cachemonitor.app_shutdown import ServiceController
    home=tmp_path/'home'
    owner=AppServices(None,[str(home)],home/'index.sqlite')
    owner.active=True
    monkeypatch.setattr(owner,'execute',lambda operation:{})
    controller=ServiceController(owner,None)
    observed=[];controller.collection_observed.connect(observed.append)
    try:
        controller.start()
        wait_until(app,lambda:bool(observed) and controller.observer is None)
        assert len(observed)==1 and observed[0]['collection_fault'].get('status_failed')
        assert observed[0]['collection_snapshot_at'] is None
        assert not home.exists()
    finally:
        controller.stop();controller.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def test_reused_process_identity_and_retired_instance_cannot_refresh_a_publication(publication):
    case=publication
    case.publish(100,1)
    assert case.observer.observe()['collection_snapshot_at']==100
    case.owner[0]={**case.owner[0],'created':2}
    assert case.observer.observe()['collection_snapshot_at'] is None
    assert case.observer.observe()['collection_snapshot_at'] is None
    case.publish(110,1,instance='replacement')
    assert case.observer.observe()['collection_snapshot_at']==110
    case.publish(120,2,instance='current')
    assert case.observer.observe()['collection_snapshot_at'] is None


def test_published_read_fault_reaches_dashboard_while_analysis_is_held(publication,tmp_path,app):
    from PySide6.QtCore import QCoreApplication,QEvent,QSettings
    from cachemonitor.dashboard import Dashboard
    from cachemonitor.overlay import OverlayController
    case=publication;now=time.time()
    settings=QSettings(str(tmp_path/'settings.ini'),QSettings.IniFormat)
    window=Dashboard(case.writer.homes,start_worker=False,live_limits=False,collection_autostart=False,
                     index_path=case.writer.path,settings=settings)
    overlay=OverlayController(settings,native_enabled=False)
    window.overlay=overlay
    try:
        window.tick.stop();window.notification_master.setChecked(False)
        analysis={**window.snapshot,'ts':now-1,'usage_collection_complete':True,
                  'collection_fault':{},'usage_errors':[]}
        window.receive(analysis)
        case.publish(now,1,collection_fault=dict(read_failed=True),usage_errors=['Read failed'])
        window.receive_collection_observation(case.observer.observe())
        assert 'collection.source' in window.error_state.sources
        assert 'collection.observer' not in window.error_state.sources
        assert overlay.collection_phase()=='failed'
        assert window.snapshot['ts']==now-1
        held={**analysis,'ts':now,'collection_fault':dict(read_failed=True),'usage_errors':['Read failed']}
        window.receive(held)
        window.receive_collection_status(dict(collection_fault=dict(start_failed=True)))
        case.publish(now+1,2,collection_fault={})
        window.receive_collection_observation(case.observer.observe())
        assert 'collection.source' not in window.error_state.sources
        assert 'collection.service' in window.error_state.sources
        assert overlay.collection_phase()=='failed'
        window.receive_collection_status(dict(collection_fault={},collection_healthy=True))
        window.receive(held)
        window.refresh_home_status()
        assert 'collection.source' not in window.error_state.sources
        assert overlay.collection_phase()=='ready'
        assert window.quota_panel.collection_phase=='ready'
        assert window.snapshot['collection_fault']==dict(read_failed=True)
    finally:
        window.quit_app()
        wait_until(app,lambda:window.quitting and window.shutdown_operation is None and window._quick is None)
        window.close();window.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
