"""§3: FVG — чистые функции обнаружения по трём последовательным закрытым свечам.

Бычий FVG: High₁ < Low₃ (L=High₁, U=Low₃); медвежий: Low₁ > High₃ (L=High₃, U=Low₁).
Равенство границ не создаёт ненулевой FVG (сравнения строгие).
Подтверждение — только после закрытия третьей свечи: confirmed_at равен границе
следующего интервала (open_time третьей свечи + длительность ТФ).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import Candle, Direction, TIMEFRAME_MINUTES


@dataclass
class FvgRecord:
    """Запись об обнаруженном FVG (ещё не зона в БД)."""

    direction: Direction
    lower: float                    # L
    upper: float                    # U
    formed_at: int                  # open_time 3-й свечи
    confirmed_at: int               # граница следующего интервала после 3-й свечи
    candle_open_times: tuple[int, int, int]  # open_time свечей 1, 2, 3


def scan_fvgs(candles: list[Candle], timeframe: str) -> list[FvgRecord]:
    """Сканирует свечи одного таймфрейма и возвращает все подтверждённые FVG.

    Учитываются только закрытые свечи: до закрытия третьей свечи FVG
    для алгоритма не существует (§3, приёмка §13.1).
    """
    tf_ms = TIMEFRAME_MINUTES[timeframe] * 60_000
    closed = sorted((c for c in candles if c.closed), key=lambda c: c.open_time)
    out: list[FvgRecord] = []
    for i in range(2, len(closed)):
        c1, _c2, c3 = closed[i - 2], closed[i - 1], closed[i]
        if c1.high < c3.low:
            direction, lower, upper = Direction.BULL, c1.high, c3.low
        elif c1.low > c3.high:
            direction, lower, upper = Direction.BEAR, c3.high, c1.low
        else:
            continue  # равенство границ — не FVG
        out.append(
            FvgRecord(
                direction=direction,
                lower=lower,
                upper=upper,
                formed_at=c3.open_time,
                confirmed_at=c3.open_time + tf_ms,
                candle_open_times=(c1.open_time, _c2.open_time, c3.open_time),
            )
        )
    return out
