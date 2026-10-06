"""§7: диапазон Premium/Discount — чистые функции.

bear: последняя связанная пара подтверждённых LH→LL; bull — зеркальная HL→HH.
Связанная — непосредственно предшествующий high/low в последовательности
pivots: глобальный HH вместо отсутствующего LH молча не подставляется
(§7, приёмка п.5). При R_high <= R_low диапазон не строится (None →
сценарий остаётся в range_pending, запасного якоря спека не определяет).

Опоры диапазона подтверждаются тремя закрытыми свечами справа (§2, §5.1):
входные pivots должны быть получены find_h1_pivots(candles, 3, 3) — отдельно
от структурного профиля l/r, если тот изменён.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ...models import Direction
from .pivots import PivotCandidate

# §5.1: для новых границ Premium/Discount — ровно три закрытые свечи справа
RANGE_PIVOT_LEFT = 3
RANGE_PIVOT_RIGHT = 3


@dataclass
class RangeDraft:
    """Версия диапазона до записи в БД (слой БД создаёт LtfRange с version+1)."""

    direction: Direction
    lower: float                     # R_low
    upper: float                     # R_high
    mid: float                       # M = (R_high + R_low) / 2
    anchor_low_ref: Optional[int]    # pivot_id опоры минимума (или pivot_at до БД)
    anchor_high_ref: Optional[int]
    available_at: int                # ms: подтверждение младшей опоры пары
    # §7 (Этап 4): continuation — связанная пара LH→LL / HL→HH;
    # origin_reversal — от якоря-источника причинного движения первичного слома
    kind: str = "continuation"
    evidence: dict[str, Any] = field(default_factory=dict)


def _ref(p: PivotCandidate) -> int:
    return p.pivot_id if p.pivot_id is not None else p.pivot_at


def current_range(
    pivots: list[PivotCandidate], direction: Direction, now_ms: int
) -> Optional[RangeDraft]:
    """Действующий диапазон по подтверждённым на now_ms опорам.

    Новый LL/HH до подтверждения (3 правые свечи) — кандидат: диапазон
    не меняется (§7, приёмка п.6). Если последний экстремум направления не
    образует связанную пару (например, перед LL стоит HH) — None.
    """
    ps = sorted(
        (
            p for p in pivots
            if p.state == "confirmed" and p.confirmed_at <= now_ms
        ),
        key=lambda p: p.pivot_at,
    )
    if direction == Direction.BEAR:
        anchor_kind, ref_kind = "low", "high"
        anchor_role, ref_role = "LL", "LH"
    else:
        anchor_kind, ref_kind = "high", "low"
        anchor_role, ref_role = "HH", "HL"

    # последний подтверждённый экстремум направления (LL для bear, HH для bull)
    anchor: Optional[PivotCandidate] = None
    anchor_idx = -1
    for i in range(len(ps) - 1, -1, -1):
        if ps[i].kind == anchor_kind and ps[i].role == anchor_role:
            anchor, anchor_idx = ps[i], i
            break
    if anchor is None:
        return None
    # связанная опора — ближайший противоположный pivot перед якорем;
    # подмена глобальным HH/LL вместо LH/HL запрещена (§7)
    ref: Optional[PivotCandidate] = None
    for i in range(anchor_idx - 1, -1, -1):
        if ps[i].kind == ref_kind:
            ref = ps[i]
            break
    if ref is None or ref.role != ref_role:
        return None
    low, high = (anchor.price, ref.price) if direction == Direction.BEAR else (
        ref.price, anchor.price
    )
    if high <= low:
        return None
    return RangeDraft(
        direction=direction,
        lower=low,
        upper=high,
        mid=(low + high) / 2,
        anchor_low_ref=(
            _ref(anchor) if direction == Direction.BEAR else _ref(ref)
        ),
        anchor_high_ref=(
            _ref(ref) if direction == Direction.BEAR else _ref(anchor)
        ),
        available_at=max(anchor.confirmed_at, ref.confirmed_at),
        evidence={
            "anchor_role": anchor_role, "ref_role": ref_role,
            "anchor_pivot_at": anchor.pivot_at, "ref_pivot_at": ref.pivot_at,
        },
    )


def origin_reversal_range(
    pivots: list[PivotCandidate],
    movement_start_ref: int,
    direction: Direction,
    now_ms: int,
) -> Optional[RangeDraft]:
    """§7 (Этап 4): диапазон от якоря-источника разворотного движения.

    Стартовый якорь — start-pivot причинного движения ПЕРВИЧНОГО слома
    (для bear это HH прежней восходящей структуры; роль НЕ переименовывается
    в LH, чтобы пройти фильтр связанной пары). Конечный якорь —
    подтверждённый (теми же 3 правыми закрытыми свечами) экстремум движения:
    для bear — минимальный low-pivot от start, для bull — максимальный
    high-pivot (при равной цене — более поздний по pivot_at). Произвольный
    экстремум окна («максимальный хай видимых дней») не подставляется:
    start приходит только из ltf_movement.origin.

    Оба якоря должны быть подтверждены на now_ms, иначе None → range_pending
    (предварительного торгуемого диапазона нет). Диапазон, подтверждённый
    до самого BOS, — не ошибка: важна принадлежность якорей движению слома.
    """
    ps = sorted(
        (
            p for p in pivots
            if p.state == "confirmed" and p.confirmed_at <= now_ms
        ),
        key=lambda p: p.pivot_at,
    )
    start = next((p for p in ps if _ref(p) == movement_start_ref), None)
    if start is None:
        return None
    bear = direction == Direction.BEAR
    end_kind = "low" if bear else "high"
    cands = [
        p for p in ps
        if p.kind == end_kind and p.pivot_at >= start.pivot_at
    ]
    if not cands:
        return None
    if bear:
        end = min(cands, key=lambda p: (p.price, -p.pivot_at))
    else:
        end = max(cands, key=lambda p: (p.price, p.pivot_at))
    low, high = (end.price, start.price) if bear else (start.price, end.price)
    if high <= low:
        return None
    return RangeDraft(
        direction=direction,
        lower=low,
        upper=high,
        mid=(low + high) / 2,
        anchor_low_ref=_ref(end) if bear else _ref(start),
        anchor_high_ref=_ref(start) if bear else _ref(end),
        available_at=max(start.confirmed_at, end.confirmed_at),
        kind="origin_reversal",
        evidence={
            "range_kind": "origin_reversal",
            # фактические роли: start может быть HH/LL прежней структуры
            "anchor_role": start.role, "ref_role": end.role,
            "anchor_pivot_at": start.pivot_at, "ref_pivot_at": end.pivot_at,
        },
    )


def range_recalc(
    prev: Optional[RangeDraft],
    pivots: list[PivotCandidate],
    direction: Direction,
    now_ms: int,
    *,
    origin_start_ref: Optional[int] = None,
    anchor_policy: str = "continuation_only",
) -> Optional[RangeDraft]:
    """Пересчёт после подтверждения новой опоры (§7).

    Возвращает новый RangeDraft, только если действующая пара изменилась;
    None — диапазон прежний (или пары всё ещё нет → range_pending).
    Старые версии не переписываются: запись LtfRange(version+1,
    prev_version_id) — на слое БД.

    §7 (Этап 4): при anchor_policy='origin_reversal' и отсутствии валидной
    continuation-пары диапазон строится от якоря-источника причинного
    движения первичного слома (kind='origin_reversal'). Переход возможен
    только origin_reversal → continuation (новая версия при подтверждении
    первой валидной пары); обратно continuation → origin_reversal — никогда.
    """
    cur = current_range(pivots, direction, now_ms)
    if (
        cur is None
        and anchor_policy == "origin_reversal"
        and origin_start_ref is not None
        and (prev is None or prev.kind != "continuation")
    ):
        cur = origin_reversal_range(pivots, origin_start_ref, direction, now_ms)
    if cur is None:
        return None
    if (
        prev is not None
        and prev.anchor_low_ref == cur.anchor_low_ref
        and prev.anchor_high_ref == cur.anchor_high_ref
        and prev.lower == cur.lower
        and prev.upper == cur.upper
    ):
        return None
    return cur


def provisional_range(
    pivots: list[PivotCandidate],
    candles: list,
    direction: Direction,
    now_ms: int,
) -> Optional[RangeDraft]:
    """§16.2 (предлагаемый режим): предварительный диапазон — временный конец
    ТЕКУЩЕГО структурного движения до подтверждения опоры тремя правыми
    закрытыми свечами.

    Начало — последний подтверждённый структурный pivot текущего движения
    (LH для bear, HL для bull), НЕ произвольный максимум/минимум окна;
    конец — текущий неподтверждённый экстремум движения (минимум low для
    bear / максимум high для bull по закрытым свечам после начала движения).

    Чистый read-only расчёт: ничего не пишет в ltf_range, не участвует в
    eligibility, отмене сценария и уведомлениях. Подтверждённый расчёт
    (current_range/range_recalc) остаётся основным. range_status —
    "provisional" в evidence; сторона неподтверждённого конца имеет
    anchor_ref=None. None — движения без предварительного конца нет
    (экстремум уже подтверждён, геометрия невалидна или опоры движения ещё нет).
    """
    ps = sorted(
        (
            p for p in pivots
            if p.state == "confirmed" and p.confirmed_at <= now_ms
        ),
        key=lambda p: p.pivot_at,
    )
    bear = direction == Direction.BEAR
    ref_role = "LH" if bear else "HL"
    extreme_kind = "low" if bear else "high"
    # начало движения — последний подтверждённый LH/HL (связь со структурой
    # обязательна, §16.2: «не заменять произвольным максимумом/минимумом окна»)
    ref: Optional[PivotCandidate] = None
    for p in reversed(ps):
        if p.role == ref_role:
            ref = p
            break
    if ref is None:
        return None
    tail = [c for c in candles if c.closed and c.open_time > ref.pivot_at]
    if not tail:
        return None
    if bear:
        bar = min(tail, key=lambda c: c.low)
        extreme = bar.low
    else:
        bar = max(tail, key=lambda c: c.high)
        extreme = bar.high
    # геометрия диапазона должна быть валидной (как в current_range)
    if bear and not (extreme < ref.price):
        return None
    if not bear and not (extreme > ref.price):
        return None
    # экстремум уже подтверждён тремя правыми свечами — его покрывает
    # обычный подтверждённый расчёт, предварительный слой не дублирует
    if any(
        p.kind == extreme_kind and p.pivot_at == bar.open_time
        for p in ps
    ):
        return None
    low, high = (extreme, ref.price) if bear else (ref.price, extreme)
    right_closed = sum(1 for c in tail if c.open_time > bar.open_time)
    return RangeDraft(
        direction=direction,
        lower=low,
        upper=high,
        mid=(low + high) / 2,
        # неподтверждённый конец якоря не имеет (пока нет pivot в БД)
        anchor_low_ref=None if bear else _ref(ref),
        anchor_high_ref=_ref(ref) if bear else None,
        available_at=now_ms,  # момент расчёта, НЕ подтверждение опоры
        evidence={
            "range_status": "provisional",
            "ref_role": ref_role,
            "ref_pivot_at": ref.pivot_at,
            "ref_price": ref.price,
            "extreme_candle_open_time": bar.open_time,
            "right_candles_closed": right_closed,
        },
    )


def target_half(rng: RangeDraft, direction: Direction) -> tuple[float, float]:
    """Целевая половина диапазона: bear → Premium [M; R_high],
    bull → Discount [R_low; M]. Обе половины включают M (§8.5)."""
    if direction == Direction.BEAR:
        return rng.mid, rng.upper
    return rng.lower, rng.mid


def eligible_overlap(
    zone_lower: float, zone_upper: float, half_low: float, half_high: float
) -> bool:
    """§8.5: достаточно частичного пересечения; равенство границе (в т.ч. M)
    считается попаданием."""
    return max(zone_lower, half_low) <= min(zone_upper, half_high)


def level_in_half(level: float, half_low: float, half_high: float) -> bool:
    """§8.5: для уровня K — Lt <= K <= Ut (равенство — попадание)."""
    return half_low <= level <= half_high


def zone_half(
    lower: float, upper: float, is_level: bool, rng, direction: str
) -> str:
    """§18: в какой половине диапазона зона: premium | discount | none.

    rng — любой объект с lower/upper/mid (RangeDraft или LtfRange);
    None — диапазона нет. Зона, пересекающая середину, получает метку
    целевой половины сценария (как §3.4 веб-таблицы)."""
    if rng is None:
        return "none"
    mid = rng.mid
    if is_level:
        if mid <= lower <= rng.upper:
            return "premium"
        if rng.lower <= lower <= mid:
            return "discount"
        return "none"
    in_premium = max(lower, mid) <= min(upper, rng.upper)
    in_discount = max(lower, rng.lower) <= min(upper, mid)
    if in_premium and in_discount:
        return "premium" if direction == "bear" else "discount"
    if in_premium:
        return "premium"
    if in_discount:
        return "discount"
    return "none"
