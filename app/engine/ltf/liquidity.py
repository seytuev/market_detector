"""§10: двухэтапное снятие BSL/SSL — снятие и закрытие той же H1-свечи.

BSL_sweep_confirmed = High_s > K AND Close_s < K; SSL зеркально.
Close == K — неподтверждённый исход (equal_close, §10); равное касание без
превышения (High == K) снятием не является. Одной тени до закрытия
недостаточно: чистая функция вызывается только на закрытой свече.
"""
from __future__ import annotations

from typing import Optional

from ...models import Candle


def resolve_sweep(zone_type: str, level: float, candle: Candle) -> Optional[str]:
    """Исход liquidity-теста на закрытии свечи.

    Возвращает "confirmed" | "failed" | "equal_close"; None — свеча не
    закрыта, исхода ещё нет (тест остаётся awaiting_close).
    """
    if not candle.closed:
        return None
    if zone_type == "BSL":
        if candle.close == level or candle.high == level:
            return "equal_close"
        # возврат под уровень на той же свече — подтверждённое снятие
        return "confirmed" if (candle.high > level and candle.close < level) else "failed"
    # SSL зеркально
    if candle.close == level or candle.low == level:
        return "equal_close"
    return "confirmed" if (candle.low < level and candle.close > level) else "failed"
