from contextlib import closing
from threading import Event
import sqlite3
import time

from cachemonitor import prepared_runtime
from cachemonitor.analysis_delivery import SnapshotReceiver
from cachemonitor.prepared_ingest import PreparedCohort
from cachemonitor.prepared_runtime import PreparedWriter
from cachemonitor.usage_collection import CollectionClient, CollectorService
from test_core import TID, fixture_home
from test_demand_loading import complete, publication, write_session
from test_prepared_live_scheduling import all_calls, history


def test_freeze_finishes_captured_history_without_adopting_new_selected_source(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    at=history(home)
    target=tmp_path/'index.sqlite'
    chunk_committed=Event();source_updated=Event();frozen=Event()
    selected_after_freeze=[];captures=[]
    convert=PreparedCohort.convert
    read=prepared_runtime.read_catalog
    def convert_history(coordinator,catalog,**kwargs):
        changes=convert(coordinator,catalog,**kwargs)
        try:
            for change in changes:
                if catalog.get('authority')!='selected' and not chunk_committed.is_set():
                    chunk_committed.set()
                    assert source_updated.wait(5), 'Post-freeze source update was not supplied'
                yield change
        finally:changes.close()
    def read_catalog(client,**kwargs):
        if kwargs.get('selected') and frozen.is_set():selected_after_freeze.append(True)
        if client.active_demand['aggregate'] and not kwargs.get('selected'):captures.append(True)
        return read(client,**kwargs)
    monkeypatch.setattr(PreparedCohort,'convert',convert_history)
    monkeypatch.setattr(prepared_runtime,'read_catalog',read_catalog)
    receiver=SnapshotReceiver()
    with closing(CollectorService([home],target)) as service, \
            closing(CollectionClient([home],target)) as lease:
        lease.set_demand([(None,TID)],aggregate=True)
        complete(service)
        with closing(PreparedWriter([home],target,None,tmp_path/'quota.sqlite')) as writer:
            try:
                writer.set_selected([(None,TID)])
                publication(service,writer,receiver,lambda value:any(row['id']==TID for row in value['sessions']))
                writer.set_aggregate(7)
                deadline=time.monotonic()+5
                while not chunk_committed.is_set() and time.monotonic()<deadline:
                    service.poll(detailed=False)
                    if writer.output.receiver.poll(.05):
                        message=writer.output.take()
                        assert message['kind']!='fatal',message
                        if message['kind']=='snapshot':receiver.receive(message['value'])
                assert chunk_committed.is_set(), 'Full history never started preparation'
                writer.freeze();frozen.set()
                write_session(home,TID,count=2,at=at)
                collected=complete(service,detailed=True)
                assert len(next(row for row in collected['sessions'] if row['id']==TID)['history'])==2
                source_updated.set()
                aggregate=publication(service,writer,receiver,lambda value:value.get('aggregate_request')==7)
                assert not selected_after_freeze
                assert len(captures)==1
                assert next(row for row in aggregate['overlay_sessions'] if row['id']==TID)['calls']==1
                result=all_calls(service,writer,receiver,aggregate)
                assert result['usage']['summary']['value']==66
                assert result['analysis']['response_count']==66
                assert result['analysis']['session_count']==66
                with sqlite3.connect(writer.path.as_uri()+'?mode=ro',uri=True) as db:
                    assert db.execute('SELECT ready FROM meta WHERE singleton=1').fetchone()[0]==1
            finally:source_updated.set()
