import json

import pytest

from cachemonitor.prepared_ingest import _weight, read_catalog
from cachemonitor.prepared_store import PreparedError, PreparedStore
from cachemonitor.usage_collection import CATALOG_PAGE_ROWS


class CatalogPages:
    def __init__(self, home, count):
        self.home, self.count = home, count
        self.selected = self.key(count-1)
        self.active_demand = dict(selected=[[None,self.selected[1]]],aggregate=False,evidence=[])
        self.header = dict(ts=100,homes=[home],demand=self.active_demand,catalog_complete=True,
            usage_collection_complete=True,index=dict(loading=False,usage_complete=True))
        self.legacy_tree_bytes = 0
        self.requests = 0

    def key(self, ordinal):
        return self.home,f'{ordinal:08x}-0000-4000-8000-000000000000'

    def poll_catalog(self, *, cursor=None, **kwargs):
        start = 0 if cursor is None else cursor
        total = 2*self.count+1
        end = min(total,start+CATALOG_PAGE_ROWS)
        membership = dict(session_catalog=[],demand_members=[])
        owners = {}
        for position in range(start,end):
            if position < self.count:
                membership['session_catalog'].append([*self.key(position),None])
            elif position == self.count:
                membership['demand_members'].append(list(self.selected))
            else:
                ordinal = position-self.count-1
                key = self.key(ordinal)
                entry = dict(home=key[0],sid=key[1],revision=1,body_digest=f'{ordinal+1:064x}',
                    state=dict(collection_complete=True),metadata_available=True,
                    owner_lineage=dict(parent_thread_id=None))
                owners[key] = entry
                self.legacy_tree_bytes += _weight((key,entry),1<<30)+96
        self.requests += 1
        return dict(source='live',scope='catalog-scale',path='source.sqlite',selected_homes=[self.home],
            publication=('catalog-scale',1),catalog_identity=('catalog-scale',1),header=self.header,
            membership_complete=True,include_metadata=True,metadata_kind='lineage',
            membership_format='paged-v1',membership=membership,owners=owners,
            cursor=end if end<total else None)

    def selected_owner_stream(self, *args, **kwargs):
        raise AssertionError('catalog metadata must not decode source bodies')


@pytest.mark.extended
def test_default_catalog_accepts_more_than_the_retired_32mib_owner_tree(tmp_path):
    client = CatalogPages(str(tmp_path/'home'),24_000)
    with PreparedStore(tmp_path/'prepared.sqlite') as store:
        catalog = read_catalog(client,store=store)
        try:
            assert client.legacy_tree_bytes > 32*1024*1024
            assert catalog['membership_complete']
            assert list(catalog['owners']) == [client.selected]
            assert catalog['owners'][client.selected]['source_ordinal'] == 0
            assert len(catalog['mode_sources']) == client.count-1
            assert set(catalog['selected_members']) == {client.selected}
            assert catalog['header']['selected_resolutions'] == [[client.selected[1],1]]
            assert 'session_catalog' not in catalog['header']
            print(json.dumps(dict(owners=client.count,pages=client.requests,
                legacy_tree_weight_bytes=client.legacy_tree_bytes,complete=True)))
        finally:
            catalog.close()
        with pytest.raises(PreparedError):
            len(catalog['owners'])


@pytest.mark.parametrize('cancel_inside_sql',[True,False],ids=['cancel','complete'])
def test_catalog_finish_scopes_sql_cancellation_and_preserves_previous_membership(tmp_path,cancel_inside_sql):
    class ExecutingConnection:
        def __init__(self,connection):
            self.connection=connection
            self.executing=False

        def execute(self,*args,**kwargs):
            self.executing=True
            try:return self.connection.execute(*args,**kwargs)
            finally:self.executing=False

        def __getattr__(self,name):
            return getattr(self.connection,name)

    home=str(tmp_path/'home')
    with PreparedStore(tmp_path/'prepared.sqlite') as store:
        previous_client=CatalogPages(home,2)
        previous=read_catalog(previous_client,store=store)
        connection=store.db
        probe=ExecutingConnection(connection)
        store.db=probe
        incoming=None
        armed=cancel_inside_sql
        cancelled=False
        sql_cancellations=0

        def cancel():
            nonlocal cancelled,sql_cancellations
            if armed and probe.executing:
                sql_cancellations+=1
                cancelled=True
            return cancelled

        try:
            client=CatalogPages(home,512)
            if cancel_inside_sql:
                with pytest.raises(InterruptedError,match='cancelled'):
                    incoming=read_catalog(client,store=store,cancel=cancel)
                assert sql_cancellations>0
            else:
                incoming=read_catalog(client,store=store,cancel=cancel)
                assert list(incoming['owners']) == [client.selected]
                assert incoming['membership_complete']

            armed=True
            total=probe.execute('WITH RECURSIVE values_to_sum(value) AS ('
                'VALUES(1) UNION ALL SELECT value+1 FROM values_to_sum WHERE value<2000) '
                'SELECT sum(value) FROM values_to_sum').fetchone()[0]
            assert total == 2_001_000
            assert list(previous['owners']) == [previous_client.selected]
            assert set(previous['selected_members']) == {previous_client.selected}
            assert previous['membership_complete']
        finally:
            if incoming is not None:incoming.close()
            store.db=connection
            previous.close()
