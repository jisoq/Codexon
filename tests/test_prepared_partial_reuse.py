from contextlib import closing
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cachemonitor import prepared_ingest as ingest
from cachemonitor import prepared_store as storage
from cachemonitor.prepared_analysis import PreparedAnalysisEngine
from cachemonitor.prepared_ledger import PreparedLedger
from cachemonitor.quota_cycles import QuotaLedger
from cachemonitor.usage_collection import CollectionCatalogUnavailable
from test_prepared_ledger_reuse import _envelope
from test_selected_preparation import catalog, source, source_reader


def client_for(sources, available, revision):
    page=catalog(sources,revision)
    for owner,entry in page['owners'].items():
        entry['state']['collection_complete']=owner[1] in available
    demand=dict(selected=[[None,next(iter(sources))]],aggregate=True,evidence=[])
    complete=len(available)==len(sources)
    page.update(publication=('source',revision),cursor=None,membership_format='paged-v1',
        membership=dict(session_catalog=[['home',sid,value.get('parent_thread_id')] for sid,value in sources.items()],
                        demand_members=[['home',sid] for sid in sources]))
    page['header'].update(demand=demand,usage_collection_complete=complete,
        index=dict(loading=not complete,usage_complete=complete))
    requested=[]
    return SimpleNamespace(active_demand=demand,poll_catalog=lambda **kwargs:deepcopy(page),
        request_sources=lambda owners:requested.extend(owners),requested=requested)


def prepared_sessions(store):
    engine=PreparedAnalysisEngine(store.path,store.token)
    try:return list(engine.sessions)
    finally:engine.close()


def test_partial_aggregate_preserves_prepared_history_and_reuses_it_when_complete(
        tmp_path,monkeypatch,source_reader):
    sources=source_reader
    sources.update({sid:source(sid) for sid in ('a','b','c')})
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store,closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger:
        first=ingest.read_catalog(client_for(sources,set(sources),1),store=store)
        try:list(ingest.PreparedCohort(store,None,None).convert(first))
        finally:first.close()
        adapter=PreparedLedger(ledger,store)
        list(adapter.sync(_envelope()))
        with store.read_current() as view:
            saved={sid:view.owner('home',sid) for sid in sources}
        reads=[]
        read_source=ingest.read_source
        def observed(client,owner,captured,**kwargs):
            reads.append(owner)
            return read_source(client,owner,captured,**kwargs)
        monkeypatch.setattr(ingest,'read_source',observed)
        partial=ingest.read_catalog(client_for(sources,{'a'},2),store=store)
        try:
            assert partial['membership_complete'] and not partial['aggregate_complete']
            changes=list(ingest.PreparedCohort(store,None,None).convert(partial))
            assert all(not change['removed'] for change in changes)
            assert prepared_sessions(store)==[('home','a')]
            with store.read_current() as view:
                assert {sid:view.owner('home',sid) for sid in sources}==saved
            envelope=dict(_envelope(),collection_partial=True,usage_collection_complete=False)
            list(adapter.sync(envelope,owners=partial['owners']))
            assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0]==3
        finally:partial.close()
        restored=ingest.read_catalog(client_for(sources,set(sources),3),store=store)
        try:list(ingest.PreparedCohort(store,None,None).convert(restored))
        finally:restored.close()
        assert reads==[], 'available prepared bodies must survive a temporary partial aggregate'
        assert prepared_sessions(store)==[('home','a'),('home','b'),('home','c')]
        list(adapter.sync(_envelope()))
        assert ledger.db.execute('SELECT count(*) FROM calls').fetchone()[0]==3
        del sources['c']
        deleted=ingest.read_catalog(client_for(sources,set(sources),4),store=store)
        try:
            changes=list(ingest.PreparedCohort(store,None,None).convert(deleted))
            assert {owner for change in changes for owner in change['removed']}=={('home','c')}
        finally:deleted.close()
        assert prepared_sessions(store)==[('home','a'),('home','b')]


def test_partial_aggregate_waits_for_retained_canonical_rival(tmp_path,source_reader):
    sources=source_reader
    sources.update(a=source('a',response='shared'),z=source('z',response='shared'))
    with storage.PreparedStore(tmp_path/'rival.sqlite') as store:
        first=ingest.read_catalog(client_for(sources,set(sources),1),store=store)
        try:list(ingest.PreparedCohort(store,None,None).convert(first))
        finally:first.close()
        sources['a']=source('a',response='new')
        client=client_for(sources,{'a'},2)
        partial=ingest.read_catalog(client,store=store)
        try:
            with pytest.raises(CollectionCatalogUnavailable,match='canonical_sources_pending'):
                list(ingest.PreparedCohort(store,client,None).convert(partial))
            assert client.requested==[('home','z')]
            with store.read_current() as view:
                assert view.record('home','a','shared')['input']==100
                assert view.record('home','z','shared') is None
        finally:partial.close()
        complete=ingest.read_catalog(client_for(sources,set(sources),3),store=store)
        try:list(ingest.PreparedCohort(store,None,None).convert(complete))
        finally:complete.close()
        with store.read_current() as view:
            assert view.record('home','a','shared') is None
            assert view.record('home','a','new')['input']==100
            assert view.record('home','z','shared')['input']==100
