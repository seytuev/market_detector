"""Bounded, single-flight caches for projections of committed inputs."""
from collections import OrderedDict
from threading import Lock, RLock
from weakref import WeakKeyDictionary


class ProjectionCache:
    def __init__(self, capacity=16):
        self.capacity = capacity
        self._stores = WeakKeyDictionary()
        self._lock = Lock()

    def peek(self, db, key, version):
        with self._lock:
            store, _ = self._stores.get(db, ({}, {}))
            hit = store.get(key)
            if hit is not None and hit[0] == version:
                store.move_to_end(key)
                return hit[1]
        return None

    def put(self, db, key, version, value):
        with self._lock:
            store, _ = self._stores.setdefault(db, (OrderedDict(), {}))
            store[key] = (version, value)
            store.move_to_end(key)
            while len(store) > self.capacity:
                store.popitem(last=False)

    def get(self, db, key, version, loader):
        with self._lock:
            store, locks = self._stores.setdefault(db, (OrderedDict(), {}))
            lock = locks.setdefault(key, RLock())
        # Readers computing the same snapshot share one result. No DB lock is
        # held here; another instrument/database has its own cache lock.
        with lock:
            with self._lock:
                hit = store.get(key)
                if hit is not None and hit[0] == version:
                    store.move_to_end(key)
                    return hit[1]
            value = loader()
            with self._lock:
                store[key] = (version, value)
                store.move_to_end(key)
                while len(store) > self.capacity:
                    store.popitem(last=False)
            return value


def structural_version(db, instrument_id, cfg):
    """Quotes and unrelated asset switches cannot invalidate closed-bar inputs."""
    return (db.instrument_domain('seq:ltf', instrument_id), db.instrument_domain('seq:zones', instrument_id),
            repr(cfg),
            tuple((tf, c.open_time if c else None, c.close if c else None,
                   db.latest_candle_revision_at(instrument_id, tf))
                  for tf in ('H1', 'D1', 'W1')
                  for c in (db.last_candle(instrument_id, tf),)))
