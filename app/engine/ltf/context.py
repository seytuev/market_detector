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
from bisect import bisect_left, bisect_right

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
    ordered: bool = False,
    index=None,
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
    sample = candles
    if ordered:
        first = bisect_left(candles, since_at, key=lambda c: c.close_time)
        if only_after is not None:
            first = max(first, bisect_right(candles, only_after, key=lambda c: c.close_time))
        sample = candles[first:]
    ordered = sorted(
        (c for c in sample
         if c.closed and c.close_time >= since_at
         and (only_after is None or c.close_time > only_after)),
        key=lambda c: c.open_time,
    )
    out: list[dict[str, Any]] = []
    use_index = index is not None and len(ordered) > 8
    if use_index:
        end = bisect_right(index.rows, ordered[-1].close_time, key=lambda c: c.close_time)
        start = bisect_left(index.rows, ordered[0].close_time, key=lambda c: c.close_time)
    for p in pivots:
        if p.kind != kind or p.state != "confirmed":
            continue
        if p.confirmed_at is None or p.confirmed_at > since_at:
            continue
        if use_index:
            first = max(start, bisect_right(index.opens, p.pivot_at))
            at = index.first(first, lambda node: (
                index.low[node] < p.price and index.max_close[node] > p.price
                if bear else index.high[node] > p.price and index.min_close[node] < p.price
            ), end=end)
            if at is not None:
                out.append({"level": p.price, "level_type": zone_type,
                            "pivot_ref": _ref(p), "pivot_at": p.pivot_at,
                            "swept_at": index.rows[at].close_time})
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
    ordered: bool = False,
) -> dict[str, Any]:
    """Агрегатор §18: снятые контр-уровни и тест 50% D1 FVG экстремумом
    движения (минимум Low / максимум High закрытых H1 от триггера).
    as_of — момент решения: учитываются только факты, доступные к нему
    (L02); момент теста для причинности — закрытие свечи-экстремума.
    sweep_only_after — инкрементальный курсор сканирования снятий (см.
    detect_counter_sweeps); на экстремум/тест FVG не влияет — хвост
    считается полностью от триггера."""
    bear = direction == Direction.BEAR
    histories = getattr(db._local, 'activation_histories', None)
    history = histories.get(instrument_id) if histories is not None else None
    index = history.get_index() if history is not None else None
    head = candles[-1].close_time if candles else None
    memo = None
    if history is not None:
        if history.context_head != head or history.context_pivots is not pivots:
            history.context_head = head
            history.context_pivots = pivots
            history.context_memo.clear()
        memo = history.context_memo
    key = (direction, since_at, sweep_only_after, len(pivots),
           _ref(pivots[-1]) if pivots else None)
    if memo is not None and key in memo:
        swept = memo[key]
    else:
        swept = detect_counter_sweeps(direction, pivots, candles, since_at,
                                      only_after=sweep_only_after, ordered=ordered, index=index)
        if memo is not None:
            memo[key] = swept
    sample = candles[bisect_left(candles, since_at, key=lambda c: c.close_time):] if ordered else candles
    tail = [c for c in sample if c.closed and c.close_time >= since_at] if index is None else []
    fvg50 = None
    bar = None
    if index is not None and head is not None:
        left = bisect_left(index.rows, since_at, key=lambda c: c.close_time)
        right = bisect_right(index.rows, head, key=lambda c: c.close_time)
        if left < right:
            extreme = index.extreme(left, bear, right)
            at = index.first(left, lambda n: index.low[n] <= extreme if bear else index.high[n] >= extreme, end=right)
            bar = index.rows[at]
    elif tail:
        bar = (
            min(tail, key=lambda c: c.low)
            if bear else max(tail, key=lambda c: c.high)
        )
    if bar is not None:
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
