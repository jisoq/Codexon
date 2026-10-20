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


def query(home='home'):
    return dict(page=2,start=0,end=1000,now=1000,period='custom',presentation=True,
        home=home,sid='selected',explorer=dict(session=[home,'selected'],view='calls',timeline=True,context=None),
        table_windows={'records':dict(start=0,size=64)})


def same_explorer(actual,expected):
    assert {k:v for k,v in actual.items() if k!='metadata'}=={k:v for k,v in expected.items() if k!='metadata'}
    if actual.get('metadata') is not None:
        for name in ('home','id','title','cwd','project_name','source','parent_thread_id'):
            assert actual['metadata'].get(name)==expected['metadata'].get(name)


def test_scoped_query_defers_bodies_and_keeps_unaffected_results(tmp_path,monkeypatch):
    values=sources();counts=Counter()
    original=storage.Snapshot.catalog
    def catalog(view,**kwargs):
        page=original(view,**kwargs);counts['owners']+=len(page.rows);return page
    decode=storage.Snapshot._decode_call
    def body(*args,**kwargs):counts['bodies']+=1;return decode(*args,**kwargs)
    monkeypatch.setattr(storage.Snapshot,'catalog',catalog)
    monkeypatch.setattr(storage.Snapshot,'_decode_call',body)
    deps={name:1 for name in storage.DEPENDENCIES};deps['source_digest']='a'*64
    with storage.PreparedStore(tmp_path/'facts.sqlite') as store,closing(PreparedQuery(store.path)) as actor:
        stage=store.begin(1)
        for ordinal,value in enumerate(values):stage_owner(stage,prepare_owner(value,owns=lambda *_:True),deps,ordinal)
        stage.catalog_order(((('home',v['id']),i) for i,v in enumerate(values)))
        token=stage.publish(1,ready=True)
        q=query();q['explorer']['call_identity']=['home','selected',values[0]['history'][-1]['key']]
        message=dict(kind='query',id=1,logical='history',query=q)
        reference=AnalysisEngine();reference.ingest(values)
        actual=actor(message,token,lambda:False)['result']
        same_explorer(actual['explorer'],reference.page_query(q)['explorer'])
        assert counts==dict(owners=1,bodies=64)
        changed=deepcopy(values[1]);changed['title']='unrelated'
        stage=store.begin(2);stage_owner(stage,prepare_owner(changed,owns=lambda *_:True),deps,1)
        newer=stage.publish(2,ready=True);counts.clear()
        message=dict(message,_prepared_changes=[((token.instance,token.generation),
            (newer.instance,newer.generation),(('home','other'),),())])
        refreshed=actor(message,newer,lambda:False)['result']
        assert refreshed['explorer']==actual['explorer']
        assert refreshed['revision']==newer.generation
        assert counts=={}
        # A broader query must restore the omitted source's metadata.
        global_query=dict(q,home='',sid='',explorer=dict(view='sessions'))
        reference.ingest([values[0],changed])
        actual=actor(dict(message,query=global_query),newer,lambda:False)['result']
        same_explorer(actual['explorer'],reference.page_query(global_query)['explorer'])


