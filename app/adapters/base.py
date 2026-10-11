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

from dataclasses import dataclass
from typing import Optional, Protocol

from ..models import Candle, Instrument, TIMEFRAME_MINUTES

# Длительность таймфрейма в миллисекундах (нужна для отсечения незакрытой свечи)
TIMEFRAME_MS = {tf: minutes * 60_000 for tf, minutes in TIMEFRAME_MINUTES.items()}

# Виды цены тика (правила v1 §10.1): trade — доказанная сделка того же
# источника; ticker/mid/mark/unknown — информационные цены: отображаются и
# участвуют в касаниях, но не подменяют сделку (R45)
PRICE_KINDS = ("trade", "ticker", "mid", "mark", "unknown")


@dataclass(frozen=True)
class QuoteTick:
    """Тик котировки источника (правила v1 §9/§10.1).

    Времена раздельно: event_at — время СДЕЛКИ (None, если источник его не
    сообщает — тогда время ответа за время сделки не выдаётся); received_at —
    когда ответ получен. price_kind различает доказанную сделку и
    информационные цены. source_seq — идентификатор/последовательность
    источника, если есть (дедуп по источнику)."""

    price: float
    event_at: Optional[int]
    received_at: int
    price_kind: str = "unknown"
    source_seq: Optional[str] = None

    @property
    def monotonic_at(self) -> int:
        """Время для контроля монотонности (F8/R47): доказанные сделки —
        по времени сделки (event_at), информационные цены — по времени
        получения (received_at)."""
        return self.event_at if self.event_at is not None else self.received_at

    def __iter__(self):
        """Обратная совместимость распаковки (price, ts): ts — monotonic_at."""
        yield self.price
        yield self.monotonic_at


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

    async def last_price(self, symbol: str) -> QuoteTick:
        """Тик котировки (§11 п.1, правила v1 §10.1): для активации —
        фактическая сделка той же площадки/инструмента/типа рынка, что и
        свечи (price_kind=trade, event_at — время сделки); если источник её
        не даёт — информационная цена с явным видом и event_at=None."""
        ...

    async def status(self) -> dict:
        """Состояние подключения к источнику."""
        ...
