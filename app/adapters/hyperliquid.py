"""Адаптер Hyperliquid Spot (POST {base}/info, публичный, без ключа).

Проверено реальными запросами к https://api.hyperliquid.xyz (16.09.2026):

- Каталог спота — {"type": "spotMeta"} → {"universe": [...], "tokens": [...]}.
  Элемент universe: {"tokens": [base_idx, quote_idx], "name": ..., "index": N,
  "isCanonical": bool}. Имя пары либо "BASE/QUOTE" (канонические),
  либо алиас вида "@142" — именно его требует candleSnapshot в поле coin.
- Реальные идентификаторы стартовых активов (§1, проверка перед подключением):
    BTC → "@142" (UBTC/USDC, index 142), также "@234" (UBTC/USDH)
    ETH → "@151" (UETH/USDC, index 151), также "@235" (UETH/USDH)
    SOL → "@156" (USOL/USDC, index 156)
  Пар с именами "BTC"/"ETH"/"SOL" на споте НЕТ; есть только перпы — они сюда
  не подмешиваются (§1: не подменять спот фьючерсом).
- candleSnapshot: {"type": "candleSnapshot", "req": {"coin": "@142",
  "interval": "4h", "startTime": ..., "endTime": ...}} → HTTP 200, список
  свечей {"t","T","s","i","o","h","l","c","v","n"}; цены — строки,
  t/T — ms (T = последняя ms интервала, как closeTime у Binance).
  Для coin="UBTC" (имя токена, не пары) API отвечает HTTP 500 → AdapterError.
  За один запрос отдаётся не более 5000 свечей — при упоре в предел
  выполняется пагинация по startTime.

last_price: у Hyperliquid нет публичного эндпоинта последней сделки без
адреса кошелька. Выбор: allMids → mid-цена по той же паре того же источника,
ts = момент запроса (мид не несёт собственного штампа времени). Это цена
того же venue (§1: без смешивания площадок), но НЕ last trade — ограничение
задокументировано, тип цены первичного сигнала остаётся открытым решением
§14.4 спеки.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

from ..models import Candle, Instrument, now_ms
from .base import TIMEFRAME_MS, AdapterError

# Маппинг внутренних таймфреймов на коды Hyperliquid
INTERVAL_MAP = {"H1": "1h", "H4": "4h", "D1": "1d", "W1": "1w"}

_PAGE_CAP = 5000  # предел свечей candleSnapshot за один запрос

# Удобные публичные имена для инструментов, у которых Hyperliquid использует
# технический alias в spotMeta/candleSnapshot.
SPOT_SYMBOL_ALIASES = {"HYPE": "@207"}


class HyperliquidSpotAdapter:
    """Спотовый адаптер Hyperliquid. venue='hyperliquid', market_type='spot'."""

    venue = "hyperliquid"

    def __init__(self, base_url: str, client: Optional[httpx.AsyncClient] = None):
        self._base = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self._base, timeout=30.0)
        self._own_client = client is None

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def _post(self, payload: dict) -> httpx.Response:
        try:
            resp = await self._client.post("/info", json=payload)
        except httpx.HTTPError as e:
            raise AdapterError(f"hyperliquid: сеть/таймаут при /info: {e}") from e
        if resp.status_code != 200:
            # Например HTTP 500 для несуществующего coin — явная ошибка,
            # без подмены перпом (§1)
            raise AdapterError(
                f"hyperliquid: HTTP {resp.status_code} при {payload.get('type')}: "
                f"{resp.text[:200]!r} (проверьте идентификатор спотовой пары, "
                f"например '@142' для UBTC/USDC)"
            )
        return resp

    async def catalog(self) -> list[Instrument]:
        """Проверенный спотовый каталог из spotMeta.

        symbol = имя пары из universe ("PURR/USDC" или алиас "@142") — именно
        оно используется как coin в candleSnapshot/allMids. Перпы сюда не
        попадают: отдельный type="meta" не запрашивается (§1).
        """
        resp = await self._post({"type": "spotMeta"})
        try:
            data = resp.json()
            tokens = {t["index"]: t for t in data["tokens"]}
            universe = data["universe"]
        except (ValueError, KeyError, TypeError) as e:
            raise AdapterError(
                f"hyperliquid: неожиданный формат spotMeta: {e}"
            ) from e
        instruments = []
        for u in universe:
            try:
                base_tok = tokens[u["tokens"][0]]
                quote_tok = tokens[u["tokens"][1]]
            except (KeyError, IndexError, TypeError) as e:
                raise AdapterError(
                    f"hyperliquid: битая запись universe в spotMeta: {u!r}"
                ) from e
            instruments.append(
                Instrument(
                    id=None,
                    asset=base_tok["name"],
                    venue=self.venue,
                    market_type="spot",
                    symbol=("HYPE" if u["name"] == "@207" else u["name"]),
                    quote_asset=quote_tok["name"],
                    # spotMeta не отдаёт tick size; берём szDecimals базового
                    # токена как ближайшую доступную точность (ограничение API)
                    precision=int(base_tok.get("szDecimals", 8)),
                )
            )
        return instruments

    async def klines(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int,
        include_forming: bool = False,
    ) -> list[Candle]:
        """Закрытые свечи спотовой пары через candleSnapshot.

        symbol — идентификатор пары из каталога ("@142", "PURR/USDC").
        Последняя незакрытая свеча отсекается: t + длительность интервала > now.
        """
        if timeframe not in INTERVAL_MAP:
            raise AdapterError(f"hyperliquid: неизвестный таймфрейм {timeframe!r}")
        interval = INTERVAL_MAP[timeframe]
        api_symbol = SPOT_SYMBOL_ALIASES.get(symbol, symbol)
        interval_ms = TIMEFRAME_MS[timeframe]

        raw: list[dict] = []
        cursor = start_ms
        while True:
            resp = await self._post(
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": api_symbol,
                        "interval": interval,
                        "startTime": cursor,
                        "endTime": end_ms,
                    },
                }
            )
            page = resp.json()
            if not isinstance(page, list):
                raise AdapterError(
                    f"hyperliquid: неожиданный ответ candleSnapshot: {str(page)[:200]}"
                )
            if not page:
                break
            raw.extend(page)
            if len(page) < _PAGE_CAP:
                break
            cursor = int(page[-1]["t"]) + 1  # догружаем после последней свечи

        now = now_ms()
        candles = []
        for k in raw:
            open_time = int(k["t"])
            forming = open_time + interval_ms > now
            if forming and not include_forming:
                continue  # незакрытая свеча — не отдаём детектору (§11)
            candles.append(
                Candle(
                    instrument_id=0,  # проставит вызывающий
                    timeframe=timeframe,
                    open_time=open_time,
                    close_time=int(k["T"]),  # последняя ms интервала
                    open=float(k["o"]),
                    high=float(k["h"]),
                    low=float(k["l"]),
                    close=float(k["c"]),
                    closed=not forming,
                    source=self.venue,
                )
            )
        return candles

    async def last_price(self, symbol: str) -> tuple[float, int]:
        """Mid-цена пары из allMids и штамп момента запроса.

        ВНИМАНИЕ: это mid, а не last trade — публичного эндпоинта последней
        сделки у Hyperliquid нет (см. docstring модуля, §14.4 спеки).
        """
        resp = await self._post({"type": "allMids"})
        ts = now_ms()
        mids = resp.json()
        api_symbol = SPOT_SYMBOL_ALIASES.get(symbol, symbol)
        if not isinstance(mids, dict) or api_symbol not in mids:
            raise AdapterError(
                f"hyperliquid: нет mid-цены для {symbol} в allMids"
            )
        return float(mids[api_symbol]), ts

    async def status(self) -> dict:
        """Состояние подключения: доступность /info (spotMeta) + время отклика."""
        t0 = time.monotonic()
        try:
            resp = await self._client.post(
                "/info", json={"type": "spotMeta"}, timeout=10.0
            )
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
