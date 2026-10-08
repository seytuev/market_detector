"""§5.1: локальные экстремумы (pivots) H1 — чистый детектор над свечами.

pivot_high(i): High_i строго больше l свечей слева и r свечей справа;
pivot_low(i) — зеркально. Равные пики/дно (плато) pivot не образуют —
это следует из строгих неравенств, отдельных правил plateau нет (§16.5).

pivot_at (open_time свечи экстремума) и confirmed_at (close_time свечи
i+r) хранятся раздельно: pivot известен только после закрытия i+r.
Свеча, одновременно являющаяся high- и low-pivot, помечается ambiguous —
порядок внутри такой свечи не выдумывается, в сигналах она не участвует.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ...models import Candle, open_times_follow


@dataclass
class PivotCandidate:
    """Найденный экстремум (ещё не обязательно строка в БД).

    state: "confirmed" — правое окно из r закрытых свечей есть (доступность
    по времени проверяется отдельно через confirmed_pivots); "ambiguous" —
    свеча одновременно high- и low-pivot. pivot_id/role заполняет слой БД.
    """

    instrument_id: int
    price: float
    kind: str                      # high | low
    pivot_at: int                  # ms: open_time свечи экстремума
    candle_open_time: int          # ms: та же свеча-якорь
    confirmed_at: int              # ms: close_time свечи i+right
    left: int
    right: int
    state: str = "confirmed"       # confirmed | ambiguous
    pivot_id: Optional[int] = None  # id в ltf_pivot после материализации
    role: str = "none"             # HH | HL | LH | LL | internal_* | none
    evidence: dict[str, Any] = field(default_factory=dict)


def pivot_candidates_at(
    closed: list[Candle], i: int, left: int, right: int
) -> list[PivotCandidate]:
    """Кандидаты (0–2) на позиции i по уже отсортированным закрытым свечам.

    Вынесено из find_h1_pivots для инкрементального расчёта (StructureBatch):
    новая закрытая свеча на хвосте создаёт не более одной новой позиции-
    кандидата (i = len-1-right), окна остальных позиций не меняются.
    """
    c = closed[i]
    span = closed[i - left : i + 1 + right]
    if any(
        not open_times_follow(span[j].open_time, span[j + 1].open_time, c.timeframe)
        for j in range(len(span) - 1)
    ):
        return []
    window = closed[i - left : i] + closed[i + 1 : i + 1 + right]
    is_high = all(c.high > w.high for w in window)
    is_low = all(c.low < w.low for w in window)
    if not (is_high or is_low):
        return []
    confirmed_at = closed[i + right].close_time
    kinds = (["high", "low"] if is_high and is_low else
             ["high"] if is_high else ["low"])
    out = []
    for kind in kinds:
        # одна свеча — и high, и low pivot: оба факта храним, но помечаем
        # ambiguous и в сигналах не используем (§5.1)
        state = "ambiguous" if len(kinds) == 2 else "confirmed"
        out.append(
            PivotCandidate(
                instrument_id=c.instrument_id,
                price=c.high if kind == "high" else c.low,
                kind=kind,
                pivot_at=c.open_time,
                candle_open_time=c.open_time,
                confirmed_at=confirmed_at,
                left=left,
                right=right,
                state=state,
                evidence={"both_sides": len(kinds) == 2},
            )
        )
    return out


def find_h1_pivots(
    candles: list[Candle], left: int, right: int
) -> list[PivotCandidate]:
    """Все локальные экстремумы по закрытым свечам, отсортированные по pivot_at.

    Свеча считается pivot только при полном правом окне (i+right существует) —
    до этого паттерн для алгоритма не существует.
    """
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    out: list[PivotCandidate] = []
    for i in range(left, len(closed) - right):
        out.extend(pivot_candidates_at(closed, i, left, right))
    out.sort(key=lambda p: (p.pivot_at, p.kind))
    return out


def confirmed_pivots(
    pivots: list[PivotCandidate], now_ms: int
) -> list[PivotCandidate]:
    """Pivots, доступные алгоритму на момент now_ms.

    Кандидат (confirmed_at в будущем) и ambiguous в сигналах не участвуют
    (§5.1, §3.5, приёмка п.6).
    """
    return [
        p for p in pivots
        if p.state == "confirmed" and p.confirmed_at <= now_ms
    ]
