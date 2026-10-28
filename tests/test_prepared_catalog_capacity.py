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
