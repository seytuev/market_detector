"""§4: Orderblock — восстановление консолидации (базы) перед подтверждённым FVG.

Рабочая интерпретация критерия консолидации (§4 «Рабочая интерпретация для
проектирования», открытое решение §14.1 — пороги НЕ калиброваны):

- идём назад от первой свечи FVG и пропускаем свечи импульса — серию
  одноцветных (в направлении движения) свечей непосредственно перед FVG;
- база начинается с первой свечи противоположного импульсу цвета; одиночная
  противоположная свеча перед импульсом — допустимый OB (§4);
- база расширяется назад, пока тело очередной свечи (open..close) целиком
  внутри текущего диапазона базы: свеча, закрывшаяся за границей базы, —
  свеча выхода и в базу не включается (для медвежьего OB — ниже L,
  для бычьего — выше U); цвет свечи значения не имеет (§4: смешанный цвет
  сам по себе не разрывает группу);
- ограничение длины базы — cfg.uncalibrated_consolidation_max_candles;
- L = min(Low), U = max(High) включённых свечей.

Подтверждение OB: FVG должен быть ЗА пределами базы в направлении импульса
(§4): для бычьего OB — полностью выше U, для медвежьего — полностью ниже L.
Внутренний FVG не подтверждает (приёмка §13.2, эталон §13.19).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..config import DetectorConfig
from ..models import Candle, Direction
from .fvg import FvgRecord


@dataclass
class BaseRecord:
    """Найденная консолидация перед FVG (ещё не зона в БД)."""

    direction: Direction            # направление OB = направлению импульса/FVG
    lower: float
    upper: float
    formed_at: int                  # open_time первой (старой) свечи базы
    source_candles: list[int]       # open_time всех свечей базы, от старых к новым
    start_idx: int                  # индекс первой свечи базы в отсортированном списке
    end_idx: int                    # индекс последней свечи базы
    gap_candles: int                # свечей от конца базы до первой свечи FVG (отложенный FVG, §14.2)
    evidence: dict = field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.lower + self.upper) / 2


def _is_impulse_color(candle: Candle, direction: Direction) -> bool:
    """Свеча окрашена в направление импульса (для медвежьего OB — медвежья)."""
    return candle.is_bull if direction == Direction.BULL else not candle.is_bull


def find_base(
    candles: list[Candle], fvg: FvgRecord, cfg: DetectorConfig
) -> Optional[BaseRecord]:
    """Восстанавливает консолидацию перед FVG. None — база не найдена."""
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    idx_by_ot = {c.open_time: i for i, c in enumerate(closed)}
    i1 = idx_by_ot.get(fvg.candle_open_times[0])
    if i1 is None or i1 == 0:
        return None
    direction = fvg.direction

    # 1) пропуск свечей импульса/выхода непосредственно перед FVG
    i = i1 - 1
    skipped: list[int] = []
    while i >= 0 and _is_impulse_color(closed[i], direction):
        skipped.append(closed[i].open_time)
        i -= 1
    if i < 0:
        return None

    # 2) затравка — первая свеча противоположного цвета (одиночный OB допустим)
    end_idx = i
    members = [closed[i]]
    lo, hi = closed[i].low, closed[i].high
    stop_note: Optional[dict] = None

    # 3) расширение назад: тело свечи должно оставаться внутри диапазона базы
    i -= 1
    while i >= 0 and len(members) < cfg.uncalibrated_consolidation_max_candles:
        c = closed[i]
        body_lo, body_hi = min(c.open, c.close), max(c.open, c.close)
        if body_lo < lo or body_hi > hi:
            stop_note = {
                "open_time": c.open_time,
                "reason": "тело за пределами текущего диапазона базы — свеча выхода, не включена",
            }
            break
        members.append(c)
        lo = min(lo, c.low)
        hi = max(hi, c.high)
        i -= 1
    else:
        if len(members) >= cfg.uncalibrated_consolidation_max_candles:
            stop_note = {"reason": f"лимит длины базы {cfg.uncalibrated_consolidation_max_candles} свечей (§14.1, не калибровано)"}

    members.reverse()
    source = [c.open_time for c in members]
    evidence = {
        "rule": "§4: консолидация перед FVG, рабочая интерпретация (§14.1, пороги не калиброваны)",
        "included_open_times": source,
        "skipped_impulse_candles": skipped,
        "stop": stop_note,
        "fvg_candle_open_times": list(fvg.candle_open_times),
    }

    # §9.3 (приоритетное уточнение): тень следующей за базой импульсной свечи,
    # расширяющая экстремум основания, входит в OB — только соответствующий
    # конец (для бычьего OB — Low, для медвежьего — High), не вся свеча.
    # Кейс-образец: 164407 (нижний якорь 57800.19).
    next_candle = closed[end_idx + 1] if end_idx + 1 < len(closed) else None
    if next_candle is not None:
        if direction == Direction.BULL and next_candle.low < lo:
            evidence["boundary_anchor"] = {
                "open_time": next_candle.open_time, "side": "low",
                "original": lo, "extended": next_candle.low,
                "rule": "§9.3: нижняя тень импульсной свечи расширяет основание",
            }
            lo = next_candle.low
        elif direction == Direction.BEAR and next_candle.high > hi:
            evidence["boundary_anchor"] = {
                "open_time": next_candle.open_time, "side": "high",
                "original": hi, "extended": next_candle.high,
                "rule": "§9.3: верхняя тень импульсной свечи расширяет основание",
            }
            hi = next_candle.high

    return BaseRecord(
        direction=direction,
        lower=lo,
        upper=hi,
        formed_at=members[0].open_time,
        source_candles=source,
        start_idx=end_idx - len(members) + 1,
        end_idx=end_idx,
        gap_candles=i1 - end_idx,
        evidence=evidence,
    )


def is_external(base: BaseRecord, fvg: FvgRecord) -> bool:
    """§4: подтверждающий FVG — за пределами базы в направлении импульса.

    Бычий OB: FVG полностью выше U базы; медвежий — полностью ниже L.
    FVG внутри диапазона консолидации не подтверждает OB.
    """
    if base.direction == Direction.BULL:
        return fvg.lower > base.upper
    return fvg.upper < base.lower
