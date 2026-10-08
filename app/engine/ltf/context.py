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


def _fvg_in_force_at(zone, as_of: Optional[int]) -> bool:
    """Зона могла быть контекстом в момент as_of.

    as_of=None — текущий снимок: только живые ACTIVE/WEAKENED.
    Иначе подтверждение уже наступило, а известный конец рисунка ещё нет.
    Смена статуса без display_until не доказывает, что зона была закрыта
    именно к as_of.
    """
    confirmed = zone.confirmed_at
    if confirmed is None or (as_of is not None and confirmed > as_of):
        return False
    until = zone.display_until
    if until is not None and as_of is not None and until <= as_of:
        return False
    if as_of is None and zone.status not in _FVG50_STATUSES:
        return False
    if as_of is None and until is not None:
        return False
    return True


def _ref(p: PivotCandidate) -> int:
    return p.pivot_id if p.pivot_id is not None else p.pivot_at


def detect_counter_sweeps(
    direction: Direction,
    pivots: list[PivotCandidate],
    candles: list[Candle],
    since_at: int,
    *,
    only_after: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Снятия уровней противоположной стороны с момента триггера сценария.

    bear → подтверждённые low-pivots (SSL), bull → high-pivots (BSL).
    Кандидат — уровень, известный к моменту триггера (confirmed_at <= since_at):
    экстремум, сформировавшийся в самом движении, прежней ликвидностью не
    является. Sweep — зеркальное применение resolve_sweep (§10): для SSL
    Low < K и Close > K на закрытой H1; берётся первое подтверждение.

    only_after — инкрементальный курсор: рассматриваются только свечи с
    close_time > only_after (уже просканированные закрытия новых фактов не
    дают — sweep фиксируется на первом подтверждающем закрытии, а повторы
    гасятся дедупом context:{scenario}:sweep:{pivot_ref}).
    """
    bear = direction == Direction.BEAR
    kind = "low" if bear else "high"
    zone_type = "SSL" if bear else "BSL"
    # фильтр и сортировка — один раз на вызов, а не на каждый pivot:
    # per-pivot sorted() по всей истории H1 делал replay квадратично-кубическим
    ordered = sorted(
        (c for c in candles
         if c.closed and c.close_time >= since_at
         and (only_after is None or c.close_time > only_after)),
        key=lambda c: c.open_time,
    )
    out: list[dict[str, Any]] = []
    for p in pivots:
        if p.kind != kind or p.state != "confirmed":
            continue
        if p.confirmed_at is None or p.confirmed_at > since_at:
            continue
        for c in ordered:
            if c.open_time <= p.pivot_at:
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
    db, instrument_id: int, extreme_price: float, direction: Direction,
    *, as_of: Optional[int] = None, event_time: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """D1 FVG противоположного направления, чей 50%-уровень достигнут
    экстремумом движения (для bear — бычья D1 FVG ниже, extreme <= mid).

    Приоритет — зона с уже зафиксированным HTF-событием DEPTH_50/FVG_WEAKENED
    (факт теста 50% пишет общий движок); запасной вариант — чистая геометрия.

    Причинность (L02, A01/A02): as_of — момент решения. Зона доступна только
    после confirmed_at, не после formed_at. Текущий статус не переписывает
    прошлый ответ: архив позже as_of зону на тот момент не стирает, а
    display_until <= as_of означает, что рисунок к этому моменту уже закрыт.
    event_time — момент самого теста. Геометрический fallback подтверждён
    только если подтверждение зоны уже было доступно к этому тесту.
    confirmed=False контекстное исключение §18 не включает.
    """
    opposite = Direction.BULL if direction == Direction.BEAR else Direction.BEAR
    zones = [
        z for z in db.get_zones(
            instrument_id=instrument_id, types=[ZoneType.FVG],
            timeframes={"D1"},
        )
        if _fvg_in_force_at(z, as_of)
    ]
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
            and (as_of is None or e.occurred_at <= as_of)
            and z.confirmed_at is not None
            and e.occurred_at >= z.confirmed_at
            for e in db.get_events(zone_id=z.id, limit=100)
        ):
            return {"zone_id": z.id, "tf": z.timeframe, "via": "event",
                    "confirmed": True}
    if reached:
        z = reached[0]
        # геометрический fallback: доказанным тестом считается только
        # пересечение зоны при допустимой последовательности событий —
        # зона сформирована не позднее момента теста (L02)
        confirmed = (
            event_time is not None
            and z.confirmed_at is not None
            and z.confirmed_at <= event_time
            and z.formed_at <= event_time
        )
        return {"zone_id": z.id, "tf": z.timeframe, "via": "geometry",
                "confirmed": confirmed}
    return None


def scenario_context(
    db, instrument_id: int, direction: Direction,
    pivots: list[PivotCandidate], candles: list[Candle],
    since_at: int, *, as_of: Optional[int] = None,
    sweep_only_after: Optional[int] = None,
) -> dict[str, Any]:
    """Агрегатор §18: снятые контр-уровни и тест 50% D1 FVG экстремумом
    движения (минимум Low / максимум High закрытых H1 от триггера).
    as_of — момент решения: учитываются только факты, доступные к нему
    (L02); момент теста для причинности — закрытие свечи-экстремума.
    sweep_only_after — инкрементальный курсор сканирования снятий (см.
    detect_counter_sweeps); на экстремум/тест FVG не влияет — хвост
    считается полностью от триггера."""
    bear = direction == Direction.BEAR
    swept = detect_counter_sweeps(direction, pivots, candles, since_at,
                                  only_after=sweep_only_after)
    tail = [c for c in candles if c.closed and c.close_time >= since_at]
    fvg50 = None
    if tail:
        bar = (
            min(tail, key=lambda c: c.low)
            if bear else max(tail, key=lambda c: c.high)
        )
        extreme = bar.low if bear else bar.high
        fvg50 = find_htf_fvg50_test(
            db, instrument_id, extreme, direction,
            as_of=as_of, event_time=bar.close_time,
        )
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
