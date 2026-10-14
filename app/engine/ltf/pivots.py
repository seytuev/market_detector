"""§5.1: локальные экстремумы (pivots) H1 — чистый детектор над свечами.

pivot_high(i): High_i строго больше l свечей слева и r свечей справа;
pivot_low(i) — зеркально. Равные пики/дно (плато) pivot не образуют —
это следует из строгих неравенств, отдельных правил plateau нет (§16.5).

Правила v1 §10.2 (R43): плато РАВНЫХ в целых шагах цены (tick) экстремумов —
единая структурная опора/ликвидность (find_plateaus): группа хранится с
уровнем, началом/концом и участниками; подтверждение — после правого окна
ПОСЛЕДНЕГО участника; новое равенство расширяет группу новой ревизией без
подтверждения задним числом. Процентный допуск не вводится.

pivot_at (open_time свечи экстремума) и confirmed_at (close_time свечи
i+r) хранятся раздельно: pivot известен только после закрытия i+r.
Свеча, одновременно являющаяся high- и low-pivot, помечается ambiguous —
порядок внутри такой свечи не выдумывается, в сигналах она не участвует.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from decimal import Decimal
from functools import lru_cache
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


@lru_cache(maxsize=65_536, typed=True)
def _price_decimal(value: float) -> Decimal:
    # Replay compares the same source prices across many prefixes. Keep the
    # exact conversion, but avoid rebuilding three Decimals per comparison.
    return Decimal(str(value))


def same_tick(a: float, b: float, tick: float) -> bool:
    """Равенство экстремумов в ЦЕЛЫХ шагах цены (§10.2): |a−b| < tick
    (один тик-шаг). Точная Decimal-арифметика; процентный допуск не
    используется."""
    if tick <= 0:
        return False
    return abs(_price_decimal(a) - _price_decimal(b)) < _price_decimal(tick)


def _plateau_candidate(closed, i, j, side, level, left, right):
    win_start, win_end = max(0, i - left), j + right
    members = closed[i:j + 1]
    if win_end >= len(closed) or not all(
        open_times_follow(a.open_time, b.open_time, members[0].timeframe)
        for a, b in zip(members, members[1:])
    ):
        return None
    outside = closed[win_start:i] + closed[j + 1:win_end + 1]
    if not all(level > c.high if side == "high" else level < c.low for c in outside):
        return None
    return PivotCandidate(
        instrument_id=closed[i].instrument_id, price=level, kind=side,
        pivot_at=closed[j].open_time, candle_open_time=closed[j].open_time,
        confirmed_at=closed[win_end].close_time, left=left, right=right,
        state="confirmed", evidence={
            "plateau": True, "level": level, "start_open": closed[i].open_time,
            "end_open": closed[j].open_time, "members": [c.open_time for c in members],
            "revision": len(members) - 1,
        },
    )


class _PlateauCursor:
    """Append-only closed history: finalized groups never change.

    A trailing group stays unconfirmed until it ends and its entire right
    window is available. StructureBatch resets this cursor on revisions.
    """

    def __init__(self, left, right, tick):
        self.params = (left, right, tick)
        self.processed = 0
        self.groups = {"high": None, "low": None}
        self.pending = {"high": deque(), "low": deque()}
        self.candidates = []
        self.by_bounds = {}

    def update(self, closed):
        left, right, tick = self.params
        if right <= 0:
            return find_plateaus(closed, left, right, tick)
        for index in range(self.processed, len(closed)):
            candle = closed[index]
            for side in ("high", "low"):
                level = getattr(candle, side)
                group = self.groups[side]
                if group is not None and same_tick(level, group[2], tick):
                    self.groups[side] = (group[0], index, group[2])
                else:
                    if group is not None and group[1] > group[0]:
                        self.pending[side].append(group)
                    self.groups[side] = (index, index, level)
                queue = self.pending[side]
                while queue and queue[0][1] + right <= index:
                    start, end, price = queue.popleft()
                    candidate = _plateau_candidate(closed, start, end, side, price, left, right)
                    if candidate is None:
                        continue
                    key = (candidate.evidence["start_open"], candidate.evidence["end_open"])
                    opposite = self.by_bounds.get(key)
                    if opposite is not None and opposite.kind != side:
                        opposite.state = candidate.state = "ambiguous"
                        opposite.evidence["both_sides"] = candidate.evidence["both_sides"] = True
                    self.by_bounds[key] = candidate
                    self.candidates.append(candidate)
        self.processed = len(closed)
        return sorted(self.candidates, key=lambda p: (p.pivot_at, p.kind))


def find_plateaus(
    closed: list[Candle], left: int, right: int, tick: float
) -> list[PivotCandidate]:
    """Плато равных в тиках экстремумов как единые опоры (§10.2, R43).

    Группа — максимальная серия ПОСЛЕДОВАТЕЛЬНЫХ свечей с тик-равным
    экстремумом (≥2 участников), строго доминирующая окно из left свечей
    слева и right свечей справа вне группы (как pivot, но группой).
    pivot_at — open_time ПОСЛЕДНЕГО участника; confirmed_at — закрытие
    свечи (последний участник + right): группа не подтверждается раньше
    доступности правого окна. Один и тот же диапазон свечей, образующий
    и high-, и low-плато, — ambiguous (порядок не выдумываем).
    """
    if tick <= 0:
        return []
    closed = sorted((c for c in closed if c.closed), key=lambda c: c.open_time)
    out: list[PivotCandidate] = []
    for side in ("high", "low"):
        i = 0
        n = len(closed)
        while i < n - 1:
            level = closed[i].high if side == "high" else closed[i].low
            j = i
            while j + 1 < n and same_tick(
                (closed[j + 1].high if side == "high" else closed[j + 1].low),
                level, tick,
            ):
                j += 1
            if j == i:
                i += 1
                continue
            candidate = _plateau_candidate(closed, i, j, side, level, left, right)
            if candidate is not None:
                out.append(candidate)
            i = j + 1
    # один диапазон — и high-, и low-плато: обе стороны ambiguous
    for a in out:
        for b in out:
            if a is not b and a.kind != b.kind and (
                a.evidence["start_open"] == b.evidence["start_open"]
                and a.evidence["end_open"] == b.evidence["end_open"]
            ):
                a.state = b.state = "ambiguous"
                a.evidence["both_sides"] = b.evidence["both_sides"] = True
    out.sort(key=lambda p: (p.pivot_at, p.kind))
    return out


def find_h1_pivots(
    candles: list[Candle], left: int, right: int, tick: float = 0
) -> list[PivotCandidate]:
    """Все локальные экстремумы по закрытым свечам, отсортированные по pivot_at.

    Свеча считается pivot только при полном правом окне (i+right существует) —
    до этого паттерн для алгоритма не существует. tick > 0 (шаг цены
    инструмента) — плато тик-равных экстремумов добавляются как опоры (§10.2);
    tick = 0 — прежнее поведение (только строгие pivots).
    """
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    out: list[PivotCandidate] = []
    for i in range(left, len(closed) - right):
        out.extend(pivot_candidates_at(closed, i, left, right))
    out.extend(find_plateaus(closed, left, right, tick))
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
