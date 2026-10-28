from contextlib import closing

from cachemonitor import prepared_runtime
from cachemonitor.analysis_delivery import SnapshotReceiver
from cachemonitor.prepared_runtime import PreparedWriter
from cachemonitor.usage_collection import CollectionCatalogUnavailable, CollectionClient, CollectorService
from test_core import TID, fixture_home
from test_demand_loading import complete, publication
from test_prepared_live_scheduling import all_calls, history


def test_pending_selected_endpoint_does_not_restart_captured_history(tmp_path,monkeypatch):
    home,_=fixture_home(tmp_path)
    history(home)
    target=tmp_path/'index.sqlite'
    captures=[];pending=[]
    read=prepared_runtime.read_catalog
    def read_catalog(client,**kwargs):
        if client.active_demand['aggregate']:
            if kwargs.get('selected'):
                if not pending:
                    pending.append(True)
                    raise CollectionCatalogUnavailable('selected_pending')
            else:captures.append(True)
        return read(client,**kwargs)
    monkeypatch.setattr(prepared_runtime,'read_catalog',read_catalog)
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
            assert pending, 'The selected endpoint must be exercised between history chunks'
            assert len(captures)==1, 'A pending selected endpoint discarded the captured full history'
            result=all_calls(service,writer,receiver,aggregate)
            assert result['usage']['summary']['value']==66
            assert result['analysis']['response_count']==66
            assert result['analysis']['session_count']==66
