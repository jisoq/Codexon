"""Native parsing and attribution retain observable data contracts."""
import json
import math
import random

import pytest

from cachemonitor.native_backend import get_native
from cachemonitor.proxy_observation import message_metadata, _python_message_metadata
from cachemonitor.quota_attribution import RequestWindows, local_request_intervals
from cachemonitor.quota_cycles import _clip_quota_interval, _quota_clipper, _continuous_quota_intervals


@pytest.fixture
def native(monkeypatch):
    pytest.importorskip('cachemonitor._cpu_native')
    monkeypatch.setenv('CODEXON_CPU_BACKEND', 'rust')
    return get_native()


@pytest.mark.parametrize('data', [
    b'', b'null', b'[]', b'"hello"', b'{"model":"m","response":{"id":"r","model":"x"}}',
    b'{"model":"first","model":"last","stream":true,"output":[{"model":"ignored"}]}',
    b'{"model":"m"} garbage', b'{"input":[1,2,]}', b'{"model":"m","input":"\\x"}',
    b'{"model":"m","input":"\xff"}', b'{"model":"m","input":"\\uD800"}',
    b'{"model":"m","input":1e400}', b'{"response.model":"dot","response":{"status":"ok"}}',
    json.dumps({'model':'a'*257,'id':'a'*256,'input':'private'*1000}).encode(),
    json.dumps({'model':'한글😀','response':{'id':'r','service_tier':'priority'}}).encode(),
])
def test_metadata_matches_streaming_validation(native, data):
    assert message_metadata(data) == _python_message_metadata(data)


def test_metadata_random_nested_payloads_and_text_input(native):
    rng = random.Random(7)
    for _ in range(50):
        payload = dict(model='m', stream=bool(rng.randrange(2)), response=dict(id='r', status='completed'),
                       input=[dict(content=['\\"\n한글😀', rng.randrange(1000), None, True],
                                   model='private') for _ in range(rng.randrange(30))])
        text = json.dumps(payload, ensure_ascii=bool(rng.randrange(2)))
        assert message_metadata(text) == _python_message_metadata(text)
        assert 'input' not in message_metadata(text)


def test_native_backend_missing_and_invalid_selection(monkeypatch):
    import cachemonitor.native_backend as backend
    monkeypatch.setattr(backend, '_load', lambda: None)
    monkeypatch.setenv('CODEXON_CPU_BACKEND','auto')
    assert get_native() is None
    monkeypatch.setenv('CODEXON_CPU_BACKEND','rust')
    with pytest.raises(RuntimeError): get_native()
    monkeypatch.setenv('CODEXON_CPU_BACKEND','wrong')
    with pytest.raises(ValueError): get_native()


def test_request_windows_overlap_order_and_open_ends(native, monkeypatch):
    from cachemonitor.quota_cycles import _PackedRows
    rows = [dict(uid=str(i), start=i//3, end=None if i%11==0 else i//3+4) for i in range(400)]
    rows[:0] = [dict(uid='positive-zero',start=0.0,end=5),dict(uid='negative-zero',start=-0.0,end=5)]
    rows += [dict(start=True,end=3),dict(start=math.nan,end=4),dict(start=4,end=2)]
    monkeypatch.setenv('CODEXON_CPU_BACKEND','python'); reference = RequestWindows(rows)
    monkeypatch.setenv('CODEXON_CPU_BACKEND','rust'); actual = RequestWindows(rows)
    packed = RequestWindows(_PackedRows(rows))
    assert actual._native is not None
    for start,end in ((0,0),(0,10),(40,50),(130,150),(-10,-1),(5,5),(200,201),(-math.inf,math.inf),(0,2**54)):
        assert list(actual.overlapping(start,end)) == list(reference.overlapping(start,end))
        assert list(packed.overlapping(start,end)) == list(reference.overlapping(start,end))
    # Force the end-sorted branch with signed-zero ties and an earlier ending.
    rows = [dict(uid='positive-zero',start=-10,end=0.0),dict(uid='negative-zero',start=-10,end=-0.0)]
    rows += [dict(uid=str(i),start=-10,end=i-1) for i in range(130)]
    monkeypatch.setenv('CODEXON_CPU_BACKEND','python'); reference = RequestWindows(rows)
    monkeypatch.setenv('CODEXON_CPU_BACKEND','rust'); actual = RequestWindows(rows)
    assert list(actual.overlapping(0,0)) == list(reference.overlapping(0,0))


def quota_cycle(count=160):
    points=[(100+i*30,i//5) for i in range(count*5)]
    rows=[dict(uid=str(i),ts=100+i*150+45,request_start=100+i*150+10,
               model='m',service_tier='Standard',cost=1.) for i in range(count)]
    return dict(id='cycle',start=points[0][0],end=points[-1][0],endpoints=points,cost_rows=rows,
                models=[dict(model='m',service_tier='Standard')],separate_models=[],
                delta=points[-1][1],forward_tracking=True,cost_complete=True,provisional=False)


def test_quota_sparse_local_intervals_keep_accounting_and_boundaries(native, monkeypatch):
    cycle=quota_cycle()
    monkeypatch.setenv('CODEXON_CPU_BACKEND','python')
    expected=local_request_intervals(cycle,[],_clip_quota_interval,_quota_clipper)
    monkeypatch.setenv('CODEXON_CPU_BACKEND','rust')
    assert local_request_intervals(cycle,[],_clip_quota_interval,_quota_clipper)==expected
    index=native.QuotaClipIndex(cycle['endpoints'],cycle['cost_rows'])
    for start,end in ((100,190),(130,190),(99,20000),(101,102),(500,500)):
        assert _clip_quota_interval(cycle,start,end,selection=index.select(start,end)) == _clip_quota_interval(cycle,start,end)
    points=cycle['endpoints'];cycle={**cycle,'endpoints':points[:200]+points[400:]}
    expected=[{**_clip_quota_interval(cycle,lo,hi),
               'id':f"cycle:observed:{lo}",'observation_gaps_excluded':True}
              for lo,hi in ((points[0][0],points[199][0]),(points[400][0],points[-1][0]))]
    for backend in ('python','rust'):
        monkeypatch.setenv('CODEXON_CPU_BACKEND',backend)
        assert list(_continuous_quota_intervals([cycle]))==expected


def test_quota_index_keeps_original_duplicate_and_unsorted_order(native):
    points=[(30,3),(10,1),(20,2),(20,2)]
    rows=[dict(ts=30,key='a'),dict(ts=10,key='b'),dict(ts=20,key='c')]
    index=native.QuotaClipIndex(points,rows)
    selected,candidates=index.select(10,30)
    assert selected == points
    assert candidates == []  # First/last retained endpoints, not their min/max.
