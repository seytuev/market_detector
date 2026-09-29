"""Тесты адаптеров рыночных данных (respx, без реальной сети).

Покрытие:
- Binance: парсинг exchangeInfo, парсинг klines, пагинация (две страницы),
  отсечение незакрытой свечи, last trade, ошибка несуществующего символа (-1121);
- Hyperliquid: парсинг spotMeta, парсинг candleSnapshot, отсечение незакрытой
  свечи, ошибка несуществующей пары (HTTP 500), last_price из allMids.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.adapters.base import AdapterError
from app.adapters.binance import BinanceSpotAdapter
from app.adapters.hyperliquid import HyperliquidSpotAdapter
from app.models import now_ms

BINANCE = "https://test.binance"
HYPERLIQUID = "https://test.hyperliquid"

H1_MS = 3_600_000


def _binance_kline(open_time: int, price: float = 100.0) -> list:
    """Строка ответа /api/v3/klines (11 полей, как у Binance)."""
    return [
        open_time,                # 0 open time
        str(price),               # 1 open
        str(price + 1),           # 2 high
        str(price - 1),           # 3 low
        str(price + 0.5),         # 4 close
        "1.0",                    # 5 volume
        open_time + H1_MS - 1,    # 6 close time (последняя ms интервала)
        "0", "0", "0", "0", "0",  # 7-11 не используются
    ]


def _hl_candle(t: int, price: float = 100.0) -> dict:
    return {
        "t": t,
        "T": t + H1_MS - 1,
        "s": "@142",
        "i": "1h",
        "o": str(price),
        "h": str(price + 1),
        "l": str(price - 1),
        "c": str(price + 0.5),
        "v": "1.0",
        "n": 10,
    }


# ---------------- Binance ----------------


async def test_binance_catalog_filters_spot_trading():
    adapter = BinanceSpotAdapter(BINANCE)
    payload = {
        "symbols": [
            {  # валидный спотовый символ
                "symbol": "BTCUSDT", "status": "TRADING",
                "isSpotTradingAllowed": True,
                "baseAsset": "BTC", "quoteAsset": "USDT", "quotePrecision": 8,
            },
            {  # не торгуется — отсекается
                "symbol": "OLDUSDT", "status": "BREAK",
                "isSpotTradingAllowed": True,
                "baseAsset": "OLD", "quoteAsset": "USDT", "quotePrecision": 8,
            },
            {  # не спот — отсекается (§1: не подменять спот)
                "symbol": "MARGINONLY", "status": "TRADING",
                "isSpotTradingAllowed": False,
                "baseAsset": "M", "quoteAsset": "USDT", "quotePrecision": 8,
            },
        ]
    }
    with respx.mock:
        respx.get(f"{BINANCE}/api/v3/exchangeInfo").respond(200, json=payload)
        catalog = await adapter.catalog()
    assert [i.symbol for i in catalog] == ["BTCUSDT"]
    ins = catalog[0]
    assert (ins.asset, ins.venue, ins.market_type, ins.quote_asset, ins.precision) == (
        "BTC", "binance", "spot", "USDT", 8,
    )
    await adapter.aclose()


async def test_binance_klines_parsing_and_unclosed_cutoff():
    """Парсинг OHLC/времён + последняя незакрытая свеча отсекается."""
    adapter = BinanceSpotAdapter(BINANCE)
    now = now_ms()
    hour_open = (now // H1_MS) * H1_MS  # открытие текущего (незакрытого) часа
    closed1 = hour_open - 2 * H1_MS
    closed2 = hour_open - H1_MS
    page = [_binance_kline(closed1, 100.0), _binance_kline(closed2, 200.0),
            _binance_kline(hour_open, 300.0)]  # час ещё идёт
    with respx.mock:
        respx.get(f"{BINANCE}/api/v3/klines").respond(200, json=page)
        candles = await adapter.klines("BTCUSDT", "H1", closed1, hour_open)
    assert len(candles) == 2  # незакрытая отсечена
    c = candles[0]
    assert (c.open_time, c.close_time) == (closed1, closed1 + H1_MS - 1)
    assert (c.open, c.high, c.low, c.close) == (100.0, 101.0, 99.0, 100.5)
    assert c.closed is True and c.source == "binance" and c.timeframe == "H1"
    assert all(x.closed and x.source == "binance" for x in candles)
    with respx.mock:
        respx.get(f"{BINANCE}/api/v3/klines").respond(200, json=page)
        forming = await adapter.klines(
            "BTCUSDT", "H1", closed1, hour_open, include_forming=True)
    assert len(forming) == 3
    assert forming[-1].closed is False and forming[-1].open_time == hour_open
    await adapter.aclose()


async def test_binance_klines_pagination_two_pages():
    """Страница ровно из 1000 свечей → запрос следующей страницы по startTime."""
    adapter = BinanceSpotAdapter(BINANCE)
    now = now_ms()
    hour_open = (now // H1_MS) * H1_MS
    t0 = hour_open - 1001 * H1_MS
    page1 = [_binance_kline(t0 + i * H1_MS, 100.0 + i) for i in range(1000)]
    page2 = [_binance_kline(t0 + 1000 * H1_MS, 1100.0),  # закрытая
             _binance_kline(hour_open, 1101.0)]          # незакрытая
    route_hits: list[int] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        route_hits.append(int(request.url.params["startTime"]))
        return httpx.Response(200, json=page1 if len(route_hits) == 1 else page2)

    with respx.mock:
        respx.get(f"{BINANCE}/api/v3/klines").mock(side_effect=side_effect)
        candles = await adapter.klines("BTCUSDT", "H1", t0, hour_open)
    assert len(route_hits) == 2
    assert route_hits[1] == t0 + 999 * H1_MS + 1  # курсор за последней свечой стр.1
    assert len(candles) == 1001  # 1000 + 1 закрытая, незакрытая отсечена
    assert candles[-1].open_time == t0 + 1000 * H1_MS
    await adapter.aclose()


async def test_binance_unknown_symbol_raises_adapter_error():
    """Код -1121 → AdapterError с именем символа, без фолбэка (§1)."""
    adapter = BinanceSpotAdapter(BINANCE)
    with respx.mock:
        respx.get(f"{BINANCE}/api/v3/klines").respond(
            400, json={"code": -1121, "msg": "Invalid symbol."}
        )
        with pytest.raises(AdapterError, match="NOSUCHPAIR"):
            await adapter.klines("NOSUCHPAIR", "H1", 0, 1)
    await adapter.aclose()


async def test_binance_last_price_is_last_trade():
    """last_price — именно последняя сделка (/trades), не тикер (§11 п.1)."""
    adapter = BinanceSpotAdapter(BINANCE)
    trade = {"id": 1, "price": "75469.47", "qty": "0.00014",
             "time": 1789581229638, "isBuyerMaker": False, "isBestMatch": True}
    with respx.mock:
        respx.get(f"{BINANCE}/api/v3/trades").respond(200, json=[trade])
        price, ts = await adapter.last_price("BTCUSDT")
    assert (price, ts) == (75469.47, 1789581229638)
    await adapter.aclose()


# ---------------- Hyperliquid ----------------

_SPOT_META = {
    "universe": [
        {"tokens": [1, 0], "name": "PURR/USDC", "index": 0, "isCanonical": True},
        {"tokens": [2, 0], "name": "@142", "index": 142, "isCanonical": False},
    ],
    "tokens": [
        {"name": "USDC", "szDecimals": 8, "weiDecimals": 8, "index": 0,
         "tokenId": "0x0", "isCanonical": True},
        {"name": "PURR", "szDecimals": 0, "weiDecimals": 5, "index": 1,
         "tokenId": "0x1", "isCanonical": True},
        {"name": "UBTC", "szDecimals": 5, "weiDecimals": 8, "index": 2,
         "tokenId": "0x2", "isCanonical": False},
    ],
}


async def test_hyperliquid_catalog_parses_spot_meta():
    adapter = HyperliquidSpotAdapter(HYPERLIQUID)
    with respx.mock:
        respx.post(f"{HYPERLIQUID}/info").respond(200, json=_SPOT_META)
        catalog = await adapter.catalog()
    assert [i.symbol for i in catalog] == ["PURR/USDC", "@142"]
    btc = catalog[1]
    assert (btc.asset, btc.venue, btc.market_type, btc.quote_asset, btc.precision) == (
        "UBTC", "hyperliquid", "spot", "USDC", 5,
    )
    await adapter.aclose()


async def test_hyperliquid_klines_parsing_and_unclosed_cutoff():
    adapter = HyperliquidSpotAdapter(HYPERLIQUID)
    now = now_ms()
    hour_open = (now // H1_MS) * H1_MS
    closed1 = hour_open - 2 * H1_MS
    closed2 = hour_open - H1_MS
    page = [_hl_candle(closed1, 100.0), _hl_candle(closed2, 200.0),
            _hl_candle(hour_open, 300.0)]
    with respx.mock:
        respx.post(f"{HYPERLIQUID}/info").respond(200, json=page)
        candles = await adapter.klines("@142", "H1", closed1, hour_open)
    assert len(candles) == 2
    c = candles[0]
    assert (c.open_time, c.close_time) == (closed1, closed1 + H1_MS - 1)
    assert (c.open, c.high, c.low, c.close) == (100.0, 101.0, 99.0, 100.5)
    assert all(x.closed and x.source == "hyperliquid" for x in candles)
    await adapter.aclose()


async def test_hyperliquid_unknown_pair_raises_adapter_error():
    """Несуществующий coin → HTTP 500 от API → AdapterError, без подмены перпом."""
    adapter = HyperliquidSpotAdapter(HYPERLIQUID)
    with respx.mock:
        respx.post(f"{HYPERLIQUID}/info").respond(500, text="null")
        with pytest.raises(AdapterError, match="hyperliquid"):
            await adapter.klines("NOSUCH", "H1", 0, 1)
    await adapter.aclose()


async def test_hyperliquid_last_price_from_allmids():
    """last_price — mid из allMids (публичного last trade у HL нет, см. docstring)."""
    adapter = HyperliquidSpotAdapter(HYPERLIQUID)
    with respx.mock:
        respx.post(f"{HYPERLIQUID}/info").respond(200, json={"@142": "75458.5"})
        price, ts = await adapter.last_price("@142")
    assert price == 75458.5
    assert ts > 0
    await adapter.aclose()


async def test_hyperliquid_last_price_unknown_symbol():
    adapter = HyperliquidSpotAdapter(HYPERLIQUID)
    with respx.mock:
        respx.post(f"{HYPERLIQUID}/info").respond(200, json={"@142": "1.0"})
        with pytest.raises(AdapterError, match="NOSUCH"):
            await adapter.last_price("NOSUCH")
    await adapter.aclose()