def test_legacy_projection_preserves_fields_without_eager_history_upgrade(tmp_path,monkeypatch):
    values=sources();owner=('home','selected')
    counts=Counter();project_rows=storage.Snapshot.projected_calls;decode=storage.Snapshot._decode_call
    def projection(view,*args,**kwargs):
        before=getattr(view,'history_legacy_rows',0)
        page=project_rows(view,*args,**kwargs)
        counts['legacy']+=getattr(view,'history_legacy_rows',0)-before
        return page
    def body(*args,**kwargs):counts['bodies']+=1;return decode(*args,**kwargs)
    monkeypatch.setattr(storage.Snapshot,'projected_calls',projection)
    monkeypatch.setattr(storage.Snapshot,'_decode_call',body)
    deps={name:1 for name in storage.DEPENDENCIES};deps['source_digest']='a'*64
    with storage.PreparedStore(tmp_path/'legacy.sqlite') as store,closing(PreparedQuery(store.path)) as actor:
        stage=store.begin(1);stage_owner(stage,prepare_owner(values[0],owns=lambda *_:True),deps,0)
        token=stage.publish(1,ready=True)
        store.db.execute('UPDATE calls SET types=substr(types,1,?)',(len(storage.METRICS),))
        q=query();q['explorer']['filters']=['model_mismatch']
        message=dict(kind='query',id=1,logical='history',query=q)
        legacy=actor(message,token,lambda:False)['result']['explorer']
        assert counts==dict(legacy=160)
        while store.maintain():pass
        assert store.token==token
        projected=actor(message,token,lambda:False)['result']['explorer']
        reference=AnalysisEngine();reference.ingest(values[:1])
        assert legacy==projected
        same_explorer(projected,reference.page_query(q)['explorer'])
        assert 'custom_scalar' in projected['available']
        assert store.db.execute('SELECT count(*) FROM calls WHERE length(types)=?',(len(storage.METRICS),)).fetchone()[0]==160
        # A different filter/range cannot use the previous result or selection index.
        # Its legacy headers nevertheless remain valid for the same call versions.
        counts.clear();q=query();q['start']=120;q['table_windows']['records']['start']=64
        fresh=actor(dict(message,query=q),token,lambda:False)['result']['explorer']
        same_explorer(fresh,reference.page_query(q)['explorer'])
        assert counts['legacy']==0 and counts['bodies']==64


def test_foreign_detail_refreshes_when_its_owner_changes(tmp_path):
    values=sources();deps={name:1 for name in storage.DEPENDENCIES};deps['source_digest']='a'*64
    with storage.PreparedStore(tmp_path/'foreign-detail.sqlite') as store,closing(PreparedQuery(store.path)) as actor:
        stage=store.begin(1)
        for ordinal,value in enumerate(values):stage_owner(stage,prepare_owner(value,owns=lambda *_:True),deps,ordinal)
        token=stage.publish(1,ready=True)
        q=query();q['explorer']['call_identity']=['home','other',values[1]['history'][0]['key']]
        message=dict(kind='query',id=1,logical='history',query=q)
        before=actor(message,token,lambda:False)['result']['explorer']
        assert before['detail']['output']==10
        changed=deepcopy(values[1]);changed['history'][0]['output']=99
        stage=store.begin(2);stage_owner(stage,prepare_owner(changed,owns=lambda *_:True),deps,1)
        newer=stage.publish(2,ready=True)
        message=dict(message,_prepared_changes=[((token.instance,token.generation),
            (newer.instance,newer.generation),(('home','other'),),())])
        actual=actor(message,newer,lambda:False)['result']['explorer']
        reference=AnalysisEngine();reference.ingest([values[0],changed])
        same_explorer(actual,reference.page_query(q)['explorer'])
        assert actual['detail']['output']==99


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


def test_event_list_and_selected_detail_share_only_the_requested_bodies(tmp_path,monkeypatch):
    value=sources()[0]
    for i,row in enumerate(value['history']):
        row.update(input=1000,cached=300 if 80<=i<83 else 900)
    material=prepare_owner(value,owns=lambda *_:True)
    event=material['summary']['own']['cache_health']['events'][0]
    q=query();q['explorer'].update(event=event['id'],
        call_identity=['home','selected',event['occurrence_keys'][0]])
    counts=Counter();decode=storage.Snapshot._decode_call
    def body(view,row,metadata,**kwargs):
        result=decode(view,row,metadata,**kwargs)
        if not kwargs.get('metrics'):counts[result['key']]+=1
        return result
    monkeypatch.setattr(storage.Snapshot,'_decode_call',body)
    deps={name:1 for name in storage.DEPENDENCIES};deps['source_digest']='a'*64
    with storage.PreparedStore(tmp_path/'event.sqlite') as store,closing(PreparedQuery(store.path)) as actor:
        stage=store.begin(1);stage_owner(stage,material,deps,0);token=stage.publish(1,ready=True)
        result=actor(dict(kind='query',id=1,logical='history',query=q),token,lambda:False)['result']['explorer']
        reference=AnalysisEngine();reference.ingest([value]);same_explorer(result,reference.page_query(q)['explorer'])
        keys={row['key'] for row in result['records']['rows']+result['event_rows']+[result['detail']]}
        assert set(counts)==keys
        assert max(counts.values())==1


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
