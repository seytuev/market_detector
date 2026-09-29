"""Базовый контракт адаптера рыночных данных (§11 п.1 спеки).

Общие требования ко всем адаптерам:
- каталог — только проверенные СПОТОВЫЕ инструменты (§1: не подменять спот
  фьючерсом/перпом);
- свечи — только закрытые (closed=True), время — миллисекунды UTC источника,
  без timeZone-параметров (§11: календарь источника);
- у каждой свечи явный source=venue (§11 п.2: источник каждого значения явный);
- last_price — цена последней сделки того же источника, не тикер и не цена
  другой площадки (§11 п.1, §1);
- ошибки (несуществующий инструмент, недоступность источника) — явные
  AdapterError, без молчаливых фолбэков.
"""
from __future__ import annotations

from typing import Protocol

from ..models import Candle, Instrument, TIMEFRAME_MINUTES

# Длительность таймфрейма в миллисекундах (нужна для отсечения незакрытой свечи)
TIMEFRAME_MS = {tf: minutes * 60_000 for tf, minutes in TIMEFRAME_MINUTES.items()}


class AdapterError(Exception):
    """Явная ошибка адаптера: несуществующий инструмент, недоступность
    источника, неожиданный формат ответа. Фолбэков на другие рынки нет (§1)."""


class MarketDataAdapter(Protocol):
    """Интерфейс источника рыночных данных (§11 п.1)."""

    venue: str

    async def catalog(self) -> list[Instrument]:
        """Проверенный каталог спотовых инструментов источника."""
        ...

    async def klines(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int,
        include_forming: bool = False,
    ) -> list[Candle]:
        """Свечи источника: closed=True, source=venue, время UTC.

        По умолчанию незакрытый текущий бар отсекается. include_forming=True
        оставляет его с closed=False — только для графика.
        timeframe — 'H1' | 'H4' | 'D1' | 'W1'.
        """
        ...

    async def last_price(self, symbol: str) -> tuple[float, int]:
        """(цена последней сделки того же источника, ts ms) — §11 п.1."""
        ...

    async def status(self) -> dict:
        """Состояние подключения к источнику."""
        ...
