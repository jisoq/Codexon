import gzip
import json
import zlib

import pytest
import zstandard

from cachemonitor.proxy_observation import DecodedObservation, SelectedJSON, message_metadata


@pytest.mark.parametrize('encoding,compress,size',[
    ('gzip',gzip.compress,25000),('deflate',zlib.compress,25000),
    ('zstd',zstandard.ZstdCompressor().compress,25000),
    pytest.param('gzip',gzip.compress,2500000,marks=pytest.mark.extended,id='large-body'),
])
def test_compressed_request_extracts_only_metadata_after_input(encoding,compress,size):
    # Chunk boundaries and ignored private fields need kilobytes, not megabytes
    # for every codec. Keep one large-body stress case outside the default suite.
    raw=json.dumps({'input':['PRIVATE'*size],'model':'requested','service_tier':'priority','stream':True}).encode()
    payload=compress(raw)
    observation=DecodedObservation(SelectedJSON(),encoding)
    for i in range(0,len(payload),7):observation.feed(payload[i:i+7])
    assert observation.finish()=={'model':'requested','service_tier':'priority','stream':True}
    assert observation.error is None


def test_invalid_truncated_or_unsupported_observation_never_fabricates_metadata():
    for encoding,data in [('gzip',b'not gzip'),('zstd',b'not zstd'),('unknown',b'{"model":"fake"}')]:
        parser=DecodedObservation(SelectedJSON(),encoding);parser.feed(data)
        assert parser.finish()=={} and parser.error
    assert message_metadata(b'{"model":"m",')=={}
    assert message_metadata(b'{"input":{"model":"fake"},"model":"actual"}')=={'model':'actual'}


@pytest.mark.parametrize('effort', ['high', 'low', 'x' * 256])
def test_nested_effort_keeps_only_bounded_scalars(effort):
    payload=json.dumps(dict(reasoning=dict(effort=effort,summary='PRIVATE'),
        response=dict(id='r',reasoning=dict(effort='low',encrypted_content='PRIVATE')),
        input=[dict(reasoning=dict(effort='PRIVATE'))])).encode()
    parser=SelectedJSON()
    for offset in range(0,len(payload),3):parser.feed(payload[offset:offset+3])
    assert parser.finish()==dict(reasoning=dict(effort=effort),response=dict(id='r',reasoning=dict(effort='low')))


@pytest.mark.parametrize('effort', [None, True, 1, ['high'], dict(effort='high'), 'x' * 257])
def test_invalid_effort_does_not_capture_reasoning_content(effort):
    payload=json.dumps(dict(model='m',reasoning=dict(effort=effort,summary='PRIVATE'),
        response=dict(reasoning=dict(effort=effort,summary='PRIVATE')))).encode()
    parser=SelectedJSON();parser.feed(payload)
    assert parser.finish()==dict(model='m')


@pytest.mark.parametrize('fragments', [
    (b' ' * 17,),
    (b' \t\r\n' * 1024,) * 4 + (b'\xef', b'\xbb', b'\xbf'),
    (b'\xef\xbb\xbf', b' \n'),
])
@pytest.mark.parametrize('sse', [False, True])
def test_format_detection_keeps_fragmented_whitespace_and_bom_bounded(fragments, sse):
    from cachemonitor.proxy_observation import ResponseMetadata
    expected=dict(type='response.completed',response=dict(id='fragmented',model='m'))
    metadata=[];parser=ResponseMetadata(metadata.append)
    for fragment in fragments:
        parser.feed(fragment)
        assert parser.error is None
        assert len(parser.prefix)<=5
    payload=json.dumps(expected).encode()
    parser.feed(b'data: '+payload+b'\n\n' if sse else payload)
    parser.finish()
    assert parser.error is None and metadata==[expected]


@pytest.mark.parametrize('encoding,compress', [
    ('identity',lambda value:value),('gzip',gzip.compress),('deflate',zlib.compress),
    ('zstd',zstandard.ZstdCompressor().compress),
])
def test_sse_terminal_survives_cumulative_decoded_bytes(monkeypatch, encoding, compress):
    from cachemonitor import proxy_observation as module
    monkeypatch.setattr(module,'MAX_OBSERVATION',512)
    filler=b'data: '+json.dumps(dict(type='response.output_text.delta',delta='x'*64)).encode()+b'\n\n'
    terminal=dict(type='response.completed',response=dict(id='after-total',model='m'))
    payload=compress(filler*8+b'data: '+json.dumps(terminal).encode()+b'\n\n')
    metadata=[];decoder=DecodedObservation(module.ResponseMetadata(metadata.append),encoding)
    for offset in range(0,len(payload),7):decoder.feed(payload[offset:offset+7])
    decoder.finish()
    assert decoder.error is None
    assert decoder.consumer.error is None
    assert metadata[-1:]==[terminal]
    assert len(metadata)==9


@pytest.mark.extended
def test_sse_terminal_survives_real_128_mib_decoded_total():
    from cachemonitor.proxy_observation import ResponseMetadata
    prefix=b'data: {"type":"response.output_text.delta","delta":"'
    suffix=b'"}\n\n'
    filler=prefix+b'x'*(65536-len(prefix)-len(suffix))+suffix
    terminal=dict(type='response.completed',response=dict(id='after-128-mib',model='m'))
    metadata=[];events=[0]
    def observe(value):
        events[0]+=1
        if value.get('type')=='response.completed':metadata.append(value)
    decoder=DecodedObservation(ResponseMetadata(observe))
    for _ in range(2048):decoder.feed(filler)
    decoder.feed(b'data: '+json.dumps(terminal).encode()+b'\n\n');decoder.finish()
    assert decoder.error is None and decoder.consumer.error is None
    assert metadata==[terminal] and events[0]==2049


@pytest.mark.parametrize('text', [False, True])
def test_message_document_budget_is_shared_before_native_dispatch(monkeypatch, text):
    from types import SimpleNamespace
    from cachemonitor import proxy_observation as module
    value=json.dumps(dict(model='한글'),ensure_ascii=False)
    payload=value if text else value.encode()
    calls=[]
    def native_metadata(data):
        calls.append(data)
        return dict(model='한글')
    monkeypatch.setattr(module,'get_native',lambda:SimpleNamespace(message_metadata=native_metadata))
    size=len(value.encode())
    monkeypatch.setattr(module,'MAX_OBSERVATION',size)
    assert module.message_metadata(payload)==module._python_message_metadata(payload)==dict(model='한글')
    assert calls==[payload]
    calls.clear();monkeypatch.setattr(module,'MAX_OBSERVATION',size-1)
    assert module.message_metadata(payload)==module._python_message_metadata(payload)=={}
    assert calls==[]
