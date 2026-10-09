"""Медиана и перцентиль. Сравнение делается до округления."""
from __future__ import annotations

from decimal import Decimal
from typing import Sequence

DAY_MS = 86_400_000
RULE_VERSION = "events-1.1.1"


def D(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def median(values: Sequence[Decimal]) -> Decimal | None:
    """Для чётного n — среднее двух центральных рангов."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n % 2 == 1:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / Decimal(2)


def percentile(values: Sequence[Decimal], p: Decimal) -> Decimal | None:
    """Линейная интерполяция, индекс (n-1)*p."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    idx = Decimal(n - 1) * p
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - Decimal(lo)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def utc_day_start(ms: int) -> int:
    return ms - (ms % DAY_MS)
