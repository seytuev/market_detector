"""Recheck an interpretation without erasing the liquidation event itself."""
from __future__ import annotations

from statistics import median

from .validation import HOUR_MS, hourly_bars

MODEL_VERSION = "market-context-2.2"


def price_check(basis: dict, bars_by_symbol: dict, *, now: int,
                max_age_ms: int, min_coverage: float) -> dict:
    moves_from_floor, moves_from_open = [], []
    closes = []
    target_end = now // HOUR_MS * HOUR_MS
    for symbol, anchor in basis.items():
        bars = [b for b in hourly_bars(bars_by_symbol.get(symbol, [])).values()
                if b["end"] == target_end and now - b["end"] <= max_age_ms]
        if not bars:
            continue
        last = max(bars, key=lambda b: b["end"])
        moves_from_floor.append(float(last["close"]) / anchor["min_close"] - 1)
        moves_from_open.append(float(last["close"]) / anchor["open"] - 1)
        closes.append(last["end"])
    ratio = len(closes) / len(basis) if basis else 0.0
    if not closes or ratio < min_coverage:
        return {"status": "unverified", "coverage": ratio}
    return {"status": "verified", "coverage": ratio, "as_of": min(closes),
            "below_floor": median(moves_from_floor) < 0,
            "above_origin": median(moves_from_open) >= 0}


def basis_for_window(prices: dict | None) -> dict:
    return {} if prices is None else {s: {k: p[k] for k in ("open", "min_close")}
                                     for s, p in prices.get("members", {}).items()}


def broken_since(basis: dict, bars_by_symbol: dict, *, start: int, end: int,
                 min_coverage: float) -> dict:
    """Inspect every subsequent closed H1, so a recovered price cannot hide a breach."""
    indexed = {s: hourly_bars(bars_by_symbol.get(s, [])) for s in basis}
    path_complete = True
    for close_at in range(start // HOUR_MS * HOUR_MS + HOUR_MS,
                          end // HOUR_MS * HOUR_MS + 1, HOUR_MS):
        moves = [float(indexed[s][close_at - HOUR_MS]["close"]) / a["min_close"] - 1
                 for s, a in basis.items() if close_at - HOUR_MS in indexed[s]]
        if not moves or len(moves) / len(basis) < min_coverage:
            path_complete = False
        elif median(moves) < 0:
            return {"broken_at": close_at, "path_complete": path_complete}
    return {"broken_at": None, "path_complete": path_complete}


def merge_basis(previous: dict, current: dict) -> dict:
    return {s: {"open": anchor["open"],
                "min_close": min(anchor["min_close"], current.get(s, anchor)["min_close"])}
            for s, anchor in previous.items()}


def phase_order(fact: dict) -> tuple:
    payload = fact.get("payload") or {}
    return (payload.get("phase_at") or fact["occurred_at"],
            payload.get("context_model") == MODEL_VERSION, fact["fact_key"])
