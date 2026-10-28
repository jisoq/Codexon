from collections import OrderedDict
from collections.abc import ItemsView, Mapping

from .analysis_engine import retained_bytes


class PreparedQueryOwners:
    def __init__(self, engine):
        self.engine = engine
        self.cache = OrderedDict()
        self.bytes = 0
        self.sessions = _OwnerMapping(self, False)
        self.internal_sessions = _OwnerMapping(self, True)

    def cached(self, key):
        value = self.cache.get(key)
        return value[:2] if value is not None else None

    def invalidate(self, keys, *, clear=False):
        if clear:
            self.cache.clear()
            self.bytes = 0
        else:
            for key in keys:
                value = self.cache.pop(key, None)
                if value is not None:
                    self.bytes -= value[2]
        self._trim()

    def _trim(self):
        while self.cache and self.bytes > self.engine.OWNER_BYTES:
            _, value = self.cache.popitem(last=False)
            self.bytes -= value[2]

    def _state(self, key, row):
        cached = self.cached(key)
        if cached is not None:
            self.cache.move_to_end(key)
            return cached
        state, internal = self.engine._owner_state(
            key, row['metadata'], row['call_metadata'], self.engine.generation,
            row['owner_ordinal'], row['request_maps'])
        weight = retained_bytes((key, state, internal), self.engine.OWNER_BYTES)
        if weight <= self.engine.OWNER_BYTES:
            self.cache[key] = (state, internal, weight)
            self.bytes += weight
            self._trim()
        return state, internal

    def get(self, key):
        with self.engine.read_scope() as view:
            view._check()
            self._trim()
            cached = self.cached(key)
            if cached is not None:
                self.cache.move_to_end(key)
                return cached
            row = view.owner(*key)
            if row is None:
                raise KeyError(key)
            return self._state(key, row)

    def items(self, internal, *, home=None, sid=None):
        with self.engine.read_scope() as view:
            self._trim()
            for row in view.pages(view.catalog, query=True, home=home, sid=sid):
                key = row['owner']
                state, classified_internal = self._state(key, row)
                if classified_internal == internal:
                    yield key, state


class _OwnerMapping(Mapping):
    def __init__(self, owners, internal):
        self.owners = owners
        self.internal = internal

    def __getitem__(self, key):
        if not isinstance(key, tuple) or len(key) != 2 or any(type(value) is not str for value in key):
            raise KeyError(key)
        state, internal = self.owners.get(key)
        if internal != self.internal:
            raise KeyError(key)
        return state

    def __iter__(self):
        for key, _ in self.items():
            yield key

    def __len__(self):
        return sum(1 for _ in self)

    def items(self):
        return _OwnerItems(self)


class _OwnerItems(ItemsView):
    def __iter__(self):
        yield from self._mapping.owners.items(self._mapping.internal)
