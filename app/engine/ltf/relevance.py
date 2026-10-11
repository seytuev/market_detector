"""Zone lifecycle, independent of a scenario, its range and entry signals."""
from __future__ import annotations

from dataclasses import dataclass, replace

from ...models import Direction
from .entries import (
    entry_reusable,
    fvg_filled,
    merge_test_extreme,
    test_depth_of,
    touch_bar,
)
from ..depth import reaches_depth
from .liquidity import resolve_sweep

HOUR = 3_600_000
VERSION = "htf-ideas-1"


@dataclass(frozen=True)
class ActivationCut:
    """Состояние зоны ДО независимой активации (правила v1 §7).

    None-поля — unknown: история до активации не восстанавливается причинно
    (разрыв данных, R25); unknown не блокирует, но и не считается свежим."""

    depth_before: Optional[float]
    fill_before: Optional[bool]
    extreme_before: Optional[float]


def activation_cut(db, zone, as_of: int, cfg) -> ActivationCut:
    """Причинное восстановление глубины/заполнения на отсечку as_of по
    закрытым H1 (та же машина zone_at: без проекции настоящего в прошлое).
    Разрыв истории — unknown (None), а не «свежая» зона."""
    candles = db.get_candles(zone.instrument_id, "H1", end_ms=int(as_of))
    z, meta = zone_at(zone, candles, as_of, cfg)
    if meta["data_quality"] == "gap":
        return ActivationCut(None, None, None)
    return ActivationCut(
        z.max_test_depth,
        fvg_filled(z) if z.type == "FVG" else False,
        z.test_extreme,
    )


def relevance_reason(zone, cfg, liquidity_tests=(), cut: Optional[ActivationCut] = None):
    """Запрет по актуальному lifecycle. cut — отсечка §7: свежесть для НОВОЙ
    активации оценивается по состоянию ДО неё (historically_worked), а глубина
    и заполнение в ТЕКУЩЕМ активирующем визите основание не отменяют (R17/R19).
    Без cut — прежнее поведение (максимум за жизнь)."""
    if zone.validity == "invalid":
        return "invalid"
    if zone.is_level:
        outcomes = {t.state for t in liquidity_tests if t.entry_zone_id == zone.id}
        if "confirmed" in outcomes:
            return "swept_level"
        if "failed" in outcomes:
            return "level_broken"
        reason = zone.evidence.get("lifecycle_reason")
        if reason in ("swept_level", "level_broken"):
            return reason
    if cut is not None and not zone.is_level:
        if zone.type == "FVG":
            if cut.fill_before is not None:
                return "fvg_filled" if cut.fill_before else None
            # unknown (R25) — консервативно по максимуму за жизнь, ниже
        elif cut.depth_before is not None:
            # известная отсечка: глубина ДО активации (её отсутствие — 0);
            # точное сравнение по сохранённому экстремуму, когда он был
            if (
                cut.extreme_before is not None
                and reaches_depth(zone, cut.extreme_before, cfg.entry_reuse_max_depth)
            ):
                return "historically_worked"
            return None
        # unknown (R25): известное состояние не выдумывается — прежний
        # максимум за жизнь (новым «свежим» сигналом unknown не становится)
    if fvg_filled(zone):
        return "fvg_filled"
    if not zone.is_level and zone.validity == "tested" and not entry_reusable(zone, cfg):
        return "tested_too_deep"
    return None


def zone_at(zone, candles, as_of, cfg):
    """Rebuild from closed bars only. Never project present depth into the past.

    Missing bars mean unknown freshness, not a fresh zone. Positive evidence
    of consumption remains valid even if a different part of history is absent.
    """
    evidence = dict(zone.evidence)
    for key in ("lifecycle_reason", "excluded_at"):
        evidence.pop(key, None)
    z = replace(zone, first_test_at=None, max_test_depth=0.0,
                test_extreme=None, validity="fresh", evidence=evidence)
    if zone.confirmed_at is None or zone.confirmed_at > as_of:
        return z, {"relevant": False, "reason": "unconfirmed", "data_quality": "pending", "excluded_at": None}
    start = (zone.formed_at + HOUR if zone.is_level else zone.confirmed_at)
    # Candle.close_time may be the inclusive final millisecond.
    if start % HOUR == HOUR - 1:
        start += 1
    expected = start
    gap = False
    excluded_at = None
    reason = None
    # Свечи отсортированы по open_time. Префикс до старта зоны на скан не влияет.
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid].open_time < start:
            lo = mid + 1
        else:
            hi = mid
    for c in candles[lo:]:
        if not c.closed or c.open_time < start or c.close_time > as_of:
            continue
        if c.open_time != expected:
            gap = True
        expected = c.open_time + HOUR
        if z.is_level:
            crossed = c.high >= z.lower if z.type == "BSL" else c.low <= z.lower
            outcome = resolve_sweep(z.type, z.lower, c) if crossed else None
            if outcome in ("confirmed", "failed") and reason is None:
                reason = "swept_level" if outcome == "confirmed" else "level_broken"
                z.evidence["lifecycle_reason"] = reason
            touched = touch_bar(z.lower, z.upper, c) or outcome is not None
        else:
            # A gap through the far boundary also consumes the zone.
            beyond = c.high >= z.upper if z.direction == Direction.BEAR else c.low <= z.lower
            touched = touch_bar(z.lower, z.upper, c) or beyond
            if touched:
                cur = c.low if z.direction == Direction.BULL else c.high
                z.test_extreme = merge_test_extreme(z.direction, z.test_extreme, cur)
                z.max_test_depth = test_depth_of(z, z.test_extreme)
        if touched:
            z.first_test_at = z.first_test_at if z.first_test_at is not None else c.close_time
            z.validity = "tested"
        current = relevance_reason(z, cfg)
        if current is not None and excluded_at is None:
            excluded_at = c.close_time
        reason = current or reason
    if expected + HOUR - 1 <= as_of:
        gap = True
    invalid_at = zone.evidence.get("invalidated_at")
    if zone.validity == "invalid" and (invalid_at is None or invalid_at <= as_of):
        # Legacy invalidation without a timestamp cannot prove historical validity.
        z.validity = "invalid"
        reason = "invalid"
        excluded_at = invalid_at or excluded_at
    z.evidence.update(lifecycle_reason=reason, excluded_at=excluded_at)
    return z, {"relevant": reason is None and not gap,
               "reason": reason or ("data_gap" if gap else "ok"),
               "data_quality": "gap" if gap else "complete", "excluded_at": excluded_at}
