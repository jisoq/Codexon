"""Exact page results and accounting survive deferred and reused reads."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
import time

import pytest

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


def test_metadata_only_generation_reuses_admitted_inventory_and_publication(tmp_path, monkeypatch):
    from cachemonitor import prepared_delivery as delivery, prepared_ingest as ingest
    from test_prepared_mode_dependencies import _catalog, _source
    values = dict(selected=_source('selected'), other=_source('other'))

    def read_source(client, owner, catalog, *, limits, cancel=None):
        source = deepcopy(values[owner[1]])
        return source, ingest._weight(source, limits.owner_source_bytes)

    monkeypatch.setattr(ingest, 'read_source', read_source)
    with storage.PreparedStore(tmp_path/'metadata-only.sqlite') as store:
        catalog = _catalog(values)
        cohort = PreparedCohort(store, None, None)
        assert list(cohort.convert(catalog))[-1]['ready']
        publisher = delivery.PreparedPublisher(0)
        before = publisher.publish(dict(ts=1000, homes=['home']), store)
        expected = deepcopy(publisher.overlays)
        with store.read_current() as view:
            inventory = deepcopy(dict(cohort._inventory(view).items()))
        stage = store.begin(ingest.source_identity(catalog))
        latest = stage.publish(ingest.source_identity(catalog), ready=True)
        traversals = Counter()

        def observed(module, name):
            original = module._weight
            def weigh(*args, **kwargs):
                traversals[name] += 1
                return original(*args, **kwargs)
            monkeypatch.setattr(module, '_weight', weigh)

        observed(ingest, 'inventory')
        observed(delivery, 'publication')
        with store.read_current() as view:
            assert dict(cohort._inventory(view).items()) == inventory
        publisher.adopt(store, ())
        assert traversals == {}
        after = publisher.publish(dict(ts=1001, homes=['home']), store)
        assert before['prepared_token'] != after['prepared_token']
        assert after['prepared_token'] == (latest.instance, latest.generation)
        assert publisher.overlays == expected
        assert after['sessions'] == after['overlay_sessions'] == []


def test_exact_key_projection_keeps_latest_version_with_bounded_reads(tmp_path):
    from dataclasses import replace
    from test_prepared_analysis import OWNER, calls, publish
    path=tmp_path/'latest-key.sqlite'
    rows=[dict(calls()[0],key=f'row-{i}',ts=i,session_ordinal=i) for i in range(512)]
    with storage.PreparedStore(path) as store:
        publish(store,1,rows)
        rows[-1]['output']=77
        publish(store,2,rows)
        limits=replace(store.limits,read_vm_steps=1000)
        with storage.PreparedReader(path,limits) as reader,reader.pin() as view:
            page=view.projected_calls(OWNER,('key','output'),keys=('row-511',))
            assert page.rows==({'key':'row-511','output':77},)
            assert page.complete


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

def test_speed_admission_reuses_only_exact_raw_frontiers_in_one_pin(tmp_path):
    from test_prepared_analysis import OWNER, calls, publish
    path = tmp_path/'speed-admission.sqlite'
    with storage.PreparedStore(path) as store:
        original = publish(store, 1, calls())
        with store.read_current() as view:
            expected = list(view.iter_speed_rows({OWNER: None}))
            assert view.speed_work['admission_reads'] == 1
            assert list(view.iter_speed_rows({OWNER: None})) == expected
            assert view.speed_work['admission_hits'] == 1
            tail = list(view.iter_speed_rows({OWNER: 0}))
            assert [row[1] for row in tail] == [1, 2]
            assert view.speed_work['admission_reads'] == 1
            assert list(view.iter_speed_rows({OWNER: 0})) == tail
            assert view.speed_work['admission_hits'] == 1
            assert view.db.execute("SELECT count(*) FROM sqlite_temp_master WHERE name='speed_selection'").fetchone()[0] == 0
        assert not view._speed_admissions
        stage = store.begin(('source', 2))
        dependency = dict.fromkeys(storage.DEPENDENCIES, 1)
        dependency['source_digest'] = '2'*64
        stage.owner(*OWNER, dict(home=OWNER[0], id=OWNER[1]), dependency, source_ordinal=0)
        stage.calls(OWNER, [dict(calls()[0], ts=300), calls()[2]], reconcile=True)
        with stage.read_snapshot() as staged:
            changed = list(staged.iter_speed_rows({OWNER: None}))
            assert [row[1] for row in changed] == [2, 0]
            assert staged.speed_work['admission_reads'] == 1
            assert list(staged.iter_speed_rows({OWNER: None})) == changed
            assert staged.speed_work['admission_hits'] == 1
        stage.abort()
        assert store.token == original
        with store.read_current() as current:
            assert list(current.iter_speed_rows({OWNER: None})) == expected
            assert current.speed_work['admission_reads'] == 1


def test_speed_admission_sums_cached_owners_and_eviction_changes_no_rows(tmp_path):
    from dataclasses import replace
    from test_prepared_analysis import calls
    owners = (('home', 'a'), ('home', 'b'))
    path = tmp_path/'speed-total.sqlite'
    with storage.PreparedStore(path) as store:
        stage = store.begin(('source', 1))
        for ordinal, owner in enumerate(owners):
            dependency = {name: 1 for name in storage.DEPENDENCIES}
            dependency['source_digest'] = '1'*64
            stage.owner(*owner, dict(home=owner[0], id=owner[1]), dependency, source_ordinal=ordinal)
            stage.calls(owner, [dict(row, key=owner[1]+row['key']) for row in calls()], reconcile=True)
        stage.publish(('source', 1), ready=True)
        with store.read_current() as view:
            expected = list(view.iter_speed_rows(dict.fromkeys(owners)))
            total = view.speed_work['sort_estimated_bytes']
            active = view.speed_work['selection_input_bytes'] + view.speed_work['selection_database_bytes']
        limits = replace(store.limits, speed_sort_bytes=total-1)
        with storage.PreparedReader(path, limits) as reader, reader.pin() as view:
            for owner in owners:
                assert len(list(view.iter_speed_rows({owner: None}))) == 3
            with pytest.raises(storage.CapacityExceeded):
                list(view.iter_speed_rows(dict.fromkeys(owners)))
            assert view.speed_work['admission_hits'] == 2
            assert view.speed_work['rows_yielded'] == 0
        limits = replace(store.limits, speed_temp_bytes=active)
        with storage.PreparedReader(path, limits) as reader, reader.pin() as view:
            assert list(view.iter_speed_rows(dict.fromkeys(owners))) == expected
            assert not view._speed_admissions
        limits = replace(store.limits, speed_range_rows=1)
        with storage.PreparedReader(path, limits) as reader, reader.pin() as view:
            for owner in (*owners, owners[0]):
                assert list(view.iter_speed_rows({owner: None})) == [row for row in expected if row[0] == owner]
                assert view.speed_work['admission_reads'] == 1
                assert len(view._speed_admissions) <= 1
                assert view._speed_admission_bytes + storage.sys.getsizeof(view._speed_admissions) < limits.speed_temp_bytes


def test_cached_speed_admission_keeps_cancellation_and_failed_queries_uncached(tmp_path):
    from dataclasses import replace
    from threading import Event
    from test_prepared_analysis import OWNER, calls, publish
    path = tmp_path/'speed-cancel.sqlite'
    rows = [dict(calls()[0], key=f'row-{i}', ts=i, session_ordinal=i) for i in range(200)]
    with storage.PreparedStore(path) as store:
        publish(store, 1, rows)
        cancelled = Event()
        with store.read_current(cancel=cancelled) as view:
            assert len(list(view.iter_speed_rows({OWNER: None}))) == 200
            cancelled.set()
            with pytest.raises(storage.ReadCancelled):
                list(view.iter_speed_rows({OWNER: None}))
            assert not view._speed_admissions
        limits = replace(store.limits, read_vm_steps=1000)
        with storage.PreparedReader(path, limits) as reader, reader.pin() as view:
            with pytest.raises(storage.ReadCancelled):
                list(view.iter_speed_rows({OWNER: None}))
            assert not view._speed_admissions

def test_speed_selection_evicts_memo_before_each_larger_batch(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from dataclasses import replace
    from test_prepared_analysis import calls
    home = 'home/'+'h'*1100
    owners = tuple((home, f'owner-{number:02d}') for number in range(40))
    path = tmp_path/'speed-selection-growth.sqlite'
    with storage.PreparedStore(path) as store:
        stage = store.begin(('source', 1))
        for ordinal, owner in enumerate(owners):
            dependency = dict.fromkeys(storage.DEPENDENCIES, 1)
            dependency['source_digest'] = f'{ordinal:064x}'
            stage.owner(*owner, dict(home=home, id=owner[1]), dependency, source_ordinal=ordinal)
            stage.calls(owner, [dict(row, key=owner[1]+row['key']) for row in calls()], reconcile=True)
        stage.publish(('source', 1), ready=True)
        limits = replace(store.limits, owners_per_batch=4, speed_temp_bytes=64*1024)
        with storage.PreparedReader(path, limits) as reader, reader.pin() as view:
            for owner in owners[:10]:
                assert len(list(view.iter_speed_rows({owner: None}))) == 3
            assert len(view._speed_admissions) == 10
            cursor = view._cursor
            seen = []

            @contextmanager
            def observed(sql, args=()):
                if sql.startswith('WITH requested'):
                    memo = view._speed_admissions
                    retained = (storage.sys.getsizeof(memo)+view._speed_admission_bytes+
                                storage.sys.getsizeof(view._speed_admission_bytes)) if memo is not None else 0
                    page_size = view.db.execute('PRAGMA temp.page_size').fetchone()[0]
                    cap = view.db.execute('PRAGMA temp.max_page_count').fetchone()[0]*page_size
                    assert cap + retained + view.speed_work['selection_input_bytes'] <= limits.speed_temp_bytes
                    seen.append(len(memo or ()))
                with cursor(sql, args) as result:
                    yield result
                if sql.startswith('WITH requested'):
                    actual = view.db.execute('PRAGMA temp.page_count').fetchone()[0]*page_size
                    assert actual + retained + view.speed_work['selection_input_bytes'] <= limits.speed_temp_bytes

            monkeypatch.setattr(view, '_cursor', observed)
            result = list(view.iter_speed_rows(dict.fromkeys(owners)))
            assert len(result) == 120 and len(seen) == 10
            assert seen[0] < 10
            assert view.speed_work['selected_rows'] == 120
