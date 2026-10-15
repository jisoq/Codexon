"""Transfer quota graphs in bounded frames without losing shared row identities.

The payload uses built-in display fields and an explicit packed-row container.
Lists, dictionaries and packed rows use stable placeholders. Small, bounded
subtrees stay inline, so an observation row needs no separate transport frame.
"""
from collections import deque
from io import BytesIO
import gc
import pickle
import time
from .quota_cycles import _PackedRows


MAGIC = 'codexon-quota-frames-1'
MAX_FRAME_BYTES = 256 * 1024
TARGET_FRAME_BYTES = 192 * 1024
INLINE_BYTES = 8 * 1024
INLINE_ITEMS = 64
BATCH_ITEMS = 128
SCALAR_BATCH_ITEMS = 24576
BATCH_CONTAINERS = 64
DECODE_YIELD_SECONDS = .004
_MISSING = object()


class TransferError(RuntimeError):
    pass


class TransferCancelled(Exception):
    pass


def _scalar_weight(value, depth=0):
    kind = type(value)
    if value is None or kind is bool:
        return 8
    if kind is int:
        return 8 + value.bit_length() // 8
    if kind is float:
        return 8
    if kind is str:
        return 8 + len(value) * 4
    if kind is bytes:
        return 8 + len(value)
    if kind is tuple and len(value) <= 8 and depth < 3:
        total = 16
        for item in value:
            weight = _scalar_weight(item, depth + 1)
            if weight is None:
                return None
            total += weight
        return total
    return None


def _inline_weight(value, depth=0, ancestors=()):
    scalar = _scalar_weight(value)
    if scalar is not None:
        return scalar
    kind = type(value)
    if (kind not in (dict, list) or len(value) > INLINE_ITEMS
            or depth >= 3 or id(value) in ancestors):
        return None
    total = 32
    entries = (item for pair in value.items() for item in pair) if kind is dict else value
    for item in entries:
        weight = _inline_weight(item, depth + 1, (*ancestors, id(value)))
        if weight is None:
            return None
        total += weight
        if total > INLINE_BYTES:
            return None
    return total


class _GraphPickler(pickle.Pickler):
    def __init__(self, stream):
        super().__init__(stream, protocol=5)
        self.references = {}
        self.pending = deque()
        self.inline = set()
        self.weights = {}

    def weight(self, value):
        if type(value) not in (dict, list):
            return _scalar_weight(value)
        identity = id(value)
        cached = self.weights.get(identity, _MISSING)
        if cached is _MISSING:
            # The source graph lives until frames() completes. Reuse only
            # depth-zero assessments, preserving nested bounds and cycles.
            cached = self.weights[identity] = _inline_weight(value)
        return cached

    def persistent_id(self, value):
        kind = type(value)
        if kind is _PackedRows:
            identity=id(value)
            reference=self.references.get(identity)
            if reference is None:
                reference=('packed_rows',len(self.references),len(value.blocks))
                self.references[identity]=reference
                self.pending.append(dict(reference=reference,source=value,
                    entries=iter(zip(value.ends,value.blocks)),offset=0))
            return reference
        if kind not in (dict, list):
            if kind not in (tuple, str, bytes, int, float, bool, type(None)):
                raise TransferError('Unsupported quota display value: ' + kind.__name__)
            return None
        identity = id(value)
        if identity in self.inline:
            return None
        reference = self.references.get(identity)
        if reference is not None:
            return reference
        if self.weight(value) is not None:
            self.inline.add(identity)
            return None
        reference = ('dict' if kind is dict else 'list', len(self.references), len(value))
        self.references[identity] = reference
        self.pending.append(dict(reference=reference, source=value,
                                 entries=iter(value.items()) if kind is dict else iter(value), offset=0))
        return reference


def frames(value):
    """One Pickler memo spans the graph; each yielded byte string is bounded."""
    stream = BytesIO()
    pickler = _GraphPickler(stream)

    def encode(frame):
        stream.seek(0)
        stream.truncate()
        pickler.dump(frame)
        payload = stream.getvalue()
        if len(payload) > MAX_FRAME_BYTES:
            raise TransferError('Quota display frame exceeds the bounded transfer size')
        return payload

    yield encode((MAGIC, 'root', value))
    active = None
    held = _MISSING
    while active is not None or pickler.pending:
        records = []
        remaining = SCALAR_BATCH_ITEMS
        containers_remaining = BATCH_ITEMS
        weight = 64
        while remaining and containers_remaining and len(records) < BATCH_CONTAINERS:
            if active is None:
                if not pickler.pending:
                    break
                active = pickler.pending.popleft()
            reference = active['reference']
            start = active['offset']
            items = []
            while remaining and containers_remaining:
                try:
                    item = held if held is not _MISSING else next(active['entries'])
                except StopIteration:
                    if active['offset'] != reference[2]:
                        raise TransferError('Quota display data changed during transfer')
                    active = None
                    break
                held = _MISSING
                values = item if reference[0] == 'dict' else (item,)
                item_weight = sum(pickler.weight(part) or 64 for part in values) + 16
                if weight + item_weight > TARGET_FRAME_BYTES and (items or records):
                    held = item
                    break
                items.append(item)
                weight += item_weight
                remaining -= 1
                # Numeric chart arrays need no new tracked containers. Keep
                # their frames useful while retaining the old allocation bound
                # for rows, nested values and forward references.
                if any(type(part) not in (str,bytes,int,float,bool,type(None)) for part in values):
                    containers_remaining -= 1
                active['offset'] += 1
            if items:
                records.append((reference[1], start, tuple(items)))
            if held is not _MISSING or weight >= TARGET_FRAME_BYTES:
                break
        if records:
            yield encode((MAGIC, 'fill', tuple(records)))
    yield encode((MAGIC, 'done', len(pickler.references)))


