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


def _source(sid, mode='Standard', *, parent=None, at=100, **row_fields):
    session = Session(sid, 'home', title=sid)
    session.add_usage(at, sid+'-response', dict(input_tokens=100, cached_input_tokens=80,
        cache_write_input_tokens=0, output_tokens=20, reasoning_output_tokens=5),
        'gpt-6-astra', sid+'-turn', 'high', service_tier=mode)
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
