"""Адаптер Bybit Spot (публичный REST V5, без ключа).

Базовый URL по умолчанию — https://api.bybit.com (Settings.bybit_base_url).

Особенности Bybit V5:
- kline: /v5/market/kline?category=spot — строки [startTime, open, high, low,
  close, volume, turnover] строками, в ОБРАТНОМ порядке (новые первыми) —
  сортируем по open_time по возрастанию;
- последняя свеча может быть незакрытой (close — цена последней сделки):
  при include_forming=False отсекается (open_time + длительность ТФ > now);
- каталог: /v5/market/instruments-info с курсорной пагинацией nextPageCursor;
- ошибки: retCode != 0 → AdapterError с retMsg, без фолбэков (§1).

Время свечей — ms UTC источника (§11: календарь источника).
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from ..models import Candle, Instrument, now_ms
from .base import TIMEFRAME_MS, AdapterError

# Маппинг внутренних таймфреймов на коды Bybit V5
INTERVAL_MAP = {"H1": "60", "H4": "240", "D1": "D", "W1": "W"}

_PAGE_LIMIT = 1000  # максимум свечей/инструментов за один запрос

DEFAULT_BASE_URL = "https://api.bybit.com"


class BybitSpotAdapter:
    """Спотовый адаптер Bybit. venue='bybit', market_type='spot'."""

    venue = "bybit"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            base_url=self._base, timeout=timeout
        )
        self._own_client = client is None

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        """GET с проверкой HTTP-статуса и retCode; возвращает result."""
        return (await self._get_payload(path, params)).get("result") or {}

    async def _get_payload(self, path: str, params: dict) -> dict:
        """GET с проверкой HTTP-статуса и retCode; возвращает весь ответ."""
        try:
            resp = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise AdapterError(f"bybit: сеть/таймаут при {path}: {e}") from e
        if resp.status_code != 200:
            raise AdapterError(
                f"bybit: HTTP {resp.status_code} при {path}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise AdapterError(f"bybit: не-JSON ответ при {path}") from e
        ret_code = payload.get("retCode", 0)
        if ret_code != 0:
            raise AdapterError(
                f"bybit: retCode={ret_code} при {path}: "
                f"{payload.get('retMsg', '')}"
            )
        return payload

    async def catalog(self) -> list[Instrument]:
        """Проверенный каталог спота: status=Trading, курсорная пагинация."""
        instruments: list[Instrument] = []
        cursor: Optional[str] = None
        while True:
            params: dict = {"category": "spot", "limit": _PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            result = await self._get("/v5/market/instruments-info", params)
            rows = result.get("list", [])
            for s in rows:
                if s.get("status") != "Trading":
                    continue
                tick_size = ((s.get("priceFilter") or {}).get("tickSize") or "")
                precision = (
                    len(tick_size.rstrip("0").split(".")[1])
                    if "." in tick_size else 8
                )
                instruments.append(
                    Instrument(
                        id=None,
                        asset=s["baseCoin"],
                        venue=self.venue,
                        market_type="spot",
                        symbol=s["symbol"],
                        quote_asset=s["quoteCoin"],
                        precision=precision,
                    )
                )
            cursor = result.get("nextPageCursor") or None
            if not cursor or not rows:
                break
        return instruments

    async def klines(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int,
        include_forming: bool = False,
    ) -> list[Candle]:
        """Свечи Bybit Spot с пагинацией по start.

        Ответ приходит в обратном порядке — сортируем по open_time.
        По умолчанию последняя незакрытая свеча отсекается: open_time +
        длительность интервала > now. include_forming=True оставляет её
        с closed=False — только для графика, не для детектора (§11).
        """
        if timeframe not in INTERVAL_MAP:
            raise AdapterError(f"bybit: неизвестный таймфрейм {timeframe!r}")
        interval = INTERVAL_MAP[timeframe]
        interval_ms = TIMEFRAME_MS[timeframe]

        raw: list[list] = []
        cursor = start_ms
        while True:
            result = await self._get(
                "/v5/market/kline",
                {
                    "category": "spot",
                    "symbol": symbol,
                    "interval": interval,
                    "start": cursor,
                    "end": end_ms,
                    "limit": _PAGE_LIMIT,
                },
            )
            page = result.get("list", [])
            if not page:
                break
            raw.extend(page)
            if len(page) < _PAGE_LIMIT:
                break
            # страница полная: следующая — после самой новой свечи страницы
            cursor = max(int(k[0]) for k in page) + 1
            if cursor > end_ms:
                break

        now = now_ms()
        candles = []
        for k in sorted(raw, key=lambda r: int(r[0])):
            open_time = int(k[0])
            forming = open_time + interval_ms > now
            if forming and not include_forming:
                continue  # незакрытая свеча — не отдаём детектору (§11)
            candles.append(
                Candle(
                    instrument_id=0,  # проставит вызывающий
                    timeframe=timeframe,
                    open_time=open_time,
                    close_time=open_time + interval_ms - 1,
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
        """Цена последней сделки (lastPrice тикера того же источника) — §11 п.1."""
        payload = await self._get_payload(
            "/v5/market/tickers", {"category": "spot", "symbol": symbol}
        )
        rows = (payload.get("result") or {}).get("list", [])
        if not rows:
            raise AdapterError(f"bybit: нет тикера по {symbol}")
        ts = int(payload["time"]) if payload.get("time") else now_ms()
        return float(rows[0]["lastPrice"]), ts

    async def status(self) -> dict:
        """Состояние подключения: время сервера + время отклика."""
        t0 = time.monotonic()
        try:
            resp = await self._client.get("/v5/market/time", timeout=10.0)
            ok = resp.status_code == 200 and resp.json().get("retCode") == 0
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
