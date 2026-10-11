"""Authenticated local collection transport; durable recovery stays in SQLite."""
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import threading
import time


TIMEOUT = 2.0
REQUEST_LIMIT = 64 * 1024
_endpoints = {}
_endpoint_lock = threading.Lock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _timeout(connection, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Collection transport deadline expired')
    connection.settimeout(remaining)


def _receive(connection, size, deadline):
    chunks = []
    while size:
        _timeout(connection, deadline)
        chunk = connection.recv(min(size, 1024 * 1024))
        if not chunk:
            raise EOFError('Collection transport closed')
        chunks.append(chunk)
        size -= len(chunk)
    return b''.join(chunks)


def _send(connection, value, deadline):
    view = memoryview(value)
    while view:
        _timeout(connection, deadline)
        count = connection.send(view[:1024 * 1024])
        if not count:
            raise EOFError('Collection transport closed')
        view = view[count:]


def _frame(connection, value, deadline):
    _send(connection, struct.pack('!Q', len(value)), deadline)
    _send(connection, value, deadline)


def _read_frame(connection, deadline, limit=None):
    size = struct.unpack('!Q', _receive(connection, 8, deadline))[0]
    if limit is not None and size > limit:
        raise ValueError('Collection request exceeds its limit')
    return _receive(connection, size, deadline)


class LiveServer:
    """One bounded request per connection, without deserializing Python objects."""
    def __init__(self, path, handler):
        self.path = Path(path)
        self.handler = handler
        self.token = secrets.token_bytes(32)
        self.closed = threading.Event()
        self.slots = threading.BoundedSemaphore(16)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.listener.bind(('127.0.0.1', 0))
            self.listener.listen(16)
            self.listener.settimeout(.2)
            self.endpoint = dict(port=self.listener.getsockname()[1], token=self.token.hex())
            temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(8))
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    json.dump(self.endpoint, stream)
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            with _endpoint_lock:
                _endpoints[str(self.path)] = self.endpoint
            self.thread = threading.Thread(target=self._serve, name='collection-live', daemon=True)
            self.thread.start()
        except BaseException:
            self.listener.close()
            raise

    def _serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.slots.acquire(blocking=False):
                connection.close()
                continue
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection):
        try:
            with connection:
                deadline = time.monotonic() + TIMEOUT
                nonce = secrets.token_bytes(32)
                _send(connection, nonce, deadline)
                signature = _receive(connection, 32, deadline)
                encoded = _read_frame(connection, deadline, REQUEST_LIMIT)
                if not hmac.compare_digest(signature, hmac.digest(self.token, nonce + encoded, 'sha256')):
                    return
                request_value = json.loads(encoded)
                if not isinstance(request_value, dict):
                    return
                metadata, body = self.handler(request_value)
                encoded = _json(metadata)
                signature = hmac.digest(self.token, nonce + encoded + body, 'sha256')
                _send(connection, signature, deadline)
                _frame(connection, encoded, deadline)
                _frame(connection, body, deadline)
        except (OSError, EOFError, ValueError, TypeError):
            pass
        finally:
            self.slots.release()

    def close(self):
        self.closed.set()
        self.listener.close()
        self.thread.join(TIMEOUT)
        # A newer service may already own the discovery path.
        try:
            if json.loads(self.path.read_text(encoding='utf-8')) == self.endpoint:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        with _endpoint_lock:
            if _endpoints.get(str(self.path)) == self.endpoint:
                _endpoints.pop(str(self.path), None)


def request(path, value):
    """Reconnect once within one deadline; discovery is cached between requests."""
    path = Path(path)
    key = str(path)
    deadline = time.monotonic() + TIMEOUT
    encoded = _json(value)
    if len(encoded) > REQUEST_LIMIT:
        raise ValueError('Collection request exceeds its limit')
    with _endpoint_lock:
        endpoint = _endpoints.get(key)
    for attempt in range(2):
        try:
            if endpoint is None:
                endpoint = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(endpoint, dict) or type(endpoint.get('port')) is not int or
                        not 0 < endpoint['port'] < 65536 or not isinstance(endpoint.get('token'), str)):
                    return None
                with _endpoint_lock:
                    _endpoints[key] = endpoint
            token = bytes.fromhex(endpoint['token'])
            if len(token) != 32:
                return None
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _timeout(connection, deadline)
                connection.settimeout(min(.2, connection.gettimeout()))
                connection.connect(('127.0.0.1', endpoint['port']))
                nonce = _receive(connection, 32, deadline)
                _send(connection, hmac.digest(token, nonce + encoded, 'sha256'), deadline)
                _frame(connection, encoded, deadline)
                signature = _receive(connection, 32, deadline)
                metadata = _read_frame(connection, deadline)
                body = _read_frame(connection, deadline)
                if not hmac.compare_digest(signature, hmac.digest(token, nonce + metadata + body, 'sha256')):
                    raise ValueError('Collection response authentication failed')
                return json.loads(metadata), body
        except (OSError, EOFError, ValueError, KeyError, TypeError):
            with _endpoint_lock:
                if _endpoints.get(key) == endpoint:
                    _endpoints.pop(key, None)
            endpoint = None
            if time.monotonic() >= deadline:
                break
    return None
