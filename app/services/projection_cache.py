"""Bounded, single-flight caches for projections of committed inputs."""
from collections import OrderedDict
from threading import Lock, RLock
from weakref import WeakKeyDictionary


class ProjectionCache:
    def __init__(self, capacity=16):
        self.capacity = capacity
        self._stores = WeakKeyDictionary()
        self._lock = Lock()

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
    """Forming candles/quotes cannot invalidate closed-bar calculations."""
    return (db.get_domain('seq:ltf'), db.get_domain('seq:zones'),
            db.get_domain('seq:config'), repr(cfg),
            tuple((tf, c.open_time if c else None, c.close if c else None,
                   db.latest_candle_revision_at(instrument_id, tf))
                  for tf in ('H1', 'D1', 'W1')
                  for c in (db.last_candle(instrument_id, tf),)))
