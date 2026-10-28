from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace

import pytest

from cachemonitor import prepared_ingest as ingest
from cachemonitor import prepared_store as storage
from cachemonitor.core import Session
from cachemonitor.prepared_analysis import PreparedAnalysisEngine


def source(sid, *, parent=None, response=None, amount=100):
    session = Session(sid, 'home', title=sid)
    session.add_usage(100, response or sid+'-response',
        dict(input_tokens=amount, cached_input_tokens=0, cache_write_input_tokens=0,
             output_tokens=20, reasoning_output_tokens=5),
        'gpt-6-astra', sid+'-turn', 'high', service_tier='Standard')
    result = session.view(400)
    result.update(usage_revision=1, parent_thread_id=parent, collection_complete=True)
    return result


def catalog(sources, revision=1, *, family=None, evidence=()):
    selected = family is not None
    financial = tuple(sources) if family is None else tuple(family)
    entries = {('home', sid): dict(body_digest=sha256(repr(sources[sid]).encode()).hexdigest(),
        source_ordinal=ordinal*3+2 if selected else ordinal,
        state=dict(collection_complete=True),
        owner_lineage=dict(parent_thread_id=sources[sid].get('parent_thread_id')))
        for ordinal, sid in enumerate((*financial, *evidence))}
    result = dict(scope='scope', path='source', selected_homes=['home'],
        catalog_identity=('source', revision), membership_complete=True,
        header=dict(usage_collection_complete=True),
        owners={('home', sid): entries[('home', sid)] for sid in financial},
        mode_sources=entries)
    if selected:
        result.update(authority='selected', membership_format='selected-v1',
            selection_request='selected-'+','.join(financial), aggregate_complete=False,
            selected_members={('home', sid) for sid in financial})
    return result


@pytest.fixture
def source_reader(monkeypatch):
    sources = {}
    def read_source(client, owner, captured, *, limits, **kwargs):
        value = deepcopy(sources[owner[1]])
        return value, ingest._weight(value, limits.owner_source_bytes)
    monkeypatch.setattr(ingest, 'read_source', read_source)
    return sources


def forbidden_population(*args, **kwargs):
    raise AssertionError('selected preparation enumerated unrelated prepared owners')


def test_selected_catalog_uses_captured_family_and_manifest_positions(tmp_path):
    owners = {sid: source(sid, parent='root' if sid=='child' else None)
              for sid in ('root', 'child', 'context')}
    page = catalog(owners, family=('child', 'root'), evidence=('context',))
    page.update(owners=page['mode_sources'], cursor=None,
        membership=dict(session_catalog=[['home','root',None], ['home','child','root'],
                                         ['home','context',None]],
                        demand_members=[['home','root'], ['home','child']],
                        matches=[[None,'root',['home']]]))
    page['header']['demand'] = dict(selected=[[None,'root']], aggregate=False, evidence=[])
    client = SimpleNamespace(active_demand=page['header']['demand'],
        poll_selection=lambda **kwargs: deepcopy(page), poll_catalog=forbidden_population)
    with storage.PreparedStore(tmp_path/'catalog.sqlite') as store:
        selected = ingest.read_catalog(client, store=store)
        try:
            assert selected['membership_complete'] and not selected['aggregate_complete']
            assert list(selected['owners']) == [('home','child'), ('home','root')]
            assert [row['source_ordinal'] for row in selected['owners'].values()] == [2,5]
            assert set(selected['selected_members']) == {('home','root'), ('home','child')}
            assert selected['header']['selected_resolutions'] == [['root',1]]
            assert ('home','context') not in selected['owners']
        finally:
            selected.close()


