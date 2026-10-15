"""Exact quota graphs survive bounded frames, cancellation, and bad transfers."""
from collections import deque
import pickle

import pytest

from cachemonitor.quota_transport import (MAX_FRAME_BYTES, TransferCancelled,
    TransferError, frames, receive_result)


class MemoryConnection:
    def __init__(self, payloads):
        self.payloads = deque(payloads)

    def poll(self, timeout=0):
        if not self.payloads:
            raise EOFError('truncated synthetic transfer')
        return True

    def recv_bytes(self, maximum):
        value = self.payloads.popleft()
        if len(value) > maximum:
            raise OSError('bad message length')
        return value


def roundtrip(value):
    payloads = list(frames(value))
    assert all(len(payload) <= MAX_FRAME_BYTES for payload in payloads)
    return receive_result(MemoryConnection(payloads)), payloads


@pytest.mark.parametrize('value', [None, {}, [], (), {'value': None}, {'tuple': (1, 'two', None)}])
def test_empty_and_small_roots_are_exact(value):
    result, _ = roundtrip(value)
    assert result == value
    assert type(result) is type(value)


def test_rows_alias_inline_values_and_forward_container_references():
    row = {'at': 1, 'remaining': 80, 'tuple': ('a', 1)}
    rows = [row] * 700
    child = {'values': rows}
    source = {'metadata': row, 'rows': rows, 'again': rows, 'nested': [child, child]}
    actual, payloads = roundtrip(source)
    assert actual == source
    assert len(payloads) > 2
    assert actual['metadata'] is actual['rows'][0]
    assert actual['rows'] is actual['again'] is actual['nested'][0]['values']
    assert actual['nested'][0] is actual['nested'][1]
    assert actual['rows'][0] is actual['rows'][-1]


def test_packed_rows_cross_frames_without_expanding_the_display_history():
    import random
    from cachemonitor.quota_cycles import _PackedRows
    randomizer=random.Random(8451)
    rows=[dict(at=i,remaining=80,label=randomizer.randbytes(512).hex()) for i in range(1100)]
    packed=_PackedRows(rows)
    actual,payloads=roundtrip(dict(rows=packed,again=packed))
    restored=actual['rows']
    assert isinstance(restored,_PackedRows) and restored is actual['again']
    assert not restored._cache and restored==rows
    assert len(payloads)>3 and all(len(block)<=64*1024 for block in restored.blocks)
    for index in (0,512,-1):assert restored[index]==rows[index]
    assert restored[510:514]==rows[510:514] and len(restored._cache)<=2
    with pytest.raises(EOFError):receive_result(MemoryConnection(payloads[:-1]))


def test_list_and_dict_cycles_keep_identity_and_dictionary_order():
    rows = list(range(300))
    rows.append(rows)
    mapping = {str(index): index for index in range(300)}
    child = {'parent': mapping, 'rows': rows}
    mapping['child'] = child
    source = {'rows': rows, 'mapping': mapping, 'child': child}
    actual, _ = roundtrip(source)
    assert actual['rows'][-1] is actual['rows']
    assert actual['mapping']['child'] is actual['child']
    assert actual['child']['parent'] is actual['mapping']
    assert actual['child']['rows'] is actual['rows']
    assert list(actual['mapping']) == list(mapping)


def test_packed_model_shares_keep_every_bin_and_span_across_transfer():
    from cachemonitor.quota_view import prepare_series
    from cachemonitor.quota_share import prepare_model_share,share_at,paint_spans
    rows=[dict(at=i*300,remaining=90,cycle_cost=i,cycle_value=1,
               cycle_start=0,connect=bool(i),reset_kind=None,label=str(i)) for i in range(1100)]
    intervals=[dict(start=0,end=rows[-1]['at'],cost_rows=[
        dict(ts=i*300-1,model='a' if i%2 else 'b',service_tier='Standard',cost=1)
        for i in range(1,len(rows))])]
    series=prepare_series(rows)
    plain=prepare_model_share(series,intervals)
    actual,_=roundtrip(prepare_model_share(series,intervals,packed=True))
    assert actual['models']==plain['models']
    assert list(actual['spans'])==plain['spans']
    assert dict(zip(actual['bucket_ids'],actual['bins']))==plain['bins']
    assert list(paint_spans(actual))==list(paint_spans(plain))
    for at in (-1,299,300,511*300,512*300,rows[-1]['at']+300):
        assert share_at({'model_share':actual},at)==share_at({'model_share':plain},at)


