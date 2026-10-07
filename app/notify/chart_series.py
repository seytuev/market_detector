"""Свечи для снимка графика в Telegram.

Детектор и события LTF читают только закрытые свечи. Картинка «сейчас»
дорисовывает текущий незакрытый бар: его close воркер двигает котировкой,
и последняя цена на снимке совпадает с подписью. Картинка события
(задан end_ms) остаётся на закрытых свечах — сегодняшний бар в историю
не подмешивается. Застрявший незакрытый бар прошлого периода не рисуется.
"""
from __future__ import annotations

from typing import Optional

from ..db import Database
from ..models import Candle, bar_period_contains, now_ms


def screenshot_candles(
    db: Database,
    instrument_id: int,
    timeframe: str,
    *,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
    limit: Optional[int] = None,
    now: Optional[int] = None,
) -> list[Candle]:
    """Закрытая серия и, для текущей ситуации, бар идущего периода.

    limit применяется после добавления бара, чтобы окно не отрезало
    именно его. end_ms задан — режим события, незакрытый бар не берём.
    """
    closed = db.get_candles(
        instrument_id, timeframe, start_ms=start_ms, end_ms=end_ms,
    )
    if end_ms is None:
        latest = db.last_candle(instrument_id, timeframe, closed_only=False)
        if (
            latest is not None
            and _forming_on_chart(latest, closed, timeframe, start_ms=start_ms, now=now)
        ):
            closed = [*closed, latest]
    if limit is not None and limit > 0:
        return closed[-limit:]
    return closed


def _forming_on_chart(
    bar: Optional[Candle],
    closed: list[Candle],
    timeframe: str,
    *,
    start_ms: Optional[int],
    now: Optional[int],
) -> bool:
    if bar is None or bar.closed:
        return False
    if start_ms is not None and bar.open_time < start_ms:
        return False
    if closed and bar.open_time <= closed[-1].open_time:
        return False
    as_of = now if now is not None else now_ms()
    return bar_period_contains(bar.open_time, timeframe, as_of)
