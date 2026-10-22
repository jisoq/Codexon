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
