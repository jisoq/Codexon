"""The native scanner must preserve byte identity and collection work limits."""
import hashlib
import io
import json

import pytest

from cachemonitor.index import _event_lines


@pytest.fixture(params=('python', 'rust'))
def backend(request, monkeypatch):
    if request.param == 'rust':
        pytest.importorskip('cachemonitor._cpu_native')
    monkeypatch.setenv('CODEXON_CPU_BACKEND', request.param)
    return request.param


def test_event_scanning_keeps_complete_line_offsets_digest_and_byte_budget(backend):
    ignored = b'{"type":"response_item","payload":"PRIVATE"}\n'
    event = '{"type":"event_msg","payload":"한글"}\r\n'.encode()
    following = b'{"type":"compacted"}\n'
    data = ignored + event + following
    source = io.BytesIO(data)
    digest = hashlib.sha256()
    state={}
    # A line crossing the budget stays pending until the next bounded read.
    selected = list(_event_lines(source, digest, limit=len(ignored) + 3,state=state))
    assert selected == []
    assert source.tell() == len(ignored)
    assert state['read_bytes']==len(ignored)+3
    assert state['digest'].digest() == hashlib.sha256(ignored).digest()
    selected = list(_event_lines(source, state['digest'],state=state))
    assert [(position,json.loads(value)) for position,value in selected]==[
        (len(ignored),json.loads(event)),(len(ignored)+len(event),json.loads(following))]
    assert state['digest'].digest() == hashlib.sha256(data).digest()
    assert state['read_bytes']==len(data)


def test_incomplete_event_is_left_unconsumed_and_marker_scope_stays_bounded(backend):
    outside = b'x' * 250 + b'"token_usage_record"\n'
    inside = b'{"type":"turn_context"}\n'
    partial = b'{"type":"event_msg"}'
    source = io.BytesIO(outside + inside + partial)
    digest = hashlib.sha256()
    state={}
    selected = list(_event_lines(source, digest,state=state))
    assert selected == [(len(outside), inside)]
    assert source.tell() == len(outside) + len(inside)
    assert state['digest'].digest() == hashlib.sha256(outside + inside).digest()
    digest_before = state['digest'].digest();read_before=state['read_bytes']
    assert list(_event_lines(source, state['digest'], limit=3,state=state)) == []
    assert source.tell() == len(outside) + len(inside)
    assert state['digest'].digest() == digest_before
    assert state['read_bytes']==read_before


def test_header_identity_survives_field_reordering_and_chunk_boundaries(backend):
    header={'unused':'x'*400,'payload':{'id':'session','source':'cli'},'type':'session_meta'}
    data=json.dumps(header).encode()+b'\n'
    for budget in (128,len(data)):
        source=io.BytesIO(data);state={};digest=hashlib.sha256();selected=[]
        while source.tell()<len(data):
            selected.extend(_event_lines(source,digest,budget,state))
            digest=state['digest']
        assert len(selected)==1 and selected[0][0]==0
        result=json.loads(selected[0][1])
        assert result['type']=='session_meta' and result['payload']==header['payload']
        assert digest.digest()==hashlib.sha256(data).digest()
