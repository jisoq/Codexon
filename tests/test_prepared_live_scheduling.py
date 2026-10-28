from contextlib import closing
from threading import Event
import time

import pytest

from cachemonitor import prepared_runtime
from cachemonitor.analysis_delivery import SnapshotReceiver
from cachemonitor.prepared_ingest import PreparedCohort
from cachemonitor.prepared_query import PreparedQuery, token_value
from cachemonitor.prepared_runtime import PreparedWriter
from cachemonitor.usage_collection import CollectionClient, CollectorService
from test_core import TID, fixture_home
from test_demand_loading import complete, publication, write_session


def history(home, count=65):
    at=time.time()-200
    write_session(home,TID,count=1,at=at)
    for number in range(count):
        sid=f'12345678-abcd-abcd-abcd-{number:012d}'
        write_session(home,sid,at=at+number+1)
    return at


def all_calls(service,writer,receiver,snapshot):
    deadline=time.monotonic()+10
    with closing(PreparedQuery(writer.path)) as query:
        while time.monotonic()<deadline:
            now=time.time()
            result=query(dict(kind='query',id='all-calls',logical=(),query=dict(
                page=1,start=0,end=now,now=now,period='custom',category_usage=True,
                usage=dict(dimension='model',metric='calls'))),
                token_value(snapshot['prepared_token']),lambda:False)
            if result['kind']!='stale':
                assert result['kind']=='result',result
                return result['result']
            snapshot=publication(service,writer,receiver,
                lambda value:value.get('aggregate_request')==7,deadline=deadline)
    pytest.fail('Complete aggregate query did not reach a current publication')


def test_stable_history_is_captured_once_across_preparation_chunks(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    history(home)
    target=tmp_path/'index.sqlite'
    captures=[];progress=[]
    read=prepared_runtime.read_catalog
    offer=prepared_runtime.Mailbox.offer_progress
    def read_catalog(client,**kwargs):
        if client.active_demand['aggregate'] and not kwargs.get('selected'):
            captures.append(True)
        return read(client,**kwargs)
    def offer_progress(mailbox,value):
        progress.append(value)
        return offer(mailbox,value)
    monkeypatch.setattr(prepared_runtime,'read_catalog',read_catalog)
    monkeypatch.setattr(prepared_runtime.Mailbox,'offer_progress',offer_progress)
    receiver=SnapshotReceiver()
    with closing(CollectorService([home],target)) as service, \
            closing(CollectionClient([home],target)) as lease:
        lease.set_demand([(None,TID)],aggregate=True)
        complete(service)
        with closing(PreparedWriter([home],target,None,tmp_path/'quota.sqlite')) as writer:
            writer.set_selected([(None,TID)])
            publication(service,writer,receiver,lambda value:any(row['id']==TID for row in value['sessions']))
            writer.set_aggregate(7)
            aggregate=publication(service,writer,receiver,lambda value:value.get('aggregate_request')==7)
            assert len(captures)==1, 'A stable catalog was recaptured for each conversion chunk'
            totals=[value['total'] for value in progress if value.get('scope')=='aggregate'
                and value.get('id')==7 and value.get('stage')=='converting' and value.get('total')]
            assert totals and set(totals)=={65}
            result=all_calls(service,writer,receiver,aggregate)
            assert result['usage']['summary']['value']==66
            assert result['analysis']['response_count']==66
            assert result['analysis']['session_count']==66


def test_selected_update_is_published_between_history_chunks(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    at=history(home)
    target=tmp_path/'index.sqlite'
    chunk_committed=Event();source_updated=Event();selected_published=Event()
    convert=PreparedCohort.convert
    put=prepared_runtime.Mailbox.put
    def convert_history(coordinator,catalog,**kwargs):
        changes=convert(coordinator,catalog,**kwargs)
        try:
            for change in changes:
                first=catalog.get('authority')!='selected' and not chunk_committed.is_set()
                if first:
                    chunk_committed.set()
                    assert source_updated.wait(5), 'Selected source update was not supplied'
                yield change
                if first:
                    assert selected_published.is_set(), 'Full conversion advanced before publishing the selected update'
        finally:changes.close()
    def observe_publication(mailbox,message):
        if message['kind']=='snapshot' and any(row['id']==TID and row.get('calls')==2
                for row in message['value'].get('overlay_sessions',())):
            selected_published.set()
        return put(mailbox,message)
    monkeypatch.setattr(PreparedCohort,'convert',convert_history)
    monkeypatch.setattr(prepared_runtime.Mailbox,'put',observe_publication)
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
                write_session(home,TID,count=2,at=at)
                complete(service)
                source_updated.set()
                live=publication(service,writer,receiver,lambda value:
                    any(row['id']==TID and row['calls']==2 for row in value['overlay_sessions']))
                assert live.get('aggregate_request') is None
                assert selected_published.is_set()
                aggregate=publication(service,writer,receiver,lambda value:value.get('aggregate_request')==7)
                result=all_calls(service,writer,receiver,aggregate)
                assert result['usage']['summary']['value']==67
                assert result['analysis']['response_count']==67
                assert result['analysis']['session_count']==66
            finally:source_updated.set()
