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
NOTIFICATION_WAIT = 10.0
REQUEST_LIMIT = 64 * 1024
_endpoints = {}
_endpoint_lock = threading.Lock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _signature(token, *chunks):
    digest = hmac.new(token, digestmod='sha256')
    for chunk in chunks:
        digest.update(chunk)
    return digest.digest()


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
        self.connection_lock = threading.Lock()
        self.connections = set()
        self.handlers = set()
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
            try:
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self.connection_lock:
                    if self.closed.is_set():
                        connection.close()
                        self.slots.release()
                        return
                    handler = threading.Thread(target=self._handle, args=(connection,), daemon=True)
                    self.connections.add(connection)
                    self.handlers.add(handler)
                    try:
                        handler.start()
                    except BaseException:
                        self.connections.discard(connection)
                        self.handlers.discard(handler)
                        raise
            except BaseException:
                connection.close()
                self.slots.release()
                raise

    def _handle(self, connection):
        try:
            with connection:
                deadline = time.monotonic() + TIMEOUT
                nonce = secrets.token_bytes(32)
                _send(connection, nonce, deadline)
                signature = _receive(connection, 32, deadline)
                encoded = _read_frame(connection, deadline, REQUEST_LIMIT)
                if not hmac.compare_digest(signature, _signature(self.token, nonce, encoded)):
                    return
                request_value = json.loads(encoded)
                if not isinstance(request_value, dict):
                    return
                if request_value.get('watch') == 1:
                    deadline = time.monotonic() + NOTIFICATION_WAIT + TIMEOUT
                metadata, body = self.handler(request_value)
                encoded = _json(metadata)
                signature = _signature(self.token, nonce, encoded, body)
                _send(connection, signature, deadline)
                _frame(connection, encoded, deadline)
                _frame(connection, body, deadline)
        except (OSError, EOFError, ValueError, TypeError):
            pass
        finally:
            with self.connection_lock:
                self.connections.discard(connection)
                self.handlers.discard(threading.current_thread())
            self.slots.release()

    def close(self):
        deadline = time.monotonic() + TIMEOUT
        self.closed.set()
        self.listener.close()
        with self.connection_lock:
            connections = tuple(self.connections)
            handlers = tuple(self.handlers)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        current = threading.current_thread()
        for worker in (self.thread, *handlers):
            if worker is not current:
                worker.join(max(0., deadline-time.monotonic()))
        # A newer service may already own the discovery path.
        try:
            if json.loads(self.path.read_text(encoding='utf-8')) == self.endpoint:
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        with _endpoint_lock:
            if _endpoints.get(str(self.path)) == self.endpoint:
                _endpoints.pop(str(self.path), None)


class RequestCancellation:
    """Own at most one socket; shutdown interrupts a notification read."""
    def __init__(self):
        self.lock = threading.Lock()
        self.connection = None
        self.closed = False

    def attach(self, connection):
        with self.lock:
            if self.closed:
                raise OSError('Collection request cancelled')
            self.connection = connection

    def detach(self, connection):
        with self.lock:
            if self.connection is connection:
                self.connection = None

    def close(self):
        with self.lock:
            self.closed = True
            connection = self.connection
            if connection is not None:
                try: connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                connection.close()


class PublicationWaiter:
    """One owned worker and one pending wakeup, rearmed only after a read."""
    def __init__(self, path, value):
        from multiprocessing import Pipe
        self.path = path
        self.value = value
        self.receiver, self.sender = Pipe(duplex=False)
        self.cancel = RequestCancellation()
        self.arm_event = threading.Event()
        self.arm_lock = threading.Lock()
        self.armed = False
        self.publication = None
        self.thread = threading.Thread(target=self._run, name='collection-notification', daemon=True)
        self.thread.start()

    def arm(self, publication):
        with self.arm_lock:
            if self.armed: return
            self.armed = True
            self.publication = publication
            self.arm_event.set()

    def consume(self):
        supported = self.receiver.recv()
        with self.arm_lock: self.armed = False
        return supported

    def _run(self):
        try:
            while True:
                self.arm_event.wait()
                self.arm_event.clear()
                if self.cancel.closed: return
                response = request(self.path, {**self.value, 'watch': 1,
                    'publication': self.publication}, timeout=NOTIFICATION_WAIT+TIMEOUT, cancel=self.cancel)
                if self.cancel.closed: return
                supported = response is not None and response[0].get('watch') == 1
                self.sender.send(response[0] if supported else None)
        except (OSError, EOFError):
            pass

    def close(self):
        self.cancel.close()
        self.arm_event.set()
        self.thread.join(TIMEOUT)
        if self.thread.is_alive():
            raise RuntimeError('Collection notification did not stop')
        self.receiver.close()
        self.sender.close()


def request(path, value, *, timeout=None, cancel=None):
    """Reconnect once within one deadline; discovery is cached between requests."""
    path = Path(path)
    key = str(path)
    deadline = time.monotonic() + (TIMEOUT if timeout is None else timeout)
    encoded = _json(value)
    if len(encoded) > REQUEST_LIMIT:
        raise ValueError('Collection request exceeds its limit')
    with _endpoint_lock:
        endpoint = _endpoints.get(key)
    for attempt in range(2):
        if cancel is not None and cancel.closed: break
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
                if cancel is not None: cancel.attach(connection)
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _timeout(connection, deadline)
                connection.settimeout(min(.2, connection.gettimeout()))
                connection.connect(('127.0.0.1', endpoint['port']))
                nonce = _receive(connection, 32, deadline)
                _send(connection, _signature(token, nonce, encoded), deadline)
                _frame(connection, encoded, deadline)
                signature = _receive(connection, 32, deadline)
                metadata = _read_frame(connection, deadline)
                body = _read_frame(connection, deadline)
                if not hmac.compare_digest(signature, _signature(token, nonce, metadata, body)):
                    raise ValueError('Collection response authentication failed')
                return json.loads(metadata), body
        except (OSError, EOFError, ValueError, KeyError, TypeError):
            with _endpoint_lock:
                if _endpoints.get(key) == endpoint:
                    _endpoints.pop(key, None)
            endpoint = None
            if time.monotonic() >= deadline:
                break
        finally:
            if cancel is not None and 'connection' in locals(): cancel.detach(connection)
    return None
