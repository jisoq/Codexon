import gzip
import json
import zlib

import pytest
import zstandard

from cachemonitor.proxy_observation import DecodedObservation, SelectedJSON, ResponseMetadata, message_metadata


@pytest.mark.parametrize('encoding,compress',[('gzip',gzip.compress),('deflate',zlib.compress),
                                             ('zstd',zstandard.ZstdCompressor().compress)])
def test_large_compressed_request_extracts_only_metadata_after_input(encoding,compress):
    raw=json.dumps({'input':['PRIVATE'*2500000],'model':'requested','service_tier':'priority','stream':True}).encode()
    payload=compress(raw)
    observation=DecodedObservation(SelectedJSON(),encoding)
    for i in range(0,len(payload),7):observation.feed(payload[i:i+7])
    assert observation.finish()=={'model':'requested','service_tier':'priority','stream':True}
    assert observation.error is None


@pytest.mark.parametrize('chunk_size',[1,3,65536])
@pytest.mark.parametrize('newline',[b'\n',b'\r',b'\r\n'])
def test_streaming_sse_boundaries_utf8_and_metadata_only(chunk_size,newline):
    events=[];observation=DecodedObservation(ResponseMetadata(events.append),'gzip')
    raw=(b'\xef\xbb\xbf: keepalive'+newline+b'event: response.completed'+newline+
         b'data: '+json.dumps({'type':'response.completed','response':{'id':'r','model':'actual',
             'service_tier':'priority','output':'PRIVATE \ud55c\uae00'}},ensure_ascii=False).encode()+newline+newline+
         b'data: [DONE]'+newline+newline)
    data=gzip.compress(raw)
    for i in range(0,len(data),chunk_size):observation.feed(data[i:i+chunk_size])
    observation.finish();observation.finish()
    assert events==[{'type':'response.completed','response':{'id':'r','model':'actual','service_tier':'priority'}}]


def test_large_websocket_metadata_preserves_request_tier():
    raw=json.dumps({'type':'response.create','input':['PRIVATE'*2500000],'model':'m','service_tier':'priority'})
    assert message_metadata(raw)=={'type':'response.create','model':'m','service_tier':'priority'}


def test_invalid_truncated_or_unsupported_observation_never_fabricates_metadata():
    for encoding,data in [('gzip',b'not gzip'),('zstd',b'not zstd'),('unknown',b'{"model":"fake"}')]:
        parser=DecodedObservation(SelectedJSON(),encoding);parser.feed(data)
        assert parser.finish()=={} and parser.error
    assert message_metadata(b'{"model":"m",')=={}
    assert message_metadata(b'{"input":{"model":"fake"},"model":"actual"}')=={'model':'actual'}
