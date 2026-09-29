"""§18: контекст сценария «обновление лоя/хая = снятие SSL/BSL + тест 50% D1 FVG».

Для bear-сценария обновление лоя в причинном движении часто является снятием
SSL (low-уровень противоположной стороны) с одновременным тестом 50% бычьей
дневной FVG-зоны ниже; bull — зеркально. Эти факты не меняют структуру и
диапазон, но разрешают контекстный допуск FVG вне Premium (см. engine) и
аннотируются в сигналах.

Хранение — без новых таблиц: движок эмитит LtfEvent(kind="context_update")
с dedupe-ключом по уровню/зоне; чтение — агрегацией событий сценария
(context_flags). Событие не доставляется отдельно (нет группы в _KIND_GROUP),
его смысл показывается аннотациями entries_ready/touch.
"""
from __future__ import annotations

from typing import Any, Optional

from ...models import Candle, Direction, EventKind, ZoneStatus, ZoneType
from .liquidity import resolve_sweep
from .pivots import PivotCandidate

# статусы D1 FVG, в которых зона считается рабочей для контекста:
# WEAKENED — качественная пометка после касания 50%, зона жива (§3 HTF-спеки)
_FVG50_STATUSES = (ZoneStatus.ACTIVE, ZoneStatus.WEAKENED)
_FVG50_EVENTS = (EventKind.DEPTH_50, EventKind.FVG_WEAKENED)


def _ref(p: PivotCandidate) -> int:
    return p.pivot_id if p.pivot_id is not None else p.pivot_at


def detect_counter_sweeps(
    direction: Direction,
    pivots: list[PivotCandidate],
    candles: list[Candle],
    since_at: int,
) -> list[dict[str, Any]]:
    """Снятия уровней противоположной стороны с момента триггера сценария.

    bear → подтверждённые low-pivots (SSL), bull → high-pivots (BSL).
    Кандидат — уровень, известный к моменту триггера (confirmed_at <= since_at):
    экстремум, сформировавшийся в самом движении, прежней ликвидностью не
    является. Sweep — зеркальное применение resolve_sweep (§10): для SSL
    Low < K и Close > K на закрытой H1; берётся первое подтверждение.
    """
    bear = direction == Direction.BEAR
    kind = "low" if bear else "high"
    zone_type = "SSL" if bear else "BSL"
    out: list[dict[str, Any]] = []
    for p in pivots:
        if p.kind != kind or p.state != "confirmed":
            continue
        if p.confirmed_at is None or p.confirmed_at > since_at:
            continue
        for c in sorted(candles, key=lambda c: c.open_time):
            if not c.closed or c.open_time <= p.pivot_at:
                continue
            if c.close_time < since_at:
                continue
            if resolve_sweep(zone_type, p.price, c) == "confirmed":
                out.append({
                    "level": p.price, "level_type": zone_type,
                    "pivot_ref": _ref(p), "pivot_at": p.pivot_at,
                    "swept_at": c.close_time,
                })
                break
    return out


def find_htf_fvg50_test(
    db, instrument_id: int, extreme_price: float, direction: Direction
) -> Optional[dict[str, Any]]:
    """D1 FVG противоположного направления, чей 50%-уровень достигнут
    экстремумом движения (для bear — бычья D1 FVG ниже, extreme <= mid).

    Приоритет — зона с уже зафиксированным HTF-событием DEPTH_50/FVG_WEAKENED
    (факт теста 50% пишет общий движок); запасной вариант — чистая геометрия.
    """
    opposite = Direction.BULL if direction == Direction.BEAR else Direction.BEAR
    zones = db.get_zones(
        instrument_id=instrument_id, statuses=list(_FVG50_STATUSES),
        types=[ZoneType.FVG], timeframes={"D1"},
    )
    reached = [
        z for z in zones
        if z.direction == opposite and (
            extreme_price <= z.mid
            if direction == Direction.BEAR else extreme_price >= z.mid
        )
    ]
    reached.sort(key=lambda z: z.formed_at, reverse=True)
    for z in reached:
        if any(
            e.kind in _FVG50_EVENTS
            for e in db.get_events(zone_id=z.id, limit=100)
        ):
            return {"zone_id": z.id, "tf": z.timeframe, "via": "event"}
    if reached:
        z = reached[0]
        return {"zone_id": z.id, "tf": z.timeframe, "via": "geometry"}
    return None


def scenario_context(
    db, instrument_id: int, direction: Direction,
    pivots: list[PivotCandidate], candles: list[Candle],
    since_at: int,
) -> dict[str, Any]:
    """Агрегатор §18: снятые контр-уровни и тест 50% D1 FVG экстремумом
    движения (минимум Low / максимум High закрытых H1 от триггера)."""
    bear = direction == Direction.BEAR
    swept = detect_counter_sweeps(direction, pivots, candles, since_at)
    tail = [c for c in candles if c.closed and c.close_time >= since_at]
    fvg50 = None
    if tail:
        bar = (
            min(tail, key=lambda c: c.low)
            if bear else max(tail, key=lambda c: c.high)
        )
        extreme = bar.low if bear else bar.high
        fvg50 = find_htf_fvg50_test(db, instrument_id, extreme, direction)
        if fvg50 is not None:
            fvg50 = {**fvg50, "extreme": extreme, "tested_at": bar.close_time}
    return {"counter_swept": swept, "htf_fvg50": fvg50}


def context_flags(events: list) -> dict[str, Any]:
    """Агрегированный контекст сценария из событий context_update (§18)."""
    flags: dict[str, Any] = {"counter_swept": [], "htf_fvg50": None}
    for ev in events:
        if ev.kind != "context_update":
            continue
        if ev.payload.get("fact") == "counter_sweep":
            flags["counter_swept"].append(ev.payload)
        elif ev.payload.get("fact") == "htf_fvg50":
            flags["htf_fvg50"] = ev.payload
    return flags


def context_complete(flags: dict[str, Any]) -> bool:
    """Контекст полон: есть и снятие контр-уровня, и тест 50% D1 FVG."""
    return bool(flags["counter_swept"]) and flags["htf_fvg50"] is not None
