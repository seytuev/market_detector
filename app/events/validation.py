"""Small data contracts shared by event history and current assessments."""
from __future__ import annotations

import math

HOUR_MS = 3_600_000


def finite_number(value, *, positive: bool = False) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
        return math.isfinite(number) and (number > 0 if positive else number >= 0)
    except (TypeError, ValueError, OverflowError):
        return False


def hourly_bars(rows) -> dict[int, dict]:
    """Reject malformed OHLC, deduplicate timestamps, retain valid closed H1 bars."""
    result = {}
    for row in rows:
        try:
            start, end = int(row["start"]), int(row["end"])
            if (row.get("quality") != "ok" or row.get("closed") is False or start % HOUR_MS
                    or end != start + HOUR_MS
                    or not finite_number(row["open"], positive=True)
                    or not finite_number(row["close"], positive=True)):
                continue
            if "low" in row or "high" in row:
                low, high = row["low"], row["high"]
                if (not finite_number(low, positive=True) or not finite_number(high, positive=True)
                        or float(low) > min(float(row["open"]), float(row["close"]))
                        or float(high) < max(float(row["open"]), float(row["close"]))):
                    continue
            result[start] = row
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return result


def complete_window(rows, start: int, end: int) -> list[dict] | None:
    if start % HOUR_MS or end <= start or (end - start) % HOUR_MS:
        return None
    indexed = rows if isinstance(rows, dict) else hourly_bars(rows)
    segment = [indexed.get(t) for t in range(start, end, HOUR_MS)]
    return segment if all(b is not None for b in segment) else None
