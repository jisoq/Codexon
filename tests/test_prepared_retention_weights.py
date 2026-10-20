"""Conservative publication admission with local subtree arithmetic reuse."""
import sys

import pytest

from cachemonitor import prepared_delivery as delivery
from cachemonitor.prepared_ingest import _weight
from cachemonitor.prepared_store import CapacityExceeded


LIMIT=256*1024*1024


def test_weight_memo_preserves_aliases_keys_exact_types_and_distinct_equal_values():
    shared={'recent':[{'key':'response','bytes':b'x'*6000}],
            'types':(None,True,1,1.,-0.,'문자열')}
    equal={'recent':[{'key':'response','bytes':b'x'*6000}],
           'types':(None,True,1,1.,-0.,'문자열')}
    key=(b'key'*2000,)
    value={key:[shared,shared,equal,key]}
    memo=delivery._WeightMemo(delivery._WeightMemo.MAX_BYTES)
    try:
        assert memo.tree(value,LIMIT)==_weight(value,LIMIT)
        expected=sys.getsizeof([shared,shared])+2*_weight(shared,LIMIT)
        assert memo.tree([shared,shared],LIMIT)==expected
        # A cache hit still consumes the entire subtree's conservative charge.
        with pytest.raises(CapacityExceeded):
            memo.tree(shared,_weight(shared,LIMIT)-1)
    finally:memo.close()
    assert memo.entries=={}


def test_weight_memo_never_installs_a_partial_container_after_capacity_failure():
    value={'recent':[{'bytes':b'x'*10000}]}
    memo=delivery._WeightMemo(delivery._WeightMemo.MAX_BYTES)
    try:
        with pytest.raises(CapacityExceeded):memo.tree(value,200)
        assert id(value) not in memo.entries
        assert memo.tree(value,LIMIT)==_weight(value,LIMIT)
    finally:memo.close()


def test_new_views_reuse_unchanged_root_weights_without_changing_alias_charges(monkeypatch):
    shared={'values':[dict(data=b'x'*6000) for _ in range(12)]}
    roots={'source':{'key':shared},'view':{}}
    previous=delivery._Retention().stage(roots,LIMIT)
    next_roots={'source':roots['source'],'view':{'new':[shared,shared]}}
    sizes=[];original=delivery.sys.getsizeof
    def observed(value,*args):
        if value is shared:sizes.append(value)
        return original(value,*args)
    monkeypatch.setattr(delivery.sys,'getsizeof',observed)
    current=previous.stage(next_roots,LIMIT)
    assert sizes == []
    assert current.entries['view']['new'][1] == _weight('new',LIMIT)+_weight([shared,shared],LIMIT)
    assert previous.entries['source']['key'][0] is shared


def test_weight_memo_limits_entries_and_stays_iterative_for_deep_values(monkeypatch):
    monkeypatch.setattr(delivery._WeightMemo,'MAX_ENTRIES',2)
    memo=delivery._WeightMemo(delivery._WeightMemo.MAX_BYTES)
    try:
        for number in range(10):
            value=[{'bytes':bytes([number])*10000}]
            assert memo.tree(value,LIMIT)==_weight(value,LIMIT)
        assert len(memo.entries)<=2
        deep=0
        for _ in range(1500):deep=[deep]
        assert memo.tree(deep,LIMIT)==_weight(deep,LIMIT)
        assert memo.scratch_bytes()<=memo.limit
    finally:memo.close()


def test_weight_memo_rejects_cycles_without_reusing_an_unfinished_weight():
    value=[];value.append(value)
    memo=delivery._WeightMemo(delivery._WeightMemo.MAX_BYTES)
    try:
        with pytest.raises(CapacityExceeded,match='cyclic'):memo.tree(value,LIMIT)
        assert id(value) not in memo.entries
    finally:memo.close()


def test_retention_keeps_original_charges_and_previous_generation_cache(monkeypatch):
    shared={'recent':[{'key':'response','data':b'x'*10000}]}
    roots={'own':{('home','owner'):shared},'presented':{('home','owner'):(shared,shared)}}
    original=delivery._Retention()
    with monkeypatch.context() as plain:
        plain.setattr(delivery._WeightMemo,'MAX_BYTES',0)
        expected=original.stage(roots,LIMIT)
    # Capture each charge before another instance can shrink CPython's
    # shared-key instance dictionary allocation. Compare payload accounting,
    # not allocator changes between the two independent admissions.
    expected_payload_bytes=expected.retained_bytes-expected._metadata_bytes(expected)
    actual=original.stage(roots,LIMIT)
    assert actual.retained_bytes-actual._metadata_bytes(actual)==expected_payload_bytes
    assert {name:{key:item[1] for key,item in values.items()} for name,values in actual.entries.items()}=={
        name:{key:item[1] for key,item in values.items()} for name,values in expected.entries.items()}
    unchanged=actual.stage(roots,LIMIT)
    assert unchanged.rescanned==0
    assert unchanged.entries['own'][('home','owner')] is actual.entries['own'][('home','owner')]


def test_retention_charges_scratch_and_releases_it_on_failed_replacement(monkeypatch):
    instances=[]
    class ObservedMemo(delivery._WeightMemo):
        __slots__=()
        def __init__(self,limit):
            super().__init__(limit);instances.append(self)
    monkeypatch.setattr(delivery,'_WeightMemo',ObservedMemo)
    old_value={'data':b'a'*40000}
    old_roots={'owners':{('home','owner'):old_value}}
    previous=delivery._Retention().stage(old_roots,LIMIT)
    old_entries=previous.entries;old_item=old_entries['owners'][('home','owner')]
    replacement={'owners':{('home','owner'):{'data':b'b'*60000}}}
    with monkeypatch.context() as plain:
        plain.setattr(ObservedMemo,'MAX_BYTES',0)
        measured=previous.stage(replacement,LIMIT)
    # The old payload/cache stays live while replacement is staged. A limit
    # admitting that state without memo scratch must reject its extra peak.
    replaced=old_item[1]+previous.containers['owners'][1]
    without_scratch=measured.retained_bytes+replaced+previous._metadata_bytes(previous)
    with pytest.raises(CapacityExceeded):previous.stage(replacement,without_scratch)
    assert previous.entries is old_entries
    assert previous.entries['owners'][('home','owner')] is old_item
    assert old_item[0] is old_value and old_value['data']==b'a'*40000
    assert all(instance.entries=={} for instance in instances)
    retry=previous.stage(replacement,LIMIT)
    assert retry.entries['owners'][('home','owner')][0] is replacement['owners'][('home','owner')]
    assert previous.entries['owners'][('home','owner')] is old_item
    assert all(instance.entries=={} for instance in instances)


def test_aliases_cannot_reject_a_publication_that_fits_unique_object_budget():
    shared={'payload':b'x'*60000}
    roots={'own':{'owner':shared},'views':{str(i):shared for i in range(20)}}
    admitted=delivery._Retention().stage(roots,200000)
    assert admitted.retained_bytes<100000
    replacement={'own':{'owner':{'payload':b'y'*60000}},'views':{}}
    # Both old and new objects stay alive while committing a replacement.
    with pytest.raises(CapacityExceeded):admitted.stage(replacement,100000)
    assert admitted.roots is roots
    assert admitted.stage(replacement,200000).retained_bytes<200000


def test_distinct_equal_objects_are_not_deduplicated_by_value():
    values=[bytes(bytearray(b'x'*60000)) for _ in range(4)]
    with pytest.raises(CapacityExceeded):
        delivery._Retention().stage({'values':dict(enumerate(values))},200000)
