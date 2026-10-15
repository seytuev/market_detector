"""Share read-only BOS/SMS projections while their causal inputs are unchanged."""
from copy import deepcopy
import hashlib
import pickle

from ..engine.ltf import breaks
from .projection_cache import ProjectionCache

_EXPECTATIONS = ProjectionCache(capacity=32)


def _project(kind, pivots, candles, direction, moment, *, db, **options):
    # Quotes, forming bars and the wall clock cannot change a close-driven
    # condition. Fingerprint the actual committed input, including revisions,
    # pivot roles and IDs, rather than a global quote publication counter.
    closed = [c for c in candles if c.closed and c.close_time <= moment]
    confirmed = [p for p in pivots if p.state == 'confirmed' and p.confirmed_at <= moment]
    version = hashlib.sha256(pickle.dumps((confirmed, closed), protocol=5)).digest()
    key = (kind, direction, tuple(sorted(options.items())),
           tuple(sorted({c.instrument_id for c in closed} |
                        {p.instrument_id for p in confirmed})))
    def load():
        return getattr(breaks, kind)(confirmed, closed, direction, moment, **options)
    return deepcopy(_EXPECTATIONS.get(db, key, version, load))


def expected_structure_conditions(pivots, candles, direction, now_ms, *, db, **options):
    return _project('expected_structure_conditions', pivots, candles, direction,
                    now_ms, db=db, **options)


def expected_reverse_condition(pivots, candles, direction, now_ms, *, db, **options):
    return _project('expected_reverse_condition', pivots, candles, direction,
                    now_ms, db=db, **options)