def test_selected_patch_preserves_retained_facts_and_global_order_without_inventory(
        tmp_path, monkeypatch, source_reader):
    sources = source_reader
    sources.update({sid: source(sid, parent='root' if sid=='old-child' else None)
                    for sid in ('unrelated','root','old-child','child')})
    with storage.PreparedStore(tmp_path/'patch.sqlite') as store:
        list(ingest.PreparedCohort(store,None,None).convert(catalog(sources)))
        with store.read_current() as view:
            ranks = {sid:view.owner('home',sid)['owner_ordinal'] for sid in sources}
            unrelated = view.record('home','unrelated','unrelated-response')
        sources['root'] = source('root', amount=101)
        selected = catalog(sources,2,family=('child','root'))
        inventory = storage.Snapshot.inventory
        def scoped_inventory(view, *, owners=None, **kwargs):
            if owners is None:
                forbidden_population()
            return inventory(view, owners=owners, **kwargs)
        with monkeypatch.context() as guard:
            guard.setattr(storage.Snapshot, 'inventory', scoped_inventory)
            guard.setattr(storage.Snapshot, 'inventory_scan', forbidden_population)
            updates = list(ingest.PreparedCohort(store,None,None).convert(selected))
        assert updates[-1]['ready']
        assert all(not update['removed'] for update in updates)
        with store.read_current() as view:
            assert {sid:view.owner('home',sid)['owner_ordinal'] for sid in sources} == ranks
            assert view.record('home','unrelated','unrelated-response') == unrelated
            assert view.record('home','old-child','old-child-response')['input'] == 100
            assert view.record('home','root','root-response')['input'] == 101
        engine = PreparedAnalysisEngine(store.path,store.token)
        try:
            assert list(engine.sessions) == [('home','child'), ('home','root')]
        finally:
            engine.close()


def test_selected_query_uses_captured_membership_and_tie_order(tmp_path, source_reader):
    sources = source_reader
    sources.update(root=source('root'), child=source('child',parent='root'),
                   omitted=source('omitted',parent='root'))
    with storage.PreparedStore(tmp_path/'order.sqlite') as store:
        list(ingest.PreparedCohort(store,None,None).convert(catalog(sources)))
        list(ingest.PreparedCohort(store,None,None).convert(catalog(sources,2,family=('child','root'))))
        engine = PreparedAnalysisEngine(store.path,store.token)
        try:
            result = engine.query(dict(page=3,start=0,end=1000,now=1000,period='all',
                include_whole_history=False,home='home',sid='root'))['analysis']
            assert [row['id'] for row in result['sessions']] == ['child','root']
            assert [row['sid'] for row in result['responses']] == ['child','root']
            assert result['totals']['input'] == 200
        finally:
            engine.close()


def test_selected_correction_keeps_outside_competitor_and_updates_its_facts_atomically(
        tmp_path, source_reader):
    sources = source_reader
    sources.update(a=source('a',response='shared'), z=source('z',response='shared'))
    with storage.PreparedStore(tmp_path/'competition.sqlite') as store:
        list(ingest.PreparedCohort(store,None,None).convert(catalog(sources)))
        with store.read_current() as view:
            assert view.record('home','a','shared') is not None
            assert view.record('home','z','shared') is None
        sources['a'] = source('a',response='new')
        selected = catalog(sources,2,family=('a',),evidence=('z',))
        updates = list(ingest.PreparedCohort(store,None,None).convert(selected))
        assert set(updates[-1]['changed']) == {('home','a'),('home','z')}
        with store.read_current() as view:
            assert view.record('home','a','shared') is None
            assert view.record('home','z','shared')['input'] == 100
        engine = PreparedAnalysisEngine(store.path,store.token)
        try:
            assert list(engine.sessions) == [('home','a')]
        finally:
            engine.close()


def test_new_selected_ranks_remain_stable_and_pinned_order_survives_switches(
        tmp_path, source_reader):
    sources = source_reader
    sources.update(a=source('a'), b=source('b'))
    with storage.PreparedStore(tmp_path/'ranks.sqlite') as store:
        list(ingest.PreparedCohort(store,None,None).convert(catalog(sources)))
        sources.update(c=source('c'), d=source('d'))
        selected = catalog(sources,2,family=('d','c'))
        list(ingest.PreparedCohort(store,None,None).convert(selected))
        with store.read_current() as view:
            assert [view.owner('home',sid)['owner_ordinal'] for sid in ('a','b','d','c')] == [0,1,2,3]
        with storage.PreparedReader(store.path) as reader, reader.pin(store.token) as pinned:
            assert [row['owner'] for row in ingest.pages(pinned.catalog)] == [('home','d'),('home','c')]
            list(ingest.PreparedCohort(store,None,None).convert(selected))
            list(ingest.PreparedCohort(store,None,None).convert(catalog(sources,3)))
            list(ingest.PreparedCohort(store,None,None).convert(catalog(sources,4,family=('b','a'))))
            for _ in range(30):
                if not store.maintain(rows=32):
                    break
            assert [row['owner'] for row in ingest.pages(pinned.catalog)] == [('home','d'),('home','c')]
        with store.read_current() as view:
            assert [view.owner('home',sid)['owner_ordinal'] for sid in sources] == [0,1,2,3]
            assert all(view.record('home',sid,sid+'-response') for sid in sources)
