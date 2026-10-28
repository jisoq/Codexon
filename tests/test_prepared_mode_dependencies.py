"""Only inheritance-capable rows depend on changing ancestor source bodies."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest

from cachemonitor import prepared_ingest as ingest
from cachemonitor import prepared_store as storage
from cachemonitor.core import Session
from cachemonitor.pricing import request_tier
from cachemonitor.quota_cycles import QuotaLedger


def _source(sid, mode='Standard', *, parent=None, at=100, model='gpt-6-astra', **row_fields):
    session = Session(sid, 'home', title=sid)
    session.add_usage(at, sid+'-response', dict(input_tokens=100, cached_input_tokens=80,
        cache_write_input_tokens=0, output_tokens=20, reasoning_output_tokens=5),
        model, sid+'-turn', 'high', service_tier=mode)
    source = session.view(400)
    source.update(usage_revision=1, parent_thread_id=parent, collection_complete=True)
    source['history'][0].update(row_fields)
    return source


def _catalog(sources, revision=1):
    return dict(scope='scope', path='source', selected_homes=['home'],
        catalog_identity=('source', revision), membership_complete=True,
        owners={('home', sid): dict(body_digest=sha256(repr(source).encode()).hexdigest(),
            source_ordinal=ordinal, state={}, owner_lineage=dict(parent_thread_id=source.get('parent_thread_id')))
            for ordinal, (sid, source) in enumerate(sources.items())})


@pytest.mark.parametrize('cohort_size',[None,1])
def test_conversion_progress_counts_committed_changed_and_removed_sessions(tmp_path,monkeypatch,cohort_size):
    sources={f'owner-{number}':_source(f'owner-{number}') for number in range(20)}
    def read_source(client,owner,catalog,*,limits,**kwargs):
        source=deepcopy(sources[owner[1]])
        return source,ingest._weight(source,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    with storage.PreparedStore(tmp_path/'progress.sqlite') as store:
        initial=[]
        for row in ingest.PreparedCohort(store,None,None).convert(_catalog(sources)):
            with store.read_current() as view:
                assert len(list(ingest.pages(view.inventory)))==row['processed']
            initial.append(row)
        assert [(row['processed'],row['total']) for row in initial]==[(16,20),(20,20)]
        del sources['owner-0']
        for source in sources.values():source['history'][0]['input']+=1
        conversion=ingest.PreparedCohort(store,None,None).convert(_catalog(sources,2),cohort_size=cohort_size)
        changes=[]
        for row in conversion:
            with store.read_current() as view:
                owners={entry['owner'] for entry in ingest.pages(view.inventory)}
                assert not owners.intersection(row['removed'])
                for owner in row['changed']:
                    assert view.record(*owner,owner[1]+'-response')['input']==101
            changes.append(row)
        expected=[(20,20)] if cohort_size is None else [(done,20) for done in range(1,21)]
        assert [(row['processed'],row['total']) for row in changes]==expected
        assert sum(len(row['changed'])+len(row['removed']) for row in changes)==20
        with store.read_current() as view:
            assert {row['owner'][1] for row in ingest.pages(view.inventory)}==set(sources)
        assert list(ingest.PreparedCohort(store,None,None).convert(_catalog(sources,2)))==[]


def test_empty_preparation_publishes_without_a_measured_denominator(tmp_path):
    with storage.PreparedStore(tmp_path/'empty.sqlite') as store:
        changes=list(ingest.PreparedCohort(store,None,None).convert(_catalog({})))
        assert len(changes)==1 and changes[0]['ready']
        assert 'total' not in changes[0] and 'processed' not in changes[0]


def test_failed_conversion_does_not_report_uncommitted_sessions(tmp_path,monkeypatch):
    sources={sid:_source(sid) for sid in ('first','second')}
    def read_source(client,owner,catalog,*,limits,**kwargs):
        if owner[1]=='second':raise ValueError('controlled source failure')
        source=deepcopy(sources[owner[1]])
        return source,ingest._weight(source,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    with storage.PreparedStore(tmp_path/'failed-progress.sqlite') as store:
        conversion=ingest.PreparedCohort(store,None,None).convert(_catalog(sources),cohort_size=1)
        first=next(conversion)
        assert (first['processed'],first['total'])==(1,2)
        with pytest.raises(ValueError,match='controlled source failure'):next(conversion)
        with store.read_current() as view:
            assert [row['owner'] for row in ingest.pages(view.inventory)]==[('home','first')]


@pytest.mark.parametrize('operation', ['convert', 'apply', 'publish_ready'])
@pytest.mark.parametrize(('with_proof', 'large_metadata'), [(False, False), (True, False), (True, True)])
def test_identical_body_without_optional_proof_reuses_complete_facts(
        tmp_path, monkeypatch, operation, with_proof, large_metadata):
    from cachemonitor.usage_collection import analysis_proof
    sources = dict(child=_source('child'))
    owner = ('home', 'child')
    initial = _catalog(sources)
    if large_metadata:
        initial['owners'][owner]['state']['status'] = 'ready-' + 'x'*10000
    if with_proof:
        proof = analysis_proof(sources['child'])
        assert proof is not None
        initial['owners'][owner].update(analysis_digest=proof['digest'],
            display_title=proof['title'], append_context=proof['context'])

    def read_source(client, key, catalog, *, limits, cancel=None):
        source = deepcopy(sources[key[1]])
        return source, ingest._weight(source, limits.owner_source_bytes)

    monkeypatch.setattr(ingest, 'read_source', read_source)
    path = tmp_path/'prepared.sqlite'
    with storage.PreparedStore(path) as store:
        assert list(ingest.PreparedCohort(store, None, None).convert(initial))[-1]['ready']
        previous = store.token
        with store.read_current() as view:
            expected = list(view.iter_calls(owner))
            summary = view.derived(*owner, 'summary')
        assert len(expected) == 1
        assert expected[0]['key'] == 'child-response'

    captured = _catalog(sources, 2)
    if large_metadata:
        captured['owners'][owner]['state'].update(initial['owners'][owner]['state'])
    original = deepcopy(captured)

    def forbidden_read(*args, **kwargs):
        raise AssertionError('identical certified source must not be decoded again')

    monkeypatch.setattr(ingest, 'read_source', forbidden_read)
    with storage.PreparedStore(path) as store:
        cohort = ingest.PreparedCohort(store, None, None)
        if operation == 'convert':
            updates = list(cohort.convert(captured))
            assert updates == []
            assert store.token == previous
        elif operation == 'apply':
            cohort.apply(captured, [owner])
        else:
            cohort.publish_ready(captured)
        assert cohort.metrics['source_reads'] == 0
        assert cohort.metrics['prepared_owners'] == 0
        assert not getattr(cohort, 'last_writes', {}).get('calls')
        assert not getattr(cohort, 'last_writes', {}).get('seen')
        with store.read_current() as view:
            assert view.source_identity == ingest.source_identity(initial if operation == 'convert' else captured)
            assert list(view.iter_calls(owner)) == expected
            assert view.derived(*owner, 'summary') == summary
        assert captured == original


def _read(sources, ledger, *, catalog=None, limits=ingest.IngestLimits(), weight=None):
    catalog = _catalog(sources) if catalog is None else catalog
    loaded = []
    def load(owner, admitted):
        loaded.append(owner)
        source = deepcopy(sources[owner[1]])
        size = ingest._weight(source, admitted.owner_source_bytes) if weight is None else weight
        if size > admitted.owner_source_bytes:
            raise storage.CapacityExceeded('source does not fit the admitted context')
        return source, size
    result = ingest.read_enriched_source(None, ('home', 'child'), catalog, ledger,
        source_loader=load, limits=limits, include_mode_proof=True)
    return (*result, loaded)


def test_explicit_child_and_empty_history_do_not_read_or_depend_on_parent(tmp_path):
    sources = dict(parent=_source('parent', 'Fast'), child=_source('child', parent='parent', at=120))
    with closing(QuotaLedger(tmp_path/'ledger.sqlite')) as ledger:
        child, dependency, required, loaded = _read(sources, ledger)
        assert loaded == [('home', 'child')]
        assert required is False and request_tier(child['history'][0]) == 'Standard'
        sources['parent']['history'][0]['service_tier'] = 'Standard'
        _, updated, required, loaded = _read(sources, ledger)
        assert updated == dependency and required is False
        assert loaded == [('home', 'child')]
        sources['child']['history'] = []
        empty, _, required, loaded = _read(sources, ledger)
        assert empty['history'] == [] and required is False
        assert loaded == [('home', 'child')]


def test_raw_known_wire_mode_is_rechecked_after_own_enrichment(tmp_path):
    sources = dict(parent=_source('parent', 'Fast'),
        child=_source('child', parent='parent', at=120, service_tier_source='wire'))
    with closing(QuotaLedger(tmp_path/'ledger.sqlite')) as ledger:
        child, _, required, loaded = _read(sources, ledger)
        assert required is True and loaded == [('home', 'child'), ('home', 'parent')]
        assert request_tier(child['history'][0]) == 'Fast'
        assert child['history'][0]['service_tier_source'] == 'parent'
        # Known raw service_tier did not prove the wire request's own setting.
        assert sources['child']['history'][0]['service_tier'] == 'Standard'


@pytest.mark.parametrize('conflict', [False, True])
def test_local_unknown_evidence_preserves_inheritance_and_conflict_rules(tmp_path, conflict):
    sources = dict(parent=_source('parent', 'Fast'), child=_source('child', parent='parent', at=120))
    with closing(QuotaLedger(tmp_path/'ledger.sqlite')) as ledger:
        ledger.mode_evidence[('home', 'child', 'child-turn')] = dict(mode='미확인',
            request_mode_source='settings_conflict' if conflict else 'settings_override',
            request_mode_action='conflict' if conflict else 'set', mode_conflict=conflict)
        child, _, required, loaded = _read(sources, ledger)
        assert required is (not conflict)
        assert loaded == ([('home', 'child')] if conflict else [('home', 'child'), ('home', 'parent')])
        assert request_tier(child['history'][0]) == ('미확인' if conflict else 'Fast')


def test_unknown_orphan_remains_sensitive_to_a_later_parent(tmp_path):
    sources = dict(child=_source('child', '미확인', parent='parent', at=120))
    with closing(QuotaLedger(tmp_path/'ledger.sqlite')) as ledger:
        child, dependency, required, loaded = _read(sources, ledger)
        assert required is True and request_tier(child['history'][0]) == '미확인'
        assert loaded == [('home', 'child')]
        sources['parent'] = _source('parent', 'Fast')
        child, updated, required, loaded = _read(sources, ledger)
        assert updated != dependency and required is True
        assert request_tier(child['history'][0]) == 'Fast'
        assert loaded == [('home', 'child'), ('home', 'parent')]


def test_ancestry_proof_keeps_the_shared_source_context_budget(tmp_path):
    sources = dict(parent=_source('parent', 'Fast'), child=_source('child', parent='parent', at=120))
    limits = replace(ingest.IngestLimits(), owner_source_bytes=100, context_bytes=128)
    with closing(QuotaLedger(tmp_path/'ledger.sqlite')) as ledger:
        _, _, required, loaded = _read(sources, ledger, limits=limits, weight=100)
        assert required is False and loaded == [('home', 'child')]
        sources['child']['history'][0]['service_tier'] = '미확인'
        with pytest.raises(storage.CapacityExceeded):
            _read(sources, ledger, limits=limits, weight=100)
        assert ledger._enriched_sessions == {}


@pytest.mark.parametrize('child_mode', ['Standard', '미확인'])
def test_prepared_restart_reuses_only_children_with_a_persisted_independence_proof(
        tmp_path, monkeypatch, child_mode):
    sources = dict(parent=_source('parent', 'Fast'),
                   child=_source('child', child_mode, parent='parent', at=120))
    def read_source(client, owner, catalog, *, limits, cancel=None):
        source = deepcopy(sources[owner[1]])
        return source, ingest._weight(source, limits.owner_source_bytes)
    monkeypatch.setattr(ingest, 'read_source', read_source)
    path = tmp_path/'prepared.sqlite'
    with closing(QuotaLedger(tmp_path/'ledger.sqlite')) as ledger:
        with storage.PreparedStore(path) as store:
            cohort = ingest.PreparedCohort(store, None, ledger)
            assert list(cohort.convert(_catalog(sources)))[-1]['ready']
            with store.read_current() as view:
                child = view.owner('home', 'child')
                assert child['dependencies']['mode_ancestry_required'] is (child_mode == '미확인')
        # Restart from persisted dependencies; no retained source/ledger cache
        # can hide a missing ancestry dependency or a needless child rebuild.
        sources['parent']['history'][0]['service_tier'] = 'Standard'
        with storage.PreparedStore(path) as store:
            cohort = ingest.PreparedCohort(store, None, ledger)
            updates = list(cohort.convert(_catalog(sources, 2)))
            changed = {owner for update in updates for owner in update['changed']}
            assert ('home', 'parent') in changed
            assert (('home', 'child') in changed) is (child_mode == '미확인')
            assert cohort.metrics['prepared_owners'] == (2 if child_mode == '미확인' else 1)
            with store.read_current() as view:
                assert request_tier(view.record('home', 'child', 'child-response')) == 'Standard'


def test_own_ledger_correction_rechecks_a_previously_independent_child(tmp_path, monkeypatch):
    sources = dict(parent=_source('parent', 'Fast'), child=_source('child', parent='parent', at=120))
    def read_source(client, owner, catalog, *, limits, cancel=None):
        source = deepcopy(sources[owner[1]])
        return source, ingest._weight(source, limits.owner_source_bytes)
    monkeypatch.setattr(ingest, 'read_source', read_source)
    with closing(QuotaLedger(tmp_path/'ledger.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        cohort = ingest.PreparedCohort(store, None, ledger)
        list(cohort.convert(_catalog(sources)))
        ledger.mode_evidence[('home', 'child', 'child-turn')] = dict(mode='미확인',
            request_mode_source='settings_override', request_mode_action='set', mode_conflict=False)
        updates = list(cohort.convert(_catalog(sources, 2)))
        assert {owner for update in updates for owner in update['changed']} == {('home', 'child')}
        with store.read_current() as view:
            assert view.owner('home', 'child')['dependencies']['mode_ancestry_required'] is True
            assert request_tier(view.record('home', 'child', 'child-response')) == 'Fast'


def test_ledger_availability_rechecks_an_independence_proof(tmp_path, monkeypatch):
    sources = dict(parent=_source('parent', 'Fast'),
                   child=_source('child', '미확인', parent='parent', at=120))
    def read_source(client, owner, catalog, *, limits, cancel=None):
        source = deepcopy(sources[owner[1]])
        return source, ingest._weight(source, limits.owner_source_bytes)
    monkeypatch.setattr(ingest, 'read_source', read_source)
    with closing(QuotaLedger(tmp_path/'ledger.sqlite')) as ledger, storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        for revision, active, expected in ((1, None, '미확인'), (2, ledger, 'Fast'), (3, None, '미확인')):
            cohort = ingest.PreparedCohort(store, None, active)
            changes = list(cohort.convert(_catalog(sources, revision)))
            assert ('home', 'child') in {owner for change in changes for owner in change['changed']}
            with store.read_current() as view:
                assert request_tier(view.record('home', 'child', 'child-response')) == expected
                assert view.owner('home', 'child')['dependencies']['mode_ancestry_required'] is (active is not None)


def test_owner_pricing_reprepares_only_effective_model_changes(tmp_path,monkeypatch):
    import cachemonitor.pricing as pricing
    from cachemonitor.analysis_engine import AnalysisEngine

    sources=dict(astra=_source('astra'),sol=_source('sol',model='gpt-5.6-sol'),
        unknown=_source('unknown',model='not-in-picker'),empty=_source('empty',model=''))
    reads=[]
    def read_source(client,owner,catalog,*,limits,**kwargs):
        reads.append(owner)
        source=deepcopy(sources[owner[1]])
        return source,ingest._weight(source,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        cohort=ingest.PreparedCohort(store,None,None)
        catalog=_catalog(sources)
        assert list(cohort.convert(catalog))[-1]['ready']
        token=store.token
        reads.clear()
        monkeypatch.setattr(pricing,'VERIFIED','later')
        monkeypatch.setattr(pricing,'MULTIPLIER_VERIFIED','later')
        monkeypatch.setitem(pricing.RATE_VERIFIED,'gpt-6-astra','later')
        monkeypatch.setitem(pricing.RATES,'unrelated',pricing.Rate(1,2,3))
        assert list(cohort.convert(catalog))==[] and store.token==token and reads==[]
        for model,sid in [('gpt-6-astra','astra'),('not-in-picker','unknown'),('미확인','empty')]:
            before=len(reads)
            rate=pricing.RATES.get(model,pricing.Rate(1,2,3))
            monkeypatch.setitem(pricing.RATES,model,replace(rate,input=rate.input+1))
            changes=list(cohort.convert(catalog))
            assert len(changes)==1 and set(changes[0]['changed'])=={('home',sid)}
            assert set(reads[before:])=={('home',sid)}
            reference=AnalysisEngine()
            reference.ingest(deepcopy(list(sources.values())))
            with store.read_current() as view:
                for owner,state in reference.sessions.items():
                    assert [row['cost'] for row in view.iter_calls(owner)]==[
                        row['cost'] for row in state['prepared']['history']]


@pytest.mark.parametrize('checkpoint_kind',['matching','missing','mismatched'])
def test_legacy_pricing_migration_preserves_facts_and_uncertified_checkpoints(tmp_path,monkeypatch,checkpoint_kind):
    sources=dict(owner=_source('owner'))
    def read_source(client,owner,catalog,*,limits,**kwargs):
        source=deepcopy(sources[owner[1]])
        return source,ingest._weight(source,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    signature=ingest.pricing_signature
    legacy=signature()
    monkeypatch.setattr(ingest,'pricing_signature',lambda models=None:legacy)
    owner=('home','owner');catalog=_catalog(sources)
    with storage.PreparedStore(tmp_path/'prepared.sqlite') as store:
        cohort=ingest.PreparedCohort(store,None,None)
        list(cohort.convert(catalog))
        with store.read_current() as view:
            old=view.owner(*owner)
            checkpoint=view.derived(*owner,'checkpoint')
            calls=list(view.iter_calls(owner))
        if checkpoint_kind!='matching':
            checkpoint=(None if checkpoint_kind=='missing' else
                dict(checkpoint,dependencies=dict(checkpoint['dependencies'],pricing='not-certified')))
            stage=store.begin(ingest.source_identity(catalog))
            stage.owner(*owner,old['metadata'],old['dependencies'],source_ordinal=0,checkpoint=checkpoint)
            stage.publish(ingest.source_identity(catalog),ready=True)
        monkeypatch.setattr(ingest,'pricing_signature',signature)
        def forbidden(*args,**kwargs):raise AssertionError('migration must not load owner facts')
        monkeypatch.setattr(ingest,'read_source',forbidden)
        cohort=ingest.PreparedCohort(store,None,None)
        changes=list(cohort.convert(catalog))
        assert len(changes)==1 and changes[0]['ready']
        assert changes[0]['writes']['calls']==changes[0]['writes']['candidates']==0
        assert cohort.metrics['prepared_owners']==cohort.metrics['source_reads']==0
        assert cohort.metrics['pricing_updates']==1 and cohort.metrics['complete_visits']==1
        with store.read_current() as view:
            actual=view.owner(*owner)['dependencies']['pricing']
            assert actual==ingest._pricing_dependency(signature(['gpt-6-astra']))
            assert list(view.iter_calls(owner))==calls
            saved=view.derived(*owner,'checkpoint')
            if checkpoint_kind=='matching':assert saved['dependencies']['pricing']==actual
            else:assert saved==checkpoint
        assert cohort.metrics.get('pricing_checkpoint_updates',0)==(checkpoint_kind=='matching')


def test_nonmatching_legacy_and_mixed_body_work_prepare_conservatively(tmp_path,monkeypatch):
    sources={sid:_source(sid) for sid in ('a','b')}
    reads=[]
    def read_source(client,owner,catalog,*,limits,**kwargs):
        reads.append(owner)
        source=deepcopy(sources[owner[1]])
        return source,ingest._weight(source,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    signature=ingest.pricing_signature;legacy=signature()
    monkeypatch.setattr(ingest,'pricing_signature',lambda models=None:legacy)
    with storage.PreparedStore(tmp_path/'mixed.sqlite') as store:
        list(ingest.PreparedCohort(store,None,None).convert(_catalog(sources)))
        monkeypatch.setattr(ingest,'pricing_signature',signature)
        sources['b']['history'][0]['input']+=1
        reads.clear()
        cohort=ingest.PreparedCohort(store,None,None)
        changes=list(cohort.convert(_catalog(sources,2),cohort_size=1))
        assert [change['ready'] for change in changes]==[False,True]
        assert set(reads)=={('home','b')}
        assert cohort.metrics['pricing_updates']==cohort.metrics['prepared_owners']==1
    monkeypatch.setattr(ingest,'pricing_signature',lambda models=None:('different-legacy',legacy))
    with storage.PreparedStore(tmp_path/'nonmatching.sqlite') as store:
        list(ingest.PreparedCohort(store,None,None).convert(_catalog(sources)))
        monkeypatch.setattr(ingest,'pricing_signature',signature)
        reads.clear()
        cohort=ingest.PreparedCohort(store,None,None)
        list(cohort.convert(_catalog(sources)))
        assert set(reads)=={('home','a'),('home','b')}
        assert cohort.metrics['prepared_owners']==2 and cohort.metrics.get('pricing_updates',0)==0


def test_pricing_migration_keeps_each_ready_generation_and_restart_reads_no_history(tmp_path,monkeypatch):
    from cachemonitor.prepared_ingest import preparation_signature
    from cachemonitor.prepared_speed_writer import PreparedSpeedWriter

    sources={sid:_source(sid,input=80000,cached=72000,output=2000,reasoning=500,
        transport='WebSocket',transport_source='response_id',response_status='completed',
        timing_valid=True,completion_latency_ms=20000) for sid in ('a','b','c')}
    def read_source(client,owner,catalog,*,limits,**kwargs):
        source=deepcopy(sources[owner[1]])
        return source,ingest._weight(source,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    signature=ingest.pricing_signature;legacy=signature()
    monkeypatch.setattr(ingest,'pricing_signature',lambda models=None:legacy)
    path=tmp_path/'restart.sqlite';catalog=_catalog(sources)
    with storage.PreparedStore(path) as store,closing(PreparedSpeedWriter(
            store,preparation=preparation_signature())) as speed:
        cohort=ingest.PreparedCohort(store,None,None,speed_writer=speed)
        list(cohort.convert(catalog))
        expected=cohort.speed_results
        generation=store.token.generation
    monkeypatch.setattr(ingest,'pricing_signature',signature)
    def forbidden(*args,**kwargs):raise AssertionError('pricing migration read historical rows')
    monkeypatch.setattr(ingest,'read_source',forbidden)
    monkeypatch.setattr(storage.Snapshot,'iter_speed_rows',forbidden)
    for expected_remaining in (2,1,0):
        with storage.PreparedStore(path) as store,closing(PreparedSpeedWriter(
                store,preparation=preparation_signature())) as speed:
            cohort=ingest.PreparedCohort(store,None,None,speed_writer=speed)
            conversion=cohort.convert(catalog,cohort_size=1)
            change=next(conversion)
            conversion.close()
            generation+=1
            assert change['ready'] and store.token.generation==generation
            assert len(change['changed'])==1 and not change['removed']
            assert change['writes']['calls']==change['writes']['candidates']==0
            assert cohort.metrics['pricing_updates']==cohort.metrics['pricing_checkpoint_updates']==1
            assert cohort.metrics['complete_visits']==3 and cohort.metrics['source_reads']==0
            assert speed.last_checkpoint['restored'] and speed.last_read_rows==0
            assert cohort.speed_results==expected
            with store.read_current() as view:
                assert sum(value['dependencies']['pricing']==ingest._pricing_dependency(legacy)
                    for value in ingest.pages(view.inventory))==expected_remaining
    with storage.PreparedStore(path) as store,closing(PreparedSpeedWriter(
            store,preparation=preparation_signature())) as speed:
        cohort=ingest.PreparedCohort(store,None,None,speed_writer=speed)
        assert list(cohort.convert(catalog))==[]
        assert cohort.current_speeds()==expected and speed.last_read_rows==0
        assert speed.last_checkpoint['restored'] and store.token.generation==generation


def test_append_merges_new_model_before_checkpoint_and_cost_certificates(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from cachemonitor import collection_append
    from cachemonitor.analysis_engine import AnalysisEngine
    from cachemonitor.comparison_usage import dependency_stamp

    owner=('home','owner');source=_source('owner');sources=dict(owner=source)
    def read_source(client,owner,catalog,*,limits,**kwargs):
        material=deepcopy(source)
        return material,ingest._weight(material,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    signature=ingest.pricing_signature;legacy=signature()
    monkeypatch.setattr(ingest,'pricing_signature',lambda models=None:legacy)
    with storage.PreparedStore(tmp_path/'append.sqlite') as store:
        cohort=ingest.PreparedCohort(store,None,None)
        list(cohort.convert(_catalog(sources)))
        monkeypatch.setattr(ingest,'pricing_signature',signature)
        assert list(cohort.convert(_catalog(sources)))[0]['ready']
        def forbidden(*args,**kwargs):raise AssertionError('certified append loaded full owner')
        monkeypatch.setattr(ingest,'read_source',forbidden)
        for number in (1,2):
            with store.read_current() as view:
                old=view.owner(*owner)
                checkpoint=view.derived(*owner,'checkpoint')
            tail=[dict(_source('owner',model='gpt-5.6-sol')['history'][0],
                       key=f'next-{number}',ts=100+number*20)]
            source['history'].extend(tail)
            source['usage_revision']+=1
            catalog=_catalog(sources,number+1)
            metadata={key:value for key,value in source.items() if key not in collection_append._METADATA_EXCLUDE}
            transition=dict(format=1,owner=owner,prior_body_digest=old['dependencies']['source_digest'],
                body_digest=catalog['owners'][owner]['body_digest'],context_digest=checkpoint['context_digest'],
                prior_raw_count=checkpoint['raw_count'],raw_count=len(source['history']),
                prior_frontier=checkpoint['frontier'],tail=tail,tail_digest=collection_append.digest(tail),
                metadata=metadata,requests=dict(upserts=[],removed=[]),turns=dict(upserts=[],removed=[]),
                request_maps=old['request_maps'])
            cohort.client=SimpleNamespace(selected_owner_append=lambda *args,**kwargs:transition)
            change=cohort.apply(catalog,[owner])
            assert change['writes']['calls']==1 and cohort.metrics['append_owners']==number
            with store.read_current() as view:
                choices=view.derived(*owner,'choices')
                current=view.owner(*owner)['dependencies']
                checkpoint=view.derived(*owner,'checkpoint')
                assert set(choices['models'])=={'gpt-6-astra','gpt-5.6-sol'}
                assert current['pricing']==ingest._pricing_dependency(signature(choices['models']))
                assert checkpoint['dependencies']['pricing']==current['pricing']
                assert checkpoint['comparison_append']['target']==dependency_stamp(current)
                reference=AnalysisEngine();reference.ingest([deepcopy(source)])
                assert [row['cost'] for row in view.iter_calls(owner)]==[
                    row['cost'] for row in reference.sessions[owner]['prepared']['history']]


def test_speed_owner_binding_ignores_only_pricing_and_rejects_old_recipe_once(tmp_path,monkeypatch):
    from cachemonitor import speed_checkpoint
    from cachemonitor.prepared_ingest import preparation_signature
    from cachemonitor.prepared_speed import OwnerSpeedStamp
    from cachemonitor.prepared_speed_writer import PreparedSpeedWriter

    source=_source('owner',input=80000,cached=72000,output=2000,reasoning=500,
        transport='WebSocket',transport_source='response_id',response_status='completed',
        timing_valid=True,completion_latency_ms=20000)
    catalog=_catalog(dict(owner=source))
    def read_source(client,owner,catalog,*,limits,**kwargs):
        material=deepcopy(source)
        return material,ingest._weight(material,limits.owner_source_bytes)
    monkeypatch.setattr(ingest,'read_source',read_source)
    binding=speed_checkpoint._binding
    def old_binding(*args,**kwargs):
        return dict(binding(*args,**kwargs),owner_binding='prepared-dependencies-pack-sha256-1')
    path=tmp_path/'recipe.sqlite'
    with monkeypatch.context() as patch:
        patch.setattr(speed_checkpoint,'_binding',old_binding)
        with storage.PreparedStore(path) as store,closing(PreparedSpeedWriter(
                store,preparation=preparation_signature())) as speed:
            cohort=ingest.PreparedCohort(store,None,None,speed_writer=speed)
            list(cohort.convert(catalog))
            with store.read_current() as view:dependency=view.owner('home','owner')['dependencies']
    stamp=OwnerSpeedStamp.from_dependencies(dependency,1,1)
    assert OwnerSpeedStamp.from_dependencies(dict(dependency,pricing='different'),1,1)==stamp
    assert OwnerSpeedStamp.from_dependencies(dict(dependency,mode='different'),1,1)!=stamp
    rows=storage.Snapshot.iter_speed_rows
    reads=[]
    def observed_rows(view,ranges):
        for row in rows(view,ranges):
            reads.append(row[0]);yield row
    monkeypatch.setattr(storage.Snapshot,'iter_speed_rows',observed_rows)
    for recovered in (True,False):
        with storage.PreparedStore(path) as store,closing(PreparedSpeedWriter(
                store,preparation=preparation_signature())) as speed:
            cohort=ingest.PreparedCohort(store,None,None,speed_writer=speed)
            before=store.token.generation;prior=len(reads)
            list(cohort.convert(catalog))
            cohort.current_speeds()
            assert (len(reads)>prior)==recovered
            assert store.token.generation==before+int(recovered)
            if not recovered:assert speed.last_checkpoint['restored']