def send_result(connection, value):
    for payload in frames(value):
        connection.send_bytes(payload)


class _GraphUnpickler(pickle.Unpickler):
    def __init__(self, stream):
        super().__init__(stream)
        self.references = {}
        self.completed = 0

    def persistent_load(self, reference):
        if (type(reference) is not tuple or len(reference) != 3
                or reference[0] not in ('dict', 'list', 'packed_rows')
                or type(reference[1]) is not int or reference[1] < 0
                or type(reference[2]) is not int or reference[2] < 0):
            raise TransferError('Invalid quota display reference')
        kind, identifier, count = reference
        existing = self.references.get(identifier)
        if existing is None:
            value={} if kind=='dict' else _PackedRows(()) if kind=='packed_rows' else []
            existing = dict(kind=kind, count=count, received=0, value=value)
            self.references[identifier] = existing
            if not count:
                self.completed += 1
        elif (existing['kind'], existing['count']) != (kind, count):
            raise TransferError('Conflicting quota display reference')
        return existing['value']


def receive_result(connection, cancelled=lambda: False, *, statistics=None):
    """Publish complete graphs, with bounded frames and a short decode time slice."""
    stream = BytesIO()
    unpickler = _GraphUnpickler(stream)
    root = None
    started = False
    yielded = time.perf_counter()
    try:
        while True:
            while not connection.poll(.02):
                if cancelled():
                    raise TransferCancelled()
            if cancelled():
                raise TransferCancelled()
            payload = connection.recv_bytes(MAX_FRAME_BYTES)
            before = time.perf_counter()
            if gc.isenabled() and gc.get_threshold()[0]:
                # Reclaim/promote only the previous small batch before decoding
                # another one. Keep the application's GC settings unchanged.
                gc.collect(0)
            stream.seek(0)
            stream.truncate()
            stream.write(payload)
            stream.seek(0)
            frame = unpickler.load()
            if not started and type(frame) is dict:
                # Phase notifications and small test/control responses retain
                # the existing single-message protocol.
                return frame
            if type(frame) is not tuple or len(frame) != 3 or frame[0] != MAGIC:
                raise TransferError('Invalid quota display frame')
            _, kind, value = frame
            complete = False
            if kind == 'root' and not started:
                root, started = value, True
            elif kind == 'fill' and started:
                for identifier, offset, items in value:
                    target = unpickler.references.get(identifier)
                    if (target is None or not items or target['received'] != offset
                            or offset + len(items) > target['count']):
                        raise TransferError('Out-of-order quota display data')
                    if target['kind'] == 'dict':
                        target['value'].update(items)
                    elif target['kind']=='packed_rows':
                        rows=target['value']
                        for item in items:
                            if (type(item) is not tuple or len(item)!=2 or type(item[0]) is not int
                                    or item[0]<=rows.count or type(item[1]) is not bytes):
                                raise TransferError('Invalid packed quota rows')
                            end,block_bytes=item
                            rows.ends.append(end);rows.blocks.append(block_bytes);rows.count=end
                    else:
                        target['value'].extend(items)
                    target['received'] += len(items)
                    if target['received'] == target['count']:
                        actual=(len(target['value'].blocks) if target['kind']=='packed_rows' else len(target['value']))
                        if actual != target['count']:
                            raise TransferError('Incomplete quota display data')
                        unpickler.completed += 1
            elif kind == 'done' and started:
                if value != len(unpickler.references) or value != unpickler.completed:
                    raise TransferError('Incomplete quota display data')
                complete = True
            else:
                raise TransferError('Unexpected quota display frame')
            if statistics is not None:
                statistics.setdefault('decode_ms', []).append((time.perf_counter() - before) * 1000)
                statistics.setdefault('frame_bytes', []).append(len(payload))
            if complete:
                return root
            # Yielding on every tiny scalar frame lets an active GUI repeatedly
            # take a full interpreter time slice between a few decoded numbers.
            # Cancellation still runs at every frame boundary; only scheduling
            # yields are due after four milliseconds and occur at the next
            # bounded frame boundary.
            if time.perf_counter()-yielded >= DECODE_YIELD_SECONDS:
                time.sleep(0)
                yielded = time.perf_counter()
    except (pickle.UnpicklingError, ValueError, TypeError, IndexError, KeyError) as error:
        raise TransferError('Cannot decode quota display data: ' + str(error)) from error
