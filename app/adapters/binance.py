"""Адаптер Binance Spot (публичный REST, без ключа).

Подтверждённый путь §11: /api/v3/klines для BTCUSDT, интервал 4h.
Базовый URL по умолчанию — https://data-api.binance.vision (Settings.binance_base_url).

Проверено реальными запросами (16.09.2026):
- GET /api/v3/klines?symbol=BTCUSDT&interval=4h&limit=5 → HTTP 200, 5 свечей;
- GET /api/v3/trades?symbol=BTCUSDT&limit=1 → HTTP 200, поля price/qty/time;
- несуществующий символ → HTTP 400 {"code":-1121,"msg":"Invalid symbol."}
  → AdapterError, без фолбэка на фьючерсы (§1).

Время свечей — UTC источника, параметр timeZone НЕ передаётся (§11).
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from ..models import Candle, Instrument, now_ms
from .base import TIMEFRAME_MS, AdapterError

# Маппинг внутренних таймфреймов на коды Binance
INTERVAL_MAP = {"H1": "1h", "H4": "4h", "D1": "1d", "W1": "1w"}

_PAGE_LIMIT = 1000  # максимум свечей за один запрос klines


class BinanceSpotAdapter:
    """Спотовый адаптер Binance. venue='binance', market_type='spot'."""

    venue = "binance"

    def __init__(self, base_url: str, client: Optional[httpx.AsyncClient] = None):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self._base, timeout=30.0)
        self._own_client = client is None

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict) -> httpx.Response:
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise AdapterError(f"binance: сеть/таймаут при {path}: {e}") from e
        if resp.status_code != 200:
            # Явная ошибка без фолбэка: -1121 = несуществующий спотовый символ
            try:
                body = resp.json()
                code, msg = body.get("code"), body.get("msg", "")
            except ValueError:
                code, msg = None, resp.text[:200]
            if code == -1121:
                raise AdapterError(
                    f"binance: спотовый инструмент не существует: "
                    f"{params.get('symbol')} (код -1121)"
                )
            raise AdapterError(
                f"binance: HTTP {resp.status_code} при {path}: код={code} {msg}"
            )
        return resp

    async def catalog(self) -> list[Instrument]:
        """Проверенный каталог: status=TRADING и isSpotTradingAllowed (§1)."""
        resp = await self._get("/api/v3/exchangeInfo", {})
        symbols = resp.json().get("symbols", [])
        return [
            Instrument(
                id=None,
                asset=s["baseAsset"],
                venue=self.venue,
                market_type="spot",
                symbol=s["symbol"],
                quote_asset=s["quoteAsset"],
                precision=int(s.get("quotePrecision", 8)),
            )
            for s in symbols
            if s.get("status") == "TRADING" and s.get("isSpotTradingAllowed")
        ]

    async def klines(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int,
        include_forming: bool = False,
    ) -> list[Candle]:
        """Свечи Binance Spot с пагинацией по startTime.

        По умолчанию последняя незакрытая свеча отсекается: open_time +
        длительность интервала > now. include_forming=True оставляет её
        с closed=False — только для графика, не для детектора (§11).
        """
        if timeframe not in INTERVAL_MAP:
            raise AdapterError(f"binance: неизвестный таймфрейм {timeframe!r}")
        interval = INTERVAL_MAP[timeframe]
        interval_ms = TIMEFRAME_MS[timeframe]

        raw: list[list] = []
        cursor = start_ms
        while True:
            resp = await self._get(
                "/api/v3/klines",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": _PAGE_LIMIT,
                },
            )
            page = resp.json()
            if not page:
                break
            raw.extend(page)
            if len(page) < _PAGE_LIMIT:
                break
            cursor = int(page[-1][0]) + 1  # следующая страница — после последней свечи

        now = now_ms()
        candles = []
        for k in raw:
            open_time = int(k[0])
            forming = open_time + interval_ms > now
            if forming and not include_forming:
                continue  # незакрытая свеча — не отдаём детектору (§11)
            candles.append(
                Candle(
                    instrument_id=0,  # проставит вызывающий
                    timeframe=timeframe,
                    open_time=open_time,
                    close_time=int(k[6]),  # closeTime — последняя ms интервала
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    closed=not forming,
                    source=self.venue,
                )
            )
        return candles

    async def last_price(self, symbol: str) -> tuple[float, int]:
        """Цена и время ПОСЛЕДНЕЙ СДЕЛКИ (last trade, не ticker) — §11 п.1."""
        resp = await self._get("/api/v3/trades", {"symbol": symbol, "limit": 1})
        trades = resp.json()
        if not trades:
            raise AdapterError(f"binance: нет сделок по {symbol}")
        return float(trades[-1]["price"]), int(trades[-1]["time"])

    async def status(self) -> dict:
        """Состояние подключения: ping + время отклика."""
        t0 = time.monotonic()
        try:
            resp = await self._client.get("/api/v3/ping", timeout=10.0)
            ok = resp.status_code == 200
            return {
                "venue": self.venue,
                "ok": ok,
                "http_status": resp.status_code,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        except httpx.HTTPError as e:
            return {
                "venue": self.venue,
                "ok": False,
                "error": str(e),
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
