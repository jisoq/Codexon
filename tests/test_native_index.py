"""The native scanner must preserve byte identity and collection work limits."""
import hashlib
import io

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
    # The selected event starts before the budget and ends after it.
    selected = list(_event_lines(source, digest, limit=len(ignored) + 3))
    assert selected == [(len(ignored), event)]
    assert source.tell() == len(ignored) + len(event)
    assert digest.digest() == hashlib.sha256(ignored + event).digest()
    selected = list(_event_lines(source, digest))
    assert selected == [(len(ignored) + len(event), following)]
    assert digest.digest() == hashlib.sha256(data).digest()


def test_incomplete_event_is_left_unconsumed_and_marker_scope_stays_bounded(backend):
    outside = b'x' * 250 + b'"token_usage_record"\n'
    inside = b'{"type":"turn_context"}\n'
    partial = b'{"type":"event_msg"}'
    source = io.BytesIO(outside + inside + partial)
    digest = hashlib.sha256()
    selected = list(_event_lines(source, digest))
    assert selected == [(len(outside), inside)]
    assert source.tell() == len(outside) + len(inside)
    assert digest.digest() == hashlib.sha256(outside + inside).digest()
    digest_before = digest.digest()
    assert list(_event_lines(source, digest, limit=3)) == []
    assert source.tell() == len(outside) + len(inside)
    assert digest.digest() == digest_before
