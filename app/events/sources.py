"""Реальные загрузчики источников движка «События и время» (§§3, 4 ТЗ).

Цикл runner работает в потоке (worker.events_loop → asyncio.to_thread),
поэтому асинхронный адаптер Binance вызывается через asyncio.run.
Coinglass — синхронный клиент. Ошибки поднимаются наверх: качество
источников фиксирует runner, здесь ничего не выдумывается.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache
from threading import RLock

from ..adapters.base import TIMEFRAME_MS
from ..adapters.binance import BinanceSpotAdapter
from ..adapters.coinglass import CoinglassClient

_COINGLASS_CLIENT_LOCK = RLock()


def fetch_binance_pairs(settings) -> set[str]:
    """Спотовые USDT-пары Binance: {symbol} вида «BTCUSDT»."""
    async def _go() -> list:
        adapter = BinanceSpotAdapter(settings.binance_base_url)
        try:
            return await adapter.catalog()
        finally:
            await adapter.aclose()

    catalog = asyncio.run(_go())
    return {ins.symbol for ins in catalog if ins.quote_asset == "USDT"}


def fetch_prices(settings, pairs: list[str], *, start_ms: int,
                 end_ms: int) -> tuple[dict[str, list[dict]], list[str]]:
    """Часовые бары по парам корзины: (bars_by_pair, failed_pairs).

    Ошибка одной пары изолируется и попадает в failed_pairs — остальные
    участники корзины загружаются. Бары только закрытые (детекторный
    контракт адаптера), end — исключительная граница интервала.
    """
    async def _go() -> tuple[dict[str, list[dict]], list[str]]:
        adapter = BinanceSpotAdapter(settings.binance_base_url)
        bars_by_pair: dict[str, list[dict]] = {}
        failed: list[str] = []
        try:
            for pair in pairs:
                try:
                    candles = await adapter.klines(pair, "H1", start_ms, end_ms)
                except Exception:
                    failed.append(pair)
                    continue
                bars_by_pair[pair] = [
                    {
                        "start": c.open_time,
                        "end": c.open_time + TIMEFRAME_MS["H1"],
                        "open": c.open,
                        "close": c.close,
                        "high": c.high,
                        "low": c.low,
                        "quality": "ok",
                    }
                    for c in candles
                ]
        finally:
            await adapter.aclose()
        return bars_by_pair, failed

    return asyncio.run(_go())


def make_coinglass(settings) -> CoinglassClient:
    """Клиент Coinglass; без ключа — AuthError (честная недоступность)."""
    with _COINGLASS_CLIENT_LOCK:
        return _coinglass_client(settings.coinglass_api_key, settings.coinglass_base_url)


@lru_cache(maxsize=4)
def _coinglass_client(api_key: str, base_url: str) -> CoinglassClient:
    # The engine runs every minute; identical 4h/daily history can be reused
    # briefly instead of exhausting the provider quota on unchanged windows.
    return CoinglassClient(api_key, base_url, cache_seconds=300)


def fetch_liq(client: CoinglassClient, symbol: str, *, interval: str = "4h",
              limit: int = 180) -> list[dict]:
    """Агрегированные ликвидации символа на Binance (явная область источника)."""
    return client.liquidations(symbol, "Binance", interval, limit)


def fetch_oi_usd(client: CoinglassClient, symbol: str, *, limit: int = 40) -> list[dict]:
    """OI символа в USD: рыночный агрегат складывается только в USD (§3)."""
    return client.open_interest(symbol, "usd", "1d", limit)


def fetch_funding_btc(client: CoinglassClient) -> list[dict]:
    """Funding BTCUSDT на Binance (1d, ставка источника — indicative)."""
    return client.funding("Binance", "BTCUSDT", "1d", 150)
