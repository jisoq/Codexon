"""Exact page results and accounting survive deferred and reused reads."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
import time

from cachemonitor import prepared_store as storage
from cachemonitor.analysis_engine import AnalysisEngine
from cachemonitor.core import Session
from cachemonitor.prepared_ingest import PreparedCohort, prepare_owner, read_catalog, stage_owner
from cachemonitor.prepared_query import PreparedQuery


def sources(home='home'):
    result=[]
    for name,count in (('selected',160),('other',3)):
        session=Session(name,home,title=name,cwd='D:/project')
        for i in range(count):
            session.add_usage(100+i,name+str(i),dict(input_tokens=100+i,cached_input_tokens=20,
                output_tokens=10),'gpt-6-astra','request','medium',service_tier='Standard')
        result.append(deepcopy(session.view(300)))
    result[0]['history'][70].update(custom_scalar='visible-field',model_alert_confirmed=True)
    return result


def test_conversion_remains_correct_when_optional_inventory_cache_cannot_fit(tmp_path,monkeypatch):
    from cachemonitor import prepared_ingest as ingest
    from test_prepared_mode_dependencies import _catalog,_source
    values={'selected':_source('selected')}
    def read_source(client,owner,catalog,*,limits,cancel=None):
        source=deepcopy(values[owner[1]])
        return source,ingest._weight(source,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    monkeypatch.setattr(PreparedCohort,'SOURCE_CACHE_BYTES',512)
    with storage.PreparedStore(tmp_path/'uncached.sqlite') as store:
        for revision in (1,2):
            values['selected']['history'][0]['output']=revision*20
            catalog=_catalog(values,revision)
            changes=list(PreparedCohort(store,None,None).convert(catalog))
            assert changes[-1]['ready']
            reference=AnalysisEngine();reference.ingest(list(values.values()))
            with store.read_current() as view:
                assert view.record('home','selected','selected-response')['output']==revision*20
                expected=reference.sessions[('home','selected')]['prepared']['history']
                assert view.derived('home','selected','summary')['own']['cost']==sum(row['cost'] for row in expected)
            assert list(PreparedCohort(store,None,None).convert(catalog))==[]


def test_title_only_and_mixed_updates_do_not_reload_unchanged_facts(tmp_path,monkeypatch):
    from cachemonitor.usage_collection import CollectionChannel,CollectionClient,LiveCollection,empty_snapshot,CollectionCatalogUnavailable
    from cachemonitor.prepared_speed_writer import PreparedSpeedWriter
    from cachemonitor.prepared_ingest import preparation_signature
    from cachemonitor.prepared_ledger import PreparedLedger
    from cachemonitor.quota_cycles import QuotaLedger
    writer=CollectionChannel([tmp_path/'home'],tmp_path/'source.sqlite')
    writer.live=LiveCollection(writer.path,writer.scope)
    client=CollectionClient(writer.homes,writer.path)
    def request(**values):
        result=writer.live.catalog_request(writer.live.current,dict(homes=writer.homes,**values))
        if not result[0].get('ready'):raise CollectionCatalogUnavailable(result[0]['error'])
        return result
    client.channel._catalog_request=request
    values=sources(writer.homes[0]);owner=(writer.homes[0],'selected')
    def publish(number):
        now=time.time();snapshot=empty_snapshot(writer.homes,writer.path,now)
        snapshot.update(sessions=values,usage_collection_complete=True,last_usage_collection_success=now,request_activity=[])
        snapshot['index'].update(loading=False,usage_complete=True)
        writer.publish(snapshot,'display',number)
        return read_catalog(client)
    try:
        with storage.PreparedStore(tmp_path/'facts.sqlite') as store,closing(PreparedSpeedWriter(store,
                preparation=preparation_signature())) as speed,closing(QuotaLedger(tmp_path/'quota.sqlite')) as ledger:
            cohort=PreparedCohort(store,client,None,speed_writer=speed)
            first=publish(1);list(cohort.convert(first))
            adapter=PreparedLedger(ledger,store);before=adapter._sources()[1]
            original=client.selected_owner_stream
            def selected(key,**kwargs):
                assert key!=owner,'a display-only source must retain its prepared history'
                return original(key,**kwargs)
            monkeypatch.setattr(client,'selected_owner_stream',selected)
            values[0]['title']='renamed'
            values[1]['history'][0]['output']+=1
            list(cohort.convert(publish(2)))
            assert cohort.metrics['display_updates']==1
            assert adapter._sources()[1][owner]==before[owner]
            reference=AnalysisEngine();reference.ingest(values)
            with store.read_current() as view:
                assert list(view.iter_calls(owner))==[storage.public_record(row) for row in reference.sessions[owner]['prepared']['history']]
            # The committed inventory must remain usable across coordinator instances.
            def forbidden(*args,**kwargs):raise AssertionError('unchanged conversion reread its inventory')
            monkeypatch.setattr(storage.Snapshot,'inventory',forbidden)
            assert list(PreparedCohort(store,client,None,speed_writer=speed).convert(read_catalog(client)))==[]
    finally:
        client.close();writer.close()


def test_known_source_sync_does_not_stat_metadata_again(tmp_path,monkeypatch):
    from cachemonitor.collection_wake import SourceChanges
    changes=SourceChanges();changes.api=None
    calls=[]
    monkeypatch.setattr(changes,'_metadata_signature',lambda home:calls.append(home) or (None,)*3)
    changes.sync([tmp_path]);changes.sync([tmp_path])
    assert calls==[tmp_path.resolve()]
    changes.sync([tmp_path,tmp_path/'new'])
    assert calls==[tmp_path.resolve(),(tmp_path/'new').resolve()]