def test_many_nested_small_containers_do_not_form_one_large_root_frame():
    source = {'periods': [{'rows': [{'at': i * 127 + j, 'remaining': 50.5,
                                   'label': 'observed row'} for j in range(127)]}
                          for i in range(127)]}
    actual, payloads = roundtrip(source)
    assert actual == source
    assert len(payloads) > 100
    assert len(payloads[0]) < 4096
    assert max(map(len, payloads)) < 100000


def test_none_elements_at_frame_boundaries_are_not_dropped():
    source = [None if index % 3 else {'label': 'x' * 2000} for index in range(512)]
    actual, _ = roundtrip(source)
    assert actual == source


def test_truncated_and_corrupt_transfers_never_return_partial_graphs():
    source = {'rows': [{'at': index} for index in range(500)]}
    payloads = list(frames(source))
    with pytest.raises(EOFError):
        receive_result(MemoryConnection(payloads[:-1]))
    with pytest.raises(TransferError):
        receive_result(MemoryConnection([*payloads[:2], b'not a pickle']))
    with pytest.raises((TransferError, pickle.UnpicklingError)):
        receive_result(MemoryConnection([payloads[0], payloads[2], payloads[1], *payloads[3:]]))


def test_cancellation_stops_between_frames_without_returning_partial_graph():
    connection = MemoryConnection(frames({'rows': [{'at': index} for index in range(1000)]}))
    checks = []
    def cancelled():
        checks.append(True)
        return len(checks) > 3
    with pytest.raises(TransferCancelled):
        receive_result(connection, cancelled)
    assert connection.payloads


def test_oversized_scalar_and_unexpected_objects_fail_without_truncation():
    with pytest.raises(TransferError, match='size'):
        list(frames({'label': 'x' * (MAX_FRAME_BYTES + 1)}))
    with pytest.raises(TransferError, match='Unsupported'):
        list(frames({'custom': object()}))
    with pytest.raises(OSError):
        receive_result(MemoryConnection([b'x' * (MAX_FRAME_BYTES + 1)]))


def test_small_control_messages_remain_compatible():
    phase = {'kind': 'phase', 'value': 'writing'}
    assert receive_result(MemoryConnection([pickle.dumps(phase)])) == phase


def test_nested_observation_fields_keep_aliases_and_complete_frame_diagnostics(monkeypatch):
    import gc
    from cachemonitor import quota_transport
    markers = []
    row = {'at': 100, 'markers': markers, 'exclusion_ids': markers}
    source = {'rows': [row] * 700, 'selected': row, 'markers': markers}
    payloads = list(frames(source))
    thresholds, enabled = gc.get_threshold(), gc.isenabled()
    diagnostics = {}
    actual = receive_result(MemoryConnection(payloads), statistics=diagnostics)
    assert actual == source
    assert actual['selected'] is actual['rows'][0] is actual['rows'][-1]
    assert actual['markers'] is actual['selected']['markers'] is actual['selected']['exclusion_ids']
    assert diagnostics['frame_bytes'] == list(map(len, payloads))
    assert len(diagnostics['decode_ms']) == len(payloads)
    assert gc.get_threshold() == thresholds and gc.isenabled() == enabled
    # A caller that disabled collection retains control of that policy.
    monkeypatch.setattr(quota_transport.gc, 'isenabled', lambda: False)
    def unexpected_collection(*args):
        raise AssertionError('Collection was disabled by the caller')
    monkeypatch.setattr(quota_transport.gc, 'collect', unexpected_collection)
    assert receive_result(MemoryConnection(payloads)) == source
