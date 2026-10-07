"""Тесты Bybit Spot адаптера (respx, без реальной сети).

Покрытие:
- klines: сортировка обратного порядка Bybit, парсинг строк в float/int ms,
  отсечение незакрытой свечи (include_forming), пагинация двух страниц,
  retCode != 0 → AdapterError;
- catalog: курсорная пагинация nextPageCursor, фильтр status=Trading;
- last_price: lastPrice тикера.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.adapters.base import AdapterError
from app.adapters.bybit import BybitSpotAdapter
from app.models import now_ms

BYBIT = "https://test.bybit"

D1_MS = 86_400_000


def _bybit_kline(open_time: int, price: float = 100.0) -> list:
    """Строка result.list /v5/market/kline (7 полей строками, как у Bybit)."""
    return [
        str(open_time),           # 0 startTime (ms, строка)
        str(price),               # 1 open
        str(price + 1),           # 2 high
        str(price - 1),           # 3 low
        str(price + 0.5),         # 4 close
        "1.0",                    # 5 volume
        "100.0",                  # 6 turnover
    ]


def _kline_payload(rows: list) -> dict:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"category": "spot", "symbol": "PUMPUSDT", "list": rows},
        "time": now_ms(),
    }


async def test_bybit_klines_sorted_ascending_and_parsed():
    """Bybit отдаёт новые свечи первыми — адаптер сортирует по open_time."""
    adapter = BybitSpotAdapter(BYBIT)
    now = now_ms()
    day_open = (now // D1_MS) * D1_MS
    closed1 = day_open - 2 * D1_MS
    closed2 = day_open - D1_MS
    # обратный порядок: новые первыми, без незакрытой
    page = [_bybit_kline(closed2, 200.0), _bybit_kline(closed1, 100.0)]
    with respx.mock:
        respx.get(f"{BYBIT}/v5/market/kline").respond(200, json=_kline_payload(page))
        candles = await adapter.klines("PUMPUSDT", "D1", closed1, day_open)
    assert [c.open_time for c in candles] == [closed1, closed2]  # по возрастанию
    c = candles[0]
    assert (c.open, c.high, c.low, c.close) == (100.0, 101.0, 99.0, 100.5)
    assert (c.open_time, c.close_time) == (closed1, closed1 + D1_MS - 1)
    assert c.closed is True and c.source == "bybit" and c.timeframe == "D1"
    await adapter.aclose()


async def test_bybit_klines_unclosed_cutoff():
    """Незакрытая последняя свеча отсекается; include_forming=True оставляет."""
    adapter = BybitSpotAdapter(BYBIT)
    now = now_ms()
    day_open = (now // D1_MS) * D1_MS
    closed1 = day_open - 2 * D1_MS
    closed2 = day_open - D1_MS
    page = [  # обратный порядок, последняя — текущий незакрытый день
        _bybit_kline(day_open, 300.0),
        _bybit_kline(closed2, 200.0),
        _bybit_kline(closed1, 100.0),
    ]
    with respx.mock:
        respx.get(f"{BYBIT}/v5/market/kline").respond(200, json=_kline_payload(page))
        candles = await adapter.klines("PUMPUSDT", "D1", closed1, day_open)
    assert len(candles) == 2  # незакрытая отсечена
    assert all(x.closed for x in candles)
    with respx.mock:
        respx.get(f"{BYBIT}/v5/market/kline").respond(200, json=_kline_payload(page))
        forming = await adapter.klines(
            "PUMPUSDT", "D1", closed1, day_open, include_forming=True)
    assert len(forming) == 3
    assert forming[-1].closed is False and forming[-1].open_time == day_open
    await adapter.aclose()


async def test_bybit_klines_pagination_two_pages():
    """Страница ровно из 1000 свечей → запрос следующей страницы по start."""
    adapter = BybitSpotAdapter(BYBIT)
    now = now_ms()
    day_open = (now // D1_MS) * D1_MS
    t0 = day_open - 1001 * D1_MS
    # Bybit отдаёт новые первыми
    page1 = [_bybit_kline(t0 + i * D1_MS, 100.0 + i) for i in range(1000)][::-1]
    page2 = [_bybit_kline(day_open, 1101.0),          # незакрытая
             _bybit_kline(t0 + 1000 * D1_MS, 1100.0)]  # закрытая
    route_hits: list[int] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        route_hits.append(int(request.url.params["start"]))
        rows = page1 if len(route_hits) == 1 else page2
        return httpx.Response(200, json=_kline_payload(rows))

    with respx.mock:
        respx.get(f"{BYBIT}/v5/market/kline").mock(side_effect=side_effect)
        candles = await adapter.klines("PUMPUSDT", "D1", t0, day_open)
    assert len(route_hits) == 2
    assert route_hits[1] == t0 + 999 * D1_MS + 1  # курсор за последней свечой стр.1
    assert len(candles) == 1001  # 1000 + 1 закрытая, незакрытая отсечена
    assert candles[-1].open_time == t0 + 1000 * D1_MS
    await adapter.aclose()


async def test_bybit_retcode_nonzero_raises_adapter_error():
    """retCode != 0 → AdapterError с retMsg, без фолбэка (§1)."""
    adapter = BybitSpotAdapter(BYBIT)
    with respx.mock:
        respx.get(f"{BYBIT}/v5/market/kline").respond(
            200, json={"retCode": 10001, "retMsg": "symbol not found",
                       "result": {}, "time": now_ms()}
        )
        with pytest.raises(AdapterError, match="symbol not found"):
            await adapter.klines("NOSUCH", "D1", 0, 1)
    await adapter.aclose()


async def test_bybit_catalog_cursor_pagination_and_trading_filter():
    """Каталог: пагинация по nextPageCursor, отбор status=Trading."""
    adapter = BybitSpotAdapter(BYBIT)
    page1 = {
        "retCode": 0, "retMsg": "OK",
        "result": {
            "category": "spot",
            "list": [
                {"symbol": "PUMPUSDT", "baseCoin": "PUMP", "quoteCoin": "USDT",
                 "status": "Trading",
                 "priceFilter": {"tickSize": "0.000001"}},
                {"symbol": "OLDUSDT", "baseCoin": "OLD", "quoteCoin": "USDT",
                 "status": "Closed",  # не торгуется — отсекается
                 "priceFilter": {"tickSize": "0.01"}},
            ],
            "nextPageCursor": "page2",
        },
        "time": now_ms(),
    }
    page2 = {
        "retCode": 0, "retMsg": "OK",
        "result": {
            "category": "spot",
            "list": [
                {"symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT",
                 "status": "Trading",
                 "priceFilter": {"tickSize": "0.01"}},
            ],
            "nextPageCursor": "",
        },
        "time": now_ms(),
    }
    cursors: list[str] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params.get("cursor", ""))
        return httpx.Response(200, json=page1 if len(cursors) == 1 else page2)

    with respx.mock:
        respx.get(f"{BYBIT}/v5/market/instruments-info").mock(side_effect=side_effect)
        catalog = await adapter.catalog()
    assert cursors == ["", "page2"]  # курсорная пагинация
    assert [i.symbol for i in catalog] == ["PUMPUSDT", "BTCUSDT"]
    pump = catalog[0]
    assert (pump.asset, pump.venue, pump.market_type, pump.quote_asset) == (
        "PUMP", "bybit", "spot", "USDT",
    )
    assert pump.precision == 6  # tickSize 0.000001
    assert catalog[1].precision == 2  # tickSize 0.01
    await adapter.aclose()


async def test_bybit_last_price_from_ticker():
    """last_price — lastPrice тикера того же источника (§11 п.1)."""
    adapter = BybitSpotAdapter(BYBIT)
    ts = now_ms()
    payload = {
        "retCode": 0, "retMsg": "OK",
        "result": {
            "category": "spot",
            "list": [{"symbol": "PUMPUSDT", "lastPrice": "0.003521",
                      "bid1Price": "0.00352", "ask1Price": "0.003522"}],
        },
        "time": ts,
    }
    with respx.mock:
        respx.get(f"{BYBIT}/v5/market/tickers").respond(200, json=payload)
        price, price_ts = await adapter.last_price("PUMPUSDT")
    assert (price, price_ts) == (0.003521, ts)
    await adapter.aclose()


async def test_bybit_http_error_raises_adapter_error():
    """HTTP-ошибка (не 200) → AdapterError."""
    adapter = BybitSpotAdapter(BYBIT)
    with respx.mock:
        respx.get(f"{BYBIT}/v5/market/kline").respond(403, text="Forbidden")
        with pytest.raises(AdapterError, match="bybit"):
            await adapter.klines("PUMPUSDT", "D1", 0, 1)
    await adapter.aclose()
